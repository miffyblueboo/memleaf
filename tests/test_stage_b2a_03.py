from tests.stage_b2a_support import *


class StageB2ATestPart03(StageB2ATestBase):
    def test_model_selects_immutable_project_type_and_target(self):
        backend = QueueBackend()
        service = self.service(backend, name="inferred-plan-update")
        old = service.create_memory(
            memory_id="mem-zhongyin-plan",
            title="中银国际员工投资行为申报系统信创改造实施计划",
            body="中银国际实施计划当前按原始安排执行。",
            type="project",
            scopes=["project:中银国际"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="inferred-plan-update",
            user_event="inferred-plan-update-user",
            assistant_event="inferred-plan-update-assistant",
            user="中银国际客户要求实施计划提前部署测试环境并重新压实计划。",
            assistant="已记录中银国际实施计划调整建议，待更新计划。",
        )
        candidate = self.candidate(
            "zhongyin-plan-feedback",
            [user_key, assistant_key],
            memory="中银国际客户提出实施计划调整建议：提前部署测试环境并重新压实计划。",
            type="project",
            update_memory_id=old.memory_id,
        )
        candidate["scopes"] = ["project:中银国际"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title=old.title,
                    body="中银国际实施计划当前按原始安排执行；客户提出提前部署测试环境并重新压实计划。",
                    type="project",
                    scopes=["project:中银国际"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memory_ids"], [old.memory_id])
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(service.read(old.memory_id).type, "project")
        self.assertIn("原始安排", service.read(old.memory_id).body)
        self.assertIn("提前部署测试环境", service.read(old.memory_id).body)
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
        self.assertNotIn("update_target_type_mismatch", " ".join(call["prompt"] for call in backend.calls))

    def test_project_plan_target_wins_over_sent_mail_for_zhongyin_and_jinyuan(self):
        cases = (
            (
                "zhongyin",
                "中银国际",
                "中银国际员工申报系统信创改造实施计划",
                "陈国金",
            ),
            (
                "jinyuan",
                "金元顺安",
                "金元顺安员工投资行为申报系统信创改造实施计划",
                "方明",
            ),
        )
        for slug, project, plan_title, recipient in cases:
            with self.subTest(project=project):
                backend = QueueBackend()
                service = self.service(backend, name=f"multi-target-{slug}")
                plan = service.create_memory(
                    memory_id=f"mem-{slug}-plan",
                    title=plan_title,
                    body=f"{project}实施计划当前按原始安排执行。",
                    type="project",
                    scopes=[f"project:{project}"],
                )
                adjacent = []
                for suffix, title, body in (
                    (
                        "sent-mail",
                        f"已发送{plan_title}邮件给{recipient}",
                        f"已向{recipient}发送{project}实施计划邮件。",
                    ),
                    (
                        "attachment",
                        f"{project}实施计划附件清单",
                        f"{project}实施计划附件已归档。",
                    ),
                    (
                        "meeting",
                        f"{project}实施计划会议纪要",
                        f"{project}实施计划会议已完成。",
                    ),
                ):
                    adjacent.append(
                        service.create_memory(
                            memory_id=f"mem-{slug}-{suffix}",
                            title=title,
                            body=body,
                            type="fact",
                            scopes=[f"project:{project}"],
                        )
                    )
                user_key, assistant_key = self.capture_turn(
                    service,
                    turn=f"multi-target-{slug}",
                    user_event=f"multi-target-{slug}-user",
                    assistant_event=f"multi-target-{slug}-assistant",
                    user=f"{project}客户要求实施计划提前部署测试环境并重新压实计划。",
                    assistant=f"已记录{project}实施计划的新约束，待更新计划。",
                )
                candidate = self.candidate(
                    f"{slug}-plan-feedback",
                    [user_key, assistant_key],
                    memory=f"{project}客户提出实施计划调整建议：提前部署测试环境并重新压实计划。",
                    type="project",
                    update_memory_id=plan.memory_id,
                )
                candidate["scopes"] = [f"project:{project}"]
                backend.responses.extend(
                    [
                        self.gate([candidate]),
                        self.summary(
                            user_key,
                            title=plan_title,
                            body=(
                                f"{project}实施计划当前按原始安排执行；"
                                "客户提出提前部署测试环境并重新压实计划。"
                            ),
                            type="project",
                            scopes=[f"project:{project}"],
                        ),
                    ]
                )

                result = service.process(source="codex", session_id="s", model=backend)

                self.assertEqual(result["memory_ids"], [plan.memory_id])
                self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
                self.assertEqual(len(self.knowledge(service)), 4)
                self.assertIn("提前部署测试环境", service.read(plan.memory_id).body)
                for record in adjacent:
                    self.assertEqual(service.read(record.memory_id).body, record.body)
                self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_multiple_same_project_plan_targets_are_deferred_without_sibling(self):
        backend = QueueBackend()
        service = self.service(backend, name="ambiguous-project-plan-target")
        for memory_id, title in (
            ("mem-jinyuan-plan-test", "金元顺安实施计划：测试环境"),
            ("mem-jinyuan-plan-release", "金元顺安实施计划：上线安排"),
        ):
            service.create_memory(
                memory_id=memory_id,
                title=title,
                body=f"{title}按原始安排执行。",
                type="project",
                scopes=["project:金元顺安"],
            )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="ambiguous-project-plan-target",
            user_event="ambiguous-project-plan-target-user",
            assistant_event="ambiguous-project-plan-target-assistant",
            user="金元顺安客户提出实施计划调整建议。",
            assistant="已记录金元顺安实施计划的新约束，待确认对应计划。",
        )
        candidate = self.candidate(
            "ambiguous-jinyuan-plan-feedback",
            [user_key, assistant_key],
            memory="金元顺安实施计划",
            type="fact",
        )
        candidate["scopes"] = ["project:金元顺安"]
        backend.responses.append(deferred_target_response)

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["memories_written"], 0)
        self.assertGreater(result["unresolved_evidence_count"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate"])
        self.assertEqual(
            self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]["deferred_evidence"][0]["reason"],
            "target_ambiguous",
        )
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_model_corrects_wrong_update_to_create_on_final_retry(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-wrong-target-final-retry")
        old = service.create_memory(
            memory_id="mem-beta-owner-final-retry",
            title="beta 项目负责人",
            body="beta 项目负责人是丙。",
            type="identity",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-wrong-target-final-retry",
            user_event="same-project-wrong-target-final-retry-user",
            assistant_event="same-project-wrong-target-final-retry-assistant",
            user="alpha 项目负责人更新为乙；beta 项目负责人仍为丙。",
            assistant="已确认 alpha 负责人为乙，beta 负责人仍为丙。",
        )
        wrong = self.candidate(
            "wrong-target-final-retry",
            [user_key, assistant_key],
            memory="alpha 项目负责人更新为乙。",
            type="identity",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:alpha"]
        invalid_gate = self.gate([wrong])
        backend.responses.extend(
            [
                invalid_gate,
                invalid_gate,
                self.gate([{k: v for k, v in wrong.items() if k != "update_memory_id"}]),
                self.summary(
                    user_key,
                    title="alpha 项目负责人",
                    body="alpha 项目负责人已更新为乙。",
                    type="identity",
                    scopes=["project:alpha"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate", "gate", "summarize"],
        )
        self.assertIn("Previous output violated: target_not_relevant.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertIn(
            "alpha 项目负责人已更新为乙。",
            "\n".join(record.memory.body for record in service._read_memories_unlocked("knowledge")),
        )

    def test_wrong_duplicate_is_retained_as_deferred_on_final_retry(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-wrong-duplicate-final-retry")
        old = service.create_memory(
            memory_id="mem-beta-duplicate-final-retry",
            title="beta 项目状态",
            body="beta 项目状态保持不变。",
            type="fact",
            scopes=["project:beta"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-wrong-duplicate-final-retry",
            user_event="same-project-wrong-duplicate-final-retry-user",
            assistant_event="same-project-wrong-duplicate-final-retry-assistant",
            user="alpha 项目状态保持不变；beta 项目状态也保持不变。",
            assistant="已确认两个项目状态均未变化。",
        )
        wrong = self.candidate(
            "wrong-duplicate-final-retry",
            [user_key, assistant_key],
            memory="alpha 项目状态保持不变。",
            duplicate=True,
            worth=False,
            type="fact",
        )
        wrong["duplicate_memory_id"] = old.memory_id
        wrong["scopes"] = ["project:alpha"]
        invalid_gate = self.gate([wrong])
        backend.responses.extend([invalid_gate, invalid_gate, invalid_gate])

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate"])
        self.assertIn("Previous output violated: target_not_relevant.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(service._read_memories_unlocked("knowledge")), 1)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")

    def test_gate_and_summary_update_target_mismatch_fails_after_bounded_retries(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-mismatch-failure")
        old = service.create_memory(
            memory_id="mem-draft",
            title="Email draft",
            body="A draft email is awaiting confirmation.",
            type="event",
        )
        other = service.create_memory(
            memory_id="mem-other",
            title="Other email",
            body="An unrelated email fact.",
            type="event",
        )
        user_key, _ = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm the email is sent.",
        )
        candidate = self.candidate(
            "email-sent",
            [user_key],
            memory="The email was sent.",
            type="event",
            update_memory_id=old.memory_id,
        )
        bad_summary = self.summary(
            user_key,
            title="Sent",
            body="The email was sent.",
            type="event",
            update_memory_id=other.memory_id,
        )
        backend.responses.extend([self.gate([candidate]), bad_summary, bad_summary, bad_summary])

        with self.assertRaises(ModelOutputError) as raised:
            service.process()

        self.assertEqual(raised.exception.validation_detail, "invalid_update_target")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(service.read(other.memory_id).body, other.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)

    def test_gate_and_summary_update_type_mismatches_are_retried(self):
        backend = QueueBackend()
        service = self.service(backend, name="update-type-mismatch-retry")
        existing = service.create_memory(
            memory_id="mem-existing",
            title="Existing fact",
            body="The existing fact.",
            type="fact",
            scopes=["global"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="update-type-mismatch",
            user_event="update-type-user",
            assistant_event="update-type-assistant",
        )
        wrong_gate = self.candidate(
            "wrong-gate-type",
            [user_key],
            type="project",
            update_memory_id=existing.memory_id,
        )
        corrected_gate = self.candidate(
            "correct-gate-type",
            [user_key],
            type="fact",
            update_memory_id=existing.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([wrong_gate]),
                self.gate([corrected_gate]),
                self.summary(user_key, title="Updated", body="Wrong summary type.", type="project", update_memory_id=existing.memory_id),
                self.summary(user_key, title="Updated", body="The fact is updated.", type="fact", update_memory_id=existing.memory_id),
            ]
        )

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "summarize", "summarize"])
        self.assertIn("Previous output violated: update_target_type_mismatch.", backend.calls[1]["prompt"])
        self.assertIn("existing active update target's type is immutable", backend.calls[1]["prompt"])
        self.assertIn("Previous output violated: invalid_type.", backend.calls[3]["prompt"])
        self.assertEqual(service.read(existing.memory_id).body, "The fact is updated.")
        self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_different_future_use_omits_update_target_and_creates_independent_memory(self):
        backend = QueueBackend()
        service = self.service(backend, name="independent-future-use")
        existing = service.create_memory(
            memory_id="mem-existing-fact",
            title="Existing fact",
            body="The existing fact remains unchanged.",
            type="fact",
            scopes=["global"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="independent-future-use",
            user_event="independent-future-use-user",
            assistant_event="independent-future-use-assistant",
            user="A separate project needs a deployment checklist.",
            assistant="The checklist is a new future action.",
        )
        candidate = self.candidate(
            "independent-project-todo",
            [user_key],
            memory="Prepare a deployment checklist for a separate project.",
            type="todo",
        )
        candidate["scopes"] = ["project:separate"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="Deployment checklist",
                    body="Prepare a deployment checklist for a separate project.",
                    type="todo",
                    scopes=["project:separate"],
                    status="active",
                ),
            ]
        )

        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual(service.read(existing.memory_id).body, existing.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        active = self.knowledge(service)
        self.assertEqual(len(active), 2)
        independent = next(record.memory for record in active if record.memory.memory_id != existing.memory_id)
        self.assertEqual(independent.type, "todo")
        self.assertEqual(independent.scopes, ["project:separate"])

    def test_three_gate_update_target_type_mismatches_keep_queue_and_write_nothing(self):
        backend = QueueBackend()
        service = self.service(backend, name="update-target-type-failure")
        existing = service.create_memory(
            memory_id="mem-fact-target",
            title="Existing fact",
            body="The existing fact.",
            type="fact",
            scopes=["global"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="update-target-type-failure",
            user_event="update-target-type-failure-user",
            assistant_event="update-target-type-failure-assistant",
        )
        wrong_gate = self.candidate(
            "wrong-target-type",
            [user_key],
            memory="The existing fact is updated to a new value.",
            type="project",
            update_memory_id=existing.memory_id,
        )
        backend.responses.extend([self.gate([wrong_gate])] * 3)

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate"] * 3)
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertEqual(len(entry["deferred_candidates"]), 1)
        self.assertEqual(entry["deferred_candidates"][0]["candidate_id"], "wrong-target-type")
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "update_target_type_mismatch")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark"), 1)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())
        self.assertEqual(service.read(existing.memory_id).body, existing.body)
        self.assertEqual(service._read_memories_unlocked("history"), [])

    def test_mixed_project_summary_retries_before_writing(self):
        backend = QueueBackend()
        service = self.service(backend, name="mixed-summary-retry")
        user_key, _ = self.capture_turn(
            service,
            turn="mixed-summary",
            user_event="mixed-summary-user",
            assistant_event="mixed-summary-assistant",
            user="zhongyin project topic",
        )
        candidate = self.candidate(
            "mixed-summary-candidate",
            [user_key],
            memory="zhongyin project topic",
            type="project",
        )
        candidate["scopes"] = ["project:zhongyin"]
        mixed_summary = self.summary(
            user_key,
            title="One topic",
            body="One project topic.",
            type="project",
            scopes=["project:zhongyin", "project:morgan"],
        )
        corrected_summary = self.summary(
            user_key,
            title="One topic",
            body="One project topic.",
            type="project",
            scopes=["project:zhongyin"],
        )
        backend.responses.extend([self.gate([candidate]), mixed_summary, corrected_summary])

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize"])
        self.assertIn("Previous output violated: mixed_project_scopes.", backend.calls[2]["prompt"])
        self.assertEqual(service._read_memories_unlocked("knowledge")[0].memory.scopes, ["project:zhongyin"])

    def test_three_mixed_project_summary_failures_keep_inbox_and_watermark(self):
        backend = QueueBackend()
        service = self.service(backend, name="mixed-summary-failure")
        user_key, _ = self.capture_turn(
            service,
            turn="mixed-summary-failure",
            user_event="mixed-summary-failure-user",
            assistant_event="mixed-summary-failure-assistant",
            user="zhongyin project topic",
        )
        candidate = self.candidate(
            "mixed-summary-failure-candidate",
            [user_key],
            memory="zhongyin project topic",
            type="project",
        )
        candidate["scopes"] = ["project:zhongyin"]
        mixed_summary = self.summary(
            user_key,
            title="One topic",
            body="One project topic.",
            type="project",
            scopes=["project:zhongyin", "project:morgan"],
        )
        backend.responses.extend([self.gate([candidate]), mixed_summary, mixed_summary, mixed_summary])

        with self.assertRaises(ModelOutputError) as raised:
            service.process()

        self.assertEqual(raised.exception.validation_detail, "mixed_project_scopes")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["validation_detail"], "mixed_project_scopes")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())
        self.assertEqual(service._read_memories_unlocked("knowledge"), [])

    def test_chinese_relative_date_alias_retries_to_absolute_date(self):
        anchor = "2026-09-01T02:01:41Z"
        backend = QueueBackend()
        with patch("memleaf.capture._timestamp", return_value=anchor):
            service = self.service(backend, name="chinese-relative-retry-anchored")
            user_key, _ = self.capture_turn(
                service,
                turn="chinese-relative",
                user_event="chinese-relative-user",
                assistant_event="chinese-relative-assistant",
                user="昨日完成。",
            )
            candidate = self.candidate(
                "chinese-relative-candidate",
                [user_key],
                memory="完成截止日期",
                type="todo",
            )
            backend.responses.extend(
                [
                    self.gate([candidate]),
                    self.summary(user_key, title="截止昨日", body="昨日完成。", type="todo", status="active"),
                    self.summary(user_key, title="截止日期", body="截止日期为2026-08-31。", type="todo", status="active"),
                ]
            )
            result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        self.assertEqual(service._read_memories_unlocked("knowledge")[0].memory.body, "2026-08-31完成。")

    def test_ambiguous_relative_date_is_deferred_without_guessing(self):
        anchor = "2026-09-01T02:01:41Z"
        backend = QueueBackend()
        service = self.service(backend, name="chinese-relative-failure")
        with patch("memleaf.capture._timestamp", return_value=anchor):
            user_key, _ = self.capture_turn(
                service,
                turn="chinese-relative-failure",
                user_event="chinese-relative-failure-user",
                assistant_event="chinese-relative-failure-assistant",
            )
        candidate = self.candidate(
            "chinese-relative-failure-candidate",
            [user_key],
            memory="完成截止日期",
            type="todo",
        )
        bad_summary = self.summary(
            user_key,
            title="截止本周末",
            body="本周末完成。",
            type="todo",
            status="active",
        )
        backend.responses.extend([self.gate([candidate]), bad_summary, bad_summary, bad_summary])

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "summarize", "summarize", "summarize"],
        )
        self.assertIn(RELATIVE_TIME_CORRECTION, backend.calls[2]["prompt"])
        self.assertIn("Previous output violated: relative_time.", backend.calls[2]["prompt"])
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["status"], "idle")
        entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        self.assertEqual(entry["deferred_candidates"][0]["reason"], "relative_time")
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 1)
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())
