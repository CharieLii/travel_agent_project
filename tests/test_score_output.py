"""Regression tests for score JSON, repeated places and batch persistence."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "baseline"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
import baseline
import score_baseline
from route_scoring import evaluate_day, transfer_feasibility
from render_results import render_answer


class ScoreOutputTest(unittest.TestCase):
    def test_detour_denominator_includes_unassessable_days(self):
        report = {"case_id": "x", "format_valid": True, "output_status": "plan",
                  "required_metrics": {}, "景点重复率": None,
                  "preference_metrics": {"景点类型偏好": None, "舒适度偏好": None,
                                         "顺路达标率": {"passed": 2, "scored": 2, "total_days": 3,
                                                   "days": [{"transfer_feasible": None,
                                                             "transfer_reason": "步行耗时缺失"}]}}}
        result = score_baseline.final_values(report)["顺路达标率"]
        self.assertEqual(result["数值"], "2/3")
        self.assertIn("步行耗时缺失", result["原因"])

    def test_itinerary_feasibility_includes_unknown_days_in_denominator(self):
        report = {"case_id": "x", "format_valid": True, "output_status": "plan",
                  "required_metrics": {}, "景点重复率": None,
                  "preference_metrics": {"景点类型偏好": None, "舒适度偏好": None,
                    "顺路达标率": {"passed": 1, "total_days": 3, "days": [
                        {"transfer_feasible": True}, {"transfer_feasible": False},
                        {"transfer_feasible": None, "transfer_reason": "路线数据缺失"}]}}}
        result = score_baseline.final_values(report)["行程可行率"]
        self.assertEqual(result["数值"], "1/3")
        self.assertIn("路线数据缺失", result["原因"])

    def test_transfer_uses_the_model_mode_and_unknown_is_not_failure(self):
        day = {"date": "2026-11-20", "activities": [
            {"place_id": "a", "end_time": "10:00"},
            {"place_id": "b", "start_time": "10:05",
             "transport_from_previous": {"from_place_id": "a", "recommended_mode": "transit"}}]}
        routes = {("a", "b"): {"walk_only_seconds": 180, "transit_total_seconds": 600,
                                "transit_walk_seconds": 60}}
        self.assertIs(transfer_feasibility(day, routes)[0], False)
        day["activities"][1]["transport_from_previous"]["recommended_mode"] = "walk"
        self.assertIs(transfer_feasibility(day, routes)[0], True)
        routes[("a", "b")]["walk_only_seconds"] = None
        self.assertIsNone(transfer_feasibility(day, routes)[0])

    def test_route_legs_show_modes_and_optional_hotel(self):
        places = {key: {"opening_hours": {"fri": ["00:00", "24:00"]}}
                  for key in ("hotel", "a", "b")}
        places["hotel"]["_is_accommodation"] = True
        routes = {}
        for origin in places:
            for destination in places:
                if origin != destination:
                    routes[origin, destination] = {"walk_only_seconds": 120,
                                                    "transit_total_seconds": 60,
                                                    "transit_walk_seconds": 10}
        day = {"date": "2026-11-20", "activities": [
            {"place_id": "a", "start_time": "10:00", "end_time": "11:00",
             "transport_from_previous": {"from_place_id": "hotel", "recommended_mode": "walk"}},
            {"place_id": "b", "start_time": "12:00", "end_time": "13:00",
             "transport_from_previous": {"from_place_id": "a", "recommended_mode": "transit"}},
            {"place_id": "hotel", "start_time": "13:05", "end_time": "13:05",
             "transport_from_previous": {"from_place_id": "b", "recommended_mode": "transit"}}]}
        result = evaluate_day(day, "hotel", {}, routes, places)
        self.assertEqual([leg["mode"] for leg in result["model_legs"]], ["walk", "transit", "transit"])
        self.assertEqual(result["model_legs"][-1]["destination"], "hotel")
        self.assertEqual(result["optimal_legs"][0]["origin"], "hotel")
        self.assertEqual(result["optimal_legs"][-1]["destination"], "hotel")
        self.assertAlmostEqual(sum(leg["seconds"] for leg in result["optimal_legs"]), result["optimal_seconds"])
        day["activities"].pop()
        day["activities"][0]["transport_from_previous"]["from_place_id"] = None
        without_hotel = evaluate_day(day, None, {}, routes, places)
        self.assertEqual(len(without_hotel["model_legs"]), 1)
        self.assertEqual(without_hotel["model_legs"][0]["origin"], "a")

    def test_render_hides_missing_transport_durations(self):
        activity = {"place_id": "a", "transport_from_previous": {
            "from_place_id": "hotel", "recommended_mode": "walk",
            "walking_minutes": None, "public_transit_minutes": None}}
        raw = json.dumps({"days": [{"activities": [activity]}]})
        text = render_answer(raw, {"a": "景点", "hotel": "酒店"})
        self.assertIn("建议步行，具体耗时待确认", text)
        self.assertNotIn("耗时 未提供", text)

    def test_repeat_rate_counts_distinct_repeated_places(self):
        activities = [{"place_id": item} for item in ("a", "a", "a", "b", "c", "c")]
        score = score_baseline.repetition_metric(activities)
        self.assertEqual(score["repeated_places"], 2)
        self.assertEqual(score["unique_places"], 3)
        self.assertAlmostEqual(score["rate"], 2 / 3)
        self.assertIsNone(score_baseline.repetition_metric([])["rate"])

    def test_real_result_structured_and_unavailable(self):
        data = Path(__file__).resolve().parents[1] / "dataset" / "travel-dialogue-dev.json"
        case = json.loads(data.read_text())["cases"][0]
        activity = {"place_id": "hk_museum_of_art", "start_time": "10:00", "end_time": "11:00",
                    "note": "", "transport_from_previous": {
                        "from_place_id": "sheraton_hong_kong_hotel_and_towers",
                        "recommended_mode": "walk", "walking_minutes": None,
                        "public_transit_minutes": None}}
        answer = {"status": "plan", "message": "", "days": [
            {"date": "2026-11-20", "activities": [activity, activity]}],
                  "assumptions": [], "questions": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.jsonl"
            path.write_text(json.dumps({"case_id": case["id"], "status": "ok",
                                        "output": json.dumps(answer)}) + "\n")
            with contextlib.redirect_stdout(io.StringIO()):
                result = score_baseline.main([str(path)])
            report = result["cases"][0]
            self.assertEqual(report["景点重复率"]["数值"], "1/1")
            self.assertEqual(report["必游景点"]["数值"], "1/2")
            self.assertEqual(report["到离时间"]["数值"], "2/2")
            self.assertEqual(report["景点开放"]["数值"], "2/2")
            self.assertIsNone(report["景点类型偏好"]["数值"])
            self.assertEqual(report["景点类型偏好"]["原因"], "用户未指定景点类型偏好")
            self.assertEqual(set(report), {"case_id", "输出格式通过", "生成行程", "目的地", "到离时间", "必游景点",
                                           "景点开放", "指定住宿", "景点类型偏好",
                                           "舒适度偏好", "行程可行率", "顺路达标率", "景点重复率"})

    def test_batch_keeps_per_case_scores_and_one_summary(self):
        cases = baseline.read_json(baseline.DATA / "travel-dialogue-dev.json")
        cases["cases"] = cases["cases"][:2]
        answer = json.dumps({"status": "help", "message": "帮助", "days": [],
                             "assumptions": [], "questions": []})
        reply = {"output": answer, "latency_seconds": 1, "usage": None,
                 "response_model": None, "finish_reason": None}
        args = SimpleNamespace(split="dev", case=None, preview=False, output=None)
        with tempfile.TemporaryDirectory() as tmp:
            read_json = baseline.read_json
            def read_test_data(path):
                return cases if path.name == "travel-dialogue-dev.json" else read_json(path)
            with patch.object(baseline, "ROOT", Path(tmp)), patch.object(baseline, "read_json", side_effect=read_test_data), \
                 patch.object(baseline, "build_model", return_value=object()), \
                 patch.object(baseline, "generate", side_effect=[reply, RuntimeError("offline")]), \
                 patch.dict(baseline.os.environ, {"TRAVEL_MODEL": "test"}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(baseline.batch(args, "{}"), 1)
            folders = list((Path(tmp) / "results" / "baseline").iterdir())
            self.assertEqual(len(folders), 1)
            summary = json.loads((folders[0] / "scores.json").read_text())
            self.assertEqual(set(summary), {"summary", "cases"})
            self.assertEqual(summary["summary"]["输出格式通过率"], "1/2")
            self.assertEqual(summary["summary"]["生成行程率"], "0/2")
            self.assertEqual([row["case_id"] for row in summary["cases"]], ["DEV-01", "DEV-02"])
            self.assertIsNone(summary["cases"][0]["景点重复率"]["数值"])
            self.assertIsNone(summary["cases"][1]["景点重复率"]["数值"])
            for index, case_id in enumerate(("DEV-01", "DEV-02")):
                folder = folders[0] / case_id
                self.assertTrue((folder / "result.score.md").exists())
                self.assertTrue((folder / "result.score.json").exists())
                single = json.loads((folder / "result.score.json").read_text())
                self.assertEqual(single["cases"], [summary["cases"][index]])

    def test_historical_and_renamed_route_aware_versions(self):
        activity = {"place_id": "hk_museum_of_art", "start_time": "11:00", "end_time": "12:00",
                    "note": "", "transport_from_previous": {
                        "from_place_id": "sheraton_hong_kong_hotel_and_towers",
                        "recommended_mode": "walk", "walking_minutes": 5,
                        "public_transit_minutes": None}}
        answer = {"status": "plan", "message": "", "days": [
            {"date": "2026-11-20", "activities": [activity]}],
                  "assumptions": [], "questions": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.jsonl"
            for version in ("baseline-v3", "improvement-v2", "improvement-v2-prompt-v2", "baseline-no-routes-v1"):
                path.write_text(json.dumps({"case_id": "DEV-01", "status": "ok",
                                            "prompt_version": version, "output": json.dumps(answer)}) + "\n")
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    score_baseline.main([str(path)])
                expected = "结构化事实=失败" if version == "baseline-no-routes-v1" else "结构化事实=通过"
                self.assertIn(expected, captured.getvalue())


if __name__ == "__main__":
    unittest.main()
