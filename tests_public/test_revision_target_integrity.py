"""Exact current-target resolution and bounded retraction recovery; no models."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.memory_retraction import RetractionCommitError, RetractionManager
from memleaf.memory_writer import MemoryWriter
from memleaf.models import MemoryVersionError
from memleaf.retrieval import RetrievalError
from memleaf.turn_plan import revision_digest
import memleaf.memory_retraction as retraction_module
import memleaf.service as service_module


class RevisionTargetIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Memleaf.initialize(Path(self.tmp.name) / 'vault')

    def create(self, identity='mem-item', **fields):
        return self.s.create_memory(memory_id=identity, title='Task', body='Original assertion', **fields)

    def snapshot(self):
        roots = (self.s.vault.knowledge_path, self.s.vault.history_path,
                 self.s.vault.state_path / 'retractions')
        return {str(p.relative_to(self.s.vault.root)): p.read_bytes()
                for root in roots for p in root.rglob('*') if p.is_file()}

    def journal(self, identity='mem-item'):
        return RetractionManager(self.s)._path(identity)

    def pause(self, point='history'):
        self.create()
        expected = self.s.memory_revision('mem-item')
        owner, name = ((MemoryWriter, '_write_history') if point == 'history'
                       else (self.s, '_rebuild_index_unlocked'))
        with patch.object(owner, name, side_effect=OSError('injected interruption')):
            with self.assertRaises(OSError):
                self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertTrue(self.journal().is_file())
        return expected

    def invalid_file(self, identity, name='invalid.md'):
        value = Memory.new(memory_id=identity, title='Invalid', body='Invalid assertion').to_markdown()
        path = self.s.vault.knowledge_path / name
        path.write_text(value.replace('hit_count: 0', 'hit_count: -1'), encoding='utf-8')
        return path

    def test_revision_rejects_case_variant_duplicate(self):
        self.create()
        duplicate = Memory.new(memory_id='MEM-ITEM', title='Other claim', body='Other assertion')
        self.s.vault.knowledge_path.joinpath('duplicate.md').write_text(duplicate.to_markdown(), encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(RetrievalError):
            self.s.memory_revision('mem-item')
        self.assertEqual(before, self.snapshot())

    def test_retraction_rejects_case_variant_duplicate_without_writing(self):
        self.create()
        expected = self.s.memory_revision('mem-item')
        duplicate = Memory.new(memory_id='MEM-ITEM', title='Other claim', body='Other assertion')
        self.s.vault.knowledge_path.joinpath('duplicate.md').write_text(duplicate.to_markdown(), encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(RetrievalError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(before, self.snapshot())

    def test_revision_unreadable_target_is_not_missing(self):
        self.invalid_file('mem-item')
        with self.assertRaises(RetrievalError):
            self.s.memory_revision('mem-item')

    def test_malformed_duplicate_does_not_grant_write_authority(self):
        self.create()
        expected = self.s.memory_revision('mem-item')
        self.invalid_file('MEM-ITEM')
        before = self.snapshot()
        with self.assertRaises(RetrievalError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(before, self.snapshot())

    def test_unreadable_target_keeps_pending_journal(self):
        expected = self.pause('index')
        path = self.s.vault.memory_path('mem-item')
        path.write_text(path.read_text(encoding='utf-8').replace('hit_count: 0', 'hit_count: -1'), encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(RetrievalError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(before, self.snapshot())

    def test_unknown_identity_keeps_pending_journal(self):
        expected = self.pause()
        self.s.vault.memory_path('mem-item').write_bytes(b'\xff unreadable')
        before = self.snapshot()
        with self.assertRaises(RetrievalError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(before, self.snapshot())

    def test_incomplete_scan_does_not_issue_revision_or_retract(self):
        self.create()
        expected = self.s.memory_revision('mem-item')
        before = self.snapshot()
        with patch('memleaf.query_scan.MAX_FILES', 0):
            with self.assertRaises(RetrievalError):
                self.s.memory_revision('mem-item')
            with self.assertRaises(RetrievalError):
                self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(before, self.snapshot())

    def test_unrelated_known_bad_identity_does_not_block_valid_target(self):
        self.create()
        self.invalid_file('mem-other')
        expected = self.s.memory_revision('mem-item')
        self.assertEqual(self.s.retract_memory('mem-item', expected_revision=expected).validity, 'retracted')

    def test_unknown_file_blocks_mutation_but_ordinary_read_stays_available(self):
        self.create()
        expected = self.s.memory_revision('mem-item')
        self.s.vault.knowledge_path.joinpath('unknown.md').write_bytes(b'\xff unknown identity')
        self.assertIsNotNone(self.s.read('mem-item'))
        before = self.snapshot()
        with self.assertRaises(RetrievalError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(before, self.snapshot())

    def test_case_alias_and_nested_rename_use_canonical_journal_identity(self):
        self.create('mem-Mixed')
        path = self.s.vault.memory_path('mem-Mixed')
        renamed = path.parent / 'nested' / 'renamed.md'
        renamed.parent.mkdir()
        path.rename(renamed)
        expected = self.s.memory_revision('MEM-MIXED')
        self.assertIsInstance(expected, str)
        with patch.object(self.s, '_rebuild_index_unlocked', side_effect=OSError('index')):
            with self.assertRaises(RetractionCommitError):
                self.s.retract_memory('MEM-MIXED', expected_revision=expected)
        self.assertTrue(self.journal('mem-Mixed').exists())
        self.assertEqual(len(list((self.s.vault.state_path / 'retractions').glob('*.json'))), 1)
        restarted = Memleaf(self.s.vault.root)
        result = restarted.retract_memory('mem-mixed', expected_revision=expected)
        self.assertEqual(result.memory_id, 'mem-Mixed')
        self.assertEqual(result.validity, 'retracted')
        self.assertTrue(renamed.exists())
        self.assertFalse(path.exists())
        self.assertFalse(self.journal('mem-Mixed').exists())

    def test_history_only_identity_is_not_a_current_target(self):
        self.s.create_memory(memory_id='hist-only', title='History', body='Old', area='history')
        before = self.snapshot()
        self.assertIsNone(self.s.memory_revision('hist-only'))
        with self.assertRaises(ValueError):
            self.s.retract_memory('hist-only', expected_revision='stale')
        self.assertEqual(before, self.snapshot())

    def test_missing_target_with_valid_journal_cancels_without_recreation(self):
        expected = self.pause()
        self.s.vault.memory_path('mem-item').unlink()
        with self.assertRaises(ValueError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertFalse(self.journal().exists())
        self.assertEqual(self.s.vault.list_markdown('knowledge'), [])
        self.assertEqual(self.s.vault.list_markdown('history'), [])

    def test_missing_target_does_not_silently_delete_corrupt_journal(self):
        expected = self.pause()
        self.s.vault.memory_path('mem-item').unlink()
        self.journal().write_text('{broken', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(before, self.snapshot())

    def test_revision_read_rechecks_scan_before_returning(self):
        self.create()
        original = service_module.scan_memories
        def changed(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            path = self.s.vault.memory_path('mem-item')
            value = Memory.from_markdown(path.read_text(encoding='utf-8'), path).to_dict()
            value['body'] = 'Edited during revision read'
            path.write_text(Memory.from_mapping(value).to_markdown(), encoding='utf-8')
            return snapshot
        with patch.object(service_module, 'scan_memories', side_effect=changed):
            with self.assertRaises(RetrievalError):
                self.s.memory_revision('mem-item')

    def test_edit_after_prepare_is_not_written_over_or_archived_as_current(self):
        self.create()
        expected = self.s.memory_revision('mem-item')
        original = retraction_module.atomic_write_json
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            path = self.s.vault.memory_path('mem-item')
            value = Memory.from_markdown(path.read_text(encoding='utf-8'), path).to_dict()
            value['body'] = 'Later edit after prepare'
            path.write_text(Memory.from_mapping(value).to_markdown(), encoding='utf-8')
            return result
        with patch.object(retraction_module, 'atomic_write_json', side_effect=changed):
            with self.assertRaises(RetrievalError):
                self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(self.s.read('mem-item').body, 'Later edit after prepare')
        self.assertEqual(self.s.vault.list_markdown('history'), [])
        self.assertTrue(self.journal().exists())

    def test_edit_after_history_is_not_overwritten_and_retry_is_stale(self):
        self.create()
        expected = self.s.memory_revision('mem-item')
        original = MemoryWriter._write_history
        def changed(writer, *args, **kwargs):
            result = original(writer, *args, **kwargs)
            path = self.s.vault.memory_path('mem-item')
            value = Memory.from_markdown(path.read_text(encoding='utf-8'), path).to_dict()
            value['body'] = 'Later edit after history'
            path.write_text(Memory.from_mapping(value).to_markdown(), encoding='utf-8')
            return result
        with patch.object(MemoryWriter, '_write_history', new=changed):
            with self.assertRaises(RetrievalError):
                self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(self.s.read('mem-item').body, 'Later edit after history')
        self.assertEqual(len(self.s.vault.list_markdown('history')), 1)
        self.assertTrue(self.journal().exists())
        with self.assertRaises(MemoryVersionError):
            self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertFalse(self.journal().exists())
        self.assertEqual(self.s.read('mem-item').body, 'Later edit after history')

    def test_retry_preserves_reads_since_prepare(self):
        expected = self.pause()
        self.s.read_page('mem-item')
        current = self.s.read('mem-item')
        self.assertEqual(current.hit_count, 1)
        self.assertEqual(revision_digest(current), expected)
        after = Memleaf(self.s.vault.root).retract_memory('mem-item', expected_revision=expected)
        self.assertEqual(after.hit_count, current.hit_count)
        self.assertEqual(after.last_hit_at, current.last_hit_at)
        self.assertEqual(self.s.read('mem-item', include_history=True).hit_count, current.hit_count)
        self.assertEqual(len(self.s.vault.list_markdown('history')), 1)

    def test_edit_after_head_is_reported_uncertain_not_applied_to_edited_state(self):
        self.create()
        expected = self.s.memory_revision('mem-item')
        original = retraction_module.atomic_write_text
        def changed(path, content):
            original(path, content)
            value = Memory.from_markdown(path.read_text(encoding='utf-8'), path).to_dict()
            value['title'] = 'A later authored title'
            path.write_text(Memory.from_mapping(value).to_markdown(), encoding='utf-8')
            raise OSError('failure after head changed again')
        with patch.object(retraction_module, 'atomic_write_text', side_effect=changed):
            with self.assertRaises(RetractionCommitError) as caught:
                self.s.retract_memory('mem-item', expected_revision=expected)
        self.assertIsNone(caught.exception.applied)
        self.assertTrue(self.journal().exists())


if __name__ == '__main__':
    unittest.main()
