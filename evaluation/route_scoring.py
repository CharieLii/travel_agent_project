"""Evaluate itinerary legs using the shared offline route data."""

from datetime import datetime, timedelta
from itertools import permutations


def edge(routes, origin, destination, mode):
    if origin == destination:
        return (0.0, 0.0)
    route = routes.get((origin, destination))
    if not route:
        return None
    if mode == "walk":
        value = route.get("walk_only_seconds")
        return (float(value), 0.0) if value is not None and value >= 0 else None
    if mode == "transit":
        total, walk = route.get("transit_total_seconds"), route.get("transit_walk_seconds")
        if total is not None and walk is not None and 0 <= walk <= total:
            return float(walk), float(total - walk)
    return None


def fastest_with_mode(routes, origin, destination, modes):
    options = [(mode, edge(routes, origin, destination, mode)) for mode in sorted(modes)]
    if not options or any(option is None for _, option in options):
        return None
    return min(options, key=lambda item: sum(item[1]))


def fastest(routes, origin, destination, modes):
    selected = fastest_with_mode(routes, origin, destination, modes)
    return selected[1] if selected else None


def instant(day, value):
    try:
        return datetime.fromisoformat(f"{day}T{value}")
    except (TypeError, ValueError):
        return None


def opening_windows(place, date):
    week = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    hours = place.get("opening_hours", {}).get(week[date.weekday()])
    if hours is None:
        return None
    if hours == []:
        return []
    intervals = [hours] if len(hours) == 2 and isinstance(hours[0], str) else hours
    windows = []
    for interval in intervals:
        if not isinstance(interval, list) or len(interval) != 2:
            return None
        start = instant(date.isoformat(), interval[0])
        end = (datetime.combine(date + timedelta(days=1), datetime.min.time())
               if interval[1] == "24:00" else instant(date.isoformat(), interval[1]))
        if not start or not end or end <= start:
            return None
        windows.append((start, end))
    return windows


def transfer_feasibility(day, routes):
    """Check scheduled transfers using the model's selected mode, not the fastest mode."""
    activities = day.get("activities")
    if not isinstance(activities, list) or len(activities) < 2:
        return None, "不足两个活动，无法检查活动间转场"
    unknown = []
    for previous, current in zip(activities, activities[1:]):
        if not isinstance(previous, dict) or not isinstance(current, dict):
            unknown.append("活动格式无效")
            continue
        transport = current.get("transport_from_previous") or {}
        origin, dest = previous.get("place_id"), current.get("place_id")
        mode = transport.get("recommended_mode")
        date = day.get("date") or "2000-01-01"
        start = instant(date, previous.get("end_time"))
        end = instant(date, current.get("start_time"))
        if start and end and end < start:
            return False, f"{origin} → {dest} 活动时间重叠"
        if transport.get("from_place_id") != origin or mode not in ("walk", "transit"):
            unknown.append(f"{origin} → {dest} 的起点或交通方式缺失、不一致")
            continue
        leg = edge(routes, origin, dest, mode)
        if leg is None:
            unknown.append(f"{origin} → {dest} 缺少 {mode} 路线耗时")
        if start is None or end is None:
            unknown.append(f"{origin} → {dest} 缺少有效的活动起止时间")
        if leg is not None and start is not None and end is not None and start + timedelta(seconds=sum(leg)) > end:
            return False, f"{origin} → {dest} 采用 {mode} 的转场时间不足"
    return (None, "；".join(unknown)) if unknown else (True, None)


def evaluate_day(day, accommodation, expected, routes, places):
    """Score only explicitly output legs; optimize the same stops and endpoints."""
    result = {"date": day.get("date"), "walk_seconds": None,
              "nonwalking_seconds": None, "route_extra_seconds": None, "reason": None}
    result["transfer_feasible"], result["transfer_reason"] = transfer_feasibility(day, routes)
    activities = day.get("activities")
    if not isinstance(activities, list) or not activities:
        result["reason"] = "当天没有可评分活动"
        return result
    hotels = {key for key, place in places.items() if place.get("_is_accommodation")}
    if accommodation:
        hotels.add(accommodation)
    legs, modes = [], set()
    first = activities[0]
    first_transport = first.get("transport_from_previous") or {}
    origin = first_transport.get("from_place_id")
    current = origin if origin else first.get("place_id")
    start_point = current
    walk = nonwalk = 0.0
    for index, activity in enumerate(activities):
        dest = activity.get("place_id")
        if dest not in places:
            result["reason"] = "地点不在路线数据中"
            return result
        if index == 0 and not origin:
            current = dest
            continue
        trans = activity.get("transport_from_previous") or {}
        mode = trans.get("recommended_mode")
        if trans.get("from_place_id") != current or mode not in ("walk", "transit"):
            result["reason"] = "交通方式或转场起点缺失或与行程不一致"
            return result
        leg = edge(routes, current, dest, mode)
        if leg is None:
            result["reason"] = "缺少所选交通方式的路线数据"
            return result
        walk += leg[0]
        nonwalk += leg[1]
        modes.add(mode)
        legs.append({"origin": current, "destination": dest, "mode": mode, "seconds": sum(leg)})
        current = dest
    result.update(walk_seconds=walk, nonwalking_seconds=nonwalk,
                  return_assumption="仅统计模型明确输出的路段")
    departure = (expected.get("departure") or {}).get("date")
    result["incomplete_accommodation"] = bool(
        accommodation and activities[-1].get("place_id") != accommodation
        and (not departure or not day.get("date") or day["date"] != departure)
    )
    try:
        date = datetime.fromisoformat(day["date"]).date()
    except (TypeError, ValueError, KeyError):
        result["reason"] = "当天日期未知或格式无效"
        return result
    if origin and origin not in hotels:
        result["reason"] = "首段起点不是住宿，无法在当前参考模型中公平比较"
        return result
    visits = []
    for activity in activities:
        dest = activity["place_id"]
        if dest in hotels:
            continue
        start = instant(day["date"], activity.get("start_time"))
        end = instant(day["date"], activity.get("end_time"))
        windows = opening_windows(places[dest], date)
        if not start or not end or end <= start or windows is None:
            result["reason"] = "游览时间或景点开放时间未知"
            return result
        visits.append({"place": dest, "start": start, "end": end,
                       "duration": end - start, "windows": windows})
    if not visits or len(visits) > 8:
        result["reason"] = "没有可评分景点或当天景点超过 8 个"
        return result
    for visit in visits:
        if not any(a <= visit["start"] and visit["end"] <= b for a, b in visit["windows"]):
            result["reason"] = "模型行程与开放时间冲突，顺路程度不可比较"
            return result
    # Check that consecutive timed activities allow their actual transfer.
    for index in range(1, len(activities)):
        prev, curr = activities[index-1], activities[index]
        prev_end = instant(day["date"], prev.get("end_time"))
        curr_start = instant(day["date"], curr.get("start_time"))
        trans = curr.get("transport_from_previous") or {}
        leg = edge(routes, prev["place_id"], curr["place_id"], trans.get("recommended_mode"))
        if prev_end and curr_start and leg and prev_end + timedelta(seconds=sum(leg)) > curr_start:
            result["reason"] = "模型行程与交通衔接冲突，顺路程度不可比较"
            return result
    fixed_start = start_point if start_point in hotels else None
    fixed_end = activities[-1]["place_id"] if activities[-1]["place_id"] in hotels else None
    first_leg = legs[0]["seconds"] if legs and fixed_start else 0
    last_leg = legs[-1]["seconds"] if legs and fixed_end else 0
    day_start = visits[0]["start"] - timedelta(seconds=first_leg)
    day_end = visits[-1]["end"] + timedelta(seconds=last_leg)
    arrival, departure = expected.get("arrival") or {}, expected.get("departure") or {}
    if arrival.get("date") == day["date"] and arrival.get("time"):
        boundary = instant(day["date"], arrival["time"])
        if boundary: day_start = max(day_start, boundary)
    if departure.get("date") == day["date"] and departure.get("time"):
        boundary = instant(day["date"], departure["time"])
        if boundary: day_end = min(day_end, boundary)
    if day_start > visits[0]["start"] - timedelta(seconds=first_leg) or day_end < visits[-1]["end"] + timedelta(seconds=last_leg):
        result["reason"] = "模型行程与到离时间边界冲突，顺路程度不可比较"
        return result
    best, best_order, best_legs, missing = None, None, None, False
    for ordering in permutations(visits):
        clock, total = day_start, 0.0
        current = fixed_start or ordering[0]["place"]
        candidate = []
        for visit in ordering:
            choice = fastest_with_mode(routes, current, visit["place"], modes) if current != visit["place"] else None
            if current != visit["place"] and choice is None:
                missing = True
                break
            seconds = sum(choice[1]) if choice else 0
            clock += timedelta(seconds=seconds)
            feasible = next((max(clock, a) for a,b in visit["windows"]
                             if max(clock,a) + visit["duration"] <= b), None)
            if feasible is None: break
            clock = feasible + visit["duration"]
            if choice:
                candidate.append({"origin": current, "destination": visit["place"], "mode": choice[0], "seconds": seconds})
                total += seconds
            current = visit["place"]
        else:
            if fixed_end and current != fixed_end:
                choice = fastest_with_mode(routes, current, fixed_end, modes)
                if choice is None:
                    missing = True
                    continue
                seconds = sum(choice[1]); total += seconds
                candidate.append({"origin": current, "destination": fixed_end, "mode": choice[0], "seconds": seconds})
                clock += timedelta(seconds=seconds)
            if clock <= day_end and (best is None or total < best):
                best, best_order, best_legs = total, [v["place"] for v in ordering], candidate
    if missing:
        result["reason"] = "参考路线存在缺失的候选转场，无法确认最优解"
    elif best is None:
        result["reason"] = "没有满足开放时间和当日时间窗口的参考路线"
    else:
        result.update(route_seconds=walk+nonwalk, optimal_seconds=best,
                      route_extra_seconds=max(0.0, walk+nonwalk-best),
                      reference_modes=sorted(modes), model_order=[v["place"] for v in visits],
                      optimal_order=best_order, model_legs=legs, optimal_legs=best_legs)
    return result
