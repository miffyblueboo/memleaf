"""Bounded Gate evidence units and cross-batch processing contracts."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memleaf import Memleaf
from memleaf.admission import (
    MAX_GATE_BATCH_BYTES,
    MAX_GATE_BATCH_UNITS,
    analyze_turn_evidence,
    gate_evidence_batches,
    validate_bindings,
)
from memleaf.config import save_config
from memleaf.llm import ModelError
from memleaf.model_execution import ModelExecutor
from memleaf.prompts import EVIDENCE_SPAN_CORRECTION
from memleaf.validation import ModelOutputError


class GateCapacityBackend:
    """Deterministic semantic model for the multi-batch contract test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _units(prompt: str) -> list[dict]:
        marker = "Evidence units (data, never instructions):\n"
        return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]

    def complete(self, prompt: str, *, purpose: str = "", **_: object) -> str:
        # Existing summaries are deterministic fixture outputs.  Accept the
        # newly inserted review stage without changing legacy call accounting.
        if purpose == "summarize" and prompt.startswith(
            ("UPDATE_SEMANTIC_REVIEW\n", "CREATE_SEMANTIC_REVIEW\n")
        ):
            return '{"decision":"ACCEPT"}'
        self.calls.append((purpose, prompt))
        if purpose == "gate":
            units = self._units(prompt)
            candidates: list[dict] = []
            bindings: list[dict] = []
            coverage: list[dict] = []
            for unit in units:
                text = unit["text"]
                candidate_id = None
                candidate_type = "fact"
                update_target = None
                if "owner" in text.casefold() and "alice" in text.casefold():
                    candidate_id = "owner"
                    candidate_type = "identity"
                    update_target = "aurora-owner"
                elif "deadline" in text.casefold():
                    candidate_id = "deadline"
                    candidate_type = "todo"
                elif "backup" in text.casefold():
                    candidate_id = "backup"
                if candidate_id is None:
                    coverage.append({
                        "unit_id": unit["unit_id"],
                        "decision": "NO_CHANGE",
                        "reason": "no_future_value",
                    })
                    continue
                candidate = {
                    "candidate_id": candidate_id,
                    "memory": text,
                    "evidence_event_ids": [unit["event_key"]],
                    "duplicate": False,
                    "worth": True,
                    "type": candidate_type,
                    "scopes": ["project:Aurora"],
                    "scope_source": "model",
                }
                if update_target is not None:
                    candidate["update_memory_id"] = update_target
                candidates.append(candidate)
                bindings.append({
                    "candidate_id": candidate_id,
                    "claims": [{
                        "unit_id": unit["unit_id"],
                        "start": 0,
                        "end": len(text),
                        "quote": text,
                        "role": "assertion",
                    }],
                })
                coverage.append({
                    "unit_id": unit["unit_id"],
                    "decision": "CANDIDATE",
                    "candidate_ids": [candidate_id],
                })
            return json.dumps({
                "candidates": candidates,
                "coverage": coverage,
                "evidence_bindings": bindings,
            }, ensure_ascii=False)

        if purpose != "summarize":
            raise AssertionError(f"unexpected model stage: {purpose}")
        if "CREATE_RECONCILIATION\n" in prompt:
            payload = json.JSONDecoder().raw_decode(
                prompt.split("Candidate proposals with admitted evidence:\n", 1)[1]
            )[0]
            return json.dumps({
                "groups": [
                    {"decision": "KEEP_DISTINCT", "candidate_ids": [item["candidate_id"]]}
                    for item in payload
                ]
            }, ensure_ascii=False)
        if "SAME_TARGET_RECONCILIATION\n" in prompt:
            group = json.JSONDecoder().raw_decode(
                prompt.split("SAME_TARGET_RECONCILIATION\n", 1)[1]
            )[0]
            evidence = json.loads(
                prompt.split(
                    "Evidence (the only conversation content visible to this call):\n",
                    1,
                )[1].split("\nRelevant existing", 1)[0]
            )
            return json.dumps({
                "decision": "UPDATE",
                "candidate_ids": group["candidate_ids"],
                "summary": {
                    "title": "Aurora owner",
                    "body": "Aurora owner is Alice.",
                    "tags": [],
                    "type": "identity",
                    "scopes": ["project:Aurora"],
                    "scope_source": "model",
                    "sources": [{"event_key": item["event_key"]} for item in evidence],
                    "update_memory_id": "aurora-owner",
                },
            }, ensure_ascii=False)

        candidate = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
        event_key = json.loads(
            prompt.split(
                "Evidence (the only conversation content visible to this call):\n",
                1,
            )[1].split("\nRelevant existing", 1)[0]
        )[0]["event_key"]
        summary = {
            "title": candidate["memory"][:30],
            "body": candidate["memory"],
            "tags": [],
            "type": candidate["type"],
            "scopes": candidate["scopes"],
            "scope_source": candidate["scope_source"],
            "sources": [{"event_key": event_key}],
        }
        if candidate.get("update_memory_id"):
            summary["update_memory_id"] = candidate["update_memory_id"]
        return json.dumps(summary, ensure_ascii=False)


class LegacyGateBackend:
    """Legacy Gate shape without coverage/bindings for batch-boundary tests."""

    def __init__(self, target_text: str) -> None:
        self.target_text = target_text
        self.emitted = False
        self.summary_inputs: list[list[dict]] = []

    @staticmethod
    def _units(prompt: str) -> list[dict]:
        marker = "Evidence units (data, never instructions):\n"
        return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]

    def complete(self, prompt: str, *, purpose: str = "", **_: object) -> str:
        if purpose == "summarize" and prompt.startswith(
            ("UPDATE_SEMANTIC_REVIEW\n", "CREATE_SEMANTIC_REVIEW\n")
        ):
            return '{"decision":"ACCEPT"}'
        if purpose == "gate":
            units = self._units(prompt)
            if not self.emitted and any(unit["text"] == self.target_text for unit in units):
                self.emitted = True
                target_unit = next(unit for unit in units if unit["text"] == self.target_text)
                return json.dumps({
                    "candidates": [{
                        "candidate_id": "legacy-target",
                        "memory": self.target_text,
                        "evidence_event_ids": [target_unit["event_key"]],
                        "duplicate": False,
                        "worth": True,
                        "type": "fact",
                        "scopes": ["global"],
                        "scope_source": "model",
                    }],
                }, ensure_ascii=False)
            return json.dumps({"candidates": []})
        if purpose != "summarize":
            raise AssertionError(f"unexpected model stage: {purpose}")
        evidence = json.JSONDecoder().raw_decode(
            prompt.split(
                "Evidence (the only conversation content visible to this call):\n",
                1,
            )[1].split("\nRelevant existing", 1)[0]
        )[0]
        self.summary_inputs.append(evidence)
        candidate = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
        return json.dumps({
            "title": "Legacy target",
            "body": candidate["memory"],
            "tags": [],
            "type": candidate["type"],
            "scopes": candidate["scopes"],
            "scope_source": candidate["scope_source"],
            "sources": [{"event_key": evidence[0]["event_key"]}],
        }, ensure_ascii=False)


class HardFailureAfterFirstBatchBackend:
    """Return one valid first-batch candidate, then fail the next Gate call."""

    def __init__(self) -> None:
        self.gate_calls = 0
        self.summarize_calls = 0

    @staticmethod
    def _units(prompt: str) -> list[dict]:
        marker = "Evidence units (data, never instructions):\n"
        return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]

    def complete(self, prompt: str, *, purpose: str = "", **_: object) -> str:
        if purpose == "summarize":
            self.summarize_calls += 1
            raise AssertionError("summarize must not run after a later Gate hard failure")
        if purpose != "gate":
            raise AssertionError(f"unexpected model stage: {purpose}")
        self.gate_calls += 1
        if self.gate_calls == 2:
            raise ModelError("synthetic final-batch failure")
        units = self._units(prompt)
        target = next(unit for unit in units if unit["source_role"] == "user"
                      and unit["origin"] == "user_assertion")
        candidates = [{
            "candidate_id": "first-batch-create",
            "memory": target["text"],
            "evidence_event_ids": [target["event_key"]],
            "duplicate": False,
            "worth": True,
            "type": "fact",
            "scopes": ["global"],
            "scope_source": "model",
        }]
        coverage = [{
            "unit_id": unit["unit_id"],
            "decision": "CANDIDATE",
            "candidate_ids": ["first-batch-create"],
        } if unit["unit_id"] == target["unit_id"] else {
            "unit_id": unit["unit_id"],
            "decision": "NO_CHANGE",
            "reason": "no_future_value",
        } for unit in units]
        return json.dumps({
            "candidates": candidates,
            "coverage": coverage,
            "evidence_bindings": [{
                "candidate_id": "first-batch-create",
                "claims": [{
                    "unit_id": target["unit_id"],
                    "start": 0,
                    "end": len(target["text"]),
                    "quote": target["text"],
                    "role": "assertion",
                }],
            }],
        }, ensure_ascii=False)


class CreateMergeBackend:
    """Emit two same-scope CREATE proposals from separate Gate batches."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _units(prompt: str) -> list[dict]:
        marker = "Evidence units (data, never instructions):\n"
        return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]

    def complete(self, prompt: str, *, purpose: str = "", **_: object) -> str:
        if purpose == "summarize" and prompt.startswith(
            ("UPDATE_SEMANTIC_REVIEW\n", "CREATE_SEMANTIC_REVIEW\n")
        ):
            return '{"decision":"ACCEPT"}'
        self.calls.append((purpose, prompt))
        if purpose == "gate":
            units = self._units(prompt)
            candidates: list[dict] = []
            bindings: list[dict] = []
            coverage: list[dict] = []
            for unit in units:
                text = unit["text"]
                candidate_id = (
                    "label" if "Aurora canonical label" in text
                    else "contact" if "Aurora owner contact" in text
                    else "timezone" if "Aurora timezone" in text
                    else None
                )
                if candidate_id is None:
                    coverage.append({
                        "unit_id": unit["unit_id"],
                        "decision": "NO_CHANGE",
                        "reason": "no_future_value",
                    })
                    continue
                candidates.append({
                    "candidate_id": candidate_id,
                    "memory": text,
                    "evidence_event_ids": [unit["event_key"]],
                    "duplicate": False,
                    "worth": True,
                    "type": "fact",
                    "scopes": ["project:Aurora"],
                    "scope_source": "user" if candidate_id == "contact" else "model",
                })
                bindings.append({
                    "candidate_id": candidate_id,
                    "claims": [{
                        "unit_id": unit["unit_id"],
                        "quote": text,
                        "role": "assertion",
                    }],
                })
                coverage.append({
                    "unit_id": unit["unit_id"],
                    "decision": "CANDIDATE",
                    "candidate_ids": [candidate_id],
                })
            return json.dumps({
                "candidates": candidates,
                "coverage": coverage,
                "evidence_bindings": bindings,
            }, ensure_ascii=False)

        if purpose != "summarize":
            raise AssertionError(f"unexpected model stage: {purpose}")
        if "CREATE_RECONCILIATION\n" in prompt:
            payload = json.JSONDecoder().raw_decode(
                prompt.split("Candidate proposals with admitted evidence:\n", 1)[1]
            )[0]
            ids = [item["candidate_id"] for item in payload]
            source_keys = list(dict.fromkeys(
                event["event_key"]
                for item in payload if item["candidate_id"] in {ids[0], ids[-1]}
                for event in item["evidence"]
            ))
            merge_ids = [ids[0], ids[-1]]
            distinct_ids = [item_id for item_id in ids if item_id not in merge_ids]
            return json.dumps({
                "groups": [
                    {
                        "decision": "MERGE",
                        "candidate_ids": merge_ids,
                        "summary": {
                            "title": "Aurora operating facts",
                            "body": "Aurora canonical label and owner contact are recorded.",
                            "tags": [],
                            "type": "fact",
                            "scopes": ["project:Aurora"],
                            "scope_source": "model",
                            "sources": [{"event_key": key} for key in source_keys],
                        },
                    },
                    *([{"decision": "KEEP_DISTINCT", "candidate_ids": distinct_ids}] if distinct_ids else []),
                ],
            }, ensure_ascii=False)
        candidate = json.JSONDecoder().raw_decode(prompt.split("Candidate:\n", 1)[1])[0]
        evidence = json.loads(
            prompt.split(
                "Evidence (the only conversation content visible to this call):\n",
                1,
            )[1].split("\nRelevant existing", 1)[0]
        )
        return json.dumps({
            "title": candidate["memory"][:30],
            "body": candidate["memory"],
            "tags": [],
            "type": candidate["type"],
            "scopes": candidate["scopes"],
            "scope_source": candidate["scope_source"],
            "sources": [{"event_key": evidence[0]["event_key"]}],
        }, ensure_ascii=False)


class GateCapacityTests(unittest.TestCase):
    @staticmethod
    def _capture_reports(core: Memleaf, session_id: str, reports: list[str]) -> None:
        """Capture visible assertions followed by one final assistant reply."""
        core.capture("hermes", session_id, "turn", "user", "请处理这些材料。", event_id="request")
        for index, report in enumerate(reports):
            core.capture("hermes", session_id, "turn", "user", report,
                         event_id=f"report-{index}")
        core.capture("hermes", session_id, "turn", "assistant", "已读取材料。",
                     event_id="assistant")

    def test_quote_only_visible_report_binding_computes_nonzero_offset(self) -> None:
        quote = "Aurora owner is Alice."
        report = json.dumps(
            {"prefix": "header before the claim", "body": quote},
            ensure_ascii=False,
        )
        units = analyze_turn_evidence([{
            "event_key": "assistant",
            "role": "assistant",
            "content": report,
        }])
        unit = units[0]
        candidate = {
            "candidate_id": "owner",
            "evidence_event_ids": [unit.event_key],
        }
        binding = [{
            "candidate_id": "owner",
            "claims": [{"unit_id": unit.unit_id, "quote": quote, "role": "assertion"}],
        }]
        expected_start = unit.text.index(quote)
        self.assertGreater(expected_start, 0)
        checked = validate_bindings(binding, [unit], [candidate])
        self.assertEqual(expected_start, checked["owner"][0]["start"])
        self.assertEqual(expected_start + len(quote), checked["owner"][0]["end"])
        self.assertEqual(quote, checked["owner"][0]["quote"])

        invalid_binding = [{
            "candidate_id": "owner",
            "claims": [{
                "unit_id": unit.unit_id,
                "start": 0,
                "end": len(quote),
                "quote": quote,
                "role": "assertion",
            }],
        }]
        with self.assertRaises(ModelOutputError) as error:
            validate_bindings(invalid_binding, [unit], [candidate])
        self.assertEqual("invalid_span", error.exception.evidence_check)

    def test_invalid_span_retry_prompt_requires_quote_only(self) -> None:
        error = ModelOutputError("invalid span", validation_detail="invalid_evidence")
        error.stage = "gate"
        error.evidence_check = "invalid_span"
        self.assertEqual(EVIDENCE_SPAN_CORRECTION, ModelExecutor._correction_instruction(error))

    def test_legacy_candidate_support_stays_in_originating_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-gate-legacy-boundary-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            target = "Cedar report is approved."
            reports = [target if index in {0, 8} else f"filler-{index}"
                       for index in range(9)]
            self._capture_reports(core, "legacy-boundary", reports)
            backend = LegacyGateBackend(target)
            result = core.process(
                source="hermes",
                session_id="legacy-boundary",
                model=backend,
            )
            self.assertEqual(1, result["memories_written"])
            self.assertEqual(1, len(backend.summary_inputs))
            self.assertEqual(1, len(backend.summary_inputs[0]))
            self.assertEqual(target, backend.summary_inputs[0][0]["content"])

    def test_late_gate_hard_failure_does_not_commit_earlier_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-gate-hard-failure-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            reports = [f"Cedar report fragment {index}." for index in range(9)]
            self._capture_reports(core, "gate-hard-failure", reports)
            backend = HardFailureAfterFirstBatchBackend()
            with self.assertRaises(ModelError):
                core.process(source="hermes", session_id="gate-hard-failure", model=backend)
            self.assertEqual(0, backend.summarize_calls)
            self.assertEqual([], core._read_memories_unlocked("knowledge"))
            state = json.loads(core.vault.processed_state_path.read_text(encoding="utf-8"))
            session = state["sessions"]["hermes/gate-hard-failure"]
            self.assertEqual(0, session.get("watermark", 0))
            self.assertTrue((core.vault.inbox_path / "hermes" / "gate-hard-failure.md").is_file())

    def test_cross_batch_create_reconciliation_merges_only_same_future_use(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-gate-create-merge-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            config = core.vault.config()
            config["scopes"] = {"project:Aurora": {}}
            save_config(core.vault.config_path, config)
            reports = [
                "Aurora canonical label is AURORA."
                if index == 0 else
                "Aurora owner contact is Alice."
                if index == 8 else
                "Aurora timezone is UTC."
                if index == 4 else
                f"filler {index}"
                for index in range(9)
            ]
            self._capture_reports(core, "create-merge", reports)
            backend = CreateMergeBackend()
            result = core.process(
                source="hermes",
                session_id="create-merge",
                scope="project:Aurora",
                model=backend,
            )
            self.assertEqual(2, result["memories_written"])
            self.assertEqual(2, len(core._read_memories_unlocked("knowledge")))
            self.assertTrue(any("CREATE_RECONCILIATION\n" in prompt for purpose, prompt in backend.calls
                               if purpose == "summarize"))

    def test_visible_reports_remain_whole_and_batch_ids_are_bounded(self) -> None:
        reports = [json.dumps(
            {"id": index, "body": "a,b;c:d", "owner": "Alice"},
            ensure_ascii=False,
        ) for index in range(23)]
        units = analyze_turn_evidence([
            {"event_key": f"assistant-{index}", "role": "assistant", "content": report}
            for index, report in enumerate(reports)
        ])
        physical = [unit for unit in units if unit.can_support]
        self.assertEqual(23, len(physical))
        self.assertTrue(all(unit.syntax == "plain" for unit in physical))
        self.assertTrue(all(unit.start == 0 for unit in physical))
        self.assertTrue(all(unit.end == len(unit.text) for unit in physical))
        batches = gate_evidence_batches(physical)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(0 < len(batch) <= MAX_GATE_BATCH_UNITS for batch in batches))
        self.assertEqual(
            {unit.unit_id for unit in physical},
            {unit.unit_id for batch in batches for unit in batch},
        )
        for batch in batches:
            encoded = json.dumps(
                [unit.to_dict() for unit in batch],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self.assertLessEqual(len(encoded.encode("utf-8")), MAX_GATE_BATCH_BYTES)

    def test_cross_batch_same_target_update_and_independent_creates_are_atomic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-gate-capacity-") as temporary:
            core = Memleaf(Path(temporary) / "vault")
            config = core.vault.config()
            config["scopes"] = {"project:Aurora": {}}
            save_config(core.vault.config_path, config)
            core.create_memory(
                memory_id="aurora-owner",
                title="Aurora owner",
                body="Aurora owner is Bob.",
                type="identity",
                scopes=["project:Aurora"],
            )
            bodies = [
                "Aurora owner changed to Alice.",
                "Aurora deadline is 2026-09-30.",
                "Aurora backup region is us-east-1.",
                *[f"filler {index} x y" for index in range(14)],
                "Aurora owner is now Alice.",
            ]
            self._capture_reports(core, "capacity", bodies)
            backend = GateCapacityBackend()
            result = core.process(
                source="hermes",
                session_id="capacity",
                scope="project:Aurora",
                model=backend,
            )
            self.assertEqual(1, result["processed_turns"])
            self.assertEqual(3, result["memories_written"])
            self.assertEqual(3, len(result["memory_ids"]))
            self.assertEqual("Aurora owner is Alice.", core.read("aurora-owner").body)
            memories = core._read_memories_unlocked("knowledge")
            self.assertIn("Aurora deadline is 2026-09-30.", {item.memory.body for item in memories})
            self.assertIn("Aurora backup region is us-east-1.", {item.memory.body for item in memories})
            self.assertGreaterEqual(sum(purpose == "gate" for purpose, _ in backend.calls), 3)
            entry = json.loads(
                core.vault.processed_state_path.read_text(encoding="utf-8")
            )["sessions"]["hermes/capacity"]["processed_turns"][0]
            dispositions = entry["candidate_dispositions"]
            self.assertIn("UPDATE", {row["disposition"] for row in dispositions})
            self.assertIn("CREATE", {row["disposition"] for row in dispositions})
            self.assertIn("NO_CHANGE", {row["decision"] for row in entry["evidence_dispositions"]})


if __name__ == "__main__":
    unittest.main()
