"""Deterministic regressions for automatic extraction quality boundaries.

These tests deliberately use a queued local backend.  They exercise the
processing contract without an external model or network and describe the
safe outcomes for ambiguous/missing extraction decisions.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.index import event_key
from memleaf.validation import parse_gate_output


class QueueBackend:
    """Small deterministic backend used by all tests in this module."""

    provider = "fake"
    model = "extraction-quality-regression"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del temperature
        self.calls.append({"prompt": prompt, "system": system, "purpose": purpose})
        if not self.responses:
            raise AssertionError("deterministic test backend response queue exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def gate(candidates):
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def candidate(
    candidate_id,
    evidence,
    *,
    memory,
    type="fact",
    scopes=None,
    update_memory_id=None,
):
    value = {
        "candidate_id": candidate_id,
        "memory": memory,
        "evidence_event_ids": list(evidence),
        "duplicate": False,
        "worth": True,
        "type": type,
        "scopes": list(scopes or ["global"]),
        "scope_source": "model",
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    return value


def summary(
    event,
    *,
    title,
    body,
    type,
    scopes=None,
    update_memory_id=None,
    status=None,
):
    value = {
        "title": title,
        "body": body,
        "tags": ["quality-regression"],
        "type": type,
        "scopes": list(scopes or ["global"]),
        "scope_source": "model",
        "sources": [{"event_key": event}],
    }
    if update_memory_id is not None:
        value["update_memory_id"] = update_memory_id
    if status is not None:
        value["status"] = status
    return json.dumps(value, ensure_ascii=False)


class ExtractionQualityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-quality-")
        self.service = Memleaf(Path(self.tempdir.name) / "vault")

    def tearDown(self):
        self.tempdir.cleanup()

    def capture_turn(self, session, *, user, assistant):
        user_id = f"{session}-user"
        assistant_id = f"{session}-assistant"
        self.service.capture("hermes", session, "turn-1", "user", user, event_id=user_id)
        self.service.capture(
            "hermes",
            session,
            "turn-1",
            "assistant",
            assistant,
            event_id=assistant_id,
        )
        return event_key(user_id), event_key(assistant_id)

    def active_memories(self):
        return [record.memory for record in self.service._read_memories_unlocked("knowledge")]

    def test_one_candidate_cannot_merge_project_constraint_with_deadline_todo(self):
        """A long-lived plan and a dated action must not persist as one memory."""

        user_key, assistant_key = self.capture_turn(
            "mixed-future-use",
            user=(
                "金元顺安实施计划采用达梦和东方通；部署测试环境的任务必须在"
                "2026-09-10前完成。"
            ),
            assistant="已确认实施计划约束和截止日期均来自本轮客户要求。",
        )
        project_scope = ["project:金元顺安"]
        mixed = candidate(
            "mixed-plan-and-todo",
            [user_key, assistant_key],
            memory=(
                "金元顺安实施计划采用达梦和东方通，并要求部署测试环境在"
                "2026-09-10前完成。"
            ),
            type="project",
            scopes=project_scope,
        )
        split_project = candidate(
            "plan-constraint",
            [user_key, assistant_key],
            memory="金元顺安实施计划采用达梦和东方通。",
            type="project",
            scopes=project_scope,
        )
        split_todo = candidate(
            "deployment-deadline",
            [user_key, assistant_key],
            memory="金元顺安部署测试环境必须在2026-09-10前完成。",
            type="todo",
            scopes=project_scope,
        )
        backend = QueueBackend(
            [
                gate([mixed]),
                gate([split_project, split_todo]),
                summary(
                    user_key,
                    title="金元顺安实施计划约束",
                    body="金元顺安实施计划采用达梦和东方通。",
                    type="project",
                    scopes=project_scope,
                ),
                summary(
                    user_key,
                    title="金元顺安测试环境部署截止日期",
                    body="金元顺安部署测试环境必须在2026-09-10前完成。",
                    type="todo",
                    scopes=project_scope,
                    status="active",
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="mixed-future-use",
            model=backend,
        )

        memories = self.active_memories()
        self.assertEqual(result["processed_turns"], 1)
        # The safe implementation may split after a bounded gate retry.  A
        # one-memory successful write would be the historical defect.
        self.assertGreaterEqual(len(memories), 2)
        self.assertEqual({memory.type for memory in memories}, {"project", "todo"})
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate", "summarize", "summarize"],
        )
        self.assertFalse(
            any(
                "达梦和东方通" in memory.body and "2026-09-10" in memory.body
                for memory in memories
            ),
            "project constraint and deadline todo were merged into one memory",
        )

    def test_fact_label_for_existing_project_plan_is_corrected_before_update(self):
        """A plan update keeps the active project type and exact target ID."""

        existing = self.service.create_memory(
            memory_id="mem-zhongyin-plan",
            title="中银国际信创实施计划",
            body="中银国际实施计划当前按原始安排执行。",
            tags=["中银国际", "实施计划"],
            type="project",
            scopes=["project:中银国际"],
        )
        user_key, assistant_key = self.capture_turn(
            "plan-fact-label",
            user="中银国际客户提出实施计划调整建议。",
            assistant="新增提前部署测试环境并重新压实实施计划。",
        )
        # This is the model mistake observed in production: it points at the
        # existing plan but labels the candidate as a fact.  The core should
        # canonicalize it to the immutable target type before validation.
        mislabeled = candidate(
            "zhongyin-plan-adjustment",
            [user_key, assistant_key],
            memory="中银国际实施计划调整建议：提前部署测试环境并重新压实计划。",
            type="fact",
            scopes=["project:中银国际"],
            update_memory_id=existing.memory_id,
        )
        backend = QueueBackend(
            [
                gate([mislabeled]),
                summary(
                    user_key,
                    title="中银国际信创实施计划",
                    body=(
                        "中银国际实施计划当前按原始安排执行；客户建议提前部署测试环境，"
                        "并重新压实实施计划。"
                    ),
                    type="project",
                    scopes=["project:中银国际"],
                    update_memory_id=existing.memory_id,
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="plan-fact-label",
            model=backend,
        )

        current = self.service.read(existing.memory_id)
        self.assertEqual(result["memory_ids"], [existing.memory_id])
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        self.assertEqual(current.type, "project")
        self.assertIn("原始安排", current.body)
        self.assertIn("提前部署测试环境", current.body)
        self.assertEqual(len(self.service.vault.list_markdown("history")), 1)

    def test_two_legal_same_project_updates_are_both_committed(self):
        """Two legal update candidates in one gate response are both applied."""

        plan = self.service.create_memory(
            memory_id="mem-zhongyin-plan",
            title="中银国际实施计划",
            body="中银国际实施计划当前按原始安排执行。",
            tags=["plan"],
            type="project",
            scopes=["project:中银国际"],
        )
        risk = self.service.create_memory(
            memory_id="mem-zhongyin-interface-risk",
            title="中银国际接口性能风险",
            body="中银国际接口性能风险待整改。",
            tags=["risk"],
            type="project",
            scopes=["project:中银国际"],
        )
        user_key, assistant_key = self.capture_turn(
            "partial-same-project-update",
            user=(
                "中银国际实施计划新增提前部署测试环境要求；中银国际接口性能风险"
                "确认需在2026-09-12前整改。"
            ),
            assistant=(
                "实施计划和接口性能风险均已发生变化，不能只更新其中一项。"
            ),
        )
        plan_update = candidate(
            "only-plan-update",
            [user_key, assistant_key],
            memory="中银国际实施计划新增提前部署测试环境要求。",
            type="project",
            scopes=["project:中银国际"],
            update_memory_id=plan.memory_id,
        )
        both_updates = [
            plan_update,
            candidate(
                "interface-risk-update",
                [user_key, assistant_key],
                memory="中银国际接口性能风险需在2026-09-12前整改。",
                type="project",
                scopes=["project:中银国际"],
                update_memory_id=risk.memory_id,
            ),
        ]
        backend = QueueBackend(
            [
                gate(both_updates),
                summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划当前按原始安排执行；新增提前部署测试环境要求。",
                    type="project",
                    scopes=["project:中银国际"],
                    update_memory_id=plan.memory_id,
                ),
                summary(
                    user_key,
                    title="中银国际接口性能风险",
                    body="中银国际接口性能风险需在2026-09-12前整改。",
                    type="project",
                    scopes=["project:中银国际"],
                    update_memory_id=risk.memory_id,
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="partial-same-project-update",
            model=backend,
            # The model may select an update target only after the active
            # memories for that project have been exposed by the scope
            # directory.  Keep this test focused on coverage, not on an
            # unrelated invalid_update_target failure.
            scope="project:中银国际",
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(set(result["memory_ids"]), {plan.memory_id, risk.memory_id})
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize"],
        )
        self.assertIn("提前部署测试环境", self.service.read(plan.memory_id).body)
        self.assertIn("2026-09-12", self.service.read(risk.memory_id).body)
        self.assertEqual(
            {
                memory.extra["active_memory_id"]
                for memory in (
                    record.memory for record in self.service._read_memories_unlocked("history")
                )
            },
            {plan.memory_id, risk.memory_id},
        )

    def test_explicit_target_is_not_deferred_when_scope_directory_has_more_than_eight_memories(self):
        """A complete target remains actionable even when directory preview is incomplete."""

        target = self.service.create_memory(
            memory_id="mem-target-zz",
            title="中银国际实施计划",
            body="中银国际实施计划当前按原始安排执行。",
            tags=["target"],
            type="project",
            scopes=["project:中银国际"],
        )
        for index in range(8):
            self.service.create_memory(
                memory_id=f"mem-extra-{index:02d}",
                title=f"中银国际历史记录{index:02d}",
                body=f"中银国际历史记录{index:02d}。",
                tags=["unrelated"],
                type="fact",
                scopes=["project:中银国际"],
            )

        user_key, assistant_key = self.capture_turn(
            "incomplete-scope-directory",
            user="中银国际实施计划新增提前部署测试环境要求。",
            assistant="已确认该实施计划目标需要更新。",
        )
        update = candidate(
            "plan-update-with-incomplete-directory",
            [user_key, assistant_key],
            memory="中银国际实施计划新增提前部署测试环境要求。",
            type="project",
            scopes=["project:中银国际"],
            update_memory_id=target.memory_id,
        )
        backend = QueueBackend(
            [
                gate([update]),
                summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划当前按原始安排执行；新增提前部署测试环境要求。",
                    type="project",
                    scopes=["project:中银国际"],
                    update_memory_id=target.memory_id,
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="incomplete-scope-directory",
            model=backend,
            scope="project:中银国际",
        )

        self.assertEqual(result["memory_ids"], [target.memory_id])
        self.assertEqual(result["deferred_candidates"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        self.assertIn("提前部署测试环境", self.service.read(target.memory_id).body)

    def test_sent_project_plan_email_remains_an_event_not_a_project_plan(self):
        """A sent-mail record mentioning a plan is not itself the project plan."""

        user_key, assistant_key = self.capture_turn(
            "sent-plan-email",
            user="发送中银国际实施计划邮件给陈国金。",
            assistant="中银国际实施计划邮件已发送给陈国金，附件已归档。",
        )
        sent_mail = candidate(
            "sent-plan-email",
            [user_key, assistant_key],
            memory="已发送中银国际实施计划邮件给陈国金，附件已归档。",
            type="event",
            scopes=["project:中银国际"],
        )
        backend = QueueBackend(
            [
                gate([sent_mail]),
                summary(
                    user_key,
                    title="已发送中银国际实施计划邮件",
                    body="已向陈国金发送中银国际实施计划邮件，附件已归档。",
                    type="event",
                    scopes=["project:中银国际"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="sent-plan-email",
            model=backend,
        )

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        memories = self.active_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].type, "event")
        self.assertNotEqual(memories[0].type, "project")

    def test_two_dates_in_one_plan_milestone_do_not_trigger_mixed_future_use(self):
        """Two dated milestones belonging to one plan remain one project candidate."""

        item = candidate(
            "plan-milestones",
            ["plan-milestones-event"],
            memory=(
                "中银国际实施计划里程碑：2026-10-27完成上线；"
                "2026-11-03完成验收。"
            ),
            type="project",
            scopes=["project:中银国际"],
        )

        parsed = parse_gate_output(
            gate([item]),
            event_keys=["plan-milestones-event"],
        )

        self.assertEqual(len(parsed["candidates"]), 1)
        self.assertEqual(parsed["candidates"][0]["type"], "project")

    def test_two_stage_dates_without_milestone_word_remain_one_project(self):
        """第一阶段/第二阶段的日期仍属于同一计划，即使没有“里程碑”字样。"""

        item = candidate(
            "plan-two-stages",
            ["plan-two-stages-event"],
            memory=(
                "中银国际实施计划：第一阶段于2026-10-27完成部署；"
                "第二阶段于2026-11-03完成验收。"
            ),
            type="project",
            scopes=["project:中银国际"],
        )

        parsed = parse_gate_output(
            gate([item]),
            event_keys=["plan-two-stages-event"],
        )

        self.assertEqual(len(parsed["candidates"]), 1)
        self.assertEqual(parsed["candidates"][0]["type"], "project")

    def test_plan_body_contexts_are_normalized_from_fact_to_project(self):
        """计划正文中的附件、发送和会议语境不应阻止 project 纠正。"""

        bodies = (
            "中银国际实施计划：根据附件V2调整部署。",
            "中银国际实施计划：发送给客户后执行。",
            "中银国际实施计划：会议后调整并新增迁移。",
        )
        for index, body in enumerate(bodies):
            event_id = f"plan-body-context-{index}"
            item = candidate(
                f"plan-body-context-{index}",
                [event_id],
                memory=body,
                type="fact",
                scopes=["project:中银国际"],
            )
            with self.subTest(body=body):
                parsed = parse_gate_output(
                    gate([item]),
                    event_keys=[event_id],
                )
                self.assertEqual(parsed["candidates"][0]["type"], "project")

    def test_adjacent_plan_records_are_not_promoted_to_project(self):
        """邮件、附件清单、会议纪要和存档记录不应冒充计划正文。"""

        records = (
            ("已发送中银国际实施计划邮件给客户。", "event"),
            ("中银国际实施计划附件清单。", "fact"),
            ("中银国际实施计划会议纪要。", "event"),
            ("中银国际实施计划存档。", "fact"),
        )
        for index, (body, expected_type) in enumerate(records):
            event_id = f"adjacent-plan-record-{index}"
            item = candidate(
                f"adjacent-plan-record-{index}",
                [event_id],
                memory=body,
                type=expected_type,
                scopes=["project:中银国际"],
            )
            with self.subTest(body=body):
                parsed = parse_gate_output(
                    gate([item]),
                    event_keys=[event_id],
                )
                self.assertEqual(parsed["candidates"][0]["type"], expected_type)

    def test_plan_markers_are_project_and_unique_same_name_targets_stay_updates(self):
        """部署/上线计划及计划调整均识别为 project，并复用唯一目标。"""

        plan_cases = (
            ("deploy", "中银国际部署计划根据附件V2调整部署。", "中银国际部署计划"),
            ("online", "中银国际上线计划发送给客户后执行。", "中银国际上线计划"),
            ("adjustment", "中银国际计划调整会议后调整并新增迁移。", "中银国际计划调整"),
        )
        for suffix, body, title in plan_cases:
            with self.subTest(title=title):
                service = Memleaf(Path(self.tempdir.name) / f"plan-target-{suffix}")
                target = service.create_memory(
                    memory_id=f"mem-{suffix}-plan",
                    title=title,
                    body=f"{title}当前按原始安排执行。",
                    tags=["plan"],
                    type="project",
                    scopes=["project:中银国际"],
                )
                user_id = f"plan-target-{suffix}-user"
                assistant_id = f"plan-target-{suffix}-assistant"
                service.capture(
                    "hermes",
                    f"plan-target-{suffix}",
                    "turn-1",
                    "user",
                    body,
                    event_id=user_id,
                )
                service.capture(
                    "hermes",
                    f"plan-target-{suffix}",
                    "turn-1",
                    "assistant",
                    "已确认更新该计划。",
                    event_id=assistant_id,
                )
                user_key = event_key(user_id)
                assistant_key = event_key(assistant_id)
                gate_candidate = candidate(
                    f"candidate-{suffix}",
                    [user_key, assistant_key],
                    memory=body,
                    type="fact",
                    scopes=["project:中银国际"],
                )
                backend = QueueBackend(
                    [
                        gate([gate_candidate]),
                        summary(
                            user_key,
                            title=title,
                            body=f"{title}{body[len('中银国际'):]}",
                            type="project",
                            scopes=["project:中银国际"],
                            update_memory_id=target.memory_id,
                        ),
                    ]
                )

                result = service.process(
                    source="hermes",
                    session_id=f"plan-target-{suffix}",
                    model=backend,
                    scope="project:中银国际",
                )

                self.assertEqual(result["memory_ids"], [target.memory_id])
                self.assertEqual(
                    [call["purpose"] for call in backend.calls],
                    ["gate", "summarize"],
                )
                self.assertEqual(service.read(target.memory_id).type, "project")
                self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_create_project_candidate_retries_fact_summary_and_finishes_as_project(self):
        """A CREATE candidate must keep its gate type even without an update target."""

        user_key, assistant_key = self.capture_turn(
            "create-project-type",
            user="记录中银国际实施计划新增提前部署测试环境要求。",
            assistant="该项目要求已确认。",
        )
        project_candidate = candidate(
            "create-project",
            [user_key, assistant_key],
            memory="中银国际实施计划新增提前部署测试环境要求。",
            type="project",
            scopes=["global"],
        )
        backend = QueueBackend(
            [
                gate([project_candidate]),
                # This summary deliberately avoids the plan marker so the
                # mismatch cannot be hidden by a classifier side effect.
                summary(
                    user_key,
                    title="中银国际交付要求",
                    body="客户要求提前部署测试环境。",
                    type="fact",
                    scopes=["global"],
                ),
                summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划新增提前部署测试环境要求。",
                    type="project",
                    scopes=["global"],
                ),
            ]
        )

        result = self.service.process(
            source="hermes",
            session_id="create-project-type",
            model=backend,
        )

        memories = self.active_memories()
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize"])
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].type, "project")
        self.assertIn("实施计划", memories[0].body)


if __name__ == "__main__":
    unittest.main()
