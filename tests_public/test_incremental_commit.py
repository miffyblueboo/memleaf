from __future__ import annotations
import json
from unittest.mock import patch
from memleaf import Memory
from memleaf.incremental_commit import IncrementalCommitError
from memleaf.incremental_journal import KEY
from memleaf.index import turn_key
from memleaf.memory_writer import MemoryWriter
from memleaf.process_journal import ProcessJournal
from incremental_test_support import IncrementalFixture


class IncrementalCommitTests(IncrementalFixture):
    def test_create_is_durable_and_repeated_intent_is_idempotent(self):
        req=self.request([self.create(),self.no_memory()]);result=self.s.apply_incremental(**req)
        self.assertEqual(result,self.s.apply_incremental(**req))
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)
        self.assertEqual(result['counts']['committed'],1);self.assertEqual(result['model_calls'],0)

    def test_update_keeps_identity_and_all_unmodified_fields(self):
        old=self.target();result=self.s.apply_incremental(**self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old']))
        current=self.s.read('mem-old')
        self.assertEqual(result['operations'][0]['memory_id'],old.memory_id)
        self.assertEqual(current.status,'completed');self.assertIsNone(current.completed_at)
        self.assertEqual(current.body,old.body);self.assertEqual(current.due_date,old.due_date)
        self.assertEqual(current.created,old.created);self.assertEqual(current.extra['custom'],{'keep':True})
        self.assertEqual(current.extra['assignee'],'user');self.assertEqual(len(self.hist()),1)

    def test_no_change_does_not_advance_business_or_provenance(self):
        self.target();path=self.s.vault.memory_path('mem-old');before=path.read_bytes()
        result=self.s.apply_incremental(**self.request([{'action':'NO_CHANGE','target':'m1','evidence':['e1']},self.no_memory()],priority_memory_ids=['mem-old']))
        self.assertEqual(result['counts']['no_change'],1);self.assertEqual(path.read_bytes(),before);self.assertEqual(self.hist(),[])

    def test_noop_update_becomes_no_change(self):
        self.target();before=self.s.vault.memory_path('mem-old').read_bytes()
        result=self.s.apply_incremental(**self.request([self.update(status='active'),self.no_memory()],priority_memory_ids=['mem-old']))
        self.assertEqual(result['operations'][0]['action'],'NO_CHANGE')
        self.assertEqual(before,self.s.vault.memory_path('mem-old').read_bytes())

    def test_no_memory_is_recorded_and_not_reextracted(self):
        result=self.s.apply_incremental(**self.request([self.no_memory('e1'),self.no_memory()]))
        self.assertEqual(result['counts']['no_memory'],2)
        snaps,_=ProcessJournal(self.s)._snapshot(source='hermes',session_id='s',now='2026-09-17T12:00:00Z',cleanup_hours=24)
        self.assertEqual(snaps,[]);self.assertEqual(self.s.vault.list_markdown('knowledge'),[])

    def test_partial_commits_valid_siblings_and_discards(self):
        self.target();items=[self.update(),self.no_memory(),{'action':'UPDATE','target':'m99','evidence':['e1'],'patch':{'status':'active'}}]
        req=self.request(items,priority_memory_ids=['mem-old']);result=self.s.apply_incremental(**req)
        self.assertEqual(result['coverage_status'],'partial');self.assertEqual(result['counts']['committed'],1)
        self.assertEqual(result['counts']['no_memory'],1);self.assertEqual(result,self.s.resume_incremental(result['work_id']))
        self.assertEqual(result,self.s.apply_incremental(**req));self.assertEqual(len(self.hist()),1)

    def test_invalid_same_target_row_blocks_whole_group(self):
        self.target('mem-a');self.target('mem-b')
        items=[self.update(),{'action':'UPDATE','target':'m1','evidence':['e1'],'patch':{'unknown':1}},
               {'action':'UPDATE','target':'m2','evidence':['e1'],'patch':{'status':'completed'}},self.no_memory()]
        result=self.s.apply_incremental(**self.request(items,priority_memory_ids=['mem-a','mem-b']))
        self.assertEqual(self.s.read('mem-a').status,'active');self.assertEqual(self.s.read('mem-b').status,'completed')
        self.assertEqual(result['counts']['committed'],1)

    def test_redundant_no_change_and_update_is_single_group(self):
        self.target();items=[self.update(),{'action':'NO_CHANGE','target':'m1','evidence':['e2']}]
        result=self.s.apply_incremental(**self.request(items,priority_memory_ids=['mem-old']))
        self.assertEqual(len(result['operations']),1);self.assertEqual(result['coverage_status'],'complete')
        self.assertEqual(set(result['operations'][0]['evidence']),{'e1','e2'})

    def test_stale_preview_before_authorization_has_no_receipt(self):
        self.target();req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old'])
        self.target(body='New obligation')
        with self.assertRaisesRegex(ValueError,'stale_planning_snapshot'): self.s.apply_incremental(**req)
        self.assertFalse(self.ledger().get(KEY));self.assertEqual(self.s.read('mem-old').body,'New obligation')

    def test_authorization_cannot_change_response(self):
        req=self.request([self.create(),self.no_memory()]);self.s.apply_incremental(**req)
        req['response']=json.dumps({'items':[self.no_memory('e1'),self.no_memory()]})
        with self.assertRaisesRegex(ValueError,'intent_payload_conflict'):self.s.apply_incremental(**req)

    def test_authorization_cannot_expand_scope(self):
        req=self.request([self.create(),self.no_memory()],scope='global');self.s.apply_incremental(**req);req['scope']=None
        with self.assertRaisesRegex(ValueError,'intent_payload_conflict'):self.s.apply_incremental(**req)

    def test_new_authorization_still_deduplicates_exact_state(self):
        self.s.apply_incremental(**self.request([self.create(),self.no_memory()]))
        req=self.request([self.create(),self.no_memory()]);req['intent_id']='new-authorized-intent'
        result=self.s.apply_incremental(**req)
        self.assertEqual(len(self.works()),2);self.assertEqual(result['counts']['no_change'],1)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),1)

    def test_all_decisions_frozen_before_first_write(self):
        self.target();req=self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old'])
        original=MemoryWriter.write_frozen_unlocked
        def check(writer,op):
            work=self.works()[0];self.assertEqual(work['operations'][0]['operation_id'],op['operation_id'])
            self.assertEqual(work['operations'][1]['action'],'NO_MEMORY');return original(writer,op)
        with patch.object(MemoryWriter,'write_frozen_unlocked',new=check):self.s.apply_incremental(**req)

    def test_terminal_receipts_omit_frozen_plaintext(self):
        self.target();self.s.apply_incremental(**self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old']))
        for op in self.works()[0]['operations']:
            self.assertNotIn('before',op);self.assertNotIn('after',op)
        self.assertNotIn('Deliver the report',self.s.vault.processed_state_path.read_text())

    def test_retraction_and_new_confirmation_keep_id(self):
        self.target(type='fact',status=None,due_date=None,assignee=None,waiting_on=None)
        self.s.apply_incremental(**self.request([self.update(validity='retracted'),self.no_memory()],priority_memory_ids=['mem-old']))
        self.assertIsNone(self.s.read('mem-old'));self.assertEqual(self.s.read('mem-old',include_history=True).validity,'retracted')
        self.capture('restore',content='This is confirmed again',seq=3)
        req=self.request([self.update(validity='valid',body='Confirmed current assertion'),self.no_memory()],turn_id='restore',priority_memory_ids=['mem-old']);req['intent_id']='restore'
        result=self.s.apply_incremental(**req)
        self.assertEqual(result['counts']['committed'],1);self.assertEqual(self.s.read('mem-old').body,'Confirmed current assertion')
        self.assertEqual(len(self.hist()),2)

    def test_scope_violation_does_not_drop_unrelated_discard(self):
        self.target(scopes=['project:Atlas'])
        result=self.s.apply_incremental(**self.request([self.update(),self.no_memory()],scope='project:Beacon',priority_memory_ids=['mem-old']))
        self.assertEqual(result['coverage_status'],'partial');self.assertEqual(result['counts']['no_memory'],1)
        self.assertEqual(self.s.read('mem-old').status,'active')

    def test_malformed_json_does_not_claim_source(self):
        req=self.request([self.create(),self.no_memory()]);req['response']='{"items":'
        with self.assertRaises(ValueError):self.s.apply_incremental(**req)
        self.assertFalse(self.ledger().get(KEY))

    def test_unknown_work_does_not_create_work(self):
        with self.assertRaisesRegex(ValueError,'work_not_found'):self.s.resume_incremental('inc-'+'0'*64)
        self.assertFalse(self.ledger().get(KEY))

    def test_terminal_replay_after_source_cleanup(self):
        req=self.request([self.create(),self.no_memory()]);result=self.s.apply_incremental(**req)
        self.s.vault.session_path('hermes','s').unlink()
        self.assertEqual(self.s.apply_incremental(**req),result)
        self.assertEqual(self.s.resume_incremental(result['work_id']),result)

    def test_legacy_does_not_replan_partial_staged_source(self):
        items=[self.no_memory('e1'),{'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'more evidence'}]
        self.s.apply_incremental(**self.request(items))
        snaps,_=ProcessJournal(self.s)._snapshot(source='hermes',session_id='s',now='2026-09-17T13:00:00Z',cleanup_hours=24,scope='global')
        self.assertEqual(snaps,[])

    def test_pending_staged_source_does_not_hide_independent_turn(self):
        req=self.request([self.create(),self.no_memory()])
        with patch.object(MemoryWriter,'write_frozen_unlocked',side_effect=OSError('before write')):
            with self.assertRaises(IncrementalCommitError):self.s.apply_incremental(**req)
        self.capture('next',seq=3)
        snaps,_=ProcessJournal(self.s)._snapshot(source='hermes',session_id='s',now='2026-09-17T13:00:00Z',cleanup_hours=24)
        self.assertEqual([x.turn.turn_key for x in snaps],[turn_key('next')])

    def test_source_revision_pins_previous_target(self):
        self.s.apply_incremental(**self.request([self.create(title='Original unique topic'),self.no_memory()]))
        self.revise('That was only an example, not a task.')
        view=json.loads(self.s.preview_incremental(source='hermes',session_id='s',turn_id='t')['request']['user'])
        self.assertTrue(any(m['title']=='Original unique topic' for m in view['memories']))

    def test_rename_preserves_target_path(self):
        self.target();old=self.s.vault.memory_path('mem-old');new=old.parent/'renamed.md';old.rename(new)
        self.s.apply_incremental(**self.request([self.update(),self.no_memory()],priority_memory_ids=['mem-old']))
        self.assertTrue(new.is_file());self.assertFalse(old.exists());self.assertEqual(Memory.from_markdown(new.read_text()).status,'completed')

    def test_explicit_clears_are_not_omission(self):
        self.revise('Remove the deadline and responsibility');self.target()
        result=self.s.apply_incremental(**self.request([self.update(deadline={'ref':'e1','clear':True,'text':'Remove the deadline'},assignee=None,waiting_on=None),self.no_memory()],priority_memory_ids=['mem-old']))
        self.assertEqual(result['counts']['committed'],1);current=self.s.read('mem-old')
        self.assertIsNone(current.due_date);self.assertIsNone(current.extra['assignee']);self.assertEqual(current.extra['due_status'],'cleared')

    def test_empty_items_not_no_memory(self):
        result=self.s.apply_incremental(**self.request([]))
        self.assertEqual(result['coverage_status'],'partial');self.assertEqual(result['counts']['no_memory'],0)
        self.assertGreater(result['counts']['issues'],0)

    def test_json_whitespace_does_not_create_intent_conflict(self):
        req=self.request([self.create(),self.no_memory()]);result=self.s.apply_incremental(**req)
        req['response']=json.dumps(json.loads(req['response']),indent=2)
        self.assertEqual(self.s.apply_incremental(**req),result)

    def test_new_scope_transaction_not_silently_enabled(self):
        req=self.request([self.create(),self.no_memory()]);req['allow_new_scopes']=True
        with self.assertRaisesRegex(ValueError,'stale_planning_snapshot'):self.s.apply_incremental(**req)
        self.assertFalse(self.ledger().get(KEY))

    def test_unparsed_deadline_is_visible_to_mcp(self):
        from memleaf.mcp_server import _read_page_result
        self.revise('Deliver before the next review meeting.')
        result=self.s.apply_incremental(**self.request([self.create(deadline={'ref':'e1','text':'before the next review meeting'}),self.no_memory()]))
        page=self.s.read_page(result['operations'][0]['memory_id'])
        self.assertEqual(page['due_text'],'before the next review meeting');self.assertIsNone(page['due_date'])
        self.assertEqual(_read_page_result(page)['due_status'],page['due_status'])

    def test_cleanup_uses_existing_config(self):
        from datetime import datetime,timedelta
        from memleaf.config import save_config
        config=self.s.vault.config();config['process']['inbox_cleanup_hours']=7;save_config(self.s.vault.config_path,config)
        self.s.apply_incremental(**self.request([self.create(),self.no_memory()]))
        e=self.ledger()['sessions']['hermes/s']['processed_turns'][0]
        self.assertEqual(datetime.fromisoformat(e['eligible_cleanup_at'].replace('Z','+00:00'))-datetime.fromisoformat(e['processed_at'].replace('Z','+00:00')),timedelta(hours=7))
