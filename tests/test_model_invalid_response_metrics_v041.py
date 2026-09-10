\
"""Regression coverage for response-vs-transport model metrics."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from memleaf.llm import ModelError
from memleaf.model_execution import ModelExecutor


class _QueueBackend:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ModelInvalidResponseMetricsTests(unittest.TestCase):
    @staticmethod
    def _executor() -> ModelExecutor:
        service = SimpleNamespace(
            vault=SimpleNamespace(config=lambda: {"llm": {"diagnostic_logging": False}})
        )
        return ModelExecutor(service)

    @staticmethod
    def _parser(raw: str):
        return {"ok": True}

    def test_non_text_backend_returns_are_failed_invalid_outputs(self):
        executor = self._executor()
        backend = _QueueBackend([{"bad": 1}, {"bad": 2}, {"bad": 3}])
        with self.assertRaises(ModelError) as caught:
            executor._complete_json_stage(
                backend, "input", system="system", purpose="gate", parser=self._parser
            )
        self.assertEqual(caught.exception.code, "model_invalid_response")
        metrics = executor.metrics()
        self.assertEqual(metrics["total"]["call_count"], 3)
        self.assertEqual(metrics["total"]["failed_calls"], 3)
        self.assertEqual(metrics["total"]["invalid_output_count"], 3)
        self.assertTrue(all(call["failed"] for call in metrics["calls"]))
        self.assertTrue(all(call["invalid_output"] for call in metrics["calls"]))

    def test_backend_model_invalid_response_is_counted_as_invalid_output(self):
        failures = [
            ModelError(
                "synthetic invalid response",
                code="model_invalid_response",
                stage="gate",
                validation_reason="invalid_json",
            )
            for _ in range(3)
        ]
        executor = self._executor()
        with self.assertRaises(ModelError):
            executor._complete_json_stage(
                _QueueBackend(failures), "input", system="system", purpose="gate", parser=self._parser
            )
        metrics = executor.metrics()
        self.assertEqual(metrics["total"]["failed_calls"], 3)
        self.assertEqual(metrics["total"]["invalid_output_count"], 3)
        self.assertEqual(metrics["operations"]["gate_semantic_retry"]["invalid_output_count"], 2)
        self.assertTrue(all(call["failed"] and call["invalid_output"] for call in metrics["calls"]))

    def test_transport_failure_stays_failed_but_not_invalid_output(self):
        executor = self._executor()
        with self.assertRaises(ModelError):
            executor._complete_json_stage(
                _QueueBackend([RuntimeError("synthetic transport failure")]),
                "input", system="system", purpose="gate", parser=self._parser,
            )
        metrics = executor.metrics()
        self.assertEqual(metrics["total"]["call_count"], 1)
        self.assertEqual(metrics["total"]["failed_calls"], 1)
        self.assertEqual(metrics["total"]["invalid_output_count"], 0)
        self.assertTrue(metrics["calls"][0]["failed"])
        self.assertFalse(metrics["calls"][0]["invalid_output"])


if __name__ == "__main__":
    unittest.main()
