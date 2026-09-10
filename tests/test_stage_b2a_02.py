from tests.stage_b2a_support import *


class StageB2ATestPart02(StageB2ATestBase):
    def test_mixed_project_digest_retries_to_atomic_project_outputs(self):
        self.enterContext(patch("memleaf.capture._timestamp", return_value="2026-09-01T02:01:41Z"))
        service = self.service(name="mixed-project-digest")
        history_memory = service.create_memory(
            memory_id="mem-zhongyin-history",
            title="中银国际历史需求",
            body="中银国际历史需求已完整覆盖：需求清单逐项确认、信创测试环境部署、历史数据库和附件全量迁移。",
            type="project",
            scopes=["project:zhongyin"],
        )
        existing = service.create_memory(
            memory_id="mem-zhongyin-plan",
            title="中银国际实施计划",
            body="中银国际实施计划当前按原始安排执行。",
            type="project",
            scopes=["project:zhongyin"],
        )
        config = service.vault.config()
        config["llm"]["diagnostic_logging"] = True
        config["scopes"] = {
            "project:zhongyin": {"aliases": ["中银国际"]},
            "project:morgan": {"aliases": ["摩根基金"]},
        }
        save_config(service.vault.config_path, config)
        user_key, assistant_key = self.capture_turn(
            service,
            session="mixed-project-digest",
            user_event="mixed-digest-user",
            assistant_event="mixed-digest-assistant",
            user=(
                "邮箱巡检：中银国际历史需求已覆盖需求清单逐项确认、信创测试环境部署、"
                "历史数据库和附件全量迁移；客户提出单点登录提前并行、数据文件规范提前确认、"
                "重新压实实施计划；摩根基金本周三排查证券申报类型；兴银转给测试同事；"
                "金元顺安待反馈；嘉实一次性启动会安排；浙江东方正文为空、尚未闭环、待问结论。"
            ),
            assistant="会议和待办已整理，后续按项目分别跟进。",
        )

        def scoped(candidate_id, *, memory, type, scopes, worth=True, update_memory_id=None):
            value = self.candidate(
                candidate_id,
                [user_key, assistant_key],
                memory=memory,
                type=type,
                worth=worth,
                update_memory_id=update_memory_id,
            )
            value["scopes"] = list(scopes)
            return value

        mixed = scoped(
            "digest",
            memory="2026-09-01邮箱巡检需关注事项",
            type="fact",
            scopes=[
                "project:zhongyin",
                "project:morgan",
                "project:兴银",
                "project:金元顺安",
                "project:嘉实",
                "project:浙江东方",
            ],
        )
        corrected = [
            scoped(
                "zhongyin-plan",
                memory="中银国际实施计划新增客户建议：单点登录提前并行、数据文件规范提前确认、重新压实实施计划",
                type="project",
                scopes=["project:zhongyin"],
                update_memory_id=existing.memory_id,
            ),
            scoped(
                "morgan-todo",
                memory="摩根基金排查证券申报类型",
                type="todo",
                scopes=["project:morgan"],
            ),
            scoped(
                "xingyin-transfer",
                memory="兴银事项已转给测试同事，等待内部处理",
                type="event",
                scopes=["project:兴银"],
            ),
            scoped(
                "jinyuan-pending",
                memory="金元顺安待反馈",
                type="event",
                scopes=["project:金元顺安"],
            ),
            scoped(
                "jiashi-kickoff",
                memory="嘉实一次性启动会安排，暂无持久决策或项目约束",
                type="event",
                scopes=["project:嘉实"],
            ),
            scoped(
                "zhejiang-pending",
                memory="浙江东方正文为空，尚未闭环，待问结论",
                type="event",
                scopes=["project:浙江东方"],
            ),
        ]
        backend = QueueBackend(
            [
                self.gate([mixed]),
                self.gate(corrected),
                self.summary(
                    user_key,
                    title="中银国际实施计划",
                    body=(
                        "客户提出单点登录提前并行、数据文件规范提前确认、重新压实实施计划；"
                        "当前待纳入，尚未表示已落地。"
                    ),
                    type="project",
                    scopes=["project:zhongyin"],
                    update_memory_id=existing.memory_id,
                ),
                self.summary(
                    user_key,
                    title="摩根基金证券申报类型排查",
                    body="待排查摩根基金证券申报类型，截止日期为2026-09-02。",
                    type="todo",
                    scopes=["project:morgan"],
                    status="active",
                ),
                json.dumps({"decision": "NO_CHANGE"}),
                json.dumps({"decision": "NO_CHANGE"}),
                json.dumps({"decision": "NO_CHANGE"}),
                json.dumps({"decision": "NO_CHANGE"}),
            ]
        )

        result = service.process(
            source="codex",
            session_id="mixed-project-digest",
            model=backend,
        )

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 2)
        self.assertEqual(
            [call["purpose"] for call in backend.calls],
            ["gate", "gate"] + ["summarize"] * 6,
        )
        self.assertIn("Previous output violated: mixed_project_scopes.", backend.calls[1]["prompt"])
        active = self.knowledge(service)
        self.assertEqual(len(active), 3)
        active_by_id = {record.memory.memory_id: record.memory for record in active}
        self.assertIn(history_memory.memory_id, active_by_id)
        self.assertIn(existing.memory_id, active_by_id)
        self.assertTrue(all(sum(scope.startswith("project:") for scope in record.memory.scopes) <= 1 for record in active))
        self.assertEqual(active_by_id[existing.memory_id].type, "project")
        self.assertIn("客户提出", active_by_id[existing.memory_id].body)
        self.assertIn("待纳入", active_by_id[existing.memory_id].body)
        todo = next(
            memory
            for memory_id, memory in active_by_id.items()
            if memory_id not in {history_memory.memory_id, existing.memory_id}
        )
        self.assertEqual(
            set(active_by_id),
            {history_memory.memory_id, existing.memory_id, todo.memory_id},
        )
        self.assertEqual(todo.type, "todo")
        self.assertEqual(todo.scopes, ["project:morgan"])
        self.assertIn("2026-09-02", todo.body)
        self.assertNotIn("本周三", todo.body)
        self.assertEqual(active_by_id[history_memory.memory_id].body, history_memory.body)
        self.assertIn("单点登录提前并行", active_by_id[existing.memory_id].body)
        self.assertNotIn("需求清单逐项确认", active_by_id[existing.memory_id].body)
        self.assertNotIn("历史数据库和附件全量迁移", active_by_id[existing.memory_id].body)
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.extra["active_memory_id"], existing.memory_id)
        self.assertEqual(history[0].memory.body, existing.body)
        bodies = "\n".join(record.memory.body for record in active)
        self.assertNotIn("邮箱巡检", bodies)
        self.assertNotIn("需关注事项", bodies)
        self.assertNotIn("转给测试同事", bodies)
        self.assertNotIn("待反馈", bodies)
        self.assertNotIn("启动会", bodies)
        self.assertNotIn("正文为空", bodies)
        self.assertEqual(
            len(service.vault.list_markdown("history")),
            1,
        )
        self.assertEqual(
            service.vault.config()["scopes"].keys(),
            {"project:zhongyin", "project:morgan"},
        )
        state = self.processed(service)["sessions"]["codex/mixed-project-digest"]
        self.assertEqual(state["scopes"], ["project:zhongyin", "project:morgan"])
        self.assertNotIn("project:兴银", state["scopes"])
        self.assertNotIn("project:金元顺安", state["scopes"])
        self.assertNotIn("project:嘉实", state["scopes"])
        self.assertNotIn("project:浙江东方", state["scopes"])
        diagnostics = [
            json.loads(line)
            for line in (service.vault.logs_path / "model-diagnostics.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(diagnostics[0]["validation_detail"], "mixed_project_scopes")

    def test_three_mixed_project_gate_failures_retain_deferred_evidence(self):
        backend = QueueBackend()
        service = self.service(backend, name="mixed-gate-failure")
        user_key, assistant_key = self.capture_turn(
            service,
            session="mixed-gate-failure",
            user_event="mixed-gate-failure-user",
            assistant_event="mixed-gate-failure-assistant",
        )
        mixed = self.candidate(
            "mixed-gate-candidate",
            [user_key, assistant_key],
            memory="cross-project digest",
            type="fact",
        )
        mixed["scopes"] = ["project:zhongyin", "project:morgan"]
        backend.responses.extend([self.gate([mixed])] * 3)

        result = service.process(source="codex", session_id="mixed-gate-failure", model=backend)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual([c["purpose"] for c in backend.calls], ["gate"] * 3)
        state = self.processed(service)["sessions"]["codex/mixed-gate-failure"]
        row = state["processed_turns"][0]["deferred_candidates"][0]
        self.assertEqual(row["reason"], "scope_conflict")
        self.assertEqual(row["scopes"], mixed["scopes"])
        # Scan progress can advance; unresolved original evidence remains.
        self.assertEqual(state["watermark"], 1)
        self.assertTrue((service.vault.inbox_path / "codex" / "mixed-gate-failure.md").is_file())
        self.assertEqual(self.knowledge(service), [])

    def test_draft_turn_is_discarded_and_final_email_state_is_the_only_memory(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-final-only")

        draft_user, draft_assistant = self.capture_turn(
            service,
            turn="draft",
            user_event="draft-user",
            assistant_event="draft-assistant",
            user="Prepare an email to Chen Zhongkai for my confirmation.",
            assistant="The email draft is ready for confirmation.",
        )
        backend.responses.append(self.gate([]))
        draft_result = service.process()
        self.assertEqual(draft_result["memories_written"], 0)
        self.assertEqual(len(self.knowledge(service)), 0)

        final_user, final_assistant = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm sending the Chen Zhongkai email now.",
            assistant="The email was sent to Chen Zhongkai.",
        )
        final_candidate = self.candidate(
            "email-sent",
            [final_user, final_assistant],
            memory="The email to Chen Zhongkai was sent.",
            type="event",
        )
        backend.responses.extend(
            [
                self.gate([final_candidate]),
                self.summary(
                    final_user,
                    title="Chen Zhongkai email sent",
                    body="The email to Chen Zhongkai was sent.",
                    type="event",
                ),
            ]
        )
        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        active = self.knowledge(service)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].memory.body, "The email to Chen Zhongkai was sent.")
        self.assertNotIn("draft", active[0].memory.body.lower())
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        self.assertEqual(
            [memory.memory_id for memory in service.search("draft", include_history=False, todo_status="all")],
            [],
        )
        found = service.search("Chen Zhongkai email", include_history=False, todo_status="all")
        self.assertEqual([memory.memory_id for memory in found], [active[0].memory.memory_id])

    def test_gate_update_target_is_forwarded_when_summary_omits_it_and_archives_old_state(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-target")
        old = service.create_memory(
            memory_id="mem-draft",
            title="Chen Zhongkai email draft",
            body="A draft email to Chen Zhongkai is awaiting confirmation.",
            tags=["email"],
            type="event",
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="sent",
            user_event="sent-user",
            assistant_event="sent-assistant",
            user="Confirm sending the Chen Zhongkai email.",
            assistant="The Chen Zhongkai email has been sent.",
        )
        candidate = self.candidate(
            "email-sent",
            [user_key, assistant_key],
            memory="The Chen Zhongkai email was sent.",
            type="event",
            update_memory_id=old.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="Chen Zhongkai email sent",
                    body="The Chen Zhongkai email was sent.",
                    type="event",
                ),
            ]
        )

        result = service.process()

        self.assertEqual(result["memories_written"], 1)
        active = self.knowledge(service)
        self.assertEqual([record.memory.memory_id for record in active], [old.memory_id])
        self.assertEqual(active[0].memory.body, "The Chen Zhongkai email was sent.")
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old.body)
        self.assertEqual(history[0].memory.extra["active_memory_id"], old.memory_id)
        self.assertEqual(
            [memory.memory_id for memory in service.search("draft", include_history=False, todo_status="all")],
            [],
        )
        self.assertEqual(
            [memory.memory_id for memory in service.search("Chen Zhongkai email", include_history=False, todo_status="all")],
            [old.memory_id],
        )

    def test_natural_project_owner_update_reuses_id_and_archives_old_state(self):
        backend = QueueBackend()
        service = self.service(backend, name="natural-owner-update")

        first_user, first_assistant = self.capture_turn(
            service,
            source="hermes",
            session="s",
            turn="owner-a",
            user_event="owner-a-user",
            assistant_event="owner-a-assistant",
            user="ML-STATE-20260827 项目负责人是甲。",
            assistant="已确认 ML-STATE-20260827 当前负责人为甲。",
        )
        backend.responses.extend(
            [
                self.gate(
                    [
                        self.candidate(
                            "ml-state-owner",
                            [first_user, first_assistant],
                            memory="ML-STATE-20260827 项目负责人是甲。",
                            type="identity",
                        )
                    ]
                ),
                self.summary(
                    first_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人是甲。",
                    type="identity",
                ),
            ]
        )
        first_result = service.process(source="hermes", session_id="s")
        self.assertEqual(first_result["processed_turns"], 1)
        first_memory = self.knowledge(service)[0].memory

        second_user, second_assistant = self.capture_turn(
            service,
            source="hermes",
            session="s",
            turn="owner-b",
            user_event="owner-b-user",
            assistant_event="owner-b-assistant",
            user="ML-STATE-20260827 同一项目负责人更新为乙。",
            assistant="已确认今后以乙为准。",
        )
        update_candidate = self.candidate(
            "ml-state-owner-update",
            [second_user, second_assistant],
            memory="ML-STATE-20260827 项目负责人更新为乙。",
            type="identity",
            update_memory_id=first_memory.memory_id,
        )
        backend.responses.extend(
            [
                self.gate([update_candidate]),
                self.summary(
                    second_user,
                    title="ML-STATE-20260827 项目负责人",
                    body="ML-STATE-20260827 项目负责人已更新为乙。",
                    type="identity",
                    update_memory_id=first_memory.memory_id,
                ),
            ]
        )
        second_result = service.process(source="hermes", session_id="s")

        self.assertEqual(first_result["memory_ids"], [first_memory.memory_id])
        self.assertEqual(second_result["processed_turns"], 1)
        active = self.knowledge(service)
        self.assertEqual([record.memory.memory_id for record in active], [first_memory.memory_id])
        self.assertEqual(active[0].memory.body, "ML-STATE-20260827 项目负责人已更新为乙。")
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, "ML-STATE-20260827 项目负责人是甲。")
        self.assertEqual(history[0].memory.extra["active_memory_id"], first_memory.memory_id)
        self.assertEqual(
            [memory.memory_id for memory in service.search("ML-STATE-20260827 项目负责人", include_history=False, todo_status="all")],
            [first_memory.memory_id],
        )
        self.assertEqual(
            [memory.memory_id for memory in service.search("负责人是甲", include_history=False, todo_status="all")],
            [],
        )
        processed = self.processed(service)["sessions"]["hermes/s"]
        self.assertEqual(processed["watermark"], 2)
        self.assertEqual(processed["processing"]["status"], "idle")

    def test_additive_project_plan_update_retains_existing_plan_facts(self):
        backend = QueueBackend()
        service = self.service(backend, name="additive-project-plan")
        old_body = (
            "金元顺安信创实施计划采用达梦和东方通，要求38个工作日完成，"
            "上线日期为2026-10-27，负责人为吴江波。"
        )
        existing = service.create_memory(
            memory_id="mem-jinyuan-plan",
            title="金元顺安员工投资行为申报系统信创改造实施计划",
            body=old_body,
            tags=["金元顺安", "达梦", "东方通", "负责人"],
            type="project",
            scopes=["project:金元顺安"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            source="hermes",
            session="jinyuan-plan",
            turn="feedback",
            user_event="jinyuan-plan-user",
            assistant_event="jinyuan-plan-assistant",
            user="金元顺安客户要求实施计划补充数据迁移、安全基线、漏洞扫描和回滚演练。",
            assistant="需增加数据迁移、安全基线、漏洞扫描和回滚演练。",
        )
        item = self.candidate(
            "jinyuan-plan-feedback",
            [user_key, assistant_key],
            memory="金元顺安实施计划需补充数据迁移、安全基线、漏洞扫描和回滚演练。",
            type="project",
            update_memory_id=existing.memory_id,
        )
        item["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([item]),
                self.summary(
                    user_key,
                    title=existing.title,
                    body=old_body + "\n\n客户要求补充数据迁移、安全基线、漏洞扫描和回滚演练。",
                    tags=["金元顺安", "达梦", "东方通", "负责人", "调整建议"],
                    type="project",
                    scopes=["project:金元顺安"],
                    update_memory_id=existing.memory_id,
                ),
            ]
        )

        result = service.process(source="hermes", session_id="jinyuan-plan", model=backend)

        self.assertEqual(result["memory_ids"], [existing.memory_id])
        current = service.read(existing.memory_id)
        self.assertIn(old_body, current.body)
        self.assertIn("回滚演练", current.body)
        self.assertEqual(current.title, existing.title)
        self.assertEqual(current.tags, ["金元顺安", "达梦", "东方通", "负责人", "调整建议"])
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, old_body)

    def test_explicit_project_plan_replacement_does_not_merge_conflicting_body(self):
        service = self.service()
        target = service.create_memory(memory_id="mem-plan-replacement", title="北辰项目实施计划",
            body="北辰项目数据库采用达梦。", tags=["达梦"], type="project", scopes=["project:北辰"])
        key, _ = self.capture_turn(service, user="北辰项目实施计划的数据库改为PostgreSQL。")
        proposal = self.candidate("replacement", [key], memory="北辰项目实施计划的数据库改为PostgreSQL。",
            type="project", update_memory_id=target.memory_id)
        proposal["scopes"] = ["project:北辰"]
        body = "北辰项目数据库改为PostgreSQL。"
        backend = QueueBackend([self.gate([proposal]), self.summary(key, title=target.title,
            body=body, type="project", scopes=["project:北辰"], update_memory_id=target.memory_id)])
        result = service.process(model=backend, scope="project:北辰")
        self.assertEqual(result["memory_ids"], [target.memory_id])
        self.assertEqual(service.read(target.memory_id).body, body)
        history = service._read_memories_unlocked("history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].memory.body, target.body)

    def test_summary_update_target_mismatch_is_retried_before_writing(self):
        backend = QueueBackend()
        service = self.service(backend, name="email-update-mismatch")
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
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(user_key, title="Sent", body="The email was sent.", type="event", update_memory_id=other.memory_id),
                self.summary(user_key, title="Sent", body="The email was sent.", type="event", update_memory_id=old.memory_id),
            ]
        )

        result = service.process()

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize", "summarize"])
        self.assertIn("gate selected the update target", backend.calls[2]["prompt"])
        self.assertEqual(service.read(old.memory_id).body, "The email was sent.")
        self.assertEqual(service.read(other.memory_id).body, other.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 1)

    def test_model_selects_create_for_same_scope_different_future_use(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-wrong-target")
        old = service.create_memory(
            memory_id="mem-orion-sync",
            title="金元顺安项目任务同步到 Orion 系统",
            body=(
                "金元顺安项目任务已同步到 Orion；里程碑包含信创环境搭建、"
                "功能验证测试和上线安排。"
            ),
            type="todo",
            scopes=["project:金元顺安"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-wrong-target",
            user_event="same-project-wrong-target-user",
            assistant_event="same-project-wrong-target-assistant",
            user="金元顺安实施计划六项建议；同时需要查看 Orion 任务同步。",
            assistant="已整理金元顺安实施计划与 Orion 任务同步。",
        )
        wrong = self.candidate(
            "wrong-jinyuan-target",
            [user_key, assistant_key],
            memory="金元顺安实施计划六项建议含里程碑、信创环境、测试和上线安排。",
            type="todo",
            update_memory_id=old.memory_id,
        )
        wrong["scopes"] = ["project:金元顺安"]
        corrected = dict(wrong, candidate_id="jinyuan-independent")
        corrected.pop("update_memory_id")
        backend.responses.extend(
            [
                self.gate([corrected]),
                self.summary(
                    user_key,
                    title="金元顺安实施计划六项建议",
                    body="金元顺安实施计划六项建议含里程碑、信创环境、测试和上线安排。",
                    type="todo",
                    scopes=["project:金元顺安"],
                    status="active",
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        self.assertNotIn("target_not_relevant", " ".join(call["prompt"] for call in backend.calls))
        self.assertEqual(service.read(old.memory_id).title, old.title)
        self.assertEqual(service.read(old.memory_id).body, old.body)
        self.assertEqual(len(service.vault.list_markdown("history")), 0)
        active = service._read_memories_unlocked("knowledge")
        self.assertEqual(len(active), 2)
        self.assertIn(
            "金元顺安实施计划六项建议含里程碑、信创环境、测试和上线安排。",
            "\n".join(item.memory.body for item in active),
        )

    def test_same_project_same_future_use_target_remains_valid(self):
        backend = QueueBackend()
        service = self.service(backend, name="same-project-valid-target")
        old = service.create_memory(
            memory_id="mem-jinyuan-plan",
            title="金元顺安实施计划",
            body="金元顺安实施计划按原方案执行。",
            type="project",
            scopes=["project:金元顺安"],
        )
        user_key, assistant_key = self.capture_turn(
            service,
            turn="same-project-valid-target",
            user_event="same-project-valid-target-user",
            assistant_event="same-project-valid-target-assistant",
            user="金元顺安实施计划新增部署要求。",
            assistant="已确认金元顺安实施计划需要纳入部署要求。",
        )
        candidate = self.candidate(
            "valid-jinyuan-target",
            [user_key, assistant_key],
            memory="金元顺安实施计划新增部署要求。",
            type="project",
            update_memory_id=old.memory_id,
        )
        candidate["scopes"] = ["project:金元顺安"]
        backend.responses.extend(
            [
                self.gate([candidate]),
                self.summary(
                    user_key,
                    title="金元顺安实施计划",
                    body="金元顺安实施计划按原方案执行。\n\n金元顺安实施计划新增部署要求。",
                    type="project",
                    scopes=["project:金元顺安"],
                    update_memory_id=old.memory_id,
                ),
            ]
        )

        result = service.process(source="codex", session_id="s", model=backend)

        self.assertEqual(result["memories_written"], 1)
        self.assertEqual([call["purpose"] for call in backend.calls], ["gate", "summarize"])
        self.assertEqual(
            service.read(old.memory_id).body,
            "金元顺安实施计划按原方案执行。\n\n金元顺安实施计划新增部署要求。",
        )
        self.assertEqual(len(service.vault.list_markdown("history")), 1)
