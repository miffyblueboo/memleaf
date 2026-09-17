"""Durability boundaries for scope registration, including real process exit."""
from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf, Memory
from memleaf.config import save_config
from memleaf.incremental_commit import IncrementalCommitError
from memleaf.incremental_execution import IncrementalRunError
from memleaf.incremental_journal import load_work
from memleaf.locking import atomic_write_text


class ScopeRecoveryTests(IncrementalFixture):
    def request_new(self, *rows):
        return self.request(list(rows) or [self.create(scope='project:Atlas'), self.no_memory()], allow_new_scopes=True)

    def interrupt_config(self, req=None):
        req = req or self.request_new()
        with patch('memleaf.incremental_scopes.save_config', side_effect=OSError('test secret must not leak')):
            with self.assertRaises(IncrementalCommitError) as caught:
                self.s.apply_incremental(**req)
        return req, caught.exception.result

    def test_head_failure_never_registers(self):
        req = self.request_new()
        with patch('memleaf.memory_writer.MemoryWriter.write_frozen_unlocked', side_effect=OSError('fixture')):
            with self.assertRaises(IncrementalCommitError):
                self.s.apply_incremental(**req)
        self.assertFalse(self.s.vault.list_markdown('knowledge'))
        self.assertEqual(self.s.vault.config()['scopes'], {})
        final = self.s.apply_incremental(**req)
        self.assertEqual(final['execution_status'], 'completed')

    def test_config_failure_retains_head_and_resumes_same_identity(self):
        req, first = self.interrupt_config()
        mid = first['operations'][0]['memory_id']
        self.assertEqual(first['execution_status'], 'recovery_required')
        self.assertEqual(first['counts']['applied'], 1)
        self.assertNotIn('test secret', json.dumps(first))
        self.assertEqual(self.s.read(mid).scopes, ['project:Atlas'])
        self.assertEqual(self.s.vault.config()['scopes'], {})
        final = self.s.apply_incremental(**req)
        self.assertEqual(final['execution_status'], 'completed')
        self.assertEqual(final['operations'][0]['memory_id'], mid)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)
        self.assertFalse(self.hist())
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})

    def test_catalog_reads_committed_head_during_registration_failure(self):
        _, first = self.interrupt_config()
        with self.s.vault.lock():
            catalog = self.s._scope_catalog_entries_unlocked()
        self.assertIn('project:Atlas', [x['scope'] for x in catalog])
        self.assertEqual(first['operations'][0]['scope_registration']['state'], 'pending')

    def test_config_write_then_failure_is_idempotent(self):
        req = self.request_new(); calls = []
        def write_then_fail(path, value):
            calls.append(1); save_config(path, value); raise OSError('after replace')
        with patch('memleaf.incremental_scopes.save_config', side_effect=write_then_fail):
            with self.assertRaises(IncrementalCommitError):
                self.s.apply_incremental(**req)
        with patch('memleaf.incremental_scopes.save_config', wraps=save_config) as saver:
            result = self.s.apply_incremental(**req)
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(saver.call_count, 0)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)

    def test_unrelated_user_configuration_survives_resume(self):
        req, _ = self.interrupt_config()
        cfg = self.s.vault.config(); cfg['llm']['model'] = 'user-selected-model'; cfg['custom'] = ['preserve']
        save_config(self.s.vault.config_path, cfg)
        self.s.apply_incremental(**req)
        current = self.s.vault.config()
        self.assertEqual(current['llm']['model'], 'user-selected-model')
        self.assertEqual(current['custom'], ['preserve'])
        self.assertEqual(current['scopes'], {'project:Atlas': {}})

    def test_new_alias_conflict_after_head_is_visible_not_overwritten(self):
        req, first = self.interrupt_config()
        cfg = self.s.vault.config(); cfg['scopes'] = {'project:Beacon': {'aliases': ['Atlas']}}
        save_config(self.s.vault.config_path, cfg); before = self.s.vault.config_path.read_bytes()
        with self.assertRaises(IncrementalCommitError) as caught:
            self.s.apply_incremental(**req)
        self.assertEqual(self.s.vault.config_path.read_bytes(), before)
        self.assertEqual(caught.exception.result['operations'][0]['scope_error'], 'scope_registration_identity_changed')
        self.assertEqual(caught.exception.result['counts']['applied'], 1)
        # An explicit config correction, not another model call, resolves it.
        cfg['scopes'] = {}; save_config(self.s.vault.config_path, cfg)
        self.assertEqual(self.s.apply_incremental(**req)['execution_status'], 'completed')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)

    def test_existing_new_node_metadata_is_preserved_on_resume(self):
        req, first = self.interrupt_config()
        cfg = self.s.vault.config(); cfg['scopes'] = {'project:Atlas': {'aliases': ['A'], 'paths': ['/user/path']}}
        save_config(self.s.vault.config_path, cfg)
        final = self.s.apply_incremental(**req)
        self.assertEqual(final['counts']['applied'], 1)
        self.assertEqual(self.s.vault.config()['scopes']['project:Atlas']['paths'], ['/user/path'])
        self.assertEqual(final['counts']['no_memory'], 1)

    def test_source_revision_after_applied_head_never_rewrites_it(self):
        req, first = self.interrupt_config(); mid = first['operations'][0]['memory_id']
        self.revise()
        result = self.s.resume_incremental(first['work_id'])
        self.assertEqual(result['counts']['committed'], 1)
        self.assertEqual(self.s.read(mid).body, 'Deliver the report.')
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})
        self.assertFalse(self.hist())

    def test_forget_cancels_pending_registration(self):
        _, first = self.interrupt_config(); mid = first['operations'][0]['memory_id']
        self.s.forget_memory(mid)
        final = self.s.resume_incremental(first['work_id'])
        self.assertFalse(self.s.vault.list_markdown('knowledge'))
        self.assertEqual(self.s.vault.config()['scopes'], {})
        op = next(op for op in final['operations'] if op.get('memory_id') == mid)
        self.assertEqual(op['state'], 'cancelled')
        self.assertEqual(op['scope_registration']['state'], 'cancelled')

    def test_removed_head_does_not_create_orphan_scope_on_recovery(self):
        _, first = self.interrupt_config(); mid = first['operations'][0]['memory_id']
        self.s.vault.memory_path(mid).unlink()
        self.s.resume_incremental(first['work_id'])
        self.assertEqual(self.s.vault.config()['scopes'], {})
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_rescoped_head_does_not_recreate_old_project(self):
        _, first = self.interrupt_config(); mid = first['operations'][0]['memory_id']
        path = self.s.vault.memory_path(mid); m = Memory.from_markdown(path.read_text()); m.scopes = ['global']
        atomic_write_text(path, m.to_markdown())
        self.s.resume_incremental(first['work_id'])
        self.assertEqual(self.s.vault.config()['scopes'], {})
        self.assertEqual(self.s.read(mid).scopes, ['global'])

    def test_scoped_update_preserves_history_and_deadline(self):
        self.target()
        req = self.request_new(self.update(scope='project:Atlas', status='completed'), self.no_memory())
        _, first = self.interrupt_config(req)
        self.assertEqual(len(self.hist()), 1)
        final = self.s.resume_incremental(first['work_id'])
        self.assertEqual(final['execution_status'], 'completed')
        self.assertEqual(len(self.hist()), 1)
        current = self.s.read('mem-old')
        self.assertEqual(current.due_date, '2026-09-20')
        self.assertEqual(current.status, 'completed')
        self.assertEqual(current.scopes, ['project:Atlas'])

    def test_retraction_does_not_register_missing_project(self):
        self.target(scopes=['project:Atlas'])
        result = self.s.apply_incremental(**self.request_new(self.update(validity='retracted'), self.no_memory()))
        self.assertEqual(result['counts']['committed'], 1)
        self.assertEqual(self.s.vault.config()['scopes'], {})

    def test_bad_target_group_does_not_create_any_registry_node(self):
        self.target()
        rows = [self.update(scope='project:Atlas'), self.update(status='invalid'), self.no_memory()]
        result = self.s.apply_incremental(**self.request_new(*rows))
        self.assertEqual(result['counts']['committed'], 0)
        self.assertEqual(result['counts']['no_memory'], 1)
        self.assertEqual(self.s.vault.config()['scopes'], {})

    def test_pending_write_is_fenced_by_registry_changes(self):
        req = self.request_new()
        with patch('memleaf.memory_writer.MemoryWriter.write_frozen_unlocked', side_effect=OSError('fixture')):
            with self.assertRaises(IncrementalCommitError) as caught:
                self.s.apply_incremental(**req)
        cfg = self.s.vault.config(); cfg['scopes'] = {'project:Beacon': {}}
        save_config(self.s.vault.config_path, cfg)
        result = self.s.resume_incremental(caught.exception.result['work_id'])
        self.assertEqual(result['counts']['committed'], 0)
        self.assertEqual(result['counts']['no_memory'], 1)
        self.assertFalse(self.s.vault.list_markdown('knowledge'))
        self.assertEqual(result['operations'][0]['code'], 'scope_registry_changed')

    def test_configured_runner_resumes_registration_without_model(self):
        b = Backend(output(self.create(scope='project:Atlas'), self.no_memory()))
        with patch('memleaf.incremental_scopes.save_config', side_effect=OSError('fixture')):
            with self.assertRaises(IncrementalRunError) as caught:
                self.s.process_incremental(source='hermes', session_id='s', turn_id='t', allow_new_scopes=True, model=b)
        self.s = Memleaf(self.s.vault.root)
        result = self.s.resume_incremental_run(caught.exception.result['run_id'])
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(result['model_calls_this_invocation'], 0)
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})

    def test_own_registration_is_not_new_context_for_replan(self):
        b = Backend(output(self.create(scope='project:Atlas'), {'action':'DEFERRED','evidence':['e2'],
                       'reason':'missing_context','need':'More context'}))
        result = self.s.run_incremental(source='hermes', session_id='s', turn_id='t', allow_new_scopes=True, backend=b)
        second = Backend(output(self.no_memory()))
        after = self.s.recover_incremental_partial(result['run_id'], model=second)
        self.assertEqual(after['partial_recovery_code'], 'context_unchanged')
        self.assertEqual(second.calls, [])

    def test_new_scope_partial_replan_preserves_parent_operation(self):
        b = Backend(output(self.create(scope='project:Atlas'), {'action':'DEFERRED','evidence':['e2'],
                       'reason':'missing_context','need':'More context'}))
        first = self.s.run_incremental(source='hermes', session_id='s', turn_id='t', allow_new_scopes=True, backend=b)
        parent = load_work(self.ledger(), first['commit_work_id'])
        self.capture('t2', content='The other statement is a separate Beacon obligation', seq=3)
        row = self.create(scope='project:Beacon', title='Second', body='Second distinct obligation'); row['evidence'] = ['e2']
        final = self.s.recover_incremental_partial(first['run_id'], model=Backend(output(row)))
        self.assertEqual(final['execution_status'], 'completed')
        self.assertEqual(final['reserved_requests'], 2)
        self.assertEqual(final['commit']['counts']['committed'], 2)
        self.assertEqual(load_work(self.ledger(), first['commit_work_id']), parent)
        self.assertEqual(set(self.s.vault.config()['scopes']), {'project:Atlas', 'project:Beacon'})

    def test_local_repair_preserves_discovery_flag(self):
        row = self.create(scope='project:Atlas'); row['evidence'] = 'e1'
        first = self.s.run_incremental(source='hermes', session_id='s', turn_id='t', allow_new_scopes=True,
                                       backend=Backend(output(row, self.no_memory())))
        final = self.s.recover_incremental_partial(first['run_id'], mode='repair')
        self.assertEqual(final['execution_status'], 'completed')
        self.assertEqual(final['model_calls_this_invocation'], 0)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})

    def test_changed_registry_cannot_be_used_for_structure_only_repair(self):
        row = self.create(scope='project:Atlas'); row['evidence'] = 'e1'
        first = self.s.run_incremental(source='hermes', session_id='s', turn_id='t', allow_new_scopes=True,
                                       backend=Backend(output(row, self.no_memory())))
        cfg = self.s.vault.config(); cfg['scopes'] = {'project:Beacon': {'aliases': ['Atlas']}}
        save_config(self.s.vault.config_path, cfg)
        with self.assertRaisesRegex(ValueError, 'partial_scope_context_changed'):
            self.s.recover_incremental_partial(first['run_id'], mode='repair')
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_true_process_exit_after_config_write_recovers(self):
        req = self.request_new()
        script = '''import json, os, sys
from unittest.mock import patch
from memleaf import Memleaf
from memleaf.config import save_config
s=Memleaf(sys.argv[1])
def stop(path, config):
    save_config(path, config)
    os._exit(37)
with patch('memleaf.incremental_scopes.save_config', side_effect=stop):
    s.apply_incremental(**json.loads(sys.argv[2]))
'''
        run = subprocess.run([sys.executable, '-c', script, str(self.s.vault.root), json.dumps(req)],
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 37, run.stderr)
        self.s = Memleaf(self.s.vault.root)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})
        final = self.s.apply_incremental(**req)
        self.assertEqual(final['execution_status'], 'completed')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)
        self.assertFalse(self.hist())

    def test_old_unguarded_completed_receipt_stays_readable(self):
        from memleaf.incremental_journal import save_work
        req = self.request_new(self.no_memory('e1'), self.no_memory())
        final = self.s.apply_incremental(**req)
        processed = self.ledger(); old = load_work(processed, final['work_id']); old.pop('scope_guard'); old['version'] = 1
        with self.s.vault.lock():
            save_work(self.s, processed, old)
        self.assertEqual(self.s.resume_incremental(final['work_id']), final)

    def test_same_request_two_processes_does_not_register_or_write_twice(self):
        req = self.request_new()
        script = '''import json, sys
from memleaf import Memleaf
result=Memleaf(sys.argv[1]).apply_incremental(**json.loads(sys.argv[2]))
print(json.dumps(result))
'''
        argv = [sys.executable, '-c', script, str(self.s.vault.root), json.dumps(req)]
        children = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})

    def test_config_edit_between_merge_and_replace_is_not_lost(self):
        from memleaf.scope_state import register_scope_nodes
        req = self.request_new()
        def edit_config(config, scopes):
            current = self.s.vault.config(); current['custom_user_value'] = 'new-value'
            save_config(self.s.vault.config_path, current)
            return register_scope_nodes(config, scopes)
        with patch('memleaf.incremental_scopes.register_scope_nodes', side_effect=edit_config):
            with self.assertRaises(IncrementalCommitError) as caught:
                self.s.apply_incremental(**req)
        self.assertEqual(self.s.vault.config()['custom_user_value'], 'new-value')
        self.assertEqual(self.s.vault.config()['scopes'], {})
        self.assertEqual(caught.exception.result['operations'][0]['scope_error'], 'scope_config_changed_during_merge')
        self.assertEqual(self.s.apply_incremental(**req)['execution_status'], 'completed')
        self.assertEqual(self.s.vault.config()['custom_user_value'], 'new-value')

    def test_new_receipt_requires_scope_guard_and_cannot_use_old_version(self):
        from memleaf.incremental_journal import save_work
        req, result = self.interrupt_config()
        for version, remove_guard in ((1, False), (2, True)):
            with self.subTest(version=version):
                processed = self.ledger(); work = load_work(processed, result['work_id'])
                work['version'] = version
                if remove_guard:
                    work.pop('scope_guard')
                before = self.s.vault.processed_state_path.read_bytes()
                with self.s.vault.lock(), self.assertRaises(ValueError):
                    save_work(self.s, processed, work)
                self.assertEqual(before, self.s.vault.processed_state_path.read_bytes())

    def test_receipt_write_failure_after_registration_keeps_same_head(self):
        from memleaf.incremental_journal import save_work
        req = self.request_new(); failed = []
        def fail_settlement(service, processed, work):
            op = work['operations'][0]
            if not failed and op['state'] == 'settled' and 'scope_registration' in op:
                failed.append(True)
                raise OSError('receipt after scope')
            return save_work(service, processed, work)
        with patch('memleaf.incremental_commit.save_work', side_effect=fail_settlement):
            with self.assertRaises(IncrementalCommitError) as caught:
                self.s.apply_incremental(**req)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})
        final = self.s.resume_incremental(caught.exception.result['work_id'])
        self.assertEqual(final['execution_status'], 'completed')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)
        self.assertFalse(self.hist())
