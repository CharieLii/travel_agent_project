"""Score required criteria independently after an itinerary has been generated."""

from datetime import date, datetime, time, timedelta

from route_scoring import opening_windows


def outcome(score, evidence):
    return score, evidence


def day_date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def day_time(value):
    if value == "24:00":
        return 1440
    try:
        parsed = time.fromisoformat(value) if isinstance(value, str) else None
        if parsed is None or parsed.second or parsed.microsecond or parsed.tzinfo:
            return None
        return parsed.hour * 60 + parsed.minute
    except ValueError:
        return None


def date_and_time(day, value):
    current = day_date(day)
    minutes = day_time(value)
    if current is None or minutes is None:
        return None
    return datetime.combine(current, time.min) + timedelta(minutes=minutes)


def score_required(answer, expected, places, city):
    activities = [(day, activity) for day in answer["days"] for activity in day["activities"]]
    if answer["status"] != "plan" or not activities:
        return {name: outcome(None, "未生成可评分行程") for name in
                ("目的地", "到离时间", "必游景点", "景点开放", "指定住宿")}

    scores = {}
    destination = expected.get("destination")
    if not destination:
        scores["目的地"] = outcome(None, "用户未指定目的地")
    else:
        ids = [activity["place_id"] for _, activity in activities]
        known = [place_id for place_id in ids if place_id in places]
        unknown = [place_id for place_id in ids if place_id not in places]
        city_aliases = {city.casefold(), "香港", "hong kong", "hongkong"}
        if known and str(destination).strip().casefold() not in city_aliases:
            scores["目的地"] = outcome(0, f"目标目的地为{destination}，行程包含香港景点")
        elif unknown:
            scores["目的地"] = outcome(None, f"地点不在资料库：{', '.join(unknown)}")
        elif known:
            scores["目的地"] = outcome(1, "行程景点均属于香港，符合用户指定目的地")
        else:
            scores["目的地"] = outcome(None, "行程地点无法确定目的地")

    arrival = expected.get("arrival") or {}
    departure = expected.get("departure") or {}
    arrival_date, departure_date = day_date(arrival.get("date")), day_date(departure.get("date"))
    arrival_time, departure_time = day_time(arrival.get("time")), day_time(departure.get("time"))
    unknown_boundary = any(
        boundary.get("date") is None or day_date(boundary.get("date")) is None
        or (boundary.get("time") is not None and day_time(boundary.get("time")) is None)
        for boundary in (arrival, departure) if boundary
    )
    if arrival_date and departure_date and (arrival_date > departure_date or (
        arrival_date == departure_date and arrival_time is not None and departure_time is not None
        and arrival_time > departure_time
    )):
        scores["到离时间"] = outcome(None, "用户抵达与离开时间互相冲突")
    elif not arrival_date and not departure_date:
        scores["到离时间"] = outcome(None, "缺少可比较的抵达或离开日期")
    else:
        violations, unknown = [], []
        for day, activity in activities:
            planned_date = day_date(day.get("date"))
            if planned_date is None:
                unknown.append(activity["place_id"])
                continue
            if arrival_date:
                if planned_date < arrival_date:
                    violations.append(f"{activity['place_id']}早于抵达日期")
                elif planned_date == arrival_date and arrival_time is not None:
                    value = day_time(activity.get("start_time"))
                    if value is None:
                        unknown.append(activity["place_id"])
                    elif value < arrival_time:
                        violations.append(f"{activity['place_id']}早于抵达时间")
            if departure_date:
                if planned_date > departure_date:
                    violations.append(f"{activity['place_id']}晚于离开日期")
                elif planned_date == departure_date and departure_time is not None:
                    value = day_time(activity.get("end_time"))
                    if value is None:
                        unknown.append(activity["place_id"])
                    elif value > departure_time:
                        violations.append(f"{activity['place_id']}晚于离开时间")
        scores["到离时间"] = (outcome(0, "；".join(violations)) if violations else
                               outcome(None, "抵达、离开或部分活动日期与时间不完整") if unknown or unknown_boundary else
                               outcome(1, "所有已知活动均在用户提供的日期和时间边界内"))

    required = expected.get("must_visit_place_ids") or []
    if not required:
        scores["必游景点"] = outcome(None, "用户未指定必游景点")
    else:
        missing = set(required) - {activity["place_id"] for _, activity in activities}
        scores["必游景点"] = (outcome(0, "遗漏：" + "、".join(sorted(missing))) if missing else
                              outcome(1, f"已覆盖全部 {len(set(required))} 个必游景点"))

    violations, unknown = [], []
    for day, activity in activities:
        place_id = activity["place_id"]
        place = places.get(place_id)
        if place and place.get("_is_accommodation"):
            continue
        current_date = day_date(day.get("date"))
        at = date_and_time(day.get("date"), activity.get("start_time"))
        if not place:
            unknown.append(f"{place_id}地点资料缺失")
            continue
        if not current_date:
            unknown.append(f"{place_id}游览日期缺失或无效")
            continue
        if at is None:
            unknown.append(f"{place_id}到达时刻缺失或无效")
            continue
        windows = opening_windows(place, current_date)
        if windows is None:
            unknown.append(f"{place_id}当天开放时间缺失")
        elif not any(start <= at < end for start, end in windows):
            violations.append(place_id)
    scores["景点开放"] = (outcome(0, "到达时未开放：" + "、".join(violations)) if violations else
                            outcome(None, "；".join(unknown)) if unknown else
                            outcome(1, "各景点到达时处于常规开放时段"))

    accommodation = expected.get("accommodation")
    if not isinstance(accommodation, dict) or accommodation.get("is_specific") is False:
        scores["指定住宿"] = outcome(None, "用户未指定具体住宿")
    elif not accommodation.get("place_id"):
        scores["指定住宿"] = outcome(None, "用户指定的住宿缺少可匹配 ID")
    else:
        expected_id = accommodation["place_id"]
        days = [day for day in answer["days"] if day["activities"]]
        arrival_date = (expected.get("arrival") or {}).get("date")
        departure_date = (expected.get("departure") or {}).get("date")
        violations, unknown = [], []
        for day in days:
            items = day["activities"]
            date = day.get("date")
            is_arrival = bool(date and arrival_date and date == arrival_date)
            is_departure = bool(date and departure_date and date == departure_date)
            start_ok = (items[0]["place_id"] == expected_id or
                        items[0]["transport_from_previous"].get("from_place_id") == expected_id)
            end_ok = items[-1]["place_id"] == expected_id
            for ok, exception, boundary, label in (
                (start_ok, is_arrival, arrival_date, "出发"),
                (end_ok, is_departure, departure_date, "返回"),
            ):
                if ok or exception:
                    continue
                if not date or not boundary:
                    unknown.append(f"{date or '日期未知'}{label}：无法判断是否为抵离日")
                else:
                    violations.append(f"{date}{label}未体现指定酒店")
        scores["指定住宿"] = (outcome(0, "；".join(violations)) if violations else
                            outcome(None, "；".join(unknown)) if unknown else
                            outcome(1, "非抵达日从酒店出发，非离开日返回酒店"))
    return scores


def score_required_items(answer, expected, places, city):
    """Return individual check outcomes for the denominators shown in reports."""
    scores = score_required(answer, expected, places, city)
    items = {"目的地": [], "到离时间": [], "必游景点": [], "景点开放": [], "指定住宿": []}
    if expected.get("destination"):
        items["目的地"].append(("目的地", *scores["目的地"]))

    for key, label in (("arrival", "抵达"), ("departure", "离开")):
        if not expected.get(key):
            continue
        if scores["到离时间"][1] == "用户抵达与离开时间互相冲突":
            score, evidence = scores["到离时间"]
        else:
            boundary_expected = dict(expected)
            boundary_expected["arrival" if key == "departure" else "departure"] = None
            score, evidence = score_required(answer, boundary_expected, places, city)["到离时间"]
        items["到离时间"].append((label, score, evidence))

    activities = [(day, activity) for day in answer["days"] for activity in day["activities"]]
    planned = answer["status"] == "plan" and bool(activities)
    visited = {activity["place_id"] for _, activity in activities}
    for place_id in dict.fromkeys(expected.get("must_visit_place_ids") or []):
        if not planned:
            score, evidence = None, "未生成可评分行程"
        elif place_id in visited:
            score, evidence = 1, "已安排"
        else:
            score, evidence = 0, "未安排"
        items["必游景点"].append((place_id, score, evidence))

    if planned:
        for day, activity in activities:
            place_id = activity["place_id"]
            if places.get(place_id, {}).get("_is_accommodation"):
                continue
            single_answer = {"status": "plan", "days": [{"date": day.get("date"),
                                                           "activities": [activity]}]}
            score, evidence = score_required(single_answer, expected, places, city)["景点开放"]
            items["景点开放"].append((place_id, score, evidence))

    accommodation = expected.get("accommodation")
    if isinstance(accommodation, dict) and accommodation.get("is_specific") is not False:
        items["指定住宿"].append((accommodation.get("name") or "住宿", *scores["指定住宿"]))
    return items
