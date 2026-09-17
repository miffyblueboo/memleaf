from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zlib

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf import extraction_work_state as budgets
from memleaf import incremental_run_state as runs
from memleaf import incremental_journal as commits
from memleaf import receipt_codec as codec
from memleaf import runtime_retention as retention
from memleaf.llm.base import ModelError


class RetentionFixture(IncrementalFixture):
    def execute(self, *items, turn="t", **kwargs):
        backend = Backend(output(*(items or (self.no_memory("e1"), self.no_memory()))))
        return self.s.run_incremental(source="hermes", session_id="s", turn_id=turn, backend=backend, **kwargs)

    def compact(self, **kwargs):
        plan = self.s.compact_runtime_state(**kwargs)
        return self.s.compact_runtime_state(dry_run=False, expected_revision=plan["state_revision"], **kwargs)

    def raw_files(self):
        return {p.relative_to(self.s.vault.root).as_posix(): p.read_bytes()
                for p in self.s.vault.root.rglob("*") if p.is_file() and not p.name.endswith(".lock")}


class RuntimeRetentionTests(RetentionFixture):
    def test_preview_is_read_only_and_reports_real_saved_bytes(self):
        self.execute()
        before = self.raw_files()
        plan = self.s.compact_runtime_state()
        self.assertEqual(before, self.raw_files())
        self.assertEqual(plan["selected"], {"runs": 1, "commits": 1, "budgets": 1})
        self.assertLess(plan["bytes_after"], plan["bytes_before"])
        self.assertEqual(plan["model_calls"], 0)

    def test_apply_keeps_exact_semantics_and_frees_both_slots(self):
        self.execute(self.create(), self.no_memory())
        old = self.ledger()
        before = self.raw_files()
        result = self.compact()
        after = self.ledger()
        self.assertEqual(result["execution_status"], "completed")
        for key in old[runs.KEY]:
            self.assertEqual(runs.load_run(old, key), runs.load_run(after, key))
        for key in old[commits.KEY]:
            self.assertEqual(commits.load_work(old, key), commits.load_work(after, key))
        for name, data in before.items():
            if name not in {"_state/processed.json", "_state/extraction_request_budget.json"}:
                self.assertEqual(self.raw_files()[name], data, name)
        self.assertEqual(result["after"]["remaining_run_slots"], 128)
        self.assertEqual(result["after"]["remaining_budget_slots"], 128)

    def test_completed_replay_does_not_call_model_or_write_again(self):
        first = self.execute(self.create(), self.no_memory())
        self.compact()
        before = self.raw_files()
        again = self.s.process_incremental(source="hermes", session_id="s", turn_id="t", model=Backend())
        self.assertEqual(again["commit"], first["commit"])
        self.assertEqual(again["model_calls_this_invocation"], 0)
        self.assertEqual(before, self.raw_files())

    def test_collector_is_idempotent(self):
        self.execute(); self.compact()
        before = self.raw_files()
        again = self.compact()
        self.assertEqual(again["selected"], {"runs": 0, "commits": 0, "budgets": 0})
        self.assertEqual(again["applied_files"], [])
        self.assertEqual(before, self.raw_files())

    def test_stale_preview_is_rejected_without_writes(self):
        self.execute()
        plan = self.s.compact_runtime_state()
        self.capture("later", seq=3)
        before = self.raw_files()
        with self.assertRaisesRegex(ValueError, "runtime_state_changed"):
            self.s.compact_runtime_state(dry_run=False, expected_revision=plan["state_revision"])
        self.assertEqual(before, self.raw_files())

    def test_preview_binds_batch_size_not_just_files(self):
        self.execute(); plan = self.s.compact_runtime_state(max_records=1)
        before = self.raw_files()
        with self.assertRaisesRegex(ValueError, "runtime_state_changed"):
            self.s.compact_runtime_state(dry_run=False, expected_revision=plan["state_revision"], max_records=2)
        self.assertEqual(before, self.raw_files())

    def test_partial_keeps_recovery_basis_and_authority(self):
        result = self.execute(self.create(), {"action": "DEFERRED", "evidence": ["e2"], "reason": "missing_context", "need": "clarify"})
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        before = self.raw_files(); plan = self.compact()
        self.assertEqual(plan["selected"], {"runs": 0, "commits": 0, "budgets": 0})
        self.assertEqual(before, self.raw_files())
        self.assertTrue(self.s.resume_incremental_run(result["run_id"])["partial_recovery_available"])

    def test_failed_run_is_not_collected_as_success(self):
        self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=Backend("{", "{"))
        before = self.raw_files()
        self.assertEqual(self.compact()["selected"]["runs"], 0)
        self.assertEqual(before, self.raw_files())

    def test_retryable_run_does_not_lose_remaining_allowance(self):
        result = self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=Backend(ModelError("temporary", code="model_timeout")))
        before = self.raw_files()
        self.assertEqual(self.compact()["selected"]["budgets"], 0)
        self.assertEqual(before, self.raw_files())
        again = self.s.resume_incremental_run(result["run_id"], backend=Backend(output(self.no_memory("e1"), self.no_memory())))
        self.assertEqual(again["reserved_requests"], 2)

    def test_missing_budget_blocks_run_retirement(self):
        self.execute(); budgets._budget_path(self.s.vault).unlink()
        plan = self.compact()
        self.assertEqual(plan["selected"]["runs"], 0)
        self.assertEqual(plan["selected"]["budgets"], 0)
        self.assertFalse(budgets._budget_path(self.s.vault).exists())

    def test_unfinalized_budget_flag_is_not_guessed(self):
        first = self.execute()
        state = self.ledger(); run = runs.load_run(state, first["run_id"])
        run["budget_finalized"] = False; runs.save_run(self.s, state, run)
        self.assertEqual(self.compact()["selected"]["runs"], 0)

    def test_bad_budget_fails_without_replacing_other_state(self):
        self.execute(); budgets._budget_path(self.s.vault).write_text("{broken")
        before = self.raw_files()
        with self.assertRaises(budgets.ExtractionWorkStateError):
            self.compact()
        self.assertEqual(before, self.raw_files())

    def test_duplicate_budget_json_key_is_not_silently_chosen(self):
        self.execute()
        budgets._budget_path(self.s.vault).write_text('{"version":1,"version":2,"works":{},"order":[]}')
        with self.assertRaises(budgets.ExtractionWorkStateError): self.compact()

    def test_corrupt_other_receipt_blocks_partial_state_rewrite(self):
        self.execute()
        state = self.ledger(); key = next(iter(state[commits.KEY]))
        state[commits.KEY][key]["checksum"] = "0" * 64
        self.s.vault.processed_state_path.write_text(json.dumps(state))
        before = self.raw_files()
        with self.assertRaises(ValueError): self.compact()
        self.assertEqual(before, self.raw_files())

    def test_forget_still_cancels_compacted_run_and_no_recreation(self):
        first = self.execute(self.create(), self.no_memory()); self.compact()
        mid = first["commit"]["operations"][0]["memory_id"]
        self.s.forget_memory(mid)
        result = self.s.resume_incremental_run(first["run_id"], backend=Backend())
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertIsNone(result["commit"])
        self.assertIsNone(self.s.read(mid))
        state = self.ledger()
        self.assertTrue(codec.is_compact(state[runs.KEY][first["run_id"]], runs.COMPACT_VERSION))

    def test_new_true_authorization_still_can_retain_same_text(self):
        text = "Use the stable API."
        b = Backend(output(self.create()))
        first = self.s.remember(text, pipeline="incremental", intent_id="original", model=b)
        self.compact()
        self.s.forget_memory(first["memory_ids"][0])
        second = self.s.remember(text, pipeline="incremental", intent_id="new-user-request", model=Backend(output(self.create())))
        self.assertEqual(second["execution_status"], "completed")
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_same_explicit_intent_changed_text_is_not_new_budget(self):
        self.s.remember("Use stable API", pipeline="incremental", intent_id="one", model=Backend(output(self.create())))
        self.compact()
        with self.assertRaises(ValueError):
            self.s.remember("Use different API", pipeline="incremental", intent_id="one", model=Backend())

    def test_active_and_retired_slots_are_distinct(self):
        with patch.object(runs, "MAX_RUNS", 1), patch.object(budgets, "_MAX_WORKS", 1):
            first = self.execute(); self.compact()
            self.capture("next", seq=3)
            second = self.execute(turn="next")
            self.assertEqual(second["execution_status"], "completed")
            self.assertEqual(len(self.ledger()[runs.KEY]), 2)
            self.assertEqual(self.s.resume_incremental_run(first["run_id"])["model_calls_this_invocation"], 0)

    def test_no_automatic_eviction_reopens_an_old_budget(self):
        with patch.object(budgets, "_MAX_WORKS", 1):
            self.assertEqual(budgets.reserve_model_request(self.s.vault, work_id="legacy-a", turn_id="t"), 1)
            budgets.complete_turn_budget(self.s.vault, work_id="legacy-a", turn_id="t")
            before = budgets._budget_path(self.s.vault).read_bytes()
            with self.assertRaises(budgets.ExtractionWorkStateError):
                budgets.reserve_model_request(self.s.vault, work_id="legacy-b", turn_id="u")
            self.assertEqual(before, budgets._budget_path(self.s.vault).read_bytes())
            self.assertIsNone(budgets.reserve_model_request(self.s.vault, work_id="legacy-a", turn_id="t"))

    def test_completed_source_budget_keeps_authority_under_capacity_pressure(self):
        first, second = "work-" + "a" * 64, "work-" + "b" * 64
        with patch.object(budgets, "_MAX_WORKS", 1):
            self.assertEqual(budgets.reserve_model_request(self.s.vault, work_id=first, turn_id="one"), 1)
            budgets.complete_turn_budget(self.s.vault, work_id=first, turn_id="one")
            self.assertEqual(budgets.reserve_model_request(self.s.vault, work_id=second, turn_id="two"), 1)
            state = budgets._read_budget_state_unlocked(self.s.vault)
            self.assertEqual(len(state["works"]), 2)
            self.assertTrue(state["works"][first]["retired"])
            self.assertEqual(state["works"][first]["turns"]["one"]["requests"], 1)
            self.assertIsNone(budgets.reserve_model_request(self.s.vault, work_id=first, turn_id="one"))

    def test_opaque_control_fields_survive_maintenance(self):
        self.execute()
        path = budgets._budget_path(self.s.vault)
        data = json.loads(path.read_text()); data["operator_note"] = {"keep": True}
        key = next(iter(data["works"])); data["works"][key]["extension"] = ["unchanged"]
        path.write_text(json.dumps(data))
        self.compact()
        after = json.loads(path.read_text())
        self.assertEqual(after["operator_note"], data["operator_note"])
        self.assertEqual(after["works"][key]["extension"], data["works"][key]["extension"])

    def test_retired_budget_denies_new_turn_under_same_work(self):
        first = self.execute(); self.compact()
        run = runs.load_run(self.ledger(), first["run_id"])
        before = budgets._budget_path(self.s.vault).read_bytes()
        self.assertIsNone(budgets.reserve_model_request(self.s.vault, work_id=run["budget_id"], turn_id="new-forged-turn"))
        self.assertEqual(before, budgets._budget_path(self.s.vault).read_bytes())

    def test_retired_budget_counts_are_kept_even_if_run_control_is_missing(self):
        first = self.execute(); self.compact()
        state = self.ledger(); run = runs.load_run(state, first["run_id"])
        state.pop(runs.KEY); self.s.vault.processed_state_path.write_text(json.dumps(state))
        self.assertIsNone(budgets.reserve_model_request(self.s.vault, work_id=run["budget_id"], turn_id=run["turn_budget_id"]))

    def test_unknown_legacy_budget_is_not_retired(self):
        budgets.reserve_model_request(self.s.vault, work_id="job-legacy", turn_id="old")
        budgets.complete_turn_budget(self.s.vault, work_id="job-legacy", turn_id="old")
        before = budgets._budget_path(self.s.vault).read_bytes()
        self.assertEqual(self.compact()["selected"]["budgets"], 0)
        self.assertEqual(before, budgets._budget_path(self.s.vault).read_bytes())

    def test_batch_is_bounded_and_next_preview_finishes_rest(self):
        self.execute(); self.capture("next", seq=3); self.execute(turn="next")
        a = self.compact(max_records=1)
        self.assertEqual(a["selected"], {"runs": 1, "commits": 1, "budgets": 1})
        b = self.compact(max_records=1)
        self.assertEqual(b["selected"], a["selected"])
        self.assertEqual(self.s.compact_runtime_state()["selected"]["runs"], 0)

    def test_compact_receipt_bound_retains_existing_suppression(self):
        self.execute(); self.compact()
        self.capture("next", seq=3); self.execute(turn="next")
        with patch.object(retention, "MAX_COMPACT_RECEIPTS", 1):
            plan = self.compact()
        self.assertEqual(plan["selected"]["runs"], 0)
        self.assertIn("receipt_retention_full", plan["skipped"])
        self.assertEqual(len(self.ledger()[runs.KEY]), 2)

    def test_options_are_typed_and_apply_requires_snapshot(self):
        for kwargs in ({"dry_run": 1}, {"max_records": True}, {"max_records": 0}, {"max_records": 129}, {"dry_run": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.s.compact_runtime_state(**kwargs)

    def test_inventory_counts_compact_receipts_without_using_active_slots(self):
        from memleaf.query_progress import retention_inventory
        self.execute(); self.compact()
        report = retention_inventory(self.s.vault)
        self.assertEqual(report["retained_runs"], 1)
        self.assertEqual(report["compact_runs"], 1)
        self.assertEqual(report["remaining_run_slots"], 128)
        self.assertFalse(report["collection_authorized"])

    def test_pipeline_progress_unchanged_by_compaction(self):
        from memleaf.query_progress import observe_progress
        self.execute(); old = observe_progress(self.s.vault)
        self.compact()
        self.assertEqual(old, observe_progress(self.s.vault))

    def test_dirty_commit_cannot_be_sealed(self):
        first = self.execute()
        state = self.ledger(); w = commits.load_work(state, first["commit_work_id"])
        w["index_status"] = "dirty"; commits.save_work(self.s, state, w)
        plan = self.compact()
        self.assertEqual(plan["selected"]["commits"], 0)
        self.assertEqual(plan["selected"]["runs"], 0)


class ReceiptCodecTests(unittest.TestCase):
    def original(self):
        payload = json.dumps({"text": "中文 data" * 100}, ensure_ascii=False)
        return {"version": 1, "payload": payload, "checksum": hashlib.sha256(payload.encode()).hexdigest()}

    def decode(self, wrapper, maximum=10000):
        return codec.decode_receipt(wrapper, version=2, maximum=maximum, payload_versions={1})

    def test_exact_utf8_roundtrip(self):
        value = self.original()
        self.assertEqual(self.decode(codec.encode_receipt(value, version=2)), value)

    def test_invalid_shapes_are_rejected(self):
        original = codec.encode_receipt(self.original(), version=2)
        for changes in ({"version": True}, {"decoded_bytes": True}, {"decoded_bytes": 0},
                        {"payload_version": 9}, {"encoding": "other"}, {"payload": "!bad!"},
                        {"checksum": "0" * 64}, {"unknown": "field"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.decode({**original, **changes})

    def test_trailing_stream_rejected(self):
        value = codec.encode_receipt(self.original(), version=2)
        value["payload"] = base64.b64encode(base64.b64decode(value["payload"]) + zlib.compress(b"more")).decode()
        with self.assertRaises(ValueError): self.decode(value)

    def test_truncated_stream_rejected(self):
        value = codec.encode_receipt(self.original(), version=2)
        value["payload"] = value["payload"][:-4]
        with self.assertRaises(ValueError): self.decode(value)

    def test_declared_size_cannot_expand_unbounded(self):
        value = codec.encode_receipt(self.original(), version=2)
        value["decoded_bytes"] = 10
        with self.assertRaises(ValueError): self.decode(value)
        with self.assertRaises(ValueError): self.decode(codec.encode_receipt(self.original(), version=2), maximum=1)

    def test_decoded_ledger_has_independent_limit(self):
        wrapper = codec.encode_receipt(self.original(), version=2)
        with patch.object(codec, "MAX_DECODED_LEDGER_BYTES", 20), self.assertRaisesRegex(ValueError, "retention_full"):
            codec.ledger_usage({"one": wrapper}, compact_version=2)


if __name__ == "__main__":
    unittest.main()
