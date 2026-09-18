"""Missing established control is not new capture or provider-call authority."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf import extraction_work_state as budgets
from memleaf.process_common import _read_processed
from memleaf.query_progress import observe_progress
from test_incremental_execution import Backend, output


class ControlAbsenceContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Memleaf.initialize(Path(self.tmp.name)/'vault')

    def run_work(self):
        self.s.capture('hermes','s','t','user','Project Atlas uses the agreed workflow.',message_id='u')
        self.s.capture('hermes','s','t','assistant','Acknowledged.',message_id='a',final=True)
        return self.s.run_incremental(source='hermes',session_id='s',turn_id='t',backend=Backend(output(
            {'action':'NO_MEMORY','evidence':['e1']},{'action':'NO_MEMORY','evidence':['e2']})))

    def test_missing_processed_blocks_capture_without_reinitialization(self):
        self.s.create_memory(memory_id='m',title='Existing',body='Still readable.')
        path=self.s.vault.processed_state_path;path.unlink()
        restarted=Memleaf(self.s.vault.root)
        self.assertFalse(path.exists())
        self.assertEqual(restarted.read('m').body,'Still readable.')
        self.assertEqual(observe_progress(restarted.vault)['status'],'unknown')
        with self.assertRaises(ValueError):restarted.capture('hermes','s','t','user','not collected')
        self.assertFalse(path.exists());self.assertEqual(restarted.vault.list_markdown('inbox'),[])

    def test_missing_processed_blocks_both_remember_routes_before_backend(self):
        self.s.vault.processed_state_path.unlink()
        for pipeline in ('legacy','incremental'):
            backend=Backend()
            with self.subTest(pipeline=pipeline),self.assertRaises((ValueError,RuntimeError)):
                self.s.remember('Keep the approved decision.',pipeline=pipeline,model=backend,intent_id='intent')
            self.assertFalse(backend.calls)

    def test_consumed_budget_cannot_be_recreated_on_restart(self):
        self.run_work();path=budgets._budget_path(self.s.vault)
        self.assertTrue(path.exists());path.unlink()
        restarted=Memleaf(self.s.vault.root)
        with self.assertRaises(budgets.ExtractionWorkStateError):budgets._read_budget_state_unlocked(restarted.vault)
        self.assertFalse(path.exists())

    def test_legacy_consumption_still_blocks_without_new_invariant(self):
        self.run_work();path=budgets._budget_path(self.s.vault);path.unlink()
        marker=self.s.vault.state_layout_path;value=json.loads(marker.read_text());value.pop('required_controls',None);marker.write_text(json.dumps(value))
        with self.assertRaises(budgets.ExtractionWorkStateError):budgets._read_budget_state_unlocked(self.s.vault)
        self.assertFalse(path.exists())

    def test_reservation_failure_leaves_no_permission_to_retry_as_fresh(self):
        state=budgets._empty_state()
        original=budgets.atomic_write_json
        def fail_budget(path,value,**kw):
            if path.name=='extraction_request_budget.json':raise OSError('disk interruption')
            return original(path,value,**kw)
        with patch.object(budgets,'atomic_write_json',fail_budget),self.s.vault.lock():
            with self.assertRaises(OSError):budgets._save_budget_state_unlocked(self.s.vault,state)
        self.assertIn('extraction_request_budget.json',json.loads(self.s.vault.state_layout_path.read_text())['required_controls'])
        with self.assertRaises(budgets.ExtractionWorkStateError):budgets._read_budget_state_unlocked(self.s.vault)

    def test_genuine_first_use_budget_is_available_without_new_files(self):
        before={p.name:p.read_bytes() for p in self.s.vault.state_path.iterdir() if p.is_file()}
        self.assertEqual(budgets._read_budget_state_unlocked(self.s.vault),budgets._empty_state())
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.s.vault.state_path.iterdir() if p.is_file()})

    def test_corrupt_required_control_invariant_not_silently_ignored(self):
        marker=self.s.vault.state_layout_path;v=json.loads(marker.read_text());v['required_controls']=['unknown'];marker.write_text(json.dumps(v))
        self.s.vault.processed_state_path.unlink()
        with self.assertRaises((ValueError,RuntimeError)):_read_processed(self.s.vault.processed_state_path)
        self.assertFalse(self.s.vault.processed_state_path.exists())

    def test_lossless_compaction_does_not_restore_missing_request_authority(self):
        self.run_work();path=budgets._budget_path(self.s.vault);path.unlink()
        p=self.s.compact_runtime_state();r=self.s.compact_runtime_state(dry_run=False,expected_revision=p['state_revision'])
        self.assertEqual(r['selected']['budgets'],0);self.assertFalse(r['budget_present'])
        self.assertFalse(r['request_authority_restored']);self.assertFalse(path.exists())
        with self.assertRaises(budgets.ExtractionWorkStateError):budgets._read_budget_state_unlocked(self.s.vault)
