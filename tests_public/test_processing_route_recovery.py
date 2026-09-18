"""The new common entry point must preserve existing durable run semantics."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from test_processing_route import EchoBackend
from memleaf import Memleaf
from memleaf.process_journal import ProcessJournal
from memleaf.process_jobs import _result_status
from memleaf.llm.base import ModelError


class ProcessingRouteRecoveryTests(IncrementalFixture):
    def call(self, backend=None, **kwargs):
        return self.s.process(source='hermes',session_id='s',pipeline='incremental',model=backend,**kwargs)

    def test_process_exit_after_response_resumes_through_normal_process(self):
        script=r'''
import sys, os
from memleaf import Memleaf
import memleaf.incremental_execution as execution
original=execution.save_run
def stop(service, processed, run):
    original(service, processed, run)
    if run['status']=='response_ready': os._exit(73)
execution.save_run=stop
class Backend:
    single_pass_safe=True
    def complete(self,prompt,**kwargs): return sys.argv[2]
Memleaf(sys.argv[1]).process(source='hermes',session_id='s',pipeline='incremental',model=Backend())
'''
        child=subprocess.run([sys.executable,'-c',script,str(self.s.vault.root),output(self.create(),self.no_memory())],
                             capture_output=True,text=True,timeout=15)
        self.assertEqual(child.returncode,73,child.stderr)
        self.s=Memleaf(self.s.vault.root)
        with patch('memleaf.incremental_runtime._resolve',side_effect=AssertionError('extra dispatch')):
            result=self.call()
        self.assertEqual(result['execution_status'],'completed')
        self.assertEqual(result['model_calls'],0)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_index_failure_keeps_success_and_zero_call_recovery(self):
        # Fail the commit's derived index, not model execution or source capture.
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index unavailable')):
            first=self.call(Backend(output(self.create(),self.no_memory())))
        self.assertEqual(first['coverage_status'],'partial')
        self.assertEqual(first['memories_written'],1)
        ids=first['memory_ids']; self.assertEqual(len(ids),1)
        second=self.call()
        self.assertEqual(second['execution_status'],'completed')
        self.assertEqual(second['model_calls'],0)
        self.assertEqual(second['memories_written'],0)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_cleanup_failure_does_not_erase_commit_result(self):
        with patch.object(ProcessJournal,'_cleanup_due_unlocked',side_effect=OSError('cleanup unavailable')):
            first=self.call(Backend(output(self.create(),self.no_memory())))
        self.assertEqual(first['memories_written'],1)
        self.assertEqual(first['cleanup_status'],'recovery_required')
        self.assertEqual(first['coverage_status'],'complete')
        self.assertEqual(_result_status(first),'deferred')
        second=self.call();self.assertEqual(second['execution_status'],'completed')
        self.assertEqual(second['memories_written'],0)

    def test_two_concurrent_common_entries_have_only_one_dispatch(self):
        entered=threading.Event();release=threading.Event();values=[]
        def hold():
            entered.set()
            if not release.wait(8): raise AssertionError('test release missing')
            return output(self.create(),self.no_memory())
        backend=Backend(hold)
        thread=threading.Thread(target=lambda:values.append(self.call(backend)))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            # No network-time Vault lock: capture and a second status pass work.
            self.s.capture('hermes','other','x','user','Concurrent independent input')
            second=self.call(backend)
            self.assertEqual(second['coverage_status'],'partial')
            self.assertEqual(second['results'][0]['code'],'processing_busy')
        finally:
            release.set(); thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(backend.calls),1)
        self.assertEqual(values[0]['memories_written'],1)

    def test_target_edit_during_model_call_is_rejected_without_fallback(self):
        self.target()
        def edit():
            path=self.s.vault.list_markdown('knowledge')[0]
            path.write_text(path.read_text().replace('Deliver the report.','User edited the task.'))
            return output(self.update(),self.no_memory())
        backend=Backend(edit)
        first=self.call(backend);self.assertEqual(first['memories_written'],0)
        self.assertEqual(self.s.read('mem-old').status,'active')
        self.assertEqual(self.call(backend,recover=True)['model_calls'],0)
        self.assertEqual(len(backend.calls),1)

    def test_source_revision_during_request_cannot_write_old_result(self):
        def edit():
            self.revise();return output(self.create(),self.no_memory())
        backend=Backend(edit);result=self.call(backend)
        self.assertEqual(result['coverage_status'],'partial')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),0)

    def test_forget_during_request_cancels_result(self):
        self.target()
        def forget():
            self.s.forget_memory('mem-old')
            return output(self.update(),self.no_memory())
        backend=Backend(forget);result=self.call(backend)
        self.assertEqual(result['coverage_status'],'partial')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),0)
        self.assertEqual(len(backend.calls),1)

    def test_scope_registration_runs_through_common_entry(self):
        response=output(self.create(scope='project:Atlas'),self.no_memory())
        result=self.call(Backend(response),scope='project:Atlas')
        self.assertEqual(result['execution_status'],'completed')
        self.assertTrue(self.s.vault.config()['scopes'].get('project:Atlas') is not None)

    def test_raw_transport_recovery_after_scheduler_uses_same_budget(self):
        backend=Backend(ModelError(code='model_timeout'), output(self.create(),self.no_memory()))
        first=self.call(backend)
        second=self.s.process_incremental(source='hermes',session_id='s',turn_id='t',model=backend,recover=True)
        self.assertEqual(second['execution_status'],'completed')
        self.assertEqual(second['reserved_requests'],2)
        self.assertEqual(first['results'][0]['run_id'],second['run_id'])

    def test_repeated_partial_preserves_unresolved_gauge(self):
        first=self.call(Backend(output(self.create(),
            {'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'Clarify.'})))
        second=self.call()
        self.assertGreater(first['unresolved_evidence_count'],0)
        self.assertEqual(first['unresolved_evidence_count'],second['unresolved_evidence_count'])
        self.assertEqual(second['model_calls'],0)

    def test_native_no_change_is_not_counted_as_local_write(self):
        from memleaf.config import save_config
        native=Path(self.temp.name)/'MEMORY.md';native.write_text('# Task\n\nAtlas task completed.\n')
        cfg=self.s.vault.config()
        cfg['native_sources']={'notes':{'agent':'hermes','path':str(native),'share':False,'enabled':True}}
        save_config(self.s.vault.config_path,cfg)
        def response(prompt,**kwargs):
            data=json.loads(prompt)
            target=next(m['ref'] for m in data['memories'] if m.get('native'))
            return output({'action':'NO_CHANGE','evidence':['e1'],'target':target},self.no_memory())
        backend=EchoBackend();backend.complete=response
        before=native.read_bytes();result=self.call(backend)
        self.assertEqual(result['execution_status'],'completed')
        self.assertEqual(result['memories_written'],0);self.assertEqual(native.read_bytes(),before)
