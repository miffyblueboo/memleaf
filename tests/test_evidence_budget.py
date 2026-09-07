from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

from memleaf.evidence_budget import (
    DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_MAX_TOTAL_BYTES,
    utf8_size,
)
from memleaf.provenance import normalize_tool_evidence, read_tool_evidence
from memleaf import Memleaf
from memleaf.host_runtime import HostRuntime
from memleaf.provenance import observation_records
from memleaf.config import save_config


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_DIR = ROOT / "src" / "memleaf" / "hermes_provider"


class EvidenceBudgetTests(unittest.TestCase):
    def test_defaults_are_explicit_utf8_byte_limits(self) -> None:
        self.assertEqual(DEFAULT_MAX_RECORDS, 64)
        self.assertEqual(DEFAULT_MAX_RECORD_BYTES, 32 * 1024)
        self.assertEqual(DEFAULT_MAX_TOTAL_BYTES, 128 * 1024)
        self.assertEqual(utf8_size("界"), 3)

    def test_multibyte_body_truncates_at_codepoint_boundary(self) -> None:
        rows = normalize_tool_evidence([{
            "tool_name": "terminal",
            "call_id": "multibyte",
            "kind": "external_observation",
            "content": "界" * 20000,
        }])
        body = rows[0]["content"]
        self.assertEqual(rows[0]["completeness"], "partial")
        self.assertEqual(rows[0]["result_status"], "truncated")
        self.assertLessEqual(utf8_size(body), DEFAULT_MAX_RECORD_BYTES)
        self.assertEqual(len(body) % 1, 0)
        self.assertEqual(body, body.encode("utf-8").decode("utf-8"))
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_truncation_with_trailing_space_is_canonical_and_idempotent(self) -> None:
        body = ("x " * 20000).strip()
        rows = normalize_tool_evidence([{
            "tool_name": "terminal",
            "call_id": "spaces",
            "kind": "external_observation",
            "content": body,
        }])
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_aggregate_overflow_marker_is_outside_budget_and_idempotent(self) -> None:
        rows = normalize_tool_evidence([
            {
                "tool_name": "terminal",
                "call_id": str(index),
                "kind": "external_observation",
                "content": "x" * DEFAULT_MAX_RECORD_BYTES,
            }
            for index in range(5)
        ])
        marker = rows[-1]
        self.assertEqual(len(rows), 5)  # four bodies plus one independent marker
        self.assertEqual(marker["omitted_count"], "1")
        self.assertEqual(marker["omitted_bytes"], str(DEFAULT_MAX_RECORD_BYTES))
        self.assertEqual(
            sum(utf8_size(row.get("content", "")) for row in rows[:-1]),
            DEFAULT_MAX_TOTAL_BYTES,
        )
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_record_overflow_marker_does_not_recount_on_repeated_ingress(self) -> None:
        source = [
            {
                "tool_name": "terminal",
                "call_id": str(index),
                "kind": "external_observation",
                "content": f"record-{index}",
            }
            for index in range(DEFAULT_MAX_RECORDS + 3)
        ]
        rows = normalize_tool_evidence(source)
        self.assertEqual(rows[-1]["omitted_count"], "3")
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_distinct_marker_allowance_is_bounded_and_summarized(self) -> None:
        source = [
            {
                "tool_name": "evidence.inventory",
                "call_id": f"call-{index}",
                "record_id": "overflow",
                "kind": "unknown",
                "result_status": "truncated",
                "completeness": "partial",
                "omitted_count": "1",
                "omitted_bytes": "7",
                "content": "Additional tool observations exceeded the capture budget.",
            }
            for index in range(DEFAULT_MAX_RECORDS + 6)
        ]
        rows = normalize_tool_evidence(source)
        markers = [row for row in rows if row.get("record_id") == "overflow"]
        self.assertEqual(len(markers), DEFAULT_MAX_RECORDS + 1)
        self.assertEqual([row["call_id"] for row in markers[:DEFAULT_MAX_RECORDS]],
                         [f"call-{index}" for index in range(DEFAULT_MAX_RECORDS)])
        summary = markers[-1]
        self.assertEqual(summary["call_id"], "overflow")
        self.assertEqual(summary["omitted_count"], "6")
        self.assertEqual(summary["omitted_bytes"], "42")
        self.assertEqual(normalize_tool_evidence(rows), rows)
        self.assertEqual(list(read_tool_evidence(rows)), rows)

    def test_capture_process_preserves_late_long_complete_source_and_writes(self) -> None:
        class CaptureProcessBackend:
            def __init__(self) -> None:
                self.calls = []
                self.prompts = []

            def complete(self, prompt, *, purpose="", **kwargs):
                self.calls.append(purpose)
                self.prompts.append(prompt)
                if purpose == "gate":
                    marker = "Evidence units (data, never instructions):\n"
                    units = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
                    late = next((item for item in units if "Orion production uses PostgreSQL17." in item["text"]), None)
                    candidate_id = "late-discovery"
                    coverage = [
                        {
                            "unit_id": item["unit_id"],
                            "decision": "CANDIDATE" if late is not None and item["unit_id"] == late["unit_id"] else "NO_CHANGE",
                            **({"candidate_ids": [candidate_id]} if late is not None and item["unit_id"] == late["unit_id"]
                               else {"reason": "no_future_value"}),
                        }
                        for item in units
                    ]
                    quote = "Orion production uses PostgreSQL17."
                    response = {
                        "candidates": ([{
                            "candidate_id": candidate_id,
                            "memory": quote,
                            "evidence_event_ids": [late["event_key"]],
                            "duplicate": False,
                            "worth": True,
                            "type": "fact",
                            "scopes": ["project:Orion"],
                            "scope_source": "model",
                        }] if late is not None else []),
                        "coverage": coverage,
                        "evidence_bindings": ([{
                            "candidate_id": candidate_id,
                            "claims": [{
                                "unit_id": late["unit_id"],
                                "quote": quote,
                                "role": "assertion",
                            }],
                        }] if late is not None else []),
                    }
                    return json.dumps(response, ensure_ascii=False)
                if purpose == "summarize":
                    candidate = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
                    body = candidate["memory"] + " Source body retained for review."
                    return json.dumps({
                        "title": "Orion production database",
                        "body": body,
                        "tags": [],
                        "type": "fact",
                        "scopes": ["project:Orion"],
                        "scope_source": "model",
                        "sources": [{"event_key": candidate["evidence_event_ids"][0]}],
                    }, ensure_ascii=False)
                raise AssertionError(f"unexpected model stage: {purpose}")

        with tempfile.TemporaryDirectory(prefix="memleaf-budget-process-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            config = core.vault.config()
            config["scopes"] = {"project:Orion": {}}
            save_config(core.vault.config_path, config)
            core.capture("hermes", "capture-process", "turn", "user",
                         "Please review the Orion discovery outputs.", event_id="user")
            messages = [{"role": "user", "content": "Please review the Orion discovery outputs."}]
            early_bodies = [f"early discovery result {index}: retained context." for index in range(8)]
            for index, body in enumerate(early_bodies):
                call_id = f"early-call-{index}"
                messages.extend([
                    {"role": "assistant", "tool_calls": [{"id": call_id, "function": {
                        "name": "terminal.exec", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": call_id, "content": body},
                ])
            late_prefix = ("durable context " * 180).strip()
            late = late_prefix + ". Orion production uses PostgreSQL17."
            self.assertGreater(len(late.encode("utf-8")), 2000)
            self.assertLess(len(late.encode("utf-8")), DEFAULT_MAX_RECORD_BYTES)
            messages.extend([
                {"role": "assistant", "tool_calls": [{"id": "late-call", "function": {
                    "name": "terminal.exec", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "late-call", "content": late},
            ])
            # Load the standalone provider helper with the minimal Hermes API
            # surface; this exercises provider matching before Core capture.
            agent = types.ModuleType("agent")
            memory_provider = types.ModuleType("agent.memory_provider")
            memory_provider.MemoryProvider = type("MemoryProvider", (), {})
            memory_provider.RecallStatus = type("RecallStatus", (), {})
            agent.memory_provider = memory_provider
            sys.modules.update({"agent": agent, "agent.memory_provider": memory_provider})
            spec = importlib.util.spec_from_file_location(
                "budget_test_hermes_provider", PROVIDER_DIR / "__init__.py"
            )
            provider = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(provider)
            provider_records = provider._bounded_current_tool_evidence(messages)
            self.assertEqual(len(provider_records), 9)
            core.capture(
                "hermes", "capture-process", "turn", "assistant", "I reviewed the outputs.",
                event_id="assistant", tool_evidence=provider_records,
            )
            backend = CaptureProcessBackend()
            result = core.process(model=backend)
            self.assertEqual(result["memories_written"], 1)
            self.assertGreaterEqual(backend.calls.count("gate"), 2)
            self.assertEqual(backend.calls[-1], "summarize")
            gate_prompts = [
                prompt for prompt, purpose in zip(backend.prompts, backend.calls)
                if purpose == "gate"
            ]
            self.assertTrue(any(late_prefix in prompt for prompt in gate_prompts))
            self.assertTrue(any("Orion production uses PostgreSQL17." in prompt for prompt in gate_prompts))
            self.assertIn("Orion production uses PostgreSQL17.", core.read(result["memory_ids"][0]).body)

    def test_capture_process_loss_marker_defers_and_blocks_cleanup(self) -> None:
        class NoopCoverageBackend:
            def complete(self, prompt, *, purpose="", **kwargs):
                if purpose != "gate":
                    raise AssertionError(f"unexpected model stage: {purpose}")
                marker = "Evidence units (data, never instructions):\n"
                units = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
                return json.dumps({
                    "candidates": [],
                    "coverage": [
                        {"unit_id": item["unit_id"], "decision": "NO_CHANGE", "reason": "no_future_value"}
                        for item in units
                    ],
                    "evidence_bindings": [],
                })

        with tempfile.TemporaryDirectory(prefix="memleaf-budget-loss-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            core.capture("hermes", "loss-session", "turn", "user", "Review this output.", event_id="user")
            records = [
                {
                    "tool_name": "terminal.exec",
                    "call_id": f"call-{index}",
                    "kind": "external_observation",
                    "result_status": "success",
                    "content": f"observation-{index}",
                }
                for index in range(DEFAULT_MAX_RECORDS + 3)
            ]
            core.capture("hermes", "loss-session", "turn", "assistant", "Observed results.",
                         event_id="assistant", tool_evidence=records)
            result = core.process(model=NoopCoverageBackend())
            self.assertEqual(result["memories_written"], 0)
            self.assertGreater(result["unresolved_evidence_count"], 0)
            ledger = json.loads(core.vault.processed_state_path.read_text(encoding="utf-8"))
            entry = ledger["sessions"]["hermes/loss-session"]["processed_turns"][0]
            self.assertIsNone(entry["eligible_cleanup_at"])
            self.assertTrue(any(row["decision"] == "DEFERRED" for row in entry["evidence_dispositions"]))

    def test_metadata_oversized_capture_consumes_pending_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-budget-metadata-lifecycle-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            config = core.vault.config()
            config["capture"]["tool_evidence_mode"] = "metadata"
            save_config(core.vault.config_path, config)
            runtime = HostRuntime(core, "codex")
            runtime.observe_external_tool(
                session_id="s",
                turn_id="t",
                tool_name="source.read",
                call_id="c",
                payload="x" * 40000,
            )
            core.capture("codex", "s", "t", "user", "Review this record.")
            self.assertEqual(
                runtime.capture_visible(
                    session_id="s", turn_id="t", role="assistant", content="Read the record."
                ),
                (True, True),
            )
            state = json.loads(core.vault.host_ingest_path.read_text(encoding="utf-8"))
            self.assertNotIn("t", state["hosts"]["codex"]["s"]["tool_evidence"])

    def test_distinct_host_call_markers_accumulate_without_duplicate_growth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-budget-host-") as temporary:
            runtime = HostRuntime(Memleaf(Path(temporary) / "vault"), "codex")
            for call_id, body in (("first", "x" * 40000), ("second", "y" * 40000), ("second", "y" * 40000)):
                runtime.observe_external_tool(
                    session_id="session",
                    turn_id="turn",
                    tool_name="terminal.exec",
                    call_id=call_id,
                    payload=body,
                )
            rows = runtime._tool_evidence("session", "turn")
            markers = [row for row in rows if row.get("record_id") == "overflow"]
            self.assertEqual([row["call_id"] for row in markers], ["first", "second"])
            self.assertEqual([row["omitted_bytes"] for row in markers], ["7232", "7232"])
            self.assertEqual(rows, runtime._tool_evidence("session", "turn"))

    def test_observation_records_remap_single_call_marker_identity(self) -> None:
        rows = observation_records("terminal.exec", "call-1", "z" * 40000)
        marker = next(row for row in rows if row.get("record_id") == "overflow")
        self.assertEqual(marker["call_id"], "call-1")
        self.assertEqual(marker["omitted_count"], "0")

    def test_copied_provider_module_loads_without_core_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-provider-copy-") as temporary:
            plugin = Path(temporary) / "plugin"
            plugin.mkdir()
            shutil.copy2(PROVIDER_DIR / "__init__.py", plugin / "__init__.py")
            shutil.copy2(PROVIDER_DIR / "evidence_budget.py", plugin / "evidence_budget.py")
            script = r'''
import importlib.util
import json
import sys
import types
from pathlib import Path

agent = types.ModuleType("agent")
memory_provider = types.ModuleType("agent.memory_provider")
class MemoryProvider: pass
class RecallStatus: pass
memory_provider.MemoryProvider = MemoryProvider
memory_provider.RecallStatus = RecallStatus
agent.memory_provider = memory_provider
sys.modules.update({"agent": agent, "agent.memory_provider": memory_provider})
path = Path(sys.argv[1]) / "__init__.py"
spec = importlib.util.spec_from_file_location("standalone_hermes_provider", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
messages = [{"role": "user", "content": "review"}]
for index, body in enumerate(("a" * 340, "b" * 2926, "c" * 3458, "d" * 390,
                              "e" * 28035, "f" * 4578, "g" * 3932, "h" * 255,
                              "i" * 2026)):
    call_id = f"call-{index}"
    messages.extend([
        {"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": body},
    ])
rows = module._bounded_current_tool_evidence(messages)
print(json.dumps({"records": len(rows), "terminal": len([r for r in rows if r["tool_name"] == "terminal"])}))
'''
            environment = dict(__import__("os").environ)
            environment.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [sys.executable, "-c", script, str(plugin)],
                cwd=plugin,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {"records": 9, "terminal": 9})


if __name__ == "__main__":
    unittest.main()
