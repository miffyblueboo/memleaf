"""Acceptance artifact, HTTP metadata and interruption regressions (no network)."""
from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance_oracles import ORACLES
from test_acceptance import Oracle, SUITE
from memleaf.acceptance import AcceptanceError, _Meter, execute_suite, main, read_suite, validate_suite
from memleaf.llm.openai_compatible import OpenAICompatibleBackend


class AcceptanceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.suite = read_suite(SUITE)
        self.small = deepcopy(self.suite)
        self.small['cases'] = [self.small['cases'][1]]

    def run_small(self, backend=None):
        return execute_suite(self.small, output=self.root/'out',
                             backend=backend or Oracle([[{'action':'NO_MEMORY','evidence':['$user']}]]),
                             max_requests=2, repeats=1, fixture=True)

    def test_final_case_stopped_midway_is_incomplete(self):
        suite=deepcopy(self.suite);suite['cases']=suite['cases'][:1]
        report=execute_suite(suite, output=self.root/'out', backend=Oracle(ORACLES[0]),
                             max_requests=1,repeats=1,fixture=True)
        self.assertEqual(report['case_repetitions_run'],1)
        self.assertEqual(report['steps_run'],1)
        self.assertEqual(report['steps_expected'],3)
        self.assertEqual(report['execution_status'],'incomplete')

    def test_exact_trace_has_no_evaluation_instructions(self):
        self.small['cases'][0]['semantic_checks']=['never-send-private-checklist']
        backend=Oracle([[{'action':'NO_MEMORY','evidence':['$user']}]]);self.run_small(backend)
        trace=json.loads((self.root/'out/traces/0001.json').read_text(encoding="utf-8"))
        self.assertEqual(trace['prompt'],backend.calls[0][0])
        self.assertEqual(trace['system'],backend.calls[0][1]['system'])
        self.assertEqual(json.loads(trace['response'])['items'][0]['action'],'NO_MEMORY')
        self.assertNotIn('never-send-private-checklist',json.dumps(trace))
        self.assertNotIn('authorization',trace)

    def test_metrics_failure_does_not_discard_returned_response(self):
        class B(Oracle):
            def consume_call_metrics(self): raise RuntimeError('private diagnostic')
        result=self.run_small(B([[{'action':'NO_MEMORY','evidence':['$user']}]]))
        self.assertEqual(result['structural_status'],'passed')
        journal=json.loads((self.root/'out/requests.json').read_text(encoding="utf-8"))
        self.assertEqual(journal['attempts'][0]['metrics_status'],'unavailable')
        self.assertNotIn('private diagnostic',json.dumps(journal))

    def test_unknown_metadata_never_becomes_zero_usage_or_disabled(self):
        class B(Oracle):
            def consume_call_metrics(self):
                return {'prompt_tokens':True,'total_tokens':-1,'response_model':['bad'],
                        'thinking_effective':['disabled'],'api_key':'private'}
        self.run_small(B([[{'action':'NO_MEMORY','evidence':['$user']}]]))
        row=json.loads((self.root/'out/requests.json').read_text(encoding="utf-8"))['attempts'][0]
        self.assertIsNone(row['usage']);self.assertEqual(row['returned_model'],'unknown')
        self.assertNotIn('thinking_effective',row['thinking'])
        self.assertNotIn('api_key',json.dumps(row))

    def test_real_transport_payload_and_response_metadata(self):
        requests=[]
        def opener(request, **kwargs):
            payload=json.loads(request.data);requests.append(payload)
            value=json.loads(payload['messages'][-1]['content'])
            reply={'items':[{'action':'NO_MEMORY','evidence':[x['ref']]} for x in value['evidence'] if x['use']=='new']}
            return io.BytesIO(json.dumps({'model':'flash-reported-revision',
              'usage':{'prompt_tokens':10,'completion_tokens':9,'total_tokens':19,
                       'completion_tokens_details':{'reasoning_tokens':0}},
              'choices':[{'finish_reason':'stop','message':{'content':json.dumps(reply)}}]}).encode())
        backend=OpenAICompatibleBackend(base_url='https://example.invalid/v1',api_key='PRIVATE-KEY',
                 provider_name='deepseek',model='configured-flash',thinking={'single_pass':'disabled'},opener=opener)
        self.run_small(backend)
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['thinking'],{'type':'disabled'})
        self.assertEqual(requests[0]['response_format'],{'type':'json_object'})
        row=json.loads((self.root/'out/requests.json').read_text(encoding="utf-8"))['attempts'][0]
        self.assertEqual(row['returned_model'],'flash-reported-revision')
        self.assertEqual(row['usage']['total_tokens'],19)
        self.assertEqual(row['thinking']['thinking_requested'],'disabled')
        self.assertTrue(row['thinking']['thinking_applied'])
        self.assertEqual(row['thinking']['thinking_effective'],'no_reasoning_observed')
        files=''.join(p.read_text(encoding="utf-8") for p in (self.root/'out').glob('*.json'))
        self.assertNotIn('PRIVATE-KEY',files)
        self.assertNotIn('example.invalid',files)

    def test_reasoning_presence_reported_without_saving_hidden_text(self):
        def opener(_request,**_kwargs):
            return io.BytesIO(json.dumps({'model':'reported','choices':[{'finish_reason':'stop','message':{
               'content':'{}','reasoning_content':'HIDDEN-REASONING-SENTINEL'}}]}).encode())
        backend=OpenAICompatibleBackend(base_url='https://example.invalid',api_key='k',provider_name='deepseek',
                                         model='configured',thinking={'single_pass':'disabled'},opener=opener)
        out=self.root/'out';out.mkdir();meter=_Meter(backend,out,2)
        meter.complete('{}',system='json',purpose='single_pass')
        text=''.join(p.read_text(encoding="utf-8") for p in out.rglob('*.json'))
        self.assertNotIn('HIDDEN-REASONING-SENTINEL',text)
        self.assertEqual(meter.attempts[0]['thinking']['thinking_effective'],'reasoning_observed')

    def test_oversized_response_not_silently_truncated_into_trace(self):
        out=self.root/'out';out.mkdir();meter=_Meter(Oracle(['x'*140000]),out,1)
        meter.complete('{}')
        trace=json.loads((out/'traces/0001.json').read_text(encoding="utf-8"))
        self.assertIsNone(trace['response'])
        self.assertEqual(trace['response_omitted'],'output_limit_exceeded')
        self.assertEqual(meter.attempts[0]['output_bytes'],140000)

    def test_input_trace_write_failure_prevents_dispatch(self):
        out=self.root/'out';out.mkdir();backend=Oracle(['{}']);meter=_Meter(backend,out,1)
        with patch('memleaf.acceptance.atomic_write_json',side_effect=OSError):
            with self.assertRaises(OSError):meter.complete('{}')
        self.assertEqual(backend.calls,[])

    def test_response_observation_failure_keeps_reservation(self):
        out=self.root/'out';out.mkdir();backend=Oracle(['{}']);meter=_Meter(backend,out,1)
        from memleaf.acceptance import atomic_write_json as write
        def fail_response(path,data):
            if path.name=='0001.json' and data.get('response') is not None: raise OSError()
            return write(path,data)
        with patch('memleaf.acceptance.atomic_write_json',side_effect=fail_response):
            with self.assertRaises(OSError):meter.complete('{}')
        journal=json.loads((out/'requests.json').read_text(encoding="utf-8"))
        self.assertEqual(journal['reservations'],1)
        self.assertEqual(journal['attempts'][0]['outcome'],'returned')
        self.assertEqual(len(backend.calls),1)

    def test_duplicate_message_identity_is_invalid_fixture_not_runtime_fault(self):
        s=deepcopy(self.small)
        for m in s['cases'][0]['turns'][0]['messages']:m['message_id']='same'
        with self.assertRaisesRegex(AcceptanceError,'ambiguous_fixture_source'):validate_suite(s)

    def test_duplicate_sequence_is_invalid_fixture(self):
        s=deepcopy(self.small)
        for m in s['cases'][0]['turns'][0]['messages']:m['source_sequence']=1
        with self.assertRaisesRegex(AcceptanceError,'ambiguous_fixture_source'):validate_suite(s)

    def test_cli_limit_checked_before_config_or_credentials(self):
        with patch('memleaf.acceptance.load_config',side_effect=AssertionError),redirect_stdout(io.StringIO()) as out:
            code=main(['--suite',str(SUITE),'--execute','--authorize-model','--max-requests','0',
                       '--output',str(self.root/'out'),'--backend-config','private.yaml'])
        self.assertEqual(code,1)
        self.assertEqual(json.loads(out.getvalue())['code'],'invalid_acceptance_request_cap')

    def test_output_collision_checked_before_config(self):
        out=self.root/'out';out.mkdir()
        with patch('memleaf.acceptance.load_config',side_effect=AssertionError),redirect_stdout(io.StringIO()) as text:
            code=main(['--suite',str(SUITE),'--execute','--authorize-model','--max-requests','1',
                       '--output',str(out),'--backend-config','private.yaml'])
        self.assertEqual(code,1)
        self.assertEqual(json.loads(text.getvalue())['code'],'new_output_directory_required')

    def test_process_exit_keeps_reserved_attempt_and_refuses_restart(self):
        script='''import os,sys
from memleaf.acceptance import execute_suite,read_suite
class B:
 single_pass_safe=True
 def complete(self,*args,**kwargs):os._exit(23)
execute_suite(read_suite(sys.argv[1]),output=sys.argv[2],backend=B(),max_requests=2,repeats=1,fixture=True)
'''
        out=self.root/'out'
        result=subprocess.run([sys.executable,'-c',script,str(SUITE),str(out)],env=os.environ.copy(),timeout=20)
        self.assertEqual(result.returncode,23)
        journal=json.loads((out/'requests.json').read_text(encoding="utf-8"))
        self.assertEqual(journal['reservations'],1)
        self.assertEqual(journal['attempts'][0]['outcome'],'reserved')
        self.assertEqual(json.loads((out/'report.json').read_text(encoding="utf-8"))['execution_status'],'running')
        with self.assertRaises(AcceptanceError):
            execute_suite(self.small,output=out,backend=Oracle([]),max_requests=2,repeats=1,fixture=True)


    def test_lifecycle_remains_replayable_after_state_compaction_and_backup(self):
        from memleaf import Memleaf
        from memleaf.migration import verify_migration_backup
        suite=deepcopy(self.suite);suite['cases']=suite['cases'][:1]
        out=self.root/'out'
        result=execute_suite(suite,output=out,backend=Oracle(ORACLES[0]),max_requests=6,repeats=1,fixture=True)
        self.assertEqual(result['structural_status'],'passed')
        service=Memleaf(out/'task_lifecycle-01/vault')
        before=service.list_todos(status='all')
        preview=service.compact_runtime_state()
        service.compact_runtime_state(dry_run=False,expected_revision=preview['state_revision'])
        backend=Oracle([])
        replay=service.process_incremental(source='hermes',session_id='acceptance',turn_id='t3',model=backend)
        self.assertEqual(backend.calls,[])
        self.assertEqual(replay['execution_status'],'completed')
        self.assertEqual(service.list_todos(status='all')['results'],before['results'])
        preflight=service.migration_preflight()
        backup=service.backup_for_migration(self.root/'backup',expected_snapshot=preflight['snapshot_revision'],writers_stopped=True)
        self.assertEqual(backup['backup_status'],'verified')
        self.assertFalse(backup['switch_authorized'])
        self.assertEqual(verify_migration_backup(self.root/'backup')['execution_status'],'verified')

    def test_reading_completed_task_keeps_identity_and_excludes_it_from_active(self):
        from memleaf import Memleaf
        suite=deepcopy(self.suite);suite['cases']=suite['cases'][:1]
        out=self.root/'out'
        execute_suite(suite,output=out,backend=Oracle(ORACLES[0]),max_requests=6,repeats=1,fixture=True)
        observations=json.loads((out/'task_lifecycle-01/observations.json').read_text(encoding="utf-8"))
        mid=observations['steps'][0]['memories'][0]['memory_id']
        other_reader=Memleaf(out/'task_lifecycle-01/vault')
        page=other_reader.read_page(mid)
        self.assertEqual(page['status'],'completed')
        self.assertEqual(page['memory_id'],mid)
        self.assertEqual(other_reader.list_todos()['results'],[])

    def test_authentication_failure_stops_before_other_cases(self):
        from memleaf.llm import ModelError
        backend=Oracle([ModelError('SECRET',code='model_auth_failed')])
        report=execute_suite(self.suite,output=self.root/'out',backend=backend,max_requests=180,repeats=5,fixture=True)
        self.assertEqual(len(backend.calls),1)
        self.assertEqual(report['stop_reason'],'model_auth_failed')
        self.assertEqual(report['execution_status'],'incomplete')
        self.assertFalse(report['budget_exhausted'])
        self.assertNotIn('SECRET',json.dumps(report))

    def test_transport_failure_does_not_poll_or_spend_later_trials(self):
        from memleaf.llm import ModelError
        for code in ('model_rate_limited','model_timeout','model_network_error'):
            with self.subTest(code=code):
                backend=Oracle([ModelError('provider detail',code=code)])
                report=execute_suite(self.suite,output=self.root/code,backend=backend,max_requests=180,repeats=5,fixture=True)
                self.assertEqual(len(backend.calls),1)
                self.assertEqual(report['stop_reason'],code)
                self.assertEqual(report['backend_reservations'],1)

if __name__=='__main__':unittest.main()
