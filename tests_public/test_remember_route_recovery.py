from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch

from test_remember_route import TextFixture
from test_incremental_execution import Backend,output
from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.extraction_work_state import _budget_path
from memleaf.incremental_execution import IncrementalRunError
from memleaf.incremental_journal import load_work
from memleaf.incremental_selection import bind_selection
from memleaf.inbox import parse_inbox_file
from memleaf.llm import ModelError
from memleaf.locking import atomic_write_json


class TextRememberRecoveryTests(TextFixture):
    def test_network_retry_requires_explicit_recover_and_same_allowance(self):
        b=Backend(ModelError(code='model_timeout'),output(self.create()))
        first=self.remember(b,source_time='2026-09-17T23:58:00+09:00')
        again=self.remember(b,source_time='2026-09-17T23:58:00+09:00')
        self.assertEqual(first['run_id'],again['run_id']);self.assertEqual(len(b.calls),1)
        final=self.remember(b,recover=True,source_time='2026-09-17T23:58:00+09:00')
        self.assertEqual(final['execution_status'],'completed');self.assertEqual(final['reservations'],2)
        self.assertEqual(b.calls[0][0],b.calls[1][0])

    def test_invalid_json_stops_after_two_and_does_not_reset(self):
        b=Backend('{','{')
        r=self.remember(b);self.assertEqual(r['execution_status'],'failed')
        self.remember(b,recover=True);self.s.resume_incremental_run(r['run_id'],backend=b)
        self.assertEqual(len(b.calls),2)

    def test_saved_response_recovers_without_model_configuration(self):
        b=Backend(output(self.create()))
        with patch('memleaf.incremental_execution.apply_incremental',side_effect=OSError('fixture')):
            with self.assertRaises(IncrementalRunError):self.remember(b)
        with patch('memleaf.incremental_runtime._resolve',side_effect=AssertionError('model')):
            r=self.remember()
        self.assertEqual(r['execution_status'],'completed');self.assertEqual(r['memories_written'],1)
        self.assertEqual(r['model_calls_this_invocation'],0);self.assertEqual(len(b.calls),1)

    def test_writer_failure_preserves_frozen_id(self):
        b=Backend(output(self.create()))
        with patch('memleaf.memory_writer.MemoryWriter.write_frozen_unlocked',side_effect=OSError('fixture')):
            with self.assertRaises(IncrementalRunError) as c:self.remember(b)
        w=load_work(self.ledger(),c.exception.result['commit_work_id']);mid=w['operations'][0]['memory_id']
        r=self.remember()
        self.assertEqual(r['memory_ids'],[mid]);self.assertEqual(len(b.calls),1)

    def test_index_failure_is_zero_call_recovery(self):
        b=Backend(output(self.create()))
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('fixture')):
            with self.assertRaises(IncrementalRunError):self.remember(b)
        r=self.remember();self.assertEqual(r['execution_status'],'completed')
        self.assertEqual(len(b.calls),1);self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_source_write_then_receipt_failure_reuses_input_and_origin(self):
        b=Backend(output(self.create()))
        with patch('memleaf.capture.atomic_write_json',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.remember(b)
        self.assertFalse(b.calls)
        paths=self.s.vault.list_markdown('inbox');self.assertEqual(len(paths),1)
        e=parse_inbox_file(paths[0])[0].events[0]
        self.assertIsNotNone(e.explicit_input);self.assertFalse(parse_inbox_file(paths[0])[0].complete)
        self.assertEqual(self.s.process(pipeline='incremental')['pending_inbox_turns'],0)
        r=self.remember(b);self.assertEqual(r['execution_status'],'completed')
        e2=parse_inbox_file(paths[0])[0].events[0]
        self.assertEqual(e.event_key,e2.event_key);self.assertEqual(e.captured_at,e2.captured_at)

    def test_failure_before_source_write_does_not_consume_budget(self):
        with patch('memleaf.capture.atomic_write_text',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.remember(Backend())
        self.assertFalse(_budget_path(self.s.vault).exists())
        r=self.remember(Backend(output(self.create())))
        self.assertEqual(r['reservations'],1)

    def test_capture_gap_rejects_changed_scope_under_same_intent(self):
        with patch('memleaf.capture.atomic_write_json',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.remember(Backend())
        b=Backend(output(self.create()))
        with self.assertRaises(ValueError):self.remember(b,scopes='project:Other')
        self.assertFalse(b.calls)

    def test_direct_selected_api_cannot_expand_precaptured_scope(self):
        with patch('memleaf.capture.atomic_write_json',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.remember(Backend(),scopes='project:Atlas')
        t=parse_inbox_file(self.s.vault.list_markdown('inbox')[0])[0]
        from memleaf.capture import recover_capture_receipts_unlocked
        with self.s.vault.lock():
            state=self.ledger();recover_capture_receipts_unlocked(self.s.vault,state,self.s.vault.list_markdown('inbox')[0])
            atomic_write_json(self.s.vault.processed_state_path,state)
        b=Backend(output(self.create()))
        with self.assertRaisesRegex(ValueError,'explicit_text_binding_changed'):
            self.s.remember_incremental(source=t.source,session_id=t.session_id,turn_id=t.events[0].turn_id,
                                       intent_id='explicit-1',selected_source_refs=[t.events[0].event_key],
                                       retention_request='Remember the submitted content.',model=b)
        self.assertFalse(b.calls)

    def test_original_session_revocation_during_call_blocks_write(self):
        def revoke():
            self.s.capture('hermes','s','stop','user','Do not record anything from now on.')
            return output(self.create())
        r=self.remember(Backend(revoke));self.assertEqual(r['execution_status'],'blocked')
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_original_privacy_checked_on_direct_resume(self):
        r=self.remember(Backend(ModelError(code='model_timeout')))
        self.s.capture('hermes','s','stop','user','Do not record anything from now on.')
        b=Backend(output(self.create()));r=self.s.resume_incremental_run(r['run_id'],backend=b)
        self.assertEqual(r['execution_status'],'blocked');self.assertFalse(b.calls)

    def test_unrelated_authorized_session_not_blocked(self):
        self.s.capture('hermes','private','stop','user','Do not record anything from now on.')
        r=self.remember(Backend(output(self.create())))
        self.assertEqual(r['execution_status'],'completed')

    def test_external_source_edit_blocks_response(self):
        def change():
            p=self.s.vault.list_markdown('inbox')[0]
            p.write_text(p.read_text().replace('Use the stable API.','Use another API.'))
            return output(self.create())
        r=self.remember(Backend(change));self.assertEqual(r['execution_status'],'blocked')
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_metadata_edit_cannot_preserve_old_input_digest(self):
        def change():
            p=self.s.vault.list_markdown('inbox')[0]
            p.write_text(p.read_text().replace('"session_id":"s"','"session_id":"other"'))
            return output(self.create())
        r=self.remember(Backend(change));self.assertEqual(r['execution_status'],'blocked')
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_unknown_origin_version_does_not_become_plain_user_source(self):
        self.remember(Backend(ModelError(code='model_timeout')))
        p=self.s.vault.list_markdown('inbox')[0]
        p.write_text(p.read_text().replace('"version":1','"version":9'))
        with self.assertRaisesRegex(ValueError,'explicit_text_origin'):parse_inbox_file(p)

    def test_lost_budget_does_not_grant_two_more_requests(self):
        self.remember(Backend(ModelError(code='model_timeout')))
        _budget_path(self.s.vault).unlink();b=Backend(output(self.create()))
        r=self.remember(b,recover=True);self.assertEqual(r['execution_status'],'blocked');self.assertFalse(b.calls)

    def test_damaged_capture_state_is_not_replaced_with_empty_state(self):
        p=self.s.vault.processed_state_path;p.write_text('{bad')
        b=Backend()
        with self.assertRaises((ValueError,OSError)):self.remember(b)
        self.assertEqual(p.read_text(),'{bad');self.assertFalse(b.calls)
        self.assertFalse(self.s.vault.list_markdown('inbox'))

    def test_native_no_change_does_not_make_local_copy(self):
        p=Path(self.temp.name)/'native.md';original='# Stable API\nUse the stable API.\n';p.write_text(original)
        cfg=self.s.vault.config();cfg['native_sources']={'notes':{'path':str(p),'agent':'hermes','share':False,'enabled':True}}
        save_config(self.s.vault.config_path,cfg)
        r=self.remember(Backend(output({'action':'NO_CHANGE','evidence':['e1'],'target':'m1'})))
        self.assertEqual(r['execution_status'],'completed');self.assertEqual(r['memory_ids'],[])
        self.assertEqual(r['memories_written'],0);self.assertFalse(self.s.vault.list_markdown('knowledge'))
        self.assertEqual(p.read_text(),original)

    def test_local_partial_repair_reuses_single_user_source(self):
        row=self.create();row['evidence']='e1'
        r=self.remember(Backend(output(row)))
        self.assertEqual(r['execution_status'],'completed_with_unresolved')
        r2=self.s.recover_incremental_partial(r['run_id'],mode='repair')
        self.assertEqual(r2['execution_status'],'completed');self.assertEqual(r2['reserved_requests'],1)
        self.assertEqual(self.remember()['execution_status'],'completed')

    def test_partial_can_replan_with_new_current_target(self):
        row={'action':'DEFERRED','evidence':['e1'],'reason':'missing_identity','need':'Which API convention?'}
        r=self.remember(Backend(output(row)))
        self.s.create_memory(memory_id='mem-context',title='API convention',body='Use the stable API.',scopes=['global'])
        # m1 did not exist in the first empty catalog, so it is the appended reference.
        b=Backend(output({'action':'NO_CHANGE','evidence':['e1'],'target':'m1'}))
        r2=self.s.recover_incremental_partial(r['run_id'],context_memory_ids=['mem-context'],model=b)
        self.assertEqual(r2['execution_status'],'completed');self.assertEqual(r2['reserved_requests'],2)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_forget_cancels_original_intent_and_new_intent_can_retain(self):
        r=self.remember(Backend(output(self.create())));mid=r['memory_ids'][0]
        self.s.forget_memory(mid)
        r2=self.remember();self.assertEqual(r2['execution_status'],'cancelled')
        r3=self.remember(Backend(output(self.create())),intent_id='new-real-request')
        self.assertEqual(r3['execution_status'],'completed');self.assertNotEqual(mid,r3['memory_ids'][0])

    def test_same_intent_concurrency_cannot_double_dispatch(self):
        entered,release=threading.Event(),threading.Event();results=[];errors=[]
        def response():
            entered.set()
            if not release.wait(5):raise RuntimeError('test timeout')
            return output(self.create())
        b=Backend(response)
        def worker():
            try:results.append(self.remember(b))
            except Exception as e:errors.append(e)
        th=threading.Thread(target=worker);th.start();self.assertTrue(entered.wait(5))
        try:
            with self.assertRaisesRegex(ValueError,'busy|owned'):self.remember(Backend(output(self.create())))
        finally:release.set();th.join(5)
        self.assertFalse(th.is_alive());self.assertFalse(errors);self.assertEqual(len(b.calls),1)
        self.assertEqual(results[0]['execution_status'],'completed')

    def test_real_process_exit_after_response_recovers_without_model(self):
        script='''import os,sys
from pathlib import Path
from unittest.mock import patch
from memleaf import Memleaf
class B:
 single_pass_safe=True
 def complete(self,prompt,**kw):
  return '{"items":[{"action":"CREATE","evidence":["e1"],"memory":{"type":"fact","scope":"global","title":"API convention","body":"Use the stable API."}}]}'
s=Memleaf(Path(sys.argv[1]))
with patch('memleaf.incremental_execution.apply_incremental',side_effect=lambda *a,**k:os._exit(73)):
 s.remember('Use the stable API.',source='hermes',session_id='s',intent_id='explicit-1',pipeline='incremental',model=B())
'''
        cp=subprocess.run([sys.executable,'-c',script,str(self.s.vault.root)],capture_output=True,text=True,timeout=15)
        self.assertEqual(cp.returncode,73,cp.stderr)
        self.s=Memleaf(self.s.vault.root)
        r=self.remember();self.assertEqual(r['execution_status'],'completed')
        self.assertEqual(r['model_calls_this_invocation'],0);self.assertEqual(r['reservations'],1)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_recover_without_stable_identity_cannot_create_new_allowance(self):
        b=Backend(output(self.create()))
        with self.assertRaisesRegex(ValueError,'recovery_requires_identity'):
            self.remember(b,intent_id=None,recover=True)
        self.assertFalse(b.calls);self.assertFalse(self.s.vault.list_markdown('inbox'))
