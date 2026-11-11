"""Generate directed attraction-pair travel estimates from a local OpenTripPlanner."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import permutations
import json
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler


ROOT = Path(__file__).resolve().parent.parent
SHERATON = "sheraton_hong_kong_hotel_and_towers"
CROSS_HARBOUR_ATTRACTIONS = {
    "tai_kwun", "hong_kong_park", "hk_zoological_botanical_gardens",
    "golden_bauhinia_square", "sky_terrace_428", "ocean_park_hong_kong",
    "hong_kong_disneyland",
}
QUERY = """
query Routes($from: PlanLabeledLocationInput!, $to: PlanLabeledLocationInput!,
             $time: OffsetDateTime!, $modes: PlanModesInput!) {
  planConnection(origin: $from, destination: $to, dateTime: {earliestDeparture: $time},
                 modes: $modes, first: 8) {
    edges { node { end duration legs { mode duration route { shortName } } } }
  }
}
"""


def location(place):
    return {"location": {"coordinate": place["coordinates"]}}


def query(endpoint, origin, destination, departure, modes):
    variables = {"from": location(origin), "to": location(destination),
                 "time": departure, "modes": modes}
    request = Request(endpoint, json.dumps({"query": QUERY, "variables": variables}).encode(),
                      {"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=90) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return [edge["node"] for edge in payload["data"]["planConnection"]["edges"]]


def filtered_transit_candidates(endpoint, origin, destination, departure):
    # Debug mode reveals candidates removed by the walk-preference filter.
    debug_query = QUERY.replace(
        "modes: $modes, first: 8)",
        "modes: $modes, first: 8, itineraryFilter: {itineraryFilterDebugProfile: LIST_ALL})",
    ).replace("duration legs", "duration systemNotices { tag } legs")
    variables = {"from": location(origin), "to": location(destination),
                 "time": departure, "modes": {"transitOnly": True}}
    request = Request(endpoint, json.dumps({"query": debug_query, "variables": variables}).encode(),
                      {"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=90) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return [
        edge["node"] for edge in payload["data"]["planConnection"]["edges"]
        if any(leg["mode"] != "WALK" for leg in edge["node"]["legs"])
        and all(notice["tag"] != "outside-search-window"
                for notice in edge["node"]["systemNotices"])
    ]


def route_pair(endpoint, departure, pair):
    origin, destination = pair
    args = endpoint, origin, destination, departure
    walks = query(*args, {"direct": ["WALK"], "directOnly": True})
    trips = query(*args, {"transitOnly": True})
    was_filtered = not trips
    if was_filtered:
        trips = filtered_transit_candidates(*args)
    # Prefer earliest arrival at the fixed departure reference. The reported
    # duration includes walking and waiting after leaving the origin.
    transit = min(trips, key=lambda trip: (datetime.fromisoformat(trip["end"]),
                                         trip["duration"]), default=None)
    legs = transit["legs"] if transit else []
    modes = list(dict.fromkeys(leg["mode"] for leg in legs if leg["mode"] != "WALK"))
    place_ids = {origin["id"], destination["id"]}
    walk_only = min((trip["duration"] for trip in walks), default=None)
    if SHERATON in place_ids and place_ids.intersection(CROSS_HARBOUR_ATTRACTIONS):
        walk_only = None
    return {
        "origin_id": origin["id"],
        "destination_id": destination["id"],
        "walk_only_seconds": walk_only,
        "transit_total_seconds": transit["duration"] if transit else None,
        "transit_walk_seconds": sum(leg["duration"] for leg in legs if leg["mode"] == "WALK")
        if transit else None,
        "transit_modes": modes,
        "uses_mtr_subway": "SUBWAY" in modes if transit else None,
        "mtr_lines": list(dict.fromkeys(leg["route"]["shortName"] for leg in legs
                          if leg["mode"] == "SUBWAY" and leg["route"])),
        **({"transit_selection": "debug_candidate_filtered_by_otp"} if transit and was_filtered else {}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:18118/otp/gtfs/v1")
    parser.add_argument("--departure", required=True, help="ISO 8601 with +08:00")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True, help="JSON describing graph inputs")
    parser.add_argument("--pair", nargs=2, metavar=("ORIGIN", "DESTINATION"))
    parser.add_argument("--include-accommodations", action="store_true",
                        help="include routes between accommodations and attractions")
    args = parser.parse_args()
    if datetime.fromisoformat(args.departure).utcoffset() is None:
        parser.error("--departure requires a UTC offset")
    places = json.loads((ROOT / "dataset/hong-kong-attractions.json").read_text())["places"]
    metadata = json.loads(args.provenance.read_text())
    accommodations = json.loads((ROOT / "dataset/hong-kong-accommodations.json").read_text())["places"]
    by_id = {place["id"]: place for place in places + accommodations}
    pairs = permutations(places, 2)
    if args.include_accommodations:
        pairs = list(pairs) + [
            pair for accommodation in accommodations for place in places
            for pair in ((accommodation, place), (place, accommodation))
        ]
    if args.pair:
        if any(place_id not in by_id for place_id in args.pair) or args.pair[0] == args.pair[1]:
            parser.error("--pair requires two different attraction IDs from the dataset")
        pairs = [(by_id[args.pair[0]], by_id[args.pair[1]])]
    with ThreadPoolExecutor(max_workers=4) as workers:
        routes = list(workers.map(lambda pair: route_pair(args.endpoint, args.departure, pair), pairs))
    dataset = {"city": "Hong Kong", "timezone": "Asia/Hong_Kong",
               "reference_departure": args.departure, "provenance": metadata, "routes": routes}
    args.output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
