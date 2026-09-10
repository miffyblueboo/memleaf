from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from memleaf.config import default_config
from benchmarks.p1.fixture import build_plan, load_cases, prepare_case_vault
from benchmarks.p1.reporting import snapshot


class P1BaselineAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent
        cls.fixture = cls.root / "cases-v1.json.gz"
        cls.data = load_cases(cls.fixture)

    def test_fixture_hash_manifest_and_b0(self):
        manifest = json.loads((self.root / "cases-v1-manifest.json").read_text(encoding="utf-8"))
        raw = gzip.decompress(self.fixture.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["decompressed_sha256"])
        self.assertEqual(len(self.data["cases"]), 10)
        self.assertEqual(self.data["baseline_refs"]["b0"], "e2106bc9d55b8109ebd8df54eff7c1bad0c11180")
        self.assertEqual(self.data["status"], "DESIGN_ONLY_NOT_RUN_WITH_REAL_MODEL")
        self.assertEqual(self.data["repetitions_per_case_per_arm"], 3)

    def test_default_exploration_plan_is_30_process_runs(self):
        plan = build_plan(self.data, self.data["cases"], 3)
        self.assertEqual(plan["planned_process_runs"], 30)
        self.assertEqual(plan["thinking"], "low")

    def test_prepare_case_uses_fresh_seed_and_fixed_timestamps_without_model(self):
        case = next(item for item in self.data["cases"] if item["id"] == "AB05_update")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            config = default_config(root)
            config["llm"]["provider"] = "synthetic-provider"
            config["llm"]["model"] = "synthetic-model"
            service = prepare_case_vault(root, case, template=config, fixed_time=self.data["fixed_event_time"])
            before = snapshot(service)
            self.assertEqual([item["memory_id"] for item in before["knowledge"]], ["alpha-config"])
            self.assertEqual(before["history"], [])
            text = service.vault.session_path(case["source"], case["session_id"]).read_text(encoding="utf-8")
            self.assertIn('"timestamp":"2026-09-09T09:00:00Z"', text)
            self.assertIn('"timestamp":"2026-09-09T09:00:05Z"', text)

    def test_scope_registry_is_case_local(self):
        case = next(item for item in self.data["cases"] if item["id"] == "AB04_project_platform")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            config = default_config(root)
            config["llm"]["provider"] = "synthetic-provider"
            config["llm"]["model"] = "synthetic-model"
            service = prepare_case_vault(root, case, template=config, fixed_time=self.data["fixed_event_time"])
            self.assertEqual(set(service.vault.config()["scopes"]), {"project:星河", "project:Orion"})


if __name__ == "__main__":
    unittest.main()
