from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from memleaf.llm.base import CallableBackend, HTTPModelBackend, ModelError
from memleaf.llm.router import ModelRouter
from memleaf.llm.thinking import requested_thinking_mode
from memleaf.model_execution import ModelExecutor
from memleaf.process_common import _failure_metadata, _model_output_statistics
from memleaf.processing import Processor
from memleaf.single_pass_memory_planner import SinglePassMemoryPlanner
from memleaf.validation import ModelOutputError


class Api:
    parallel_safe = True
    structured_batch_safe = True
    single_pass_safe = True
    provider = "deepseek"
    model = "deepseek-v4-flash-vision-exp"
    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        return "{}"


class Vault:
    def config(self): return {"llm": {}}


class Service:
    vault = Vault()


class SequenceBackend:
    single_pass_safe = True
    parallel_safe = True
    structured_batch_safe = True
    provider = "test"
    model = "test"
    def __init__(self, values): self.values=list(values); self.calls=[]
    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        self.calls.append((prompt, system, purpose))
        return self.values.pop(0)


class B3ActivationTests(unittest.TestCase):
    def test_fixed_api_router_exposes_single_pass_but_host_routes_do_not(self):
        api = Api()
        self.assertTrue(ModelRouter(mode="api", api=api).single_pass_safe)
        self.assertTrue(ModelRouter(mode="auto", api=api).single_pass_safe)
        self.assertFalse(ModelRouter(mode="auto", api=api, host=lambda prompt: "{}").single_pass_safe)
        self.assertFalse(ModelRouter(mode="host", host=lambda prompt: "{}").single_pass_safe)
        self.assertFalse(CallableBackend(lambda prompt: "{}").single_pass_safe)

    def test_builtin_http_backend_advertises_single_pass(self):
        backend = object.__new__(HTTPModelBackend)
        self.assertTrue(backend.single_pass_safe)

    def test_single_pass_thinking_defaults_low(self):
        self.assertEqual(requested_thinking_mode({}, "single_pass"), "low")
        self.assertEqual(requested_thinking_mode({"single_pass": "disabled"}, "single_pass"), "disabled")

    def test_processor_constructs_single_pass_planner(self):
        # Avoid constructing all collaborators: this assertion protects the
        # production wiring without relying on source-text inspection.
        self.assertIn("SinglePassMemoryPlanner", Processor.__init__.__code__.co_names)

    def test_single_pass_failure_metadata_and_stats_are_stage_specific(self):
        error = ModelOutputError("bad", validation_detail="unknown_fields")
        error.stage = "single_pass"
        error.attempt_count = 2
        code, stage, reason, detail, attempts = _failure_metadata(error)
        self.assertEqual(stage, "single_pass")
        self.assertEqual(attempts, 2)
        stats = _model_output_statistics(json.dumps({
            "protocol_version": "b3-single-pass-v1",
            "items": [{"candidate_id": "c1", "decision": "CREATE", "evidence": [], "memory": {}, "extra": "secret-like"}],
            "no_memory": [],
        }), "single_pass")
        self.assertEqual(stats["candidate_count"], 1)
        self.assertEqual(stats["missing_fields_count"], 0)
        self.assertEqual(stats["unknown_fields_count"], 1)
        self.assertNotIn("extra", json.dumps(stats))
        self.assertNotIn("secret-like", json.dumps(stats))

    def test_single_pass_gets_only_one_repair_and_never_gate_repair_contract(self):
        executor = ModelExecutor(Service())
        backend = SequenceBackend([
            '{"bad":true}',
            '{"protocol_version":"b3-single-pass-v1","items":[],"no_memory":[]}',
        ])
        def parser(raw):
            value = json.loads(raw)
            if set(value) != {"protocol_version", "items", "no_memory"}:
                raise ModelOutputError("bad schema", validation_detail="unknown_fields")
            return value
        result = executor._complete_json_stage(
            backend,
            "ORIGINAL",
            system="B3SYSTEM",
            purpose="single_pass",
            parser=parser,
            max_attempts=2,
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(len(backend.calls), 2)
        self.assertTrue(all(call[2] == "single_pass" for call in backend.calls))
        self.assertTrue(all(call[1] == "B3SYSTEM" for call in backend.calls))
        self.assertNotIn("Gate JSON", backend.calls[1][0])
        self.assertIn("unknown_fields", backend.calls[1][0])
        metrics = executor.metrics()
        self.assertEqual(metrics["stages"]["single_pass"]["call_count"], 2)
        self.assertEqual(metrics["stages"]["single_pass"]["retry_count"], 1)

    def test_single_pass_stops_after_second_invalid_output(self):
        executor = ModelExecutor(Service())
        backend = SequenceBackend(['{}', '{}', '{}'])
        def parser(raw):
            raise ModelOutputError("bad", validation_detail="invalid_evidence")
        with self.assertRaises(ModelOutputError) as caught:
            executor._complete_json_stage(
                backend, "ORIGINAL", system="B3SYSTEM", purpose="single_pass",
                parser=parser, max_attempts=2,
            )
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(caught.exception.attempt_count, 2)


if __name__ == "__main__":
    unittest.main()
