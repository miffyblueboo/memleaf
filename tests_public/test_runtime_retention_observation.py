"""Post-write observation failures must preserve verified maintenance outcomes."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from unittest.mock import patch

from test_runtime_retention import RetentionFixture
from test_incremental_execution import Backend
from memleaf import runtime_retention as retention
from memleaf.cli import main
from memleaf import extraction_work_state as budgets


class RetentionObservationTests(RetentionFixture):
    def broken_readback(self, after_writes, exception):
        original_write, original_read = retention.atomic_write_json, retention._bytes
        writes = []

        def write(path, value, **kwargs):
            original_write(path, value, **kwargs)
            writes.append(path)

        def read(path):
            if len(writes) >= after_writes:
                raise exception
            return original_read(path)

        return writes, patch.object(retention, 'atomic_write_json', side_effect=write), patch.object(retention, '_bytes', side_effect=read)

    def check_observation_failure(self, after_writes, exception):
        self.execute(self.create(), self.no_memory())
        preview = self.s.compact_runtime_state()
        writes, writer, reader = self.broken_readback(after_writes, exception)
        with writer, reader:
            with self.assertRaises(retention.RuntimeRetentionError) as caught:
                self.s.compact_runtime_state(dry_run=False, expected_revision=preview['state_revision'])
        result = caught.exception.result
        self.assertEqual(result['execution_status'], 'interrupted')
        self.assertEqual(result['code'], 'runtime_state_observation_failed')
        self.assertEqual(result['applied_files'], ['processed', 'budget'][:after_writes])
        self.assertEqual(len(writes), after_writes)
        self.assertEqual(result['model_calls'], 0)
        return result

    def test_read_error_between_writes_preserves_first_outcome(self):
        self.check_observation_failure(1, OSError('private read failure'))

    def test_unsafe_control_between_writes_preserves_first_outcome(self):
        self.check_observation_failure(1, ValueError('unsafe_runtime_state'))

    def test_last_read_error_preserves_both_outcomes(self):
        self.check_observation_failure(2, OSError('private read failure'))

    def test_last_validation_error_preserves_both_outcomes(self):
        self.check_observation_failure(2, ValueError('unsafe_runtime_state'))

    def test_last_external_change_is_not_reported_as_verified_completion(self):
        self.execute()
        preview = self.s.compact_runtime_state()
        original = retention.atomic_write_json
        budget_path = budgets._budget_path(self.s.vault)
        changed = []

        def write(path, value, **kwargs):
            original(path, value, **kwargs)
            if path == budget_path:
                altered = json.loads(path.read_text())
                altered['operator_note'] = 'preserve external change'
                original(path, altered)
                changed.append(path.read_bytes())

        with patch.object(retention, 'atomic_write_json', side_effect=write):
            with self.assertRaises(retention.RuntimeRetentionError) as caught:
                self.s.compact_runtime_state(dry_run=False, expected_revision=preview['state_revision'])
        self.assertEqual(caught.exception.result['code'], 'runtime_state_changed')
        self.assertEqual(caught.exception.result['applied_files'], ['processed', 'budget'])
        self.assertEqual(budget_path.read_bytes(), changed[0])

    def test_reinspect_after_final_read_error_is_zero_call_and_idempotent(self):
        self.check_observation_failure(2, OSError('read unavailable'))
        after = self.raw_files()
        result = self.compact()
        self.assertEqual(result['selected'], {'runs': 0, 'commits': 0, 'budgets': 0})
        self.assertEqual(result['applied_files'], [])
        self.assertEqual(self.raw_files(), after)
        backend = Backend()
        replay = self.s.process_incremental(source='hermes', session_id='s', turn_id='t', model=backend)
        self.assertEqual(replay['execution_status'], 'completed')
        self.assertEqual(backend.calls, [])
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')), 1)

    def test_reinspect_after_first_read_error_finishes_only_budget(self):
        self.check_observation_failure(1, OSError('read unavailable'))
        before = self.s.vault.processed_state_path.read_bytes()
        result = self.compact()
        self.assertEqual(result['applied_files'], ['budget'])
        self.assertEqual(self.s.vault.processed_state_path.read_bytes(), before)

    def test_cli_reports_applied_files_without_echoing_private_error(self):
        self.execute()
        preview = self.s.compact_runtime_state()
        _, writer, reader = self.broken_readback(1, OSError('sensitive-path-or-credential'))
        output = StringIO()
        with writer, reader, redirect_stdout(output):
            code = main(['maintain-state', '--vault', str(self.s.vault.root), '--apply',
                         '--expected-revision', preview['state_revision'], '--json'])
        self.assertEqual(code, 1)
        self.assertNotIn('sensitive-path-or-credential', output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result['execution_status'], 'interrupted')
        self.assertEqual(result['applied_files'], ['processed'])

    def test_failed_write_and_failed_readback_report_uncertainty(self):
        self.execute()
        preview = self.s.compact_runtime_state()
        original_write, original_read = retention.atomic_write_json, retention._bytes
        attempted = []

        def write(path, value, **kwargs):
            original_write(path, value, **kwargs)
            attempted.append(path)
            raise OSError('write return failed')

        def read(path):
            if attempted:
                raise OSError('cannot verify whether replacement happened')
            return original_read(path)

        with patch.object(retention, 'atomic_write_json', side_effect=write), patch.object(retention, '_bytes', side_effect=read):
            with self.assertRaises(retention.RuntimeRetentionError) as caught:
                self.s.compact_runtime_state(dry_run=False, expected_revision=preview['state_revision'])
        self.assertEqual(caught.exception.result['applied_files'], [])
        self.assertEqual(caught.exception.result['uncertain_files'], ['processed'])
        self.assertEqual(self.compact()['applied_files'], ['budget'])

    def test_unexpected_bytes_after_failed_write_are_not_claimed_unchanged(self):
        self.execute()
        preview = self.s.compact_runtime_state()
        original = retention.atomic_write_json

        def write(path, value, **kwargs):
            altered = json.loads(path.read_text())
            altered['operator_note'] = 'a concurrent replacement'
            original(path, altered)
            raise OSError('write failed after another change')

        with patch.object(retention, 'atomic_write_json', side_effect=write):
            with self.assertRaises(retention.RuntimeRetentionError) as caught:
                self.s.compact_runtime_state(dry_run=False, expected_revision=preview['state_revision'])
        self.assertEqual(caught.exception.result['uncertain_files'], ['processed'])
        self.assertEqual(caught.exception.result['applied_files'], [])
        self.assertEqual(self.ledger()['operator_note'], 'a concurrent replacement')

    def test_noop_apply_rejects_change_observed_after_preparation(self):
        self.execute()
        self.compact()
        preview = self.s.compact_runtime_state()
        original = retention._prepare

        def prepare(service, max_records):
            result = original(service, max_records)
            altered = self.ledger()
            altered['operator_note'] = 'changed during no-op apply'
            retention.atomic_write_json(self.s.vault.processed_state_path, altered)
            return result

        with patch.object(retention, '_prepare', side_effect=prepare):
            with self.assertRaises(retention.RuntimeRetentionError) as caught:
                self.s.compact_runtime_state(dry_run=False, expected_revision=preview['state_revision'])
        self.assertEqual(caught.exception.result['applied_files'], [])
        self.assertEqual(caught.exception.result['code'], 'runtime_state_changed')
        self.assertEqual(self.ledger()['operator_note'], 'changed during no-op apply')
