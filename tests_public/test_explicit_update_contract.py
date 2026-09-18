"""Trusted exact edits; same Core writer, bounded journals and zero model calls."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf, Memory
from memleaf.memory_writer import MemoryWriter
from memleaf.models import MemoryVersionError
from memleaf.retention import RetentionManager
from memleaf.query_progress import observe_progress
from memleaf.config import save_config


class ExplicitUpdateContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Memleaf.initialize(Path(self.tmp.name)/'vault')
        self.old=self.s.create_memory(memory_id='mem-target',title='Atlas report',body='Deliver a report.',
             type='todo',status='active',scopes=['global'],assignee='owner',due_date='2026-10-20',
             custom={'keep':['中文']},created='2026-01-01T00:00:00Z')
        self.rev=self.s.memory_revision(self.old.memory_id)

    def edit(self, changes=None, **kwargs):
        args=dict(expected_revision=self.rev,patch=changes or {'body':'Revised report.'},authorized_scopes=['global'])
        args.update(kwargs)
        return self.s.update_memory('mem-target',**args)

    def head(self):
        return Memory.from_markdown(self.s.vault.memory_path('mem-target').read_text())

    def journal(self):
        return self.s.vault.state_path/'explicit_updates/mem-target.json'

    def test_edit_preserves_identity_unspecified_fields_and_original_history(self):
        result=self.edit({'status':'completed'})
        self.assertEqual(result['action'],'UPDATE'); self.assertEqual(result['model_calls'],0)
        current=self.head()
        self.assertEqual(current.memory_id,self.old.memory_id)
        self.assertEqual(current.body,self.old.body);self.assertEqual(current.created,self.old.created)
        self.assertEqual(current.due_date,self.old.due_date);self.assertEqual(current.extra['custom'],self.old.extra['custom'])
        self.assertEqual(current.status,'completed');self.assertIsNone(current.completed_at)
        history=Memory.from_markdown(self.s.vault.list_markdown('history')[0].read_text())
        self.assertEqual(history.status,'active');self.assertEqual(history.body,self.old.body)

    def test_identical_patch_is_no_change_and_does_not_generate_history(self):
        before=self.s.vault.memory_path('mem-target').read_bytes()
        result=self.edit({'status':'active'})
        self.assertEqual(result['action'],'NO_CHANGE')
        self.assertEqual(before,self.s.vault.memory_path('mem-target').read_bytes())
        self.assertEqual(self.s.vault.list_markdown('history'),[])

    def test_stale_revision_cannot_overwrite_later_edit(self):
        self.edit();before=self.s.vault.memory_path('mem-target').read_bytes()
        with self.assertRaises(MemoryVersionError):self.edit({'status':'completed'})
        self.assertEqual(before,self.s.vault.memory_path('mem-target').read_bytes())

    def test_missing_target_does_not_turn_into_create(self):
        self.s.forget_memory('mem-target')
        with self.assertRaises(ValueError):self.edit()
        self.assertEqual(self.s.vault.list_markdown('knowledge'),[])

    def test_same_request_replay_has_no_second_history(self):
        result=self.edit();replay=self.edit()
        self.assertEqual(result['revision'],replay['revision']);self.assertTrue(replay['replayed'])
        self.assertEqual(len(self.s.vault.list_markdown('history')),1)

    def test_reopen_requires_explicit_flag(self):
        self.edit({'status':'completed'});self.rev=self.s.memory_revision('mem-target')
        with self.assertRaises(ValueError):self.edit({'status':'active'})
        self.edit({'status':'active'},reopen=True)
        self.assertEqual(self.head().status,'active')

    def test_restore_requires_new_body_and_explicit_flag(self):
        self.edit({'validity':'retracted'});self.rev=self.s.memory_revision('mem-target')
        self.assertIsNone(self.s.read('mem-target'))
        with self.assertRaises(ValueError):self.edit({'validity':'valid','body':'Restated current assertion.'})
        self.edit({'validity':'valid','body':'Restated current assertion.'},restore=True)
        self.assertEqual(self.head().validity,'valid')

    def test_null_clears_only_requested_nullable_fields(self):
        self.edit({'due_date':None,'due_text':None,'assignee':None})
        current=self.head()
        self.assertIsNone(current.due_date);self.assertIsNone(current.extra['assignee'])
        self.assertEqual(current.extra['due_status'],'cleared');self.assertEqual(current.body,self.old.body)

    def test_protected_fields_and_invalid_values_do_not_write(self):
        before=self.s.vault.memory_path('mem-target').read_bytes()
        for value in ({'memory_id':'other'},{'sources':[]},{'field_basis':{}},{'body':None},
                      {'status':'unknown'},{'completed_at':'not-a-time'},{'due_date':'2026-02-30'},
                      {'title':'\x00'},{'scopes':[]},{'type':'fact'}):
            with self.subTest(value=value),self.assertRaises((ValueError,TypeError)):
                self.edit(value)
        self.assertEqual(before,self.s.vault.memory_path('mem-target').read_bytes())

    def test_scope_correction_requires_both_existing_scopes(self):
        config=self.s.vault.config();config['scopes']={'project:Atlas':{},'project:Beacon':{}};save_config(self.s.vault.config_path,config)
        with self.assertRaises(ValueError):self.edit({'scopes':['project:Atlas']})
        self.edit({'scopes':['project:Atlas']},authorized_scopes=['global','project:Atlas'])
        self.rev=self.s.memory_revision('mem-target')
        with self.assertRaises(ValueError):self.edit({'scopes':['project:Beacon']},authorized_scopes=['project:Beacon'])
        self.edit({'scopes':['project:Beacon']},authorized_scopes=['project:Atlas','project:Beacon'])
        self.assertEqual(self.head().scopes,['project:Beacon'])
        self.assertEqual(len(self.s.vault.list_markdown('history')),2)

    def test_type_correction_is_explicit_and_clears_incompatible_fields(self):
        changes={'type':'fact','status':None,'due_date':None,'assignee':None}
        self.edit(changes,allow_type_change=True)
        self.assertEqual(self.head().type,'fact')
        self.assertIsNone(self.head().status)

    def test_index_failure_recovers_with_identical_operation_and_counters(self):
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index')):
            with self.assertRaises(OSError) as caught:self.edit()
        self.assertTrue(caught.exception.applied);self.assertTrue(self.journal().exists())
        self.assertEqual(observe_progress(self.s.vault)['status'],'pending')
        self.s.read('mem-target'); count=self.head().hit_count
        restarted=Memleaf(self.s.vault.root)
        result=restarted.update_memory('MEM-TARGET',expected_revision=self.rev,patch={'body':'Revised report.'},authorized_scopes=['global'])
        self.assertEqual(result['action'],'UPDATE');self.assertFalse(self.journal().exists())
        self.assertEqual(self.head().hit_count,count);self.assertEqual(len(self.s.vault.list_markdown('history')),1)

    def test_forget_cancels_pending_edit_payload_before_deletion(self):
        with patch.object(MemoryWriter,'_write_history',side_effect=OSError('history')):
            with self.assertRaises(OSError):self.edit()
        self.assertTrue(self.journal().exists())
        self.assertTrue(self.s.forget_memory('mem-target'))
        self.assertFalse(self.journal().exists())
        with self.assertRaises(ValueError):self.edit()
        self.assertEqual(self.s.vault.list_markdown('knowledge'),[])

    def test_newer_edit_wins_after_failed_prepared_update(self):
        with patch.object(MemoryWriter,'_write_history',side_effect=OSError('history')):
            with self.assertRaises(OSError):self.edit()
        current=self.head();current.body='Later external edit.'
        self.s.vault.memory_path(current.memory_id).write_text(current.to_markdown())
        with self.assertRaises(MemoryVersionError):self.edit()
        self.assertEqual(self.head().body,'Later external edit.');self.assertTrue(self.journal().exists())

    def test_corrupt_journal_not_reset_by_retry(self):
        with patch.object(MemoryWriter,'_write_history',side_effect=OSError('history')):
            with self.assertRaises(OSError):self.edit()
        self.journal().write_text('{}')
        with self.assertRaises(ValueError):self.edit()
        self.assertEqual(self.journal().read_text(),'{}')

    def test_pending_edit_blocks_retention_and_migration_switch(self):
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index')):
            with self.assertRaises(OSError):self.edit()
        self.assertEqual(RetentionManager(self.s).maintain('2099-01-01T00:00:00Z')['history_pruned'],0)
        self.assertIn('pending_explicit_updates',self.s.migration_preflight()['blockers'])

    def test_renamed_target_does_not_create_standard_path_copy(self):
        standard=self.s.vault.memory_path('mem-target');renamed=standard.with_name('renamed.md');standard.rename(renamed)
        self.edit()
        self.assertFalse(standard.exists());self.assertTrue(renamed.exists())

    def test_edit_during_history_write_keeps_later_content(self):
        original=MemoryWriter._write_history
        def intervene(writer,*a,**k):
            result=original(writer,*a,**k)
            value=self.head();value.body='Later concurrent content.'
            self.s.vault.memory_path('mem-target').write_text(value.to_markdown())
            return result
        with patch.object(MemoryWriter,'_write_history',intervene):
            with self.assertRaises(ValueError):self.edit()
        self.assertEqual(self.head().body,'Later concurrent content.')
        self.assertTrue(self.journal().exists())

    def test_changed_scope_registry_before_head_write_is_detected(self):
        original=MemoryWriter._write_history
        def intervene(writer,*args,**kwargs):
            result=original(writer,*args,**kwargs)
            config=self.s.vault.config();config['scopes']['project:Other']={};save_config(self.s.vault.config_path,config)
            return result
        with patch.object(MemoryWriter,'_write_history',intervene):
            with self.assertRaises(MemoryVersionError):self.edit()
        self.assertEqual(self.head().body,self.old.body)
        self.assertTrue(self.journal().exists())

    def test_applied_recovery_does_not_need_old_registry_to_rewrite_head(self):
        with patch.object(self.s,'_rebuild_index_unlocked',side_effect=OSError('index')):
            with self.assertRaises(OSError):self.edit()
        config=self.s.vault.config();config['scopes']['project:Other']={};save_config(self.s.vault.config_path,config)
        self.edit()
        self.assertEqual(self.head().body,'Revised report.')
        self.assertFalse(self.journal().exists())
        self.assertEqual(len(self.s.vault.list_markdown('history')),1)

    def test_checksums_alone_cannot_validate_an_out_of_patch_frozen_result(self):
        import hashlib
        from memleaf.turn_plan import revision_digest
        with patch.object(MemoryWriter,'_write_history',side_effect=OSError('history')):
            with self.assertRaises(OSError):self.edit()
        wrapper=json.loads(self.journal().read_text());plan=json.loads(wrapper['payload'])
        after=Memory.from_markdown(plan['after']);after.extra['unrequested']='not allowed'
        plan['after']=after.to_markdown();plan['replacement_revision']=revision_digest(after)
        wrapper['payload']=json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        wrapper['checksum']=hashlib.sha256(wrapper['payload'].encode()).hexdigest()
        self.journal().write_text(json.dumps(wrapper))
        before=self.s.vault.memory_path('mem-target').read_bytes()
        with self.assertRaises(ValueError):self.edit()
        self.assertEqual(before,self.s.vault.memory_path('mem-target').read_bytes())
