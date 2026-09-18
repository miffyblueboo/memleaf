"""Integration between divergent candidate contracts, not extra model review."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from candidate_update_fixtures import FIXTURES
from memleaf import Memleaf, Memory
from memleaf.config import save_config
from memleaf.incremental_protocol import basis_status
from memleaf.memory_update import pending_explicit_mutations, MemoryUpdateCommitError
from memleaf.memory_writer import MemoryWriter
from memleaf.models import MemoryVersionError
from memleaf.query_progress import observe_progress


class CandidateReconciliationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = Memleaf.initialize(Path(tmp.name) / "vault")

    def seed_legacy(self, branch, stage):
        case = next(c for c in FIXTURES if c['branch'] == branch and c['stage'] == stage)
        self.s.write_memory(Memory.from_markdown(case['head']))
        for name, text in case['history'].items():
            (self.s.vault.history_path / name).write_text(text, encoding='utf-8')
        path = self.s.vault.state_path / case['journal_directory'] / 'legacy-target.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(case['journal'], encoding='utf-8')
        return case, path

    def recover_legacy(self, branch, stage):
        case, path = self.seed_legacy(branch, stage)
        old = json.loads(json.loads(case['journal'])['payload'])
        self.assertEqual(pending_explicit_mutations(self.s.vault)[0]['updates'], 1)
        result = self.s.update_memory('LEGACY-TARGET', expected_revision=case['expected_revision'],
                                     patch=case['patch'], **case['kwargs'])
        self.assertEqual(result['operation_id'], old['operation_id'])
        self.assertEqual(result['revision'], old['replacement_revision'])
        self.assertFalse(path.exists())
        self.assertEqual(len(self.s.vault.list_markdown('history')), 1)
        head = self.s.read('legacy-target')
        self.assertEqual(head.status, 'completed')
        self.assertEqual(basis_status(head), 'verified_revision')
        replay = self.s.update_memory('legacy-target', expected_revision=case['expected_revision'],
                                     patch=case['patch'], **case['kwargs'])
        self.assertTrue(replay['already_applied'])
        self.assertEqual(replay['operation_id'], old['operation_id'])
        self.assertEqual(len(self.s.vault.list_markdown('history')), 1)

    def test_a_prepared_v1_recovers_with_original_operation(self):
        self.recover_legacy('A', 'prepared')

    def test_a_applied_v1_settles_without_duplicate_history(self):
        self.recover_legacy('A', 'head_applied')

    def test_b_prepared_v1_recovers_with_original_operation(self):
        self.recover_legacy('B', 'prepared')

    def test_b_applied_v1_settles_without_duplicate_history(self):
        self.recover_legacy('B', 'head_applied')

    def test_b_legacy_plan_is_pending_and_exact_forget_cancels_it(self):
        _, path = self.seed_legacy('B', 'prepared')
        before = path.read_bytes()
        self.assertEqual(observe_progress(self.s.vault)['pending_explicit_mutations'], 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.s.forget_memory('LEGACY-TARGET'))
        self.assertFalse(path.exists())
        self.assertEqual(self.s.vault.list_markdown('knowledge'), [])
        self.assertEqual(self.s.vault.list_markdown('history'), [])

    def test_two_old_formats_for_same_identity_block_without_choosing_winner(self):
        _, first = self.seed_legacy('A', 'prepared')
        old_b = next(c for c in FIXTURES if c['branch'] == 'B' and c['stage'] == 'prepared')
        second = self.s.vault.state_path / 'memory_updates' / 'legacy-target.json'
        second.parent.mkdir()
        second.write_text(old_b['journal'], encoding='utf-8')
        before = (first.read_bytes(), second.read_bytes())
        with self.assertRaisesRegex(ValueError, 'duplicate_explicit_mutation_identity'):
            pending_explicit_mutations(self.s.vault)
        self.assertEqual(before, (first.read_bytes(), second.read_bytes()))
        self.assertEqual(observe_progress(self.s.vault)['status'], 'unknown')

    def test_different_request_cannot_replace_legacy_prepared_payload(self):
        case, path = self.seed_legacy('B', 'prepared')
        before = path.read_bytes()
        with self.assertRaises(MemoryVersionError):
            self.s.update_memory('legacy-target', expected_revision=case['expected_revision'],
                                 patch={'body':'Different request.'}, **case['kwargs'])
        self.assertEqual(path.read_bytes(), before)

    def test_valid_new_current_edit_can_supersede_stale_legacy_payload(self):
        case, path = self.seed_legacy('B', 'prepared')
        head = Memory.from_markdown(case['before'])
        head.body = 'Later external content.'
        self.s.write_memory(head, overwrite=True)
        revision = self.s.memory_revision(head.memory_id)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.s.update_memory(head.memory_id, expected_revision=revision,
                                 patch={'completed_at':'2026-09-18T12:00:00Z'})
        self.assertEqual(path.read_bytes(), before)
        result = self.s.update_memory(head.memory_id, expected_revision=revision, patch={'waiting_on':'Reviewer'})
        self.assertEqual(result['action'], 'UPDATE')
        self.assertFalse(path.exists())
        self.assertEqual(self.s.read(head.memory_id).body, 'Later external content.')

    def fresh(self):
        self.s.create_memory(memory_id='m', title='Synthetic task', body='Deliver work.', type='todo', status='active')
        return self.s.memory_revision('m')

    def test_no_scope_argument_cannot_authorize_new_scope(self):
        revision = self.fresh()
        cfg = self.s.vault.config(); cfg['scopes']['project:Atlas'] = {}
        save_config(self.s.vault.config_path, cfg)
        before = self.s.vault.memory_path('m').read_bytes()
        with self.assertRaisesRegex(ValueError, 'blocked_scope'):
            self.s.update_memory('m', expected_revision=revision, patch={'scopes':['project:Atlas']})
        self.assertEqual(before, self.s.vault.memory_path('m').read_bytes())
        self.s.update_memory('m', expected_revision=revision, patch={'scopes':['project:Atlas']},
                             authorized_scopes=['global','project:Atlas'])
        self.assertEqual(self.s.read('m').scopes, ['project:Atlas'])

    def test_two_deadline_forms_cannot_silently_override_each_other(self):
        revision = self.fresh()
        with self.assertRaisesRegex(ValueError, 'ambiguous_deadline_patch'):
            self.s.update_memory('m', expected_revision=revision,
                patch={'due_date':'2026-10-01', 'deadline':{'text':'2026-11-01'}})
        self.assertEqual(self.s.vault.list_markdown('history'), [])

    def test_unknown_source_time_is_observed_not_invented(self):
        revision = self.fresh()
        self.s.update_memory('m', expected_revision=revision, patch={'assignee':'Reviewer'})
        basis = self.s.read('m').extra['field_basis']['responsibility']
        self.assertIn('observed_at', basis)
        self.assertNotIn('source_time', basis)

    def test_unchanged_status_in_patch_does_not_retime_its_basis(self):
        revision = self.fresh()
        self.s.update_memory('m', expected_revision=revision, patch={'status':'completed'},
                             source_time='2026-09-18T08:00:00Z')
        head = self.s.read('m'); before = head.extra['field_basis']['status']
        self.s.update_memory('m', expected_revision=self.s.memory_revision('m'),
                             patch={'body':'More precise goal.', 'status':'completed'})
        self.assertEqual(self.s.read('m').extra['field_basis']['status'], before)

    def test_old_b_reader_has_no_write_or_recovery_entrypoints(self):
        import memleaf.legacy_update_journal as module
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertNotIn('update_unlocked', methods)
        self.assertNotIn('cancel_unlocked', methods)
        self.assertNotIn('_freeze', methods)
        self.assertFalse(any(isinstance(n, ast.Name) and n.id in {'atomic_write_json','atomic_write_text','atomic_unlink'}
                             for n in ast.walk(tree)))
