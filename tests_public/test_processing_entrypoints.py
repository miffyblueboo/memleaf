"""Existing CLI/MCP/jobs use one opt-in automatic route, never a second worker."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_processing_route import EchoBackend
from test_incremental_execution import Backend, output
from memleaf.config import save_config
from memleaf import Memleaf
from memleaf import process_jobs as jobs
from memleaf.cli import main, build_parser
from memleaf.inspection import preview_process
from memleaf.mcp_server import _invoke_tool, _TOOL_BY_NAME
from memleaf.process_journal import processing_health


class ProcessingEntrypointTests(IncrementalFixture):
    def cfg(self, route):
        value = self.s.vault.config()
        value['process']['automatic_pipeline'] = route
        save_config(self.s.vault.config_path, value)

    def enqueue(self, **kwargs):
        with patch.object(jobs, '_dispatch'):
            return jobs.enqueue(self.s.vault.root, source='hermes', session_id='s', **kwargs)

    def start(self, job_id, backend=None):
        with self.s.vault.lock():
            state = jobs._read_state(self.s.vault)
            state['jobs'][job_id].update(status='running', owner_pid=os.getpid())
            state['active_job_id'] = job_id
            jobs._write_state(self.s.vault, state)
        with patch.object(jobs, '_dispatch'), patch('memleaf.incremental_runtime._resolve', return_value=backend):
            jobs.run_worker(self.s.vault.root, job_id)
        return jobs.status(self.s.vault.root, job_id=job_id)

    def payload(self, result):
        self.assertFalse(result.get('isError'), result)
        return json.loads(result['content'][0]['text'])

    def test_configured_queue_records_effective_route(self):
        self.cfg('incremental')
        accepted = self.enqueue()
        value = jobs.status(self.s.vault.root, job_id=accepted['job_id'])
        self.assertEqual(value['pipeline'], 'incremental')
        self.assertEqual(value['configured_pipeline'], 'incremental')
        self.assertFalse(value['recover'])

    def test_equal_requests_coalesce_with_normalized_scope(self):
        first = self.enqueue(pipeline='incremental', scope='project:Atlas')
        second = self.enqueue(pipeline='incremental', scope=['project:Atlas'])
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertTrue(second['accepted'])

    def test_queued_selection_cannot_change_scope_recovery_or_pipeline(self):
        first = self.enqueue(pipeline='incremental', scope='project:Atlas')
        for kwargs in ({'pipeline':'incremental', 'scope':'project:Beacon'},
                       {'pipeline':'legacy', 'scope':'project:Atlas'},
                       {'pipeline':'incremental', 'scope':'project:Atlas', 'recover':True}):
            with self.subTest(kwargs=kwargs):
                result = self.enqueue(**kwargs)
                self.assertFalse(result['accepted']); self.assertEqual(result['job_id'], first['job_id'])
                self.assertEqual(result['reason'], 'process_arguments_changed')

    def test_config_change_does_not_coalesce_into_old_job(self):
        first = self.enqueue()
        self.cfg('incremental')
        second = self.enqueue()
        self.assertFalse(second['accepted']); self.assertEqual(first['job_id'], second['job_id'])

    def test_worker_uses_captured_route(self):
        backend = EchoBackend()
        accepted = self.enqueue(pipeline='incremental')
        with patch('memleaf.processing.Processor.process', side_effect=AssertionError('legacy')):
            result = self.start(accepted['job_id'], backend)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['pipeline'], 'incremental')
        self.assertEqual(result['result']['model_calls'], 1)
        self.assertEqual(len(backend.calls), 1)

    def test_waiting_job_blocks_after_config_change(self):
        accepted = self.enqueue(pipeline='incremental'); self.cfg('incremental')
        backend = EchoBackend(); result = self.start(accepted['job_id'], backend)
        self.assertEqual(len(backend.calls), 0)
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['result']['results'][0]['code'], 'processing_pipeline_changed')

    def test_old_job_without_route_is_not_reinterpreted(self):
        accepted = self.enqueue()
        state = jobs._read_state(self.s.vault)
        for key in ('pipeline','configured_pipeline','recover'): state['jobs'][accepted['job_id']].pop(key)
        jobs._write_state(self.s.vault, state)
        self.cfg('incremental')
        result = self.start(accepted['job_id'], EchoBackend())
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['result']['results'][0]['code'], 'processing_pipeline_changed')

    def test_legacy_job_still_runs_legacy_when_not_switched(self):
        accepted = self.enqueue()
        with patch('memleaf.processing.Processor.process', return_value={'processed_turns':0}) as process:
            result = self.start(accepted['job_id'])
        process.assert_called_once(); self.assertEqual(result['status'], 'succeeded')

    def test_invalid_queue_controls_fail_before_write(self):
        for kwargs in ({'pipeline':'invalid'}, {'recover':'yes'}, {'pipeline':'legacy','recover':True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.enqueue(**kwargs)
        self.assertEqual(jobs._read_state(self.s.vault)['jobs'], {})

    def test_invalid_persisted_route_does_not_reset_queue(self):
        accepted = self.enqueue(); state = jobs._read_state(self.s.vault)
        state['jobs'][accepted['job_id']]['pipeline'] = []
        with self.assertRaises(jobs.ProcessJobStateError): jobs._valid_state(state)
        state['jobs'][accepted['job_id']]['pipeline'] = 'incremental'
        state['jobs'][accepted['job_id']]['recover'] = 1
        with self.assertRaises(jobs.ProcessJobStateError): jobs._valid_state(state)

    def test_partial_execution_is_not_succeeded_even_when_coverage_complete(self):
        for status in ('partial','recovery_required','blocked'):
            with self.subTest(status=status):
                self.assertEqual(jobs._result_status({'pipeline':'incremental','execution_status':status,'coverage_status':'complete'}), 'deferred')

    def test_safe_job_projection_keeps_codes_and_drops_text(self):
        value={'pipeline':'incremental','execution_status':'partial','cleanup_status':'recovery_required',
               'results':[{'execution_status':'blocked','code':'scope_boundary','turn_index':2,
                           'unresolved_evidence_count':1, 'partial_recovery_available':True, 'body':'PRIVATE'}],
               'request':{'api_key':'PRIVATE'}}
        result=jobs._safe_result(value)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual(result['results'][0]['code'],'scope_boundary')
        self.assertEqual(result['results'][0]['unresolved_evidence_count'],1)
        self.assertEqual(result['cleanup_status'],'recovery_required')

    def test_attempt_aggregate_uses_latest_backlog_and_unique_operations(self):
        first={'pipeline':'incremental','execution_status':'partial','coverage_status':'partial','memories_written':1,
               'committed_operation_ids':['op1'],'memory_ids':['mem1'],'deferred_inbox_turns':2,'pending_inbox_turns':3,'model_calls':1}
        second={**first,'execution_status':'completed','coverage_status':'complete','deferred_inbox_turns':0,'pending_inbox_turns':0,'model_calls':0}
        result=jobs._aggregate_attempt_results([{'result':first},{'result':second}])
        self.assertEqual(result['memories_written'],1)
        self.assertEqual(result['pending_inbox_turns'],0); self.assertEqual(result['deferred_inbox_turns'],0)
        self.assertEqual(result['model_calls'],1)
        self.assertEqual(result['coverage_status'],'complete')

    def test_constructor_backend_is_used_without_extra_route(self):
        backend=EchoBackend(); service=Memleaf(self.s.vault.root,model=backend)
        result=service.process(source='hermes',session_id='s',pipeline='incremental')
        self.assertEqual(result['model_calls'],1); self.assertEqual(len(backend.calls),1)

    def test_cli_parser_exposes_route_without_changing_default(self):
        a=build_parser().parse_args(['process'])
        self.assertIsNone(a.pipeline); self.assertFalse(a.recover)
        a=build_parser().parse_args(['process','--pipeline','incremental','--recover'])
        self.assertEqual(a.pipeline,'incremental'); self.assertTrue(a.recover)

    def test_cli_forwards_exact_route_and_recovery(self):
        with patch.object(Memleaf,'process',return_value={'ok':True}) as call, redirect_stdout(StringIO()):
            code=main(['process','--vault',str(self.s.vault.root),'--pipeline','incremental','--recover','--json'])
        self.assertEqual(code,0)
        self.assertEqual(call.call_args.kwargs['pipeline'],'incremental')
        self.assertTrue(call.call_args.kwargs['recover'])

    def test_cli_dry_run_passes_same_route(self):
        with patch('memleaf.inspection.preview_process',return_value={'ok':True}) as call, redirect_stdout(StringIO()):
            code=main(['process','--vault',str(self.s.vault.root),'--pipeline','incremental','--dry-run','--json'])
        self.assertEqual(code,0)
        self.assertEqual(call.call_args.kwargs['pipeline'],'incremental')

    def test_dry_run_executes_shared_runner_only_in_copy(self):
        before={str(p):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()}
        backend=Backend(output(self.create(),self.no_memory()))
        result=preview_process(self.s.vault.root,source='hermes',session_id='s',pipeline='incremental',model=backend)
        self.assertEqual(result['result']['memories_written'],1); self.assertTrue(result['source_unchanged'])
        after={str(p):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_mcp_schema_extends_existing_tool(self):
        props=_TOOL_BY_NAME['process']['inputSchema']['properties']
        self.assertEqual(props['pipeline']['enum'],['legacy','incremental'])
        self.assertEqual(props['recover']['type'],'boolean')

    def test_mcp_sync_invokes_same_route(self):
        backend=EchoBackend()
        with patch('memleaf.incremental_runtime._resolve',return_value=backend):
            result=self.payload(_invoke_tool(self.s,'process',{'source':'hermes','session_id':'s','pipeline':'incremental'}))
        self.assertEqual(result['pipeline'],'incremental'); self.assertEqual(len(backend.calls),1)

    def test_mcp_background_freezes_controls(self):
        with patch.object(jobs,'_dispatch'):
            result=self.payload(_invoke_tool(self.s,'process',{'source':'hermes','session_id':'s',
                'pipeline':'incremental','recover':True,'background':True,'scope':['project:Atlas']}))
        job=jobs.status(self.s.vault.root,job_id=result['job_id'])
        self.assertEqual(job['pipeline'],'incremental'); self.assertTrue(job['recover'])
        self.assertEqual(job['scope'],['project:Atlas'])

    def test_mcp_invalid_controls_reject_before_process(self):
        for kwargs in ({'pipeline':'other'},{'recover':'true'}):
            with self.subTest(kwargs=kwargs), patch.object(Memleaf,'process') as called:
                result=_invoke_tool(self.s,'process',kwargs)
                self.assertTrue(result.get('isError')); called.assert_not_called()

    def test_mcp_status_is_read_only_even_after_partial(self):
        self.s.process(pipeline='incremental',source='hermes',session_id='s',model=Backend(output(self.create())))
        before={str(p):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()}
        result=self.payload(_invoke_tool(self.s,'process_status',{}))
        self.assertEqual(result['status'],'deferred'); self.assertFalse(result['switch_permission'])
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()})

    def test_raw_id_facade_returns_scheduler_receipt(self):
        self.s.process(pipeline='incremental',source='hermes',session_id='s',model=EchoBackend())
        result=self.s.process_incremental(source='hermes',session_id='s',turn_id='t')
        self.assertEqual(result['execution_status'],'completed')
        self.assertEqual(result['model_calls_this_invocation'],0)

    def test_raw_id_facade_cannot_widen_scheduler_scope(self):
        self.s.process(pipeline='incremental',source='hermes',session_id='s',scope='project:Atlas',model=EchoBackend())
        with self.assertRaises(ValueError): self.s.process_incremental(source='hermes',session_id='s',turn_id='t')
        result=self.s.process_incremental(source='hermes',session_id='s',turn_id='t',scope='project:Atlas')
        self.assertEqual(result['execution_status'],'completed')

    def test_host_runtime_reports_index_or_cleanup_degradation(self):
        from memleaf.host_runtime import HostRuntime
        runtime=HostRuntime(self.s,'hermes');runtime._set_process_pending('s',True)
        with patch.object(runtime,'process',return_value={'pipeline':'incremental','execution_status':'partial','coverage_status':'complete'}):
            result=runtime.complete_turn(session_id='s',turn_id='t',assistant_content=None)
        self.assertTrue(result.process_deferred)
        self.assertFalse(runtime._process_pending('s'))

    def test_host_runtime_keeps_new_backlog_pending_without_transport_retry(self):
        from memleaf.host_runtime import HostRuntime
        runtime=HostRuntime(self.s,'hermes');runtime._set_process_pending('s',True)
        with patch.object(runtime,'process',return_value={'pipeline':'incremental','execution_status':'partial',
                  'pending_inbox_turns':2,'retryable_deferred_turns':2}):
            result=runtime.complete_turn(session_id='s',turn_id='t',assistant_content=None)
        self.assertTrue(result.process_deferred);self.assertTrue(runtime._process_pending('s'))

    def test_transient_only_backlog_is_not_auto_retryable(self):
        from memleaf.llm.base import ModelError
        backend=Backend(ModelError(code='model_timeout'))
        result=self.s.process(source='hermes',session_id='s',pipeline='incremental',model=backend)
        self.assertEqual(result['pending_inbox_turns'],1)
        self.assertEqual(result['retryable_deferred_turns'],0)

    def test_zero_call_status_polling_keeps_job_file_identical(self):
        accepted=self.enqueue(pipeline='incremental')
        before={str(p):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()}
        for _ in range(3): self.payload(_invoke_tool(self.s,'process_status',{'job_id':accepted['job_id']}))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()})

    def test_final_worker_partial_carries_successful_memory_ids(self):
        accepted=self.enqueue(pipeline='incremental')
        result=self.start(accepted['job_id'], Backend(output(self.create())))
        self.assertEqual(result['status'],'deferred')
        self.assertEqual(result['result']['memories_written'],1)
        self.assertEqual(len(result['result']['memory_ids']),1)
        self.assertGreater(result['result']['unresolved_evidence_count'],0)
