from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from memleaf.config import default_config
from memleaf.llm import ModelUnavailable
from benchmarks.p1.fixture import build_plan, evaluation_template, load_cases, prepare_case_vault
from benchmarks.p1.reporting import safe_failure, snapshot
from benchmarks.p1.run_baseline import BudgetedModel, _route_identity, main


class _FakeBackend:
    parallel_safe = True
    provider = "synthetic"
    model = "synthetic-model"

    def __init__(self):
        self.calls = 0
        self.metrics = {}

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls += 1
        self.metrics = {"prompt_tokens": 3, "completion_tokens": 1}
        return "{}"

    def consume_call_metrics(self):
        value, self.metrics = self.metrics, {}
        return value


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
        self.assertEqual(self.data["baseline_refs"]["b0"], "9e53b1e991e66315fd9fce51aca71ff6ac0eb022")
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

    def test_evaluation_template_forces_low_without_mutating_source(self):
        config = default_config("/tmp/p1-source")
        config["llm"]["provider"] = "deepseek"
        config["llm"]["model"] = "synthetic-model"
        config["llm"]["thinking"] = {"gate": "high", "summarize": "high", "compact": "high"}
        evaluated = evaluation_template(config)
        self.assertEqual(evaluated["llm"]["thinking"], {"gate": "low", "summarize": "low", "compact": "low"})
        self.assertEqual(config["llm"]["thinking"]["gate"], "high")
        self.assertFalse(evaluated["llm"]["diagnostic_logging"])

    def test_budgeted_model_never_delegates_past_cap_and_keeps_metrics(self):
        backend = _FakeBackend()
        model = BudgetedModel(backend, 2)
        self.assertTrue(model.parallel_safe)
        self.assertEqual(model.complete("one"), "{}")
        self.assertEqual(model.consume_call_metrics()["prompt_tokens"], 3)
        self.assertEqual(model.complete("two"), "{}")
        with self.assertRaises(ModelUnavailable):
            model.complete("three")
        self.assertEqual(model.calls, 2)
        self.assertEqual(backend.calls, 2)

    def test_execute_requires_explicit_model_call_budget_before_reading_config(self):
        with self.assertRaisesRegex(SystemExit, "--max-model-calls"):
            main([
                "--execute",
                "--config-template", "/definitely/missing/config.yaml",
                "--output", "/tmp/never-written.json",
            ])

    def test_route_identity_never_contains_secret_or_base_url(self):
        config = default_config("/tmp/p1-route")
        config["llm"].update({
            "mode": "api",
            "provider": "deepseek",
            "protocol": "openai",
            "base_url": "https://api.deepseek.com",
            "api_key": "synthetic-secret-never-print",
            "model": "deepseek-chat",
        })
        identity = _route_identity(evaluation_template(config))
        encoded = json.dumps(identity)
        self.assertTrue(identity["api_route_ready"])
        self.assertTrue(identity["credential_configured"])
        self.assertNotIn("synthetic-secret-never-print", encoded)
        self.assertNotIn("api.deepseek.com", encoded)
        self.assertNotIn("base_url", identity)

    def test_safe_failure_does_not_persist_exception_message(self):
        error = ModelUnavailable("synthetic-secret-in-message")
        error.stage = "gate"
        projected = safe_failure(error)
        self.assertNotIn("synthetic-secret-in-message", json.dumps(projected))
        self.assertEqual(projected["error_type"], "ModelUnavailable")
        self.assertEqual(projected["stage"], "gate")


if __name__ == "__main__":
    unittest.main()
