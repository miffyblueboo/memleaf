"""Automatic duplicate observations must stay out of mutation batches."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from memleaf.planning_context import PlanningContext
from tests.semantic_fixtures import bind_response


def _candidate(candidate_id: str, source_key: str, memory: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "evidence_event_ids": [source_key],
        "memory": memory,
        "worth": True,
        "duplicate": False,
        "type": "fact",
        "scopes": ["project:alpha"],
        "scope_source": "model",
    }


class _Backend:
    def __init__(self, source_key: str):
        self.source_key = source_key
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, *, purpose: str = "", **kwargs: object) -> str:
        # The production UPDATE path now performs an independent semantic
        # review.  This fixture owns the authored summary and must not consume
        # its historical call accounting for that compatibility stage.
        if purpose == "summarize" and prompt.startswith(
            ("UPDATE_SEMANTIC_REVIEW\n", "CREATE_SEMANTIC_REVIEW\n")
        ):
            return '{"decision":"ACCEPT"}'
        self.calls.append((purpose, prompt))
        if prompt.startswith("Target reconciliation input"):
            payload, _ = json.JSONDecoder().raw_decode(prompt.split("\n", 1)[1])
            candidate_id = payload["candidate"]["candidate_id"]
            if candidate_id == "update":
                return json.dumps({
                    "decision": "UPDATE",
                    "target_memory_id": "mem-alpha",
                    "type": "fact",
                })
            return json.dumps({
                "decision": "NO_CHANGE",
                "target_memory_id": "mem-alpha",
            })
        if purpose == "gate":
            raw = json.dumps({
                "candidates": [
                    _candidate("update", self.source_key, "alpha deployment changed state"),
                    _candidate("duplicate", self.source_key, "alpha deployment current state"),
                ],
            })
            return bind_response(raw, prompt, purpose)
        if purpose == "summarize":
            return json.dumps({
                "title": "alpha deployment record",
                "body": "alpha deployment changed state",
                "type": "fact",
                "scopes": ["project:alpha"],
                "scope_source": "model",
                "tags": [],
                "sources": [{"event_key": self.source_key}],
                "update_memory_id": "mem-alpha",
            })
        raise AssertionError(f"unexpected model stage: {purpose}")


class AutomaticDuplicateNoopCollisionTests(unittest.TestCase):
    def test_no_change_duplicate_does_not_collide_with_same_target_update(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-duplicate-noop-") as root:
            core = Memleaf(Path(root) / "vault")
            config = core.vault.config()
            config["scopes"] = {"project:alpha": {}}
            save_config(core.vault.config_path, config)
            core.create_memory(
                memory_id="mem-alpha",
                title="alpha deployment record",
                body="alpha deployment current state",
                type="fact",
                scopes=["project:alpha"],
            )
            core.capture(
                "hermes", "session", "turn", "user",
                "alpha deployment changed state", event_id="user",
            )
            core.capture(
                "hermes", "session", "turn", "assistant",
                "Noted.", event_id="assistant",
            )
            backend = _Backend(event_key("user"))

            # Hide the initial related-memory directory so both targetless
            # candidates exercise the bounded candidate-level reconciliation.
            with patch.object(PlanningContext, "_related", return_value=([], [], [], None)):
                result = core.process(
                    source="hermes", session_id="session", model=backend,
                )

            self.assertEqual(result["memories_written"], 1)
            self.assertEqual(result["memory_ids"], ["mem-alpha"])
            self.assertEqual(result["metadata_merged"], 0)
            self.assertEqual(core.read("mem-alpha").body, "alpha deployment changed state")
            self.assertEqual(len(core.vault.list_markdown("history")), 1)
            # Candidate target reconciliation is deliberately completed before
            # the bounded summary phase so independent summaries can be safely
            # scheduled while same-target work remains ordered.
            self.assertEqual(
                [purpose for purpose, _ in backend.calls],
                ["gate", "gate", "gate", "summarize"],
            )


if __name__ == "__main__":
    unittest.main()
