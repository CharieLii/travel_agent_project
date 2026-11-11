"""Render baseline JSONL results as a readable Markdown itinerary."""

import argparse
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "dataset"


def place_names():
    names = {}
    for filename in ("hong-kong-attractions.json", "hong-kong-accommodations.json"):
        data = json.loads((DATA / filename).read_text(encoding="utf-8"))
        names.update({place["id"]: place.get("name_zh") or place["id"] for place in data["places"]})
    return names


def render_answer(raw, names):
    try:
        answer = json.loads(raw)
        if not isinstance(answer, dict) or not isinstance(answer.get("days"), list):
            raise ValueError("missing days")
        lines = []
        if answer.get("status"):
            lines.append(f"**行程状态** {answer['status']}")
        if answer.get("message"):
            lines.extend(["", str(answer["message"])])
        for index, day in enumerate(answer["days"], 1):
            if not isinstance(day, dict) or not isinstance(day.get("activities"), list):
                raise ValueError("invalid day")
            date = day.get("date") or "日期未确定"
            lines.extend(["", f"### 第 {index} 天 · {date}"])
            for activity in day["activities"]:
                if not isinstance(activity, dict):
                    raise ValueError("invalid activity")
                place_id = activity.get("place_id")
                name = names.get(place_id, place_id or "地点未确定")
                start = activity.get("start_time") or "时间未确定"
                end = activity.get("end_time") or "时间未确定"
                lines.extend(["", f"- **{name}**（{start}–{end}）"])
                if activity.get("note"):
                    lines.append(f"  - 安排说明：{activity['note']}")
                transport = activity.get("transport_from_previous")
                if isinstance(transport, dict):
                    origin = transport.get("from_place_id")
                    origin_name = names.get(origin, origin) if origin else "起点未确定"
                    mode = {"walk": "步行", "transit": "公共交通"}.get(
                        transport.get("recommended_mode"), "方式未确定"
                    )
                    walk = transport.get("walking_minutes")
                    transit = transport.get("public_transit_minutes")
                    durations = []
                    if walk is not None:
                        durations.append(f"步行耗时 {walk} 分钟")
                    if transit is not None:
                        durations.append(f"公共交通耗时 {transit} 分钟")
                    detail = "；" + "，".join(durations) if durations else "，具体耗时待确认"
                    lines.append(f"  - 从 {origin_name} 出发，建议{mode}{detail}。")
        for key, heading in (("assumptions", "假设"), ("questions", "待确认")):
            values = answer.get(key)
            if isinstance(values, list) and values:
                lines.extend(["", f"### {heading}", ""])
                lines.extend(f"- {value}" for value in values)
        return "\n".join(lines) if lines else "模型未提供可展示的内容。"
    except (TypeError, ValueError, json.JSONDecodeError):
        # Preserve malformed model responses for inspection instead of losing them.
        return "**模型输出无法解析，以下是原始内容**\n\n" + str(raw)


def render_row(row, names):
    lines = [f"## {row.get('case_id', '未命名案例')}", ""]
    for message in row.get("messages", []):
        if isinstance(message, dict) and message.get("role") == "user":
            lines.extend([f"**用户** {message.get('content', '')}", ""])
    if row.get("status") != "ok":
        detail = row.get("error_type") or "未知错误"
        if row.get("http_status"):
            detail += f"（HTTP {row['http_status']}）"
        lines.append(f"**请求失败** {detail}")
    else:
        lines.append(render_answer(row.get("output"), names))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="baseline JSONL file")
    args = parser.parse_args()
    names = place_names()
    print("# Baseline 行程结果\n")
    with args.results.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                print(render_row(json.loads(line), names))


if __name__ == "__main__":
    main()
