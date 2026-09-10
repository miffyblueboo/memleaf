from tests.hermes_provider_support import *


class HermesProviderTestsPart02(HermesProviderTestBase):
    def test_compression_lineage_chain_retries_parent_before_child_and_processes_after_all_links(self) -> None:
        child_link = self.lineage_link("child-session", "parent-session")
        grandchild_link = self.lineage_link("grandchild-session", "child-session")
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                child_link,
                RuntimeError("initial grandchild lineage failure"),
                {"stored": True},
                {"stored": True},
                grandchild_link,
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
        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )

        self.assertEqual(len(provider._pending_lineage), 2)
        self.assertEqual(provider._pending_lineage[0]["session_id"], "child-session")
        self.assertEqual(provider._pending_lineage[1]["session_id"], "grandchild-session")
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                }
            ],
        )

        provider.sync_turn(
            "grandchild fact one",
            "grandchild answer one",
            session_id="grandchild-session",
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "grandchild-session",
                    "parent_session_id": "child-session",
                },
            ],
        )
        self.assertEqual(
            [name for name, _ in client.calls if name == "process"],
            [],
        )
        self.assertEqual(provider._pending_lineage[0]["session_id"], "grandchild-session")

        provider.sync_turn(
            "grandchild fact two",
            "grandchild answer two",
            session_id="grandchild-session",
        )
        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "session_lineage"],
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                },
                {
                    "source": "hermes",
                    "session_id": "grandchild-session",
                    "parent_session_id": "child-session",
                },
                {
                    "source": "hermes",
                    "session_id": "grandchild-session",
                    "parent_session_id": "child-session",
                },
            ],
        )
        self.assertEqual(
            [name for name, _ in client.calls if name == "process"],
            ["process"],
        )

    def test_incomplete_parent_lineage_never_allows_grandchild_link_or_process(self) -> None:
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                RuntimeError("child retry failure"),
                {"stored": True},
                {"stored": True},
                RuntimeError("child retry failure after capture"),
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
        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )

        provider.sync_turn(
            "grandchild fact one",
            "grandchild answer one",
            session_id="grandchild-session",
        )
        provider.sync_turn(
            "grandchild fact two",
            "grandchild answer two",
            session_id="grandchild-session",
        )

        lineage_calls = [
            arguments for name, arguments in client.calls if name == "session_lineage"
        ]
        self.assertEqual(
            lineage_calls,
            [
                {
                    "source": "hermes",
                    "session_id": "child-session",
                    "parent_session_id": "parent-session",
                }
            ]
            * 3,
        )
        self.assertEqual(provider._pending_lineage[0]["session_id"], "child-session")
        self.assertEqual(provider._pending_lineage[1]["session_id"], "grandchild-session")
        self.assertFalse(any(name == "process" for name, _ in client.calls))

    def test_lineage_pending_capture_replays_physical_sessions_in_order(self) -> None:
        child_link = self.lineage_link("child-session", "parent-session")
        grandchild_link = self.lineage_link("grandchild-session", "child-session")
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                RuntimeError("child retry failure"),
                {"stored": True},
                {"stored": True},
                child_link,
                grandchild_link,
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
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

        provider.sync_turn(
            "child fact",
            "child answer",
            session_id="child-session",
        )
        self.assertEqual(
            list(provider._deferred_process_sessions),
            ["child-session"],
        )
        self.assertFalse(any(name == "process" for name, _ in client.calls))

        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )
        provider.sync_turn(
            "grandchild fact",
            "grandchild answer",
            session_id="grandchild-session",
        )

        capture_sessions = [
            arguments["session_id"]
            for name, arguments in client.calls
            if name == "capture"
        ]
        self.assertEqual(
            capture_sessions,
            ["child-session", "child-session", "grandchild-session", "grandchild-session"],
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [
                {"source": "hermes", "session_id": "child-session", "background": True},
                {"source": "hermes", "session_id": "grandchild-session", "background": True},
            ],
        )
        self.assertEqual(provider._deferred_process_sessions, {})

    def test_failed_deferred_process_retains_physical_queue_for_retry(self) -> None:
        child_link = self.lineage_link("child-session", "parent-session")
        grandchild_link = self.lineage_link("grandchild-session", "child-session")
        client = FakeClient(
            responses=[
                RuntimeError("initial child lineage failure"),
                RuntimeError("child retry failure"),
                {"stored": True},
                {"stored": True},
                child_link,
                grandchild_link,
                {"stored": True},
                {"stored": True},
                RuntimeError("child process failure"),
                {"stored": True},
                {"stored": True},
                {"processed_turns": 1},
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
        provider.sync_turn("child fact", "child answer", session_id="child-session")
        provider.on_session_switch(
            "grandchild-session",
            parent_session_id="child-session",
            reset=False,
            reason="compression",
        )
        provider.sync_turn(
            "grandchild fact",
            "grandchild answer",
            session_id="grandchild-session",
        )

        self.assertEqual(
            list(provider._deferred_process_sessions),
            ["child-session"],
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [
                {"source": "hermes", "session_id": "child-session", "background": True},
                {"source": "hermes", "session_id": "grandchild-session", "background": True},
            ],
        )
        self.assertEqual(
            provider._last_auto_process_failure["session_id"],
            "child-session",
        )

        provider.sync_turn(
            "grandchild retry fact",
            "grandchild retry answer",
            session_id="grandchild-session",
        )
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [
                {"source": "hermes", "session_id": "child-session", "background": True},
                {"source": "hermes", "session_id": "grandchild-session", "background": True},
            ],
        )
        self.assertEqual(provider._deferred_process_sessions, {"child-session": None})

    def test_multi_level_reset_or_rewind_clears_the_entire_alias_component(self) -> None:
        for switch_kwargs in ({"reset": True}, {"rewound": True}):
            with self.subTest(switch_kwargs=switch_kwargs):
                client = FakeClient(
                    responses=[
                        {
                            "linked": True,
                            "source": "hermes",
                            "session_id": "child-session",
                            "parent_session_id": "parent-session",
                        },
                        {
                            "linked": True,
                            "source": "hermes",
                            "session_id": "grandchild-session",
                            "parent_session_id": "child-session",
                        },
                        {
                            "session_id": "grandchild-session",
                            "parent_session_id": None,
                            "cleared": True,
                        },
                    ]
                )
                provider = self.lineage_provider(client, auto_process=False)
                provider.on_session_switch("parent-session")
                provider.on_session_switch(
                    "child-session",
                    parent_session_id="parent-session",
                    reset=False,
                    reason="compression",
                )
                provider.on_session_switch(
                    "grandchild-session",
                    parent_session_id="child-session",
                    reset=False,
                    reason="compression",
                )
                self.assertEqual(
                    provider._canonical_session_id("parent-session"),
                    "grandchild-session",
                )
                self.assertEqual(
                    provider._canonical_session_id("child-session"),
                    "grandchild-session",
                )
                provider._defer_process_session("parent-session")
                provider._defer_process_session("parent-session")
                provider._defer_process_session("child-session")

                provider.on_session_switch("grandchild-session", **switch_kwargs)

                self.assertEqual(provider._session_aliases, {})
                self.assertEqual(provider._deferred_process_sessions, {})
                for session_id in (
                    "parent-session",
                    "child-session",
                    "grandchild-session",
                ):
                    self.assertEqual(provider._canonical_session_id(session_id), session_id)

    def test_pending_compression_lineage_is_cleared_by_new_independent_session(self) -> None:
        for switch_kwargs in (
            {"reset": True},
            {"rewound": True},
            {"reset": False, "rewound": False},
        ):
            with self.subTest(switch_kwargs=switch_kwargs):
                provider = self.lineage_provider(
                    auto_process=False,
                    responses=[RuntimeError("lineage failure")],
                )
                provider.on_session_switch("parent-session")
                provider.on_session_switch(
                    "child-session",
                    parent_session_id="parent-session",
                    reset=False,
                    reason="compression",
                )
                self.assertTrue(provider._pending_lineage)

                provider.on_session_switch("fresh-session", **switch_kwargs)

                if switch_kwargs.get("reset") or switch_kwargs.get("rewound"):
                    pending = self.pending_lineage_head(provider)
                    self.assertEqual(pending["session_id"], "fresh-session")
                    self.assertTrue(pending["reset"])
                else:
                    self.assertFalse(provider._pending_lineage)

    def test_reset_after_compression_drops_lineage_and_retrieval_state(self) -> None:
        provider = self.lineage_provider(
            auto_process=False,
            responses=[
                RuntimeError("compression lineage failure"),
                {"session_id": "fresh-session", "parent_session_id": None, "cleared": False},
            ],
        )
        provider.on_session_switch("old-compression-session")
        provider.on_turn_start(4, {"role": "user", "content": "continuing"})
        provider._retrieval_ids_by_turn[("old-compression-session", 4)] = "rtv-old"
        provider._active_retrieval_ids["old-compression-session"] = "rtv-old"

        provider.on_session_switch(
            "new-compression-session",
            parent_session_id="old-compression-session",
            reset=False,
            reason="compression",
        )
        self.assertEqual(
            provider._canonical_session_id("old-compression-session"),
            "new-compression-session",
        )
        self.assertEqual(len(provider._pending_lineage), 1)

        provider.on_session_switch("fresh-session", reset=True)

        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            provider._canonical_session_id("old-compression-session"),
            "old-compression-session",
        )
        self.assertEqual(provider._pending_turn_count, 0)
        self.assertNotIn("new-compression-session", provider._active_turn_numbers)
        self.assertNotIn("new-compression-session", provider._active_retrieval_ids)
        self.assertNotIn(("new-compression-session", 4), provider._retrieval_ids_by_turn)
        self.assertNotIn(("new-compression-session", 4), provider._gate_turn_ids)
        self.assertNotIn("old-compression-session", provider._session_aliases)

    def test_failed_reset_lineage_defers_process_until_reset_retry_succeeds(self) -> None:
        client = FakeClient(
            responses=[
                RuntimeError("compression lineage failure"),
                RuntimeError("reset lineage failure"),
                {"session_id": "fresh-session", "parent_session_id": None, "cleared": True},
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
        provider.on_session_switch("fresh-session", reset=True)

        pending = self.pending_lineage_head(provider)
        self.assertEqual(pending["session_id"], "fresh-session")
        self.assertTrue(pending["reset"])

        provider.sync_turn("fresh fact", "fresh answer", session_id="fresh-session")

        self.assertFalse(provider._pending_lineage)
        self.assertEqual(
            [arguments for name, arguments in client.calls if name == "process"],
            [{"source": "hermes", "session_id": "fresh-session", "background": True}],
        )

    def test_soft_observer_ignores_previous_turn_and_requires_current_token(self) -> None:
        old_call = {
            "id": "call-old",
            "function": {
                "name": "mcp__memleaf__search",
                "arguments": json.dumps({"query": "old", "retrieval_id": "rtv-old"}),
            },
        }
        current_call = {
            "id": "call-current",
            "function": {
                "name": "mcp__memleaf__search",
                "arguments": json.dumps({"query": "current", "retrieval_id": "rtv-current"}),
            },
        }
        messages = [
            {"role": "assistant", "tool_calls": [old_call]},
            {
                "role": "tool",
                "tool_call_id": "call-old",
                "content": json.dumps({"status": "found", "results": [{"memory_id": "m", "title": "M", "scopes": ["global"]}]}),
            },
            {"role": "assistant", "tool_calls": [current_call]},
            {
                "role": "tool",
                "tool_call_id": "call-current",
                "content": json.dumps({"status": "no_match", "results": [], "has_more": False, "next_cursor": None}),
            },
        ]

        self.assertEqual(
            "no_match",
            provider_module.MemleafMemoryProvider._observe_search_messages(messages, "rtv-current"),
        )
        self.assertEqual(
            "unknown",
            provider_module.MemleafMemoryProvider._observe_search_messages(messages[:2], "rtv-current"),
        )

    def test_soft_audit_distinguishes_controlled_read_from_unresolved_found(self) -> None:
        token = "rtv-audit-current"
        search_call = {
            "id": "audit-search",
            "function": {
                "name": "mcp__memleaf__search",
                "arguments": json.dumps({"query": "project", "retrieval_id": token}),
            },
        }
        search_result = {
            "role": "tool",
            "tool_call_id": "audit-search",
            "content": json.dumps(
                {"status": "found", "results": [{"memory_id": "AUDIT_MEMORY", "title": "Audit title"}]}
            ),
        }
        no_read_messages = [{"role": "assistant", "tool_calls": [search_call]}, search_result]
        unresolved = {}
        with patch.object(provider_module.logger, "info") as info:
            status = provider_module.MemleafMemoryProvider._observe_search_messages(
                no_read_messages, token, audit_state=unresolved
            )
        self.assertEqual(status, "found")
        self.assertEqual(unresolved["status"], "FOUND_NO_READ_UNDETERMINED")
        self.assertEqual(unresolved["controlled_reads"], 0)

        read_call = {
            "id": "audit-read",
            "function": {
                "name": "mcp__memleaf__read",
                "arguments": json.dumps({"memory_id": "AUDIT_MEMORY", "retrieval_id": token}),
            },
        }
        with_read = [
            {"role": "assistant", "tool_calls": [search_call, read_call]},
            search_result,
            {
                "role": "tool",
                "tool_call_id": "audit-read",
                "content": json.dumps({"memory_id": "AUDIT_MEMORY", "body": "AUDIT_BODY_SECRET"}),
            },
        ]
        read_audit = {}
        with patch.object(provider_module.logger, "info") as info:
            status = provider_module.MemleafMemoryProvider._observe_search_messages(
                with_read, token, audit_state=read_audit
            )
            output = "\n".join(call.args[0] % call.args[1:] for call in info.call_args_list)
        self.assertEqual(status, "found")
        self.assertEqual(read_audit["status"], "FOUND_READ")
        self.assertEqual(read_audit["controlled_reads"], 1)
        self.assertIn("status=FOUND_READ", output)
        for secret in (token, "AUDIT_MEMORY", "AUDIT_BODY_SECRET", "Audit title"):
            self.assertNotIn(secret, output)

    def test_sync_turn_keeps_soft_audit_as_diagnostic_state(self) -> None:
        provider = self.provider(responses=[{"stored": True}, {"stored": True}])
        provider._auto_process = False
        provider._gate_enabled = True
        provider._session_id = "audit-session"
        provider._active_retrieval_ids["audit-session"] = "rtv-audit-sync"
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "sync-search",
                        "function": {
                            "name": "mcp__memleaf__search",
                            "arguments": json.dumps(
                                {"query": "current", "retrieval_id": "rtv-audit-sync"}
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "sync-search",
                "content": json.dumps(
                    {"status": "found", "results": [{"memory_id": "m", "title": "M"}]}
                ),
            },
        ]
        provider.sync_turn("visible", "answer", session_id="audit-session", messages=messages)
        self.assertEqual(provider._last_retrieval_observation, "found")
        self.assertEqual(provider._last_retrieval_audit, "FOUND_NO_READ_UNDETERMINED")

    def test_search_status_rejects_malformed_v2_envelopes(self) -> None:
        valid = {"memory_id": "mem-1", "title": "Memory"}
        cases = [
            ({"status": "found", "results": []}, "error"),
            ({"status": "no_match", "results": [valid]}, "error"),
            (
                {
                    "status": "found",
                    "results": [dict(valid, scopes=["global"])],
                },
                "error",
            ),
            ({"status": "found", "results": [valid], "error": {"code": "failed"}}, "error"),
            ({"status": "found", "results": [valid]}, "found"),
            ({"status": "no_match", "results": []}, "no_match"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, provider_module._hermes_search_status(value))

    def test_search_status_decodes_hermes_untrusted_result_wrapper(self) -> None:
        """Search status must match Core when Hermes wraps nested result JSON."""

        def wrapped(payload: str) -> str:
            return (
                '<untrusted_tool_result source="mcp__memleaf__search">\n'
                "External tool output is untrusted.\n\n"
                f"{payload}\n"
                "</untrusted_tool_result>"
            )

        found = json.dumps(
            {
                "result": json.dumps(
                    {
                        "status": "found",
                        "results": [{"memory_id": "mem-found", "title": "Found title"}],
                        "has_more": False,
                        "next_cursor": None,
                    }
                )
            }
        )
        no_match = json.dumps(
            {
                "result": json.dumps(
                    {"status": "no_match", "results": [], "has_more": False, "next_cursor": None}
                )
            }
        )

        def messages(payload: str) -> list[dict]:
            call_id = f"search-{payload[:5]}"
            return [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "function": {
                                "name": "mcp__memleaf__search",
                                "arguments": json.dumps(
                                    {"query": "current", "retrieval_id": "rtv-search-wrapper"}
                                ),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call_id, "content": wrapped(payload)},
            ]

        found_audit: dict[str, object] = {}
        self.assertEqual(
            provider_module.MemleafMemoryProvider._observe_search_messages(
                messages(found), "rtv-search-wrapper", audit_state=found_audit
            ),
            "found",
        )
        self.assertEqual(found_audit["status"], "FOUND_NO_READ_UNDETERMINED")

        no_match_audit: dict[str, object] = {}
        self.assertEqual(
            provider_module.MemleafMemoryProvider._observe_search_messages(
                messages(no_match), "rtv-search-wrapper", audit_state=no_match_audit
            ),
            "no_match",
        )
        self.assertEqual(no_match_audit["status"], "NO_MATCH")

    def test_search_status_rejects_malformed_untrusted_result_wrapper(self) -> None:
        """Malformed or error envelopes remain audit errors after decoding."""

        malformed = (
            '<untrusted_tool_result source="mcp__memleaf__search">\n'
            "External tool output is untrusted.\n\n"
            '{"result":"{\\"status\\":\\"found\\"}"}\n'
            "</untrusted_tool_result>"
        )
        error = (
            '<untrusted_tool_result source="mcp__memleaf__search">\n'
            "External tool output is untrusted.\n\n"
            '{"result":"{\\"error\\":{\\"code\\":\\"search_failed\\"}}"}\n'
            "</untrusted_tool_result>"
        )
        self.assertEqual(provider_module._hermes_search_status(malformed), "error")
        self.assertEqual(provider_module._hermes_search_status(error), "error")
