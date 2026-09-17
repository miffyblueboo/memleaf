from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.incremental_execution import IncrementalRunError
from memleaf.incremental_run_state import load_run, save_run
from memleaf.incremental_journal import load_work, canonical
from memleaf.llm import ModelError
from memleaf.locking import atomic_write_json


class PartialBoundaryTests(IncrementalFixture):
    def deferred(self, ref="e1"):
        return {"action":"DEFERRED","evidence":[ref],"reason":"missing_context","need":"Clarify the task."}

    def execute(self, *rows, **kw):
        return self.s.run_incremental(source="hermes",session_id="s",turn_id="t",backend=Backend(output(*rows)),**kw)

    def later(self):
        self.capture("t2", content="The task identity is clarified.", seq=3)

    def native(self, agent="hermes", share=False):
        path=Path(self.temp.name)/"native.md"; path.write_text("# Atlas\n\nDeliver the report.")
        config=self.s.vault.config()
        config["native_sources"]={"notes":{"path":str(path),"agent":agent,"share":share,"enabled":True}}
        save_config(self.s.vault.config_path,config)
        return path

    def test_native_no_change_on_replan_does_not_copy_or_modify(self):
        path=self.native(); before=path.read_bytes()
        first=self.execute(self.deferred(),self.no_memory());self.later()
        b=Backend(output({"action":"NO_CHANGE","evidence":["e1"],"target":"m1"}))
        final=self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertEqual(final["execution_status"],"completed")
        self.assertTrue(any(op.get("native") for op in final["commit"]["operations"]))
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(self.s.vault.list_markdown("knowledge"))
        self.assertFalse(self.hist())

    def test_revoked_sharing_never_resends_old_native_body(self):
        path=self.native(agent="codex",share=True)
        first=self.execute(self.deferred(),self.no_memory())
        config=self.s.vault.config();config["native_sources"]["notes"]["share"]=False;save_config(self.s.vault.config_path,config)
        b=Backend(output(self.no_memory("e1")))
        final=self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(json.loads(b.calls[0][0])["memories"],[])
        self.assertNotIn("Deliver the report.",b.calls[0][0])

    def test_native_changed_during_replan_blocks_pending_decision(self):
        path=self.native();first=self.execute(self.deferred(),self.no_memory());self.later()
        def change():
            path.write_text("# Atlas\n\nDo not deliver the report.")
            return output({"action":"NO_CHANGE","evidence":["e1"],"target":"m1"})
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(change))
        self.assertEqual(final["execution_status"],"blocked")
        self.assertEqual(final["commit"]["counts"]["no_memory"],1)

    def test_missing_eligible_native_blocks_before_extra_dispatch(self):
        path=self.native();first=self.execute(self.deferred(),self.no_memory());self.later();path.unlink()
        b=Backend()
        with self.assertRaisesRegex(ValueError,"native_source_missing"):
            self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertFalse(b.calls)
        self.assertEqual(load_run(self.ledger(),first["run_id"])["reserved_requests"],1)

    def test_replan_cannot_mutate_native_target(self):
        path=self.native();before=path.read_bytes();first=self.execute(self.deferred(),self.no_memory());self.later()
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.update(body="replace native"))))
        self.assertEqual(final["execution_status"],"completed_with_unresolved")
        self.assertEqual(path.read_bytes(),before)

    def test_cross_scope_context_does_not_expand_writes(self):
        self.target(scopes=["project:Beacon"])
        first=self.execute(self.deferred(),self.no_memory(),scope="project:Atlas",priority_memory_ids=["mem-old"])
        self.later()
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.update())))
        self.assertEqual(final["execution_status"],"completed_with_unresolved")
        self.assertEqual(self.s.read("mem-old").status,"active")
        self.assertFalse(self.hist())

    def test_mixed_target_group_not_split_around_settled_block(self):
        self.target()
        a=self.create(title="independent task")
        b=self.update(body="required companion text");b["evidence"]=["e1"]
        c=self.update(status="completed");c["evidence"]=["e2"];c["unexpected"]=True
        first=self.execute(a,b,c,priority_memory_ids=["mem-old"]);self.later()
        with self.assertRaisesRegex(ValueError,"no_isolated"):
            self.s.recover_incremental_partial(first["run_id"],model=Backend())
        self.assertEqual(self.s.read("mem-old").status,"active")

    def test_unknown_reference_not_repaired_to_number_or_nearest(self):
        row=self.create();row["evidence"]="1"
        first=self.execute(row,self.no_memory())
        final=self.s.recover_incremental_partial(first["run_id"],mode="repair")
        self.assertEqual(final["partial_recovery_code"],"no_safe_structural_repair")
        self.assertFalse(self.s.vault.list_markdown("knowledge"))

    def test_single_scope_container_preserves_exact_business_values(self):
        row=self.create();row["memory"]["scope"]=["global"]
        first=self.execute(row,self.no_memory())
        final=self.s.recover_incremental_partial(first["run_id"],mode="repair")
        self.assertEqual(final["execution_status"],"completed")
        mid=next(o["memory_id"] for o in final["commit"]["operations"] if o["action"]=="CREATE")
        self.assertEqual(self.s.read(mid).body,row["memory"]["body"])
        self.assertEqual(self.s.read(mid).status,row["memory"]["status"])

    def test_no_safe_repair_does_not_consume_later_replan_option(self):
        first=self.execute(self.deferred(),self.no_memory())
        before=self.s.vault.processed_state_path.read_bytes()
        self.s.recover_incremental_partial(first["run_id"],mode="repair")
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())
        self.later()
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.create())))
        self.assertEqual(final["execution_status"],"completed")

    def test_second_call_timeout_never_grants_third_call(self):
        first=self.execute(self.deferred(),self.no_memory());self.later()
        b=Backend(ModelError("sensitive failure detail",code="model_timeout"))
        final=self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertEqual(final["execution_status"],"failed")
        self.assertEqual(final["reserved_requests"],2)
        self.s.resume_incremental_run(first["run_id"],backend=b)
        self.assertEqual(len(b.calls),1)
        self.assertNotIn("sensitive failure detail",self.s.vault.processed_state_path.read_text())

    def test_oversized_context_does_not_spend_or_truncate(self):
        first=self.execute(self.deferred(),self.no_memory());self.later()
        before=self.s.vault.processed_state_path.read_bytes();b=Backend()
        with patch("memleaf.incremental_partial.MAX_BYTES",100):
            with self.assertRaisesRegex(ValueError,"blocked_context"):
                self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes());self.assertFalse(b.calls)

    def test_unlocated_error_cannot_be_claimed_resolved_by_replan(self):
        first=self.execute({"foo":"cannot locate decision"},self.deferred(),self.no_memory());self.later()
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.no_memory("e1"))))
        self.assertEqual(final["execution_status"],"completed_with_unresolved")
        self.assertTrue(any(not i["evidence"] for i in final["commit"]["issues"]))

    def test_bad_replan_reference_does_not_touch_original_success(self):
        first=self.execute(self.create(),self.deferred("e2"));self.later()
        b=Backend(output({"action":"UPDATE","evidence":["e2"],"target":"m999","patch":{"body":"bad"}}))
        original=deepcopy(first["commit"]["operations"][0])
        final=self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertEqual(final["execution_status"],"completed_with_unresolved")
        self.assertIn(original,final["commit"]["operations"])
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)

    def test_recording_disabled_before_replan_preserves_receipt(self):
        first=self.execute(self.deferred(),self.no_memory());self.later()
        state=self.ledger();state["sessions"]["hermes/s"]["capture_policy"]={"enabled":False}
        atomic_write_json(self.s.vault.processed_state_path,state)
        before=self.s.vault.processed_state_path.read_bytes()
        with self.assertRaisesRegex(ValueError,"source_recording_revoked"):
            self.s.recover_incremental_partial(first["run_id"],model=Backend())
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())

    def test_bad_partial_capsule_digest_fails_closed(self):
        first=self.execute(self.deferred(),self.no_memory());state=self.ledger();run=load_run(state,first["run_id"])
        run["partial_basis"]["snapshot"]["evidence"][0]["text"]="changed without source revision"
        with self.s.vault.lock(),self.assertRaisesRegex(ValueError,"snapshot_digest"):
            save_run(self.s,state,run)

    def test_recovery_child_keeps_original_evidence_set_and_ids(self):
        first=self.execute(self.create(),self.deferred("e2"));self.later()
        b=Backend(output(self.no_memory("e2")))
        final=self.s.recover_incremental_partial(first["run_id"],model=b)
        parent=load_work(self.ledger(),first["commit_work_id"]);child=load_work(self.ledger(),final["commit_work_id"])
        self.assertEqual(child["recovery_parent"],parent["work_id"])
        self.assertEqual({e["event_key"] for e in parent["evidence"] if e["use"]=="new"},
                         {e["event_key"] for e in child["evidence"] if e["use"]=="new"})
        self.assertEqual(parent["operations"][0],child["operations"][0])
