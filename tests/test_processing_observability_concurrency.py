from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from memleaf.config import DEFAULT_MODEL_CONCURRENCY, load_config, save_config
from memleaf.model_execution import ModelExecutor
from memleaf.parallel_model import run_ordered_keyed_jobs
from memleaf.process_jobs import _safe_result
from memleaf.prompts import GATE_SYSTEM, SUMMARIZE_SYSTEM
from memleaf.update_coordinator import UpdateCoordinator
from memleaf.update_review import CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM
from memleaf.validation import ModelOutputError


class _VaultStub:
    def __init__(self, concurrency: int = 3):
        self._concurrency = concurrency

    def config(self):
        return {
            "process": {"model_concurrency": self._concurrency},
            "llm": {"diagnostic_logging": False},
        }


class _ServiceStub:
    def __init__(self, concurrency: int = 3):
        self.vault = _VaultStub(concurrency)


class _ParallelBackend:
    parallel_safe = True

    def __init__(self, outputs: list[str] | None = None):
        self.outputs = list(outputs or ['{"ok":true}'])
        self._lock = threading.Lock()

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        with self._lock:
            if len(self.outputs) > 1:
                return self.outputs.pop(0)
            return self.outputs[0]


class _UnsafeBackend:
    parallel_safe = False


def _concurrency_jobs(count: int, active: dict[str, int], lock: threading.Lock):
    jobs = []
    for index in range(count):
        def job(value=index):
            with lock:
                active["current"] += 1
                active["peak"] = max(active["peak"], active["current"])
            try:
                time.sleep(0.04)
                return {"decision": "ACCEPT", "value": value}
            finally:
                with lock:
                    active["current"] -= 1
        jobs.append(job)
    return jobs


class ProcessingObservabilityConcurrencyTests(unittest.TestCase):
    def test_model_concurrency_defaults_and_bounds_are_strict(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-concurrency-config-") as root:
            path = Path(root) / "config.yaml"
            loaded = load_config(path, vault=Path(root) / "vault")
            self.assertEqual(
                loaded["process"]["model_concurrency"],
                DEFAULT_MODEL_CONCURRENCY,
            )

            for value in (1, 3, 8):
                config = load_config(path, vault=Path(root) / "vault")
                config["process"]["model_concurrency"] = value
                save_config(path, config)
                self.assertEqual(load_config(path)["process"]["model_concurrency"], value)

            for value in (True, False, 0, 9, 2.5, "3"):
                with self.subTest(value=value):
                    config = load_config(path)
                    config["process"]["model_concurrency"] = value
                    with self.assertRaises(ValueError):
                        save_config(path, config)

    def test_model_metrics_count_retry_lengths_and_never_include_text(self):
        executor = ModelExecutor(_ServiceStub())
        backend = _ParallelBackend(["bad", '{"ok":true}'])

        def parse(raw: str):
            if raw == "bad":
                raise ModelOutputError("invalid", validation_detail="root_shape")
            return json.loads(raw)

        result = executor._complete_json_stage(
            backend,
            "TOP-SECRET-PROMPT",
            system="TOP-SECRET-SYSTEM",
            purpose="summarize",
            metric_stage="semantic_review",
            parser=parse,
        )
        self.assertEqual(result, {"ok": True})
        metrics = executor.metrics()
        stage = metrics["stages"]["semantic_review"]
        self.assertEqual(stage["call_count"], 2)
        self.assertEqual(stage["retry_count"], 1)
        self.assertEqual(stage["failed_calls"], 0)
        self.assertGreater(stage["input_chars"], 0)
        self.assertGreaterEqual(stage["input_bytes"], stage["input_chars"])
        self.assertEqual(stage["output_chars"], len("bad") + len('{"ok":true}'))
        serialized = json.dumps(metrics, ensure_ascii=False)
        self.assertNotIn("TOP-SECRET-PROMPT", serialized)
        self.assertNotIn("TOP-SECRET-SYSTEM", serialized)
        self.assertNotIn("bad", serialized)

    def test_review_scheduler_uses_configured_limit_and_preserves_result_order(self):
        executor = ModelExecutor(_ServiceStub(concurrency=3))
        coordinator = UpdateCoordinator(executor, audit=None, read_target=lambda _memory_id: None)
        active = {"current": 0, "peak": 0}
        lock = threading.Lock()
        outcomes = coordinator._run_review_jobs(
            _concurrency_jobs(7, active, lock),
            backend=_ParallelBackend(),
        )
        self.assertEqual(active["peak"], 3)
        self.assertEqual([item["value"] for item in outcomes], list(range(7)))

    def test_review_scheduler_keeps_unsafe_backend_serial(self):
        executor = ModelExecutor(_ServiceStub(concurrency=3))
        coordinator = UpdateCoordinator(executor, audit=None, read_target=lambda _memory_id: None)
        active = {"current": 0, "peak": 0}
        lock = threading.Lock()
        coordinator._run_review_jobs(
            _concurrency_jobs(4, active, lock),
            backend=_UnsafeBackend(),
        )
        self.assertEqual(active["peak"], 1)

    def test_keyed_scheduler_serializes_same_target_and_preserves_input_order(self):
        executor = ModelExecutor(_ServiceStub(concurrency=3))
        backend = _ParallelBackend()
        lock = threading.Lock()
        active = {"current": 0, "peak": 0}
        per_key_active: dict[str, int] = {}
        per_key_peak: dict[str, int] = {}

        def make_job(key: str, label: str):
            def job():
                with lock:
                    active["current"] += 1
                    active["peak"] = max(active["peak"], active["current"])
                    per_key_active[key] = per_key_active.get(key, 0) + 1
                    per_key_peak[key] = max(per_key_peak.get(key, 0), per_key_active[key])
                try:
                    time.sleep(0.04)
                    return label
                finally:
                    with lock:
                        active["current"] -= 1
                        per_key_active[key] -= 1
            return job

        jobs = [
            ("update:a", make_job("update:a", "a1")),
            ("update:b", make_job("update:b", "b1")),
            ("update:a", make_job("update:a", "a2")),
            ("create:c", make_job("create:c", "c1")),
            ("update:b", make_job("update:b", "b2")),
            ("update:a", make_job("update:a", "a3")),
        ]
        outcomes = run_ordered_keyed_jobs(executor, backend, jobs)
        self.assertEqual(outcomes, ["a1", "b1", "a2", "c1", "b2", "a3"])
        self.assertEqual(active["peak"], 3)
        self.assertTrue(per_key_peak)
        self.assertTrue(all(value == 1 for value in per_key_peak.values()))

    def test_background_result_projects_only_structural_model_metrics(self):
        result = _safe_result({
            "processed_turns": 1,
            "model_metrics": {
                "total": {
                    "call_count": 4,
                    "retry_count": 1,
                    "request_duration_ms": 1234,
                    "wall_clock_ms": 800,
                    "input_chars": 100,
                    "input_bytes": 120,
                    "output_chars": 40,
                    "output_bytes": 45,
                    "failed_calls": 0,
                    "max_in_flight": 3,
                    "prompt": "DO-NOT-PERSIST",
                },
                "stages": {
                    "semantic_review": {"call_count": 3, "max_in_flight": 3},
                    "evil-stage": {"call_count": 99, "body": "DO-NOT-PERSIST"},
                },
                "response": "DO-NOT-PERSIST",
            },
        })
        self.assertEqual(result["model_metrics"]["total"]["call_count"], 4)
        self.assertEqual(result["model_metrics"]["total"]["max_in_flight"], 3)
        self.assertEqual(
            result["model_metrics"]["stages"],
            {"semantic_review": {"call_count": 3, "max_in_flight": 3}},
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("DO-NOT-PERSIST", serialized)
        self.assertNotIn("evil-stage", serialized)

    def test_gate_and_summary_contracts_preserve_meaning_and_attribution(self):
        gate = " ".join(GATE_SYSTEM.split())
        summary = " ".join(SUMMARIZE_SYSTEM.split())
        for text in (gate, summary):
            self.assertIn("business/workstream/background context", text)
            self.assertIn("number", text)
            self.assertIn("implementation", text)
        self.assertIn("Candidate semantic completeness is mandatory", gate)
        self.assertIn("ownership/affiliation", gate)
        self.assertIn("implementation context alone", gate)
        self.assertIn("Semantic completeness is required", summary)
        self.assertIn("Scope metadata does not substitute", summary)
        self.assertIn("owning subject", summary)
        self.assertIn("Existing memories cannot supply a new relationship", summary)

    def test_semantic_review_contract_requires_completeness_not_only_non_invention(self):
        for system in (CREATE_SEMANTIC_REVIEW_SYSTEM, UPDATE_SEMANTIC_REVIEW_SYSTEM):
            with self.subTest(system=system[:32]):
                flattened = " ".join(system.split())
                self.assertIn("Semantic completeness is as important as non-invention", flattened)
                self.assertIn("meaning-defining", flattened)
                self.assertIn("business/workstream/background context", flattened)
                self.assertIn("Scope metadata alone does not substitute", flattened)
                self.assertIn("implementation context", flattened)
                self.assertIn("Never add an owner", flattened)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
