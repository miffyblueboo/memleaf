from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf.incremental_journal import load_work
from memleaf.incremental_run_state import load_run


class PartialTests(IncrementalFixture):
    def execute(self, *rows, **kw):
        return self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=Backend(output(*rows)), **kw)

    def deferred(self, ref="e1"):
        return {"action":"DEFERRED", "evidence":[ref], "reason":"missing_identity", "need":"Identify the task."}

    def recover(self, result, *rows, **kw):
        backend = Backend(output(*rows))
        final = self.s.recover_incremental_partial(result["run_id"], model=backend, **kw)
        return final, backend

    def add_context(self):
        self.capture("t2", content="Clarification: the pending statement is an independent Atlas requirement.", seq=3)

    def test_scalar_evidence_repair_without_model(self):
        row = self.create(); row["evidence"] = "e1"
        first = self.execute(row, self.no_memory())
        self.assertEqual(first["execution_status"], "completed_with_unresolved")
        parent = load_work(self.ledger(), first["commit_work_id"])
        final, backend = self.recover(first, mode="repair")
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(backend.calls, [])
        self.assertEqual(final["reserved_requests"], 1)
        self.assertEqual(final["commit"]["counts"]["committed"], 1)
        self.assertEqual(final["commit"]["counts"]["no_memory"], 1)
        self.assertEqual(load_work(self.ledger(), first["commit_work_id"]), parent)
        again = self.s.recover_incremental_partial(first["run_id"], mode="repair")
        self.assertEqual(again["commit"]["operations"], final["commit"]["operations"])
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 1)
        self.assertNotIn("partial_basis", load_run(self.ledger(), first["run_id"]))

    def test_replan_unchanged_context_does_not_call_or_mutate(self):
        first = self.execute(self.create(), self.deferred("e2"))
        before = self.s.vault.processed_state_path.read_bytes()
        final, backend = self.recover(first)
        self.assertEqual(final["partial_recovery_code"], "context_unchanged")
        self.assertEqual(before, self.s.vault.processed_state_path.read_bytes())
        self.assertEqual(backend.calls, [])

    def test_replan_new_context_only_unresolved_is_new(self):
        first = self.execute(self.no_memory(), self.deferred())
        parent = load_work(self.ledger(), first["commit_work_id"])
        self.add_context()
        final, backend = self.recover(first, self.create())
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(final["reserved_requests"], 2)
        value = json.loads(backend.calls[0][0])
        self.assertEqual([e["ref"] for e in value["evidence"] if e["use"] == "new"], ["e1"])
        self.assertIn("recovery", value)
        self.assertNotIn("上次请求", backend.calls[0][1]["system"])
        self.assertEqual(load_work(self.ledger(), first["commit_work_id"]), parent)
        entry = self.ledger()["sessions"]["hermes/s"]["processed_turns"][0]
        self.assertEqual(len(entry["event_keys"]), 2)
        self.assertEqual(entry["incremental_coverage"], "complete")

    def test_replan_own_writes_not_new_context(self):
        first = self.execute(self.create(), self.deferred("e2"))
        final, backend = self.recover(first)
        self.assertEqual(final["partial_recovery_code"], "context_unchanged")
        self.assertEqual(backend.calls, [])

    def test_shared_block_is_not_blindly_replanned(self):
        first = self.execute(self.create(), self.deferred("e1"), self.no_memory())
        self.add_context()
        with self.assertRaisesRegex(ValueError, "no_isolated"):
            self.recover(first, self.create())
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 1)

    def test_new_context_target_can_be_named_without_scope_expansion(self):
        first = self.execute(self.deferred(), self.no_memory())
        self.target()
        final, backend = self.recover(first, self.update(), context_memory_ids=["mem-old"])
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(self.s.read("mem-old").status, "completed")
        self.assertEqual(final["reserved_requests"], 2)

    def test_repair_target_group_whole(self):
        self.target()
        row = self.update(body="Deliver the report; confirmation received."); row["evidence"] = "e1"
        first = self.execute(self.update(), row, self.no_memory(), priority_memory_ids=["mem-old"])
        self.assertEqual(self.s.read("mem-old").status, "active")
        final, backend = self.recover(first, mode="repair")
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(self.s.read("mem-old").status, "completed")
        self.assertEqual(len(self.hist()), 1)
        self.assertEqual(backend.calls, [])

    def test_repair_cannot_change_target_snapshot(self):
        self.target()
        row = self.update(); row["evidence"] = "e1"
        first = self.execute(row, self.no_memory(), priority_memory_ids=["mem-old"])
        m = self.s.read("mem-old"); m.body = "A changed objective"; self.s.write_memory(m)
        with self.assertRaisesRegex(ValueError, "repair_target_changed"):
            self.recover(first, mode="repair")
        self.assertEqual(self.s.read("mem-old").body, "A changed objective")

    def test_repair_cannot_ignore_added_source_context(self):
        row = self.create(); row["evidence"] = "e1"
        first = self.execute(row, self.no_memory()); self.add_context()
        with self.assertRaisesRegex(ValueError, "repair_context_changed"):
            self.recover(first, mode="repair")

    def test_invalid_replan_response_does_not_repeat_or_erase_success(self):
        first = self.execute(self.create(), self.deferred("e2")); self.add_context()
        backend = Backend("not JSON")
        final = self.s.recover_incremental_partial(first["run_id"], model=backend)
        self.assertEqual(final["execution_status"], "failed")
        self.assertEqual(final["code"], "invalid_partial_response")
        self.assertEqual(final["commit"]["counts"]["committed"], 1)
        self.assertEqual(final["reserved_requests"], 2)
        self.assertEqual(len(backend.calls), 1)
        self.s.resume_incremental_run(first["run_id"], backend=backend)
        self.assertEqual(len(backend.calls), 1)

    def test_completed_root_has_no_recovery_basis(self):
        first = self.execute(self.no_memory("e1"), self.no_memory())
        with self.assertRaisesRegex(ValueError,"not_available"):
            self.recover(first)

    def test_root_two_calls_no_third_replan(self):
        backend = Backend("bad", output(self.deferred(), self.no_memory()))
        first = self.s.run_incremental(source="hermes",session_id="s",turn_id="t",backend=backend)
        self.add_context()
        final, unused = self.recover(first, self.create())
        self.assertEqual(final["partial_recovery_code"], "request_budget_exhausted")
        self.assertEqual(unused.calls, [])

    def test_remaining_partial_no_new_round(self):
        first = self.execute(self.deferred(), self.no_memory()); self.add_context()
        final, backend = self.recover(first, self.deferred())
        self.assertEqual(final["execution_status"], "completed_with_unresolved")
        self.assertFalse(final["partial_recovery_available"])
        self.s.recover_incremental_partial(first["run_id"], model=backend)
        self.assertEqual(len(backend.calls), 1)

    def test_replan_does_not_rewrite_settled_target(self):
        self.target()
        first = self.execute(self.update(), self.deferred("e2"), priority_memory_ids=["mem-old"])
        self.add_context()
        row = self.update(body="do not overwrite"); row["evidence"] = ["e2"]
        final, _ = self.recover(first, row)
        self.assertEqual(final["execution_status"], "completed_with_unresolved")
        self.assertEqual(self.s.read("mem-old").status,"completed")
        self.assertNotIn("overwrite", self.s.read("mem-old").body)
        self.assertEqual(len(self.hist()),1)

    def test_replan_source_edit_rejected_before_dispatch(self):
        first = self.execute(self.deferred(), self.no_memory()); self.revise()
        with self.assertRaises(ValueError):
            self.recover(first, self.create())
