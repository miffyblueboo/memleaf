from __future__ import annotations
import json
import unittest
from memleaf import Memory
from memleaf.incremental_dates import selected_calendar
from memleaf.incremental_protocol import PlanningSnapshot, compile_incremental


class IncrementalDateTests(unittest.TestCase):
    def compile(self, text, patch, *, time="2026-09-17T00:30:00+08:00", **row):
        evidence = {"ref": "e1", "use": "new", "role": "user", "text": text,
                    "source": "hermes", "session_id": "s", "event_key": "event-1", "source_time": time}
        target = Memory.new(memory_id="mem-1", title="Task", body="Deliver.", type="todo", status="active", due_date="2026-09-25")
        snap = PlanningSnapshot.build(evidence=[evidence], targets={"m1": target})
        return compile_incremental(json.dumps({"items": [{"action": "UPDATE", "target": "m1", "evidence": ["e1"], "patch": patch, **row}]},ensure_ascii=False), snap)

    def test_relative_uses_source_local_day_not_utc(self):
        out = self.compile("明天下班前提交", {"deadline": {"ref": "e1", "text": "明天下班前"}})
        mem = out["operations"][0]["memory"]
        self.assertEqual(mem["due_date"], "2026-09-18")
        self.assertEqual(mem["due_text"], "明天下班前")

    def test_missing_anchor_retains_unresolved_expression(self):
        out = self.compile("明天提交", {"deadline": {"ref": "e1", "text": "明天"}}, time=None)
        mem = out["operations"][0]["memory"]
        self.assertIsNone(mem["due_date"])
        self.assertEqual(mem["due_text"], "明天")
        self.assertEqual(mem["due_status"], "unresolved")
        self.assertFalse(out["issues"])

    def test_explicit_date_needs_no_source_clock(self):
        out = self.compile("2026-09-20之前提交", {"deadline": {"ref": "e1", "text": "2026-09-20之前"}}, time=None)
        self.assertEqual(out["operations"][0]["memory"]["due_date"], "2026-09-20")

    def test_progress_does_not_remove_old_deadline(self):
        out = self.compile("继续推进", {"body": "Continue delivery."})
        self.assertEqual(out["operations"][0]["memory"]["due_date"], "2026-09-25")

    def test_clear_is_explicit(self):
        out = self.compile("取消原期限", {"deadline": {"ref": "e1", "clear": True}})
        self.assertIsNone(out["operations"][0]["memory"]["due_date"])
        self.assertEqual(out["operations"][0]["memory"]["due_status"], "cleared")

    def test_unknown_new_deadline_invalidates_old_calendar_value(self):
        out = self.compile("改为发布后一周内", {"deadline": {"ref": "e1", "text": "发布后一周内"}})
        self.assertIsNone(out["operations"][0]["memory"]["due_date"])
        self.assertEqual(out["operations"][0]["memory"]["due_text"], "发布后一周内")

    def test_formatting_does_not_change_calendar(self):
        out = self.compile("需**09-18（明天）下班前**完成", {"deadline": {"ref": "e1", "text": "09-18（明天）下班前"}})
        self.assertEqual(out["operations"][0]["memory"]["due_date"], "2026-09-18")

    def test_conflicting_date_keeps_text_and_core_fact(self):
        out = self.compile("09-20（明天）之前", {"deadline": {"ref": "e1", "text": "09-20（明天）之前"}})
        self.assertFalse(out["issues"])
        self.assertIsNone(out["operations"][0]["memory"]["due_date"])

    def test_unselected_mail_date_does_not_become_deadline(self):
        out = self.compile("邮件日期2026-09-10，继续处理", {"body": "Continue delivery."})
        self.assertEqual(out["operations"][0]["memory"]["due_date"], "2026-09-25")

    def test_unknown_quote_reference_rejected(self):
        out = self.compile("明天提交", {"deadline": {"ref": "e9", "text": "明天"}})
        self.assertFalse(out["operations"])

    def test_quote_must_exist(self):
        out = self.compile("明天提交", {"deadline": {"ref": "e1", "text": "下个月"}})
        self.assertEqual(out["issues"][0]["code"], "unsupported_time_quote")

    def test_bad_date_preserves_unresolved_text(self):
        out = self.compile("2026-02-30提交", {"deadline": {"ref": "e1", "text": "2026-02-30"}})
        self.assertIsNone(out["operations"][0]["memory"]["due_date"])

    def test_future_effective_cannot_close_task_now(self):
        out = self.compile("2026-09-20才完成", {"status": "completed"}, effective={"ref": "e1", "text": "2026-09-20"})
        self.assertEqual(out["issues"][0]["code"], "future_state_change")

    def test_completion_without_actual_time_does_not_invent_it(self):
        out = self.compile("已经完成", {"status": "completed"})
        self.assertNotIn("completed_at", out["operations"][0]["memory"])

    def test_naive_source_timestamp_is_not_trusted(self):
        with self.assertRaisesRegex(ValueError,"invalid_source_time"):
            selected_calendar("明天", {"text": "明天", "source_time":"2026-09-17T12:00:00"})

    def test_future_plan_fact_can_be_saved_without_applying_future_state(self):
        e = {"ref":"e1","use":"new","role":"user","text":"2026-09-20计划评审",
             "source":"hermes","session_id":"s","event_key":"event-1","source_time":"2026-09-17T10:00:00+08:00"}
        snap=PlanningSnapshot.build(evidence=[e])
        out=compile_incremental(json.dumps({"items":[{"action":"CREATE","evidence":["e1"],
            "memory":{"type":"fact","scope":"global","title":"Review plan","body":"Plan a review on 2026-09-20."},
            "effective":{"ref":"e1","text":"2026-09-20"}}]}),snap)
        self.assertFalse(out["issues"])
