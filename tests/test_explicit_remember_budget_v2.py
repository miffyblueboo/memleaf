from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.index import event_key
from memleaf.llm import ModelError, ModelRouter


class ExplicitRememberBudgetV2Tests(unittest.TestCase):
    @staticmethod
    def summary(key: str) -> str:
        return json.dumps(
            {
                "title": "Explicit fact",
                "body": "Remember this explicit fact.",
                "tags": [],
                "type": "fact",
                "scopes": ["global"],
                "scope_source": "model",
                "sources": [{"event_key": key}],
            }
        )

    def test_auto_router_is_pinned_and_never_exceeds_two_actual_requests(self):
        calls = {"host": 0, "api": 0}

        def host(prompt, **kwargs):
            del prompt, kwargs
            calls["host"] += 1
            return "{}"

        def api(prompt, **kwargs):
            del prompt, kwargs
            calls["api"] += 1
            return self.summary(event_key("remember-budget"))

        router = ModelRouter(mode="auto", host=host, api=api)
        with tempfile.TemporaryDirectory(prefix="memleaf-remember-budget-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            with self.assertRaises(ModelError) as caught:
                service.remember(
                    "Remember this explicit fact.",
                    source="codex",
                    session_id="remember-budget",
                    turn_id="remember-budget-turn",
                    event_id="remember-budget",
                    router=router,
                )

            self.assertEqual(caught.exception.code, "model_timeout")
            self.assertEqual(calls, {"host": 2, "api": 0})
            self.assertEqual(service._read_memories_unlocked("knowledge"), [])

    def test_one_format_repair_stays_on_same_route_and_commits(self):
        key = event_key("remember-repair-budget")
        responses = ["{}", self.summary(key)]
        calls = {"host": 0, "api": 0}

        def host(prompt, **kwargs):
            del prompt, kwargs
            calls["host"] += 1
            return responses.pop(0)

        def api(prompt, **kwargs):
            del prompt, kwargs
            calls["api"] += 1
            raise AssertionError("explicit remember must not fall through to API")

        router = ModelRouter(mode="auto", host=host, api=api)
        with tempfile.TemporaryDirectory(prefix="memleaf-remember-repair-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            result = service.remember(
                "Remember this explicit fact.",
                source="codex",
                session_id="remember-repair-budget",
                turn_id="remember-repair-budget-turn",
                event_id="remember-repair-budget",
                router=router,
            )

            self.assertEqual(result["memories_written"], 1)
            self.assertEqual(calls, {"host": 2, "api": 0})
            self.assertEqual(len(service._read_memories_unlocked("knowledge")), 1)


if __name__ == "__main__":
    unittest.main()
