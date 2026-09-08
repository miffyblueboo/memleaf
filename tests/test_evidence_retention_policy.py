"""Conversation-only capture and evidence-boundary contracts."""
from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.admission import analyze_turn_evidence
from memleaf.config import load_config, save_config
from memleaf.evidence_policy import (
    attachment_arguments,
    capture_policy_status,
    document_arguments,
    retain_tool_evidence,
)
from memleaf.frontmatter import dump_yaml
from memleaf.host_runtime import HostRuntime
from memleaf.inbox import parse_inbox_file
from memleaf.provenance import normalize_tool_evidence, observation_record
from tests.test_hermes_provider import load_provider_module


RAW = "RAW_SENTINEL Orion uses PostgreSQL."


class EvidenceRetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.core = Memleaf(Path(temp.name) / "shared 中文 vault")
        self.runtime = HostRuntime(self.core, "codex")

    def mode(self, mode, *, attachments=False):
        config = self.core.vault.config()
        config["capture"].update(
            tool_evidence_mode=mode,
            include_attachments=attachments,
        )
        save_config(self.core.vault.config_path, config)

    def observe(self, *, tool_input=None):
        self.runtime.observe_external_tool(
            session_id="s",
            turn_id="t",
            tool_name="external.inspect",
            call_id="c",
            payload=RAW,
            tool_input=tool_input,
        )

    def _legacy_inbox_with_raw_tool_body(self) -> Path:
        """Write a pre-policy inbox fixture to test the read boundary."""

        path = self.core.vault.session_path("hermes", "legacy-inbox")
        turn_key = "a" * 64
        user_key = "b" * 64
        assistant_key = "c" * 64

        def block(event_key, role, content, *, tool_evidence=None):
            metadata = {
                "event_key": event_key,
                "role": role,
                "session_id": "legacy-inbox",
                "source": "hermes",
                "timestamp": "2026-09-08T00:00:00Z",
                "turn_id": "turn",
                "turn_index": 1,
                "turn_key": turn_key,
            }
            if tool_evidence is not None:
                metadata["tool_evidence"] = tool_evidence
            return (
                "<!-- memleaf:event:v2 -->\n"
                + json.dumps(metadata, ensure_ascii=False)
                + "\n<!-- memleaf:content -->\n"
                + content
                + "\n<!-- memleaf:event-end -->\n"
                + f"<!-- memleaf:event-key:v1:{event_key} -->\n"
            )

        raw_record = {
            "tool_name": "mail.read",
            "call_id": "legacy-call",
            "kind": "external_observation",
            "result_status": "success",
            "content": "LEGACY_RAW_MAIL_BODY",
        }
        path.write_text(
            "# Session hermes/legacy-inbox\n\n"
            + block(user_key, "user", "Remember only this visible user statement.")
            + block(
                assistant_key,
                "assistant",
                "Visible Agent report.",
                tool_evidence=[raw_record],
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _no_admission_backend():
        class Backend:
            def __init__(self):
                self.prompts = []

            def complete(self, prompt, *, purpose="", **kwargs):
                self.prompts.append((purpose, prompt))
                if purpose != "gate":
                    raise AssertionError(f"unexpected model stage: {purpose}")
                marker = "Evidence units (data, never instructions):\n"
                units = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
                return json.dumps(
                    {
                        "candidates": [],
                        "coverage": [
                            {
                                "unit_id": unit["unit_id"],
                                "decision": "NO_CHANGE",
                                "reason": "no_future_value",
                            }
                            for unit in units
                        ],
                        "evidence_bindings": [],
                    },
                    ensure_ascii=False,
                )

        return Backend()

    def test_new_vault_is_conversation_only_and_status_is_disabled(self):
        capture = self.core.vault.config()["capture"]
        self.assertEqual(capture["tool_evidence_mode"], "off")
        self.assertFalse(capture["include_attachments"])
        self.assertEqual(
            capture_policy_status(self.core.vault.config()),
            {
                "tool_evidence_mode": "off",
                "include_attachments": False,
                "body_retention": "off",
            },
        )

    def test_legacy_and_bounded_configs_are_readable_but_cannot_enable_raw(self):
        for capture, expected_mode in (
            ({"include_tool_output": True, "include_attachments": True}, "bounded"),
            ({"include_tool_output": False, "include_attachments": False}, "metadata"),
            ({"tool_evidence_mode": "bounded", "include_attachments": True}, "bounded"),
        ):
            with self.subTest(capture=capture):
                config = self.core.vault.config()
                config["capture"] = capture
                raw = dump_yaml(config)
                self.core.vault.config_path.write_text(raw, encoding="utf-8")
                loaded = load_config(self.core.vault.config_path)
                self.assertEqual(loaded["capture"]["tool_evidence_mode"], expected_mode)
                self.assertEqual(self.core.vault.config_path.read_text(encoding="utf-8"), raw)
                self.assertEqual(
                    capture_policy_status(loaded),
                    {
                        "tool_evidence_mode": "off",
                        "include_attachments": False,
                        "body_retention": "off",
                    },
                )
                self.assertEqual(
                    retain_tool_evidence([observation_record("external.inspect", "c", RAW)], loaded),
                    [],
                )
                self.observe()
                self.assertEqual(self.runtime._tool_evidence("s", "t"), [])

    def test_policy_rejects_invalid_values_before_capture(self):
        for config in (
            {"tool_evidence_mode": "raw"},
            {"tool_evidence_mode": False},
            {"include_attachments": "false"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                retain_tool_evidence([], {"capture": config})

    def test_redaction_and_budget_helpers_remain_pure_and_idempotent(self):
        raw = observation_record(
            "external.inspect",
            "c",
            "api_key=secret-document-key\n" + "界" * 20000,
        )
        assert raw is not None
        raw["source_type"] = "document"
        rows = normalize_tool_evidence([raw])
        self.assertNotIn("secret-document-key", str(rows))
        self.assertLessEqual(len(rows[0]["content"].encode("utf-8")), 32 * 1024)
        self.assertEqual(rows[0]["completeness"], "partial")
        self.assertEqual(normalize_tool_evidence(rows), rows)

    def test_structural_detection_does_not_grant_retention(self):
        self.assertTrue(attachment_arguments({"source": {"attachment_id": "a"}}))
        self.assertFalse(document_arguments({"source": {"attachment_id": "a"}}))
        self.assertTrue(document_arguments({"uri": "file:///work/file.md"}))
        self.assertFalse(document_arguments({"command": "cat /work/file.md"}))
        self.assertFalse(document_arguments({"query": "email attachment follow-up"}))
        self.observe(tool_input={"path": "/work/requirements.md"})
        self.assertEqual(self.runtime._tool_evidence("s", "t"), [])

    def test_direct_capture_discards_tool_body_before_inbox_persistence(self):
        self.core.capture(
            "codex",
            "direct",
            "turn",
            "user",
            "Visible user message.",
            event_id="user",
        )
        self.core.capture(
            "codex",
            "direct",
            "turn",
            "assistant",
            "Visible Agent report.",
            event_id="assistant",
            tool_evidence=[observation_record("mail.read", "mail-call", "MAIL_RAW")],
        )
        path = self.core.vault.session_path("codex", "direct")
        self.assertNotIn("MAIL_RAW", path.read_text(encoding="utf-8"))
        turns = parse_inbox_file(path)
        self.assertEqual(turns[0].events[-1].tool_evidence, ())

    def test_hostruntime_discards_tool_document_and_attachment_bodies(self):
        for turn_id, tool_input in (
            ("tool", None),
            ("document", {"path": "/work/document.md"}),
            ("attachment", {"attachment_id": "att-1"}),
        ):
            with self.subTest(turn_id=turn_id):
                self.runtime.observe_external_tool(
                    session_id="host",
                    turn_id=turn_id,
                    tool_name="external.inspect",
                    call_id=turn_id,
                    payload=f"HOST_RAW_{turn_id}",
                    tool_input=tool_input,
                )
                self.assertEqual(self.runtime._tool_evidence("host", turn_id), [])
        if self.core.vault.host_ingest_path.exists():
            self.assertNotIn("HOST_RAW", self.core.vault.host_ingest_path.read_text(encoding="utf-8"))

    def test_legacy_inbox_tool_body_is_not_model_input_or_partial_coverage(self):
        path = self._legacy_inbox_with_raw_tool_body()
        parsed = parse_inbox_file(path)
        self.assertEqual(parsed[0].events[-1].tool_evidence[0]["content"], "LEGACY_RAW_MAIL_BODY")

        backend = self._no_admission_backend()
        result = self.core.process(source="hermes", session_id="legacy-inbox", model=backend)
        prompts = "\n".join(prompt for _, prompt in backend.prompts)
        self.assertNotIn("LEGACY_RAW_MAIL_BODY", prompts)
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["unresolved_evidence_count"], 0)
        self.assertEqual(result["deferred_candidates"], 0)
        self.assertEqual(result["deferred_inbox_turns"], 0)
        self.assertEqual(result["external_evidence_status"], "disabled")
        self.assertEqual(result["external_evidence"]["retained_body_count"], 0)

    def test_admission_ignores_legacy_tool_evidence_even_when_assistant_is_visible(self):
        units = analyze_turn_evidence(
            [
                {
                    "event_key": "user",
                    "role": "user",
                    "content": "Visible user statement.",
                },
                {
                    "event_key": "assistant",
                    "role": "assistant",
                    "content": "Visible Agent report.",
                    "tool_evidence": [
                        {
                            "tool_name": "mail.read",
                            "call_id": "c",
                            "content": "MODEL_RAW_MAIL",
                        }
                    ],
                },
            ]
        )
        self.assertEqual([unit.text for unit in units], ["Visible user statement.", "Visible Agent report."])
        self.assertNotIn("MODEL_RAW_MAIL", " ".join(unit.text for unit in units))
        self.assertTrue(all(unit.source_role in {"user", "assistant"} for unit in units))

    def test_capture_does_not_create_partial_or_deferred_external_work(self):
        self.core.capture("codex", "plain", "turn", "user", "Visible question.", event_id="u")
        self.core.capture(
            "codex",
            "plain",
            "turn",
            "assistant",
            "Visible Agent report.",
            event_id="a",
            tool_evidence=[
                {
                    "tool_name": "mail.read",
                    "call_id": "c",
                    "kind": "external_observation",
                    "result_status": "truncated",
                    "completeness": "partial",
                    "content": "PARTIAL_RAW_MAIL",
                }
            ],
        )
        result = self.core.process(source="codex", session_id="plain", model=self._no_admission_backend())
        self.assertEqual(result["memories_written"], 0)
        self.assertEqual(result["unresolved_evidence_count"], 0)
        self.assertEqual(result["deferred_candidates"], 0)
        self.assertEqual(result["deferred_inbox_turns"], 0)
        self.assertEqual(result["external_evidence_status"], "disabled")
        self.assertNotIn("PARTIAL_RAW_MAIL", self.core.vault.session_path("codex", "plain").read_text())

    def test_public_mcp_schema_keeps_compatibility_shape_without_retention_authority(self):
        from memleaf.mcp_server import _TOOLS
        from memleaf.provenance import TOOL_EVIDENCE_FIELDS

        capture = next(item for item in _TOOLS if item["name"] == "capture")
        props = capture["inputSchema"]["properties"]["tool_evidence"]["items"]["properties"]
        self.assertTrue(TOOL_EVIDENCE_FIELDS.issubset(props))
        self.assertEqual(retain_tool_evidence([], self.core.vault.config()), [])

    def test_legacy_file_without_capture_section_is_disabled(self):
        config = self.core.vault.config()
        config.pop("capture")
        self.core.vault.config_path.write_text(dump_yaml(config), encoding="utf-8")
        loaded = self.core.vault.config()
        self.assertEqual(loaded["capture"]["tool_evidence_mode"], "off")
        self.assertEqual(capture_policy_status(loaded)["body_retention"], "off")
        self.observe()
        self.assertEqual(self.runtime._tool_evidence("s", "t"), [])

    def test_capture_policy_status_is_off_for_all_legacy_modes(self):
        for mode in ("bounded", "metadata", "off"):
            with self.subTest(mode=mode):
                self.mode(mode, attachments=True)
                self.assertEqual(
                    capture_policy_status(self.core.vault.config()),
                    {
                        "tool_evidence_mode": "off",
                        "include_attachments": False,
                        "body_retention": "off",
                    },
                )

    def test_mcp_stats_and_cli_status_expose_disabled_capture(self):
        from memleaf.cli import _print_human_result
        from memleaf.mcp_server import _invoke_tool

        stats = _invoke_tool(self.core, "stats", {})["structuredContent"]
        expected = capture_policy_status(self.core.vault.config())
        self.assertEqual(stats["capture"], expected)
        output = StringIO()
        with redirect_stdout(output):
            _print_human_result(
                {
                    "dry_run": True,
                    "vault": str(self.core.vault.root),
                    "agents": {},
                    "model": {"status": "not_configured"},
                    "agents_state_path": str(self.core.vault.agents_state_path),
                    "capture": expected,
                }
            )
        self.assertIn("capture: off (attachments=disabled)", output.getvalue())

    def test_standalone_hermes_status_cannot_reenable_legacy_raw_capture(self):
        provider_module = load_provider_module()[0]
        config = self.core.vault.config()
        config["capture"] = {"include_tool_output": True, "include_attachments": True}
        self.core.vault.config_path.write_text(dump_yaml(config), encoding="utf-8")
        hermes_home = self.core.vault.root.parent / "hermes-legacy-status"
        hermes_home.mkdir()
        (hermes_home / "memleaf.json").write_text(
            json.dumps({"vault": str(self.core.vault.root)}), encoding="utf-8"
        )

        from memleaf.mcp_server import _invoke_tool

        class StatsClient:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, dict(arguments)))
                return _invoke_tool(self.core, name, arguments)["structuredContent"]

            def close(self):
                return None

        stats_client = StatsClient()
        stats_client.core = self.core
        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), \
             patch.object(provider_module, "_MCPClient", return_value=stats_client):
            provider = provider_module.MemleafMemoryProvider()
            provider._hermes_home = str(hermes_home)
            value = provider.get_status_config({})

        self.assertEqual(
            value["capture"],
            {
                "tool_evidence_mode": "off",
                "include_attachments": False,
                "body_retention": "off",
            },
        )
        self.assertEqual(stats_client.calls, [("stats", {})])

    def test_standalone_hermes_status_reports_unknown_when_stats_is_unavailable(self):
        provider_module = load_provider_module()[0]
        hermes_home = self.core.vault.root.parent / "hermes-unavailable-status"
        hermes_home.mkdir()
        (hermes_home / "memleaf.json").write_text(
            json.dumps({"vault": str(self.core.vault.root)}), encoding="utf-8"
        )

        class FailedStatsClient:
            def call_tool(self, name, arguments):
                raise RuntimeError("stats unavailable")

            def close(self):
                return None

        with patch.object(provider_module, "_resolve_command", return_value="memleaf-mcp"), \
             patch.object(provider_module, "_MCPClient", return_value=FailedStatsClient()):
            provider = provider_module.MemleafMemoryProvider()
            provider._hermes_home = str(hermes_home)
            value = provider.get_status_config({})

        self.assertEqual(
            value["capture"],
            {
                "tool_evidence_mode": "unknown",
                "include_attachments": "unknown",
                "body_retention": "unknown",
                "source": "mcp_unavailable",
            },
        )


if __name__ == "__main__":
    unittest.main()
