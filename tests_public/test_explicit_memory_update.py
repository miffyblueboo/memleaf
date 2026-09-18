"""Explicit-update and recovery integration, using only synthetic local Vaults."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.config import save_config
from memleaf.memory_update import MemoryUpdateCommitError, MemoryUpdateManager, pending_explicit_mutations
from memleaf.memory_writer import MemoryWriter
from memleaf.models import MemoryVersionError
from memleaf.query_progress import observe_progress
from memleaf.retention import RetentionManager, RetentionError
from memleaf.retrieval import RetrievalError
import memleaf.memory_update as update_module


class ExplicitMemoryUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Memleaf.initialize(Path(self.tmp.name) / 'vault')
        self.s.create_memory(memory_id='m', title='目标', body='完成交付', type='todo',
            status='active', due_date='2026-09-30', assignee='A', waiting_on='B',
            tags=['tag'], sources=[{'source': 'fixture'}], custom={'keep': True})
        self.rev = self.s.memory_revision('m')

    def call(self, fields, *, expected=None, identity='m', **kwargs):
        if "scopes" in fields and "authorized_scopes" not in kwargs:
            kwargs["authorized_scopes"] = ["global", "project:Atlas"]
        return self.s.update_memory(identity, expected_revision=expected or self.rev, patch=fields, **kwargs)

    def head(self):
        p = self.s.vault.memory_path('m')
        return Memory.from_markdown(p.read_text(encoding='utf-8'), p)

    def journal(self):
        return MemoryUpdateManager(self.s)._path('m')

    def files(self):
        return {p.relative_to(self.s.vault.root).as_posix(): p.read_bytes()
                for folder in ('knowledge', 'history', '_state/explicit_updates')
                for p in (self.s.vault.root / folder).rglob('*') if p.is_file()}

    def history(self):
        return self.s.vault.list_markdown('history')

    def interrupted(self, point='index', fields=None):
        target, method = ((MemoryWriter, '_write_history') if point == 'history'
                          else (self.s, '_rebuild_index_unlocked'))
        with patch.object(target, method, side_effect=OSError('synthetic interruption')):
            with self.assertRaises(MemoryUpdateCommitError) as caught:
                self.call(fields or {'status': 'completed'})
        self.assertTrue(self.journal().is_file())
        return caught.exception

    def test_same_id_completion_keeps_unselected_fields_and_history(self):
        before = self.head().to_dict()
        result = self.call({'status': 'completed'})
        self.assertEqual(result['action'], 'UPDATE')
        self.assertEqual(result['memory_id'], 'm')
        self.assertEqual(result['model_calls'], 0)
        after = self.head()
        self.assertEqual(after.status, 'completed')
        self.assertIsNone(after.completed_at)
        for key in ('body', 'type', 'scopes', 'due_date', 'assignee', 'waiting_on', 'tags', 'sources', 'custom', 'created'):
            self.assertEqual(after.to_dict()[key], before[key], key)
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(Memory.from_markdown(self.history()[0].read_text()).status, 'active')
        self.assertEqual(result['revision'], self.s.memory_revision('m'))
        self.assertEqual(self.s.list_todos(status='active')['results'], [])

    def test_no_change_does_not_refresh_revision_time_or_make_history(self):
        before = self.files()
        result = self.call({'status': 'active', 'body': '完成交付'})
        self.assertEqual(result['action'], 'NO_CHANGE')
        self.assertEqual(result['revision'], self.rev)
        self.assertEqual(before, self.files())

    def test_canonical_trailing_newline_does_not_break_journal_or_receipt(self):
        result = self.call({'body': '新的正文\r\n第二行\r\n'})
        self.assertEqual(self.head().body, '新的正文\n第二行')
        self.assertEqual(result['revision'], self.s.memory_revision('m'))
        self.assertTrue(self.call({'body': '新的正文\r\n第二行\r\n'})['already_applied'])

    def test_multiple_new_fields_recover_independent_of_patch_key_order(self):
        self.s.create_memory(memory_id='minimal', title='Minimal', body='old', type='todo')
        revision = self.s.memory_revision('minimal')
        fields = {'waiting_on': None, 'assignee': 'Reviewer', 'deadline': {'text': '2026-09-30'},
                  'status': 'completed', 'body': 'Delivery goal'}
        with patch.object(self.s, '_rebuild_index_unlocked', side_effect=OSError('index')):
            with self.assertRaises(MemoryUpdateCommitError):
                self.s.update_memory('minimal', expected_revision=revision, patch=fields)
        self.assertEqual(pending_explicit_mutations(self.s.vault)[0]['updates'], 1)
        result = self.s.update_memory('minimal', expected_revision=revision,
                                      patch=dict(reversed(list(fields.items()))))
        self.assertTrue(result['already_applied'])
        head = self.s.read('minimal')
        self.assertEqual((head.status, head.extra['assignee'], head.due_date), ('completed', 'Reviewer', '2026-09-30'))
        self.assertEqual(len(self.history()), 1)

    def test_multifield_recovery_across_process_hash_seeds(self):
        fields = {'body': 'Delivery goal', 'status': 'completed', 'assignee': 'Reviewer',
                  'deadline': {'text': '2026-09-30'}}
        code = """import json,os,sys
from memleaf import Memleaf
from memleaf.memory_writer import MemoryWriter
s=Memleaf(sys.argv[1])
MemoryWriter._write_history=lambda *a,**k:os._exit(71)
s.update_memory('m',expected_revision=sys.argv[2],patch=json.loads(sys.argv[3]))
"""
        result = subprocess.run([sys.executable, '-c', code, str(self.s.vault.root), self.rev,
            json.dumps(fields)], capture_output=True, timeout=30,
            env={**os.environ, 'PYTHONHASHSEED': '11'})
        self.assertEqual(result.returncode, 71, result.stderr.decode(errors='replace'))
        self.assertEqual(pending_explicit_mutations(self.s.vault)[0]['updates'], 1)
        self.call(fields)
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.head().status, 'completed')
        self.assertEqual(self.head().extra['assignee'], 'Reviewer')

    def test_missing_target_never_becomes_create(self):
        before = self.files()
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            self.call({'body': 'new'}, identity='absent')
        self.assertEqual(before, self.files())

    def test_history_only_identity_cannot_be_updated(self):
        self.s.create_memory(memory_id='old', title='Old', body='old', area='history')
        before = self.files()
        with self.assertRaises(ValueError): self.call({'body': 'new'}, identity='old')
        self.assertEqual(before, self.files())

    def test_stale_revision_rejects_without_business_or_journal_changes(self):
        self.call({'assignee': 'C'})
        before = self.files()
        with self.assertRaises(MemoryVersionError): self.call({'body': 'stale'})
        self.assertEqual(before, self.files())

    def test_protected_and_invalid_fields_are_not_raw_overwrites(self):
        for fields in ({'memory_id': 'other'}, {'type': 'fact'}, {'created': 'fake'},
                       {'sources': []}, {'field_basis': {}}, {'hit_count': 500},
                       {'status': None}, {'title': ''}, {'body': ''},
                       {'tags': 'a'}, {'aliases': [3]}, {'scopes': []}, {'scopes': ['unscoped','global']},
                       {'deadline': None}, {'deadline': {'clear': 1}},
                       {'deadline': {'text':'tomorrow','date':'2030-01-01'}}, {'completed_at': 'tomorrow'}):
            with self.subTest(fields=fields):
                before = self.files()
                with self.assertRaises((ValueError, TypeError)): self.call(fields)
                self.assertEqual(before, self.files())

    def test_unknown_revision_not_optional_or_content_version(self):
        for revision in ('bad', '', None, 'g'*64):
            with self.subTest(revision=revision):
                before = self.files()
                with self.assertRaises(ValueError):
                    self.s.update_memory('m', expected_revision=revision, patch={'status':'completed'})
                self.assertEqual(before, self.files())

    def test_nullable_responsibility_and_lists_clear_only_selected_fields(self):
        self.call({'assignee': None, 'waiting_on': None, 'tags': []})
        head = self.head()
        self.assertIsNone(head.extra['assignee']); self.assertIsNone(head.extra['waiting_on'])
        self.assertEqual(head.tags, []); self.assertEqual(head.due_date, '2026-09-30')
        self.assertEqual(head.extra['custom'], {'keep': True})

    def test_deadline_uses_supplied_source_time_not_processing_clock(self):
        self.call({'deadline': {'text': '明天'}}, source_time='2026-09-18T00:15:00+08:00')
        head = self.head()
        self.assertEqual(head.due_date, '2026-09-19')
        self.assertEqual(head.extra['due_text'], '明天')
        self.assertEqual(head.extra['due_anchor']['source_time'], '2026-09-18T00:15:00+08:00')

    def test_unknown_deadline_time_stays_unresolved(self):
        self.call({'deadline': {'text': '发布后一周内'}})
        self.assertIsNone(self.head().due_date)
        self.assertEqual(self.head().extra['due_status'], 'unresolved')
        self.assertNotIn('source_time', self.head().extra['field_basis']['deadline'])
        self.assertIn('observed_at', self.head().extra['field_basis']['deadline'])

    def test_deadline_clear_has_basis_and_other_progress_does_not_restore_it(self):
        self.call({'deadline': {'clear': True}})
        first = self.head()
        self.assertEqual(first.extra['due_status'], 'cleared')
        self.call({'waiting_on': 'D'}, expected=self.s.memory_revision('m'))
        after = self.head()
        self.assertIsNone(after.due_date)
        self.assertEqual(after.extra['field_basis']['deadline'], first.extra['field_basis']['deadline'])

    def test_completion_timestamp_requires_completed_and_is_not_invented(self):
        before = self.files()
        with self.assertRaises(ValueError): self.call({'completed_at': '2026-09-18T10:00:00Z'})
        self.assertEqual(before, self.files())
        self.call({'status': 'completed', 'completed_at': '2026-09-18T10:00:00Z'})
        self.assertEqual(self.head().completed_at, '2026-09-18T10:00:00Z')

    def test_reopen_is_explicit_and_clears_previous_completion_time(self):
        self.call({'status': 'completed', 'completed_at': '2026-09-18T10:00:00Z'})
        revision = self.s.memory_revision('m')
        with self.assertRaisesRegex(ValueError, 'explicit_reopen_required'):
            self.call({'status': 'active'}, expected=revision)
        self.call({'status': 'active'}, expected=revision, reopen=True)
        self.assertEqual(self.head().status, 'active'); self.assertIsNone(self.head().completed_at)

    def test_retracted_restore_requires_explicit_validity_and_body(self):
        self.s.retract_memory('m', expected_revision=self.rev)
        revision = self.s.memory_revision('m')
        for fields in ({'body':'Restored'}, {'validity':'valid'}, {'title':'Not sufficient'}):
            with self.assertRaisesRegex(ValueError, 'explicit_restore_required'):
                self.call(fields, expected=revision)
        self.call({'validity':'valid', 'body':'Restored'}, expected=revision, restore=True)
        self.assertEqual(self.head().validity, 'valid')
        self.assertNotIn('retraction_operation_id', self.head().extra)
        self.assertEqual(len(self.history()), 2)

    def test_todo_fields_not_allowed_on_fact(self):
        self.s.create_memory(memory_id='fact', title='Fact', body='f', type='fact')
        revision = self.s.memory_revision('fact')
        with self.assertRaisesRegex(ValueError, 'todo_fields_on_non_todo'):
            self.call({'assignee':'A'}, identity='fact', expected=revision)
        self.call({'body':'Updated fact'}, identity='fact', expected=revision)

    def test_scope_correction_requires_registered_destination_and_keeps_id(self):
        with self.assertRaisesRegex(ValueError, 'registered_scope'):
            self.call({'scopes':['project:Atlas']})
        config=self.s.vault.config();config['scopes']={'project:Atlas':{}}
        save_config(self.s.vault.config_path, config)
        self.call({'scopes':['project:Atlas'], 'body':'Atlas delivery'})
        self.assertEqual(self.head().scopes, ['project:Atlas'])
        self.assertEqual(self.head().memory_id, 'm')
        self.assertEqual(self.head().scope_source, 'explicit')

    def test_case_alias_and_renamed_target_do_not_create_second_file(self):
        original=self.s.vault.memory_path('m'); new=original.parent/'nested'/'renamed.md'
        new.parent.mkdir();original.rename(new)
        self.call({'status':'completed'}, identity='M')
        self.assertFalse(original.exists())
        self.assertEqual(Memory.from_markdown(new.read_text()).status, 'completed')

    def test_duplicate_identity_blocks_before_history_or_journal(self):
        value=self.head().to_dict();value['memory_id']='M'
        (self.s.vault.knowledge_path/'copy.md').write_text(Memory.from_mapping(value).to_markdown())
        before=self.files()
        with self.assertRaises(RetrievalError): self.call({'status':'completed'})
        self.assertEqual(before, self.files())

    def test_settled_retry_is_idempotent_and_preserves_original_operation(self):
        first=self.call({'body':'new'})
        before=self.files()
        self.s=Memleaf(self.s.vault.root)
        second=self.call({'body':'new'})
        self.assertEqual(first['operation_id'],second['operation_id'])
        self.assertTrue(second['already_applied'])
        self.assertEqual(before, self.files());self.assertEqual(len(self.history()),1)

    def test_pending_request_cannot_be_replaced_by_different_patch(self):
        self.interrupted('history');before=self.files()
        with self.assertRaises(MemoryVersionError): self.call({'status':'cancelled'})
        self.assertEqual(before,self.files())

    def test_retry_after_index_failure_repairs_without_new_history_or_model(self):
        error=self.interrupted()
        self.assertTrue(error.applied)
        self.assertEqual(error.result['execution_status'],'recovery_required')
        self.assertEqual(self.head().status,'completed')
        self.s=Memleaf(self.s.vault.root)
        self.call({'status':'completed'})
        self.assertFalse(self.journal().exists());self.assertEqual(len(self.history()),1)

    def test_retry_after_pre_head_failure_applied_false(self):
        error=self.interrupted('history')
        self.assertFalse(error.applied);self.assertEqual(self.head().status,'active')
        self.call({'status':'completed'})
        self.assertEqual(self.head().status,'completed');self.assertEqual(len(self.history()),1)

    def test_unreadable_current_does_not_destroy_pending_journal(self):
        self.interrupted('history');p=self.s.vault.memory_path('m');p.write_bytes(b'\xff broken')
        before=self.files()
        with self.assertRaises(RetrievalError): self.call({'status':'completed'})
        self.assertEqual(before,self.files())

    def test_corrupt_journal_is_preserved_and_not_silently_reset(self):
        self.interrupted();self.journal().write_text('{broken')
        before=self.files()
        with self.assertRaises(ValueError): self.call({'status':'completed'})
        self.assertEqual(before,self.files())
        self.assertEqual(observe_progress(self.s.vault)['status'],'unknown')

    def test_later_edit_with_copied_operation_marker_does_not_prove_replay(self):
        self.call({'body':'first'})
        value=self.head().to_dict();value['body']='later'
        self.s.write_memory(value,overwrite=True)
        before=self.files()
        with self.assertRaises(MemoryVersionError): self.call({'body':'first'})
        self.assertEqual(before,self.files())

    def test_edit_during_history_write_is_preserved(self):
        original=MemoryWriter._write_history
        def changed(writer,*args,**kwargs):
            result=original(writer,*args,**kwargs)
            value=self.head().to_dict();value['body']='later direct edit'
            self.s.vault.memory_path('m').write_text(Memory.from_mapping(value).to_markdown())
            return result
        with patch.object(MemoryWriter,'_write_history',changed):
            with self.assertRaises(RetrievalError): self.call({'status':'completed'})
        self.assertEqual(self.head().body,'later direct edit')
        self.assertEqual(self.head().status,'active');self.assertTrue(self.journal().exists())
        with self.assertRaises(MemoryVersionError): self.call({'status':'completed'})

    def test_new_current_revision_can_supersede_stale_pending_update(self):
        self.interrupted('history')
        value=self.head().to_dict();value['body']='new external state'
        self.s.write_memory(value,overwrite=True)
        self.call({'waiting_on':'D'},expected=self.s.memory_revision('m'))
        self.assertEqual(self.head().body,'new external state')
        self.assertEqual(self.head().extra['waiting_on'],'D')
        self.assertFalse(self.journal().exists())

    def test_read_counters_after_preparation_survive_recovery(self):
        self.interrupted('history')
        value=self.head().to_dict();value.update(hit_count=42,last_hit_at='2026-09-18T12:00:00Z')
        self.s.vault.memory_path('m').write_text(Memory.from_mapping(value).to_markdown())
        self.call({'status':'completed'})
        self.assertEqual(self.head().hit_count,42)
        self.assertEqual(self.head().last_hit_at,'2026-09-18T12:00:00Z')

    def test_forget_cancels_pending_plaintext_before_deleting_and_no_resurrection(self):
        self.interrupted()
        self.assertTrue(self.s.forget_memory('m'))
        self.assertFalse(self.journal().exists());self.assertEqual(self.history(),[])
        with self.assertRaisesRegex(ValueError,'does not exist'):self.call({'status':'completed'})
        self.assertEqual(self.s.vault.list_markdown('knowledge'),[])

    def test_forget_preserves_memory_when_canceling_pending_payload_fails(self):
        self.interrupted('history')
        with patch.object(update_module,'atomic_unlink',side_effect=OSError('cancel failure')):
            with self.assertRaises(OSError):self.s.forget_memory('m')
        self.assertTrue(self.journal().exists());self.assertEqual(self.head().status,'active')

    def test_pending_status_and_migration_do_not_replay_any_write(self):
        self.interrupted()
        before=self.files()
        for _ in range(3):
            progress=observe_progress(self.s.vault)
            self.assertEqual(progress['status'],'pending')
            self.assertEqual(progress['pending_explicit_mutations'],1)
            self.assertIn('explicit_updates_recovery_pending',self.s.migration_preflight()['blockers'])
        self.assertEqual(before,self.files())
        self.call({'status':'completed'})
        self.assertEqual(observe_progress(self.s.vault)['status'],'current')

    def test_retention_does_not_prune_pending_update_history(self):
        self.interrupted()
        before={str(p):p.read_bytes() for p in self.history()}
        result=RetentionManager(self.s).maintain('2040-01-01T00:00:00Z')
        self.assertEqual(result['history_pruned'],0)
        self.assertEqual(result['protected_history_groups'],1)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.history()})
        self.call({'status':'completed'})
        self.assertEqual(RetentionManager(self.s).maintain('2040-01-01T00:00:00Z')['history_pruned'],1)

    def test_retention_and_migration_also_recognize_existing_retraction_journal(self):
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index')):
            with self.assertRaises(OSError):self.s.retract_memory('m',expected_revision=self.rev)
        before={str(p):p.read_bytes() for p in self.history()}
        self.assertEqual(RetentionManager(self.s).maintain('2040-01-01T00:00:00Z')['history_pruned'],0)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.history()})
        self.assertIn('explicit_retractions_recovery_pending',self.s.migration_preflight()['blockers'])
        self.assertEqual(observe_progress(self.s.vault)['status'],'pending')

    def test_retention_can_prune_unrelated_group_while_update_is_pending(self):
        self.s.create_memory(memory_id='other',title='other',body='old')
        self.s.update_memory('other',expected_revision=self.s.memory_revision('other'),patch={'body':'new'})
        self.interrupted()
        result=RetentionManager(self.s).maintain('2040-01-01T00:00:00Z')
        self.assertEqual(result['history_pruned'],1)
        self.assertEqual(len(self.history()),1)
        self.assertEqual(Memory.from_markdown(self.history()[0].read_text()).extra['active_memory_id'],'m')

    def test_invalid_pending_control_blocks_retention_without_deleting(self):
        self.interrupted();self.journal().write_text('{broken')
        before=self.files()
        with self.assertRaises(RetentionError):RetentionManager(self.s).maintain('2040-01-01T00:00:00Z')
        self.assertEqual(before,self.files())

    def test_process_exit_before_head_can_be_recovered_after_restart(self):
        code='''import os,sys
from memleaf import Memleaf
from memleaf.memory_writer import MemoryWriter
s=Memleaf(sys.argv[1])
MemoryWriter._write_history=lambda *a,**k:os._exit(71)
s.update_memory('m',expected_revision=sys.argv[2],patch={'status':'completed'})
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.s.vault.root),self.rev],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,71,result.stderr.decode(errors='replace'))
        self.assertTrue(self.journal().exists());self.assertEqual(self.head().status,'active')
        self.s=Memleaf(self.s.vault.root);self.call({'status':'completed'})
        self.assertEqual(len(self.history()),1);self.assertEqual(self.head().status,'completed')

    def test_process_exit_after_head_does_not_duplicate_on_restart(self):
        code='''import os,sys
from pathlib import Path
from memleaf import Memleaf
import memleaf.memory_writer as m
s=Memleaf(sys.argv[1]);original=m.atomic_write_text
def stop(*a,**k):
 original(*a,**k)
 if Path(a[0]) == s.vault.memory_path('m'): os._exit(72)
m.atomic_write_text=stop
s.update_memory('m',expected_revision=sys.argv[2],patch={'status':'completed'})
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.s.vault.root),self.rev],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,72,result.stderr.decode(errors='replace'))
        self.assertTrue(self.journal().exists())
        self.assertEqual(self.head().status,'completed')
        self.s=Memleaf(self.s.vault.root);self.call({'status':'completed'})
        self.assertEqual(len(self.history()),1);self.assertFalse(self.journal().exists())

    def test_two_processes_same_revision_allow_only_one_different_edit(self):
        code='''import sys
from memleaf import Memleaf
from memleaf.models import MemoryVersionError
s=Memleaf(sys.argv[1])
try:s.update_memory('m',expected_revision=sys.argv[2],patch={'body':sys.argv[3]})
except MemoryVersionError:sys.exit(3)
'''
        processes=[subprocess.Popen([sys.executable,'-c',code,str(self.s.vault.root),self.rev,text],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for text in ('first','second')]
        outputs=[p.communicate(timeout=30) for p in processes]
        self.assertEqual(sorted(p.returncode for p in processes),[0,3],outputs)
        self.assertEqual(len(self.history()),1)


    def test_scope_change_during_history_cannot_commit_old_scope_view(self):
        config=self.s.vault.config();config['scopes']={'project:Atlas':{}}
        save_config(self.s.vault.config_path,config)
        original=MemoryWriter._write_history
        def changed(writer,*args,**kwargs):
            result=original(writer,*args,**kwargs)
            config['scopes']['project:Atlas']={'aliases':['renamed']}
            save_config(self.s.vault.config_path,config)
            return result
        with patch.object(MemoryWriter,'_write_history',changed):
            with self.assertRaisesRegex(ValueError,'scope_registry_changed'):
                self.call({'scopes':['project:Atlas']})
        self.assertEqual(self.head().scopes,['global'])
        self.assertTrue(self.journal().exists())

    def test_applied_scope_correction_can_settle_after_registry_changes(self):
        config = self.s.vault.config()
        config['scopes'] = {'project:Atlas': {}}
        save_config(self.s.vault.config_path, config)
        error = self.interrupted(fields={'scopes': ['project:Atlas']})
        self.assertTrue(error.applied)
        before_head = self.s.vault.memory_path('m').read_bytes()
        history = {str(p): p.read_bytes() for p in self.history()}
        config['scopes']['project:Atlas'] = {'aliases': ['new display alias']}
        save_config(self.s.vault.config_path, config)
        result = self.call({'scopes': ['project:Atlas']})
        self.assertTrue(result['already_applied'])
        self.assertEqual(result['operation_id'], error.result['operation_id'])
        self.assertEqual(before_head, self.s.vault.memory_path('m').read_bytes())
        self.assertEqual(history, {str(p): p.read_bytes() for p in self.history()})
        self.assertFalse(self.journal().exists())

    def test_settled_replay_rechecks_head_after_index_side_effect(self):
        for fail_index in (False, True):
            with self.subTest(fail_index=fail_index):
                identity = 'replay-failure' if fail_index else 'replay-success'
                self.s.create_memory(memory_id=identity, title='Replay', body='old')
                revision = self.s.memory_revision(identity)
                fields = {'body': 'updated'}
                self.s.update_memory(identity, expected_revision=revision, patch=fields)
                path = self.s.vault.memory_path(identity)
                def changed():
                    value = Memory.from_markdown(path.read_text()).to_dict()
                    value['body'] = 'later direct edit'
                    path.write_text(Memory.from_mapping(value).to_markdown(), encoding='utf-8')
                    if fail_index:
                        raise OSError('index failed after later edit')
                with patch.object(self.s, '_rebuild_index_unlocked', side_effect=changed):
                    if fail_index:
                        with self.assertRaises(MemoryUpdateCommitError) as caught:
                            self.s.update_memory(identity, expected_revision=revision, patch=fields)
                        self.assertIsNone(caught.exception.applied)
                    else:
                        with self.assertRaises(MemoryVersionError):
                            self.s.update_memory(identity, expected_revision=revision, patch=fields)
                self.assertEqual(Memory.from_markdown(path.read_text()).body, 'later direct edit')

    def test_index_error_after_newer_edit_reports_unknown_not_applied(self):
        def changed():
            value=self.head().to_dict();value['body']='independent later edit'
            self.s.vault.memory_path('m').write_text(Memory.from_mapping(value).to_markdown())
            raise OSError('index interruption after later edit')
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=changed):
            with self.assertRaises(MemoryUpdateCommitError) as caught:
                self.call({'status':'completed'})
        self.assertIsNone(caught.exception.applied)
        self.assertEqual(self.head().body,'independent later edit')
        self.assertTrue(self.journal().exists())

    def test_mcp_projection_keeps_pending_count_but_not_internal_payload(self):
        from memleaf.mcp_server import _query_metadata
        value={'status':'pending','scope':'vault','pending_explicit_mutations':1,'private':'do not expose'}
        result=_query_metadata({'pipeline_status':value})['pipeline_status']
        self.assertEqual(result['pending_explicit_mutations'],1)
        self.assertNotIn('private',result)
        value['pending_explicit_mutations']=True
        with self.assertRaises(ValueError):_query_metadata({'pipeline_status':value})

    def test_capacity_rejects_new_journal_without_overwriting_current(self):
        before=self.files()
        with patch.object(update_module,'pending_explicit_mutations',return_value=({'updates':128,'retractions':0},{},set())):
            with self.assertRaisesRegex(ValueError,'capacity_exhausted'):
                self.call({'body':'new'})
        self.assertEqual(before,self.files())

    def test_invalid_new_request_does_not_erase_stale_pending_payload(self):
        self.interrupted('history')
        value=self.head().to_dict();value['body']='new external state'
        self.s.write_memory(value,overwrite=True)
        before=self.files()
        with self.assertRaisesRegex(ValueError,'completion_time_requires_completed'):
            self.call({'completed_at':'2026-09-18T10:00:00Z'},expected=self.s.memory_revision('m'))
        self.assertEqual(before,self.files())


from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output


class UnresolvedHistoryRetentionTests(IncrementalFixture):
    def test_partial_incremental_work_defers_destructive_history_cleanup(self):
        self.s.create_memory(memory_id='unrelated',title='Unrelated',body='old')
        self.s.update_memory('unrelated',expected_revision=self.s.memory_revision('unrelated'),patch={'body':'new'})
        result=self.s.run_incremental(source='hermes',session_id='s',turn_id='t',backend=Backend(output(
            self.create(), {'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'synthetic clarification'})))
        self.assertEqual(result['execution_status'],'completed_with_unresolved')
        before={str(p):p.read_bytes() for p in self.s.vault.list_markdown('history')}
        result=RetentionManager(self.s).maintain('2040-01-01T00:00:00Z')
        self.assertTrue(result['cleanup_deferred_for_recovery'])
        self.assertEqual(result['history_pruned'],0)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.s.vault.list_markdown('history')})


if __name__ == '__main__':
    unittest.main()
