from tests.stage_b2a_support import *


class StageB2ATestPart01(StageB2ATestBase):
    def test_process_zero_candidates_is_success_and_marks_eligibility(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service)
        backend.responses.append(self.gate([]))

        result = service.process(source="codex", session_id="s")

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(len(self.knowledge(service)), 0)
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(len(state["processed_turns"]), 1)
        entry = state["processed_turns"][0]
        self.assertEqual(set(entry["event_keys"]), {user_key, assistant_key})
        self.assertEqual(
            entry["eligible_cleanup_at"],
            "2026-08-25T00:00:00Z",
        )
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").exists())

    def test_automatic_no_change_skips_writes_and_scope_observation(self):
        backend = QueueBackend()
        service = self.service(backend, name="automatic-no-change")
        user_key, _ = self.capture_turn(
            service,
            turn="automatic-no-change",
            user_event="automatic-no-change-user",
            assistant_event="automatic-no-change-assistant",
            user="浙江东方正文为空，尚未闭环，待问结论。",
            assistant="先暂存为待跟进事项。",
        )
        no_change = self.candidate(
            "zhejiang-pending",
            [user_key],
            memory="浙江东方正文为空，尚未闭环，待问结论。",
            type="event",
        )
        no_change["scopes"] = ["project:浙江东方"]
        temporary = self.candidate(
            "temporary-feedback",
            [user_key],
            memory="金元顺安等待反馈。",
            worth=False,
            type=None,
        )
        temporary["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([no_change, temporary]),
                json.dumps({"decision": "NO_CHANGE"}),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["memory_ids"], [])
        self.assertEqual(self.knowledge(service), [])
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(state["processed_turns"][0]["memory_ids"], [])
        self.assertNotIn("project:浙江东方", state.get("scopes", []))
        self.assertNotIn("project:金元顺安", state.get("scopes", []))
        self.assertNotIn("project:浙江东方", service.vault.config()["scopes"])
        self.assertNotIn("project:金元顺安", service.vault.config()["scopes"])
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_explicit_no_change_is_rejected_and_retried(self):
        backend = QueueBackend([json.dumps({"decision": "NO_CHANGE"})] * 3)
        service = self.service(backend, name="explicit-no-change")

        with self.assertRaises(ModelOutputError) as raised:
            service.remember(
                "A concrete explicitly requested memory.",
                source="codex",
                session_id="explicit-no-change",
                turn_id="remember-turn",
                event_id="explicit-no-change-event",
                model=backend,
            )

        self.assertEqual(raised.exception.validation_detail, "unknown_fields")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        self.assertEqual([call["purpose"] for call in backend.calls], ["summarize"] * 3)
        self.assertEqual(self.knowledge(service), [])
        marker = self.processed(service)["sessions"]["codex/explicit-no-change"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["validation_detail"], "unknown_fields")
        self.assertEqual(marker["attempt_count"], 3)

    def test_gate_scope_misroute_retries_then_automatic_no_change_is_safe(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-misroute-no-change")
        config = service.vault.config()
        config["scopes"] = {"project:zhongyin": {"aliases": ["中银国际"]}}
        save_config(service.vault.config_path, config)
        user_key, _ = self.capture_turn(
            service,
            turn="scope-misroute",
            user_event="scope-misroute-user",
            assistant_event="scope-misroute-assistant",
            user="浙江东方正文为空，尚未闭环，待问结论。",
            assistant="先暂存为待跟进事项。",
        )
        wrong = self.candidate(
            "wrong-zhejiang-scope",
            [user_key],
            memory="浙江东方正文为空，尚未闭环，待问结论。",
            type="event",
        )
        wrong["scopes"] = ["project:zhongyin"]
        corrected = dict(wrong, candidate_id="correct-zhejiang-scope", scopes=["project:浙江东方"])
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([corrected]),
                json.dumps({"decision": "NO_CHANGE"}),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(self.knowledge(service), [])
        state = self.processed(service)["sessions"]["codex/s"]
        self.assertNotIn("project:zhongyin", state.get("scopes", []))
        self.assertNotIn("project:浙江东方", state.get("scopes", []))
        self.assertNotIn("project:浙江东方", service.vault.config()["scopes"])

    def test_gate_scope_not_grounded_model_corrects_on_final_retry(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-correct")
        config = service.vault.config()
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:金元顺安": {"aliases": ["金元顺安"]},
        }
        save_config(service.vault.config_path, config)
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-correct",
            user_event="scope-final-correct-user",
            assistant_event="scope-final-correct-assistant",
            user="中银国际实施计划需要更新。",
            assistant="确认中银国际计划更新。",
        )
        wrong = self.candidate(
            "wrong-zg-scope",
            [user_key],
            memory="中银国际实施计划需要更新。",
            type="project",
        )
        wrong["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([wrong]),
                self.gate([{**wrong, "scopes": ["project:zhongyin"]}]),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划需要更新。",
                    type="project",
                    scopes=["project:zhongyin"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(self.knowledge(service)[0].memory.scopes, ["project:zhongyin"])
        self.assertIn("project:zhongyin", service.vault.config()["scopes"])

    def test_scope_not_grounded_final_retry_keeps_other_valid_candidates(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-keep-valid")
        config = service.vault.config()
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:xinyuan": {"aliases": ["鑫元基金"]},
        }
        save_config(service.vault.config_path, config)
        user_key, assistant_key = self.capture_turn(
            service,
            turn="scope-final-keep-valid",
            user_event="scope-final-keep-valid-user",
            assistant_event="scope-final-keep-valid-assistant",
            user="中银国际计划已更新；鑫元待反馈。",
            assistant="待确认中银国际与鑫元相关事项。",
        )

        valid = self.candidate(
            "zhongyin-plan",
            [user_key],
            memory="中银国际计划已更新。",
            type="project",
        )
        valid["scopes"] = ["project:zhongyin"]
        bad = self.candidate(
            "xinyuan-ambiguous",
            [assistant_key],
            memory="鑫元 待反馈。",
            type="event",
        )
        bad["scopes"] = ["project:xinyuan"]
        backend.responses.extend(
            [
                self.gate([bad, valid]),
                self.gate([bad, valid]),
                self.gate([bad, valid]),
                self.summary(
                    user_key,
                    title="中银国际计划",
                    body="中银国际计划已更新。",
                    type="project",
                    scopes=["project:zhongyin"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(len(self.knowledge(service)), 1)
        self.assertEqual(self.knowledge(service)[0].memory.scopes, ["project:zhongyin"])
        turn_entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        deferred = turn_entry["deferred_candidates"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["candidate_id"], "xinyuan-ambiguous")
        self.assertNotIn("鑫元", "\n".join(record.memory.body for record in self.knowledge(service)))

    def test_scope_not_grounded_final_retry_ambiguous_mention_no_write(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-ambiguous")
        config = service.vault.config()
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:jinyuan": {"aliases": ["金元顺安"]},
        }
        save_config(service.vault.config_path, config)
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-ambiguous",
            user_event="scope-final-ambiguous-user",
            assistant_event="scope-final-ambiguous-assistant",
            user="中银国际和金元顺安均有更新。",
            assistant="已确认两个项目的进展。",
        )
        wrong = self.candidate(
            "both-mentioned",
            [user_key],
            memory="中银国际和金元顺安均有更新。",
            type="fact",
        )
        wrong["scopes"] = ["project:zhongyin"]
        backend.responses.extend([self.gate([wrong]), self.gate([wrong]), self.gate([wrong])])

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 0)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate"])
        self.assertEqual(self.knowledge(service), [])
        turn_entry = self.processed(service)["sessions"]["codex/s"]["processed_turns"][0]
        deferred = turn_entry["deferred_candidates"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["candidate_id"], "both-mentioned")
        self.assertEqual(deferred[0]["reason"], "scope_conflict")
        self.assertEqual(deferred[0]["scopes"], ["project:zhongyin"])
        self.assertEqual(self.processed(service)["sessions"]["codex/s"]["processing"]["status"], "idle")

    def test_scope_not_grounded_final_retry_wrong_scope_and_wrong_update_target_is_safe(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-wrong-scope-target")
        config = service.vault.config()
        config["scopes"] = {
            "project:alpha": {"aliases": ["阿尔法"]},
            "project:beta": {"aliases": ["贝塔"]},
        }
        save_config(service.vault.config_path, config)
        old = service.create_memory(
            memory_id="alpha-existing",
            title="阿尔法 项目负责人",
            body="阿尔法 项目负责人当前为甲。",
            type="identity",
            scopes=["project:alpha"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-wrong-scope-target",
            user_event="scope-final-wrong-scope-target-user",
            assistant_event="scope-final-wrong-scope-target-assistant",
            user="阿尔法 项目负责人当前为甲；贝塔 项目负责人更新为乙。",
            assistant="已确认两个项目的负责人信息。",
        )
        wrong = self.candidate(
            "wrong-scope-target",
            [user_key],
            memory="贝塔 项目负责人更新为乙。",
            type="identity",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:alpha"]
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([wrong]),
                self.gate([{**{k: v for k, v in wrong.items() if k != "update_memory_id"},
                            "scopes": ["project:beta"]}]),
                self.summary(
                    user_key,
                    title="贝塔 项目负责人",
                    body="贝塔 项目负责人更新为乙。",
                    type="identity",
                    scopes=["project:beta"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: scope_not_grounded.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertIn(
            "贝塔 项目负责人更新为乙。",
            "\n".join(record.memory.body for record in self.knowledge(service)),
        )
        self.assertEqual(
            {
                tuple(record.memory.scopes): record.memory.body
                for record in self.knowledge(service)
            }[("project:beta",)],
            "贝塔 项目负责人更新为乙。",
        )

    def test_scope_not_grounded_final_retry_preserves_unregistered_project_scope(self):
        backend = QueueBackend()
        service = self.service(backend, name="scope-final-unregistered-legal")
        config = service.vault.config()
        config["scopes"] = {
            "project:alpha": {"aliases": ["阿尔法"]},
        }
        save_config(service.vault.config_path, config)
        old = service.create_memory(
            memory_id="alpha-existing",
            title="阿尔法 项目状态",
            body="阿尔法 项目状态保持不变。",
            type="project",
            scopes=["project:alpha"],
        )
        user_key, _ = self.capture_turn(
            service,
            turn="scope-final-unregistered-legal",
            user_event="scope-final-unregistered-legal-user",
            assistant_event="scope-final-unregistered-legal-assistant",
            user="阿尔法项目状态不变；浙江东方实施计划待确认。",
            assistant="已确认两个项目的状态。",
        )
        wrong = self.candidate(
            "new-unregistered-final-retry",
            [user_key],
            memory="浙江东方实施计划待确认。",
            type="project",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:浙江东方"]
        backend.responses.extend(
            [
                self.gate([wrong]),
                self.gate([wrong]),
                self.gate([{k: v for k, v in wrong.items() if k != "update_memory_id"}]),
                self.summary(
                    user_key,
                    title="浙江东方实施计划",
                    body="浙江东方实施计划待确认。",
                    type="project",
                    scopes=["project:浙江东方"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "gate", "gate", "summarize"])
        self.assertIn("Previous output violated: target_not_relevant.", backend.calls[1]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(self.knowledge(service)), 2)
        unregistered = [
            record
            for record in self.knowledge(service)
            if record.memory.scopes == ["project:浙江东方"]
        ]
        self.assertEqual(len(unregistered), 1)
        self.assertEqual(unregistered[0].memory.body, "浙江东方实施计划待确认。")

    def test_summary_scope_drift_retries_before_writing(self):
        backend = QueueBackend()
        service = self.service(backend, name="summary-scope-drift")
        user_key, _ = self.capture_turn(
            service,
            turn="summary-scope-drift",
            user_event="summary-scope-drift-user",
            assistant_event="summary-scope-drift-assistant",
            user="中银国际实施计划需要更新。",
            assistant="已确认中银国际计划调整。",
        )
        candidate = self.candidate(
            "zhongyin-plan",
            [user_key],
            memory="中银国际实施计划需要更新。",
            type="project",
        )
        candidate["scopes"] = ["project:中银国际"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划需要更新。",
                    type="project",
                    scopes=["project:摩根基金"],
                ),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body="中银国际实施计划需要更新。",
                    type="project",
                    scopes=["project:中银国际"],
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize"])
        self.assertIn("Previous output violated: scope_drift.", backend.calls[2]["prompt"])
        self.assertEqual(self.knowledge(service)[0].memory.scopes, ["project:中银国际"])
        self.assertNotIn("project:摩根基金", service.vault.config()["scopes"])

    def test_three_summary_scope_drifts_keep_queue_and_write_nothing(self):
        backend = QueueBackend()
        service = self.service(backend, name="summary-scope-drift-failure")
        user_key, _ = self.capture_turn(
            service,
            turn="summary-scope-drift-failure",
            user_event="summary-scope-drift-failure-user",
            assistant_event="summary-scope-drift-failure-assistant",
            user="中银国际实施计划需要更新。",
            assistant="已确认中银国际计划调整。",
        )
        candidate = self.candidate(
            "zhongyin-plan-failure",
            [user_key],
            memory="中银国际实施计划需要更新。",
            type="project",
        )
        candidate["scopes"] = ["project:中银国际"]
        bad_summary = self.summary(
            user_key,
            title="中银国际实施计划",
            body="中银国际实施计划需要更新。",
            type="project",
            scopes=["project:摩根基金"],
        )
        backend.responses.extend([self.gate([candidate]), bad_summary, bad_summary, bad_summary])

        with self.assertRaises(ModelOutputError) as raised:
            service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(raised.exception.validation_detail, "scope_drift")
        self.assertEqual(raised.exception.stage, "summarize")
        self.assertEqual(raised.exception.attempt_count, 3)
        marker = self.processed(service)["sessions"]["codex/s"]["processing"]
        self.assertEqual(marker["failure_stage"], "summarize")
        self.assertEqual(marker["validation_detail"], "scope_drift")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(self.processed(service)["sessions"]["codex/s"].get("watermark", 0), 0)
        self.assertEqual(self.knowledge(service), [])
        self.assertNotIn("project:摩根基金", service.vault.config()["scopes"])
        self.assertTrue((service.vault.inbox_path / "codex" / "s.md").is_file())

    def test_two_candidates_are_separate_deterministic_memories_and_repeat_is_noop(self):
        backend = QueueBackend()
        service = self.service(backend)
        user_key, assistant_key = self.capture_turn(service, user="A durable fact. A second durable fact.")
        backend.responses.extend(
            [
                self.gate(
                    [
                        self.candidate("c1", [user_key]),
                        self.candidate("c2", [user_key], memory="another durable fact"),
                    ]
                ),
                self.summary(user_key, title="First"),
                self.summary(user_key, title="Second"),
            ]
        )

        first = service.process()
        calls_after_first = len(backend.calls)
        second = service.process()

        self.assertEqual(first["memories_written"], 2)
        self.assertEqual(len(set(first["memory_ids"])), 2)
        self.assertEqual(len(self.knowledge(service)), 2)
        self.assertEqual(second["processed_turns"], 0)
        self.assertEqual(second["memories_written"], 0)
        self.assertEqual(len(backend.calls), calls_after_first)
        self.assertEqual(len(self.knowledge(service)), 2)
