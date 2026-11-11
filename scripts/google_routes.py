"""Look up walking and public-transit estimates for one Hong Kong place pair."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATASET = Path(__file__).resolve().parent.parent / "dataset"
ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"


def compute_route(key, origin, destination, mode, departure_time):
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": mode,
        "regionCode": "hk",
    }
    field_mask = "routes.duration"
    if mode == "TRANSIT":
        body["departureTime"] = departure_time
        field_mask += ",routes.legs.steps.travelMode,routes.legs.steps.staticDuration"
    request = Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": field_mask,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            routes = json.load(response).get("routes", [])
    except HTTPError as error:
        raise RuntimeError(f"{mode} 查询失败（HTTP {error.code}）。") from None
    except (URLError, TimeoutError):
        raise RuntimeError(f"{mode} 查询失败，无法连接 Google Routes API。") from None
    return routes[0] if routes else {}


def seconds(value):
    return Decimal(value.removesuffix("s")) if value is not None else None


def transit_durations(route):
    legs = route.get("legs", [])
    steps = [step for leg in legs for step in leg.get("steps", [])]
    modes = {step.get("travelMode") for step in steps}
    # A walking-only result does not supply a public-transit estimate.
    if modes == {"WALK"}:
        return None, None
    total = seconds(route.get("duration"))
    if "TRANSIT" not in modes or modes - {"WALK", "TRANSIT"} or any(
        not leg.get("steps") for leg in legs
    ):
        return total, None
    walking = [seconds(step.get("staticDuration")) for step in steps
               if step["travelMode"] == "WALK"]
    if None in walking:
        return total, None
    walk_total = sum(walking, Decimal(0))
    return total, walk_total if total is None or walk_total <= total else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin_id")
    parser.add_argument("destination_id")
    parser.add_argument("--departure-time", required=True, help="ISO 8601 with timezone")
    args = parser.parse_args()
    try:
        departure = datetime.fromisoformat(args.departure_time)
    except ValueError:
        parser.error("--departure-time 须为 ISO 8601 时间。")
    if departure.utcoffset() is None:
        parser.error("--departure-time 须包含时区，例如 +08:00。")
    departure_time = departure.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    places = {}
    for filename in ("hong-kong-attractions.json", "hong-kong-accommodations.json"):
        for place in json.loads((DATASET / filename).read_text(encoding="utf-8"))["places"]:
            places[place["id"]] = place["address"]
    for place_id in (args.origin_id, args.destination_id):
        if not places.get(place_id):
            parser.error(f"地点 ID 不存在或缺少地址：{place_id}")
    if args.origin_id == args.destination_id:
        parser.error("请选择两个不同地点。")

    key = os.environ.get("GOOGLE_ROUTES_API_KEY", "").strip()
    if not key:
        parser.error("请先在本机 .env 配置 GOOGLE_ROUTES_API_KEY，再运行 source .env。")
    origin, destination = (places[place_id] for place_id in (args.origin_id, args.destination_id))
    try:
        walk = compute_route(key, origin, destination, "WALK", departure_time)
        transit = compute_route(key, origin, destination, "TRANSIT", departure_time)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    transit_total, transit_walk = transit_durations(transit)
    result = {
        "origin_id": args.origin_id,
        "destination_id": args.destination_id,
        "transit_departure_time": departure_time,
        "walk_only_seconds": seconds(walk.get("duration")),
        "transit_total_seconds": transit_total,
        "transit_walk_seconds": transit_walk,
        "source": "Google Maps",
    }
    print(json.dumps(result, indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
