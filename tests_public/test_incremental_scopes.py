"""Scope contract tests use only synthetic local Vaults; no model calls."""
from __future__ import annotations

import json
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf.config import save_config
from memleaf.incremental_recovery import restore_snapshot
from memleaf.incremental_scopes import MAX_SCOPES, validate_guard


class ScopeContractTests(IncrementalFixture):
    def config(self, nodes):
        value = self.s.vault.config(); value['scopes'] = nodes
        save_config(self.s.vault.config_path, value)

    def preview(self, **kw):
        return self.s.preview_incremental(source='hermes', session_id='s', turn_id='t', **kw)

    def request_new(self, *rows, **kw):
        return self.request(list(rows) or [self.create(scope='project:Atlas'), self.no_memory()],
                            allow_new_scopes=True, **kw)

    def test_preview_does_not_register(self):
        before = self.s.vault.config_path.read_bytes()
        req = self.request_new()
        self.assertEqual(before, self.s.vault.config_path.read_bytes())
        self.assertFalse(self.s.vault.list_markdown('knowledge'))
        self.assertTrue(req['allow_new_scopes'])

    def test_new_scope_commit_and_receipt_replay(self):
        req = self.request_new(); result = self.s.apply_incremental(**req)
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})
        self.assertEqual(result['operations'][0]['scope_registration']['state'], 'current')
        again = self.s.apply_incremental(**req)
        self.assertEqual(again, result)
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)
        self.assertFalse(self.hist())

    def test_new_scope_permission_defaults_off(self):
        result = self.s.apply_incremental(**self.request([self.create(scope='project:Atlas'), self.no_memory()]))
        self.assertEqual(result['coverage_status'], 'partial')
        self.assertEqual(self.s.vault.config()['scopes'], {})
        self.assertFalse(self.s.vault.list_markdown('knowledge'))
        self.assertEqual(result['counts']['no_memory'], 1)

    def test_explicit_named_boundary_authorizes_that_name_only(self):
        result = self.s.apply_incremental(**self.request([self.create(scope='s1'), self.no_memory()], scope='project:Atlas'))
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})

    def test_discovery_flag_does_not_expand_explicit_boundary(self):
        result = self.s.apply_incremental(**self.request_new(self.create(scope='project:Beacon'), self.no_memory(), scope='project:Atlas'))
        self.assertEqual(result['counts']['committed'], 0)
        self.assertEqual(result['counts']['no_memory'], 1)
        self.assertEqual(self.s.vault.config()['scopes'], {})

    def test_known_empty_project_is_available_without_memory_target(self):
        self.config({'project:Atlas': {}})
        view = json.loads(self.preview()['request']['user'])
        self.assertEqual(view['scopes'], {'s1': 'project:Atlas'})
        self.assertEqual(view['memories'], [])
        result = self.s.apply_incremental(**self.request([self.create(scope='s1'), self.no_memory()]))
        self.assertEqual(result['execution_status'], 'completed')

    def test_exact_registered_alias_reuses_canonical_scope(self):
        self.config({'project:Atlas': {'aliases': ['Orion']}})
        req = self.request_new(self.create(scope='project:Orion'), self.no_memory())
        result = self.s.apply_incremental(**req)
        self.assertEqual(self.s.read(result['operations'][0]['memory_id']).scopes, ['project:Atlas'])
        self.assertNotIn('project:Orion', self.s.vault.config()['scopes'])

    def test_explicit_alias_boundary_is_canonical_and_not_widened(self):
        self.config({'project:Atlas': {'aliases': ['Orion']}})
        view = json.loads(self.preview(scope='project:Orion')['request']['user'])
        self.assertEqual(view['write_scopes'], ['project:Atlas'])
        result = self.s.apply_incremental(**self.request([self.create(scope='s1'), self.no_memory()], scope='project:Orion'))
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(set(self.s.vault.config()['scopes']), {'project:Atlas'})

    def test_ambiguous_alias_rejected_without_creating_node(self):
        self.config({'project:Atlas': {'aliases': ['Example']}, 'project:Beacon': {'aliases': ['Example']}})
        result = self.s.apply_incremental(**self.request_new(self.create(scope='project:Example'), self.no_memory()))
        self.assertEqual(result['counts']['committed'], 0)
        self.assertEqual(result['counts']['no_memory'], 1)
        self.assertNotIn('project:Example', self.s.vault.config()['scopes'])

    def test_similar_names_are_not_fuzzy_merged(self):
        self.config({'project:Atlas': {}})
        result = self.s.apply_incremental(**self.request_new(self.create(scope='project:Atlass'), self.no_memory()))
        self.assertEqual(self.s.read(result['operations'][0]['memory_id']).scopes, ['project:Atlass'])
        self.assertEqual(set(self.s.vault.config()['scopes']), {'project:Atlas', 'project:Atlass'})

    def test_case_variant_uses_exact_known_identity(self):
        self.config({'project:Atlas': {}})
        result = self.s.apply_incremental(**self.request([self.create(scope='project:atlas'), self.no_memory()]))
        self.assertEqual(self.s.read(result['operations'][0]['memory_id']).scopes, ['project:Atlas'])

    def test_non_project_discovery_and_special_nodes(self):
        rows = [self.create(scope='domain:Finance'), self.no_memory()]
        result = self.s.apply_incremental(**self.request_new(*rows))
        self.assertEqual(result['counts']['committed'], 0)
        self.assertEqual(self.s.vault.config()['scopes'], {})

    def test_global_and_unscoped_never_registered(self):
        rows = [self.create(), self.create(scope='unscoped', title='Other', body='A separate target'), self.no_memory()]
        result = self.s.apply_incremental(**self.request_new(*rows))
        self.assertEqual(result['counts']['committed'], 2)
        self.assertEqual(self.s.vault.config()['scopes'], {})

    def test_two_independent_heads_register_same_scope_once(self):
        req = self.request_new(self.create(scope='project:Atlas'), self.create(scope='project:Atlas', title='Second', body='Second independent obligation'), self.no_memory())
        with patch('memleaf.incremental_scopes.save_config', wraps=save_config) as saver:
            result = self.s.apply_incremental(**req)
        self.assertEqual(result['counts']['committed'], 2)
        self.assertEqual(saver.call_count, 1)

    def test_unrelated_success_survives_invalid_scope_candidate(self):
        rows = [self.create(scope='project:Atlas'), self.create(scope='domain:Unknown'), self.no_memory()]
        result = self.s.apply_incremental(**self.request_new(*rows))
        self.assertEqual(result['counts']['committed'], 1)
        self.assertEqual(result['counts']['no_memory'], 1)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})

    def test_registration_only_for_successful_memory_not_discarded_scope(self):
        req = self.request_new(self.no_memory('e1'), self.no_memory(), scope='project:NeverCreated')
        result = self.s.apply_incremental(**req)
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(self.s.vault.config()['scopes'], {})

    def test_alias_edit_invalidates_model_snapshot(self):
        self.config({'project:Atlas': {'aliases': ['A']}})
        req = self.request_new()
        self.config({'project:Atlas': {'aliases': ['B']}})
        with self.assertRaisesRegex(ValueError, 'stale_planning_snapshot'):
            self.s.apply_incremental(**req)
        self.assertFalse(self.works())

    def test_config_route_change_does_not_invalidate_scope_snapshot(self):
        req = self.request_new()
        cfg = self.s.vault.config(); cfg['custom_user_setting'] = {'keep': True}
        save_config(self.s.vault.config_path, cfg)
        result = self.s.apply_incremental(**req)
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(self.s.vault.config()['custom_user_setting'], {'keep': True})

    def test_scope_guard_does_not_send_paths_or_identifiers(self):
        self.config({'project:Atlas': {'aliases': ['Orion'], 'paths': ['/private/project'], 'identifiers': ['example.test']}})
        request = self.preview()['request']
        self.assertNotIn('/private/project', request['user'])
        self.assertNotIn('example.test', request['user'])
        self.assertNotIn('scope_guard', request['user'])
        self.assertEqual(json.loads(request['user'])['scope_aliases'], {'s1': ['Orion']})

    def test_registry_cap_fails_before_model(self):
        self.config({f'project:P{i}': {} for i in range(MAX_SCOPES + 1)})
        backend = Backend(output(self.create(), self.no_memory()))
        with self.assertRaisesRegex(ValueError, 'scope_catalog_limit'):
            self.s.process_incremental(source='hermes', session_id='s', turn_id='t', model=backend, allow_new_scopes=True)
        self.assertEqual(backend.calls, [])

    def test_invalid_guard_types_are_controlled_errors(self):
        for value in (None, [], {'version': True, 'nodes': {}}, {'version': 1, 'nodes': {'global': '0'*64}}, {'version': 1, 'nodes': {'project:A': []}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_guard(value)

    def test_guard_and_aliases_round_trip_through_partial_seed(self):
        from memleaf.incremental_preview import _prepare_incremental_unlocked
        self.config({'project:Atlas': {'aliases': ['A']}})
        with self.s.vault.lock():
            snap = _prepare_incremental_unlocked(self.s, source='hermes', session_id='s', turn_id='t', allow_new_scopes=True)
        rebuilt = restore_snapshot(snap.state())
        self.assertEqual(snap.snapshot_id, rebuilt.snapshot_id)
        self.assertEqual(snap.model_input(), rebuilt.model_input())

    def test_configured_and_direct_runners_forward_discovery_without_extra_call(self):
        backend = Backend(output(self.create(scope='project:Atlas'), self.no_memory()))
        result = self.s.process_incremental(source='hermes', session_id='s', turn_id='t', model=backend, allow_new_scopes=True)
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})

    def test_intent_cannot_change_discovery_after_freeze(self):
        from memleaf.llm.base import ModelError
        backend = Backend(ModelError('network failure', code='model_network_error'))
        first = self.s.process_incremental(source='hermes', session_id='s', turn_id='t', model=backend)
        self.assertEqual(first['execution_status'], 'retryable')
        with self.assertRaisesRegex(ValueError, 'arguments_changed'):
            self.s.process_incremental(source='hermes', session_id='s', turn_id='t', model=Backend(output(self.create())), allow_new_scopes=True, recover=True)

    def test_selected_retention_can_register_with_same_shared_runner(self):
        ref = self.preview()['source_refs'][0]['source_ref']
        backend = Backend(output(self.create(scope='project:Atlas')))
        result = self.s.remember_incremental(source='hermes', session_id='s', turn_id='t', intent_id='selected-1',
            selected_source_refs=[ref], retention_request='Keep this task', model=backend, allow_new_scopes=True)
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(self.s.vault.config()['scopes'], {'project:Atlas': {}})
