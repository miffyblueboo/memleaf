"""Create-only identity checks; raw writes remain explicitly trusted operations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.retrieval import RetrievalError
import memleaf.service as service_module


class CreateMemoryContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = Memleaf.initialize(Path(self.temporary.name) / 'vault')

    def create(self, identity='mem-item', **values):
        return self.service.create_memory(memory_id=identity,
            title=values.pop('title', 'Task'), body=values.pop('body', 'Original'), **values)

    def snapshot(self):
        return {str(p.relative_to(self.service.vault.root)): p.read_bytes()
            for area in ('knowledge', 'history', 'index')
            for p in (self.service.vault.root / area).rglob('*') if p.is_file()}

    def test_new_memory_preserves_all_structured_and_custom_fields(self):
        memory = self.create(type='todo', status='active', assignee='owner',
            due_date='2026-10-01', custom={'nested': ['value']})
        self.assertEqual(self.service.read('mem-item').to_dict(), memory.to_dict())
        self.assertEqual(self.service.vault.list_markdown('history'), [])

    def test_existing_identity_never_overwrites_or_makes_history(self):
        self.create()
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.create(body='New replacement')
        self.assertEqual(self.snapshot(), before)

    def test_identical_create_is_a_conflict_not_an_update_or_retry_receipt(self):
        self.create()
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(self.snapshot(), before)

    def test_closed_and_retracted_identities_are_not_recreated(self):
        for identity, values in (
            ('mem-closed', {'type': 'todo', 'status': 'completed', 'completed_at': '2026-09-01T00:00:00Z'}),
            ('mem-retracted', {'validity': 'retracted', 'body': ''}),
        ):
            with self.subTest(identity=identity):
                self.create(identity, **values)
                before = self.snapshot()
                with self.assertRaises(FileExistsError):
                    self.create(identity, type='todo', status='active')
                self.assertEqual(self.snapshot(), before)

    def test_renamed_nested_memory_still_owns_its_identity(self):
        self.create()
        original = self.service.vault.memory_path('mem-item')
        renamed = original.parent / 'nested' / 'display-name.md'
        renamed.parent.mkdir()
        original.rename(renamed)
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertFalse(original.exists())
        self.assertEqual(self.snapshot(), before)

    def test_identity_comparison_is_case_insensitive(self):
        self.create('mem-Mixed')
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.create('MEM-MIXED')
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_claims_are_not_arbitrarily_selected(self):
        self.create()
        original = self.service.vault.memory_path('mem-item')
        original.with_name('duplicate.md').write_bytes(original.read_bytes())
        before = self.snapshot()
        with self.assertRaises((FileExistsError, RetrievalError)):
            self.create()
        self.assertEqual(self.snapshot(), before)

    def test_invalid_record_with_requested_identity_is_not_absence(self):
        value = Memory.new(memory_id='mem-item', title='Bad', body='original').to_markdown()
        path = self.service.vault.knowledge_path / 'renamed-invalid.md'
        path.write_text(value.replace('hit_count: 0', 'hit_count: -1'), encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(RetrievalError):
            self.create()
        self.assertEqual(self.snapshot(), before)

    def test_unknown_identity_or_scan_limit_cannot_prove_absence(self):
        unknown = self.service.vault.knowledge_path / 'unknown.md'
        unknown.write_bytes(b'\xff\xfe invalid file')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.snapshot(), before)
        unknown.unlink()
        self.create('mem-other')
        before = self.snapshot()
        with patch('memleaf.query_scan.MAX_FILES', 0):
            with self.assertRaises(ValueError):
                self.create()
        self.assertEqual(self.snapshot(), before)

    def test_unrelated_identifiable_bad_record_does_not_block_creation(self):
        value = Memory.new(memory_id='mem-bad', title='Bad', body='original').to_markdown()
        path = self.service.vault.knowledge_path / 'bad.md'
        path.write_text(value.replace('hit_count: 0', 'hit_count: -1'), encoding='utf-8')
        raw = path.read_bytes()
        self.create()
        self.assertEqual(self.service.read('mem-item').body, 'Original')
        self.assertEqual(path.read_bytes(), raw)

    def test_destination_file_with_different_identity_is_not_overwritten(self):
        path = self.service.vault.memory_path('mem-item')
        path.write_text(Memory.new(memory_id='mem-other', title='Other', body='Other').to_markdown(), encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(self.snapshot(), before)

    def test_history_and_knowledge_share_the_create_identity_namespace(self):
        self.create('hist-existing', area='history')
        before = self.snapshot()
        for area in ('knowledge', 'history'):
            with self.subTest(area=area):
                with self.assertRaises(FileExistsError):
                    self.create('hist-existing', area=area)
        self.assertEqual(self.snapshot(), before)
        self.create('mem-current')
        with self.assertRaises(FileExistsError):
            self.create('mem-current', area='history')

    def test_raw_write_aliases_offer_opt_in_create_only(self):
        for name in ('write_memory', 'save_memory', 'add_memory'):
            with self.subTest(name=name):
                memory = Memory.new(memory_id='mem-' + name, title='Raw', body='first')
                writer = getattr(self.service, name)
                writer(memory, overwrite=False)
                before = self.snapshot()
                with self.assertRaises(FileExistsError):
                    writer(memory, overwrite=False)
                self.assertEqual(self.snapshot(), before)

    def test_legacy_raw_write_and_explicit_overwrite_keep_their_contract(self):
        for name in ('write_memory', 'save_memory', 'add_memory'):
            with self.subTest(name=name):
                memory = self.create('mem-' + name)
                memory.body = 'Trusted replacement'
                getattr(self.service, name)(memory)
                self.assertEqual(self.service.read(memory.memory_id).body, 'Trusted replacement')
                memory.body = 'Explicit trusted replacement'
                getattr(self.service, name)(memory, overwrite=True)
                self.assertEqual(self.service.read(memory.memory_id).body, memory.body)
        self.assertEqual(self.service.vault.list_markdown('history'), [])

    def test_overwrite_flag_requires_a_real_boolean(self):
        for value in (None, 0, 1, '', 'false', [], {}):
            with self.subTest(value=value):
                before = self.snapshot()
                with self.assertRaises(TypeError):
                    self.service.write_memory(Memory.new(title='Bad', body='Bad'), overwrite=value)
                self.assertEqual(self.snapshot(), before)

    def test_scan_change_is_detected_before_writing(self):
        scan = service_module.scan_memories
        def changed_scan(*args, **kwargs):
            result = scan(*args, **kwargs)
            path = self.service.vault.knowledge_path / 'concurrent.md'
            path.write_text(Memory.new(memory_id='mem-item', title='Concurrent', body='new').to_markdown(), encoding='utf-8')
            return result
        with patch.object(service_module, 'scan_memories', side_effect=changed_scan):
            with self.assertRaises(RetrievalError):
                self.create()
        self.assertFalse(self.service.vault.memory_path('mem-item').exists())
        self.assertEqual(self.service.read('mem-item').body, 'new')

    def test_two_cooperating_clients_cannot_both_create_one_identity(self):
        barrier = threading.Barrier(2)
        def create_from_client(body):
            client = Memleaf(self.service.vault.root)
            barrier.wait(timeout=5)
            try:
                client.create_memory(memory_id='mem-race', title='Race', body=body)
                return ('created', body)
            except FileExistsError:
                return ('conflict', body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create_from_client, ('first', 'second')))
        self.assertEqual(sorted(state for state, _ in results), ['conflict', 'created'])
        winner = next(body for state, body in results if state == 'created')
        self.assertEqual(self.service.read('mem-race').body, winner)
        self.assertEqual(len(self.service.vault.list_markdown('knowledge')), 1)


if __name__ == '__main__':
    unittest.main()
