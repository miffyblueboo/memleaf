from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf.config import save_config, load_config
from memleaf.index import turn_key
from memleaf.inbox import captured_turn_selector
from memleaf.llm.base import ModelError
from memleaf.locking import atomic_write_json
from memleaf.process_journal import processing_health
from memleaf.processing_route import select_pipeline
from memleaf.turn_plan import turn_identity_key


class EchoBackend:
    single_pass_safe = True
    def __init__(self):
        self.calls = []
    def complete(self, prompt, **kwargs):
        value = json.loads(prompt)
        self.calls.append(value)
        return output({"action": "NO_MEMORY"})


class ProcessingRouteTests(IncrementalFixture):
    def process(self, backend=None, **kwargs):
        return self.s.process(source="hermes", session_id="s", pipeline="incremental", model=backend, **kwargs)

    def configure(self, value):
        cfg = self.s.vault.config(); cfg['process']['automatic_pipeline'] = value
        save_config(self.s.vault.config_path, cfg)

    def test_legacy_processor_has_no_executable_engine(self):
        from memleaf.processing import Processor
        from memleaf.process_common import ProcessingError
        with self.assertRaisesRegex(ProcessingError,'legacy_pipeline_removed'):
            Processor(self.s)

    def test_default_route_is_incremental(self):
        b=EchoBackend()
        r=self.s.process(source='hermes',session_id='s',model=b)
        self.assertEqual((r['pipeline'],r['execution_status'],r['model_calls']),
                         ('incremental','completed',1))
        self.assertEqual(select_pipeline(self.s.vault.config()), 'incremental')

    def test_explicit_incremental_uses_the_same_engine(self):
        r = self.process(EchoBackend())
        self.assertEqual((r['pipeline'],r['execution_status'],r['model_calls']),
                         ('incremental','completed',1))

    def test_legacy_route_is_not_executable(self):
        self.configure('incremental')
        with self.assertRaisesRegex(ValueError,'legacy_pipeline_removed'):
            self.s.process(pipeline='legacy')
        self.configure('legacy')
        with self.assertRaisesRegex(ValueError,'legacy_pipeline_removed'):
            self.s.process()

    def test_invalid_config_and_pipeline_are_not_silent_fallbacks(self):
        for value in (None, [], True, 'other', 'Incremental'):
            with self.subTest(value=value):
                if value is not None:
                    with self.assertRaises(ValueError): self.s.process(pipeline=value)
                cfg=self.s.vault.config(); cfg['process']['automatic_pipeline']=value
                with self.assertRaises(ValueError): save_config(self.s.vault.config_path,cfg)

    def test_load_rejects_invalid_pipeline(self):
        from memleaf.frontmatter import load_yaml, dump_yaml
        path=self.s.vault.config_path
        cfg=load_yaml(path.read_text()); cfg['process']['automatic_pipeline']='broken'
        path.write_text(dump_yaml(cfg))
        with self.assertRaises(ValueError): load_config(path)

    def test_no_work_does_not_require_model(self):
        self.process(EchoBackend())
        with patch('memleaf.incremental_runtime._resolve', side_effect=AssertionError('model')):
            r=self.process()
        self.assertEqual((r['processed_turns'],r['model_calls']), (0,0))

    def test_create_and_repeat_keep_one_id(self):
        b=Backend(output(self.create(),self.no_memory()))
        first=self.process(b); again=self.process(b)
        self.assertEqual(first['memories_written'],1)
        self.assertEqual(again['memories_written'],0)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)
        self.assertEqual(len(b.calls),1)

    def test_completed_updates_original_identity(self):
        self.target(); r=self.process(Backend(output(self.update(),self.no_memory())))
        self.assertEqual(r['memory_ids'],['mem-old'])
        self.assertEqual(self.s.read('mem-old').status,'completed')
        self.assertEqual(self.s.read('mem-old').due_date,'2026-09-20')

    def test_partial_is_visible_on_repeat_and_later_source_runs(self):
        b=Backend(output(self.create(), {'action':'UPDATE','target':'m999','evidence':['e2'],'patch':{'status':'completed'}}))
        first=self.process(b); second=self.process(b)
        self.assertEqual(first['coverage_status'],'partial')
        self.assertEqual(second['coverage_status'],'partial')
        self.assertEqual(second['model_calls'],0)
        self.capture('next',content='Other independent input',seq=3)
        echo=EchoBackend(); third=self.process(echo)
        self.assertEqual(len(echo.calls),1)
        self.assertEqual(third['coverage_status'],'partial')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_transport_requires_explicit_remaining_recovery(self):
        b=Backend(ModelError(code='model_timeout'),output(self.create(),self.no_memory()))
        r=self.process(b); self.assertEqual(len(b.calls),1)
        self.s=Memleaf(self.s.vault.root)
        self.process(b); self.assertEqual(len(b.calls),1)
        r=self.process(b,recover=True)
        self.assertEqual(r['execution_status'],'completed'); self.assertEqual(len(b.calls),2)

    def test_invalid_top_level_is_still_bounded_to_two(self):
        b=Backend('{','{'); self.process(b)
        for _ in range(3):
            r=self.process(b,recover=True)
            self.assertEqual(r['coverage_status'],'partial')
        self.assertEqual(len(b.calls),2)

    def test_existing_direct_run_resumes_without_new_identity(self):
        b=Backend(ModelError(code='model_timeout'),output(self.create(),self.no_memory()))
        old=self.s.process_incremental(source='hermes',session_id='s',turn_id='t',model=b)
        new=self.process(b,recover=True)
        self.assertEqual(new['results'][0]['run_id'],old['run_id'])
        self.assertEqual(len(b.calls),2)

    def test_source_order_and_four_turn_cap_do_not_skip_low_local_index(self):
        # Initial t is source-first. The rest arrive in reverse source order.
        for i in range(6,0,-1): self.capture('t'+str(i),content='source-'+str(i),seq=2*i+1)
        b=EchoBackend(); first=self.process(b)
        self.assertEqual(first['processed_turns'],4)
        self.assertEqual(first['pending_inbox_turns'],3)
        second=self.process(b)
        self.assertEqual(second['processed_turns'],3)
        self.assertEqual(second['pending_inbox_turns'],0)
        self.assertEqual(len(b.calls),7)
        contents=[next(e['text'] for e in value['evidence'] if e['use']=='new' and e['role']=='user') for value in b.calls]
        self.assertEqual(contents[1:],['source-'+str(i) for i in range(1,7)])

    def test_incomplete_predecessor_blocks_later_turn(self):
        self.process(EchoBackend())
        self.s.capture('hermes','s','missing','user','not complete',message_id='missing-u',source_sequence=3)
        self.capture('later',seq=5)
        b=EchoBackend(); r=self.process(b)
        self.assertEqual(len(b.calls),0)
        self.assertEqual(r['coverage_status'],'partial')
        self.assertIn('earlier_source_incomplete',[x.get('code') for x in r['results']])

    def test_redacted_display_turn_id_is_not_the_grouping_identity(self):
        self.process(EchoBackend())
        raw='api_key=sk-'+'x'*32
        self.s.capture('hermes','s',raw,'user','New input',message_id='private-u',source_sequence=3)
        self.s.capture('hermes','s',raw,'assistant','OK',message_id='private-a',source_sequence=4,final=True)
        path=self.s.vault.inbox_path/'hermes'/'s.md'
        self.assertNotIn(raw,path.read_text())
        b=EchoBackend(); r=self.process(b)
        self.assertEqual(len(b.calls),1)
        self.assertEqual(r['results'][0]['turn_key'],turn_key(raw))
        self.assertNotIn(raw,json.dumps(r))

    def test_key_selector_does_not_fallback_or_accept_two_identities(self):
        key=turn_key('t'); self.assertEqual(captured_turn_selector(key,key),key)
        for value in ('1',[], 'A'*64):
            with self.assertRaises(ValueError): captured_turn_selector(key,value)
        with self.assertRaises(ValueError): captured_turn_selector('t',key)

    def test_scope_remains_a_write_boundary(self):
        b=Backend(output(self.create(scope='project:Beacon'),self.no_memory()))
        r=self.process(b,scope='project:Atlas')
        self.assertEqual(r['coverage_status'],'partial')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),0)

    def test_old_frozen_plan_blocks_new_dispatch(self):
        state=self.ledger();state['pending_turn_plans']={turn_identity_key('hermes','s',turn_key('t')):{}}
        atomic_write_json(self.s.vault.processed_state_path,state)
        b=EchoBackend();r=self.process(b)
        self.assertEqual(len(b.calls),0)
        self.assertIn('legacy_pending_plan',[x['code'] for x in r['results']])
        self.assertEqual(self.ledger()['pending_turn_plans'],state['pending_turn_plans'])

    def test_legacy_partial_not_reextracted_under_new_mode(self):
        state=self.ledger();keys=[e['event_key'] for e in state['events'].values()]
        state['sessions']['hermes/s']['processed_turns']=[{'turn_key':turn_key('t'),'turn_index':1,'event_keys':keys,'deferred_evidence':[{}]}]
        atomic_write_json(self.s.vault.processed_state_path,state)
        b=EchoBackend();r=self.process(b)
        self.assertEqual(len(b.calls),0)
        self.assertIn('legacy_partial_requires_migration',[x['code'] for x in r['results']])

    def test_source_filter_does_not_consume_other_sessions(self):
        self.s.capture('hermes','other','x','user','Other',message_id='u')
        self.s.capture('hermes','other','x','assistant','ok',message_id='a',final=True)
        b=EchoBackend();r=self.process(b)
        self.assertEqual(len(b.calls),1)
        self.assertEqual(r['processed_turns'],1)
        self.assertFalse(self.ledger()['sessions']['hermes/other'].get('processed_turns'))

    def test_unavailable_backend_does_not_report_no_memory(self):
        r=self.process()
        self.assertEqual(r['coverage_status'],'partial')
        self.assertEqual(r['memories_written'],0)
        self.assertFalse(self.ledger()['sessions']['hermes/s'].get('processed_turns'))

    def test_error_messages_do_not_leak_backend_credentials(self):
        b=Backend(RuntimeError('secret api_key=abcdef'))
        r=self.process(b)
        self.assertNotIn('abcdef',json.dumps(r));self.assertNotIn('abcdef',self.s.vault.processed_state_path.read_text())

    def test_invalid_recover_and_legacy_recovery_rejected(self):
        with self.assertRaises(ValueError): self.process(EchoBackend(),recover='true')
        with self.assertRaises(ValueError): self.s.process(pipeline='legacy',recover=True)

    def test_status_reads_do_not_dispatch_or_mutate_files(self):
        self.process(Backend(ModelError(code='model_timeout')))
        before={p.relative_to(self.s.vault.root):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()}
        for _ in range(3):
            r=processing_health(self.s.vault.root)
            self.assertEqual(r['incremental']['retained_by_status'].get('retryable'),1)
            self.assertFalse(r['switch_permission'])
        after={p.relative_to(self.s.vault.root):p.read_bytes() for p in self.s.vault.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_same_batch_stops_at_changed_default_without_rolling_back_first(self):
        self.capture('later',seq=3)
        def response():
            self.configure('legacy')
            return output(self.create(),self.no_memory())
        b=Backend(response)
        r=self.process(b)
        self.assertEqual(len(b.calls),1)
        self.assertEqual(r['memories_written'],1)
        self.assertEqual(r['coverage_status'],'partial')
        self.assertIn('processing_pipeline_changed',[x.get('code') for x in r['results']])

    def test_due_cleanup_is_shared_without_model_or_memory_deletion(self):
        self.process(Backend(output(self.create(),self.no_memory())))
        state=self.ledger();state['sessions']['hermes/s']['processed_turns'][0]['eligible_cleanup_at']='2000-01-01T00:00:00Z'
        atomic_write_json(self.s.vault.processed_state_path,state)
        r=self.process()
        self.assertEqual(r['cleaned_turns'],1)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)
        self.assertEqual(r['model_calls'],0)

    def test_partial_is_not_cleaned(self):
        self.process(Backend(output(self.create(), {"action":"UPDATE","target":"missing",
                                                   "evidence":["e2"],"patch":{"body":"bad"}})))
        state=self.ledger();state['sessions']['hermes/s']['processed_turns'][0]['eligible_cleanup_at']='2000-01-01T00:00:00Z'
        atomic_write_json(self.s.vault.processed_state_path,state)
        r=self.process()
        self.assertEqual(r['cleaned_turns'],0)
        self.assertTrue((self.s.vault.inbox_path/'hermes'/'s.md').exists())

    def test_real_process_reopen_returns_no_new_model_call(self):
        self.process(Backend(output(self.create(),self.no_memory())))
        code='''from memleaf import Memleaf
import json,sys
s=Memleaf(sys.argv[1]); print(json.dumps(s.process(pipeline="incremental",source="hermes",session_id="s")))
'''
        p=subprocess.run([sys.executable,'-c',code,str(self.s.vault.root)],capture_output=True,text=True,check=True)
        result=json.loads(p.stdout)
        self.assertEqual(result['memories_written'],0); self.assertEqual(result['model_calls'],0)

    def test_corrupt_run_remains_visible_and_does_not_reset(self):
        r=self.process(Backend(ModelError(code='model_timeout')))
        state=self.ledger();next(iter(state['incremental_runs'].values()))['checksum']='bad'
        atomic_write_json(self.s.vault.processed_state_path,state)
        before=self.s.vault.processed_state_path.read_bytes()
        b=EchoBackend();r=self.process(b)
        self.assertEqual(len(b.calls),0);self.assertEqual(r['coverage_status'],'partial')
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())
