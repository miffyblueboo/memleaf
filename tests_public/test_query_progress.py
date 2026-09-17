from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch
from incremental_test_support import IncrementalFixture
from memleaf.query_progress import observe_progress, retention_inventory
from memleaf.process_common import _read_processed
from memleaf.locking import atomic_write_json
from memleaf.incremental_run_state import load_run, save_run, KEY
from memleaf.processing_route import health_view

class Backend:
    single_pass_safe=True
    def __init__(self, items):self.items=items;self.calls=0
    def complete(self,*args,**kwargs):self.calls+=1;return json.dumps({'items':self.items})

class QueryProgressTests(IncrementalFixture):
    def execute(self,items):
        backend=Backend(items)
        value=self.s.run_incremental(source='hermes',session_id='s',turn_id='t',backend=backend)
        self.assertEqual(backend.calls,1)
        return value

    def test_captured_but_unprocessed_is_pending_even_no_run(self):
        result=observe_progress(self.s.vault)
        self.assertEqual(result['status'],'pending');self.assertEqual(result['pending_turns'],1)

    def test_successful_no_memory_is_current(self):
        self.execute([self.no_memory('e1'),self.no_memory()])
        self.assertEqual(observe_progress(self.s.vault)['status'],'current')

    def test_successful_commit_is_current(self):
        self.execute([self.create(),self.no_memory()])
        self.assertEqual(self.s.list_todos()['pipeline_status']['status'],'current')

    def test_manual_apply_receipt_settles_without_run(self):
        self.s.apply_incremental(**self.request([self.create(),self.no_memory()]))
        self.assertEqual(observe_progress(self.s.vault)['status'],'current')

    def test_partial_is_not_current(self):
        self.execute([self.create(),{'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'Need detail'}])
        value=observe_progress(self.s.vault)
        self.assertEqual(value['status'],'pending');self.assertEqual(value['unresolved_runs'],1)

    def test_query_does_not_consume_partial_or_budget(self):
        self.execute([self.create(),{'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'Need detail'}])
        before=self.s.vault.processed_state_path.read_bytes()
        with patch.object(self.s,'process',side_effect=AssertionError('process')):
            for _ in range(3):self.s.list_todos();self.s.search_candidates('Atlas');observe_progress(self.s.vault)
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())

    def test_bad_control_state_is_unknown_but_valid_memory_readable(self):
        self.target();self.s.vault.processed_state_path.write_text('{broken')
        for result in (self.s.read_page('mem-old'),self.s.list_todos(),self.s.search_candidates('Atlas'),self.s.scope_catalog()):
            self.assertEqual(result['pipeline_status']['status'],'unknown')
            self.assertEqual(result['scan_status']['status'],'complete')
        self.assertEqual(self.s.vault.processed_state_path.read_text(),'{broken')

    def test_bad_queue_is_unknown_not_a_local_query_failure(self):
        self.target();(self.s.vault.state_path/'process_jobs.json').write_text('{bad')
        self.assertEqual(self.s.list_todos()['pipeline_status']['status'],'unknown')
        self.assertEqual(self.s.read('mem-old').title,'Atlas task')

    def test_single_incomplete_new_turn_is_reported(self):
        self.s.capture('hermes','s','next','user','new pending',message_id='next-u')
        self.assertEqual(observe_progress(self.s.vault)['incomplete_turns'],1)

    def test_source_revision_makes_status_pending(self):
        self.execute([self.create(),self.no_memory()]);self.revise()
        value=observe_progress(self.s.vault)
        self.assertNotEqual(value['status'],'current')

    def test_unknown_inbox_material_is_not_no_pending_proof(self):
        self.execute([self.no_memory('e1'),self.no_memory()])
        p=self.s.vault.root/'inbox/hermes/stray.md';p.write_text('Unknown raw source')
        self.assertEqual(observe_progress(self.s.vault)['status'],'unknown')

    def test_private_source_not_replayed_by_query(self):
        self.target();self.s.capture('hermes','s','private','user','Do not record this',record=False)
        before=self.s.vault.processed_state_path.read_bytes()
        self.s.list_todos()
        self.assertEqual(self.s.vault.processed_state_path.read_bytes(),before)

    def test_capacity_inventory_is_not_gc_permission(self):
        self.execute([self.no_memory('e1'),self.no_memory()])
        before=self.s.vault.processed_state_path.read_bytes()
        value=retention_inventory(self.s.vault)
        self.assertEqual(value['retained_runs'],1);self.assertEqual(value['remaining_run_slots'],127)
        self.assertFalse(value['collection_authorized']);self.assertTrue(value['read_only'])
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())

    def test_health_exposes_capacity_without_auto_cleanup(self):
        self.execute([self.no_memory('e1'),self.no_memory()])
        before=self.s.vault.processed_state_path.read_bytes()
        value=health_view(self.s.vault)
        self.assertIn('retention_inventory',value)
        self.assertEqual(before,self.s.vault.processed_state_path.read_bytes())

    def test_counts_metadata_does_not_expose_control_plaintext(self):
        self.execute([self.create(),{'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'secret detail'}])
        value=observe_progress(self.s.vault)
        text=json.dumps(value)
        for private in ('secret detail','Deliver the report','hermes','t-u',str(self.s.vault.root)):
            self.assertNotIn(private,text)

    def test_explicit_retention_does_not_consume_automatic_turn(self):
        preview=self.s.preview_incremental(source='hermes',session_id='s',turn_id='t')
        ref=preview['source_refs'][0]['source_ref']
        self.s.remember_incremental(source='hermes',session_id='s',turn_id='t',intent_id='selection',
            selected_source_refs=[ref],retention_request='Remember this',model=Backend([self.create()]))
        self.assertEqual(observe_progress(self.s.vault)['pending_turns'],1)

    def test_corrupt_source_block_with_valid_sibling_is_unknown(self):
        p=self.s.vault.root/'inbox/hermes/s.md'
        with p.open('a') as stream:stream.write('\n<!-- memleaf:event:v2 -->\n{bad}\n')
        self.assertEqual(observe_progress(self.s.vault)['status'],'unknown')

    def test_observation_during_control_change_is_unknown(self):
        from memleaf.query_progress import _control_stamp
        count=0
        def changing(path):
            nonlocal count
            count+=1
            return ('changed' if count>1 and path==self.s.vault.processed_state_path else _control_stamp(path))
        with patch('memleaf.query_progress._control_stamp',side_effect=changing):
            self.assertEqual(observe_progress(self.s.vault)['status'],'unknown')

    def test_empty_vault_observation_creates_no_control_files(self):
        from memleaf import Memleaf
        fresh=Memleaf.initialize(Path(self.temp.name)/'fresh')
        before={str(p.relative_to(fresh.vault.root)):p.read_bytes() for p in fresh.vault.root.rglob('*') if p.is_file()}
        observe_progress(fresh.vault);retention_inventory(fresh.vault)
        after={str(p.relative_to(fresh.vault.root)):p.read_bytes() for p in fresh.vault.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

    def test_invalid_source_identity_is_unknown_even_with_valid_sibling(self):
        self.execute([self.no_memory('e1'),self.no_memory()])
        p=self.s.vault.root/'inbox/hermes/s.md'
        with p.open('a') as stream:stream.write('\n<!-- memleaf:event:v2 -->\n{"event_key":"bad"}\n<!-- memleaf:content -->\nUnprocessed\n<!-- memleaf:event-end -->\n')
        self.assertEqual(observe_progress(self.s.vault)['status'],'unknown')
