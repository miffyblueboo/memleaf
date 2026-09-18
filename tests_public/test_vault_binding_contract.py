"""Local ownership metadata is explicit, persistent and never inferred from prose."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from memleaf import Memleaf
from memleaf.locking import atomic_write_json
from memleaf.vault import Vault


class VaultBindingContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.s=Memleaf.initialize(self.root/'vault')

    def unbind_fixture(self):
        value=json.loads(self.s.vault.state_layout_path.read_text());value.pop('binding')
        atomic_write_json(self.s.vault.state_layout_path,value)

    def test_new_vault_and_two_reopened_agents_share_stable_binding(self):
        one=self.s.vault.identity_status()
        self.assertEqual(one['status'],'bound')
        self.assertEqual(one,Memleaf(self.s.vault.root).vault.identity_status())
        self.assertEqual(one,Vault(self.s.vault.root,create=False).identity_status())

    def test_distinct_vaults_do_not_merge_same_named_people(self):
        other=Memleaf.initialize(self.root/'other')
        one,two=self.s.vault.identity_status(),other.vault.identity_status()
        self.assertNotEqual(one['principal_id'],two['principal_id'])
        self.assertNotEqual(one['vault_id'],two['vault_id'])

    def test_identity_is_not_rekeyed_by_moving_a_complete_vault(self):
        identity=self.s.vault.identity_status();moved=self.root/'moved'
        shutil.copytree(self.s.vault.root,moved)
        self.assertEqual(Memleaf(moved).vault.identity_status(),identity)

    def test_old_unbound_vault_requires_explicit_action_not_automatic_guess(self):
        self.unbind_fixture();before=self.s.vault.processed_state_path.read_bytes()
        opened=Memleaf(self.s.vault.root)
        self.assertEqual(opened.vault.identity_status()['status'],'legacy_unbound')
        self.assertIn('legacy_vault_requires_explicit_binding',opened.migration_preflight()['blockers'])
        bound=opened.vault.bind_identity('trusted-local-owner')
        self.assertEqual(bound['principal_id'],'trusted-local-owner')
        self.assertEqual(before,opened.vault.processed_state_path.read_bytes())
        self.assertEqual(bound,opened.vault.bind_identity('trusted-local-owner'))

    def test_rebinding_is_not_a_silent_user_or_work_migration(self):
        before=self.s.vault.state_layout_path.read_bytes()
        with self.assertRaises(ValueError):self.s.vault.bind_identity('another-principal')
        self.assertEqual(before,self.s.vault.state_layout_path.read_bytes())

    def test_pending_source_prevents_binding_an_old_vault(self):
        self.unbind_fixture()
        self.s.capture('hermes','s','t','user','Pending statement')
        with self.assertRaisesRegex(ValueError,'pending migration'):
            self.s.vault.bind_identity('trusted-owner')
        self.assertEqual(self.s.vault.identity_status()['status'],'legacy_unbound')

    def test_malformed_binding_is_preserved_and_reported(self):
        value=json.loads(self.s.vault.state_layout_path.read_text());value['binding']['principal_id']={}
        atomic_write_json(self.s.vault.state_layout_path,value);before=self.s.vault.state_layout_path.read_bytes()
        with self.assertRaises((ValueError,RuntimeError)):self.s.vault.identity_status()
        self.assertIn('invalid_vault_binding',self.s.migration_preflight()['blockers'])
        self.assertEqual(before,self.s.vault.state_layout_path.read_bytes())

    def test_trusted_source_namespaces_do_not_collide_on_session_message_ids(self):
        for namespace in ('hermes-work','hermes-personal','codex'):
            result=self.s.capture(namespace,'same-session','same-turn','user',namespace,
                                  message_id='same-message',message_revision='1')
            self.assertTrue(result.stored)
        files=self.s.vault.list_markdown('inbox')
        self.assertEqual(len(files),3)
        state=json.loads(self.s.vault.processed_state_path.read_text())
        self.assertEqual(len(state['events']),3)

    def test_two_concurrent_first_initializations_do_not_rebind(self):
        target=self.root/'concurrent';barrier=Barrier(2)
        def init(_):
            barrier.wait();return Memleaf.initialize(target).vault.identity_status()
        with ThreadPoolExecutor(2) as executor:result=list(executor.map(init,range(2)))
        self.assertEqual(result[0],result[1])

    def test_binding_fences_identical_inputs_without_sending_owner_ids_to_model(self):
        other=Memleaf.initialize(self.root/'other')
        previews=[]
        for service in (self.s,other):
            service.capture('hermes','s','t','user','The same observation.',message_id='u',source_time='2026-09-18T08:00:00Z')
            service.capture('hermes','s','t','assistant','Acknowledged.',message_id='a',final=True)
            previews.append(service.preview_incremental(source='hermes',session_id='s',turn_id='t'))
        self.assertNotEqual(previews[0]['snapshot_id'],previews[1]['snapshot_id'])
        self.assertEqual(previews[0]['request'],previews[1]['request'])
