"""External source dates must not inherit the tool retrieval timestamp."""
from __future__ import annotations

import json
import unittest

from memleaf.inbox import InboxEvent, InboxTurn
from memleaf.process_common import (
    _grounded_due_dates,
    _normalize_summary_dates,
    _summary_date_grounding_violations,
)
from memleaf.validation import ModelOutputError, parse_strict_json, parse_summarize_output


class ExternalSourceDateTests(unittest.TestCase):
    USER_KEY = "u" * 64
    ASSISTANT_KEY = "a" * 64
    TIMESTAMP = "2026-09-07T10:00:00Z"

    def turn(self, external_content: str) -> InboxTurn:
        return InboxTurn(
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
                    self.USER_KEY,
                    "请在本周三前完成当前工作。",
                    timestamp=self.TIMESTAMP,
                ),
                InboxEvent(
                    "hermes",
                    "session",
                    "t" * 64,
                    1,
                    "assistant",
                    self.ASSISTANT_KEY,
                    "已读取外部来源。",
                    timestamp=self.TIMESTAMP,
                    tool_evidence=(
                        {
                            "tool_name": "mail.read",
                            "call_id": "mail-call",
                            "kind": "external_observation",
                            "result_status": "success",
                            "completeness": "complete",
                            "content": external_content,
                        },
                    ),
                ),
            ),
        )

    def test_turn_dates_keep_user_anchor_but_external_uses_explicit_iso_only(self) -> None:
        turn = self.turn("原文日期为 2026-08-31，另写有 9月2日，明天跟进。")
        projection = [
            {
                "event_key": self.USER_KEY,
                "unit_id": "user-unit",
                "role": "user",
                "timestamp": self.TIMESTAMP,
                "content": "请在本周三前完成当前工作。",
            },
            {
                "event_key": self.ASSISTANT_KEY,
                "unit_id": "external-unit",
                "role": "tool",
                "timestamp": self.TIMESTAMP,
                "content": "原文日期为 2026-08-31，另写有 9月2日，明天跟进。",
            },
        ]

        dates = _grounded_due_dates(turn, evidence_events=projection)

        self.assertIn("2026-09-09", dates)  # user 本周三, anchored to Sep 7
        self.assertNotIn("2026-08-31", dates)  # tool bodies are never admitted
        self.assertNotIn("2026-09-02", dates)  # external yearless date
        self.assertNotIn("2026-09-08", dates)  # external relative date
        self.assertNotIn("2026-09-07", dates)  # retrieval timestamp
        self.assertNotIn("2026-08-31", _grounded_due_dates(turn))  # no projection, no tool body

    def test_admitted_external_projection_never_uses_capture_timestamp(self) -> None:
        turn = self.turn("原文日期为 2026年9月2日，另写 9月3日，截止明天。")
        projection = [
            {
                "event_key": self.ASSISTANT_KEY,
                "unit_id": "external-unit",
                "role": "tool",
                "timestamp": self.TIMESTAMP,
                "content": "原文日期为 2026年9月2日，另写 9月3日，截止明天。",
            }
        ]

        dates = _grounded_due_dates(turn, evidence_events=projection)

        self.assertEqual(dates, set())

    def test_external_relative_due_date_is_rejected_by_final_summary_validation(self) -> None:
        turn = self.turn("原文日期为 2026-09-02，截止明天。")
        projection = [
            {
                "event_key": self.ASSISTANT_KEY,
                "unit_id": "external-unit",
                "role": "tool",
                "timestamp": self.TIMESTAMP,
                "content": "原文日期为 2026-09-02，截止明天。",
            }
        ]
        summary = {
            "title": "外部事项",
            "body": "外部来源明确记录了待办。",
            "tags": [],
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": self.ASSISTANT_KEY}],
            "due_date": "2026-09-09",
        }

        with self.assertRaisesRegex(ModelOutputError, "not grounded") as raised:
            parse_summarize_output(
                json.dumps(summary, ensure_ascii=False),
                current_event_keys=[self.ASSISTANT_KEY],
                expected_type="todo",
                expected_scopes=["global"],
                expected_scope_source="model",
                allowed_due_dates=_grounded_due_dates(turn, evidence_events=projection),
            )
        self.assertEqual(raised.exception.validation_detail, "due_date_not_grounded")

    def test_final_reply_summary_dates_use_message_timestamp_not_tool_body(self) -> None:
        turn = self.turn("原文日期为 2026-09-02。")
        summary = {
            "title": "外部事项",
            "body": "请在本周三前完成。",
            "tags": [],
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": self.ASSISTANT_KEY}],
            "due_date": "本周三",
        }

        normalized = _normalize_summary_dates(
            json.dumps(summary, ensure_ascii=False),
            turn,
            {"evidence_event_ids": [self.ASSISTANT_KEY]},
        )

        self.assertEqual(parse_strict_json(normalized)["due_date"], "2026-09-09")
        self.assertNotIn("2026-09-02", normalized)

    def test_user_summary_relative_dates_use_user_event_timestamp(self) -> None:
        turn = self.turn("原文日期为 2026-09-02。")
        summary = {
            "title": "用户事项",
            "body": "请在本周三前完成。",
            "tags": [],
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": self.USER_KEY}],
            "due_date": "本周三",
        }

        normalized = _normalize_summary_dates(
            json.dumps(summary, ensure_ascii=False),
            turn,
            {"evidence_event_ids": [self.USER_KEY]},
        )
        parsed = parse_strict_json(normalized)

        self.assertEqual(parsed["body"], "请在2026-09-09前完成。")
        self.assertEqual(parsed["due_date"], "2026-09-09")
        self.assertEqual(
            parse_summarize_output(
                normalized,
                current_event_keys=[self.USER_KEY],
                expected_type="todo",
                expected_scopes=["global"],
                expected_scope_source="model",
                allowed_due_dates={"2026-09-09"},
            )["due_date"],
            "2026-09-09",
        )

    def test_summary_body_date_must_be_grounded_by_current_source_or_target(self) -> None:
        summary = {
            "title": "事项",
            "body": "原文日期 2026-09-02；截止日期 2026-09-09。",
        }

        self.assertEqual(
            _summary_date_grounding_violations(
                summary,
                grounded_dates={"2026-09-02"},
            ),
            ("2026-09-09",),
        )
        self.assertEqual(
            _summary_date_grounding_violations(
                summary,
                grounded_dates=(),
                preserved_texts=("旧目标原文日期 2026-09-02。",),
            ),
            ("2026-09-09",),
        )
        self.assertEqual(
            _summary_date_grounding_violations(
                {"title": "事项", "body": "保留 2026-09-02。"},
                preserved_texts=("旧目标原文日期 2026-09-02。",),
            ),
            (),
        )

    def test_summary_body_user_date_is_allowed_but_external_yearless_is_not(self) -> None:
        self.assertEqual(
            _summary_date_grounding_violations(
                {"title": "事项", "body": "用户确认 2026-09-09。"},
                grounded_dates={"2026-09-09"},
            ),
            (),
        )
        self.assertEqual(
            _summary_date_grounding_violations(
                {"title": "事项", "body": "来源日期 9月2日。"},
                grounded_dates={"2026-09-02"},
            ),
            ("9月2日",),
        )

    def test_source_yearless_date_may_be_preserved_but_not_expanded(self) -> None:
        source_texts = ("外部原文写有 9月3日下班前。",)

        self.assertEqual(
            _summary_date_grounding_violations(
                {"title": "事项", "body": "外部原文写有 9月3日下班前。"},
                source_texts=source_texts,
            ),
            (),
        )
        self.assertEqual(
            _summary_date_grounding_violations(
                {"title": "事项", "body": "外部原文写有 2026-09-03下班前。"},
                source_texts=source_texts,
            ),
            ("2026-09-03",),
        )


if __name__ == "__main__":
    unittest.main()
