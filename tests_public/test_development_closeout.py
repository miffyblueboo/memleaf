"""Finalization checks for shared write, deletion and retained recovery inputs.

All Vaults and edits are synthetic and local. No network/model/backend is used.
"""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.memory_retraction import RetractionManager
from memleaf.memory_writer import MemoryWriter
from memleaf.models import MemoryVersionError
from memleaf.query_progress import observe_progress
from memleaf.retention import RetentionManager, RetentionError
from memleaf.retrieval import RetrievalError
from memleaf.turn_plan import revision_digest


class CloseoutFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Memleaf.initialize(Path(self.tmp.name) / 'vault')

    def memory(self, **kwargs):
        values = dict(memory_id='mem-item', title='Atlas delivery', body='Deliver the specification.',
                      type='todo', status='active', scopes=['global'], due_date='2026-09-28',
                      assignee='owner', waiting_on='review', custom={'keep': True})
        values.update(kwargs)
        return self.s.create_memory(**values)

    def frozen(self, before=None):
        after = Memory.from_mapping(before.to_dict()) if before else Memory.new(
            memory_id='mem-item', title='Atlas delivery', body='Deliver the specification.')
        after.body = 'Current specification.'
        after.extra['incremental_operation_id'] = 'test-operation'
        return dict(memory_id=after.memory_id, operation_id='test-operation',
                    action='UPDATE' if before else 'CREATE', before=before.to_markdown() if before else None,
                    after=after.to_markdown(), expected_revision=revision_digest(before) if before else None,
                    replacement_revision=revision_digest(after), prepared_at='2026-09-18T12:00:00+00:00')

    def invalid_duplicate(self):
        raw = Memory.new(memory_id='MEM-ITEM', title='Duplicate', body='Duplicate').to_markdown()
        self.s.vault.knowledge_path.joinpath('bad-copy.md').write_text(raw.replace('hit_count: 0','hit_count: -1'),encoding='utf-8')

    def pause_retraction(self):
        self.memory()
        revision = self.s.memory_revision('mem-item')
        with patch.object(self.s, '_rebuild_index_unlocked', side_effect=OSError('index unavailable')):
            with self.assertRaises(OSError):
                self.s.retract_memory('mem-item',expected_revision=revision)
        return revision


class SharedWriterCloseoutTests(CloseoutFixture):
    def test_history_io_does_not_authorize_overwriting_new_edit(self):
        old = self.memory()
        operation = self.frozen(old)
        original = MemoryWriter._write_history
        def intervene(writer, *args, **kwargs):
            result = original(writer, *args, **kwargs)
            edited = Memory.from_mapping(old.to_dict()); edited.body = 'Newer external edit.'
            self.s.vault.memory_path(old.memory_id).write_text(edited.to_markdown(),encoding='utf-8')
            return result
        with patch.object(MemoryWriter, '_write_history', intervene), self.s.vault.lock():
            with self.assertRaises(RetrievalError):
                MemoryWriter(self.s).write_frozen_unlocked(operation)
        self.assertEqual(self.s.read(old.memory_id).body, 'Newer external edit.')

    def test_invalid_duplicate_is_not_proof_of_applied_frozen_state(self):
        operation = self.frozen()
        self.s.write_memory(Memory.from_markdown(operation['after']))
        self.invalid_duplicate()
        with self.s.vault.lock():
            self.assertFalse(MemoryWriter(self.s).frozen_state_applied_unlocked(operation))

    def test_invalid_duplicate_prevents_frozen_update(self):
        old = self.memory(); operation = self.frozen(old); self.invalid_duplicate()
        before = self.s.vault.memory_path('mem-item').read_bytes()
        with self.s.vault.lock(), self.assertRaises(RetrievalError):
            MemoryWriter(self.s).write_frozen_unlocked(operation)
        self.assertEqual(before, self.s.vault.memory_path('mem-item').read_bytes())

    def test_generated_create_cannot_reuse_history_identity(self):
        old = self.memory(area='history'); operation = self.frozen()
        with self.s.vault.lock(), self.assertRaises((MemoryVersionError, RetrievalError)):
            MemoryWriter(self.s).write_frozen_unlocked(operation)
        self.assertFalse(self.s.vault.memory_path(old.memory_id).exists())

    def test_same_frozen_update_reuses_history_and_preserves_counters(self):
        old=self.memory(); operation=self.frozen(old)
        with self.s.vault.lock():
            MemoryWriter(self.s).write_frozen_unlocked(operation)
        self.s.read('mem-item'); self.s.read('mem-item')
        count = Memory.from_markdown(self.s.vault.memory_path('mem-item').read_text()).hit_count
        with self.s.vault.lock():
            self.assertEqual(MemoryWriter(self.s).write_frozen_unlocked(operation), 'applied')
        self.assertEqual(len(self.s.vault.list_markdown('history')),1)
        self.assertEqual(Memory.from_markdown(self.s.vault.memory_path('mem-item').read_text()).hit_count,count)


class ForgetSelectionCloseoutTests(CloseoutFixture):
    def test_single_fuzzy_candidate_is_not_deletion_authority(self):
        old=self.memory(); result=self.s.forget_about('specification')
        self.assertEqual(result.status,'ambiguous')
        self.assertEqual([m.memory_id for m in result.candidates],[old.memory_id])
        self.assertTrue(self.s.vault.memory_path(old.memory_id).exists())

    def test_exact_case_alias_removes_current_and_linked_history(self):
        revision=self.pause_retraction()
        self.s.retract_memory('mem-item',expected_revision=revision)
        self.assertTrue(self.s.forget_memory('MEM-ITEM'))
        self.assertEqual(self.s.vault.list_markdown('knowledge'),[])
        self.assertEqual(self.s.vault.list_markdown('history'),[])

    def test_exact_title_remains_supported_without_second_confirmation(self):
        self.memory()
        self.assertEqual(self.s.forget_about('Atlas delivery').status,'deleted')

    def test_invalid_duplicate_is_not_deleted_as_a_single_target(self):
        self.memory(); self.invalid_duplicate()
        before=self.s.vault.memory_path('mem-item').read_bytes()
        with self.assertRaises(RetrievalError):
            self.s.forget_memory('mem-item')
        self.assertEqual(before,self.s.vault.memory_path('mem-item').read_bytes())

    def test_unreadable_history_does_not_report_complete_forget(self):
        self.memory(); self.s.vault.history_path.joinpath('bad.md').write_bytes(b'\xff')
        with self.assertRaises(RetrievalError):
            self.s.forget_about('mem-item')
        self.assertTrue(self.s.vault.memory_path('mem-item').exists())

    def test_exact_history_address_does_not_delete_current(self):
        rev=self.pause_retraction(); self.s.retract_memory('mem-item',expected_revision=rev)
        history=Memory.from_markdown(self.s.vault.list_markdown('history')[0].read_text())
        self.assertTrue(self.s.forget_memory(history.memory_id))
        self.assertTrue(self.s.vault.memory_path('mem-item').exists())


class PendingMutationCloseoutTests(CloseoutFixture):
    def test_history_retention_waits_for_retraction_recovery(self):
        revision=self.pause_retraction()
        before={p.name:p.read_bytes() for p in self.s.vault.list_markdown('history')}
        result=RetentionManager(self.s).maintain('2099-01-01T00:00:00Z')
        self.assertEqual(result['history_pruned'],0)
        self.assertEqual(result.get('code'),'pending_mutation_dependencies')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.s.vault.list_markdown('history')})
        self.s.retract_memory('mem-item',expected_revision=revision)
        self.assertEqual(RetentionManager(self.s).maintain('2099-01-01T00:00:00Z')['history_pruned'],1)

    def test_pending_explicit_retraction_is_visible_without_replay(self):
        self.pause_retraction()
        path=RetractionManager(self.s)._path('mem-item'); before=path.read_bytes()
        progress=observe_progress(self.s.vault)
        self.assertEqual(progress['status'],'pending')
        self.assertEqual(progress['pending_commits'],1)
        self.assertEqual(path.read_bytes(),before)

    def test_migration_preflight_reports_retraction_journal(self):
        self.pause_retraction(); result=self.s.migration_preflight()
        self.assertIn('pending_retractions',result['blockers'])

    def test_corrupt_explicit_journal_defers_cleanup_and_is_not_reset(self):
        self.pause_retraction(); path=RetractionManager(self.s)._path('mem-item');path.write_text('{}')
        before=path.read_bytes()
        with self.assertRaises(RetentionError):
            RetentionManager(self.s).maintain('2099-01-01T00:00:00Z')
        self.assertEqual(observe_progress(self.s.vault)['status'],'unknown')
        self.assertEqual(before,path.read_bytes())

    def test_valid_duplicate_copies_are_all_removed_by_exact_id(self):
        self.memory()
        first=self.s.vault.memory_path('mem-item')
        self.s.vault.knowledge_path.joinpath('second.md').write_bytes(first.read_bytes())
        self.assertTrue(self.s.forget_memory('mem-item'))
        self.assertEqual(self.s.vault.list_markdown('knowledge'),[])

    def test_ambiguous_identity_is_not_selected_by_title(self):
        self.memory()
        self.s.vault.knowledge_path.joinpath('copy.md').write_bytes(self.s.vault.memory_path('mem-item').read_bytes())
        with self.assertRaises(RetrievalError):self.s.forget_about('Atlas delivery')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),2)

    def test_broken_scan_defers_optional_cleanup_without_touching_valid_history(self):
        revision=self.pause_retraction();self.s.retract_memory('mem-item',expected_revision=revision)
        self.s.vault.knowledge_path.joinpath('unknown.md').write_bytes(b'\xff')
        before={p.name:p.read_bytes() for p in self.s.vault.list_markdown('history')}
        result=RetentionManager(self.s).maintain('2099-01-01T00:00:00Z')
        self.assertEqual(result['code'],'memory_scan_incomplete')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.s.vault.list_markdown('history')})
