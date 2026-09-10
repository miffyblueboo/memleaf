from tests.stage_b1_support import *


class StageB1TestPart01(StageB1TestBase):
    def test_rebuild_preserves_all_runtime_state_including_old_event_evidence(self):
        self.service.capture("codex", "s", "turn-1", "user", "hello", event_id="live")
        processed_path = self.vault_path / "_state" / "processed.json"
        current = json.loads(processed_path.read_text(encoding="utf-8"))
        stale = "f" * 64
        current["events"][stale] = {"event_key": stale, "event_id": "should-not-survive"}
        current["event_keys"].append(stale)
        state = {
            "watermark": 7,
            "processed_turn_index": 7,
            "processing": {"status": "processing", "turn_index": 8},
            "eligible": [{"turn_index": 7, "eligible_at": "2026-01-01T00:00:00Z"}],
            "host_owned": {"opaque": True},
        }
        current["sessions"]["codex/s"] = state
        processed_path.write_text(json.dumps(current), encoding="utf-8")

        self.service.rebuild_index()

        rebuilt = json.loads(processed_path.read_text(encoding="utf-8"))
        live_key = event_key("live")
        self.assertIn(live_key, rebuilt["event_keys"])
        self.assertIn(stale, rebuilt["event_keys"])
        self.assertEqual(rebuilt["events"][stale]["event_id"], "should-not-survive")
        self.assertEqual(rebuilt["sessions"]["codex/s"], state)

    def test_turn_key_survives_redaction_and_separates_colliding_display_ids(self):
        raw_one = "password=one"
        raw_two = "password=two"
        self.service.capture("codex", "s", raw_one, "user", "one", event_id="one-user")
        self.service.capture("codex", "s", raw_one, "assistant", "answer", event_id="one-assistant")
        self.service.capture("codex", "s", raw_two, "user", "two", event_id="two-user")
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].turn_key, turn_key(raw_one))
        self.assertEqual(turns[1].turn_key, turn_key(raw_two))
        self.assertTrue(turns[0].complete)
        self.assertFalse(turns[1].complete)
        self.assertEqual(turns[0].events[0].turn_id, turns[1].events[0].turn_id)
        self.assertNotEqual(turns[0].turn_key, turns[1].turn_key)
        text = (self.vault_path / "inbox" / "codex" / "s.md").read_text(encoding="utf-8")
        self.assertNotIn(raw_one, text)
        self.assertNotIn(raw_two, text)

    def test_complete_turn_requires_exactly_one_assistant(self):
        self.service.capture("codex", "s", "turn-1", "user", "one", event_id="u1")
        self.service.capture("codex", "s", "turn-1", "assistant", "answer", event_id="a1")
        self.service.capture("codex", "s", "turn-1", "assistant", "extra", event_id="a2")
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 1)
        self.assertFalse(turns[0].complete)
        self.assertEqual(complete_turns(self.service.vault), [])

    def test_complete_turn_allows_multiple_users_before_one_assistant(self):
        self.service.capture("codex", "s", "turn-group", "user", "first", event_id="u1")
        self.service.capture("codex", "s", "turn-group", "user", "second", event_id="u2")
        self.service.capture("codex", "s", "turn-group", "assistant", "answer", event_id="a1")
        turns = parse_inbox(self.service.vault)
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].complete)
        self.assertEqual(complete_turns(self.service.vault), turns)

    def test_event_timestamp_is_preserved_in_extraction_payload_and_prompts(self):
        anchor = "2026-09-01T02:01:41Z"
        with mock.patch("memleaf.capture._timestamp", return_value=anchor):
            self.service.capture("codex", "calendar", "turn-1", "user", "Finish before Wednesday", event_id="calendar-user")
            self.service.capture(
                "codex",
                "calendar",
                "turn-1",
                "assistant",
                "I will track the deadline.",
                event_id="calendar-assistant",
            )

        turn = parse_inbox(self.service.vault)[0]
        events = _event_payload(turn)
        self.assertEqual([event["timestamp"] for event in events], [anchor, anchor])
        candidate = {
            "candidate_id": "calendar-deadline",
            "memory": "Finish before Wednesday",
            "evidence_event_ids": [event_key("calendar-user")],
            "duplicate": False,
            "worth": True,
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
        }
        gate_text = gate_prompt(events)
        summary_text = summarize_prompt(candidate, events)
        self.assertIn(anchor, gate_text)
        self.assertIn(anchor, summary_text)
        for prompt_system in (GATE_SYSTEM, SUMMARIZE_SYSTEM):
            self.assertIn("ISO-8601 UTC timestamp", prompt_system)
            self.assertIn("YYYY-MM-DD", prompt_system)
            self.assertIn("recurring schedules", prompt_system.casefold())
        # Exact multilingual date forms are validated/normalized by Core, not taught in every model prompt.
        self.assertNotIn("本周X/这周X/下周X/上周X", GATE_SYSTEM + SUMMARIZE_SYSTEM)

    def test_relative_summary_is_deterministically_normalized_before_validation(self):
        anchor = "2026-09-02T02:01:41Z"
        evidence_key = event_key("relative-retry-user")
        gate = {
            "candidates": [{
                "candidate_id": "relative-deadline",
                "memory": "Complete before Wednesday",
                "evidence_event_ids": [evidence_key],
                "duplicate": False,
                "worth": True,
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
            }]
        }

        def summary(body):
            return {
                "title": "Project deadline",
                "body": body,
                "tags": ["deadline"],
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
                "sources": [{"event_key": evidence_key}],
                "status": "active",
            }

        responses = [
            json.dumps(gate),
            json.dumps(summary("在本周三（9/3）前完成。")),
        ]
        calls = []

        @semantic_function
        def callback(prompt, **kwargs):
            calls.append((prompt, kwargs["purpose"]))
            return responses.pop(0)

        with mock.patch("memleaf.capture._timestamp", return_value=anchor):
            self.service.capture("codex", "relative-retry", "turn-1", "user", "Finish before this Wednesday", event_id="relative-retry-user")
            self.service.capture(
                "codex",
                "relative-retry",
                "turn-1",
                "assistant",
                "The deadline is confirmed.",
                event_id="relative-retry-assistant",
            )

        result = self.service.process(
            source="codex",
            session_id="relative-retry",
            model=FakeBackend(callback, model="relative-retry"),
        )
        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual([purpose for _, purpose in calls], ["gate", "summarize"])
        self.assertIn(anchor, calls[0][0])
        memory = self.service.read(result["memory_ids"][0])
        self.assertIsNotNone(memory)
        self.assertIn("2026-09-02", memory.body)
        self.assertNotIn("本周三", memory.body)

    def test_recurring_weekday_expression_is_not_rejected_as_relative_date(self):
        for body in (
            "每周三例会。",
            "The recurring meeting is every Wednesday.",
            "会议时间：2026-09-01（周二）16:30。",
        ):
            with self.subTest(body=body):
                summary = {
                    "title": "Recurring meeting",
                    "body": body,
                    "tags": ["meeting"],
                    "type": "event",
                    "scopes": ["global"],
                    "sources": [{"event_key": "event-a"}],
                }
                parsed = parse_summarize_output(json.dumps(summary), current_event_keys=["event-a"])
                self.assertEqual(parsed["body"], body)

    def test_relative_summary_without_timestamp_is_rejected_and_only_title_body_are_scanned(self):
        relative = {
            "title": "截止今天",
            "body": "完成项目交付。",
            "tags": [],
            "type": "project",
            "scopes": ["global"],
            "sources": [{"event_key": "event-a", "conversation_title": "今天的会话"}],
        }
        with self.assertRaises(ModelOutputError) as raised:
            parse_summarize_output(json.dumps(relative), current_event_keys=["event-a"])
        self.assertEqual(raised.exception.validation_detail, "relative_time")

        recurring_metadata = dict(relative, title="交付安排", body="每周三例会。")
        parsed = parse_summarize_output(json.dumps(recurring_metadata), current_event_keys=["event-a"])
        self.assertEqual(parsed["sources"][0]["conversation_title"], "今天的会话")

    def test_relative_summary_without_reliable_timestamp_is_deferred(self):
        anchor = "2026-09-02T02:01:41Z"
        evidence_key = event_key("relative-failure-user")
        gate = {
            "candidates": [{
                "candidate_id": "relative-failure",
                "memory": "Complete before Wednesday",
                "evidence_event_ids": [evidence_key],
                "duplicate": False,
                "worth": True,
                "type": "todo",
                "scopes": ["global"],
                "scope_source": "model",
            }]
        }
        relative_summary = {
            "title": "Project deadline",
            "body": "在本周三前完成。",
            "tags": ["deadline"],
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
            "sources": [{"event_key": evidence_key}],
            "status": "active",
        }
        responses = [json.dumps(gate)] + [json.dumps(relative_summary)] * 3
        calls = []

        @semantic_function
        def callback(prompt, **kwargs):
            calls.append(kwargs["purpose"])
            return responses.pop(0)

        with mock.patch("memleaf.capture._timestamp", return_value=anchor):
            self.service.capture("codex", "relative-failure", "turn-1", "user", "Finish before Wednesday", event_id="relative-failure-user")
            self.service.capture(
                "codex",
                "relative-failure",
                "turn-1",
                "assistant",
                "The deadline is confirmed.",
                event_id="relative-failure-assistant",
            )

        inbox = self.vault_path / "inbox" / "codex" / "relative-failure.md"
        inbox.write_text(
            inbox.read_text(encoding="utf-8").replace(
                f'"timestamp":"{anchor}"',
                '"timestamp":"not-a-timestamp"',
            ),
            encoding="utf-8",
        )

        result = self.service.process(
            source="codex",
            session_id="relative-failure",
            model=FakeBackend(callback, model="relative-failure"),
        )
        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["deferred_candidates"], 1)
        self.assertEqual(calls, ["gate", "summarize", "summarize", "summarize"])
        processed = json.loads(self.service.vault.processed_state_path.read_text(encoding="utf-8"))
        state = processed["sessions"]["codex/relative-failure"]
        self.assertEqual(state.get("watermark", 0), 1)
        self.assertEqual(state["processing"]["status"], "idle")
        self.assertEqual(state["processed_turns"][0]["deferred_candidates"][0]["reason"], "relative_time")
        self.assertTrue(inbox.is_file())
        self.assertIn("Finish before Wednesday", inbox.read_text(encoding="utf-8"))

    def test_relative_calendar_normalizer_uses_week_dates_and_preserves_recurring_schedules(self):
        anchor = "2026-09-02T02:01:41Z"
        self.assertEqual(
            normalize_relative_calendar_text("本周四（明天 9/3）", anchor),
            "2026-09-03",
        )
        self.assertEqual(
            normalize_relative_calendar_text("本周四（明天 9/4）", anchor),
            "2026-09-03",
        )
        self.assertEqual(
            normalize_relative_calendar_text("明天（部署版本1.2.3）", anchor),
            "2026-09-03（部署版本1.2.3）",
        )
        self.assertEqual(
            normalize_relative_calendar_text("明天（2/31）", anchor),
            "2026-09-03（2/31）",
        )
        self.assertEqual(
            normalize_relative_calendar_text("明天（检查 10.0.0.1）", anchor),
            "2026-09-03（检查 10.0.0.1）",
        )
        self.assertEqual(
            normalize_relative_calendar_text("本周三就是今天", anchor),
            "2026-09-02",
        )
        self.assertEqual(
            normalize_relative_calendar_text("today; tomorrow; yesterday", anchor),
            "2026-09-02; 2026-09-03; 2026-09-01",
        )
        self.assertEqual(
            normalize_relative_calendar_text("this Thursday; next Monday; last Sunday", anchor),
            "2026-09-03; 2026-09-07; 2026-08-30",
        )
        self.assertEqual(normalize_relative_calendar_text("每周三例会", anchor), "每周三例会")
        self.assertEqual(normalize_relative_calendar_text("every Wednesday", anchor), "every Wednesday")
        self.assertIsNone(normalize_relative_calendar_text("本周末安排", anchor))
        self.assertIsNone(normalize_relative_calendar_text("明天完成", None))
        self.assertIsNone(normalize_relative_calendar_text("明天完成", "invalid"))

    def test_date_parentheses_are_removed_only_when_empty(self):
        anchor = "2026-09-02T02:01:41Z"
        self.assertEqual(
            normalize_relative_calendar_text("2026-09-03（）", anchor),
            "2026-09-03",
        )
        self.assertEqual(
            normalize_relative_calendar_text("2026-09-03()", anchor),
            "2026-09-03",
        )
        summary = {
            "title": "反馈截止 2026-09-03（）",
            "body": "English spelling: 2026-09-03()",
            "tags": [],
            "type": "todo",
            "scopes": ["global"],
            "sources": [{"event_key": "event-a"}],
            "status": "active",
        }
        parsed = parse_summarize_output(json.dumps(summary), current_event_keys=["event-a"])
        self.assertEqual(parsed["title"], "反馈截止 2026-09-03")
        self.assertEqual(parsed["body"], "English spelling: 2026-09-03")
        self.assertEqual(
            parse_summarize_output(
                json.dumps(dict(summary, body="2026-09-03（周四）")),
                current_event_keys=["event-a"],
            )["body"],
            "2026-09-03（周四）",
        )

    def test_business_shaped_candidate_is_not_reclassified_by_local_rules(self):
        key = event_key("business-shaped")
        raw = json.dumps({"candidates": [{
            "candidate_id": "business-shaped",
            "memory": "Orion汇总：子任务完成4条；现场增补待受理2条。",
            "evidence_event_ids": [key],
            "duplicate": False,
            "worth": True,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }]})
        parsed = parse_gate_output(raw, current_event_keys=[key])
        self.assertEqual(parsed["candidates"][0]["type"], "fact")
        self.assertTrue(parsed["candidates"][0]["worth"])

    def test_document_shaped_candidate_is_not_reclassified_by_local_rules(self):
        key = event_key("document-shaped")
        raw = json.dumps({"candidates": [{
            "candidate_id": "document-shaped",
            "memory": "评审材料需要在2026-09-03前由张三逐项整改。",
            "evidence_event_ids": [key],
            "duplicate": False,
            "worth": True,
            "type": "todo",
            "scopes": ["global"],
            "scope_source": "model",
        }]})
        parsed = parse_gate_output(raw, current_event_keys=[key])
        self.assertEqual(parsed["candidates"][0]["type"], "todo")
        self.assertTrue(parsed["candidates"][0]["worth"])

    def test_process_retries_schema_violation_until_third_gate_attempt_and_commits(self):
        responses = [
            json.dumps({"candidates": "invalid"}),
            json.dumps({"candidates": "invalid"}),
            json.dumps({"candidates": []}),
        ]
        calls = []

        @semantic_function
        def callback(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return responses.pop(0)

        backend = FakeBackend(callback, model="schema-retry")
        self.service.capture("codex", "schema-retry", "turn-1", "user", "visible user", event_id="u1")
        self.service.capture(
            "codex",
            "schema-retry",
            "turn-1",
            "assistant",
            "visible assistant",
            event_id="a1",
        )

        result = self.service.process(source="codex", session_id="schema-retry", model=backend)

        self.assertEqual(result["processed_turns"], 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual([kwargs["purpose"] for _, kwargs in calls], ["gate", "gate", "gate"])
        self.assertTrue(all("Correction:" in prompt for prompt, _ in calls[1:]))
        state = json.loads(self.service.vault.processed_state_path.read_text(encoding="utf-8"))["sessions"][
            "codex/schema-retry"
        ]
        self.assertEqual(state["watermark"], 1)
        self.assertEqual(state["processing"]["status"], "idle")

    def test_process_three_schema_violations_fail_with_complete_gate_diagnostics(self):
        invalid = json.dumps({"candidates": "invalid"})
        responses = [invalid, invalid, invalid]
        calls = []

        @semantic_function
        def callback(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return responses.pop(0)

        backend = FakeBackend(callback, model="schema-failure")
        self.service.capture("codex", "schema-failure", "turn-1", "user", "visible user", event_id="u1")
        self.service.capture(
            "codex",
            "schema-failure",
            "turn-1",
            "assistant",
            "visible assistant",
            event_id="a1",
        )

        with self.assertRaises(ModelOutputError) as raised:
            self.service.process(source="codex", session_id="schema-failure", model=backend)

        error = raised.exception
        self.assertEqual(error.stage, "gate")
        self.assertEqual(error.validation_reason, "schema_violation")
        self.assertEqual(error.attempt_count, 3)
        processed = json.loads(self.service.vault.processed_state_path.read_text(encoding="utf-8"))
        marker = processed["sessions"]["codex/schema-failure"]["processing"]
        self.assertEqual(marker["status"], "failed")
        self.assertTrue(
            {
                "status",
                "token",
                "turn_keys",
                "turn_indices",
                "failed_at",
                "failure_code",
                "failure_stage",
                "validation_reason",
                "validation_detail",
                "attempt_count",
            }.issubset(marker)
        )
        self.assertEqual(marker["failure_code"], "model_invalid_response")
        self.assertEqual(marker["failure_stage"], "gate")
        self.assertEqual(marker["validation_reason"], "schema_violation")
        self.assertEqual(marker["validation_detail"], "root_shape")
        self.assertEqual(marker["attempt_count"], 3)
        self.assertEqual(processed["sessions"]["codex/schema-failure"].get("watermark", 0), 0)
        self.assertEqual(len(calls), 3)

    def test_capture_keeps_processing_owned_session_fields(self):
        processed_path = self.vault_path / "_state" / "processed.json"
        value = json.loads(processed_path.read_text(encoding="utf-8"))
        owned = {
            "watermark": 3,
            "processed_turn_index": 3,
            "processing": {"status": "processing", "turn_index": 4, "token": "opaque"},
            "host_state": {"keep": [1, 2, 3]},
        }
        value["sessions"] = {"codex/s": owned}
        processed_path.write_text(json.dumps(value), encoding="utf-8")
        self.service.capture("codex", "s", "turn-4", "user", "pending", event_id="pending")
        after = json.loads(processed_path.read_text(encoding="utf-8"))
        kept = after["sessions"]["codex/s"]
        for key, item in owned.items():
            self.assertEqual(kept[key], item)

    def test_parser_never_pairs_legacy_v1_or_forged_body_marker(self):
        legacy_a = event_key("legacy-a")
        legacy_b = event_key("legacy-b")
        legacy = (
            "# old\n"
            f"<!-- memleaf:event-key:v1:{legacy_a} -->\n"
            f"<!-- memleaf:event-key:v1:{legacy_b} -->\n"
        )
        parsed_legacy = parse_inbox(self._write_inbox("legacy", "old", legacy))
        self.assertEqual(len(parsed_legacy), 2)
        self.assertTrue(all(item.legacy and not item.complete for item in parsed_legacy))

        fake = f"<!-- memleaf:event:v2 -->\n{{\"event_key\":\"{event_key('fake')}\"}}\n"
        stored = self.service.capture("codex", "s", "turn-1", "user", fake, event_id="real")
        self.service.rebuild_index()
        self.assertEqual([item.event_key for item in parse_inbox(self.service.vault)[0].events], [event_key("real")])
        self.assertTrue(stored.stored)
