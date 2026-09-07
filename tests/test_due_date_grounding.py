"""Due-date grounding must follow the admitted evidence projection."""
from __future__ import annotations

import json
import unittest

from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.process_common import _grounded_due_dates
from memleaf.validation import ModelOutputError, parse_summarize_output


class DueDateGroundingTests(unittest.TestCase):
    def setUp(self) -> None:
        timestamp = "2026-09-07T10:00:00Z"
        self.turn = InboxTurn(
            "hermes",
            "session",
            "t" * 64,
            1,
            (
                InboxEvent(
                    "hermes",
                    "session",
                    "t" * 64,
                    1,
                    "user",
                    "u" * 64,
                    "请安排这项工作。",
                    timestamp=timestamp,
                ),
                InboxEvent(
                    "hermes",
                    "session",
                    "t" * 64,
                    1,
                    "assistant",
                    "a" * 64,
                    "工具结果是 2026-09-14，已完成。",
                    timestamp=timestamp,
                    tool_evidence=(
                        {
                            "call_id": "tool-call",
                            "source_type": "tool_result",
                            "retention": "bounded",
                            "content": "deadline is 2026-09-14",
                        },
                    ),
                ),
            ),
        )

    @staticmethod
    def summary(due_date: str) -> str:
        return json.dumps(
            {
                "title": "安排工作",
                "body": f"请在 {due_date} 前完成。",
                "tags": [],
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
                "sources": [{"event_key": "u" * 64}],
                "due_date": due_date,
            },
            ensure_ascii=False,
        )

    def parse_todo(self, due_date: str, allowed_due_dates: set[str]) -> dict[str, object]:
        return parse_summarize_output(
            self.summary(due_date),
            current_event_keys=["u" * 64],
            expected_type="todo",
            expected_scopes=["global"],
            expected_scope_source="model",
            allowed_due_dates=allowed_due_dates,
        )

    def test_bound_tool_absolute_date_is_legal(self) -> None:
        projection = [
            {
                "event_key": "a" * 64,
                "unit_id": "tool-unit",
                "role": "tool",
                "timestamp": "2026-09-07T10:00:00Z",
                "content": "deadline is 2026-09-14",
            }
        ]
        dates = _grounded_due_dates(self.turn, evidence_events=projection)

        self.assertEqual(dates, {"2026-09-14"})
        self.assertEqual(self.parse_todo("2026-09-14", dates)["due_date"], "2026-09-14")

    def test_unbound_tool_date_is_not_grounding(self) -> None:
        dates = _grounded_due_dates(self.turn, evidence_events=[])

        self.assertEqual(dates, set())
        with self.assertRaisesRegex(ModelOutputError, "not grounded"):
            self.parse_todo("2026-09-14", dates)

    def test_metadata_and_off_projections_cannot_restore_tool_date(self) -> None:
        projections = (
            [
                {
                    "event_key": "a" * 64,
                    "unit_id": "metadata-unit",
                    "role": "tool",
                    "timestamp": "2026-09-07T10:00:00Z",
                    "retention": "metadata",
                }
            ],
            [],
        )
        for projection in projections:
            with self.subTest(projection=projection):
                dates = _grounded_due_dates(self.turn, evidence_events=projection)
                self.assertEqual(dates, set())
                with self.assertRaisesRegex(ModelOutputError, "not grounded"):
                    self.parse_todo("2026-09-14", dates)


if __name__ == "__main__":
    unittest.main()
