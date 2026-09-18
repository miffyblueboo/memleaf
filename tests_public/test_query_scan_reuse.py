"""Request-local parse reuse must not weaken the public snapshot contract."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf import query_scan as qs
from memleaf.retrieval import RetrievalError


class QueryScanReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Memleaf.initialize(Path(self.tmp.name) / '中文 vault')
        self.paths = [self.write('one'), self.write('two')]

    def write(self, identity, *, area='knowledge', **kwargs):
        args = dict(memory_id=identity, title='Task '+identity, body='Original business fact',
                    type='todo', status='active', scopes=['project:Atlas'],
                    created='2026-09-17T10:00:00+08:00', updated='2026-09-17T10:00:00+08:00')
        args.update(kwargs)
        path = self.s.vault.root / area / (identity+'.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(Memory(**args).to_markdown(), encoding='utf-8')
        return path

    def snapshot(self, history=False):
        return qs.scan_memories(self.s.vault, history)

    def changed(self, snapshot):
        with self.assertRaises(RetrievalError) as caught:
            qs.ensure_scan_current(self.s.vault, snapshot)
        self.assertEqual(caught.exception.code, 'scan_changed')

    def test_unchanged_recheck_reads_all_files_but_does_not_parse_again(self):
        before = self.snapshot()
        opened = []
        original = Path.open
        def tracked(path, *args, **kwargs):
            if path in self.paths: opened.append(path)
            return original(path, *args, **kwargs)
        with patch.object(qs, 'parse_frontmatter', wraps=qs.parse_frontmatter) as parse, patch.object(Path, 'open', tracked):
            qs.ensure_scan_current(self.s.vault, before)
        self.assertEqual(parse.call_count, 0)
        self.assertCountEqual(opened, self.paths)

    def test_next_request_does_not_reuse_the_previous_request(self):
        before = self.snapshot()
        qs.ensure_scan_current(self.s.vault, before)
        with patch.object(qs, 'parse_frontmatter', wraps=qs.parse_frontmatter) as parse:
            after = self.snapshot()
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(after.generation, before.generation)
        self.assertIsNot(after.records[0].memory, before.records[0].memory)

    def test_reference_scan_without_reuse_has_same_generation(self):
        first = self.snapshot()
        for record in first.records:
            self.assertTrue(record.account_ready)
        full = self.snapshot()
        self.assertEqual(full.generation, first.generation)
        qs.ensure_scan_current(self.s.vault, first)

    def test_changed_body_same_length_and_mtime_is_rejected(self):
        before = self.snapshot(); path = self.paths[0]; st = path.stat()
        raw = path.read_bytes().replace(b'Original', b'Changed!')
        self.assertEqual(len(raw), st.st_size)
        path.write_bytes(raw); os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        with patch.object(qs, 'parse_frontmatter', wraps=qs.parse_frontmatter) as parse:
            self.changed(before)
        self.assertEqual(parse.call_count, 1)

    def test_accounting_change_is_reparsed_but_does_not_invalidate(self):
        before = self.snapshot(); path = self.paths[0]
        path.write_text(path.read_text(encoding='utf-8').replace('hit_count: 0','hit_count: 7'),encoding='utf-8')
        with patch.object(qs, 'parse_frontmatter', wraps=qs.parse_frontmatter) as parse:
            qs.ensure_scan_current(self.s.vault, before)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(self.snapshot().generation, before.generation)

    def test_format_only_change_is_reparsed_with_existing_equivalence(self):
        path = self.paths[0]
        # Establish actual LF bytes first. Windows write_text already emits
        # CRLF; replacing those LFs directly would create CRCRLF, not a
        # formatting-only change, and correctly invalidate the snapshot.
        original = path.read_bytes().replace(b'\r\n', b'\n')
        self.assertNotIn(b'\r', original)
        path.write_bytes(original)
        before = self.snapshot()
        changed = original.replace(b'\n', b'\r\n')
        self.assertNotEqual(changed, original)
        path.write_bytes(changed)
        with patch.object(qs, 'parse_frontmatter', wraps=qs.parse_frontmatter) as parse:
            qs.ensure_scan_current(self.s.vault, before)
        self.assertEqual(parse.call_count, 1)

    def test_mutating_returned_record_does_not_mutate_the_original_proof(self):
        before = self.snapshot()
        before.records[0].memory.body = 'not persisted'
        before.records[0].memory.scopes.append('project:Beacon')
        before.records[0].memory.memory_id = 'different'
        before.records[0].path = Path('/not/a/source')
        qs.ensure_scan_current(self.s.vault, before)

    def test_mutating_record_does_not_hide_a_file_change(self):
        before = self.snapshot(); record = before.records[0]
        record.memory.body = 'changed fact'
        record.path.write_text(record.memory.to_markdown(),encoding='utf-8')
        self.changed(before)

    def test_file_added_even_with_identical_bytes_is_detected(self):
        before = self.snapshot()
        (self.s.vault.knowledge_path/'copy.md').write_bytes(self.paths[0].read_bytes())
        self.changed(before)
        self.assertIn('one', self.snapshot().ambiguous)

    def test_file_removed_is_detected(self):
        before = self.snapshot(); self.paths[0].unlink(); self.changed(before)

    def test_file_renamed_is_detected(self):
        before = self.snapshot(); self.paths[0].rename(self.paths[0].with_name('renamed.md')); self.changed(before)

    def test_current_and_history_claimants_remain_ambiguous(self):
        self.write('one',area='history')
        before = self.snapshot(True)
        self.assertEqual(before.ambiguous, {'one'})
        self.assertEqual([r.memory.memory_id for r in before.records], ['two'])
        with patch.object(qs, 'parse_frontmatter', wraps=qs.parse_frontmatter) as parse:
            qs.ensure_scan_current(self.s.vault, before)
        self.assertEqual(parse.call_count, 0)
        self.assertEqual(before.report()['codes']['duplicate_id'], 2)

    def test_existing_duplicate_claimants_are_not_lost_when_records_are_removed(self):
        (self.s.vault.knowledge_path/'copy.md').write_bytes(self.paths[0].read_bytes())
        before = self.snapshot(); self.assertEqual(before.ambiguous, {'one'})
        qs.ensure_scan_current(self.s.vault, before)

    def test_resolving_duplicate_invalidates_old_snapshot(self):
        duplicate=self.s.vault.knowledge_path/'copy.md'; duplicate.write_bytes(self.paths[0].read_bytes())
        before=self.snapshot(); duplicate.unlink(); self.changed(before)

    def test_invalid_memory_is_always_reparsed(self):
        bad=self.s.vault.knowledge_path/'bad.md'
        bad.write_text('---\nmemory_id: bad\nvalidity: []\n---\nBody',encoding='utf-8')
        before=self.snapshot()
        with patch.object(qs,'parse_frontmatter',wraps=qs.parse_frontmatter) as parse:
            qs.ensure_scan_current(self.s.vault,before)
        self.assertEqual(parse.call_count,1)
        self.assertEqual(before.report()['status'],'partial')

    def test_invalid_claimant_cannot_disappear_behind_cached_valid_one(self):
        bad=self.s.vault.knowledge_path/'bad.md'
        bad.write_text('---\nmemory_id: one\nvalidity: []\n---\nBody',encoding='utf-8')
        before=self.snapshot(); self.assertIn('one',before.ambiguous)
        qs.ensure_scan_current(self.s.vault,before)

    def test_invalid_bytes_change_is_detected(self):
        bad=self.s.vault.knowledge_path/'bad.md'; bad.write_bytes(b'\xffone')
        before=self.snapshot(); bad.write_bytes(b'\xfftwo'); self.changed(before)

    def test_valid_to_invalid_cannot_reuse_old_success(self):
        before=self.snapshot(); self.paths[0].write_bytes(b'\xffinvalid'); self.changed(before)

    def test_bad_to_valid_invalidates_snapshot(self):
        bad=self.s.vault.knowledge_path/'bad.md'; bad.write_bytes(b'\xffinvalid')
        before=self.snapshot(); self.write('bad'); self.changed(before)

    def test_source_scope_status_and_validity_changes_are_not_ignored(self):
        for changes in ({'scopes':['project:Beacon']},{'status':'completed'},
                        {'validity':'retracted','body':''}, {'extra':{'assignee':'another'}}):
            with self.subTest(changes=changes):
                self.write('one'); before=self.snapshot(); self.write('one',**changes); self.changed(before)

    def test_missing_legacy_times_stay_unknown_after_verification(self):
        path=self.paths[0]
        path.write_text('---\nmemory_id: one\ntitle: one\n---\nLegacy',encoding='utf-8')
        before=self.snapshot(); legacy=next(r for r in before.records if r.memory.memory_id=='one')
        self.assertEqual(legacy.memory.created,''); self.assertFalse(legacy.account_ready)
        qs.ensure_scan_current(self.s.vault,before)
        self.assertNotIn('created:',path.read_text(encoding='utf-8'))

    def test_cache_cannot_bypass_new_size_bound(self):
        before=self.snapshot()
        with patch.object(qs,'MAX_FILE_BYTES',10): self.changed(before)

    def test_cache_cannot_bypass_total_size_bound(self):
        before=self.snapshot()
        with patch.object(qs,'MAX_TOTAL_BYTES',self.paths[0].stat().st_size):self.changed(before)

    def test_cache_cannot_bypass_file_count_bound(self):
        before=self.snapshot()
        with patch.object(qs,'MAX_FILES',1):self.changed(before)

    def test_file_read_failure_is_not_reused_as_success(self):
        before=self.snapshot();original=Path.open
        def failed(path,*args,**kwargs):
            if path==self.paths[0]:raise OSError('private path failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',failed):self.changed(before)

    def test_symlink_substitution_cannot_reuse_identical_content(self):
        before=self.snapshot(); outside=Path(self.tmp.name)/'outside.md'
        outside.write_bytes(self.paths[0].read_bytes());self.paths[0].unlink()
        try:self.paths[0].symlink_to(outside)
        except OSError:self.skipTest('symlinks unavailable')
        self.changed(before)

    def test_missing_parse_receipts_use_normal_recheck(self):
        before=self.snapshot();before._validated.clear()
        with patch.object(qs,'parse_frontmatter',wraps=qs.parse_frontmatter) as parse:
            qs.ensure_scan_current(self.s.vault,before)
        self.assertEqual(parse.call_count,2)

    def test_history_changes_checked_only_when_requested(self):
        history=self.write('old',area='history')
        current=self.snapshot(); historical=self.snapshot(True)
        history.write_text(history.read_text(encoding='utf-8')+'\nchanged',encoding='utf-8')
        qs.ensure_scan_current(self.s.vault,current);self.changed(historical)

    def test_empty_snapshot_detects_first_new_file(self):
        for path in self.paths:path.unlink()
        before=self.snapshot();qs.ensure_scan_current(self.s.vault,before)
        self.write('new');self.changed(before)

    def test_public_envelope_does_not_expose_parse_receipts(self):
        page=self.s.list_todos(as_of='2026-09-17',timezone='UTC')
        self.assertNotIn('_validated',page)
        self.assertEqual(page['scan_status']['status'],'complete')
        self.assertEqual({r['memory_id'] for r in page['results']},{'one','two'})
