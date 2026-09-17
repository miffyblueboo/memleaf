"""Existing grants, receipts and partial bases survive an offline byte backup."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import patch

from test_runtime_retention import RetentionFixture
from test_incremental_execution import Backend, output
from memleaf import extraction_work_state as budgets
from memleaf import incremental_run_state as runs
from memleaf import incremental_journal as commits
from memleaf import inspection, migration
from memleaf.locking import atomic_write_json
from memleaf.llm.base import ModelError


class MigrationRuntimeTests(RetentionFixture):
    def destination(self):return Path(self.temp.name)/'backup'

    def backup(self):
        r=self.s.migration_preflight()
        return self.s.backup_for_migration(self.destination(),expected_snapshot=r['snapshot_revision'],writers_stopped=True)

    def test_compressed_receipts_and_retired_budgets_are_preserved_exactly(self):
        first=self.execute(self.create(),self.no_memory())
        self.compact()
        before=self.raw_files();self.backup()
        copied=inspection._snapshot(self.destination()/'vault')
        for area in ('_state/processed.json','_state/extraction_request_budget.json'):
            self.assertEqual(copied[area],before[area])
        stored=json.loads(copied['_state/processed.json'])
        self.assertEqual(runs.load_run(stored,first['run_id']),runs.load_run(self.ledger(),first['run_id']))
        again=self.s.process_incremental(source='hermes',session_id='s',turn_id='t',model=Backend())
        self.assertEqual(again['commit'],first['commit']);self.assertEqual(again['model_calls_this_invocation'],0)

    def test_partial_basis_survives_but_switch_stays_blocked(self):
        first=self.execute(self.create(),{'action':'DEFERRED','evidence':['e2'],'reason':'missing_context','need':'clarify'})
        before=self.raw_files();r=self.backup()
        self.assertIn('unresolved_runs_require_review',r['migration_blockers'])
        self.assertEqual(self.raw_files(),before)
        self.assertEqual((self.destination()/'vault/_state/processed.json').read_bytes(),before['_state/processed.json'])
        self.assertTrue(self.s.resume_incremental_run(first['run_id'])['partial_recovery_available'])

    def test_retryable_budget_is_not_finalized_by_backup(self):
        first=self.s.run_incremental(source='hermes',session_id='s',turn_id='t',backend=Backend(ModelError('temporary',code='model_timeout')))
        before=budgets._budget_path(self.s.vault).read_bytes()
        self.backup()
        self.assertEqual(budgets._budget_path(self.s.vault).read_bytes(),before)
        again=self.s.resume_incremental_run(first['run_id'],backend=Backend(output(self.no_memory('e1'),self.no_memory())))
        self.assertEqual(again['reserved_requests'],2)

    def test_missing_budget_blocks_switch_and_is_not_recreated(self):
        self.execute();budgets._budget_path(self.s.vault).unlink()
        r=self.s.migration_preflight()
        self.assertIn('request_budget_evidence_missing',r['blockers'])
        self.backup();self.assertFalse(budgets._budget_path(self.s.vault).exists())

    def test_forget_suppression_survives_backup(self):
        first=self.execute(self.create(),self.no_memory());mid=first['commit']['operations'][0]['memory_id']
        self.s.forget_memory(mid)
        before=self.raw_files()
        self.backup()
        self.assertEqual(self.raw_files(),before)
        again=self.s.resume_incremental_run(first['run_id'])
        self.assertEqual(again['execution_status'],'cancelled')
        self.assertEqual(len(self.s.vault.list_markdown('knowledge')),0)

    def test_live_owner_blocks_backup_without_cancelling_owner(self):
        self.execute()
        with patch.object(runs,'owner_live',return_value=True):
            before=self.raw_files()
            with self.assertRaises(migration.MigrationError) as c:self.backup()
        self.assertIn('processing_owner_live',c.exception.result['blockers'])
        self.assertEqual(self.raw_files(),before);self.assertFalse(self.destination().exists())

    def test_concurrent_destination_creation_cannot_overwrite(self):
        self.execute();r=self.s.migration_preflight();gate=Barrier(2)
        def attempt():
            gate.wait()
            try:return self.s.backup_for_migration(self.destination(),expected_snapshot=r['snapshot_revision'],writers_stopped=True)['execution_status']
            except migration.MigrationError:return 'blocked'
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sorted(results),['blocked','completed'])
        self.assertEqual(migration.verify_migration_backup(self.destination())['execution_status'],'verified')

    def test_native_source_is_not_read_or_copied(self):
        self.execute()
        config=self.s.vault.config_path
        # External native notes belong to a separate backup boundary.
        external=Path(self.temp.name)/'native.md';external.write_text('private native note')
        from memleaf.config import save_config
        value=self.s.vault.config();value['native_sources']={'hermes':{'agent':'hermes','path':str(external),'format':'markdown','enabled':True,'share':False}}
        save_config(config,value)
        before=external.read_bytes()
        with patch('memleaf.native_index.NativeIndexer.refresh',side_effect=AssertionError('must not touch native notes')):
            self.backup()
        self.assertEqual(external.read_bytes(),before)
        all_files=[p.name for p in (self.destination()/'vault').rglob('*') if p.is_file()]
        self.assertNotIn('native.md',all_files)

    def test_schema_markers_and_unknown_state_files_copied_not_upgraded(self):
        self.execute()
        p=self.s.vault.state_path/'future-extension.json';p.write_text('{"future":true}')
        before=self.raw_files();self.backup()
        self.assertEqual(self.raw_files(),before)
        self.assertEqual((self.destination()/'vault/_state/future-extension.json').read_bytes(),p.read_bytes())
