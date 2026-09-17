"""Migration tooling never treats a backup or clear scan as release permission."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf import inspection, migration
from memleaf.cli import main
from memleaf.locking import atomic_write_json
from memleaf.vault import Vault


class MigrationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memleaf-migration-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.s = Memleaf.initialize(self.home / "库 vault")
        self.dest = self.home / "备份 copy"

    def snapshot(self):
        return inspection._checked_snapshot(self.s.vault.root)

    def inspect(self):
        return self.s.migration_preflight()

    def backup(self, **options):
        options.setdefault("expected_snapshot", self.inspect()["snapshot_revision"])
        options.setdefault("writers_stopped", True)
        return self.s.backup_for_migration(self.dest, **options)

    def capture(self):
        self.s.capture("hermes", "session", "t", "user", "Track the report.", message_id="u", source_sequence=1,
                       source_time="2026-09-17T10:00:00+08:00")

    def cli(self, args):
        stream = StringIO()
        with redirect_stdout(stream):
            code = main(args)
        return code, json.loads(stream.getvalue())


class MigrationPreflightTests(MigrationFixture):
    def test_clear_is_not_switch_permission(self):
        r = self.inspect()
        self.assertEqual(r["local_status"], "clear")
        self.assertFalse(r["switch_authorized"])
        self.assertEqual(r["configured_pipelines"], {"automatic_pipeline":"legacy", "remember_pipeline":"legacy"})
        self.assertIn("real_model_multiturn_acceptance", r["required_external_checks"])
        self.assertEqual(r["runtime"]["installed_host_verification"], "not_performed")
        self.assertEqual(len(r["runtime"]["implementation_fingerprint"]),64)

    def test_preflight_does_not_initialize_or_lock_or_resolve_model(self):
        before = self.snapshot()
        self.s.vault.lock_path.unlink()
        with patch.object(Vault,"ensure",side_effect=AssertionError), patch.object(Vault,"lock",side_effect=AssertionError):
            r = self.inspect()
            self.assertEqual(r["model_calls"],0)
        self.assertFalse(self.s.vault.lock_path.exists())
        self.assertEqual(before,self.snapshot())

    def test_missing_processed_is_not_fresh_authority(self):
        self.s.vault.processed_state_path.unlink()
        r = self.inspect()
        self.assertIn("processed_state_missing", r["blockers"])
        self.assertFalse(self.s.vault.processed_state_path.exists())

    def test_invalid_runtime_controls_preserved(self):
        path=self.s.vault.processed_state_path
        path.write_text('{"version":1,"version":2}')
        before=path.read_bytes()
        r=self.inspect()
        self.assertIn("invalid_runtime_controls",r["blockers"])
        self.assertEqual(path.read_bytes(),before)

    def test_lineage_cannot_reset_damaged_source_authority(self):
        path = self.s.vault.processed_state_path
        for raw in (b'bad JSON', b'[]', b'{"version":2}', b'{"version":1,"version":1}'):
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises((ValueError, RuntimeError)):
                self.s.session_lineage("hermes", "child", parent_session_id="parent")
            self.assertEqual(path.read_bytes(), raw)

    def test_unknown_job_status_blocks_readiness(self):
        p=self.s.vault.state_path / 'process_jobs.json'
        atomic_write_json(p,{"version":1,"jobs":{"j":{"status":"new-unsupported"}},"order":["j"],"active_job_id":None})
        self.assertIn("unknown_job_state",self.inspect()["blockers"])

    def test_queue_blocks_backup_before_creation(self):
        atomic_write_json(self.s.vault.state_path/'process_jobs.json',
                          {"version":1,"jobs":{"j":{"status":"pending"}},"order":["j"],"active_job_id":None})
        with self.assertRaises(migration.MigrationError) as c:self.backup()
        self.assertIn("queue_not_quiescent",c.exception.result["blockers"])
        self.assertFalse(self.dest.exists())

    def test_starting_job_is_not_mistaken_for_quiescence(self):
        atomic_write_json(self.s.vault.state_path/'process_jobs.json',
                          {"version":1,"jobs":{"j":{"status":"starting"}},"order":["j"],"active_job_id":None})
        report = self.inspect()
        self.assertIn("queue_not_quiescent", report["backup_blockers"])
        self.assertNotIn("unknown_job_state", report["blockers"])
        with self.assertRaises(migration.MigrationError):
            self.backup()
        self.assertFalse(self.dest.exists())

    def test_bad_memory_and_conflict_block_switch_not_data_preservation(self):
        m=self.s.create_memory(memory_id="mem-task",title="task",body="Deliver report")
        p=next(self.s.vault.knowledge_path.rglob('*.md'))
        (p.parent/'copy.md').write_bytes(p.read_bytes())
        (p.parent/'bad.md').write_text('---\n: [\n---\n')
        r=self.inspect()
        self.assertIn("memory_scan_incomplete",r["blockers"])
        result=self.backup()
        self.assertEqual(result["local_migration_status"],"blocked")
        self.assertFalse(result["switch_authorized"])

    def test_pending_source_backed_up_without_being_consumed(self):
        self.capture()
        before=self.snapshot()
        r=self.inspect()
        self.assertIn("unsettled_sources_require_review",r["blockers"])
        result=self.backup()
        self.assertEqual(result['backup_status'],'verified')
        self.assertEqual(before,self.snapshot())
        self.assertEqual(before,inspection._snapshot(self.dest/'vault'))

    def test_old_layout_is_reported_and_saved(self):
        (self.s.vault.index_path/'processed.json').write_text('{"version":1,"sessions":{}}')
        self.assertIn("legacy_state_layout_requires_review",self.inspect()["blockers"])
        self.backup()
        self.assertTrue((self.dest/'vault/_index/processed.json').exists())

    def test_inspection_must_not_swallow_enumeration_failure(self):
        def walk(root,**kw):
            kw['onerror'](PermissionError('private-path-secret'))
            return iter(())
        with patch.object(inspection.os,'walk',side_effect=walk):
            with self.assertRaises(inspection.InspectionError):self.inspect()

    def test_arbitrary_lock_named_control_preserved(self):
        path=self.s.vault.state_path/'authority.lock'
        path.write_bytes(b'opaque cancellation metadata')
        self.backup()
        self.assertEqual((self.dest/'vault/_state/authority.lock').read_bytes(),path.read_bytes())
        self.assertFalse((self.dest/'vault/_state/vault.lock').exists())

    def test_managed_area_as_file_is_error(self):
        self.s.vault.knowledge_path.rmdir()
        self.s.vault.knowledge_path.write_text('must not vanish')
        with self.assertRaises(inspection.InspectionError): self.inspect()

    def test_snapshot_budget_is_bounded(self):
        with patch.object(inspection,'MAX_SNAPSHOT_BYTES',20):
            with self.assertRaises(inspection.InspectionError):self.inspect()

    def test_empty_directories_count_towards_limit(self):
        for i in range(12): (self.s.vault.knowledge_path/str(i)).mkdir()
        with patch.object(inspection,'MAX_SNAPSHOT_FILES',10):
            with self.assertRaises(inspection.InspectionError):self.inspect()

    def test_cli_readonly_status_has_nonzero_when_blocked(self):
        self.capture()
        code,r=self.cli(['migration-check','--vault',str(self.s.vault.root),'--json'])
        self.assertEqual(code,2)
        self.assertFalse(r['switch_authorized'])


class BackupTests(MigrationFixture):
    def test_roundtrip_preserves_config_permissions_and_unknown_state(self):
        path=self.s.vault.config_path
        path.write_text(path.read_text()+'\n# preserve formatting and comment\n')
        (self.s.vault.state_path/'future-control.dat').write_bytes(b'\x00opaque\r\n')
        before=self.snapshot()
        r=self.backup()
        self.assertEqual(before,inspection._snapshot(self.dest/'vault'))
        meta=json.loads((self.dest/'manifest.json').read_text())
        self.assertEqual(set(meta['files']),set(before))
        self.assertFalse(r['restore_authorized'])
        self.assertEqual(before,self.snapshot())
        if os.name=='posix':
            self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode),0o700)
            for p in self.dest.rglob('*'):
                self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o700 if p.is_dir() else 0o600)

    def test_writers_attestation_required_before_output(self):
        with self.assertRaises(migration.MigrationError) as c:self.backup(writers_stopped=False)
        self.assertEqual(c.exception.code,'writers_stopped_confirmation_required')
        self.assertFalse(self.dest.exists())

    def test_stale_revision_never_creates_destination(self):
        r=self.inspect()
        self.s.create_memory(memory_id='m',title='new',body='new')
        with self.assertRaises(migration.MigrationError):self.backup(expected_snapshot=r['snapshot_revision'])
        self.assertFalse(self.dest.exists())

    def test_overlap_rejected(self):
        r=self.inspect()
        for dest in (self.s.vault.root,self.s.vault.root/'backup',self.home):
            with self.subTest(dest=dest),self.assertRaises(migration.MigrationError):
                self.s.backup_for_migration(dest,expected_snapshot=r['snapshot_revision'],writers_stopped=True)

    def test_existing_destination_not_overwritten(self):
        self.dest.mkdir();(self.dest/'keep').write_text('keep')
        with self.assertRaises(migration.MigrationError):self.backup()
        self.assertEqual((self.dest/'keep').read_text(),'keep')

    @unittest.skipUnless(os.name=='posix','symlink setup exercised on POSIX')
    def test_source_and_destination_links_rejected(self):
        original=self.s.vault.config_path.read_bytes()
        self.s.vault.config_path.unlink()
        outside=self.home/'external';outside.write_bytes(original)
        self.s.vault.config_path.symlink_to(outside)
        with self.assertRaises((ValueError,OSError)):self.inspect()
        (self.s.vault.root/"config.yaml").unlink();self.s.vault.config_path.write_bytes(original)
        self.dest.symlink_to(self.home,target_is_directory=True)
        with self.assertRaises(migration.MigrationError):self.backup()

    def test_manifest_last_and_disk_failure_is_explicit(self):
        original=migration._exclusive_write
        def write(path,raw):
            if path.name=='manifest.json':raise OSError('private credential in underlying message')
            original(path,raw)
        before=self.snapshot()
        with patch.object(migration,'_exclusive_write',side_effect=write):
            with self.assertRaises(migration.MigrationError) as c:self.backup()
        self.assertEqual(c.exception.result['backup_status'],'incomplete')
        self.assertNotIn('credential',json.dumps(c.exception.result))
        self.assertFalse((self.dest/'manifest.json').exists())
        with self.assertRaises(migration.MigrationError):migration.verify_migration_backup(self.dest)
        self.assertEqual(before,self.snapshot())

    def test_source_changes_during_copy_not_success(self):
        original=migration._exclusive_write; changed=[]
        def write(path,raw):
            original(path,raw)
            if not changed:
                self.s.vault.config_path.write_text(self.s.vault.config_path.read_text()+'\n# changed\n')
                changed.append(True)
        with patch.object(migration,'_exclusive_write',side_effect=write):
            with self.assertRaises(migration.MigrationError) as c:self.backup()
        self.assertEqual(c.exception.code,'migration_snapshot_changed')
        self.assertFalse((self.dest/'manifest.json').exists())

    def test_changed_backup_content_fails_verification(self):
        self.backup()
        p=self.dest/'vault/config.yaml';p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaises(migration.MigrationError):migration.verify_migration_backup(self.dest)

    def test_missing_and_extra_file_fail_verification(self):
        self.backup()
        extra=self.dest/'vault/unmanaged.txt';extra.write_text('unmanaged')
        with self.assertRaises(migration.MigrationError):migration.verify_migration_backup(self.dest)
        extra.unlink();(self.dest/'vault/config.yaml').unlink()
        with self.assertRaises(migration.MigrationError):migration.verify_migration_backup(self.dest)

    def test_path_traversal_in_manifest_never_extracted(self):
        self.backup();p=self.dest/'manifest.json';meta=json.loads(p.read_text());
        meta['files']['../../outside']={'size':0,'sha256':hashlib.sha256(b'').hexdigest()};p.write_text(json.dumps(meta))
        with self.assertRaises(migration.MigrationError):migration.verify_migration_backup(self.dest)
        self.assertFalse((self.home/'outside').exists())

    def test_manifest_schema_and_duplicate_keys_rejected(self):
        self.backup();p=self.dest/'manifest.json';base=p.read_text()
        for text in (base.replace('"version": 1','"version": true'),base.replace('"version": 1','"version": 2'),'{"version":1,"version":1}'):
            p.write_text(text)
            with self.subTest(text=text[:40]),self.assertRaises(migration.MigrationError):migration.verify_migration_backup(self.dest)

    def test_verify_cli_does_not_open_or_initialize_source(self):
        self.backup()
        with patch.object(Vault,'__init__',side_effect=AssertionError):
            code,r=self.cli(['migration-backup','--verify',str(self.dest),'--json'])
        self.assertEqual(code,0);self.assertFalse(r['restore_authorized'])

    def test_backup_cli_and_no_default_side_effect(self):
        p=self.inspect()
        code,r=self.cli(['migration-backup','--vault',str(self.s.vault.root),'--destination',str(self.dest),
                         '--expected-snapshot',p['snapshot_revision'],'--json'])
        self.assertEqual(code,1);self.assertFalse(self.dest.exists())
        code,r=self.cli(['migration-backup','--vault',str(self.s.vault.root),'--destination',str(self.dest),
                         '--expected-snapshot',p['snapshot_revision'],'--writers-stopped','--json'])
        self.assertEqual(code,0);self.assertEqual(r['backup_status'],'verified')

    def test_verification_and_creation_controls_not_mixed(self):
        self.backup()
        code,r=self.cli(['migration-backup','--verify',str(self.dest),'--writers-stopped','--json'])
        self.assertEqual(code,1);self.assertEqual(r['code'],'invalid_backup_verify_options')

    def test_process_exit_leaves_unverifiable_backup_and_intact_vault(self):
        revision=self.inspect()['snapshot_revision'];before=self.snapshot()
        script='''from memleaf import Memleaf, migration
from memleaf.vault import Vault
import os,sys
s=Memleaf(Vault(sys.argv[1],create=False))
orig=migration._exclusive_write
def write(path,raw):
    orig(path,raw)
    os._exit(77)
migration._exclusive_write=write
s.backup_for_migration(sys.argv[2],expected_snapshot=sys.argv[3],writers_stopped=True)
'''
        p=subprocess.run([sys.executable,'-c',script,str(self.s.vault.root),str(self.dest),revision],timeout=20)
        self.assertEqual(p.returncode,77)
        self.assertEqual(before,self.snapshot())
        with self.assertRaises(migration.MigrationError):migration.verify_migration_backup(self.dest)

    def test_raw_corrupt_state_can_be_preserved_not_declared_migration_ready(self):
        self.s.vault.processed_state_path.write_bytes(b'bad json')
        r=self.backup()
        self.assertEqual(r['local_migration_status'],'blocked')
        self.assertEqual((self.dest/'vault/_state/processed.json').read_bytes(),b'bad json')
        self.assertFalse(r['switch_authorized'])
