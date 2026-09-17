from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.incremental_commit import IncrementalCommitError
from memleaf.incremental_journal import KEY
from memleaf.index import turn_key
from memleaf.locking import atomic_write_json
from memleaf.memory_writer import MemoryWriter
from memleaf.models import utc_now
from memleaf.process_journal import ProcessJournal
from incremental_test_support import IncrementalFixture


class IncrementalRecoveryTests(IncrementalFixture):
    def pending(self, *, update=False):
        if update:self.target()
        req=self.request([self.update() if update else self.create(),self.no_memory()],**({'priority_memory_ids':['mem-old']} if update else {}))
        with patch.object(MemoryWriter,'write_frozen_unlocked',side_effect=OSError('before write')):
            with self.assertRaises(IncrementalCommitError):self.s.apply_incremental(**req)
        return req,self.works()[0]

    def test_pending_create_reuses_permanent_id(self):
        req,work=self.pending();result=self.s.resume_incremental(work['work_id'])
        self.assertEqual(result['operations'][0]['memory_id'],work['operations'][0]['memory_id'])
        self.assertEqual(result['execution_status'],'completed');self.assertEqual(self.s.apply_incremental(**req),result)

    def test_index_failure_is_recovered_without_duplicate_history(self):
        self.target();req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old'])
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index failure')):
            with self.assertRaises(IncrementalCommitError) as caught:self.s.apply_incremental(**req)
        self.assertEqual(caught.exception.result['index_status'],'dirty');self.assertEqual(self.s.read('mem-old').status,'completed')
        self.assertEqual(self.s.apply_incremental(**req)['execution_status'],'completed');self.assertEqual(len(self.hist()),1)

    def test_dirty_index_reads_current_markdown(self):
        req=self.request([self.create(),self.no_memory()])
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index failure')):
            with self.assertRaises(IncrementalCommitError):self.s.apply_incremental(**req)
            self.assertEqual(self.s.search_candidates('Atlas task')['status'],'found')

    def stop_head(self,req,*,after):
        import memleaf.memory_writer as mw
        original=mw.atomic_write_text
        def stop(path,text,*a,**kw):
            if Path(path).parent==self.s.vault.knowledge_path and not after:raise OSError('before head')
            result=original(path,text,*a,**kw)
            if Path(path).parent==self.s.vault.knowledge_path and after:raise OSError('after head')
            return result
        with patch.object(mw,'atomic_write_text',new=stop):
            with self.assertRaises(IncrementalCommitError):self.s.apply_incremental(**req)

    def test_head_written_before_receipt_restart(self):
        self.target();req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old'])
        self.stop_head(req,after=True);self.s=Memleaf(self.s.vault.root)
        self.assertEqual(self.s.resume_incremental(self.works()[0]['work_id'])['counts']['committed'],1)
        self.assertEqual(len(self.hist()),1)

    def test_history_written_before_head_restart(self):
        self.target();req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old']);self.stop_head(req,after=False)
        self.assertEqual(len(self.hist()),1);self.assertEqual(self.s.read('mem-old').status,'active')
        self.s.resume_incremental(self.works()[0]['work_id']);self.assertEqual(len(self.hist()),1)
        self.assertEqual(self.s.read('mem-old').status,'completed')

    def test_source_revision_blocks_unapplied_write(self):
        _,w=self.pending();self.revise();result=self.s.resume_incremental(w['work_id'])
        self.assertEqual(result['counts']['committed'],0);self.assertEqual(self.s.vault.list_markdown('knowledge'),[])
        self.assertTrue(any(x.get('code')=='source_or_context_changed' for x in result['operations']))

    def test_new_context_blocks_unapplied_old_proposal(self):
        _,w=self.pending();self.capture('later',content='This task is no longer needed',seq=3)
        self.assertEqual(self.s.resume_incremental(w['work_id'])['counts']['committed'],0)

    def test_changed_target_is_not_overwritten(self):
        _,w=self.pending(update=True);self.target(body='Newer edit')
        result=self.s.resume_incremental(w['work_id']);self.assertEqual(self.s.read('mem-old').body,'Newer edit')
        self.assertEqual(result['counts']['committed'],0);self.assertEqual(result['counts']['no_memory'],1)

    def test_supported_later_mutation_acknowledges_old_application(self):
        self.target();req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old']);self.stop_head(req,after=True)
        self.target(body='Later authorized content',status='cancelled')
        result=self.s.resume_incremental(self.works()[0]['work_id'])
        self.assertEqual(result['counts']['committed'],1);self.assertEqual(self.s.read('mem-old').body,'Later authorized content')

    def test_forget_cancels_pending_body_without_replay(self):
        _,w=self.pending(update=True);self.assertTrue(self.s.forget_memory('mem-old'))
        self.assertNotIn('Deliver the report',self.s.vault.processed_state_path.read_text())
        self.s.resume_incremental(w['work_id']);self.assertIsNone(self.s.read('mem-old',include_history=True));self.assertEqual(self.hist(),[])

    def test_partial_source_is_protected_from_old_cleanup(self):
        self.s.apply_incremental(**self.request([self.no_memory('e1'),{'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'more'}]))
        state=self.ledger();entry=state['sessions']['hermes/s']['processed_turns'][0]
        entry['eligible_cleanup_at']='2000-01-01T00:00:00Z';entry.pop('deferred_evidence',None)
        atomic_write_json(self.s.vault.processed_state_path,state)
        with self.s.vault.lock():count=ProcessJournal(self.s)._cleanup_due_unlocked(state,'2030-01-01T00:00:00Z',24)
        self.assertEqual(count,0);self.assertTrue(self.s.vault.session_path('hermes','s').exists())

    def test_checksum_corruption_never_resets_receipt(self):
        result=self.s.apply_incremental(**self.request([self.create(),self.no_memory()]));state=self.ledger()
        state[KEY][result['work_id']]['checksum']='bad';atomic_write_json(self.s.vault.processed_state_path,state)
        before=self.s.vault.processed_state_path.read_bytes()
        with self.assertRaisesRegex(ValueError,'checksum'):self.s.resume_incremental(result['work_id'])
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())

    def test_duplicate_target_is_local_conflict(self):
        _,w=self.pending(update=True);path=self.s.vault.memory_path('mem-old');(path.parent/'copy.md').write_bytes(path.read_bytes())
        result=self.s.resume_incremental(w['work_id'])
        self.assertEqual(result['counts']['committed'],0);self.assertEqual(result['counts']['no_memory'],1)

    def test_legacy_plan_not_implicitly_migrated(self):
        from memleaf.turn_plan import turn_identity_key
        req=self.request([self.create(),self.no_memory()]);state=self.ledger()
        state.setdefault('pending_turn_plans',{})[turn_identity_key('hermes','s',turn_key('t'))]={'version':1}
        atomic_write_json(self.s.vault.processed_state_path,state)
        with self.assertRaisesRegex(ValueError,'legacy_pending_plan'):self.s.apply_incremental(**req)
        self.assertFalse(self.ledger().get(KEY))

    def test_live_legacy_owner_not_stolen(self):
        req=self.request([self.create(),self.no_memory()])
        snaps,_=ProcessJournal(self.s)._snapshot(source='hermes',session_id='s',now=utc_now(),cleanup_hours=24)
        self.assertEqual(len(snaps),1)
        with self.assertRaisesRegex(ValueError,'legacy_processing_busy'):self.s.apply_incremental(**req)
        self.assertFalse(self.ledger().get(KEY))

    def test_missing_update_never_becomes_create(self):
        _,w=self.pending(update=True);self.s.vault.memory_path('mem-old').unlink()
        self.assertEqual(self.s.resume_incremental(w['work_id'])['counts']['committed'],0)
        self.assertFalse(self.s.vault.memory_path('mem-old').exists())

    def test_second_target_failure_preserves_first_success(self):
        self.target('mem-a');self.target('mem-b')
        req=self.request([self.update(),{'action':'UPDATE','target':'m2','evidence':['e1'],'patch':{'status':'completed'}},self.no_memory()],priority_memory_ids=['mem-a','mem-b'])
        original=MemoryWriter.write_frozen_unlocked
        def stop(writer,op):
            if op['memory_id']=='mem-b':raise OSError('second failure')
            return original(writer,op)
        with patch.object(MemoryWriter,'write_frozen_unlocked',new=stop):
            with self.assertRaises(IncrementalCommitError) as caught:self.s.apply_incremental(**req)
        self.assertEqual(caught.exception.result['counts']['committed'],1)
        w=self.works()[0];ids=[o['operation_id'] for o in w['operations']]
        result=self.s.resume_incremental(w['work_id'])
        self.assertEqual([o['operation_id'] for o in result['operations']],ids)
        self.assertEqual(result['counts']['committed'],2);self.assertEqual(len(self.hist()),2)

    def test_shared_source_forget_cancels_pending_sibling(self):
        req=self.request([self.create(title='Task A'),self.create(title='Task B',body='Secret pending content'),self.no_memory()])
        original=MemoryWriter.write_frozen_unlocked;calls=[]
        def stop(writer,op):
            calls.append(1)
            if len(calls)==2:raise OSError('second write')
            return original(writer,op)
        with patch.object(MemoryWriter,'write_frozen_unlocked',new=stop):
            with self.assertRaises(IncrementalCommitError):self.s.apply_incremental(**req)
        w=self.works()[0];self.s.forget_memory(w['operations'][0]['memory_id'])
        self.assertNotIn('Secret pending content',self.s.vault.processed_state_path.read_text())
        self.s.resume_incremental(w['work_id']);self.assertEqual(self.s.vault.list_markdown('knowledge'),[])

    def test_freeze_ack_failure_retains_allocated_id(self):
        import memleaf.incremental_journal as journal
        req=self.request([self.create(),self.no_memory()]);original=journal.atomic_write_json
        def stop(path,value):original(path,value);raise OSError('after receipt')
        with patch.object(journal,'atomic_write_json',new=stop):
            with self.assertRaises(OSError):self.s.apply_incremental(**req)
        w=self.works()[0];self.assertEqual(self.s.vault.list_markdown('knowledge'),[])
        self.assertEqual(self.s.apply_incremental(**req)['operations'][0]['memory_id'],w['operations'][0]['memory_id'])

    def test_ten_receipt_write_fault_boundaries(self):
        import memleaf.incremental_journal as journal
        original=journal.atomic_write_json
        for ordinal in range(1,6):
            for after in (False,True):
                with self.subTest(ordinal=ordinal,after=after):
                    self.s=Memleaf.initialize(Path(self.temp.name)/f'fault-{ordinal}-{after}');self.capture();self.target()
                    req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old']);calls=[]
                    def stop(path,value):
                        calls.append(1)
                        if len(calls)==ordinal and not after:raise OSError('before receipt')
                        original(path,value)
                        if len(calls)==ordinal and after:raise OSError('after receipt')
                    with patch.object(journal,'atomic_write_json',new=stop):
                        try:self.s.apply_incremental(**req)
                        except OSError:pass
                    self.assertGreaterEqual(len(calls),ordinal)
                    self.assertEqual(self.s.apply_incremental(**req)['execution_status'],'completed')
                    self.assertEqual(self.s.read('mem-old').status,'completed');self.assertEqual(len(self.hist()),1)

    def child_request(self,req):
        path=Path(self.temp.name)/'request.json';path.write_text(json.dumps(req));return str(path)

    def test_real_process_exit_after_head(self):
        self.target();req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old'])
        code="""import json,os,sys
from pathlib import Path
from memleaf import Memleaf
import memleaf.memory_writer as mw
s=Memleaf(sys.argv[1]); request=json.loads(Path(sys.argv[2]).read_text())
original=mw.atomic_write_text
def stop(path,text,*a,**kw):
 original(path,text,*a,**kw)
 if Path(path).parent==s.vault.knowledge_path:os._exit(91)
mw.atomic_write_text=stop
s.apply_incremental(**request)
"""
        result=subprocess.run([sys.executable,'-c',code,str(self.s.vault.root),self.child_request(req)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,91,result.stderr);self.s=Memleaf(self.s.vault.root)
        self.assertEqual(self.s.resume_incremental(self.works()[0]['work_id'])['execution_status'],'completed')
        self.assertEqual(len(self.hist()),1)

    def test_two_processes_same_authorization(self):
        req=self.request([self.create(),self.no_memory()])
        code="""import json,sys
from pathlib import Path
from memleaf import Memleaf
print(json.dumps(Memleaf(sys.argv[1]).apply_incremental(**json.loads(Path(sys.argv[2]).read_text())),sort_keys=True))
"""
        command=[sys.executable,'-c',code,str(self.s.vault.root),self.child_request(req)]
        children=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        values=[]
        for child in children:
            out,err=child.communicate(timeout=20);self.assertEqual(child.returncode,0,err);values.append(json.loads(out))
        self.assertEqual(values[0],values[1]);self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_no_memory_final_source_receipt_recovers(self):
        import memleaf.incremental_journal as journal
        req=self.request([self.no_memory('e1'),self.no_memory()]);original=journal.atomic_write_json;calls=[]
        def stop(path,value):
            calls.append(1);original(path,value)
            if len(calls)==3:raise OSError('all rows written but source not settled')
        with patch.object(journal,'atomic_write_json',new=stop):
            with self.assertRaises(OSError):self.s.apply_incremental(**req)
        self.assertFalse(self.works()[0]['receipt_settled'])
        self.assertEqual(self.s.apply_incremental(**req)['execution_status'],'completed')
        self.assertTrue(self.works()[0]['receipt_settled'])
        self.assertIsNotNone(self.ledger()['sessions']['hermes/s']['processed_turns'][0]['eligible_cleanup_at'])

    def rewrite_receipt(self,result,mutate):
        data=self.ledger();wrapper=data[KEY][result['work_id']];work=json.loads(wrapper['payload']);mutate(work)
        wrapper['payload']=json.dumps(work);wrapper['checksum']=hashlib.sha256(wrapper['payload'].encode()).hexdigest()
        atomic_write_json(self.s.vault.processed_state_path,data)

    def test_invalid_receipt_field_types_not_reset(self):
        result=self.s.apply_incremental(**self.request([self.create(),self.no_memory()]));original=self.s.vault.processed_state_path.read_bytes()
        for field,value in [('index_status',[]),('turn_index',True),('receipt_settled',None),('source',{})]:
            with self.subTest(field=field):
                self.s.vault.processed_state_path.write_bytes(original);self.rewrite_receipt(result,lambda w:w.update({field:value}))
                before=self.s.vault.processed_state_path.read_bytes()
                with self.assertRaises(ValueError):self.s.resume_incremental(result['work_id'])
                self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())

    def test_action_container_fails_as_controlled_error(self):
        result=self.s.apply_incremental(**self.request([self.create(),self.no_memory()]))
        self.rewrite_receipt(result,lambda w:w['operations'][0].update(action=[]))
        with self.assertRaisesRegex(ValueError,'invalid_incremental_operation'):self.s.resume_incremental(result['work_id'])

    def test_recording_revoke_prevents_authorization(self):
        req=self.request([self.create(),self.no_memory()]);self.s.capture('hermes','s','t','user','private',record=False)
        with self.assertRaises(ValueError):self.s.apply_incremental(**req)
        self.assertFalse(self.ledger().get(KEY));self.assertEqual(self.s.vault.list_markdown('knowledge'),[])
