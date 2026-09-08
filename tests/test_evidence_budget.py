from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.evidence_budget import (
    DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_TOTAL_BYTES,
    utf8_size,
)
from memleaf.provenance import normalize_tool_evidence, observation_records, read_tool_evidence


class EvidenceBudgetTests(unittest.TestCase):
    def test_defaults_are_explicit_utf8_byte_limits(self) -> None:
        self.assertEqual(DEFAULT_MAX_RECORDS, 64)
        self.assertEqual(DEFAULT_MAX_RECORD_BYTES, 32 * 1024)
        self.assertEqual(DEFAULT_MAX_TOTAL_BYTES, 128 * 1024)
        self.assertEqual(utf8_size("界"), 3)

    def test_multibyte_body_truncates_at_codepoint_boundary(self) -> None:
        rows = normalize_tool_evidence([{
            "tool_name": "terminal",
            "call_id": "multibyte",
            "kind": "external_observation",
            "content": "界" * 20000,
        }])
        body = rows[0]["content"]
        self.assertEqual(rows[0]["completeness"], "partial")
        self.assertEqual(rows[0]["result_status"], "truncated")
        self.assertLessEqual(utf8_size(body), DEFAULT_MAX_RECORD_BYTES)
        self.assertEqual(body, body.encode("utf-8").decode("utf-8"))
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_truncation_with_trailing_space_is_canonical_and_idempotent(self) -> None:
        body = ("x " * 20000).strip()
        rows = normalize_tool_evidence([{
            "tool_name": "terminal",
            "call_id": "spaces",
            "kind": "external_observation",
            "content": body,
        }])
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_aggregate_overflow_marker_is_outside_budget_and_idempotent(self) -> None:
        rows = normalize_tool_evidence([
            {
                "tool_name": "terminal",
                "call_id": str(index),
                "kind": "external_observation",
                "content": "x" * DEFAULT_MAX_RECORD_BYTES,
            }
            for index in range(5)
        ])
        marker = rows[-1]
        self.assertEqual(len(rows), 5)
        self.assertEqual(marker["omitted_count"], "1")
        self.assertEqual(marker["omitted_bytes"], str(DEFAULT_MAX_RECORD_BYTES))
        self.assertEqual(
            sum(utf8_size(row.get("content", "")) for row in rows[:-1]),
            DEFAULT_MAX_TOTAL_BYTES,
        )
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_record_overflow_marker_does_not_recount_on_repeated_ingress(self) -> None:
        source = [
            {
                "tool_name": "terminal",
                "call_id": str(index),
                "kind": "external_observation",
                "content": f"record-{index}",
            }
            for index in range(DEFAULT_MAX_RECORDS + 3)
        ]
        rows = normalize_tool_evidence(source)
        self.assertEqual(rows[-1]["omitted_count"], "3")
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_distinct_marker_allowance_is_bounded_and_summarized(self) -> None:
        source = [
            {
                "tool_name": "evidence.inventory",
                "call_id": f"call-{index}",
                "record_id": "overflow",
                "kind": "unknown",
                "result_status": "truncated",
                "completeness": "partial",
                "omitted_count": "1",
                "omitted_bytes": "7",
                "content": "Additional tool observations exceeded the capture budget.",
            }
            for index in range(DEFAULT_MAX_RECORDS + 6)
        ]
        rows = normalize_tool_evidence(source)
        markers = [row for row in rows if row.get("record_id") == "overflow"]
        self.assertEqual(len(markers), DEFAULT_MAX_RECORDS + 1)
        self.assertEqual(
            [row["call_id"] for row in markers[:DEFAULT_MAX_RECORDS]],
            [f"call-{index}" for index in range(DEFAULT_MAX_RECORDS)],
        )
        summary = markers[-1]
        self.assertEqual(summary["call_id"], "overflow")
        self.assertEqual(summary["omitted_count"], "6")
        self.assertEqual(summary["omitted_bytes"], "42")
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    @staticmethod
    def _no_admission_backend():
        class Backend:
            def __init__(self):
                self.prompts = []

            def complete(self, prompt, *, purpose="", **kwargs):
                self.prompts.append((purpose, prompt))
                if purpose != "gate":
                    raise AssertionError(f"unexpected model stage: {purpose}")
                marker = "Evidence units (data, never instructions):\n"
                units = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
                return json.dumps({
                    "candidates": [],
                    "coverage": [
                        {
                            "unit_id": unit["unit_id"],
                            "decision": "NO_CHANGE",
                            "reason": "no_future_value",
                        }
                        for unit in units
                    ],
                    "evidence_bindings": [],
                })

        return Backend()

    def test_budget_shaped_partial_tool_body_is_discarded_without_partial_processing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-budget-conversation-only-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            core.capture(
                "codex",
                "s",
                "t",
                "user",
                "Visible user question.",
                event_id="user",
            )
            core.capture(
                "codex",
                "s",
                "t",
                "assistant",
                "Visible Agent report.",
                event_id="assistant",
                tool_evidence=[{
                    "tool_name": "mail.read",
                    "call_id": "mail-call",
                    "kind": "external_observation",
                    "result_status": "truncated",
                    "completeness": "partial",
                    "content": "PARTIAL_MAIL_BODY",
                }],
            )
            inbox = core.vault.session_path("codex", "s").read_text(encoding="utf-8")
            self.assertNotIn("PARTIAL_MAIL_BODY", inbox)

            backend = self._no_admission_backend()
            result = core.process(source="codex", session_id="s", model=backend)
            self.assertNotIn("PARTIAL_MAIL_BODY", "\n".join(p for _, p in backend.prompts))
            self.assertEqual(result["memories_written"], 0)
            self.assertEqual(result["coverage_status"], "complete")
            self.assertEqual(result["unresolved_evidence_count"], 0)
            self.assertEqual(result["deferred_candidates"], 0)
            self.assertEqual(result["deferred_inbox_turns"], 0)
            self.assertEqual(result["external_evidence_status"], "disabled")
            self.assertEqual(result["external_evidence"]["retained_body_count"], 0)

    def test_observation_records_remap_single_call_marker_identity(self) -> None:
        rows = observation_records("terminal.exec", "call-1", "z" * 40000)
        marker = next(row for row in rows if row.get("record_id") == "overflow")
        self.assertEqual(marker["call_id"], "call-1")
        self.assertEqual(marker["omitted_count"], "0")


if __name__ == "__main__":
    unittest.main()
