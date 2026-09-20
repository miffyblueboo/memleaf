from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from memleaf import Memleaf
from memleaf.incremental_execution import IncrementalRunError
from memleaf.incremental_run_state import KEY, OWNER, load_run, save_run
from memleaf.extraction_work_state import extraction_work_id, reserve_model_request, _budget_path
from memleaf.incremental_commit import _window
from memleaf.index import turn_key
from memleaf.llm.base import ModelError


class Backend:
    single_pass_safe = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        result = self.responses.pop(0)
        if callable(result):
            return result()
        if isinstance(result, Exception):
            raise result
        return result


def output(*items):
    return json.dumps({"items": list(items)})


class ExecutionTests(IncrementalFixture):
    def execute(self, backend=None, **kwargs):
        return self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=backend, **kwargs)

    def runs(self):
        state = self.ledger()
        return [load_run(state, key) for key in state.get(KEY, {})]

    def test_normal_one_call_create_read_and_cached_replay(self):
        backend = Backend(output(self.create(), self.no_memory()))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["model_calls_this_invocation"], 1)
        self.assertEqual(result["reserved_requests"], 1)
        identity = result["commit"]["operations"][0]["memory_id"]
        self.assertEqual(self.s.read(identity).body, "Deliver the report.")
        again = self.execute(backend)
        self.assertEqual(again["model_calls_this_invocation"], 0)
        self.assertEqual(again["commit"]["operations"], result["commit"]["operations"])
        self.assertEqual(len(backend.calls), 1)
        self.assertNotIn("request", self.runs()[0]); self.assertNotIn("response", self.runs()[0])
        self.assertNotIn(OWNER, self.ledger())

    def test_turn_level_create_does_not_require_assistant_disposition(self):
        result = self.execute(Backend(output(self.create())))
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["commit"]["coverage_status"], "complete")
        self.assertEqual(result["commit"]["turn_disposition"], "memory")
        entry = self.ledger()["sessions"]["hermes/s"]["processed_turns"][0]
        self.assertEqual(entry["incremental_disposition"], "memory")

    def test_turn_level_no_memory_without_evidence_settles_and_is_persisted(self):
        result = self.execute(Backend(output({"action": "NO_MEMORY"})))
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["commit"]["counts"]["no_memory"], 1)
        self.assertEqual(result["commit"]["turn_disposition"], "no_memory")
        entry = self.ledger()["sessions"]["hermes/s"]["processed_turns"][0]
        self.assertEqual(entry["incremental_disposition"], "no_memory")
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 0)

    def test_orion_choice_uses_only_current_complete_turn_after_prior_no_memory(self):
        service = Memleaf.initialize(Path(self.temp.name) / "orion-vault")
        service.capture("hermes", "choice", "options", "user", "Orion用什么数据库好？",
                        message_id="o-u", source_sequence=1)
        service.capture("hermes", "choice", "options", "assistant",
                        "A. MySQL；B. Oracle。", message_id="o-a", source_sequence=2, final=True)
        first = service.run_incremental(source="hermes", session_id="choice", turn_id="options",
                                        backend=Backend(output({"action": "NO_MEMORY"})))
        self.assertEqual(first["execution_status"], "completed")
        self.assertEqual(first["commit"]["turn_disposition"], "no_memory")
        self.assertEqual(len(service.vault.list_markdown("knowledge")), 0)

        service.capture("hermes", "choice", "chosen", "user", "那就用A吧。",
                        message_id="c-u", source_sequence=3)
        service.capture("hermes", "choice", "chosen", "assistant",
                        "好的，Orion数据库就使用MySQL。", message_id="c-a", source_sequence=4, final=True)

        outer = self
        class InspectBackend:
            single_pass_safe = True
            def __init__(self):
                self.calls = []
            def complete(self, prompt, **kwargs):
                self.calls.append((prompt, kwargs))
                payload = json.loads(prompt)
                outer.assertEqual([(e["role"], e["text"]) for e in payload["evidence"]],
                                  [("user", "那就用A吧。"), ("assistant", "好的，Orion数据库就使用MySQL。")])
                outer.assertNotIn("Oracle", prompt)
                return output({"action": "CREATE", "evidence": ["e1", "e2"], "at": "e1",
                               "memory": {"type": "fact", "scope": "global",
                                          "title": "Orion 数据库", "body": "Orion 数据库使用 MySQL。"}})

        backend = InspectBackend()
        result = service.run_incremental(source="hermes", session_id="choice", turn_id="chosen", backend=backend)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["commit"]["turn_disposition"], "memory")
        self.assertEqual(len(backend.calls), 1)
        identity = result["commit"]["operations"][0]["memory_id"]
        self.assertEqual(service.read(identity).body, "Orion 数据库使用 MySQL。")
        ledger = json.loads(service.vault.processed_state_path.read_text(encoding="utf-8"))
        entries = ledger["sessions"]["hermes/choice"]["processed_turns"]
        self.assertEqual([e["incremental_disposition"] for e in entries], ["no_memory", "memory"])

    def test_update_same_id_preserves_fields(self):
        self.target()
        result = self.execute(Backend(output(self.update(), self.no_memory())), priority_memory_ids=["mem-old"])
        self.assertEqual(result["execution_status"], "completed")
        current = self.s.read("mem-old")
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.due_date, "2026-09-20")
        self.assertEqual(current.extra["custom"], {"keep": True})
        self.assertEqual(len(self.hist()), 1)

    def test_no_change_and_no_memory_settle_without_rewrite(self):
        self.target(); before = self.s.read("mem-old").to_markdown()
        result = self.execute(Backend(output({"action":"NO_CHANGE","evidence":["e1"],"target":"m1"}, self.no_memory())),
                              priority_memory_ids=["mem-old"])
        self.assertEqual(result["commit"]["counts"]["no_change"], 1)
        self.assertEqual(self.s.read("mem-old").to_markdown(), before)
        self.assertEqual(len(self.hist()), 0)

    def test_all_no_memory_is_a_terminal_valid_result(self):
        result = self.execute(Backend(output(self.no_memory("e1"), self.no_memory())))
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["commit"]["counts"]["no_memory"], 2)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 0)

    def test_top_level_invalid_json_retries_once(self):
        backend = Backend("not JSON", output(self.create(), self.no_memory()))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["model_calls_this_invocation"], 2)
        self.assertEqual(backend.calls[0][0], backend.calls[1][0])
        self.assertEqual(backend.calls[0][1]["purpose"], "single_pass")
        self.assertIn("上次请求", backend.calls[1][1]["system"])
        self.assertNotIn("not JSON", backend.calls[1][0])

    def test_repeated_invalid_never_dispatches_a_third_time(self):
        backend = Backend("{", "{")
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["code"], "request_budget_exhausted")
        self.assertEqual(result["reserved_requests"], 2)
        result = self.s.resume_incremental_run(result["run_id"], backend=backend)
        self.assertEqual(result["model_calls_this_invocation"], 0)
        self.assertEqual(len(backend.calls), 2)

    def test_empty_and_oversize_are_bounded(self):
        backend = Backend("", "x" * (128*1024+1))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(len(backend.calls), 2)
        self.assertNotIn("response", self.runs()[0])

    def test_partial_valid_result_does_not_semantically_retry(self):
        backend = Backend(output(self.create(), {"action":"UPDATE","evidence":["e2"],"target":"m999","patch":{"body":"wrong"}}))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(result["commit"]["counts"]["committed"], 1)
        self.s.resume_incremental_run(result["run_id"], backend=backend)
        self.assertEqual(len(backend.calls), 1)

    def test_deferred_is_not_protocol_failure(self):
        backend = Backend(output({"action":"DEFERRED","evidence":["e1"],"reason":"missing_identity","need":"Identify the task"},self.no_memory()))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertEqual(len(backend.calls), 1)

    def test_transport_retry_explicit_and_persisted(self):
        backend = Backend(ModelError("secret", code="model_timeout"), output(self.create(), self.no_memory()))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "retryable")
        self.assertEqual(len(backend.calls), 1)
        self.s = Memleaf(self.s.vault.root)
        final = self.s.resume_incremental_run(result["run_id"], backend=backend)
        self.assertEqual(final["execution_status"], "completed")
        self.assertEqual(final["reserved_requests"], 2)
        self.assertNotIn("secret", self.s.vault.processed_state_path.read_text())

    def test_auth_error_has_no_automatic_or_manual_retry(self):
        backend = Backend(ModelError("api-secret", code="model_auth_failed"))
        result = self.execute(backend)
        self.assertEqual(result["execution_status"], "failed")
        self.s.resume_incremental_run(result["run_id"], backend=backend)
        self.assertEqual(len(backend.calls), 1)
        self.assertNotIn("api-secret", self.s.vault.processed_state_path.read_text())

    def test_missing_or_unsafe_backend_spends_nothing(self):
        result = self.execute()
        self.assertEqual(result["code"], "backend_required")
        backend = Backend(); backend.single_pass_safe = False
        result = self.s.resume_incremental_run(result["run_id"], backend=backend)
        self.assertEqual(result["code"], "backend_not_single_dispatch")
        self.assertEqual(result["reserved_requests"], 0)
        self.assertFalse(_budget_path(self.s.vault).exists())

    def test_old_precommit_protocol_is_blocked_without_dispatch(self):
        first = self.execute()
        self.assertEqual(first["code"], "backend_required")
        with self.s.vault.lock():
            state = self.ledger()
            run = load_run(state, first["run_id"])
            run.pop("protocol_digest", None)
            save_run(self.s, state, run)
        backend = Backend(output(self.create()))
        final = self.s.resume_incremental_run(first["run_id"], backend=backend)
        self.assertEqual(final["execution_status"], "blocked")
        self.assertEqual(final["code"], "protocol_upgrade_required")
        self.assertEqual(backend.calls, [])
        self.assertEqual(final["reserved_requests"], 0)

    def test_changing_arguments_cannot_refresh_allowance(self):
        self.execute(Backend(ModelError(code="model_timeout")))
        with self.assertRaisesRegex(ValueError, "arguments_changed"):
            self.execute(Backend(), scope="project:Atlas")

    def test_source_changed_during_request_cannot_commit(self):
        def change():
            self.revise()
            return output(self.create(), self.no_memory())
        result = self.execute(Backend(change))
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 0)
        self.assertNotIn("response", self.runs()[0])

    def test_target_changed_during_request_cannot_commit(self):
        self.target()
        def change():
            m = self.s.read("mem-old"); m.body = "New user edit"; self.s.write_memory(m)
            return output(self.update(), self.no_memory())
        result = self.execute(Backend(change), priority_memory_ids=["mem-old"])
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(self.s.read("mem-old").body, "New user edit")

    def test_same_source_legacy_budget_is_not_reset(self):
        turn, _ = _window(self.s,"hermes","s",turn_key("t"))
        work_id = extraction_work_id(turn,request_kind="automatic",intent_id="automatic")
        turn_id = f"hermes/s/{turn.turn_key}"
        for _ in range(3): reserve_model_request(self.s.vault,work_id=work_id,turn_id=turn_id)
        backend = Backend()
        result = self.execute(backend)
        self.assertEqual(result["code"], "request_budget_exhausted")
        self.assertEqual(result["reserved_requests"], 3)
        self.assertEqual(len(backend.calls), 0)

    def test_ambiguous_legacy_job_migration_blocks(self):
        turn, _ = _window(self.s,"hermes","s",turn_key("t"))
        reserve_model_request(self.s.vault,work_id="job-old",turn_id=f"hermes/s/{turn.turn_key}")
        result = self.execute(Backend())
        self.assertEqual(result["code"], "budget_state_or_migration_required")
        self.assertEqual(result["model_calls_this_invocation"], 0)

    def test_forget_cancels_inflight_response(self):
        self.target()
        def forget():
            self.s.forget_memory("mem-old")
            return output(self.update(), self.no_memory())
        result = self.execute(Backend(forget), priority_memory_ids=["mem-old"])
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertIsNone(self.s.read("mem-old",include_history=True))
        self.assertNotIn("request", self.runs()[0]); self.assertNotIn("response", self.runs()[0])

    def test_no_file_lock_across_backend_and_nested_incremental_is_fenced(self):
        import threading
        observed=[]
        def check():
            def worker():
                with self.s.vault.lock(): observed.append("locked")
            thread=threading.Thread(target=worker); thread.start(); thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(observed,["locked"])
            nested=self.s.process(source="hermes",session_id="s",model=Backend())
            self.assertEqual(nested["execution_status"],"partial")
            self.assertIn(
                nested["results"][0].get("code"),
                {"processing_busy","incremental_model_busy"},
            )
            return output(self.create(), self.no_memory())
        result=self.execute(Backend(check))
        self.assertEqual(result["execution_status"],"completed")

    def test_corrupt_runtime_does_not_reset(self):
        result=self.execute(); state=self.ledger()
        state[KEY][result["run_id"]]["checksum"]="bad"
        self.s.vault.processed_state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError,"checksum"):
            self.s.resume_incremental_run(result["run_id"],backend=Backend())
        self.assertFalse(_budget_path(self.s.vault).exists())

    def test_cached_response_after_commit_failure_needs_no_backend(self):
        from memleaf.memory_writer import MemoryWriter
        backend=Backend(output(self.create(),self.no_memory()))
        with patch.object(MemoryWriter,"write_frozen_unlocked",side_effect=OSError("disk")):
            with self.assertRaises(IncrementalRunError) as caught: self.execute(backend)
        final=self.s.resume_incremental_run(caught.exception.result["run_id"])
        self.assertEqual(final["execution_status"],"completed")
        self.assertEqual(final["model_calls_this_invocation"],0)
        self.assertEqual(len(backend.calls),1)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)

    def test_terminal_resume_does_not_need_raw_source(self):
        result=self.execute(Backend(output(self.create(),self.no_memory())))
        self.s.vault.session_path("hermes","s").unlink()
        again=self.s.resume_incremental_run(result["run_id"])
        self.assertEqual(again["execution_status"],"completed")
        self.assertEqual(again["model_calls_this_invocation"],0)

    def test_index_failure_does_not_reissue_model_or_duplicate_history(self):
        self.target()
        backend=Backend(output(self.update(),self.no_memory()))
        with patch.object(self.s,"_rebuild_index_unlocked",side_effect=OSError("index")):
            with self.assertRaises(IncrementalRunError) as caught:
                self.execute(backend,priority_memory_ids=["mem-old"])
        self.assertEqual(self.s.read("mem-old").status,"completed")
        result=self.s.resume_incremental_run(caught.exception.result["run_id"])
        self.assertEqual(result["execution_status"],"completed")
        self.assertEqual(len(backend.calls),1)
        self.assertEqual(len(self.hist()),1)

    def test_multiturn_create_complete_repeat_keeps_original_identity(self):
        first=self.execute(Backend(output(self.create(),self.no_memory())))
        identity=first["commit"]["operations"][0]["memory_id"]
        self.capture("finish",content="Atlas task is complete",seq=3)
        class ContextBackend:
            single_pass_safe=True
            def __init__(self,action):self.action=action;self.calls=0
            def complete(self,prompt,**kwargs):
                self.calls+=1; value=json.loads(prompt)
                new=[e["ref"] for e in value["evidence"] if e["use"]=="new"]
                target=value["memories"][0]["ref"]
                row={"action":self.action,"target":target,"evidence":[new[0]]}
                if self.action=="UPDATE":row["patch"]={"status":"completed"}
                return output(row,{"action":"NO_MEMORY","evidence":[new[1]]})
        backend=ContextBackend("UPDATE")
        second=self.s.run_incremental(source="hermes",session_id="s",turn_id="finish",backend=backend,priority_memory_ids=[identity])
        self.assertEqual(second["execution_status"],"completed")
        self.assertEqual(self.s.read(identity).status,"completed")
        self.capture("repeat",content="Confirm that Atlas is completed",seq=5)
        backend2=ContextBackend("NO_CHANGE")
        third=self.s.run_incremental(source="hermes",session_id="s",turn_id="repeat",backend=backend2,priority_memory_ids=[identity])
        self.assertEqual(third["execution_status"],"completed")
        self.assertEqual(backend.calls,1);self.assertEqual(backend2.calls,1)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")),1)
        self.assertEqual(len(self.hist()),1)


if __name__ == "__main__": unittest.main()
