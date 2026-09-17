from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.incremental_run_state import KEY, load_run
from memleaf.inbox import parse_inbox_file
from memleaf.index import event_key, turn_key
from memleaf.locking import atomic_write_json
from memleaf.llm import ModelError
from test_incremental_execution import Backend, output


class TextFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Memleaf.initialize(Path(self.temp.name)/'vault')

    def ledger(self):
        p=self.s.vault.processed_state_path
        return json.loads(p.read_text()) if p.exists() else {}

    def create(self, scope='global'):
        return {'action':'CREATE','evidence':['e1'],'memory':{'type':'fact','scope':scope,
                'title':'API convention','body':'Use the stable API.'}}

    def remember(self, backend=None, **kwargs):
        args=dict(content='Use the stable API.', source='hermes',session_id='s',
                  intent_id='explicit-1',pipeline='incremental',model=backend)
        args.update(kwargs)
        return self.s.remember(**args)

    def last_run(self):
        data=self.ledger();keys=list(data.get(KEY,{}))
        return load_run(data,keys[-1])


class TextRememberRouteTests(TextFixture):
    def test_default_legacy_is_unchanged(self):
        with patch('memleaf.processing.Processor.remember',return_value={'old':True}) as f:
            self.assertEqual(self.s.remember('x'),{'old':True})
            f.assert_called_once()
        self.assertEqual(self.s.vault.config()['process']['remember_pipeline'],'legacy')

    def test_configured_and_explicit_routes_share_existing_runner(self):
        cfg=self.s.vault.config();cfg['process']['remember_pipeline']='incremental'
        save_config(self.s.vault.config_path,cfg)
        b=Backend(output(self.create()))
        with patch('memleaf.processing.Processor.remember',side_effect=AssertionError('legacy')):
            r=self.s.remember('Use the stable API.',source='hermes',session_id='s',intent_id='explicit-1',model=b)
            again=self.remember()
        self.assertEqual(r['run_id'],again['run_id']);self.assertEqual(len(b.calls),1)

    def test_automatic_pipeline_does_not_silently_switch_remember(self):
        cfg=self.s.vault.config();cfg['process']['automatic_pipeline']='incremental'
        save_config(self.s.vault.config_path,cfg)
        with patch('memleaf.processing.Processor.remember',return_value={'legacy':True}):
            self.assertEqual(self.s.remember('x'),{'legacy':True})

    def test_invalid_route_and_configuration_are_rejected(self):
        for value in ('other',[],True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):self.s.remember('x',pipeline=value)
                cfg=self.s.vault.config();cfg['process']['remember_pipeline']=value
                with self.assertRaises(ValueError):save_config(self.s.vault.config_path,cfg)
        for kwargs in ({'recover':True},{'source_time':'2026-09-17T11:00:00Z'}):
            with self.assertRaises(ValueError):self.s.remember('x',**kwargs)

    def test_single_real_user_input_has_no_fabricated_assistant(self):
        b=Backend(output(self.create()));r=self.remember(b)
        evidence=json.loads(b.calls[0][0])['evidence']
        self.assertEqual([(e['role'],e['use']) for e in evidence],[('user','new')])
        self.assertEqual(evidence[0]['text'],'Use the stable API.')
        self.assertEqual(r['memories_written'],1)
        captured=parse_inbox_file(self.s.vault.session_path('hermes',self.last_run()['session_id']))[0]
        self.assertEqual(len(captured.events),1);self.assertFalse(captured.complete)

    def test_repeat_has_no_new_write_or_model(self):
        r=self.remember(Backend(output(self.create())))
        with patch('memleaf.incremental_runtime._resolve',side_effect=AssertionError('resolve')):
            r2=self.remember()
        self.assertEqual(r['run_id'],r2['run_id']);self.assertEqual(r2['memories_written'],0)
        self.assertEqual(r2['model_calls_this_invocation'],0)
        self.assertEqual(r['memory_ids'],r2['memory_ids'])

    def test_new_authorization_has_distinct_work_but_no_duplicate_memory(self):
        r=self.remember(Backend(output(self.create())))
        r2=self.remember(Backend(output(self.create())),intent_id='explicit-2')
        self.assertNotEqual(r['run_id'],r2['run_id'])
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)
        self.assertEqual(r2['commit']['counts']['no_change'],1)

    def test_generated_intent_can_be_used_for_retry(self):
        r=self.remember(Backend(output(self.create())),intent_id=None)
        r2=self.remember(intent_id=r['intent_id'])
        self.assertEqual(r['run_id'],r2['run_id'])
        self.assertEqual(r2['reservations'],1)

    def test_event_and_turn_receipts_keep_legacy_identity(self):
        for options in ({'event_id':'evt'},{'turn_id':'turn'}):
            with self.subTest(options=options):
                r=self.remember(Backend(output(self.create())),intent_id=None,**options)
                r2=self.remember(intent_id=None,**options)
                self.assertEqual(r['run_id'],r2['run_id'])
                expected='receipt-'+hashlib.sha256(next(iter(options.values())).encode()).hexdigest()
                self.assertEqual(r['intent_id'],expected)

    def test_origin_session_receipts_are_not_consumed(self):
        self.s.capture('hermes','s','real','user','First')
        self.s.capture('hermes','s','real','assistant','Second')
        old=deepcopy(self.ledger()['sessions']['hermes/s'])
        self.remember(Backend(output(self.create())))
        self.assertEqual(self.ledger()['sessions']['hermes/s'],old)

    def test_text_source_is_excluded_from_both_automatic_routes(self):
        from memleaf.process_journal import ProcessJournal
        self.remember(Backend(output(self.create())))
        self.assertEqual(ProcessJournal(self.s)._turns_by_session(),{})
        r=self.s.process(pipeline='incremental')
        self.assertEqual((r['pending_inbox_turns'],r['attempted_turns']),(0,0))
        self.assertEqual(self.s.process()['processed_turns'],0)

    def test_foreign_direct_auto_or_wrong_intent_is_rejected(self):
        from memleaf.incremental_selection import bind_selection
        from memleaf.incremental_execution import run_incremental
        self.remember(Backend(ModelError(code='model_timeout')))
        r=self.last_run();args={k:r['arguments'][k] for k in ('source','session_id','turn_id')}
        with self.assertRaisesRegex(ValueError,'explicit_text_requires|already_owned'):
            self.s.run_incremental(**args)
        selection,request=bind_selection('wrong',r['arguments']['selection']['source_refs'],'Remember the submitted content.')
        with self.assertRaisesRegex(ValueError,'explicit_text_requires|already_owned'):
            run_incremental(self.s,selection=selection,retention_request=request,**args)

    def test_same_intent_changed_text_time_scope_or_origin_rejected(self):
        self.remember(Backend(output(self.create())))
        for changes in ({'content':'Other text'},{'source_time':'2026-09-17T00:00:00Z'},
                        {'scopes':'project:Atlas'},{'event_id':'other'},{'turn_id':'other'}):
            with self.subTest(changes=changes):
                b=Backend(output(self.create()))
                with self.assertRaises(ValueError):self.remember(b,**changes)
                self.assertFalse(b.calls)

    def test_unknown_source_time_does_not_borrow_processing_day(self):
        row={'action':'CREATE','evidence':['e1'],'memory':{'type':'todo','scope':'global',
             'title':'Task','body':'Deliver report.','status':'active','deadline':{'ref':'e1','text':'明天'}}}
        b=Backend(output(row));r=self.remember(b,content='明天交付报告。')
        e=json.loads(b.calls[0][0])['evidence'][0];self.assertIsNone(e['source_time'])
        memory=self.s.read(r['memory_ids'][0]);self.assertIsNone(memory.due_date)
        self.assertEqual(memory.extra['due_text'],'明天')

    def test_explicit_original_time_anchors_local_day(self):
        row={'action':'CREATE','evidence':['e1'],'memory':{'type':'todo','scope':'global',
             'title':'Task','body':'Deliver report.','status':'active','deadline':{'ref':'e1','text':'明天'}}}
        r=self.remember(Backend(output(row)),content='明天交付报告。',source_time='2026-09-17T00:10:00+09:00')
        self.assertEqual(self.s.read(r['memory_ids'][0]).due_date,'2026-09-18')

    def test_completion_updates_same_todo_and_preserves_fields(self):
        self.s.create_memory(memory_id='mem-t',title='Task',body='Deliver report.',type='todo',
                             status='active',scopes=['global'],due_date='2026-09-20',custom={'a':1})
        b=Backend(output({'action':'UPDATE','evidence':['e1'],'target':'m1','patch':{'status':'completed'}}))
        r=self.remember(b,content='Deliver report is completed.')
        self.assertEqual(r['memory_ids'],['mem-t'])
        memory=self.s.read('mem-t');self.assertEqual(memory.status,'completed')
        self.assertEqual(memory.due_date,'2026-09-20');self.assertEqual(memory.extra['custom'],{'a':1})

    def test_explicit_no_memory_remains_visible_partial(self):
        r=self.remember(Backend(output({'action':'NO_MEMORY','evidence':['e1']})))
        self.assertEqual(r['execution_status'],'completed_with_unresolved')
        self.assertFalse(r['memory_ids'])
        self.assertTrue(any(x['code']=='explicit_retention_required' for x in r['commit']['issues']))

    def test_scope_boundary_does_not_relabel_content(self):
        r=self.remember(Backend(output(self.create())),scopes='project:Atlas')
        self.assertEqual(r['execution_status'],'completed_with_unresolved')
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_explicit_named_new_scope_registers(self):
        r=self.remember(Backend(output(self.create('s1'))),scopes='project:Atlas')
        self.assertEqual(r['execution_status'],'completed')
        self.assertEqual(self.s.read(r['memory_ids'][0]).scopes,['project:Atlas'])

    def test_origin_stop_recording_is_respected_without_new_body(self):
        self.s.capture('hermes','s','stop','user','Do not record anything from now on.')
        b=Backend(output(self.create()));before=set(self.s.vault.list_markdown('inbox'))
        with self.assertRaisesRegex(ValueError,'recording_revoked'):self.remember(b)
        self.assertFalse(b.calls);self.assertEqual(set(self.s.vault.list_markdown('inbox')),before)

    def test_original_turn_privacy_is_respected(self):
        self.s.capture('hermes','s','private','user','private',record=False)
        with self.assertRaisesRegex(ValueError,'recording_revoked'):
            self.remember(Backend(output(self.create())),turn_id='private')

    def test_content_control_is_not_stored_as_memory(self):
        b=Backend(output(self.create()));r=self.remember(b,content='Do not remember this confidential text.')
        self.assertEqual(r['execution_status'],'suppressed');self.assertFalse(b.calls)
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_input_validation_before_any_capture(self):
        for kwargs in ({'content':''},{'content':['x']},{'content':'a\0b'},
                       {'content':'x'*65537},{'source_time':'2026-09-17'},
                       {'recover':'yes'},{'text':'different'},{'intent_id':1},{'event_id':[]}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((ValueError,TypeError)):self.remember(Backend(),**kwargs)
        self.assertFalse(self.s.vault.list_markdown('inbox'))

    def test_secrets_redacted_before_capture_and_model(self):
        b=Backend(output(self.create()));self.remember(b,content='token=sk-0123456789abcdefghijklmnopqrstuvwxyzABCDE')
        self.assertNotIn('sk-0123456789abcdefghijklmnopqrstuvwxyzABCDE',b.calls[0][0])
        for path in self.s.vault.list_markdown('inbox'):
            self.assertNotIn('sk-0123456789abcdefghijklmnopqrstuvwxyzABCDE',path.read_text())

    def test_terminal_payload_stripped_but_source_has_cleanup_deadline(self):
        self.remember(Backend(output(self.create())))
        r=self.last_run()
        self.assertNotIn('request',r);self.assertNotIn('response',r)
        entries=self.ledger()['sessions'][f"{r['source']}/{r['session_id']}"]['processed_turns']
        self.assertIsNotNone(entries[0]['eligible_cleanup_at'])

    def test_cleanup_does_not_prevent_completed_receipt_replay(self):
        from memleaf.process_journal import ProcessJournal
        self.remember(Backend(output(self.create())))
        with self.s.vault.lock():
            state=self.ledger();ProcessJournal(self.s)._cleanup_due_unlocked(state,'2099-01-01T00:00:00Z',24)
        r=self.remember();self.assertEqual(r['execution_status'],'completed')
        self.assertEqual(r['model_calls_this_invocation'],0)

    def test_legacy_receipt_cannot_refresh_budget_in_new_route(self):
        from memleaf.remember_route import _identity
        intent,raw,turn,preimage=_identity('hermes','s',None,None,'explicit-1')
        state=self.ledger();state.setdefault('events',{})[event_key(preimage)]={'request_kind':'explicit_remember','payload_digest':'old'}
        atomic_write_json(self.s.vault.processed_state_path,state)
        b=Backend(output(self.create()))
        with self.assertRaisesRegex(ValueError,'pipeline_changed'):self.remember(b)
        self.assertFalse(b.calls)

    def test_new_receipt_cannot_refresh_budget_via_old_route(self):
        self.remember(Backend(output(self.create())))
        from memleaf.process_common import ProcessingError
        with self.assertRaisesRegex(ProcessingError,'intent reused'):
            self.remember(Backend(),pipeline='legacy')
        self.assertEqual(len(self.ledger()[KEY]),1)

    def test_mcp_existing_remember_tool_exposes_route_without_new_tool(self):
        from memleaf.mcp_server import _TOOLS, _invoke_tool
        schema=next(x for x in _TOOLS if x['name']=='remember')['inputSchema']['properties']
        self.assertEqual(schema['pipeline']['enum'],['legacy','incremental'])
        self.assertIn('source_time',schema);self.assertIn('recover',schema)
        with patch('memleaf.incremental_runtime._resolve',return_value=Backend(output(self.create()))):
            r=_invoke_tool(self.s,'remember',{'content':'Use the stable API.','intent_id':'mcp','pipeline':'incremental'})
        self.assertFalse(r.get('isError'));self.assertEqual(r['structuredContent']['execution_status'],'completed')

    def test_origin_is_traceable_after_capture_cleanup_but_not_model_control(self):
        b=Backend(output(self.create()));r=self.remember(b,turn_id='real-caller-turn')
        m=self.s.read(r['memory_ids'][0])
        # Persistent provenance distinguishes API transport from caller origin.
        src=m['sources'][0]
        self.assertEqual(src['origin_session_id'],'s')
        self.assertEqual(src['origin_turn_key'],turn_key('real-caller-turn'))
        self.assertEqual(src['input_kind'],'explicit_text')
        self.assertNotIn('explicit_input',b.calls[0][0])
        self.assertNotIn('request_hash',b.calls[0][0])
