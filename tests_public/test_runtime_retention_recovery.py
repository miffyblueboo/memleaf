from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_runtime_retention import RetentionFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf, runtime_retention as retention
from memleaf import incremental_run_state as runs, incremental_journal as commits
from memleaf import extraction_work_state as budgets, receipt_codec as codec
from memleaf.cli import main
from memleaf.config import save_config
from memleaf.locking import atomic_write_json


class RuntimeRetentionRecoveryTests(RetentionFixture):
    def test_atomic_replacement_faults_can_resume_without_regeneration(self):
        original = retention.atomic_write_json
        for fail_at in (1, 2):
            for after in (False, True):
                with self.subTest(write=fail_at, after=after):
                    self.s = Memleaf.initialize(Path(self.temp.name) / f"fault-{fail_at}-{after}")
                    self.capture()
                    first = self.execute(self.create(), self.no_memory())
                    preview = self.s.compact_runtime_state()
                    counter = [0]
                    def fail(path, value, **kwargs):
                        counter[0] += 1
                        if counter[0] == fail_at:
                            if after: original(path, value, **kwargs)
                            raise OSError("injected")
                        original(path, value, **kwargs)
                    with patch.object(retention, "atomic_write_json", side_effect=fail):
                        with self.assertRaises(retention.RuntimeRetentionError) as caught:
                            self.s.compact_runtime_state(dry_run=False, expected_revision=preview["state_revision"])
                    self.assertEqual(caught.exception.result["execution_status"], "interrupted")
                    result = self.compact()
                    self.assertEqual(result["execution_status"], "completed")
                    replay = self.s.resume_incremental_run(first["run_id"], backend=Backend())
                    self.assertEqual(replay["commit"], first["commit"])
                    self.assertEqual(replay["model_calls_this_invocation"], 0)
                    self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 1)

    def test_process_exit_between_control_files_preserves_old_decision(self):
        for phase in (1, 2):
            with self.subTest(phase=phase):
                self.s = Memleaf.initialize(Path(self.temp.name) / f"exit-{phase}")
                self.capture(); first = self.execute(self.create(), self.no_memory())
                program = '''import os,sys
from memleaf import Memleaf
from memleaf import runtime_retention as r
s=Memleaf(sys.argv[1]); phase=int(sys.argv[2]); n=[0]; original=r.atomic_write_json
plan=s.compact_runtime_state()
def write(path,value,**kw):
 original(path,value,**kw); n[0]+=1
 if n[0]==phase: os._exit(73)
r.atomic_write_json=write
s.compact_runtime_state(dry_run=False,expected_revision=plan['state_revision'])
'''
                proc = subprocess.run([sys.executable, "-c", program, str(self.s.vault.root), str(phase)],
                                      capture_output=True, timeout=20)
                self.assertEqual(proc.returncode, 73, proc.stderr.decode())
                self.compact()
                replay = Memleaf(self.s.vault.root).resume_incremental_run(first["run_id"], backend=Backend())
                self.assertEqual(replay["commit"], first["commit"])

    def test_concurrent_apply_serializes_and_stale_one_rejects(self):
        self.execute(); plan = self.s.compact_runtime_state()
        def attempt():
            try:
                return Memleaf(self.s.vault.root).compact_runtime_state(dry_run=False, expected_revision=plan["state_revision"])["execution_status"]
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(values, ["completed", "runtime_state_changed"])
        self.assertEqual(self.s.compact_runtime_state()["after"]["runs_compact"], 1)

    def test_model_owner_is_not_expired_by_maintenance(self):
        before = []
        def callback():
            before.append(self.raw_files())
            with self.assertRaisesRegex(ValueError, "processing_busy"):
                self.s.compact_runtime_state()
            self.assertEqual(before[0], self.raw_files())
            return output(self.no_memory("e1"), self.no_memory())
        result = self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=Backend(callback))
        self.assertEqual(result["execution_status"], "completed")

    def test_live_legacy_marker_is_not_removed(self):
        from memleaf.models import utc_now
        self.execute()
        state = self.ledger()
        state["sessions"]["hermes/s"]["processing"] = {"status": "processing", "owner_pid": os.getpid(), "token": "legacy", "started_at": utc_now()}
        atomic_write_json(self.s.vault.processed_state_path, state)
        before = self.raw_files()
        with self.assertRaisesRegex(ValueError, "processing_busy"): self.compact()
        self.assertEqual(before, self.raw_files())

    def test_external_edit_between_replacements_is_not_overwritten(self):
        self.execute(); plan = self.s.compact_runtime_state()
        original = retention.atomic_write_json
        path = budgets._budget_path(self.s.vault)
        changed = []
        def edit(path_arg, value, **kwargs):
            original(path_arg, value, **kwargs)
            if path_arg == self.s.vault.processed_state_path:
                raw = json.loads(path.read_text()); raw["operator_note"] = "preserve"
                path.write_text(json.dumps(raw)); changed.append(path.read_bytes())
        with patch.object(retention, "atomic_write_json", side_effect=edit):
            with self.assertRaises(retention.RuntimeRetentionError) as caught:
                self.s.compact_runtime_state(dry_run=False, expected_revision=plan["state_revision"])
        self.assertEqual(caught.exception.result["code"], "runtime_state_changed")
        self.assertEqual(path.read_bytes(), changed[0])

    def test_resolved_parent_child_operations_remain_identical(self):
        row = self.create(); row["evidence"] = "e1"
        first = self.execute(row, self.no_memory())
        final = self.s.recover_incremental_partial(first["run_id"], mode="repair")
        self.assertEqual(final["execution_status"], "completed")
        old = self.ledger(); parents = commits.resolved_parent_ids(old)
        self.assertEqual(len(parents), 1)
        result = self.compact()
        self.assertEqual(result["selected"]["commits"], 2)
        self.assertEqual(commits.resolved_parent_ids(self.ledger()), parents)
        for key in old[commits.KEY]:
            self.assertEqual(commits.load_work(self.ledger(), key), commits.load_work(old, key))
        replay = self.s.recover_incremental_partial(first["run_id"], mode="repair")
        self.assertEqual(replay["commit"]["operations"], final["commit"]["operations"])

    def test_native_no_change_receipt_survives_missing_native_file(self):
        native = Path(self.temp.name) / "notes.md"
        native.write_text("# Atlas task\nDeliver the report.")
        config = self.s.vault.config(); config["native_sources"] = {"notes": {"path": str(native), "agent": "hermes", "share": False, "enabled": True}}
        save_config(self.s.vault.config_path, config)
        first = self.execute({"action": "NO_CHANGE", "target": "m1", "evidence": ["e1"]}, self.no_memory())
        original = native.read_bytes()
        self.compact(); self.assertEqual(native.read_bytes(), original)
        native.unlink()
        again = self.s.resume_incremental_run(first["run_id"], backend=Backend())
        self.assertEqual(again["commit"], first["commit"])
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 0)

    def test_replay_after_source_cleanup_is_still_historical(self):
        first = self.execute(); self.compact()
        for path in self.s.vault.list_markdown("inbox"): path.unlink()
        again = self.s.process_incremental(source="hermes", session_id="s", turn_id="t", model=Backend())
        self.assertEqual(again["commit"], first["commit"])
        self.assertEqual(again["reservations"], 1)

    def test_compacted_run_cannot_be_reopened(self):
        first = self.execute(); self.compact()
        state = self.ledger(); run = runs.load_run(state, first["run_id"])
        run["status"] = "ready"
        before = self.raw_files()
        with self.assertRaises(ValueError): runs.save_run(self.s, state, run)
        self.assertEqual(before, self.raw_files())

    def test_compacted_commit_cannot_hide_unsettled_index_work(self):
        first = self.execute(); self.compact()
        state = self.ledger(); work = commits.load_work(state, first["commit_work_id"])
        work["index_status"] = "dirty"
        before = self.raw_files()
        with self.assertRaisesRegex(ValueError, "compact_work_not_sealed"):
            commits.save_work(self.s, state, work)
        self.assertEqual(before, self.raw_files())

    def test_corrupt_compact_receipt_stays_untouched(self):
        first = self.execute(); self.compact()
        state = self.ledger(); state[runs.KEY][first["run_id"]]["checksum"] = "0" * 64
        atomic_write_json(self.s.vault.processed_state_path, state)
        before = self.raw_files()
        with self.assertRaisesRegex(ValueError, "invalid_compact_receipt"): self.compact()
        self.assertEqual(before, self.raw_files())

    def test_invalid_retired_budget_does_not_release_slot(self):
        self.execute(); self.compact()
        path = budgets._budget_path(self.s.vault)
        data = json.loads(path.read_text())
        next(iter(next(iter(data["works"].values()))["turns"].values()))["completed"] = False
        atomic_write_json(path, data)
        with self.assertRaises(budgets.ExtractionWorkStateError): self.compact()

    def test_encoding_versions_are_distinct_from_old_writers(self):
        first = self.execute(); self.compact()
        state = self.ledger()
        self.assertNotEqual(state[runs.KEY][first["run_id"]]["version"], runs.VERSION)
        self.assertNotIn(state[commits.KEY][first["commit_work_id"]]["version"], commits.SUPPORTED_VERSIONS)
        self.assertEqual(json.loads(budgets._budget_path(self.s.vault).read_text())["version"], 2)

    def test_dangling_state_link_is_rejected_before_open(self):
        path = budgets._budget_path(self.s.vault)
        path.symlink_to(Path(self.temp.name) / "missing")
        with self.assertRaises((ValueError, budgets.ExtractionWorkStateError)):
            self.s.compact_runtime_state()
        self.assertTrue(path.is_symlink())

    def test_cli_defaults_to_read_only_preview(self):
        self.execute(); before = self.raw_files(); buffer = StringIO()
        with redirect_stdout(buffer): rc = main(["maintain-state", "--vault", str(self.s.vault.root), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buffer.getvalue())["execution_status"], "preview")
        self.assertEqual(before, self.raw_files())

    def test_cli_apply_needs_exact_revision(self):
        self.execute(); buffer = StringIO()
        with redirect_stdout(buffer): rc = main(["maintain-state", "--vault", str(self.s.vault.root), "--apply", "--json"])
        self.assertEqual(rc, 1)
        plan = self.s.compact_runtime_state(); buffer = StringIO()
        with redirect_stdout(buffer):
            rc = main(["maintain-state", "--vault", str(self.s.vault.root), "--apply", "--expected-revision", plan["state_revision"], "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buffer.getvalue())["execution_status"], "completed")

    def test_cli_interruption_retains_partial_outcome(self):
        self.execute(); plan = self.s.compact_runtime_state(); buffer = StringIO()
        with patch.object(retention, "atomic_write_json", side_effect=OSError("fault")), redirect_stdout(buffer):
            rc = main(["maintain-state", "--vault", str(self.s.vault.root), "--apply", "--expected-revision", plan["state_revision"], "--json"])
        self.assertEqual(rc, 1)
        value = json.loads(buffer.getvalue())
        self.assertEqual(value["execution_status"], "interrupted")
        self.assertEqual(value["applied_files"], [])
