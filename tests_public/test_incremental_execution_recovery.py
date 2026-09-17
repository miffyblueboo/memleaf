from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf.incremental_execution import IncrementalRunError
from memleaf.incremental_run_state import KEY, OWNER, load_run
from memleaf.extraction_work_state import _budget_path


class ExecutionRecoveryTests(IncrementalFixture):
    def execute(self, backend=None, **kwargs):
        return self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=backend, **kwargs)

    def only_run(self):
        state=self.ledger(); self.assertEqual(len(state[KEY]),1)
        return load_run(state,next(iter(state[KEY])))

    def test_runtime_save_crash_matrix(self):
        import memleaf.incremental_run_state as state
        original=state.atomic_write_json
        # Prepare the initial authorization so every injected error has a
        # durable recovery ID. The test includes owner, dispatch, reply,
        # committing, and final result saves, before and after replacement.
        for at in range(1,6):
            for after in (False,True):
                with self.subTest(at=at,after=after):
                    self.s=Memleaf.initialize(Path(self.temp.name)/f"case-{at}-{after}")
                    self.capture(); request=self.execute(); rid=request["run_id"]
                    backend=Backend(*[output(self.create(),self.no_memory())]*2)
                    count=[0]
                    def fail(path,value,*args,**kwargs):
                        count[0]+=1
                        if count[0]==at and not after: raise OSError("disk before save")
                        result=original(path,value,*args,**kwargs)
                        if count[0]==at and after: raise OSError("disk after save")
                        return result
                    with patch.object(state,"atomic_write_json",new=fail):
                        with self.assertRaises(IncrementalRunError):
                            self.s.resume_incremental_run(rid,backend=backend)
                    final=self.s.resume_incremental_run(rid,backend=backend)
                    self.assertEqual(final["execution_status"],"completed")
                    self.assertLessEqual(len(backend.calls),2)
                    self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)
                    self.assertNotIn(OWNER,self.ledger())

    def test_reservation_written_before_runtime_save_is_not_free(self):
        import memleaf.extraction_work_state as state
        original=state.atomic_write_json
        def fail(path,value,*args,**kwargs):
            original(path,value,*args,**kwargs)
            raise OSError("after durable reservation")
        request=self.execute(); backend=Backend(output(self.create(),self.no_memory()))
        with patch.object(state,"atomic_write_json",new=fail):
            with self.assertRaises(IncrementalRunError):
                self.s.resume_incremental_run(request["run_id"],backend=backend)
        self.assertEqual(len(backend.calls),0)
        final=self.s.resume_incremental_run(request["run_id"],backend=backend)
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["reserved_requests"],2)
        self.assertEqual(len(backend.calls),1)

    def test_response_survives_real_process_exit_without_second_call(self):
        request=self.execute()
        script=r'''
import os, sys
from memleaf import Memleaf
import memleaf.incremental_run_state as state
original=state.save_run
# Patch the runtime's imported name: response is already durably written.
import memleaf.incremental_execution as execution
def stop(service, processed, run):
    original(service,processed,run)
    if run['status']=='response_ready': os._exit(73)
execution.save_run=stop
class Backend:
    single_pass_safe=True
    def complete(self,prompt,**kwargs): return sys.argv[3]
Memleaf(sys.argv[1]).resume_incremental_run(sys.argv[2],backend=Backend())
'''
        child=subprocess.run([sys.executable,"-c",script,str(self.s.vault.root),request["run_id"],output(self.create(),self.no_memory())],
                             capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,73,child.stderr)
        self.s=Memleaf(self.s.vault.root)
        final=self.s.resume_incremental_run(request["run_id"])
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["model_calls_this_invocation"],0)
        self.assertEqual(final["reserved_requests"],1)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)

    def test_dead_dispatch_owner_consumes_unknown_attempt(self):
        request=self.execute()
        script=r'''
import os,sys
from memleaf import Memleaf
class Backend:
    single_pass_safe=True
    def complete(self,*args,**kwargs): os._exit(74)
Memleaf(sys.argv[1]).resume_incremental_run(sys.argv[2],backend=Backend())
'''
        child=subprocess.run([sys.executable,"-c",script,str(self.s.vault.root),request["run_id"]],capture_output=True,timeout=10)
        self.assertEqual(child.returncode,74)
        final=self.s.resume_incremental_run(request["run_id"],backend=Backend(output(self.create(),self.no_memory())))
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["reserved_requests"],2)
        self.assertEqual(final["uncertain_attempts"],1)

    def test_two_threads_cannot_dispatch_simultaneously(self):
        started=threading.Event(); release=threading.Event(); results=[]
        def waiting():
            started.set(); self.assertTrue(release.wait(3));return output(self.create(),self.no_memory())
        backend=Backend(waiting)
        worker=threading.Thread(target=lambda:results.append(self.execute(backend)))
        worker.start(); self.assertTrue(started.wait(3))
        try:
            with self.assertRaisesRegex(ValueError,"incremental_model_busy"): self.execute(Backend())
        finally:
            release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["execution_status"],"completed")
        self.assertEqual(len(backend.calls),1)

    def test_other_process_cannot_steal_live_owner(self):
        def check():
            script='''from memleaf import Memleaf
import sys
try: Memleaf(sys.argv[1]).resume_incremental_run(sys.argv[2])
except ValueError as error: print(str(error))
'''
            child=subprocess.run([sys.executable,"-c",script,str(self.s.vault.root),self.only_run()["run_id"]],capture_output=True,text=True,timeout=10)
            self.assertEqual(child.returncode,0,child.stderr)
            self.assertIn("incremental_model_busy",child.stdout)
            return output(self.create(),self.no_memory())
        self.assertEqual(self.execute(Backend(check))["execution_status"],"completed")

    def test_release_receipt_failure_allows_same_process_recovery(self):
        import memleaf.incremental_execution as execution
        original=execution.atomic_write_json
        def fail(path,value,*args,**kwargs): raise OSError("release disk failure")
        backend=Backend(output(self.create(),self.no_memory()))
        with patch.object(execution,"atomic_write_json",new=fail):
            with self.assertRaises(IncrementalRunError) as caught: self.execute(backend)
        final=self.s.resume_incremental_run(caught.exception.result["run_id"])
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(len(backend.calls),1)
        # A genuinely different run must not be held by a stale same-process token.
        self.capture("next",seq=3)
        result=self.s.run_incremental(source="hermes",session_id="s",turn_id="next",backend=None)
        self.assertEqual(result["code"],"backend_required")

    def test_forget_between_ready_and_commit_is_fenced_under_commit_lock(self):
        import memleaf.incremental_execution as execution
        self.target(); original=execution.apply_incremental
        def forget_then_commit(*args,**kwargs):
            self.s.forget_memory("mem-old")
            return original(*args,**kwargs)
        with patch.object(execution,"apply_incremental",new=forget_then_commit):
            result=self.execute(Backend(output(self.update(),self.no_memory())),priority_memory_ids=["mem-old"])
        self.assertEqual(result["execution_status"],"cancelled")
        self.assertIsNone(self.s.read("mem-old",include_history=True))
        self.assertNotIn("response",self.only_run())

    def test_budget_file_loss_cannot_grant_extra_attempts(self):
        from memleaf.llm.base import ModelError
        request=self.execute(Backend(ModelError(code="model_timeout")))
        _budget_path(self.s.vault).unlink()
        backend=Backend(output(self.create(),self.no_memory()))
        final=self.s.resume_incremental_run(request["run_id"],backend=backend)
        self.assertEqual(final["execution_status"],"blocked")
        self.assertEqual(len(backend.calls),0)

    def test_runtime_source_is_not_cleaned_while_model_runs(self):
        from memleaf.process_journal import ProcessJournal
        from memleaf.incremental_journal import protected_turn_keys
        def inspect():
            run=self.only_run()
            self.assertIn(run["turn_key"],protected_turn_keys(self.ledger(),"hermes","s"))
            return output(self.create(),self.no_memory())
        result=self.execute(Backend(inspect))
        self.assertEqual(result["execution_status"],"completed")
        self.assertNotIn(self.only_run()["turn_key"],protected_turn_keys(self.ledger(),"hermes","s"))

    def test_genuinely_new_revision_is_not_old_terminal_decision(self):
        first=self.execute(Backend(output(self.no_memory("e1"),self.no_memory())))
        self.s.capture("hermes","s","t","user","Track the new request",message_id="t-u",message_revision="2",
                       previous_message_revision="1",source_sequence=1,source_time="2026-09-17T11:00:00+08:00")
        self.s.capture("hermes","s","t","assistant","Acknowledged new request",message_id="t-a",message_revision="2",
                       previous_message_revision="1",source_sequence=2,final=True)
        second=self.execute(Backend(output(self.create(),self.no_memory())))
        self.assertEqual(second["execution_status"],"completed")
        self.assertNotEqual(first["run_id"],second["run_id"])

    def test_native_http_adapter_makes_one_post_and_retains_settings(self):
        from memleaf.llm.openai_compatible import OpenAICompatibleBackend
        backend=OpenAICompatibleBackend(base_url="https://api.deepseek.com",api_key="fixture-only",model="fixture-model",
                                        thinking={"single_pass":"disabled"})
        seen=[]
        def post(url,payload,headers,**kwargs):
            seen.append(payload)
            return {"choices":[{"message":{"content":output(self.create(),self.no_memory())},"finish_reason":"stop"}]}
        with patch.object(backend,"_post_json",side_effect=post): final=self.execute(backend)
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(len(seen),1)
        self.assertEqual([m["role"] for m in seen[0]["messages"]],["system","user"])
        self.assertEqual(seen[0]["response_format"],{"type":"json_object"})
        self.assertEqual(seen[0]["model"],"fixture-model")
        self.assertNotIn("fixture-only",self.s.vault.processed_state_path.read_text())

    def test_budget_finalization_failure_is_visible_and_repairable(self):
        import memleaf.incremental_execution as execution
        backend=Backend(output(self.create(),self.no_memory()))
        with patch.object(execution,"complete_turn_budget",return_value=False):
            first=self.execute(backend)
        self.assertEqual(first["execution_status"],"completed")
        self.assertFalse(first["budget_finalized"])
        again=self.s.resume_incremental_run(first["run_id"])
        self.assertTrue(again["budget_finalized"])
        self.assertEqual(again["model_calls_this_invocation"],0)
        self.assertEqual(len(backend.calls),1)

    def test_initial_receipt_save_failure_can_repeat_original_arguments(self):
        import memleaf.incremental_run_state as state
        original=state.atomic_write_json
        for after in (False, True):
            with self.subTest(after=after):
                self.s=Memleaf.initialize(Path(self.temp.name)/f"first-{after}"); self.capture()
                def fail(path,value,*args,**kwargs):
                    if after: original(path,value,*args,**kwargs)
                    raise OSError("initial save")
                with patch.object(state,"atomic_write_json",new=fail):
                    with self.assertRaises(OSError): self.execute()
                final=self.execute(Backend(output(self.create(),self.no_memory())))
                self.assertEqual(final["execution_status"],"completed")
                self.assertEqual(final["reserved_requests"],1)

    def test_finished_run_cache_tracks_forget(self):
        result=self.execute(Backend(output(self.create(),self.no_memory())))
        identity=result["commit"]["operations"][0]["memory_id"]
        self.s.forget_memory(identity)
        result=self.s.resume_incremental_run(result["run_id"])
        self.assertEqual(result["execution_status"],"cancelled")
        self.assertIsNone(result["commit"])

    def test_receipt_capacity_backpressure_preserves_first_work(self):
        import memleaf.incremental_run_state as state
        first=self.execute()
        self.capture("next",seq=3)
        with patch.object(state,"MAX_RUNS",1):
            with self.assertRaisesRegex(ValueError,"runs_full"):
                self.s.run_incremental(source="hermes",session_id="s",turn_id="next",backend=Backend())
        self.assertEqual(len(self.ledger()[KEY]),1)
        self.assertEqual(self.only_run()["run_id"],first["run_id"])

    def test_legacy_processing_already_inflight_is_not_overtaken(self):
        from memleaf.models import utc_now
        state=self.ledger()
        state["sessions"]["hermes/s"]["processing"]={"status":"processing","owner_pid":os.getpid(),
            "token":"legacy","started_at":utc_now()}
        self.s.vault.processed_state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError,"legacy_processing_busy"):
            self.execute(Backend())
        self.assertNotIn(KEY,self.ledger())

    def test_initial_input_too_large_never_calls_backend(self):
        backend=Backend()
        self.s=Memleaf.initialize(Path(self.temp.name)/"large")
        self.capture(content="long "*30000)
        with self.assertRaises(ValueError):self.execute(backend)
        self.assertEqual(len(backend.calls),0)
        self.assertNotIn(KEY,self.ledger())

    def test_generic_provider_error_text_not_persisted(self):
        backend=Backend(RuntimeError("secret-fixture-exception"))
        result=self.execute(backend)
        self.assertEqual(result["code"],"model_failed")
        self.assertNotIn("secret-fixture-exception",self.s.vault.processed_state_path.read_text())

    def test_raw_source_cleanup_does_not_need_new_model_for_committed_recovery(self):
        import memleaf.incremental_execution as execution
        original=execution.save_run
        def fail(service, processed, run):
            if run["status"]=="completed":raise OSError("before final run receipt")
            return original(service,processed,run)
        with patch.object(execution,"save_run",new=fail):
            with self.assertRaises(IncrementalRunError) as caught:
                self.execute(Backend(output(self.create(),self.no_memory())))
        self.s.vault.session_path("hermes","s").unlink()
        final=self.s.resume_incremental_run(caught.exception.result["run_id"])
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["model_calls_this_invocation"],0)


if __name__ == "__main__": unittest.main()
