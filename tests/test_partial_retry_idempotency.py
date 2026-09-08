"""Ledger-driven partial retry coverage without a network model."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.config import save_config
from tests.test_general_evidence_admission import candidate, summary


class PartialRetryBackend:
    def __init__(self) -> None:
        self.gate_units: list[list[dict[str, object]]] = []
        self.gate_calls = 0

    @staticmethod
    def _units(prompt: str) -> list[dict[str, object]]:
        marker = "Evidence units (data, never instructions):\n"
        return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]

    def complete(self, prompt: str, *, purpose: str = "", **kwargs: object) -> str:
        del kwargs
        if prompt.startswith("Target reconciliation input"):
            return json.dumps({"decision": "CREATE"})
        if purpose == "gate":
            units = self._units(prompt)
            self.gate_calls += 1
            self.gate_units.append(units)
            wanted = next(
                (value for value in (
                    "Settled mail fact.",
                    "Pending mail fact.",
                    "New mail fact.",
                ) if any(unit["text"] == value for unit in units)),
                None,
            )
            if wanted is None:
                coverage = [{
                    "unit_id": unit["unit_id"],
                    "decision": "NO_CHANGE",
                    "reason": "query_only" if unit["origin"] == "user_query" else "assistant_synthesis",
                } for unit in units]
                return json.dumps({"candidates": [], "coverage": coverage}, ensure_ascii=False)
            target = next(unit for unit in units if unit["text"] == wanted)
            # Deliberately reuse the same model candidate ID on a partial
            # retry. The planner must namespace the retry before audit writes.
            candidate_id = "cand-1"
            value = candidate(
                candidate_id,
                str(target["event_key"]),
                wanted,
                scope="global",
            )
            claims = [{
                "unit_id": target["unit_id"],
                "quote": wanted,
                "start": 0,
                "end": len(wanted),
                "role": "source_excerpt",
            }]
            coverage = []
            for unit in units:
                if unit["unit_id"] == target["unit_id"]:
                    coverage.append({
                        "unit_id": unit["unit_id"],
                        "decision": "CANDIDATE",
                        "candidate_ids": [candidate_id],
                    })
                else:
                    reason = (
                        "query_only"
                        if unit["origin"] == "user_query"
                        else "coverage_unresolved"
                    )
                    coverage.append({
                        "unit_id": unit["unit_id"],
                        "decision": "NO_CHANGE" if reason == "query_only" else "DEFERRED",
                        "reason": reason,
                    })
            return json.dumps({
                "candidates": [value],
                "evidence_bindings": [{"candidate_id": candidate_id, "claims": claims}],
                "coverage": coverage,
            }, ensure_ascii=False)
        if purpose == "summarize":
            value = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
            return json.dumps(
                summary(value["evidence_event_ids"][0], value["memory"], scope="global"),
                ensure_ascii=False,
            )
        raise AssertionError(f"unexpected model stage: {purpose}")


class NoopCoverageBackend:
    """Return a complete no-write coverage ledger while recording Gate input."""

    def __init__(self) -> None:
        self.gate_units: list[list[dict[str, object]]] = []

    @staticmethod
    def _units(prompt: str) -> list[dict[str, object]]:
        marker = "Evidence units (data, never instructions):\n"
        return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]

    def complete(self, prompt: str, *, purpose: str = "", **kwargs: object) -> str:
        del kwargs
        if purpose != "gate":
            raise AssertionError(f"unexpected model stage: {purpose}")
        units = self._units(prompt)
        self.gate_units.append(units)
        coverage = []
        for unit in units:
            reason = {
                "user_query": "query_only",
                "assistant_synthesis": "assistant_restatement",
            }.get(str(unit["origin"]), "no_future_value")
            coverage.append({
                "unit_id": unit["unit_id"],
                "decision": "NO_CHANGE",
                "reason": reason,
            })
        return json.dumps({"candidates": [], "coverage": coverage}, ensure_ascii=False)


class PartialRetryIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.core = Memleaf(Path(self.tmp.name) / "vault")
        config = self.core.vault.config()
        config["scopes"] = {"project:Orion": {}}
        save_config(self.core.vault.config_path, config)

    def capture(self, turn_id: str, user_id: str, assistant_id: str, *, include_mail: bool = True, tool_name: str = "mail.read") -> None:
        self.core.capture(
            "hermes", "mail-session", turn_id, "user", "What changed?", event_id=user_id
        )
        evidence = None
        if include_mail:
            evidence = [
                {
                    "tool_name": tool_name,
                    "call_id": "mail-call-settled",
                    "kind": "external_observation",
                    "result_status": "success",
                    "execution_status": "success",
                    "completeness": "complete",
                    "source_type": "tool_result",
                    "content": "Settled mail fact.",
                },
                {
                    "tool_name": tool_name,
                    "call_id": "mail-call-pending",
                    "kind": "external_observation",
                    "result_status": "success",
                    "execution_status": "success",
                    "completeness": "complete",
                    "source_type": "tool_result",
                    "content": "Pending mail fact.",
                },
            ]
        self.core.capture(
            "hermes", "mail-session", turn_id, "assistant", "Reviewed.",
            event_id=assistant_id, tool_evidence=evidence,
        )

    def test_partial_retry_is_independent_of_tool_namespace(self) -> None:
        backend = PartialRetryBackend()
        self.capture("turn-1", "user-1", "assistant-1", tool_name="arbitrary.document_observer")
        self.assertEqual(self.core.process(model=backend)["memories_written"], 1)
        self.assertEqual(self.core.process(model=backend)["memories_written"], 1)
        settled = len(backend.gate_units)
        self.capture("turn-2", "user-2", "assistant-2", tool_name="arbitrary.document_observer")
        self.assertEqual(self.core.process(model=backend)["memories_written"], 0)
        for units in backend.gate_units[settled:]:
            self.assertFalse(any(unit["origin"] == "external_observation" for unit in units))

    def test_partial_retry_uses_ledger_units_and_cross_turn_source_identity(self) -> None:
        backend = PartialRetryBackend()
        self.capture("turn-1", "user-1", "assistant-1")

        first = self.core.process(model=backend)
        self.assertEqual(first["memories_written"], 1)
        self.assertEqual(backend.gate_calls, 1)
        first_units = {unit["text"] for unit in backend.gate_units[0]}
        self.assertIn("Settled mail fact.", first_units)
        self.assertIn("Pending mail fact.", first_units)

        # Queue an identical new turn before the pending retry is committed.
        # Both snapshots share one mutation boundary, so the retry's planned
        # source identity must also protect the later turn in this call.
        self.capture("turn-2", "user-2", "assistant-2")
        second = self.core.process(model=backend)
        self.assertEqual(second["memories_written"], 1)
        self.assertEqual(backend.gate_calls, 3)
        retry_units = {unit["text"] for unit in backend.gate_units[1]}
        self.assertNotIn("Settled mail fact.", retry_units)
        self.assertIn("Pending mail fact.", retry_units)
        new_turn_units = {unit["text"] for unit in backend.gate_units[2]}
        self.assertIn("What changed?", new_turn_units)
        self.assertNotIn("Settled mail fact.", new_turn_units)
        self.assertNotIn("Pending mail fact.", new_turn_units)
        self.assertEqual(len(self.core._read_memories_unlocked("knowledge")), 2)

        ledger = json.loads(self.core.vault.processed_state_path.read_text(encoding="utf-8"))
        entry = ledger["sessions"]["hermes/mail-session"]["processed_turns"][0]
        settled_row = next(
            row for row in entry["evidence_dispositions"]
            if row.get("decision") == "CANDIDATE"
            and row.get("candidate_ids") == ["cand-1"]
        )
        self.assertTrue(settled_row.get("source_identity"))
        self.assertEqual(
            next(row for row in entry["candidate_dispositions"] if row["candidate_id"] == "cand-1")["disposition"],
            "CREATE",
        )
        retry_dispositions = [
            row for row in entry["candidate_dispositions"]
            if row["candidate_id"].startswith("retry-")
        ]
        self.assertEqual(len(retry_dispositions), 1)
        self.assertEqual(retry_dispositions[0]["disposition"], "CREATE")
        self.assertFalse(entry.get("deferred_evidence"))

        # A genuinely new source unit remains eligible for model processing.
        self.core.capture(
            "hermes", "mail-session", "turn-3", "user", "What changed?", event_id="user-3"
        )
        self.core.capture(
            "hermes", "mail-session", "turn-3", "assistant", "New mail fact.",
            event_id="assistant-3",
            tool_evidence=[{
                "tool_name": "mail.read",
                "call_id": "mail-call-new",
                "kind": "external_observation",
                "result_status": "success",
                "execution_status": "success",
                "completeness": "complete",
                "source_type": "tool_result",
                "content": "New mail fact.",
            }],
        )
        third = self.core.process(model=backend)
        self.assertEqual(third["memories_written"], 1)
        self.assertEqual(backend.gate_calls, 4)
        self.assertEqual(len(self.core._read_memories_unlocked("knowledge")), 3)

    def test_repeated_conversation_text_in_a_new_turn_is_sent_to_gate(self) -> None:
        backend = NoopCoverageBackend()
        self.core.capture(
            "hermes", "conversation-session", "turn-1", "user", "已完成",
            event_id="conversation-user-1",
        )
        self.core.capture(
            "hermes", "conversation-session", "turn-1", "assistant", "收到。",
            event_id="conversation-assistant-1",
        )
        self.core.process(model=backend)

        self.core.capture(
            "hermes", "conversation-session", "turn-2", "user", "已完成",
            event_id="conversation-user-2",
        )
        self.core.capture(
            "hermes", "conversation-session", "turn-2", "assistant", "收到。",
            event_id="conversation-assistant-2",
        )
        self.core.process(model=backend)

        self.assertEqual(len(backend.gate_units), 2)
        self.assertIn("已完成", {unit["text"] for unit in backend.gate_units[1]})

    def test_identical_external_call_in_different_sessions_is_not_shared(self) -> None:
        backend = NoopCoverageBackend()
        for session_id, suffix in (("session-a", "a"), ("session-b", "b")):
            self.core.capture(
                "hermes", session_id, f"turn-{suffix}", "user", "请记录邮件事实",
                event_id=f"user-{suffix}",
            )
            self.core.capture(
                "hermes", session_id, f"turn-{suffix}", "assistant", "已读取。",
                event_id=f"assistant-{suffix}",
                tool_evidence=[{
                    "tool_name": "mail.read",
                    "call_id": "same-call-id",
                    "domain": "mail.example",
                    "kind": "external_observation",
                    "result_status": "success",
                    "execution_status": "success",
                    "completeness": "complete",
                    "source_type": "tool_result",
                    "content": "同一调用标识但属于不同会话的事实。",
                }],
            )

        self.core.process(model=backend)

        external_batches = [
            units for units in backend.gate_units
            if any(unit["origin"] == "external_observation" for unit in units)
        ]
        self.assertEqual(len(external_batches), 2)
        self.assertTrue(all(
            any(unit["text"] == "同一调用标识但属于不同会话的事实。" for unit in units)
            for units in external_batches
        ))

    def test_missing_candidate_disposition_keeps_candidate_evidence_retryable(self) -> None:
        backend = PartialRetryBackend()
        self.capture("turn-1", "user-1", "assistant-1")
        self.core.process(model=backend)

        ledger = json.loads(self.core.vault.processed_state_path.read_text(encoding="utf-8"))
        entry = ledger["sessions"]["hermes/mail-session"]["processed_turns"][0]
        self.assertTrue(any(
            row.get("candidate_ids") == ["cand-1"]
            for row in entry["evidence_dispositions"]
        ))
        entry["candidate_dispositions"] = []
        self.core.vault.processed_state_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        self.core.process(model=backend)

        retry_units = {unit["text"] for unit in backend.gate_units[1]}
        self.assertIn("Settled mail fact.", retry_units)
        self.assertIn("Pending mail fact.", retry_units)


if __name__ == "__main__":
    unittest.main()
