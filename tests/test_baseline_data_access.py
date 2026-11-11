"""Verify the two single-call configurations differ in generation data access."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))


class BaselineDataAccessTest(unittest.TestCase):
    def test_previews_differ_only_in_available_catalog(self):
        prompts = []
        for path in ("baseline/baseline.py", "ImprovementV2/addRouteSet.py"):
            output = subprocess.check_output([
                sys.executable, "-B", str(ROOT / path), "run",
                "--split", "dev", "--case", "DEV-01", "--preview",
            ], cwd=ROOT, text=True)
            prompt = json.loads(output)
            system = prompt["messages"][0][1]
            context = json.loads(system.split("\n本地资料：", 1)[1])
            self.assertNotIn("expected", context)
            self.assertEqual(prompt["case_id"], "DEV-01")
            prompts.append((context, prompt["messages"][1:]))
        without_routes, with_routes = prompts
        self.assertEqual(set(without_routes[0]), {"attractions", "accommodations"})
        self.assertEqual(set(with_routes[0]), {"attractions", "accommodations", "routes"})
        self.assertEqual(without_routes[0]["attractions"], with_routes[0]["attractions"])
        self.assertEqual(without_routes[0]["accommodations"], with_routes[0]["accommodations"])
        self.assertEqual(without_routes[1], with_routes[1])
        self.assertEqual(len(with_routes[0]["routes"]["records"]), 420)

    def test_no_route_access_until_after_model_response(self):
        spec = importlib.util.spec_from_file_location(
            "baseline_no_routes_test", ROOT / "baseline" / "baseline.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        phase = {"model_finished": False, "route_reads": 0}
        original_read = Path.read_text

        def guarded_read(path, *args, **kwargs):
            if path.name == "hong-kong-routes.json":
                self.assertTrue(phase["model_finished"], "route data read during model generation")
                phase["route_reads"] += 1
            return original_read(path, *args, **kwargs)

        def generate(model, messages):
            prompt = messages[0][1]
            self.assertNotIn('"routes":', prompt)
            self.assertNotIn("expected", prompt)
            phase["model_finished"] = True
            answer = {"status": "help", "message": "可规划香港行程", "days": [],
                      "assumptions": [], "questions": []}
            return {"output": json.dumps(answer, ensure_ascii=False),
                    "latency_seconds": 0.0, "usage": None,
                    "response_model": "test", "finish_reason": "stop"}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(Path, "read_text", guarded_read), \
             patch.object(module, "ROOT", Path(tmp)), \
             patch.object(module, "build_model", return_value=object()), \
             patch.object(module, "generate", side_effect=generate), \
             patch.dict(module.os.environ, {"TRAVEL_MODEL": "test"}), \
             contextlib.redirect_stdout(io.StringIO()):
            catalog = module.catalog_text()
            args = SimpleNamespace(split="dev", case="DEV-01", preview=False, output=None)
            self.assertEqual(module.batch(args, catalog), 0)
            runs = list((Path(tmp) / "results" / "baseline").iterdir())
            row = json.loads((runs[0] / "DEV-01" / "result.jsonl").read_text())
            self.assertEqual(row["prompt_version"], "baseline-no-routes-v2")
            self.assertEqual(row["method"], "llm_direct_no_routes")
            self.assertTrue((runs[0] / "scores.json").exists())
            self.assertGreater(phase["route_reads"], 0)

    def test_same_plan_gets_same_scores_in_both_configurations(self):
        # Use a synthetic itinerary so this check does not depend on saved experiment results.
        answer = {"status": "plan", "message": "行程", "days": [
            {"date": "2026-11-20", "activities": [
                {"place_id": "hk_museum_of_art", "start_time": "11:30", "end_time": "12:30",
                 "note": "", "transport_from_previous": {
                     "from_place_id": "sheraton_hong_kong_hotel_and_towers",
                     "recommended_mode": "walk", "walking_minutes": None,
                     "public_transit_minutes": None}},
                {"place_id": "avenue_of_stars", "start_time": "13:00", "end_time": "14:00",
                 "note": "", "transport_from_previous": {
                     "from_place_id": "hk_museum_of_art", "recommended_mode": "walk",
                     "walking_minutes": None, "public_transit_minutes": None}},
            ]}], "assumptions": [], "questions": []}
        reply = {"output": json.dumps(answer, ensure_ascii=False), "latency_seconds": 0,
                 "usage": None, "response_model": "test", "finish_reason": "stop"}
        scores = []
        with tempfile.TemporaryDirectory() as tmp:
            for index, script in enumerate(("baseline/baseline.py", "ImprovementV2/addRouteSet.py")):
                spec = importlib.util.spec_from_file_location(f"compare_test_{index}", ROOT / script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                output = Path(tmp) / f"{index}.jsonl"
                args = SimpleNamespace(split="dev", case="DEV-01", preview=False, output=output)
                with patch.object(module, "build_model", return_value=object()), \
                     patch.object(module, "generate", return_value=reply), \
                     patch.dict(module.os.environ, {"TRAVEL_MODEL": "test"}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.batch(args, module.catalog_text()), 0)
                scores.append(json.loads(output.with_name(f"{index}.score.json").read_text()))
        self.assertEqual(scores[0], scores[1])

    def test_improvement_default_directory_and_version(self):
        spec = importlib.util.spec_from_file_location(
            "improvement_test", ROOT / "ImprovementV2" / "addRouteSet.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        answer = {"status": "help", "message": "帮助", "days": [], "assumptions": [], "questions": []}
        reply = {"output": json.dumps(answer), "latency_seconds": 0, "usage": None,
                 "response_model": "test", "finish_reason": "stop"}
        args = SimpleNamespace(split="dev", case="DEV-01", preview=False, output=None)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(module, "ROOT", Path(tmp)), \
             patch.object(module, "build_model", return_value=object()), \
             patch.object(module, "generate", return_value=reply), \
             patch.dict(module.os.environ, {"TRAVEL_MODEL": "test"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.batch(args, module.catalog_text()), 0)
            run = next((Path(tmp) / "results" / "ImprovementV2").iterdir())
            row = json.loads((run / "DEV-01" / "result.jsonl").read_text())
            self.assertEqual(row["prompt_version"], "improvement-v2-prompt-v2")
            self.assertTrue((run / "scores.json").exists())


if __name__ == "__main__":
    unittest.main()
