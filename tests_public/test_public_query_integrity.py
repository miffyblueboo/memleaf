from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.budget import payload_chars
from memleaf.mcp_server import _catalog_result, _search_result, _read_page_result
from memleaf.models import MemoryVersionError
from memleaf.query_scan import scan_memories, ensure_scan_current
from memleaf.retrieval import RetrievalError


class PublicQueryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Memleaf.initialize(Path(self.tmp.name) / 'vault')

    def memory(self, mid='task', **kw):
        args = dict(memory_id=mid, title='Delivery task', body='Private body for delivery task',
                    type='todo', status='active', scopes=['project:Atlas'])
        args.update(kw)
        return self.s.create_memory(**args)

    def bad(self, scopes='[project:Atlas]', name='broken.md'):
        path = self.s.vault.knowledge_path / name
        path.write_text('---\nmemory_id: broken\ntitle: Broken\nscopes: '+scopes+'\nvalidity: []\n---\nSECRET BAD BODY', encoding='utf-8')
        return path

    def test_valid_files_survive_malformed_sibling(self):
        self.memory(); self.bad()
        value = self.s.list_todos()
        self.assertEqual([v['memory_id'] for v in value['results']], ['task'])
        self.assertEqual(value['scan_status']['status'], 'partial')
        self.assertEqual(value['scan_status']['codes'], {'invalid_memory': 1})
        self.assertEqual(self.s.read('task').body, 'Private body for delivery task')

    def test_49_valid_and_one_bad_is_not_complete(self):
        for i in range(49):
            self.memory(f't-{i:02d}')
        self.bad(); values=[]; cursor=None
        while True:
            page=self.s.list_todos(cursor=cursor)
            self.assertEqual(page['scan_status']['status'], 'partial')
            values.extend(page['results'])
            cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(values),49)
        self.assertEqual(len({v['memory_id'] for v in values}),49)

    def test_known_foreign_error_does_not_degrade_precise_scope(self):
        self.memory(); self.bad('[project:Beacon]')
        self.assertEqual(self.s.list_todos(scope='project:Atlas')['scan_status']['status'],'complete')
        self.assertEqual(self.s.list_todos()['scan_status']['status'],'partial')

    def test_unknown_error_scope_is_not_guessed_from_directory(self):
        self.memory(); self.bad('[bad scope]')
        self.assertEqual(self.s.list_todos(scope='project:Atlas')['scan_status']['status'],'partial')

    def test_invalid_utf8_is_visible_without_echo(self):
        self.memory();(self.s.vault.knowledge_path/'broken.md').write_bytes(b'\xffsecret')
        result=self.s.search_candidates('Delivery')
        self.assertEqual(result['status'],'found')
        self.assertEqual(result['scan_status']['status'],'partial')
        self.assertNotIn('secret',json.dumps(result))

    def test_duplicate_identity_is_quarantined_and_read_rejected(self):
        self.memory()
        source=self.s.vault.memory_path('task')
        (self.s.vault.knowledge_path/'copy.md').write_bytes(source.read_bytes())
        self.assertEqual(self.s.list_todos()['results'],[])
        with self.assertRaises(RetrievalError) as error:self.s.read_page('task')
        self.assertEqual(error.exception.code,'memory_id_conflict')
        with self.assertRaises(RetrievalError):self.s.read('task')

    def test_bad_claimant_cannot_lose_to_valid_duplicate(self):
        self.memory();p=self.bad(name='copy.md');p.write_text(p.read_text().replace('memory_id: broken','memory_id: task'))
        with self.assertRaises(RetrievalError):self.s.read_page('task')
        self.assertEqual(self.s.search_candidates('Delivery')['results'],[])

    def test_case_variant_duplicate_portable_identity_conflict(self):
        self.memory('Task');self.memory('task')
        self.assertEqual(self.s.list_todos()['results'],[])
        with self.assertRaises(RetrievalError):self.s.read_page('Task')

    def test_same_title_independent_inherited_todos_are_not_hidden(self):
        self.memory('global',scopes=['global'])
        self.memory('local')
        value=self.s.list_todos(scope='project:Atlas')
        self.assertEqual({r['memory_id'] for r in value['results']},{'global','local'})

    def test_external_edit_uses_current_markdown_without_rebuild(self):
        self.memory();p=self.s.vault.memory_path('task');old_index=self.s.vault.tags_index_path.read_bytes()
        p.write_text(p.read_text().replace('Delivery task','External new title'))
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=AssertionError('rebuild')):
            value=self.s.search_candidates('External')
        self.assertEqual(value['results'][0]['title'],'External new title')
        self.assertEqual(self.s.vault.tags_index_path.read_bytes(),old_index)

    def test_absent_index_does_not_mutate_or_hide_current_record(self):
        self.memory();self.s.vault.tags_index_path.unlink()
        self.assertEqual(self.s.search_candidates('Delivery')['status'],'found')
        self.assertFalse(self.s.vault.tags_index_path.exists())

    def test_damaged_index_does_not_mutate_or_hide_current_record(self):
        self.memory();self.s.vault.tags_index_path.write_text('{bad')
        self.assertEqual(self.s.search_candidates('Delivery')['status'],'found')
        self.assertEqual(self.s.vault.tags_index_path.read_text(),'{bad')

    def test_no_public_query_runs_compaction_recovery(self):
        self.memory()
        with patch.object(self.s,'_recover_compaction_unlocked',side_effect=AssertionError('recovery')):
            self.s.read('task');self.s.read_page('task');self.s.list_todos();self.s.scope_catalog();self.s.search_candidates('Delivery')

    def test_accounting_does_not_invalidate_generation_or_pages(self):
        self.memory('one');self.memory('two')
        first=self.s.search_candidates('Delivery',limit=1)
        page=self.s.read_page(first['results'][0]['memory_id'],max_chars=5)
        self.assertEqual(page['knowledge_generation'],first['knowledge_generation'])
        second=self.s.search_candidates('Delivery',cursor=first['next_cursor'])
        self.assertEqual(second['knowledge_generation'],first['knowledge_generation'])
        self.s.read_page(page['memory_id'],offset=page['next_offset'],expected_version=page['version'])

    def test_external_state_change_invalidates_cursor(self):
        self.memory('one');self.memory('two')
        first=self.s.search_candidates('Delivery',limit=1)
        p=self.s.vault.memory_path('two');mem=self.s.read('two');mem.status='completed';p.write_text(mem.to_markdown())
        with self.assertRaises(RetrievalError) as error:self.s.search_candidates('Delivery',cursor=first['next_cursor'])
        self.assertEqual(error.exception.code,'stale_cursor')

    def test_damage_change_invalidates_even_identical_valid_candidates(self):
        self.memory('one');self.memory('two');bad=self.bad()
        first=self.s.search_candidates('Delivery',limit=1)
        bad.write_text(bad.read_text()+'\nCHANGED')
        with self.assertRaises(RetrievalError):self.s.search_candidates('Delivery',cursor=first['next_cursor'])

    def test_complete_scan_rechecks_same_mtime_edits(self):
        import os
        self.memory();before=scan_memories(self.s.vault);p=self.s.vault.memory_path('task');stamp=p.stat()
        p.write_text(p.read_text().replace('Delivery','delivery'))
        os.utime(p,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        with self.assertRaises(RetrievalError):ensure_scan_current(self.s.vault,before)

    def test_symlink_never_followed_and_is_reported(self):
        self.memory();outside=Path(self.tmp.name)/'secret';outside.write_text('SECRET')
        try:(self.s.vault.knowledge_path/'link.md').symlink_to(outside)
        except OSError:self.skipTest('symlinks unavailable')
        value=self.s.list_todos()
        self.assertEqual(value['scan_status']['codes'].get('unsafe_path'),1)
        self.assertNotIn('SECRET',json.dumps(value))

    def test_symlink_directory_is_not_silent_absence(self):
        self.memory();outside=Path(self.tmp.name)/'external';outside.mkdir()
        try:(self.s.vault.knowledge_path/'linked').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('symlinks unavailable')
        self.assertEqual(self.s.scope_catalog()['scan_status']['status'],'partial')

    def test_scan_resource_limit_does_not_claim_no_match_complete(self):
        self.memory()
        with patch('memleaf.query_scan.MAX_FILE_BYTES',10):value=self.s.search_candidates('Delivery')
        self.assertEqual(value['status'],'no_match');self.assertEqual(value['scan_status']['status'],'partial')

    def test_no_source_or_memory_body_in_metadata_or_candidates(self):
        self.memory();self.bad()
        for value in (self.s.search_candidates('Delivery'),self.s.list_todos(),self.s.scope_catalog()):
            self.assertNotIn('Private body',json.dumps(value))
            self.assertNotIn('SECRET BAD BODY',json.dumps(value))
            self.assertNotIn(str(self.s.vault.root),json.dumps(value))

    def test_mcp_keeps_observation_metadata_without_expanding_candidates(self):
        self.memory();self.bad()
        search=_search_result(self.s.search_candidates('Delivery'))
        self.assertEqual(set(search['results'][0]),{'memory_id','title'})
        self.assertEqual(search['scan_status']['status'],'partial')
        self.assertIn('pipeline_status',_read_page_result(self.s.read_page('task')))
        self.assertIn('knowledge_generation',_catalog_result(self.s.scope_catalog()))

    def test_mcp_rejects_unvalidated_diagnostic_payload(self):
        self.memory();result=self.s.search_candidates('Delivery')
        result['scan_status']['codes']={'secret credential':1}
        with self.assertRaises(ValueError):_search_result(result)

    def test_body_offset_equal_end_valid_beyond_end_invalid(self):
        self.memory(body='abc')
        self.assertEqual(self.s.read_page('task',offset=3)['body'],'')
        with self.assertRaises(RetrievalError) as error:self.s.read_page('task',offset=4)
        self.assertEqual(error.exception.code,'invalid_offset')
        _read_page_result(self.s.read_page('task',offset=3))

    def test_empty_retracted_page_and_overflow_are_distinct(self):
        self.memory();self.s.retract_memory('task',expected_revision=self.s.memory_revision('task'))
        self.assertIsNone(self.s.read_page('task'))
        self.assertEqual(self.s.read_page('task',include_history=True)['body'],'')
        with self.assertRaises(RetrievalError):self.s.read_page('task',include_history=True,offset=1)

    def test_counter_write_failure_does_not_hide_body(self):
        self.memory()
        with patch('memleaf.service.atomic_write_text',side_effect=OSError('read-only')):
            result=self.s.read_page('task')
        self.assertEqual(result['read_accounting'],'unavailable');self.assertTrue(result['body'])

    def test_legacy_missing_times_do_not_get_persisted_by_read(self):
        p=self.s.vault.knowledge_path/'legacy.md';p.write_text('---\ntitle: Legacy\n---\nUseful')
        old=p.read_bytes();one=self.s.read_page('legacy');two=self.s.read_page('legacy')
        self.assertEqual(one['version'],two['version']);self.assertEqual(p.read_bytes(),old)
        self.assertEqual(one['read_accounting'],'not_counted')

    def test_catalog_and_search_include_envelope_within_original_budget(self):
        for i in range(25):self.memory(f't-{i}',scopes=[f'project:Project{i}'])
        for kind,max_size in ((self.s.scope_catalog,2000),(lambda **kw:self.s.search_candidates('Delivery',**kw),4000)):
            cursor=None
            for _ in range(30):
                value=kind(cursor=cursor)
                self.assertLessEqual(payload_chars(value),max_size)
                cursor=value['next_cursor']
                if not cursor:break
            else:self.fail('pagination failed to terminate')

    def test_retracted_current_head_cannot_fall_back_to_retired_todo(self):
        mem=self.memory()
        self.s.create_memory(memory_id='history-task',title=mem.title,body=mem.body,type='todo',status='completed',
            area='history',scopes=mem.scopes,active_memory_id='task',invalidated_reason='todo_closed')
        self.s.retract_memory('task',expected_revision=self.s.memory_revision('task'))
        self.assertEqual(self.s.list_todos(status='all')['results'],[])

    def test_read_version_mismatch_does_not_count_hit(self):
        self.memory();before=self.s.vault.memory_path('task').read_bytes()
        with self.assertRaises(MemoryVersionError):self.s.read_page('task',expected_version='old')
        self.assertEqual(before,self.s.vault.memory_path('task').read_bytes())

    def test_changed_record_during_query_is_not_returned_as_current(self):
        self.memory()
        from memleaf.query_scan import ensure_scan_current
        def interfere(vault,snapshot):
            p=vault.memory_path('task');p.write_text(p.read_text()+'\nchanged')
            ensure_scan_current(vault,snapshot)
        with patch('memleaf.service.ensure_scan_current',side_effect=interfere):
            with self.assertRaises(RetrievalError) as error:self.s.search_candidates('Delivery')
        self.assertEqual(error.exception.code,'scan_changed')

    def test_duplicate_broken_identity_does_not_mutate_on_forget(self):
        self.memory();p=self.s.vault.memory_path('task');copy=p.parent/'copy.md';copy.write_bytes(p.read_bytes())
        # Public reads reject ambiguity; original explicit Forget still sees all
        # matching files rather than losing them through the query quarantine.
        with self.s.vault.lock():
            self.assertEqual(len(self.s._find_forget_records_unlocked('task')),2)

    def test_invalid_metadata_diagnostic_is_stable_between_queries(self):
        self.memory();self.bad()
        one=self.s.list_todos();two=self.s.list_todos()
        self.assertEqual(one['knowledge_generation'],two['knowledge_generation'])
        self.assertEqual(one['scan_status'],two['scan_status'])

    def test_legacy_search_does_not_pick_duplicate_identity(self):
        self.memory();p=self.s.vault.memory_path('task');(p.parent/'copy.md').write_bytes(p.read_bytes())
        self.assertEqual(self.s.search('Delivery'),[])
