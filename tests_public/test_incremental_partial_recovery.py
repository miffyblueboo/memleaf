from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.extraction_work_state import _budget_path
from memleaf.incremental_execution import IncrementalRunError
from memleaf.incremental_journal import load_work, protected_turn_keys, canonical
from memleaf.incremental_run_state import KEY, load_run
from memleaf.index import turn_key
from memleaf.llm import ModelError
from memleaf.locking import atomic_write_json


class PartialRecoveryTests(IncrementalFixture):
    def partial(self, *, explicit=False, scalar=False):
        row = self.create()
        if scalar:
            row["evidence"] = "e1"
        else:
            row = {"action":"DEFERRED","evidence":["e1"],"reason":"missing_context","need":"Confirm the objective."}
        if explicit:
            preview = self.s.preview_incremental(source="hermes",session_id="s",turn_id="t")
            return self.s.remember_incremental(source="hermes",session_id="s",turn_id="t",intent_id="explicit-1",
                selected_source_refs=[preview["source_refs"][0]["source_ref"]], retention_request="Keep the task only",
                model=Backend(output(row)))
        return self.s.run_incremental(source="hermes",session_id="s",turn_id="t",backend=Backend(output(row,self.no_memory())))

    def later(self):
        self.capture("t2",content="The original objective is confirmed.",seq=3)

    def test_saved_replan_response_recovers_without_backend(self):
        first=self.partial(); self.later(); b=Backend(output(self.create()))
        with patch("memleaf.incremental_execution.apply_incremental",side_effect=OSError("fixture")):
            with self.assertRaises(IncrementalRunError):
                self.s.recover_incremental_partial(first["run_id"],model=b)
        self.s=Memleaf(self.s.vault.root)
        with patch("memleaf.incremental_runtime._resolve",side_effect=AssertionError("no dispatch")):
            final=self.s.recover_incremental_partial(first["run_id"])
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["reserved_requests"],2)
        self.assertEqual(len(b.calls),1)

    def test_frozen_child_write_recovers_same_permanent_id(self):
        first=self.partial();self.later();b=Backend(output(self.create()))
        with patch("memleaf.memory_writer.MemoryWriter.write_frozen_unlocked",side_effect=OSError("fixture")):
            with self.assertRaises(IncrementalRunError) as caught:
                self.s.recover_incremental_partial(first["run_id"],model=b)
        work=load_work(self.ledger(),caught.exception.result["commit_work_id"])
        mid=next(op["memory_id"] for op in work["operations"] if op["action"]=="CREATE")
        final=self.s.resume_incremental_run(first["run_id"])
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(len(b.calls),1)
        self.assertIsNotNone(self.s.read(mid))
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)

    def test_repair_index_failure_then_recovery_is_zero_call(self):
        first=self.partial(scalar=True)
        with patch.object(self.s,"_rebuild_index_unlocked",side_effect=OSError("fixture")):
            with self.assertRaises(IncrementalRunError):
                self.s.recover_incremental_partial(first["run_id"],mode="repair")
        final=self.s.recover_incremental_partial(first["run_id"],mode="repair")
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["model_calls_this_invocation"],0)
        self.assertEqual(final["reserved_requests"],1)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)

    def test_source_edit_during_second_call_preserves_original_success(self):
        first=self.partial();self.later()
        def changed():
            self.revise()
            return output(self.create())
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(changed))
        self.assertEqual(final["execution_status"],"blocked")
        self.assertEqual(final["commit"]["counts"]["no_memory"],1)
        self.assertFalse(self.s.vault.list_markdown("knowledge"))

    def test_target_edit_during_replan_refuses_old_payload(self):
        first=self.partial(); self.target()
        def edit():
            m=self.s.read("mem-old");m.body="new manual objective";self.s.write_memory(m)
            return output(self.update())
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(edit),context_memory_ids=["mem-old"])
        self.assertEqual(final["execution_status"],"blocked")
        self.assertEqual(self.s.read("mem-old").body,"new manual objective")

    def test_local_repair_does_not_invent_missing_target(self):
        b=Backend(output({"action":"UPDATE","evidence":"e1","target":"m999","patch":{"status":"completed"}},self.no_memory()))
        first=self.s.run_incremental(source="hermes",session_id="s",turn_id="t",backend=b)
        final=self.s.recover_incremental_partial(first["run_id"],mode="repair")
        self.assertEqual(final["execution_status"],"completed_with_unresolved")
        self.assertFalse(self.s.vault.list_markdown("knowledge"))
        self.assertEqual(final["reserved_requests"],1)

    def test_budget_missing_does_not_allow_second_call(self):
        first=self.partial();self.later();_budget_path(self.s.vault).unlink();b=Backend(output(self.create()))
        with self.assertRaisesRegex(ValueError,"partial_budget_state_lost"):
            self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertEqual(b.calls,[])

    def test_mode_or_context_change_does_not_reopen_recovery(self):
        first=self.partial();self.later()
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.no_memory("e1"))))
        with self.assertRaisesRegex(ValueError,"already_bound"):
            self.s.recover_incremental_partial(first["run_id"],mode="repair")
        self.assertEqual(final["reserved_requests"],2)

    def test_selected_work_does_not_consume_automatic_turn(self):
        first=self.partial(explicit=True);self.later()
        before=deepcopy(self.ledger()["sessions"]["hermes/s"])
        b=Backend(output(self.create()))
        final=self.s.recover_incremental_partial(first["run_id"],model=b)
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["request_kind"],"explicit_remember")
        self.assertEqual(self.ledger()["sessions"]["hermes/s"],before)
        value=json.loads(b.calls[0][0])
        self.assertEqual(value["retention_request"],"Keep the task only")
        self.assertEqual([e["ref"] for e in value["evidence"] if e["use"]=="new"],["e1"])
        self.assertNotIn("Keep the task only",self.s.vault.processed_state_path.read_text())

    def test_explicit_replan_cannot_no_memory_authorized_content(self):
        first=self.partial(explicit=True);self.later()
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.no_memory("e1"))))
        self.assertEqual(final["execution_status"],"completed_with_unresolved")
        self.assertTrue(any(i["code"]=="explicit_retention_required" for i in final["commit"]["issues"]))

    def test_completed_child_releases_parent_context_protection(self):
        first=self.partial();self.later()
        self.assertIn(turn_key("t"),protected_turn_keys(self.ledger(),"hermes","s"))
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.no_memory("e1"))))
        self.assertEqual(final["execution_status"],"completed")
        self.assertNotIn(turn_key("t"),protected_turn_keys(self.ledger(),"hermes","s"))
        self.assertNotIn(turn_key("t2"),protected_turn_keys(self.ledger(),"hermes","s"))

    def test_forget_clears_retained_partial_basis(self):
        self.target()
        b=Backend(output({"action":"DEFERRED","evidence":["e1"],"reason":"missing_context","need":"confirm"},self.no_memory()))
        first=self.s.run_incremental(source="hermes",session_id="s",turn_id="t",backend=b,priority_memory_ids=["mem-old"])
        self.assertIn("partial_basis",load_run(self.ledger(),first["run_id"]))
        self.s.forget_memory("mem-old")
        run=load_run(self.ledger(),first["run_id"])
        self.assertEqual(run["status"],"cancelled")
        self.assertNotIn("partial_basis",run)
        with self.assertRaisesRegex(ValueError,"not_available"):
            self.s.recover_incremental_partial(first["run_id"])

    def test_forget_during_replan_cancels_callback(self):
        first=self.partial();self.target()
        def forget():
            self.s.forget_memory("mem-old")
            return output(self.update())
        final=self.s.recover_incremental_partial(first["run_id"],model=Backend(forget),context_memory_ids=["mem-old"])
        self.assertEqual(final["execution_status"],"cancelled")
        self.assertFalse(self.s.vault.list_markdown("knowledge"))
        self.assertNotIn("partial_basis",load_run(self.ledger(),first["run_id"]))

    def test_only_one_concurrent_replan_dispatches(self):
        first=self.partial();self.later();entered=threading.Event();release=threading.Event();results=[]
        def hold():
            entered.set()
            if not release.wait(10):raise AssertionError("fixture timeout")
            return output(self.no_memory("e1"))
        b=Backend(hold)
        def work():
            try:results.append(self.s.recover_incremental_partial(first["run_id"],model=b))
            except Exception as e:results.append(e)
        thread=threading.Thread(target=work);thread.start()
        try:
            self.assertTrue(entered.wait(10))
            with self.assertRaisesRegex(ValueError,"busy"):
                self.s.recover_incremental_partial(first["run_id"],model=Backend())
        finally:release.set();thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["execution_status"],"completed")
        self.assertEqual(len(b.calls),1)

    def test_process_exit_after_response_saves_second_call(self):
        first=self.partial();self.later()
        code='''import os,sys,json
from unittest.mock import patch
from memleaf import Memleaf
from memleaf.incremental_run_state import save_run as original
s=Memleaf(sys.argv[1])
class B:
 single_pass_safe=True
 def complete(self,*a,**k):return json.dumps({"items":[{"action":"NO_MEMORY","evidence":["e1"]}]})
def save(service,state,run):
 original(service,state,run)
 if run.get("partial_used") and run["status"]=="response_ready":os._exit(73)
with patch("memleaf.incremental_execution.save_run",save):s.recover_incremental_partial(sys.argv[2],model=B())
'''
        result=subprocess.run([sys.executable,"-c",code,str(self.s.vault.root),first["run_id"]],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,73,result.stderr.decode())
        final=Memleaf(self.s.vault.root).resume_incremental_run(first["run_id"])
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["reserved_requests"],2)
        self.assertEqual(final["model_calls_this_invocation"],0)

    def test_actual_exit_during_replan_spends_allowance_but_preserves_success(self):
        first=self.partial();self.later()
        code='''import os,sys
from memleaf import Memleaf
class B:
 single_pass_safe=True
 def complete(self,*a,**k):os._exit(74)
Memleaf(sys.argv[1]).recover_incremental_partial(sys.argv[2],model=B())
'''
        result=subprocess.run([sys.executable,"-c",code,str(self.s.vault.root),first["run_id"]],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,74,result.stderr.decode())
        b=Backend()
        final=Memleaf(self.s.vault.root).resume_incremental_run(first["run_id"],backend=b)
        self.assertEqual(final["execution_status"],"failed")
        self.assertEqual(final["reserved_requests"],2)
        self.assertEqual(final["commit"]["counts"]["no_memory"],1)
        self.assertFalse(b.calls)

    def test_malformed_recovery_mode_rejected_as_value_error(self):
        first=self.partial()
        with self.assertRaises(ValueError):self.s.recover_incremental_partial(first["run_id"],mode=[])
        with self.assertRaises(ValueError):self.s.recover_incremental_partial(first["run_id"],context_memory_ids=[{}])

    def test_old_partial_without_basis_not_reconstructed(self):
        first=self.partial();state=self.ledger();run=load_run(state,first["run_id"]);run.pop("partial_basis")
        from memleaf.incremental_run_state import save_run
        with self.s.vault.lock():save_run(self.s,state,run)
        self.later()
        with self.assertRaisesRegex(ValueError,"basis_unavailable"):
            self.s.recover_incremental_partial(first["run_id"],model=Backend(output(self.create())))

    def test_ledger_failure_boundaries_keep_child_identity_and_no_extra_dispatch(self):
        # Test actual ledger replacements before/after application, not only a
        # happy-path helper. Each subcase has its own disposable Vault.
        import tempfile
        for after in (False,True):
            for boundary in (1,2,3,4,5,6):
                with self.subTest(after=after,boundary=boundary),tempfile.TemporaryDirectory() as root:
                    self.s=Memleaf.initialize(Path(root)/"vault");self.capture()
                    first=self.partial(scalar=True)
                    from memleaf.locking import atomic_write_json as real_write
                    hits=[0]
                    def write(path,value):
                        if Path(path)==self.s.vault.processed_state_path:
                            hits[0]+=1
                            if hits[0]==boundary:
                                if after:real_write(path,value)
                                raise OSError("fixture ledger boundary")
                        return real_write(path,value)
                    try:
                        with patch("memleaf.incremental_partial.save_run", wraps=__import__("memleaf.incremental_run_state",fromlist=["save_run"]).save_run), \
                             patch("memleaf.incremental_run_state.atomic_write_json",side_effect=write), \
                             patch("memleaf.incremental_journal.atomic_write_json",side_effect=write), \
                             patch("memleaf.incremental_execution.atomic_write_json",side_effect=write):
                            self.s.recover_incremental_partial(first["run_id"],mode="repair")
                    except (OSError,IncrementalRunError):
                        pass
                    run=load_run(self.ledger(),first["run_id"])
                    child=load_work(self.ledger(),run["commit_work_id"]) if run.get("partial_used") else None
                    ids={o["memory_id"] for o in child["operations"] if o["action"]=="CREATE"} if child else set()
                    self.s=Memleaf(self.s.vault.root)
                    final=self.s.recover_incremental_partial(first["run_id"],mode="repair")
                    self.assertEqual(final["execution_status"],"completed")
                    self.assertEqual(final["reserved_requests"],1)
                    self.assertEqual(final["model_calls_this_invocation"],0)
                    actual={o["memory_id"] for o in final["commit"]["operations"] if o["action"]=="CREATE"}
                    if ids:self.assertEqual(ids,actual)
                    self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)
