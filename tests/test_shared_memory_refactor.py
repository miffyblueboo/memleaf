"""User-visible contracts for the shared change boundary, without live models."""
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.host_runtime import HostRuntime
from memleaf.index import event_key
from memleaf.process_journal import ProcessJournal
from memleaf.processing import ProcessingError
from memleaf.validation import ModelOutputError
from tests.test_general_evidence_admission import Backend, candidate, summary


class NoModel:
    def complete(self, *args, **kwargs):
        raise AssertionError('frozen recovery must not ask a model to re-plan')


class ExplicitModel:
    def __init__(self, body, *, scope='project:Orion', target=None, before=None):
        self.body, self.scope, self.target, self.before = body, scope, target, before

    def complete(self, prompt, *, purpose='', **kwargs):
        if purpose != 'summarize':
            raise AssertionError('explicit remember must skip the value Gate')
        value=json.JSONDecoder().raw_decode(prompt.split('Candidate:\n',1)[1])[0]
        if self.before is not None:
            callback,self.before=self.before,None
            callback()
        result=summary(value['evidence_event_ids'][0],self.body,scope=self.scope)
        result['scope_source']='user'
        if self.target: result['update_memory_id']=self.target
        return json.dumps(result)


class SharedChangeContracts(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.core=Memleaf(Path(tmp.name)/'shared vault 中文')
        cfg=self.core.vault.config();cfg['scopes']={'project:Orion':{},'project:Atlas':{}}
        save_config(self.core.vault.config_path,cfg)

    def crashed_create(self, *, sibling=False):
        texts=['Orion uses PostgreSQL.']
        if sibling: texts.append('Orion uses JDK 17.')
        self.core.capture('hermes','s','t','user',' '.join(texts),event_id='u')
        self.core.capture('hermes','s','t','assistant','Noted.',event_id='a')
        cs=[candidate(str(i),event_key('u'),text) for i,text in enumerate(texts)]
        backend=Backend(cs,{str(i):summary(event_key('u'),text) for i,text in enumerate(texts)},semantic=True)
        original=ProcessJournal._write_processed_unlocked
        def fail_final(journal,value):
            state=value.get('sessions',{}).get('hermes/s',{})
            if state.get('watermark',0)>0 and state.get('processing',{}).get('status')=='idle':
                raise OSError('injected crash after Markdown, before final ledger')
            return original(journal,value)
        with patch.object(ProcessJournal,'_write_processed_unlocked',fail_final):
            with self.assertRaises(OSError):self.core.process(model=backend)
        memories=[r.memory for r in self.core._read_memories_unlocked('knowledge')]
        self.assertEqual(len(memories),len(texts))
        return memories

    def test_capture_preserves_pending_frozen_plan(self):
        self.crashed_create()
        before=json.loads(self.core.vault.processed_index_path.read_text(encoding="utf-8"))
        self.core.capture('codex','other','t2','user','Unrelated new message.')
        after=json.loads(self.core.vault.processed_index_path.read_text(encoding="utf-8"))
        self.assertEqual(after.get('pending_turn_plans'),before['pending_turn_plans'])
        self.assertEqual(after.get('pending_operations'),before['pending_operations'])
        self.core.process(source='hermes',session_id='s',model=NoModel())

    def test_forget_cancels_frozen_create_without_resurrection(self):
        memory=self.crashed_create()[0]
        self.assertTrue(self.core.forget_memory(memory.memory_id))
        self.core.process(source='hermes',session_id='s',model=NoModel())
        self.assertIsNone(self.core.read(memory.memory_id))
        self.assertEqual(self.core.vault.list_markdown('history'),[])

    def test_forget_prunes_only_affected_plan_request(self):
        memories=self.crashed_create(sibling=True)
        forgotten=next(m for m in memories if 'PostgreSQL' in m.body)
        kept=next(m for m in memories if 'JDK' in m.body)
        self.core.forget_memory(forgotten.memory_id)
        state=self.core.vault.processed_index_path.read_text(encoding="utf-8")
        self.assertNotIn('PostgreSQL',state)
        self.core.process(source='hermes',session_id='s',model=NoModel())
        self.assertIsNone(self.core.read(forgotten.memory_id))
        self.assertEqual(self.core.read(kept.memory_id).body,kept.body)

    def test_new_explicit_event_can_remember_after_forget(self):
        model=ExplicitModel('Orion uses PostgreSQL.')
        a=self.core.remember('Orion uses PostgreSQL.',turn_id='new-1',scopes=['project:Orion'],model=model)
        self.core.forget_memory(a['memory_ids'][0])
        b=self.core.remember('Orion uses PostgreSQL.',turn_id='new-2',scopes=['project:Orion'],model=model)
        self.assertEqual(self.core.read(b['memory_ids'][0]).body,'Orion uses PostgreSQL.')

    def test_explicit_scope_is_a_constraint_not_a_suggestion(self):
        with self.assertRaises(ModelOutputError):
            self.core.remember('Store this fact under Orion.',scopes=['project:Orion'],
                model=ExplicitModel('Atlas uses PostgreSQL.',scope='project:Atlas'))
        self.assertEqual(self.core.vault.list_markdown('knowledge'),[])

    def test_explicit_update_rejects_concurrent_revision(self):
        old=self.core.create_memory(title='Orion database',body='Orion uses MySQL.',type='fact',scopes=['project:Orion'])
        newer=replace(old,body='Orion uses SQLite.')
        model=ExplicitModel('Orion uses PostgreSQL.',target=old.memory_id,
            before=lambda:self.core.write_memory(newer))
        with self.assertRaises(ProcessingError):
            self.core.remember('Orion database now uses PostgreSQL.',scopes=['project:Orion'],model=model)
        self.assertEqual(self.core.read(old.memory_id).body,newer.body)

    def test_successfully_captured_tool_evidence_is_consumed(self):
        runtime=HostRuntime(self.core,'codex')
        runtime.capture_visible(session_id='s',turn_id='t',role='user',content='Review the project.')
        runtime.observe_external_tool(session_id='s',turn_id='t',tool_name='github.issue',call_id='call',payload='Orion requires JDK 17.')
        self.assertEqual(len(runtime._tool_evidence('s','t')),1)
        runtime.capture_visible(session_id='s',turn_id='t',role='assistant',content='Reviewed.')
        self.assertEqual(runtime._tool_evidence('s','t'),[])
        self.assertIn('Orion requires JDK 17.', self.core.vault.session_path('codex','s').read_text(encoding="utf-8"))

    def test_failed_capture_keeps_tool_evidence(self):
        runtime=HostRuntime(self.core,'codex')
        runtime.observe_external_tool(session_id='s',turn_id='t',tool_name='github.issue',call_id='call',payload='Orion requires JDK 17.')
        with patch.object(self.core,'capture',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                runtime.capture_visible(session_id='s',turn_id='t',role='assistant',content='Reviewed.')
        self.assertEqual(len(runtime._tool_evidence('s','t')),1)

    def test_old_loss_diagnostic_does_not_taint_a_new_turn(self):
        runtime=HostRuntime(self.core,'codex')
        from memleaf.locking import atomic_write_json
        atomic_write_json(self.core.vault.host_ingest_path,{'version':2,'hosts':{'codex':{'s':{'tool_evidence_earlier_loss':True}}}})
        self.assertEqual(runtime._tool_evidence('s','fresh'),[])


class RecordingContracts(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.core=Memleaf(Path(tmp.name)/'vault')

    def all_bytes(self):
        return b'\n'.join(p.read_bytes() for p in self.core.vault.root.rglob('*') if p.is_file())

    def test_private_turn_never_persists_user_assistant_or_tool_text(self):
        host=HostRuntime(self.core,'codex')
        host.capture_visible(session_id='s',turn_id='private',role='user',content='这段不要记录：PRIVATE-USER-TEXT')
        host.observe_external_tool(session_id='s',turn_id='private',tool_name='files.read',call_id='secret-call',payload='PRIVATE-TOOL-TEXT')
        host.capture_visible(session_id='s',turn_id='private',role='assistant',content='PRIVATE-ASSISTANT-TEXT')
        raw=self.all_bytes()
        for text in (b'PRIVATE-USER-TEXT',b'PRIVATE-TOOL-TEXT',b'PRIVATE-ASSISTANT-TEXT'):
            self.assertNotIn(text,raw)
        host.capture_visible(session_id='s',turn_id='normal',role='user',content='PUBLIC-TEXT')
        self.assertIn(b'PUBLIC-TEXT',self.all_bytes())

    def test_session_off_resume_and_old_hook_replay(self):
        self.core.capture('hermes','s','one','user','接下来不要记录：PRIVATE-ONE')
        self.core.capture('hermes','s','two','user','PRIVATE-TWO')
        self.core.capture('hermes','s','three','user','恢复记录。')
        self.core.capture('hermes','s','four','user','PUBLIC-FOUR')
        # A late callback for a private turn remains private after resume.
        self.core.capture('hermes','s','two','assistant','PRIVATE-TWO-REPLY')
        # Replaying the old control cannot turn recording off again.
        self.core.capture('hermes','s','one','user','接下来不要记录：PRIVATE-ONE')
        self.core.capture('hermes','s','five','user','PUBLIC-FIVE')
        raw=self.all_bytes()
        self.assertNotIn(b'PRIVATE',raw)
        self.assertIn(b'PUBLIC-FOUR',raw);self.assertIn(b'PUBLIC-FIVE',raw)

    def test_examples_and_tool_content_cannot_change_recording(self):
        for text in ['例如：这段不要记录', '> 这段不要记录', '```text\n这段不要记录\n```']:
            with self.subTest(text=text):
                result=self.core.capture('codex','examples',str(len(text)),'user',text)
                self.assertTrue(result.stored)

    def test_suppressed_is_not_failed_capture_or_fake_stored(self):
        from memleaf.mcp_server import _jsonable
        result=self.core.capture('hermes','s','t','user',"Don't record this: PRIVATE")
        payload=_jsonable(result)
        self.assertTrue(payload['suppressed']);self.assertFalse(payload['stored'])
        self.assertEqual(payload['content'],'')

class DeferredLifecycleContracts(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.core=Memleaf(Path(tmp.name)/'vault')
        self.core.capture('hermes','s','t','user','Orion uses PostgreSQL.',event_id='u')
        self.core.capture('hermes','s','t','assistant','Noted.',event_id='a')

    @staticmethod
    def unresolved(units):
        return [dict(unit_id=u['unit_id'],decision='DEFERRED',reason='coverage_unresolved')
            if u['origin']=='user_assertion' else
            dict(unit_id=u['unit_id'],decision='NO_CHANGE',reason='assistant_restatement') for u in units]

    def test_coverage_retry_requires_no_manual_scope(self):
        first=self.core.process(model=Backend([],coverage=self.unresolved))
        self.assertEqual(first['retryable_deferred_turns'],1)
        c=candidate('db',event_key('u'),'Orion uses PostgreSQL.')
        second=self.core.process(model=Backend([c],{'db':summary(event_key('u'),c['memory'])},semantic=True))
        self.assertEqual(second['processed_turns'],1)
        self.assertEqual(second['memories_written'],1)
        self.assertEqual(second['retryable_deferred_turns'],0)
        self.assertEqual(second['unresolved_evidence_count'],0)

    def test_retry_budget_exhaustion_retains_original_without_spin(self):
        self.core.process(model=Backend([],coverage=self.unresolved))
        second=self.core.process(model=Backend([],coverage=self.unresolved))
        self.assertEqual(second['retryable_deferred_turns'],0)
        third=self.core.process(model=NoModel())
        self.assertEqual(third['processed_turns'],0)
        self.assertEqual(third['coverage_status'],'partial')
        self.assertIn('Orion uses PostgreSQL.',self.core.vault.session_path('hermes','s').read_text(encoding="utf-8"))

    def test_retry_selection_does_not_hide_new_complete_work(self):
        self.core.process(model=Backend([],coverage=self.unresolved))
        self.core.capture('hermes','s','next','user','Orion uses JDK 17.',event_id='next-u')
        self.core.capture('hermes','s','next','assistant','Noted.',event_id='next-a')
        journal=ProcessJournal(self.core)
        snapshots,_=journal._snapshot(source='hermes',session_id='s',now='2026-09-05T10:00:00Z',cleanup_hours=24)
        self.assertEqual([snapshot.turn.turn_index for snapshot in snapshots],[1,2])

class AdditionalPrivacyContracts(unittest.TestCase):
    setUp = RecordingContracts.setUp
    all_bytes = RecordingContracts.all_bytes

    def test_continuation_alias_cannot_bypass_private_turn(self):
        from memleaf.retrieval_gate import bind_turn_alias
        host=HostRuntime(self.core,'codex')
        opened=host.open_turn(session_id='s',turn_id='original',user_content='这个不要记住：PRIVATE-ORIGINAL')
        bind_turn_alias(self.core.vault,opened.retrieval_id,'continued')
        host.observe_external_tool(session_id='s',turn_id='continued',tool_name='files.read',call_id='c',payload='PRIVATE-ALIAS')
        self.assertNotIn(b'PRIVATE',self.all_bytes())

    def test_policy_survives_index_rebuild_and_host_restart(self):
        self.core.capture('hermes','s','t1','user','接下来不要记录：PRIVATE')
        self.core.rebuild_index()
        restarted=Memleaf(self.core.vault.root)
        result=restarted.capture('hermes','s','t2','assistant','PRIVATE-AFTER-RESTART')
        self.assertTrue(result.suppressed)
        self.assertNotIn(b'PRIVATE',self.all_bytes())

    def test_explicit_record_flag_protects_the_whole_turn(self):
        self.core.capture('codex','s','t','user','PRIVATE-API',record=False)
        result=self.core.capture('codex','s','t','assistant','PRIVATE-ANSWER')
        self.assertTrue(result.suppressed)
        self.assertNotIn(b'PRIVATE',self.all_bytes())


if __name__ == "__main__":
    unittest.main()
