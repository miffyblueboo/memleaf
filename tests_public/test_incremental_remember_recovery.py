from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf.incremental_execution import IncrementalRunError, run_incremental
from memleaf.incremental_journal import protected_turn_keys, owned_turn_keys, load_work
from memleaf.incremental_run_state import KEY, load_run
from memleaf.incremental_selection import bind_selection
from memleaf.index import turn_key
from memleaf.llm import ModelError
from memleaf.locking import atomic_write_json
from memleaf.config import save_config
from memleaf.extraction_work_state import _budget_path


class SelectedRecoveryTests(IncrementalFixture):
    def args(self, **changes):
        preview = self.s.preview_incremental(source="hermes", session_id="s", turn_id="t")
        args = dict(source="hermes", session_id="s", turn_id="t", intent_id="retain-1",
                    selected_source_refs=[preview["source_refs"][0]["source_ref"]],
                    retention_request="Keep only the task.")
        args.update(changes)
        return args

    def prepare(self):
        args = self.args()
        selection, text = bind_selection(args["intent_id"], args["selected_source_refs"], args["retention_request"])
        result = run_incremental(self.s, source="hermes", session_id="s", turn_id="t",
                                 selection=selection, retention_request=text)
        return args, result

    def test_transport_retry_requires_explicit_recovery_and_uses_same_request(self):
        backend = Backend(ModelError(code="model_timeout"), output(self.create()))
        args = self.args()
        first = self.s.remember_incremental(model=backend, **args)
        again = self.s.remember_incremental(model=backend, **args)
        self.assertEqual(len(backend.calls), 1)
        final = self.s.remember_incremental(model=backend, recover=True, **args)
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertEqual(first["run_id"], final["run_id"])
        self.assertEqual(final["reservations"], 2)
        self.assertEqual(backend.calls[0][0], backend.calls[1][0])
        self.assertEqual(final["execution_status"], "completed")

    def test_invalid_responses_cannot_reset_budget_via_facade_or_run_id(self):
        args = self.args()
        backend = Backend("{", "{")
        result = self.s.remember_incremental(model=backend, **args)
        self.assertEqual(result["execution_status"], "failed")
        self.s.remember_incremental(model=backend, recover=True, **args)
        self.s.resume_incremental_run(result["run_id"], backend=backend)
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(len(self.ledger()[KEY]), 1)

    def test_saved_response_resumes_with_no_backend(self):
        args = self.args()
        backend = Backend(output(self.create()))
        with patch("memleaf.incremental_execution.apply_incremental", side_effect=OSError("fixture")):
            with self.assertRaises(IncrementalRunError): self.s.remember_incremental(model=backend, **args)
        with patch("memleaf.incremental_runtime._resolve", side_effect=AssertionError("do not resolve")):
            result = self.s.remember_incremental(**args)
        self.assertEqual(result["model_calls_this_invocation"], 0)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(len(backend.calls), 1)

    def test_write_failure_recovers_same_id_with_no_new_semantic_call(self):
        args = self.args(); backend = Backend(output(self.create()))
        with patch("memleaf.memory_writer.MemoryWriter.write_frozen_unlocked", side_effect=OSError("fixture")):
            with self.assertRaises(IncrementalRunError) as caught:
                self.s.remember_incremental(model=backend, **args)
        work = load_work(self.ledger(), caught.exception.result["commit_work_id"])
        expected = work["operations"][0]["memory_id"]
        self.assertIn(turn_key("t"), owned_turn_keys(self.ledger(), "hermes", "s"))
        result = self.s.remember_incremental(**args)
        self.assertEqual(result["commit"]["operations"][0]["memory_id"], expected)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 1)
        self.assertNotIn(turn_key("t"), owned_turn_keys(self.ledger(), "hermes", "s"))

    def test_index_failure_does_not_repeat_history_or_model(self):
        self.target(); args=self.args(priority_memory_ids=["mem-old"])
        backend=Backend(output(self.update()))
        with patch.object(self.s, "_rebuild_index_unlocked", side_effect=OSError("fixture")):
            with self.assertRaises(IncrementalRunError): self.s.remember_incremental(model=backend, **args)
        result=self.s.remember_incremental(**args)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(self.s.read("mem-old").status, "completed")
        self.assertEqual(len(self.hist()), 1)
        self.assertEqual(len(backend.calls), 1)

    def test_known_source_time_does_not_rebase_at_retry(self):
        args, first=self.prepare()
        original=load_run(self.ledger(), first["run_id"])["request"]
        with patch("memleaf.incremental_execution.utc_now", return_value="2027-02-20T00:00:00Z"):
            backend=Backend(output(self.create()))
            self.s.remember_incremental(model=backend, recover=True, **args)
        self.assertEqual(backend.calls[0][0], original["user"])
        self.assertEqual(json.loads(backend.calls[0][0])["evidence"][0]["source_time"], "2026-09-17T10:00:00+08:00")

    def test_recording_revocation_blocks_before_dispatch(self):
        args=self.args()
        state=self.ledger(); state["sessions"]["hermes/s"]["capture_policy"]={"enabled":False}
        atomic_write_json(self.s.vault.processed_state_path,state)
        backend=Backend(output(self.create()))
        with self.assertRaisesRegex(ValueError, "source_recording_revoked"):
            self.s.remember_incremental(model=backend, **args)
        self.assertFalse(backend.calls)

    def test_budget_loss_cannot_reopen_same_intent(self):
        args=self.args()
        first=self.s.remember_incremental(model=Backend(ModelError(code="model_timeout")), **args)
        _budget_path(self.s.vault).unlink()
        backend=Backend(output(self.create()))
        result=self.s.remember_incremental(model=backend, recover=True, **args)
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(result["run_id"], first["run_id"])
        self.assertFalse(backend.calls)

    def test_target_edit_during_call_blocks_old_response(self):
        self.target()
        def change():
            path=next(self.s.vault.knowledge_path.rglob("*.md"))
            path.write_text(path.read_text().replace("Deliver the report.", "A different task."))
            return output(self.update())
        result=self.s.remember_incremental(model=Backend(change), **self.args(priority_memory_ids=["mem-old"]))
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(self.s.read("mem-old").body, "A different task.")

    def test_pending_sources_protected_without_automatic_terminal_ownership(self):
        args, result=self.prepare()
        self.assertIn(turn_key("t"), protected_turn_keys(self.ledger(), "hermes", "s"))
        self.s.remember_incremental(model=Backend(output(self.create())), recover=True, **args)
        self.assertNotIn(turn_key("t"), protected_turn_keys(self.ledger(), "hermes", "s"))
        self.assertFalse(self.ledger()["sessions"]["hermes/s"].get("processed_turns"))

    def test_native_no_change_is_read_only_and_does_not_create_copy(self):
        path=Path(self.temp.name)/"notes.md"; original="# Notes\n\nAtlas task completed\n"; path.write_text(original)
        config=self.s.vault.config(); config["native_sources"]={"notes":{"path":str(path),"agent":"hermes","share":False,"enabled":True}}
        save_config(self.s.vault.config_path,config)
        backend=Backend(output({"action":"NO_CHANGE","evidence":["e1"],"target":"m1"}))
        result=self.s.remember_incremental(model=backend, **self.args())
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["commit"]["counts"]["no_change"], 1)
        self.assertTrue(result["commit"]["operations"][0]["native"])
        self.assertFalse(self.s.vault.list_markdown("knowledge"))
        self.assertEqual(path.read_text(),original)

    def test_forget_cancels_pending_authorization_and_late_response(self):
        self.target(); args=self.args(priority_memory_ids=["mem-old"], retention_request="Keep the task, private selector")
        def forget():
            self.s.forget_memory("mem-old")
            return output(self.update())
        result=self.s.remember_incremental(model=Backend(forget), **args)
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertNotIn(args["retention_request"], self.s.vault.processed_state_path.read_text())
        self.assertIsNone(self.s.read("mem-old", include_history=True))
        self.assertEqual(self.s.remember_incremental(**args)["execution_status"], "cancelled")

    def test_new_real_intent_can_remember_after_forget(self):
        args=self.args(); result=self.s.remember_incremental(model=Backend(output(self.create())), **args)
        mid=result["commit"]["operations"][0]["memory_id"]
        self.s.forget_memory(mid)
        self.assertEqual(self.s.remember_incremental(**args)["execution_status"], "cancelled")
        result=self.s.remember_incremental(model=Backend(output(self.create())), **{**args,"intent_id":"keep-again"})
        self.assertEqual(result["execution_status"], "completed")
        self.assertNotEqual(result["commit"]["operations"][0]["memory_id"], mid)

    def test_request_is_redacted_before_dispatch_or_persistence(self):
        secret="sk-"+"a"*24
        backend=Backend(ModelError(code="model_timeout"))
        self.s.remember_incremental(model=backend, **self.args(retention_request="Keep selection with "+secret))
        self.assertNotIn(secret, backend.calls[0][0])
        self.assertNotIn(secret, self.s.vault.processed_state_path.read_text())
        self.assertIn("[REDACTED_TOKEN]",backend.calls[0][0])

    def test_partial_is_not_replanned_by_recover(self):
        args=self.args(); backend=Backend(output(self.no_memory("e1")))
        self.s.remember_incremental(model=backend, **args)
        self.s.remember_incremental(model=backend, recover=True, **args)
        self.assertEqual(len(backend.calls),1)

    def test_two_callers_same_authorization_share_owner(self):
        args=self.args(); started=threading.Event(); release=threading.Event(); results=[]; errors=[]
        def wait():
            started.set(); self.assertTrue(release.wait(5)); return output(self.create())
        def invoke():
            try: results.append(self.s.remember_incremental(model=Backend(wait), **args))
            except BaseException as exc: errors.append(exc)
        worker=threading.Thread(target=invoke);worker.start();self.assertTrue(started.wait(5))
        try:
            with self.assertRaisesRegex(ValueError,"incremental_model_busy"):
                self.s.remember_incremental(model=Backend(output(self.create())), **args)
        finally: release.set();worker.join(10)
        self.assertFalse(worker.is_alive()); self.assertFalse(errors)
        self.assertEqual(results[0]["execution_status"],"completed")
        self.assertEqual(len(self.ledger()[KEY]),1)

    def test_saved_response_survives_actual_process_exit(self):
        args,result=self.prepare()
        code='''
import os,sys
from memleaf import Memleaf
import memleaf.incremental_execution as execution
from memleaf.incremental_run_state import save_run
class Backend:
 single_pass_safe=True
 def complete(self,*args,**kwargs): return sys.argv[3]
def stop(service,processed,run):
 save_run(service,processed,run)
 if run['status']=='response_ready': os._exit(73)
execution.save_run=stop
Memleaf(sys.argv[1]).resume_incremental_run(sys.argv[2],backend=Backend())
'''
        child=subprocess.run([sys.executable,"-c",code,str(self.s.vault.root),result["run_id"],output(self.create())],capture_output=True,text=True,timeout=15)
        self.assertEqual(child.returncode,73,child.stderr)
        self.s=Memleaf(self.s.vault.root)
        final=self.s.remember_incremental(**args)
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["model_calls_this_invocation"],0)
        self.assertEqual(final["reservations"],1)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)

    def test_selection_lifecycle_validators_reject_mismatched_control_state(self):
        from memleaf.incremental_run_state import save_run
        args,result=self.prepare(); state=self.ledger(); run=load_run(state,result["run_id"])
        for key,value in (("request_kind","automatic"),("authorization_intent","other"),("request_kind",[])):
            altered=dict(run);altered[key]=value
            with self.subTest(key=key,value=value):
                with self.assertRaises(ValueError): save_run(self.s, self.ledger(), altered)
        self.assertEqual(load_run(self.ledger(),result["run_id"])["authorization_intent"],args["intent_id"])
