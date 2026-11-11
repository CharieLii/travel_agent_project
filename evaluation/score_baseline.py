"""Summarize baseline outputs offline without calling or repairing the model."""

import argparse
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from route_scoring import evaluate_day
from hard_scoring import score_required_items

DATA = Path(__file__).resolve().parent.parent / "dataset"
MODES = (None, "walk", "transit")
PREFERENCE_THRESHOLD_PERCENT = 60
COMFORT_LIMITS_MINUTES = {"轻松": (45, 90), "适宜": (75, 120), "充实": (105, 150)}


def repetition_metric(activities):
    counts = Counter(activity["place_id"] for activity in activities)
    repeated = {place_id: count for place_id, count in counts.items() if count > 1}
    return {
        "repeated_places": len(repeated),
        "unique_places": len(counts),
        "rate": len(repeated) / len(counts) if counts else None,
        "places": [{"place_id": place_id, "visits": count}
                   for place_id, count in repeated.items()],
    }


def final_values(report):
    """Return metric values with reasons for unavailable or partially unknown scores."""
    result = {"case_id": report["case_id"]}
    def metric(value, reason=None):
        return {"数值": value, "原因": reason}
    unavailable = report.get("unavailable_reason") or "没有可评分行程"
    result["输出格式通过"] = metric("1/1" if report["format_valid"] else "0/1")
    result["生成行程"] = metric("1/1" if report["format_valid"] and report["output_status"] == "plan" else "0/1")
    no_items = {"目的地": "用户未指定目的地", "到离时间": "用户未指定抵达或离开时间",
                "必游景点": "用户未指定可匹配的必游景点", "景点开放": "没有需要检查的景点",
                "指定住宿": "用户未指定具体住宿"}
    for name, metric in report["required_metrics"].items():
        if metric and metric["total"] and not metric["unknown"]:
            result[name] = {"数值": f"{metric['passed']}/{metric['total']}", "原因": None}
        else:
            unknown = [str(item['evidence']) for item in (metric or {}).get('items', [])
                       if item.get('score') is None]
            reason = "；".join(dict.fromkeys(unknown)) if unknown else (
                unavailable if report.get("unavailable_reason") else no_items[name])
            result[name] = {"数值": None, "原因": reason}

    preferences = report["preference_metrics"]
    for name in ("景点类型偏好", "舒适度偏好"):
        metric = preferences[name]
        value = f"{metric['passed']}/{metric['total']}" if metric and metric.get("rate") is not None else None
        reason = None if value is not None else ((metric or {}).get("reason") or unavailable)
        if name == "舒适度偏好" and metric and metric.get("days"):
            details = [f"第{d['day']}天：{d['reason']}" for d in metric['days'] if d.get('reason')]
            if details and metric.get('preference'):
                reason = "；".join(details)
        result[name] = {"数值": value, "原因": reason}
    route = preferences["顺路达标率"]
    days = (route or {}).get("days", [])
    feasible = sum(day.get("transfer_feasible") is True for day in days)
    feasibility_reasons = [f"第{index}天：{day.get('transfer_reason') or '转场信息不足'}"
                           for index, day in enumerate(days, 1)
                           if day.get("transfer_feasible") is None]
    result["行程可行率"] = {
        "数值": f"{feasible}/{len(days)}" if days else None,
        "原因": "；".join(feasibility_reasons) if feasibility_reasons else (unavailable if not days else None),
    }
    value = f"{route['passed']}/{route['total_days']}" if route and route.get("total_days") else None
    reasons = []
    for index, day in enumerate((route or {}).get("days", []), 1):
        if day.get("transfer_feasible") is None:
            reasons.append(f"第{index}天：{day.get('transfer_reason') or '转场信息不足'}")
        elif day.get("transfer_feasible") is True and day.get("route_extra_seconds") is None:
            reasons.append(f"第{index}天：{day.get('reason') or '无法比较参考路线'}")
    result["顺路达标率"] = {"数值": value, "原因": "；".join(reasons) if reasons else (unavailable if value is None else None)}
    repetition = report["景点重复率"]
    value = f"{repetition['repeated_places']}/{repetition['unique_places']}" if repetition and repetition["unique_places"] else None
    result["景点重复率"] = {"数值": value, "原因": None if value is not None else (unavailable if report.get("unavailable_reason") else "没有可统计的景点")}
    return result


def summarize_scores(cases):
    """Report denominators explicitly, including failed requests and invalid outputs."""
    def fraction(name):
        values = [case[name].get("数值") for case in cases if isinstance(case.get(name), dict) and case[name].get("数值") is not None]
        pairs = [tuple(map(int, value.split('/'))) for value in values]
        return f"{sum(a for a, _ in pairs)}/{sum(b for _, b in pairs)}" if pairs else None
    return {"输出格式通过率": fraction("输出格式通过"),
            "生成行程率": fraction("生成行程"),
            "行程可行率": fraction("行程可行率")}


def clock_minutes(value):
    if value == "24:00":
        return 1440
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        return None
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def preference_coverage(activities, preferred, types_by_place):
    """Return per-tag minutes and total visit minutes, or None if unscorable."""
    if not preferred or not activities:
        return None
    matched = dict.fromkeys(preferred, 0)
    total = 0
    for activity in activities:
        types = types_by_place.get(activity["place_id"])
        start = clock_minutes(activity["start_time"])
        end = clock_minutes(activity["end_time"])
        if not types or start is None or end is None or end <= start:
            return None
        duration = end - start
        total += duration
        for tag in matched.keys() & set(types):
            matched[tag] += duration
    return matched, total


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def parse_answer(raw):
    # No fence stripping: the prompt explicitly asks for a raw JSON object.
    try:
        answer = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(answer, dict):
        return None
    if answer.get("status") not in ("plan", "clarification", "out_of_scope", "help"):
        return None
    if not isinstance(answer.get("message"), str) or any(
        not isinstance(answer.get(k), list) for k in ("days", "assumptions", "questions")
    ):
        return None
    activities = []
    for day in answer["days"]:
        if not isinstance(day, dict) or "date" not in day or not isinstance(day.get("activities"), list):
            return None
        for item in day["activities"]:
            if not isinstance(item, dict) or not isinstance(item.get("place_id"), str):
                return None
            if any(k not in item for k in ("start_time", "end_time", "note")):
                return None
            transport = item.get("transport_from_previous")
            if not isinstance(transport, dict) or any(
                k not in transport for k in
                ("from_place_id", "recommended_mode", "walking_minutes", "public_transit_minutes")
            ):
                return None
            if transport["recommended_mode"] not in MODES:
                return None
            activities.append(item)
    if bool(activities) != (answer["status"] == "plan"):
        return None
    return answer["status"], activities


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="baseline JSONL or one run directory")
    parser.add_argument("--json-output", type=Path, help="save structured scores as JSON")
    args = parser.parse_args(argv)
    labels = {
        c["id"]: c for split in ("dev", "test")
        for c in load(f"travel-dialogue-{split}.json")["cases"]
    }
    types_by_place = {p["id"]: p.get("types") for p in load("hong-kong-attractions.json")["places"]}
    places = set(types_by_place)
    place_data = {p["id"]: p for p in load("hong-kong-attractions.json")["places"]}
    place_data.update({p["id"]: {**p, "_is_accommodation": True}
                       for p in load("hong-kong-accommodations.json")["places"]})
    hotel_ids = set(place_data) - places
    route_data = load("hong-kong-routes.json")
    route_rows = route_data["routes"]
    routes = {(r["origin_id"], r["destination_id"]): r for r in route_rows}
    city = load("hong-kong-attractions.json")["city"]
    required_names = ("目的地", "到离时间", "必游景点", "景点开放", "指定住宿")
    required_totals = {name: Counter() for name in required_names}
    totals = Counter()
    seen = set()
    case_reports = []
    if args.results.is_dir():
        files = sorted(args.results.glob("*/result.jsonl"))
        if not files and (args.results / "result.jsonl").is_file():
            files = [args.results / "result.jsonl"]
        if not files:
            parser.error("该目录中没有找到案例结果。")
    else:
        files = [args.results]
    lines = (line for file in files for line in file.read_text(encoding="utf-8").splitlines())
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        cid = row["case_id"]
        if cid not in labels or cid in seen:
            parser.error(f"Unknown or duplicate case ID: {cid}")
        seen.add(cid)
        case = labels[cid]
        expected = case["expected"]
        report = {
            "case_id": cid, "split": row.get("split"), "method": row.get("method"),
            "model": row.get("model"), "prompt_version": row.get("prompt_version"),
            "request_status": row.get("status"), "output_status": None,
            "format_valid": None, "unavailable_reason": None,
            "required_metrics": {name: None for name in required_names},
            "preference_metrics": {"景点类型偏好": None, "舒适度偏好": None, "顺路达标率": None},
            "景点重复率": None,
        }
        case_reports.append(report)
        required = set(expected.get("must_visit_place_ids") or [])
        preferred = expected.get("preferred_attraction_types")
        totals["runs"] += 1
        totals["required_cases"] += bool(required)
        totals["preference_cases"] += bool(preferred)
        if row.get("status") != "ok":
            report["unavailable_reason"] = "请求失败"
            print(f"{cid}  请求失败  HTTP={row.get('http_status')}")
            for name, counts in required_totals.items():
                counts["unavailable"] += 1
                print(f"  必要指标 {name}: N/A（请求失败）")
            continue
        totals["responses"] += 1
        parsed = parse_answer(row.get("output"))
        if parsed is None:
            report["format_valid"] = False
            report["unavailable_reason"] = "输出格式不通过"
            print(f"{cid}  格式不通过")
            for name, counts in required_totals.items():
                counts["unavailable"] += 1
                print(f"  必要指标 {name}: N/A（输出格式不通过）")
            continue
        totals["formatted"] += 1
        status, activities = parsed
        report["format_valid"] = True
        report["output_status"] = status
        if status != "plan":
            report["unavailable_reason"] = "未生成行程"
        visited = {a["place_id"] for a in activities}
        missing = required - visited
        unknown = visited - places - hotel_ids
        invented_times = 0
        for activity in activities:
            transport = activity["transport_from_previous"]
            origin = transport.get("from_place_id")
            destination = activity["place_id"]
            route = routes.get((origin, destination), {})
            for field, route_field in (("walking_minutes", "walk_only_seconds"),
                                       ("public_transit_minutes", "transit_total_seconds")):
                value = transport[field]
                if value is None:
                    continue
                reference = route.get(route_field)
                if origin == destination and origin in place_data:
                    reference = 0
                if (row.get("prompt_version") not in ("baseline-v3", "improvement-v2", "improvement-v2-prompt-v2")
                        or isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0
                        or reference is None or abs(value * 60 - reference) > 60):
                    invented_times += 1
        is_plan = status == "plan"
        totals["plans"] += is_plan
        totals["required_plans"] += is_plan and bool(required)
        totals["required_pass"] += is_plan and bool(required) and not missing
        totals["grounded_plans"] += is_plan and not unknown and not invented_times
        visit_score = (
            f"{len(required)-len(missing)}/{len(required)}"
            if is_plan and required else "N/A"
        )
        facts = ("通过" if not unknown and not invented_times else "失败") if is_plan else "N/A"
        revision = ("待人工核对" if sum(m["role"] == "user" for m in case["messages"]) > 1
                    else "N/A（单轮案例，无多轮修改）")
        print(f"{cid}  {status}  必游={visit_score}  结构化事实={facts}  多轮={revision}")
        answer = json.loads(row["output"])
        required_scores = score_required_items(answer, expected, place_data, city)
        for name in required_names:
            items = required_scores[name]
            counts = required_totals[name]
            if not items:
                report["required_metrics"][name] = {
                    "passed": None, "total": 0, "unknown": 0,
                    "reason": "没有适用项目或未生成行程", "items": [],
                }
                counts["unavailable"] += 1
                print(f"  必要指标 {name}: N/A（没有适用项目或未生成行程）")
                continue
            passed = sum(score == 1 for _, score, _ in items)
            unknown_count = sum(score is None for _, score, _ in items)
            counts["passed"] += passed
            counts["total"] += len(items)
            counts["unknown"] += unknown_count
            report["required_metrics"][name] = {
                "passed": passed, "total": len(items), "unknown": unknown_count,
                "items": [{"label": label, "score": score, "evidence": evidence}
                          for label, score, evidence in items],
            }
            unknown_note = f"；未判定 {unknown_count}" if unknown_count else ""
            print(f"  必要指标 {name}: {passed}/{len(items)}{unknown_note}")
            for label, score, evidence in items:
                name_or_id = place_data.get(label, {}).get("name_zh", label)
                state = "通过" if score == 1 else "未通过" if score == 0 else "未判定"
                print(f"    {name_or_id}: {state}（{evidence}）")
        if preferred:
            attraction_activities = [a for a in activities if a["place_id"] not in hotel_ids]
            coverage = preference_coverage(attraction_activities, preferred, types_by_place)
            if coverage is None:
                reasons = []
                if not attraction_activities:
                    reasons.append("未生成可评分的景点行程")
                for activity in attraction_activities:
                    place_id = activity["place_id"]
                    name = place_data.get(place_id, {}).get("name_zh", place_id)
                    if not types_by_place.get(place_id):
                        reasons.append(f"{name}缺少景点类型标签")
                    start, end = clock_minutes(activity["start_time"]), clock_minutes(activity["end_time"])
                    if start is None or end is None or end <= start:
                        reasons.append(f"{name}缺少有效的游览起止时间")
                report["preference_metrics"]["景点类型偏好"] = {
                    "passed": None, "total": len(preferred), "rate": None,
                    "reason": "；".join(dict.fromkeys(reasons)),
                }
                print(f"  类型偏好: N/A（{report['preference_metrics']['景点类型偏好']['reason']}）")
            else:
                matched, total = coverage
                # Compare integer minutes so a rounded display cannot turn a failure into a pass.
                passed = sum(minutes * 100 >= total * PREFERENCE_THRESHOLD_PERCENT
                             for minutes in matched.values())
                totals["preference_scored"] += 1
                totals["preference_labels_passed"] += passed
                totals["preference_labels_scored"] += len(matched)
                report["preference_metrics"]["景点类型偏好"] = {
                    "passed": passed, "total": len(matched), "rate": passed / len(matched),
                    "visit_minutes": total,
                    "tags": {tag: {"minutes": minutes, "share": minutes / total,
                                   "passed": minutes * 100 >= total * PREFERENCE_THRESHOLD_PERCENT}
                             for tag, minutes in matched.items()},
                }
                parts = ", ".join(f"{tag} {minutes / total:.1%}" for tag, minutes in matched.items())
                print(f"  类型偏好: {parts}；标签达标率 {passed / len(matched):.1%}（{passed}/{len(matched)}）")
        else:
            report["preference_metrics"]["景点类型偏好"] = {
                "passed": None, "total": 0, "rate": None, "reason": "用户未指定景点类型偏好",
            }
            print("  类型偏好: N/A（用户未指定景点类型偏好）")
        if is_plan:
            repetition = repetition_metric([a for a in activities if a["place_id"] not in hotel_ids])
            report["景点重复率"] = repetition
            for item in repetition["places"]:
                item["name"] = place_data.get(item["place_id"], {}).get("name_zh", item["place_id"])
            repeat_rate = f"{repetition['rate']:.1%}" if repetition['rate'] is not None else "N/A"
            print(f"  景点重复率: {repeat_rate}（"
                  f"{repetition['repeated_places']}/{repetition['unique_places']}）")
            if repetition["places"]:
                print("    重复景点: " + "、".join(
                    f"{item['name']}（{item['visits']} 次）" for item in repetition["places"]
                ))
            accommodation = expected.get("accommodation") or {}
            accommodation_id = accommodation.get("place_id") if isinstance(accommodation, dict) else None
            daily = [evaluate_day(day, accommodation_id, expected, routes, place_data)
                     for day in answer["days"]]
            preference = expected.get("comfort_preference")
            limits = COMFORT_LIMITS_MINUTES.get(preference)
            comfort_scored = comfort_passed = 0
            comfort_days = []
            print(f"  舒适度偏好: {preference or '未指定'}")
            for index, day in enumerate(daily, 1):
                if day.get("incomplete_accommodation"):
                    day["walk_seconds"] = day["nonwalking_seconds"] = None
                day_comfort = {"day": index, "date": day["date"],
                               "walking_seconds": day["walk_seconds"],
                               "nonwalking_seconds": day["nonwalking_seconds"],
                               "passed": None, "walking_limit_minutes": None,
                               "nonwalking_limit_minutes": None,
                               "reason": ("未输出返回住宿，交通负担无法完整核实"
                                          if day.get("incomplete_accommodation") else day.get("reason"))
                                          if day["walk_seconds"] is None else None}
                comfort_days.append(day_comfort)
                if day["walk_seconds"] is None:
                    print(f"    第{index}天: N/A（{day_comfort['reason']}）")
                else:
                    comfort_text = "达标 N/A（未指定舒适度偏好）"
                    if limits:
                        walk_limit, transport_limit = limits
                        arrival = expected.get("arrival") or {}
                        departure = expected.get("departure") or {}
                        if (arrival.get("date") == day["date"] == departure.get("date")
                                and arrival.get("time") and departure.get("time")):
                            try:
                                start = datetime.fromisoformat(f"{day['date']}T{arrival['time']}")
                                end = datetime.fromisoformat(f"{day['date']}T{departure['time']}")
                                if 0 < (end - start).total_seconds() <= 6 * 3600:
                                    walk_limit /= 2
                                    transport_limit /= 2
                            except ValueError:
                                pass
                        passed = (day["walk_seconds"] <= walk_limit * 60
                                  and day["nonwalking_seconds"] <= transport_limit * 60)
                        comfort_scored += 1
                        comfort_passed += passed
                        day_comfort.update(passed=passed, walking_limit_minutes=walk_limit,
                                           nonwalking_limit_minutes=transport_limit)
                        comfort_text = (f"{'达标' if passed else '未达标'}"
                                        f"（上限 {walk_limit:g}/{transport_limit:g} 分钟）")
                    print(f"    第{index}天: 步行 {day['walk_seconds']/60:.1f} 分钟，"
                          f"非步行交通 {day['nonwalking_seconds']/60:.1f} 分钟"
                          f"；{comfort_text}（{day['return_assumption']}）")
                if day.get("transfer_feasible") is False:
                    print(f"    顺路达标: 未达标（{day['transfer_reason']}）")
                elif day.get("transfer_feasible") is None:
                    print(f"    顺路达标: N/A（{day.get('transfer_reason')}）")
                elif day.get("reason"):
                    print(f"    顺路程度: N/A（{day['reason']}）")
                else:
                    threshold = max(day['optimal_seconds'] * 0.15, 300)
                    outcome = '达标' if day['route_extra_seconds'] <= threshold else '未达标'
                    print(f"    顺路程度: 行程 {day['route_seconds']/60:.1f} 分钟，"
                          f"参考最优 {day['optimal_seconds']/60:.1f} 分钟，"
                          f"额外 {day['route_extra_seconds']/60:.1f} 分钟，"
                          f"阈值 {threshold/60:.1f} 分钟；{outcome}")
                    for label, key, order_key in (("模型顺序", "model_legs", "model_order"),
                                                  ("参考顺序", "optimal_legs", "optimal_order")):
                        legs = day[key]
                        start = legs[0]["origin"] if legs else day[order_key][0]
                        path = place_data[start].get("name_zh") or start
                        for leg in legs:
                            name = place_data[leg["destination"]].get("name_zh") or leg["destination"]
                            assumption = "，返程估计" if leg.get("assumed") else ""
                            path += f" → ({leg['mode']} {leg['seconds']/60:.1f} 分钟{assumption}) → {name}"
                        print(f"      {label}: {path}")
            if limits:
                print(f"  舒适度偏好达标率: "
                      f"{f'{comfort_passed / comfort_scored:.1%}（{comfort_passed}/{comfort_scored}）' if comfort_scored else 'N/A'}；"
                      f"可评分 {comfort_scored}/{len(daily)} 天")
            report["preference_metrics"]["舒适度偏好"] = {
                "preference": preference, "passed": comfort_passed if comfort_scored else None,
                "total": comfort_scored, "total_days": len(daily),
                "rate": comfort_passed / comfort_scored if comfort_scored else None,
                "reason": None if comfort_scored else ("用户未指定舒适度偏好" if not limits else "没有可评分日期"),
                "days": comfort_days,
            }
            daily_scores = []
            for day in daily:
                if day.get("transfer_feasible") is False:
                    daily_scores.append(0)
                elif day.get("transfer_feasible") is True and day.get("route_extra_seconds") is not None:
                    daily_scores.append(int(day["route_extra_seconds"] <= max(day["optimal_seconds"] * 0.15, 300)))
                else:
                    daily_scores.append(None)
            scored = sum(value is not None for value in daily_scores)
            passed = sum(value == 1 for value in daily_scores)
            feasible = sum(day.get("transfer_feasible") is True for day in daily)
            unknown_feasible = sum(day.get("transfer_feasible") is None for day in daily)
            feasible_summary = f"{feasible}/{len(daily)}" if daily else "N/A"
            print(f"  行程可行率: {feasible_summary}；未判定 {unknown_feasible} 天")
            report["preference_metrics"]["顺路达标率"] = {
                "passed": passed, "scored": scored, "total_days": len(daily), "days": daily,
            }
            summary = f"{passed}/{len(daily)}" if daily else "N/A"
            print(f"  顺路达标率: {summary}；未判定 {len(daily) - scored} 天")
        if is_plan and missing:
            print("  遗漏:", ", ".join(sorted(missing)))
        if unknown or invented_times:
            print(f"  库外景点: {', '.join(sorted(unknown)) or '无'}；无依据交通耗时: {invented_times} 项")

    def ratio(n, d):
        return f"{totals[n]}/{totals[d]}" if totals[d] else "N/A"

    print(f"请求成功 {ratio('responses', 'runs')}；输出格式通过率 {ratio('formatted', 'runs')}")
    for name, counts in required_totals.items():
        display = f"{counts['passed']}/{counts['total']}" if counts["total"] else "N/A"
        details = []
        if counts["unknown"]:
            details.append(f"未判定 {counts['unknown']} 项")
        if counts["unavailable"]:
            details.append(f"无适用项目或结果 {counts['unavailable']} 个案例")
        suffix = f"（{'；'.join(details)}）" if details else ""
        print(f"必要指标 {name}: {display}{suffix}")
    print(f"生成行程率 {ratio('plans', 'runs')}；必游全覆盖 {ratio('required_pass', 'required_plans')}"
          f"（含必游且生成行程 {ratio('required_plans', 'required_cases')}）")
    print(f"结构化事实通过 {ratio('grounded_plans', 'plans')}（仅检查地点 ID 和交通耗时字段）")
    label_count = totals["preference_labels_scored"]
    label_rate = (
        f"{totals['preference_labels_passed'] / label_count:.1%}"
        f"（{ratio('preference_labels_passed', 'preference_labels_scored')}）"
        if label_count else "N/A"
    )
    print(f"类型偏好标签达标率 {label_rate}"
          f"（有类型偏好的案例中可评分 {ratio('preference_scored', 'preference_cases')}）")
    final_cases = [final_values(report) for report in case_reports]
    result = {"summary": summarize_scores(final_cases), "cases": final_cases}
    if args.json_output:
        with args.json_output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    return result


if __name__ == "__main__":
    main()
