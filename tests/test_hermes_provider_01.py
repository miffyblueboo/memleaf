from tests.hermes_provider_support import *


class HermesProviderTestsPart01(HermesProviderTestBase):
    def test_native_provider_registration_and_auto_process_config_boolean(self) -> None:
        context = types.SimpleNamespace(register_memory_provider=lambda provider: setattr(context, "provider", provider))
        provider_module.register(context)
        self.assertIsInstance(context.provider, HermesMemoryProvider)
        self.assertEqual(context.provider.name, "memleaf")

        config_path = self.hermes_home / "memleaf.json"
        config_path.write_text(json.dumps({"auto_process": "false"}), encoding="utf-8")
        loaded = provider_module._load_config(self.hermes_home)
        self.assertIs(loaded["auto_process"], False)
        self.assertIs(provider_module._as_bool("false", True), False)
        self.assertIs(provider_module._as_bool("true", False), True)

        provider_module.MemleafMemoryProvider().save_config({"auto_process": "false"}, str(self.hermes_home))
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertIs(saved["auto_process"], False)

    def test_non_primary_hermes_sessions_skip_recall_capture_and_process(self) -> None:
        lifecycle_values = [
            {"platform": "cron", "agent_context": "primary"},
            {"platform": "cli", "agent_context": "flush"},
            {"platform": "cli", "agent_context": "subagent"},
            {"platform": "cli", "agent_context": "cron"},
        ]

        for index, lifecycle in enumerate(lifecycle_values):
            with self.subTest(lifecycle=lifecycle):
                provider = self.provider(responses=[])
                client = provider._client
                with patch.object(provider_module, "_resolve_command") as resolve_command, patch.object(
                    provider_module, "_MCPClient"
                ) as mcp_client:
                    provider.initialize(
                        f"excluded-session-{index}",
                        hermes_home=str(self.hermes_home),
                        **lifecycle,
                    )

                self.assertFalse(provider._write_enabled)
                self.assertIsNone(provider._client)
                resolve_command.assert_not_called()
                mcp_client.assert_not_called()
                provider.on_turn_start(1, {"role": "user", "content": "excluded user"})
                self.assertEqual(provider.prefetch("excluded query"), "")
                provider.sync_turn("excluded user", "excluded assistant")
                self.assertEqual(client.calls, [])
                self.assertEqual(provider.system_prompt_block(), "")
                self.assertIsNone(provider.recall_status())

    def test_interactive_primary_session_keeps_recall_capture_and_process(self) -> None:
        client = FakeClient(
            responses=[
                {"stats": True},
                {
                    "scopes": [
                        {
                            "scope": "project:phoenix",
                            "parent": "project",
                            "aliases": ["phoenix"],
                        }
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = provider_module.MemleafMemoryProvider()
        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), patch.object(
            provider_module, "_MCPClient", return_value=client
        ):
            provider.initialize(
                "interactive-session",
                hermes_home=str(self.hermes_home),
                platform="cli",
                agent_context="primary",
            )

        context = provider.prefetch("what do we already know?")
        provider.sync_turn("visible user", "visible assistant", session_id="interactive-session")

        self.assertIn("project:phoenix", context)
        self.assertNotIn("memory_id", context)
        self.assertNotIn("PROVIDER_BODY_SENTINEL", context)
        self.assertIsNone(provider.recall_status())
        self.assertEqual(
            [name for name, _ in client.calls],
            ["stats", "scope_catalog", "capture", "capture", "process"],
        )
        self.assertEqual(
            client.calls[1][1],
            {
                "limit": provider_module._MAX_SCOPE_ITEMS,
            },
        )

    def test_background_jobs_poll_completed_notice_and_keep_sessions_independent(self) -> None:
        client = FakeClient(
            responses=[
                # session one, first turn
                {"stored": True}, {"stored": True},
                {"accepted": True, "completed": False, "status": "pending", "job_id": "job-one"},
                # session two, first turn while session one is active
                {"stored": True}, {"stored": True},
                {"status": "running", "completed": False, "job_id": "job-one"},
                {"accepted": True, "completed": False, "status": "pending", "job_id": "job-two"},
                # session one, second turn requests a rerun on job-one
                {"stored": True}, {"stored": True},
                {"status": "running", "completed": False, "job_id": "job-two"},
                {"status": "running", "completed": False, "job_id": "job-one"},
                {"accepted": True, "completed": False, "status": "running", "job_id": "job-one", "rerun_requested": True},
                # session one, next turn observes completion then queues a fresh job
                {"stored": True}, {"stored": True},
                {"status": "running", "completed": False, "job_id": "job-two"},
                {
                    "accepted": True,
                    "completed": True,
                    "status": "succeeded",
                    "job_id": "job-one",
                    "result": {
                        "processed_turns": 1,
                        "memories_written": 1,
                        "external_evidence_status": "metadata_only",
                        "external_evidence": {
                            "status": "metadata_only",
                            "external_record_count": 1,
                            "retained_body_count": 0,
                        },
                    },
                },
                {"accepted": True, "completed": False, "status": "pending", "job_id": "job-three"},
                {"scopes": [], "has_more": False, "next_cursor": None},
            ]
        )
        provider = self.provider(responses=[])
        provider._client = client
        provider.sync_turn("one first", "one answer", session_id="one")
        provider.sync_turn("two first", "two answer", session_id="two")
        provider.sync_turn("one second", "one answer two", session_id="one")
        provider.sync_turn("one third", "one answer three", session_id="one")

        self.assertEqual(provider._process_jobs_by_session["one"], "job-three")
        self.assertEqual(provider._process_jobs_by_session["two"], "job-two")
        notice = provider.prefetch("continue", session_id="one")
        self.assertIn("completed successfully", notice)
        self.assertIn("external records=1", notice)
        self.assertEqual(
            [name for name, _ in client.calls if name == "process_status"],
            ["process_status", "process_status", "process_status", "process_status", "process_status"],
        )

    def test_provider_warns_with_one_line_update_when_core_versions_differ(self) -> None:
        client = FakeClient(responses=[{"stats": True}])
        client.server_version = "0.2.15"
        provider = provider_module.MemleafMemoryProvider()
        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), \
             patch.object(provider_module, "_MCPClient", return_value=client), \
             patch.object(provider_module, "_provider_manifest_version", return_value="0.2.10"), \
             patch.object(provider_module.logger, "warning") as warning:
            provider.initialize(
                "version-mismatch-session",
                hermes_home=str(self.hermes_home),
                platform="cli",
                agent_context="primary",
            )

        self.assertEqual(warning.call_count, 1)
        self.assertIn(provider_module._UPDATE_COMMAND, " ".join(map(str, warning.call_args.args)))
        self.assertIn("mismatch", warning.call_args.args[0])

    def test_provider_does_not_warn_when_core_versions_match(self) -> None:
        client = FakeClient(responses=[{"stats": True}])
        client.server_version = "0.2.19"
        provider = provider_module.MemleafMemoryProvider()
        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), \
             patch.object(provider_module, "_MCPClient", return_value=client), \
             patch.object(provider_module, "_provider_manifest_version", return_value="0.2.19"), \
             patch.object(provider_module.logger, "warning") as warning:
            provider.initialize(
                "version-match-session",
                hermes_home=str(self.hermes_home),
                platform="cli",
                agent_context="primary",
            )

        warning.assert_not_called()

    def test_provider_handles_missing_core_version_without_claiming_sync(self) -> None:
        client = FakeClient(responses=[{"stats": True}])
        client.server_version = {"unexpected": True}
        provider = provider_module.MemleafMemoryProvider()
        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), \
             patch.object(provider_module, "_MCPClient", return_value=client), \
             patch.object(provider_module, "_provider_manifest_version", return_value="0.2.18"), \
             patch.object(provider_module.logger, "warning") as warning:
            provider.initialize(
                "version-missing-session",
                hermes_home=str(self.hermes_home),
                platform="cli",
                agent_context="primary",
            )

        self.assertEqual(warning.call_count, 1)
        self.assertIn("unavailable", warning.call_args.args[0])
        self.assertIn(provider_module._UPDATE_COMMAND, " ".join(map(str, warning.call_args.args)))

    def test_prefetch_bodyless_directory_is_bounded_and_keeps_read_ids(self) -> None:
        responses = [
            {
                "scope": f"project:p{index}",
                "parent": "project",
                "aliases": [f"p{index}"],
            }
            for index in range(8)
        ]
        provider = self.provider(
            responses=[{"scopes": responses, "has_more": False, "next_cursor": None}]
        )

        rendered = provider.prefetch("directory query", session_id="directory-session")

        self.assertLessEqual(len(rendered), provider_module._MAX_SCOPE_CHARS)
        self.assertEqual(8, rendered.count("\n- "))
        self.assertIn("call memleaf MCP search", rendered)
        self.assertIn("read only the selected memory", rendered)
        for index in range(8):
            self.assertIn(f"project:p{index}", rendered)
        self.assertNotIn("memory_id", rendered)
        self.assertNotIn("title", rendered)
        self.assertNotIn("body", rendered.casefold())
        self.assertEqual(
            provider._client.calls[0],
            (
                "scope_catalog",
                {
                    "limit": provider_module._MAX_SCOPE_ITEMS,
                },
            ),
        )

    def test_prefetch_connection_failure_reports_unavailable_without_fallback_or_writes(self) -> None:
        provider = self.provider(responses=[RuntimeError("MCP_CONNECTION_SECRET")])

        rendered = provider.prefetch("query", session_id="failed-context-session")

        self.assertIn("scope map was unavailable", rendered.casefold())
        self.assertIn("retrieval was not verified", rendered.casefold())
        self.assertEqual([name for name, _ in provider._client.calls], ["scope_catalog"])
        self.assertFalse(list(self.root.rglob("*.md")))
        self.assertNotIn("MCP_CONNECTION_SECRET", rendered)

    def test_scope_context_keeps_long_ids_and_marks_omitted_metadata(self) -> None:
        long_scope = "project:" + "s" * 320
        long_parent = "parent:" + "p" * 180
        long_alias = "alias:" + "a" * 180
        rendered, _ = provider_module._scope_context(
            {
                "scopes": [
                    {
                        "scope": long_scope,
                        "parent": long_parent,
                        "aliases": [long_alias],
                    }
                ],
                "has_more": True,
                "next_cursor": "scope-next-page",
            },
            retrieval_id="rtv-long-metadata",
        )

        self.assertLessEqual(len(rendered), provider_module._MAX_SCOPE_CHARS)
        self.assertIn(long_scope, rendered)
        self.assertNotIn(long_parent, rendered)
        self.assertNotIn(long_alias, rendered)
        self.assertIn("scope-next-page", rendered)
        self.assertIn("preview incomplete", rendered.casefold())

    def test_prefetch_malformed_scope_catalog_reports_safe_diagnostic(self) -> None:
        provider = self.provider(
            responses=[
                {
                    "scopes": [{"scope": "project:broken", "parent": None, "aliases": "not-a-list"}],
                    "has_more": False,
                    "next_cursor": None,
                }
            ]
        )

        rendered = provider.prefetch("query", session_id="malformed-context-session")

        self.assertIn("scope map was unavailable", rendered.casefold())
        self.assertIn("retrieval was not verified", rendered.casefold())
        self.assertEqual([name for name, _ in provider._client.calls], ["scope_catalog"])
        self.assertNotIn("not-a-list", rendered)

    def test_prefetch_requests_mcp_token_for_current_turn_and_injects_only_scope_map(self) -> None:
        client = FakeClient(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:phoenix", "parent": "project", "aliases": ["phoenix"]}
                    ],
                    "has_more": False,
                    "next_cursor": None,
                    "retrieval_id": "rtv-current-turn",
                }
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("token-session")
        provider.on_turn_start(7, {"role": "user", "content": "What changed in Phoenix?"})

        rendered = provider.prefetch("What changed in Phoenix?", session_id="token-session")

        self.assertIn("project:phoenix", rendered)
        self.assertIn("rtv-current-turn", rendered)
        self.assertNotIn("memory_id", rendered)
        self.assertNotIn("title", rendered)
        self.assertNotIn("body", rendered.casefold())
        self.assertEqual(
            client.calls[0],
            (
                "scope_catalog",
                {
                    "limit": provider_module._MAX_SCOPE_ITEMS,
                    "source": "hermes",
                    "session_id": "token-session",
                    "turn_id": "turn-000007-"
                    + provider_module._visible_fingerprint("What changed in Phoenix?"),
                },
            ),
        )

    def test_prefetch_hints_unique_project_scope_without_injecting_memory_data(self) -> None:
        provider = self.provider(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:alpha", "parent": "project", "aliases": ["Alpha"]},
                        {"scope": "project:beta", "parent": "project", "aliases": ["Beta"]},
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            ]
        )

        rendered = provider.prefetch("请查询 Alpha 项目部署进展", session_id="scope-hint-session")

        self.assertIn("scope=project:alpha", rendered)
        self.assertIn("unique project scope", rendered)
        self.assertIn("business subject words", rendered)
        self.assertNotIn("memory_id", rendered)
        self.assertNotIn("title", rendered)
        self.assertNotIn("body", rendered.casefold())

    def test_prefetch_does_not_guess_ambiguous_project_scope(self) -> None:
        provider = self.provider(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:alpha", "parent": "project", "aliases": ["shared"]},
                        {"scope": "project:beta", "parent": "project", "aliases": ["shared"]},
                    ],
                    "has_more": False,
                    "next_cursor": None,
                }
            ]
        )

        rendered = provider.prefetch("查询 shared 项目", session_id="ambiguous-scope-session")

        self.assertNotIn("scope=project:alpha", rendered)
        self.assertNotIn("scope=project:beta", rendered)

    def test_compression_rotation_migrates_pending_turn_and_retrieval_token(self) -> None:
        token = "rtv-compression-continuity"
        client = FakeClient(
            responses=[
                {
                    "scopes": [
                        {"scope": "project:alpha", "parent": "project", "aliases": ["alpha"]}
                    ],
                    "has_more": False,
                    "next_cursor": None,
                    "retrieval_id": token,
                },
                {
                    "linked": True,
                    "source": "hermes",
                    "session_id": "new-compression-session",
                    "parent_session_id": "old-compression-session",
                },
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("old-compression-session")
        provider.on_turn_start(4, {"role": "user", "content": "What changed in alpha?"})
        provider.prefetch("What changed in alpha?", session_id="old-compression-session")

        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "search-after-compression",
                        "function": {
                            "name": "mcp__memleaf__search",
                            "arguments": json.dumps(
                                {"query": "alpha changes", "scope": "project:alpha", "retrieval_id": token}
                            ),
                        },
                    },
                    {
                        "id": "read-after-compression",
                        "function": {
                            "name": "mcp__memleaf__read",
                            "arguments": json.dumps(
                                {"memory_id": "mem-alpha", "retrieval_id": token}
                            ),
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "search-after-compression",
                "content": json.dumps(
                    {
                        "status": "found",
                        "results": [
                            {"memory_id": "mem-alpha", "title": "Alpha"}
                        ],
                    }
                ),
            },
            {
                "role": "tool",
                "tool_call_id": "read-after-compression",
                "content": json.dumps({"memory_id": "mem-alpha", "body": "Alpha details"}),
            },
        ]

        provider.on_session_switch(
            "new-compression-session",
            parent_session_id="old-compression-session",
            reset=False,
            reason="compression",
        )
        with patch.object(provider_module.logger, "info") as info:
            provider.sync_turn(
                "What changed in alpha?",
                "The latest Alpha update is recorded.",
                session_id="new-compression-session",
                messages=messages,
            )

        self.assertEqual(
            provider._canonical_session_id("old-compression-session"),
            "new-compression-session",
        )
        self.assertEqual(provider._last_retrieval_observation, "found")
        capture_calls = [
            arguments for name, arguments in client.calls if name == "capture"
        ]
        self.assertEqual(len(capture_calls), 2)
        self.assertTrue(all(item["session_id"] == "new-compression-session" for item in capture_calls))
        self.assertTrue(all(item["turn_id"].startswith("turn-000004-") for item in capture_calls))
        lineage_calls = [
            arguments for name, arguments in client.calls if name == "session_lineage"
        ]
        self.assertEqual(
            lineage_calls,
            [{
                "source": "hermes",
                "session_id": "new-compression-session",
                "parent_session_id": "old-compression-session",
            }],
        )
        process_calls = [
            arguments for name, arguments in client.calls if name == "process"
        ]
        self.assertEqual(process_calls, [{"source": "hermes", "session_id": "new-compression-session", "background": True}])
        self.assertEqual(provider._active_retrieval_ids["new-compression-session"], token)
        self.assertNotIn(("old-compression-session", 4), provider._retrieval_ids_by_turn)
        self.assertEqual(provider._retrieval_ids_by_turn[("new-compression-session", 4)], token)
        output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertIn(
            "retrieval_present=True retrieval_match=True result=ok",
            output,
        )

    def test_failed_compression_lineage_defers_process_until_bounded_retry_succeeds(self) -> None:
        linked = {
            "linked": True,
            "source": "hermes",
            "session_id": "child-session",
            "parent_session_id": "parent-session",
        }
        client = FakeClient(
            responses=[
                RuntimeError("initial lineage failure"),
                RuntimeError("retry lineage failure"),
                {"stored": True},
                {"stored": True},
                linked,
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )

        self.assertEqual(self.pending_lineage_head(provider)["attempts"], 1)
        provider.sync_turn("child fact one", "child answer one", session_id="child-session")

        self.assertEqual(self.pending_lineage_head(provider)["attempts"], 2)
        self.assertFalse(any(name == "process" for name, _ in client.calls))
        self.assertEqual(
            [name for name, _ in client.calls if name == "capture"],
            ["capture", "capture"],
        )

        provider.sync_turn("child fact two", "child answer two", session_id="child-session")

        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            [name for name, _ in client.calls if name == "process"],
            ["process"],
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                }
            ]
            * 3,
        )

    def test_failed_compression_lineage_stops_after_two_retries(self) -> None:
        client = FakeClient(
            responses=[
                RuntimeError("initial lineage failure"),
                RuntimeError("retry one failure"),
                {"stored": True},
                {"stored": True},
                RuntimeError("retry two failure"),
                {"stored": True},
                {"stored": True},
                {"stored": True},
                {"stored": True},
            ]
        )
        provider = self.lineage_provider(client)
        provider.on_session_switch("parent-session")
        provider.on_session_switch(
            "child-session",
            parent_session_id="parent-session",
            reset=False,
            reason="compression",
        )

        for index in range(3):
            provider.sync_turn(
                f"child fact {index}",
                f"child answer {index}",
                session_id="child-session",
            )

        self.assertEqual(
            len([name for name, _ in client.calls if name == "session_lineage"]),
            3,
        )
        self.assertFalse(any(name == "process" for name, _ in client.calls))
        self.assertEqual(self.pending_lineage_head(provider)["attempts"], 3)

    def test_lineage_queue_capacity_preserves_old_links_and_fails_closed(self) -> None:
        provider = self.lineage_provider(auto_process=False)
        for index in range(provider_module._MAX_SESSION_ALIASES):
            self.assertTrue(
                provider._remember_pending_lineage(
                    {
                        "source": "hermes",
                        "session_id": f"child-{index}",
                        "parent_session_id": f"parent-{index}",
                    },
                    1,
                )
            )
        oldest = dict(provider._pending_lineage[0])

        self.assertFalse(
            provider._remember_pending_lineage(
                {
                    "source": "hermes",
                    "session_id": "child-overflow",
                    "parent_session_id": "parent-overflow",
                },
                1,
            )
        )
        self.assertEqual(len(provider._pending_lineage), provider_module._MAX_SESSION_ALIASES)
        self.assertEqual(dict(provider._pending_lineage[0]), oldest)
        self.assertFalse(provider._retry_pending_lineage("child-overflow"))
        self.assertEqual(provider._client.calls, [])
