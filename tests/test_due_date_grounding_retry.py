"""Ungrounded todo deadlines defer one candidate without hiding other work."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.index import event_key
from memleaf.llm import ModelOutputError
from memleaf.prompts import RELATIVE_TIME_CORRECTION
from tests.semantic_fixtures import bind_response, semantic_fixture


def _candidate(candidate_id: str, source_key: str, *, type: str, memory: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "evidence_event_ids": [source_key],
        "memory": memory,
        "worth": True,
        "duplicate": False,
        "type": type,
        "scopes": ["global"],
        "scope_source": "model",
    }


def _summary(source_key: str, *, type: str, title: str, body: str, **extra: object) -> str:
    value = {
        "title": title,
        "body": body,
        "tags": [],
        "type": type,
        "scopes": ["global"],
        "scope_source": "model",
        "sources": [{"event_key": source_key}],
    }
    value.update(extra)
    return json.dumps(value, ensure_ascii=False)


@semantic_fixture
class _Backend:
    def __init__(self, responses: list[str], source_key: str):
        self.responses = list(responses)
        self.source_key = source_key
        self.calls: list[dict[str, str]] = []

    def complete(self, prompt: str, *, purpose: str = "", **kwargs: object) -> str:
        self.calls.append({"purpose": purpose, "prompt": prompt})
        if not self.responses:
            raise AssertionError("test backend response queue exhausted")
        response = self.responses.pop(0)
        if purpose == "gate":
            response = bind_response(response, prompt, purpose)
        return response


class DueDateGroundingRetryTests(unittest.TestCase):
    def _service(self) -> tuple[Memleaf, str]:
        root = tempfile.TemporaryDirectory(prefix="memleaf-due-date-retry-")
        self.addCleanup(root.cleanup)
        core = Memleaf(Path(root.name) / "vault")
        core.capture(
            "hermes", "session", "turn", "user",
            "alpha 项目负责人已确认。", event_id="user",
        )
        core.capture(
            "hermes", "session", "turn", "assistant",
            "已记录。", event_id="assistant",
        )
        return core, event_key("user")

    def test_ungrounded_due_date_defers_candidate_and_commits_valid_sibling(self) -> None:
        core, source_key = self._service()
        bad = _candidate(
            "bad-deadline", source_key, type="todo", memory="alpha 项目截止日期待确认。",
        )
        good = _candidate(
            "good-fact", source_key, type="fact", memory="alpha 项目负责人已确认。",
        )
        gate = json.dumps({"candidates": [bad, good]}, ensure_ascii=False)
        bad_summary = _summary(
            source_key,
            type="todo",
            title="alpha 项目截止日期",
            body="alpha 项目需要在 2026-09-09 前完成。",
            status="active",
            due_date="2026-09-09",
        )
        good_summary = _summary(
            source_key,
            type="fact",
            title="alpha 项目负责人",
            body="alpha 项目负责人已确认。",
        )
        backend = _Backend([gate, bad_summary, bad_summary, bad_summary, good_summary], source_key)

        result = core.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(core._read_memories_unlocked("knowledge")[0].memory.body, "alpha 项目负责人已确认。")
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize", "summarize", "summarize"])
        self.assertIn(RELATIVE_TIME_CORRECTION, backend.calls[2]["prompt"])
        self.assertIn("Previous output violated: relative_time.", backend.calls[2]["prompt"])
        state = json.loads(core.vault.processed_state_path.read_text(encoding="utf-8"))
        entry = state["sessions"]["hermes/session"]["processed_turns"][0]
        self.assertEqual(entry["deferred_candidates"][0]["candidate_id"], "bad-deadline")
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "relative_time")

    def test_other_schema_error_still_fails_the_turn(self) -> None:
        core, source_key = self._service()
        candidate = _candidate(
            "bad-schema", source_key, type="fact", memory="alpha 项目负责人已确认。",
        )
        gate = json.dumps({"candidates": [candidate]}, ensure_ascii=False)
        invalid_summary = _summary(
            source_key,
            type="todo",
            title="alpha 项目负责人",
            body="alpha 项目负责人已确认。",
        )
        backend = _Backend([gate, invalid_summary, invalid_summary, invalid_summary], source_key)

        with self.assertRaises(ModelOutputError) as raised:
            core.process(source="hermes", session_id="session", model=backend)

        self.assertEqual(raised.exception.validation_detail, "invalid_type")
        self.assertEqual(core._read_memories_unlocked("knowledge"), [])


if __name__ == "__main__":
    unittest.main()
