"""Body compaction is not another business-state planner."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import memleaf
from memleaf import Memleaf, Memory
from memleaf.compaction import CompactionError, Compactor, _sha256
from memleaf.config import save_config
from memleaf.locking import atomic_write_text
from memleaf.prompts import COMPACT_SYSTEM, compact_prompt
from memleaf.validation import ModelOutputError, parse_compact_output, COMPACT_MAX_BYTES, COMPACT_MAX_ITEMS


class BodyCompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Memleaf.initialize(Path(self.tmp.name) / 'vault')
        config = self.s.vault.config()
        config['process']['memory_compact_threshold_tokens'] = 1
        config['process']['memory_compact_candidate_ratio'] = 1.0
        config['history']['policy'] = 'keep_all'
        save_config(self.s.vault.config_path, config)
        self.calls = []

    def target(self, memory_id='task-1', **overrides):
        data = dict(memory_id=memory_id, title='Confirmed review',
                    body='Only the confirmed review is complete. ' * 70,
                    type='todo', scopes=['project:Atlas'], scope_source='user',
                    status='completed', completed_at='2026-09-16T10:00:00Z',
                    due_date='2026-09-16', tags=['review'], aliases=['R1'], keywords=['review'],
                    created='2026-08-01T00:00:00Z', updated='2026-09-16T10:00:00Z',
                    assignee='Alice', waiting_on=None, due_text='September 16, end of day',
                    due_anchor={'ref': 'original-proof'},
                    field_basis={'status': {'event_key': 'proof', 'source_time': '2026-09-16T10:00:00Z'}},
                    custom={'list': [1, {'keep': '原值'}]}, explicit_remember=True,
                    sources=[{'event_key': 'proof', 'session_id': 's'}])
        data.update(overrides)
        self.s.create_memory(**data)
        return self.head(memory_id)

    def head(self, memory_id='task-1'):
        path = self.s.vault.memory_path(memory_id)
        return Memory.from_markdown(path.read_text(encoding='utf-8'), path)

    def raw(self):
        return {str(p.relative_to(self.s.vault.root)): p.read_bytes()
                for area in ('knowledge', 'history') for p in self.s.vault.list_markdown(area)}

    def backend(self, items):
        def complete(prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return json.dumps({'memories': items}, ensure_ascii=False)
        return complete

    @staticmethod
    def proposal(memory_id='task-1', body='Only the confirmed review is complete.'):
        return {'source_memory_ids': [memory_id], 'body': body}

    @staticmethod
    def protected(memory):
        result = deepcopy(memory.to_dict())
        for key in ('body', 'updated', 'compacted_at', 'compaction_source_ids'):
            result.pop(key, None)
        return result

    def test_closed_todo_keeps_all_fields_same_id_and_history(self):
        old = self.target()
        result = self.s.compact(model=self.backend([self.proposal()]))
        current = self.head()
        self.assertEqual(self.protected(current), self.protected(old))
        self.assertEqual(current.body, self.proposal()['body'])
        self.assertEqual(result['replacements'], ['task-1'])
        self.assertEqual(result['backend_calls'], 1)
        self.assertLess(result['record_bytes_after'], result['record_bytes_before'])
        self.assertEqual(self.s.list_todos()['results'], [])
        history = Memory.from_markdown(self.s.vault.list_markdown('history')[0].read_text(encoding='utf-8'))
        self.assertEqual(history.body, old.body)
        self.assertEqual(history.status, old.status)
        self.assertEqual(history.extra['field_basis'], old.extra['field_basis'])
        self.assertEqual(history.sources, old.sources)

    def test_active_and_cancelled_status_are_not_defaults(self):
        for status in ('active', 'cancelled'):
            with self.subTest(status=status):
                old = self.target(status, status=status, completed_at=None)
                self.s.compact(model=self.backend([self.proposal(status)]))
                self.assertEqual(self.protected(self.head(status)), self.protected(old))

    def test_all_model_metadata_fields_are_rejected(self):
        self.target()
        for field, value in {'status':'active', 'due_date':None, 'scopes':['project:Beacon'],
                             'type':'fact', 'validity':'valid', 'sources':[], 'field_basis':{},
                             'title':'new', 'assignee':'Bob', 'waiting_on':'Bob', 'tags':[],
                             'completed_at':None, 'updated':'now', 'custom':None}.items():
            with self.subTest(field=field):
                before = self.raw()
                with self.assertRaises(CompactionError):
                    self.s.compact(model=self.backend([dict(self.proposal(), **{field:value})]))
                self.assertEqual(self.raw(), before)

    def test_multi_fact_merge_is_rejected_without_deleting_any_identity(self):
        for name in ('a','b'):
            self.target(name, type='fact', status=None, completed_at=None, due_date=None)
        before=self.raw()
        with self.assertRaises(CompactionError):
            self.s.compact(model=self.backend([{'source_memory_ids':['a','b'], 'body':'merged'}]))
        self.assertEqual(self.raw(), before)

    def test_unselected_memory_is_unchanged(self):
        self.target(); self.target('other')
        path=self.s.vault.memory_path('other'); before=path.read_bytes()
        self.s.compact(model=self.backend([self.proposal()]))
        self.assertEqual(path.read_bytes(), before)

    def test_unchanged_body_is_noop_without_history(self):
        old=self.target(); before=self.raw()
        result=self.s.compact(model=self.backend([self.proposal(body=old.body)]))
        self.assertEqual(result['status'],'noop'); self.assertEqual(before,self.raw())

    def test_empty_result_has_no_retention_side_effects(self):
        old=self.target()
        self.s.create_memory(memory_id='ancient',title='old',body='historical',area='history',
                             archived_at='2000-01-01T00:00:00Z')
        before=self.raw()
        result=self.s.compact(model=self.backend([]))
        self.assertEqual(before,self.raw()); self.assertEqual(result['retention']['status'],'not_run')
        self.assertEqual(self.protected(old),self.protected(self.head()))

    def test_oversized_provenance_not_trimmed_as_a_hidden_side_effect(self):
        self.target(); memory=self.head()
        memory.sources=[{'event_key':f'proof-{i}'} for i in range(25)]
        atomic_write_text(self.s.vault.memory_path('task-1'),memory.to_markdown())
        self.s.compact(model=self.backend([self.proposal()]))
        self.assertEqual(self.head().sources,memory.sources)
        history=Memory.from_markdown(self.s.vault.list_markdown('history')[0].read_text(encoding='utf-8'))
        self.assertEqual(history.sources,memory.sources)

    def test_retracted_records_never_sent_or_restored(self):
        self.target(validity='retracted',body=''); before=self.raw()
        result=self.s.compact(model=self.backend([self.proposal()]))
        self.assertEqual(result['status'],'not_due'); self.assertFalse(self.calls)
        self.assertEqual(before,self.raw())

    def test_complete_context_contains_responsibility_and_unparsed_deadline(self):
        self.target(status='active',completed_at=None,due_date=None,due_text='after acceptance')
        self.s.compact(model=self.backend([]))
        prompt=self.calls[0][0]
        self.assertIn('Alice',prompt); self.assertIn('after acceptance',prompt)
        self.assertNotIn('original-proof',prompt); self.assertNotIn('field_basis',prompt)
        self.assertEqual(self.calls[0][1]['system'],COMPACT_SYSTEM)

    def test_input_bound_rejects_before_resolving_backend(self):
        self.target(body='x'*(COMPACT_MAX_BYTES+1))
        with patch.object(Compactor,'_resolve_backend',side_effect=AssertionError('must not resolve')):
            with self.assertRaisesRegex(CompactionError,'input exceeds'):
                self.s.compact()

    def test_output_bound_and_empty_body_are_not_usable_decisions(self):
        for raw in ('x'*(COMPACT_MAX_BYTES+1), json.dumps({'memories':[self.proposal(body=' ')]})):
            with self.subTest(length=len(raw)), self.assertRaises(ModelOutputError):
                parse_compact_output(raw,['task-1'])

    def test_refs_not_guessed_and_duplicates_not_applied(self):
        for ids in ([1], [], ['unknown'], ['TASK-1'], ['task-1','task-1']):
            with self.subTest(ids=ids), self.assertRaises(ModelOutputError):
                parse_compact_output(json.dumps({'memories':[{'source_memory_ids':ids,'body':'short'}]}),['task-1'])
        with self.assertRaises(ModelOutputError):
            parse_compact_output(json.dumps({'memories':[self.proposal(),self.proposal()]}),['task-1'])

    def test_candidate_count_is_bounded_without_splitting_bodies(self):
        for i in range(COMPACT_MAX_ITEMS+1): self.target(f'task-{i:02}')
        result=self.s.compact(model=self.backend([]))
        self.assertEqual(len(result['candidates']), COMPACT_MAX_ITEMS); self.assertEqual(len(self.calls),1)

    def test_bigger_body_is_rejected_without_changes(self):
        old=self.target(); before=self.raw()
        with self.assertRaises(CompactionError):
            self.s.compact(model=self.backend([self.proposal(body=old.body+'more')]))
        self.assertEqual(before,self.raw())

    def test_duplicate_id_blocks_before_model(self):
        old=self.target()
        atomic_write_text(self.s.vault.knowledge_path/'duplicate.md',old.to_markdown())
        before=self.raw()
        with self.assertRaises(CompactionError): self.s.compact(model=self.backend([]))
        self.assertFalse(self.calls); self.assertEqual(before,self.raw())

    def test_bad_file_does_not_turn_into_an_empty_maintenance_catalog(self):
        self.target(); (self.s.vault.knowledge_path/'bad.md').write_bytes(b'---\nvalidity: []\n---\nbad')
        with self.assertRaises(CompactionError): self.s.compact(model=self.backend([]))
        self.assertFalse(self.calls)
        self.assertIsNotNone(self.s.read('task-1'))

    def test_relocated_target_is_not_copied_to_canonical_path(self):
        self.target(); path=self.s.vault.memory_path('task-1'); moved=path.with_name('relocated.md'); path.rename(moved)
        with self.assertRaisesRegex(CompactionError,'noncanonical'):
            self.s.compact(model=self.backend([self.proposal()]))
        self.assertTrue(moved.exists()); self.assertFalse(path.exists()); self.assertFalse(self.calls)

    def test_concurrent_business_edit_is_not_overwritten(self):
        self.target()
        def backend(*args,**kwargs):
            current=self.head();current.status='active';current.completed_at=None
            atomic_write_text(self.s.vault.memory_path('task-1'),current.to_markdown())
            return json.dumps({'memories':[self.proposal()]})
        with self.assertRaises(CompactionError): self.s.compact(model=backend)
        self.assertEqual(self.head().status,'active');self.assertEqual(self.s.vault.list_markdown('history'),[])

    def test_forget_during_model_does_not_resurrect(self):
        self.target()
        def backend(*a,**k):
            self.s.forget_memory('task-1')
            return json.dumps({'memories':[self.proposal()]})
        with self.assertRaises(CompactionError): self.s.compact(model=backend)
        self.assertFalse(self.s.vault.memory_path('task-1').exists())

    def test_preflight_rejects_metadata_mutation_even_after_parser(self):
        self.target(); compactor=Compactor(self.s)
        selected,all_active,_=compactor._snapshot(1,1.0)
        replacement=compactor._build_replacement(self.proposal(),selected,now='2026-09-18T00:00:00Z')
        replacement.memory.extra['assignee']='Bob'
        with self.assertRaisesRegex(CompactionError,'protected'):
            compactor._preflight(selected,all_active,[replacement])

    def test_index_failure_rolls_back_original_without_model_retries(self):
        self.target();before=self.raw(); original=self.s._rebuild_index_unlocked; calls=[]
        def rebuild():
            calls.append(1)
            if len(calls)==1: raise OSError('synthetic failure')
            return original()
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=rebuild):
            with self.assertRaises(CompactionError): self.s.compact(model=self.backend([self.proposal()]))
        self.assertEqual(before,self.raw());self.assertEqual(len(self.calls),1)
        self.assertFalse(self.s.vault.compaction_journal_path.exists())

    def test_legacy_multi_source_rollback_is_still_supported(self):
        self.target('a',type='fact',status=None,completed_at=None,due_date=None)
        self.target('b',type='fact',status=None,completed_at=None,due_date=None)
        compactor=Compactor(self.s); originals={i:self.s.vault.memory_path(i).read_text(encoding='utf-8') for i in ('a','b')}
        with self.s.vault.lock():
            staging=compactor._staging_dir_unlocked('legacy',create=True)
            entries=[]
            for i,raw in originals.items():
                atomic_write_text(staging/f'{i}.md',raw)
                entries.append({'memory_id':i,'sha256':_sha256(raw),'staging_file':f'{i}.md'})
            replacement=self.head('a'); replacement.body='previously merged'
            atomic_write_text(self.s.vault.memory_path('a'),replacement.to_markdown())
            self.s.vault.memory_path('b').unlink()
            compactor._write_journal_unlocked({'version':1,'transaction_id':'legacy','phase':'sources_removed',
                'sources':entries,'replacements':[{'memory_id':'a','sha256':_sha256(replacement.to_markdown())}], 'histories':[]})
        with self.s._mutation_boundary(): pass
        for i,raw in originals.items(): self.assertEqual(self.s.vault.memory_path(i).read_text(encoding='utf-8'),raw)

    def test_actual_process_exit_recovers_original_record(self):
        self.target(); before=self.raw()
        script=r'''
import json, os, sys
from pathlib import Path
from memleaf import Memleaf
import memleaf.compaction as module
s=Memleaf(Path(sys.argv[1]))
real=module.atomic_write_text
def write(path, text, *a, **k):
    real(path,text,*a,**k)
    if path == s.vault.memory_path('task-1'): os._exit(79)
module.atomic_write_text=write
s.compact(model=lambda *a,**k: json.dumps({'memories':[{'source_memory_ids':['task-1'],'body':'Only the confirmed review is complete.'}]}))
'''
        env=dict(os.environ, PYTHONPATH=str(Path(memleaf.__file__).resolve().parent.parent))
        child=subprocess.run([sys.executable,'-c',script,str(self.s.vault.root)],env=env,capture_output=True,timeout=30)
        self.assertEqual(child.returncode,79,child.stderr.decode('utf-8','replace'))
        fresh=Memleaf(self.s.vault.root)
        with fresh._mutation_boundary(): pass
        self.assertEqual(before,self.raw()); self.assertFalse(self.s.vault.compaction_journal_path.exists())


if __name__=='__main__': unittest.main()
