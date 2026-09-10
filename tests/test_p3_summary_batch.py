from __future__ import annotations

import json
import unittest

from memleaf.llm.router import ModelRouter
from memleaf.summary_batch import BATCH_SUMMARIZE_SYSTEM, run_summary_jobs_with_create_batching
from memleaf.validation import ModelOutputError, parse_strict_json


class _BatchBackend:
    structured_batch_safe = True
    parallel_safe = True


class _LegacyBackend:
    parallel_safe = True


class _FakeExecutor:
    def __init__(self, responses, *, workers=1):
        self.responses = list(responses)
        self.calls = []
        self.workers = workers

    def max_parallel_calls(self, _backend):
        return self.workers

    def _complete_json_stage(
        self,
        _backend,
        prompt,
        *,
        system,
        purpose,
        parser,
        diagnostic_context=None,
    ):
        self.calls.append({
            "prompt": prompt,
            "system": system,
            "purpose": purpose,
            "diagnostic_context": diagnostic_context,
        })
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return parser(response)


def _parser(expected_title: str):
    def parse(raw: str):
        value = parse_strict_json(raw)
        if not isinstance(value, dict) or value.get("title") != expected_title:
            raise ModelOutputError("bad item", validation_detail="schema_violation")
        return value
    return parse


def _job(
    item_id: str,
    *,
    title: str,
    batchable: bool,
    key: str | None = None,
    singles: list[str],
    order: list[str] | None = None,
):
    def single():
        singles.append(item_id)
        if order is not None:
            order.append(item_id)
        return {"status": "ok", "summary": {"title": title, "single": True}}

    return {
        "key": key or f"create:{item_id}",
        "call": single,
        "batchable": batchable,
        "item_id": item_id,
        "prompt": f"PROMPT-{item_id}",
        "parser": _parser(title),
        "diagnostic_context": {"item": item_id},
    }


class P3SummaryBatchTests(unittest.TestCase):
    def test_two_independent_creates_use_one_batch_call(self):
        singles: list[str] = []
        response = json.dumps({
            "items": [
                {"item_id": "c1", "result": {"title": "one"}},
                {"item_id": "c2", "result": {"title": "two"}},
            ]
        })
        executor = _FakeExecutor([response])
        jobs = [
            _job("c1", title="one", batchable=True, singles=singles),
            _job("c2", title="two", batchable=True, singles=singles),
        ]
        result = run_summary_jobs_with_create_batching(executor, _BatchBackend(), jobs)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(singles, [])
        self.assertEqual([item["summary"]["title"] for item in result], ["one", "two"])
        self.assertIn('"item_id":"c1"', executor.calls[0]["prompt"])
        self.assertIn('"item_id":"c2"', executor.calls[0]["prompt"])

    def test_invalid_one_item_falls_back_only_that_item(self):
        singles: list[str] = []
        response = json.dumps({
            "items": [
                {"item_id": "c1", "result": {"title": "one"}},
                {"item_id": "c2", "result": {"title": "WRONG"}},
            ]
        })
        executor = _FakeExecutor([response])
        jobs = [
            _job("c1", title="one", batchable=True, singles=singles),
            _job("c2", title="two", batchable=True, singles=singles),
        ]
        result = run_summary_jobs_with_create_batching(executor, _BatchBackend(), jobs)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(singles, ["c2"])
        self.assertNotIn("single", result[0]["summary"])
        self.assertTrue(result[1]["summary"]["single"])

    def test_invalid_batch_envelope_falls_back_every_member(self):
        singles: list[str] = []
        executor = _FakeExecutor([json.dumps({"items": [{"item_id": "c1", "result": {"title": "one"}}]})])
        jobs = [
            _job("c1", title="one", batchable=True, singles=singles),
            _job("c2", title="two", batchable=True, singles=singles),
        ]
        result = run_summary_jobs_with_create_batching(executor, _BatchBackend(), jobs)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(singles, ["c1", "c2"])
        self.assertTrue(all(item["summary"]["single"] for item in result))

    def test_router_exposes_batch_capability_only_for_fixed_safe_api_route(self):
        api = _BatchBackend()
        api.complete = lambda *args, **kwargs: "{}"
        api.provider = "synthetic-api"
        api.model = "synthetic-model"
        api_router = ModelRouter(mode="api", api=api)
        self.assertTrue(api_router.structured_batch_safe)
        auto_api_router = ModelRouter(mode="auto", api=api)
        self.assertTrue(auto_api_router.structured_batch_safe)
        host = _LegacyBackend()
        host.complete = lambda *args, **kwargs: "{}"
        host.provider = "host"
        host.model = "host-model"
        auto_host_router = ModelRouter(mode="auto", host=host, api=api)
        self.assertFalse(auto_host_router.structured_batch_safe)
        self.assertIn("replaces only the outer single-item return shape", BATCH_SUMMARIZE_SYSTEM)

    def test_backend_without_explicit_batch_capability_uses_original_single_calls(self):
        singles: list[str] = []
        executor = _FakeExecutor([])
        jobs = [
            _job("c1", title="one", batchable=True, singles=singles),
            _job("c2", title="two", batchable=True, singles=singles),
        ]
        result = run_summary_jobs_with_create_batching(executor, _LegacyBackend(), jobs)
        self.assertEqual(singles, ["c1", "c2"])
        self.assertEqual(executor.calls, [])
        self.assertTrue(all(item["summary"]["single"] for item in result))

    def test_non_batch_update_jobs_keep_same_key_order(self):
        singles: list[str] = []
        order: list[str] = []
        executor = _FakeExecutor([], workers=4)
        jobs = [
            _job(
                "u1",
                title="one",
                batchable=False,
                key="update:target",
                singles=singles,
                order=order,
            ),
            _job(
                "u2",
                title="two",
                batchable=False,
                key="update:target",
                singles=singles,
                order=order,
            ),
        ]
        result = run_summary_jobs_with_create_batching(executor, _BatchBackend(), jobs)
        self.assertEqual(order, ["u1", "u2"])
        self.assertEqual([item["summary"]["title"] for item in result], ["one", "two"])
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
