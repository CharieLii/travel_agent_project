"""Regression checks for the five post-generation required criteria."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from hard_scoring import score_required, score_required_items


def activity(place="a", start="10:00", end="11:00", origin="hotel"):
    return {"place_id": place, "start_time": start, "end_time": end,
            "transport_from_previous": {"from_place_id": origin}}


class RequiredScoresTest(unittest.TestCase):
    def setUp(self):
        self.places = {"a": {"opening_hours": {"fri": ["10:00", "18:00"]}},
                       "b": {"opening_hours": {"fri": None}}}
        self.expected = {"destination": "香港", "arrival": {"date": "2026-11-20", "time": "09:00"},
                         "departure": {"date": "2026-11-20", "time": "19:00"},
                         "must_visit_place_ids": ["a"], "accommodation": {"place_id": "hotel"}}
        self.answer = {"status": "plan", "days": [{"date": "2026-11-20", "activities": [activity()]}]}

    def scores(self):
        return score_required(self.answer, self.expected, self.places, "Hong Kong")

    def test_all_pass_and_independent_violations(self):
        self.assertEqual([self.scores()[k][0] for k in self.scores()], [1, 1, 1, 1, 1])
        self.answer["days"][0]["activities"][0].update(start_time="08:00", end_time="09:30")
        scores = self.scores()
        self.assertEqual(scores["到离时间"][0], 0)
        self.assertEqual(scores["景点开放"][0], 0)
        self.assertEqual(scores["必游景点"][0], 1)
        self.assertEqual(scores["指定住宿"][0], 1)

    def test_unknown_and_definite_failure(self):
        self.answer["days"][0]["activities"].append(activity("b", "12:00", "13:00", "a"))
        self.assertIsNone(self.scores()["景点开放"][0])
        self.answer["days"][0]["activities"][0]["start_time"] = "09:59"
        self.assertEqual(self.scores()["景点开放"][0], 0)

    def test_partial_date_and_opening_boundary(self):
        self.expected["arrival"]["time"] = None
        self.answer["days"][0]["activities"][0]["start_time"] = None
        self.assertEqual(self.scores()["到离时间"][0], 1)
        self.assertIsNone(self.scores()["景点开放"][0])
        self.answer["days"][0]["activities"][0]["start_time"] = "18:00"
        self.assertEqual(self.scores()["景点开放"][0], 0)

    def test_missing_destination_hotel_and_contradictory_times(self):
        self.expected["destination"] = None
        self.expected["accommodation"] = {"name": "未收录酒店", "place_id": None}
        self.expected["arrival"]["date"] = "2026-11-21"
        scores = self.scores()
        self.assertIsNone(scores["目的地"][0])
        self.assertIsNone(scores["指定住宿"][0])
        self.assertIsNone(scores["到离时间"][0])

    def test_wrong_destination_must_visit_and_hotel(self):
        self.expected["destination"] = "澳门"
        self.expected["must_visit_place_ids"] = ["b"]
        self.expected["arrival"]["date"] = "2026-11-19"
        self.expected["departure"]["date"] = "2026-11-21"
        self.answer["days"][0]["activities"][0]["transport_from_previous"]["from_place_id"] = None
        scores = self.scores()
        for name in ("目的地", "必游景点", "指定住宿"):
            self.assertEqual(scores[name][0], 0)

    def test_hotel_boundary_and_first_last_day_exceptions(self):
        self.places["hotel"] = {"_is_accommodation": True}
        self.expected["arrival"]["date"] = "2026-11-19"
        self.expected["departure"]["date"] = "2026-11-21"
        self.assertEqual(self.scores()["指定住宿"][0], 0)
        self.answer["days"][0]["activities"].append(activity("hotel", "12:00", "12:00", "a"))
        self.assertEqual(self.scores()["指定住宿"][0], 1)
        self.assertEqual(len(score_required_items(self.answer, self.expected, self.places, "Hong Kong")["景点开放"]), 1)

    def test_non_plan_is_na(self):
        self.answer = {"status": "clarification", "days": []}
        self.assertTrue(all(score is None for score, _ in self.scores().values()))

    def test_item_denominators_and_unknown_attraction(self):
        self.expected["must_visit_place_ids"] = ["a", "b"]
        self.answer["days"][0]["activities"].append(activity("b", "12:00", "13:00", "a"))
        items = score_required_items(self.answer, self.expected, self.places, "Hong Kong")
        self.assertEqual([score for _, score, _ in items["到离时间"]], [1, 1])
        self.assertEqual([score for _, score, _ in items["必游景点"]], [1, 1])
        self.assertEqual([score for _, score, _ in items["景点开放"]], [1, None])
        self.assertIn("缺失", items["景点开放"][1][2])
        self.assertEqual([score for _, score, _ in items["指定住宿"]], [1])

    def test_only_one_boundary_and_unknown_date(self):
        self.expected["departure"] = None
        self.expected["arrival"]["date"] = None
        items = score_required_items(self.answer, self.expected, self.places, "Hong Kong")
        self.assertEqual(len(items["到离时间"]), 1)
        self.assertEqual(items["到离时间"][0][0], "抵达")
        self.assertIsNone(items["到离时间"][0][1])

    def test_missing_must_visit_and_conflicting_boundaries(self):
        self.expected["must_visit_place_ids"] = ["a", "b"]
        self.expected["arrival"]["date"] = "2026-11-21"
        items = score_required_items(self.answer, self.expected, self.places, "Hong Kong")
        self.assertEqual([score for _, score, _ in items["必游景点"]], [1, 0])
        self.assertEqual([score for _, score, _ in items["到离时间"]], [None, None])

    def test_no_specified_items(self):
        self.expected["arrival"] = None
        self.expected["departure"] = None
        self.expected["must_visit_place_ids"] = []
        self.expected["accommodation"] = None
        items = score_required_items(self.answer, self.expected, self.places, "Hong Kong")
        self.assertEqual(items["到离时间"], [])
        self.assertEqual(items["必游景点"], [])
        self.assertEqual(items["指定住宿"], [])


if __name__ == "__main__":
    unittest.main()
