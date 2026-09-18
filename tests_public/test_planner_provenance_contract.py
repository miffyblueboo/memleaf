"""A copied old field basis does not certify a manually changed current record."""
from __future__ import annotations
import json
from unittest.mock import patch

from memleaf import Memory
from memleaf.incremental_preview import prepare_incremental
from memleaf.incremental_protocol import compile_incremental, basis_status, applied_revision_index
from memleaf.incremental_commit import IncrementalCommitError
from memleaf.memory_writer import MemoryWriter
from memleaf.memory_retraction import RetractionManager
from memleaf.turn_plan import cancel_frozen_targets, FrozenTurn
from incremental_test_support import IncrementalFixture


class PlannerProvenanceContractTests(IncrementalFixture):
    def snapshot(self):
        return prepare_incremental(self.s,source='hermes',session_id='s',turn_id='t',priority_memory_ids=['mem-old'])

    def controlled(self):
        self.target();self.s.apply_incremental(**self.request([self.update(body='Confirmed current report.'),self.no_memory()],priority_memory_ids=['mem-old']))

    def head(self):
        return Memory.from_markdown(self.s.vault.memory_path('mem-old').read_text())

    def manual(self,body='Manual edit with unknown business time.',identity=None):
        head=self.head();head.body=body
        if identity is not None:head.memory_id=identity
        self.s.vault.memory_path('mem-old').write_text(head.to_markdown(),encoding='utf-8')
        return head

    def test_unchanged_authored_state_has_verified_observation_without_heat_conflicts(self):
        self.controlled();one=self.snapshot();self.s.read('mem-old');two=self.snapshot()
        self.assertEqual(one.snapshot_id,two.snapshot_id)
        self.assertEqual(two.model_input()['memories'][0]['provenance_status'],'verified_revision')
        self.assertTrue(two.model_input()['memories'][0]['observed'])

    def test_external_edit_is_detected_without_rewriting_or_claiming_old_time(self):
        self.controlled();self.manual();before=self.s.vault.memory_path('mem-old').read_bytes()
        view=self.snapshot().model_input()['memories'][0]
        self.assertEqual(view['provenance_status'],'external_change_detected')
        self.assertEqual(view['observed'],{})
        self.assertEqual(view['body'],'Manual edit with unknown business time.')
        self.assertEqual(before,self.s.vault.memory_path('mem-old').read_bytes())

    def test_external_change_keeps_revision_but_compiles_only_new_field_basis(self):
        self.controlled();self.manual();snap=self.snapshot()
        outcome=compile_incremental(json.dumps({'items':[self.update(status='completed'),self.no_memory()]}),snap)
        op=outcome['operations'][0]
        self.assertEqual(op['expected_revision'],self.s.memory_revision('mem-old'))
        self.assertEqual(op['memory']['body'],'Manual edit with unknown business time.')
        self.assertEqual(set(op['memory']['field_basis']),{'status'})

    def test_old_unprovable_metadata_is_explicitly_unknown_in_model_view(self):
        self.target(field_basis={'content':{'source_time':'2026-01-01T00:00:00Z'}})
        view=self.snapshot().model_input()['memories'][0]
        self.assertEqual(view['provenance_status'],'legacy_unverified')
        self.assertEqual(view['observed'],{})

    def test_explicit_receipt_also_detects_external_content_change(self):
        self.target();self.s.update_memory('mem-old',expected_revision=self.s.memory_revision('mem-old'),
             patch={'body':'Explicit current text.'},authorized_scopes=['global'])
        self.assertEqual(basis_status(self.head()),'verified_revision')
        self.manual();self.assertEqual(basis_status(self.head()),'external_change_detected')
        self.s.update_memory('mem-old',expected_revision=self.s.memory_revision('mem-old'),
             patch={'assignee':'reviewer'},authorized_scopes=['global'])
        now=self.head()
        self.assertEqual(set(now.extra['field_basis']),{'responsibility'})
        self.assertIn('external_edit_observed_at',now.extra)
        self.assertEqual(now.body,'Manual edit with unknown business time.')

    def test_new_retraction_records_observation_not_fabricated_completion(self):
        self.target();self.s.retract_memory('mem-old',expected_revision=self.s.memory_revision('mem-old'))
        head=self.head();basis=head.extra['field_basis']['validity']
        self.assertEqual(basis['source'],'explicit_retraction')
        self.assertIn('source_time',basis);self.assertIsNone(head.completed_at)

    def test_bounded_scan_rejects_oversized_catalog_without_partial_target_write(self):
        self.target();before=self.s.vault.memory_path('mem-old').read_bytes()
        with patch('memleaf.query_scan.MAX_FILE_BYTES',1):
            with self.assertRaisesRegex(ValueError,'incomplete_library'):self.snapshot()
        self.assertEqual(before,self.s.vault.memory_path('mem-old').read_bytes())

    def test_case_changed_head_forget_cancels_pending_incremental_plaintext(self):
        self.target();req=self.request([self.update(body='Secret pending correction.'),self.no_memory()],priority_memory_ids=['mem-old'])
        with patch.object(MemoryWriter,'write_frozen_unlocked',side_effect=OSError('pending')):
            with self.assertRaises(IncrementalCommitError):self.s.apply_incremental(**req)
        work=self.works()[0];self.manual(identity='MEM-OLD')
        self.assertTrue(self.s.forget_memory('mem-old'))
        self.assertNotIn('Secret pending correction.',self.s.vault.processed_state_path.read_text())
        self.s.resume_incremental(work['work_id'])
        self.assertEqual(self.s.vault.list_markdown('knowledge'),[])

    def test_case_changed_head_forget_cancels_retained_retraction_file(self):
        self.target();rev=self.s.memory_revision('mem-old')
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index')):
            with self.assertRaises(OSError):self.s.retract_memory('mem-old',expected_revision=rev)
        head=self.head();head.memory_id='MEM-OLD'
        self.s.vault.memory_path('mem-old').write_text(head.to_markdown())
        journal=RetractionManager(self.s)._path('mem-old');self.assertTrue(journal.exists())
        self.s.forget_memory('MEM-OLD');self.assertFalse(journal.exists())

    def test_frozen_legacy_cancellation_uses_the_same_logical_id(self):
        import hashlib
        value={'requests':[{'candidate_id':'c1','memory_id':'mem-old','summary':{'update_memory_id':'mem-old'}}]}
        text=json.dumps(value)
        stored=FrozenTurn(text,hashlib.sha256(text.encode()).hexdigest()).to_dict()
        result,removed=cancel_frozen_targets(stored,{'MEM-OLD'})
        self.assertEqual(removed,{'c1'});self.assertEqual(json.loads(result['payload'])['requests'],[])

    def test_legacy_snapshot_digest_does_not_depend_on_new_provenance_projection(self):
        from memleaf.incremental_recovery import restore_snapshot
        self.target();value=self.snapshot().state();value.pop('vault_binding',None)
        for target in value['targets'].values():target.pop('basis_status',None)
        old=restore_snapshot(value)
        current=dict(value);current['targets']={key:{**item,'basis_status':'legacy_unverified'} for key,item in value['targets'].items()}
        self.assertEqual(old.snapshot_id,restore_snapshot(current).snapshot_id)
