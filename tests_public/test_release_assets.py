"""Pure local checks for immutable release reconciliation; never contact GitHub."""
from __future__ import annotations

import copy
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import release_assets as subject

SHA = 'a' * 40
VERSION = '0.2.65'


class FakeGitHub:
    def __init__(self, files):
        self.files = dict(files)
        self.sha = SHA
        self.release_id = 17
        self.uploads = []
        self.extra = []
        self.snapshots = 0
        self.before_snapshot = None
        self.after_download = None
        self.upload_hook = None

    def snapshot(self, tag):
        self.snapshots += 1
        if self.before_snapshot:
            self.before_snapshot(self)
        rows = [{'name': name, 'id': i + 100, 'size': len(data), 'state': 'uploaded'}
                for i, (name, data) in enumerate(sorted(self.files.items()))]
        self.ids = {row['id']: self.files[row['name']] for row in rows}
        return self.sha, self.release_id, rows + copy.deepcopy(self.extra)

    def download(self, identity, path):
        path.write_bytes(self.ids[identity])
        if self.after_download:
            self.after_download(self)

    def upload(self, tag, path):
        self.uploads.append(path.name)
        if self.upload_hook:
            return self.upload_hook(self, path)
        self.files[path.name] = path.read_bytes()
        return True


class ReleaseAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wheel = f'memleaf-{VERSION}-py3-none-any.whl'
        self.source = f'memleaf-{VERSION}.tar.gz'
        self.data = {self.wheel: b'wheel contents', self.source: b'sdist contents'}
        self.data['SHA256SUMS'] = ''.join(
            f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in self.data.items()
        ).encode()
        for name, data in self.data.items():
            (self.root / name).write_bytes(data)

    def reconcile(self, client):
        return subject.reconcile(client, dist=self.root, version=VERSION, expected_sha=SHA)

    def test_identical_existing_is_read_only(self):
        remote = FakeGitHub(self.data)
        result = self.reconcile(remote)
        self.assertEqual('verified', result['status'])
        self.assertEqual([], remote.uploads)
        self.assertFalse(result['replacement_allowed'])

    def test_missing_assets_are_uploaded_create_only(self):
        remote = FakeGitHub({})
        result = self.reconcile(remote)
        self.assertEqual(list(self.data), remote.uploads)
        self.assertEqual(self.data, remote.files)
        self.assertEqual(list(self.data), result['uploaded_assets'])

    def test_only_missing_checksum_is_uploaded(self):
        remote = FakeGitHub({name: value for name, value in self.data.items() if name != 'SHA256SUMS'})
        self.reconcile(remote)
        self.assertEqual(['SHA256SUMS'], remote.uploads)

    def test_all_existing_assets_checked_before_first_upload(self):
        remote = FakeGitHub({self.source: b'wrong content!'})
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'published_asset_conflict'):
            self.reconcile(remote)
        self.assertEqual([], remote.uploads)
        self.assertEqual({self.source: b'wrong content!'}, remote.files)

    def test_equal_size_different_bytes_rejected(self):
        remote = FakeGitHub(self.data)
        remote.files[self.wheel] = b'x' * len(self.data[self.wheel])
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'published_asset_conflict'):
            self.reconcile(remote)
        self.assertEqual([], remote.uploads)

    def test_size_mismatch_rejected_before_download(self):
        remote = FakeGitHub({self.wheel: b'x'})
        with patch.object(remote, 'download', side_effect=AssertionError('not called')):
            with self.assertRaisesRegex(subject.ReleaseAssetError, 'published_asset_conflict'):
                self.reconcile(remote)

    def test_duplicate_expected_name_is_rejected(self):
        remote = FakeGitHub(self.data)
        remote.extra = [{'name': self.wheel, 'id': 999, 'size': 14, 'state': 'uploaded'}]
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'duplicate_release_asset'):
            self.reconcile(remote)

    def test_nonfinal_asset_is_not_deleted_or_replaced(self):
        remote = FakeGitHub({})
        remote.extra = [{'name': self.wheel, 'id': 999, 'size': 0, 'state': 'starter'}]
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'incomplete_release_asset'):
            self.reconcile(remote)
        self.assertEqual([], remote.uploads)

    def test_wrong_tag_target_cannot_upload(self):
        remote = FakeGitHub({})
        remote.sha = 'b' * 40
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'release_target_changed'):
            self.reconcile(remote)
        self.assertEqual([], remote.uploads)

    def test_tag_target_rechecked_between_operations(self):
        remote = FakeGitHub({})
        remote.before_snapshot = lambda c: setattr(c, 'sha', 'b' * 40) if c.snapshots == 2 else None
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'release_target_changed'):
            self.reconcile(remote)
        self.assertEqual([], remote.uploads)

    def test_recreated_release_is_not_original_release(self):
        remote = FakeGitHub({})
        remote.before_snapshot = lambda c: setattr(c, 'release_id', 18) if c.snapshots == 2 else None
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'release_identity_changed'):
            self.reconcile(remote)

    def test_concurrent_identical_upload_is_verified(self):
        remote = FakeGitHub({})
        def won_elsewhere(c, path):
            c.files[path.name] = path.read_bytes()
            return False
        remote.upload_hook = won_elsewhere
        result = self.reconcile(remote)
        self.assertEqual('verified', result['status'])
        self.assertEqual([], result['uploaded_assets'])
        self.assertEqual(self.data, remote.files)

    def test_concurrent_different_upload_is_conflict(self):
        remote = FakeGitHub({})
        def won_elsewhere(c, path):
            c.files[path.name] = b'other'
            return False
        remote.upload_hook = won_elsewhere
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'published_asset_conflict'):
            self.reconcile(remote)
        self.assertEqual([self.wheel], remote.uploads)
        self.assertEqual(b'other', remote.files[self.wheel])

    def test_failed_upload_without_asset_is_not_retried(self):
        remote = FakeGitHub({})
        remote.upload_hook = lambda c, p: False
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'release_upload_unconfirmed'):
            self.reconcile(remote)
        self.assertEqual([self.wheel], remote.uploads)

    def test_success_code_without_asset_not_treated_as_success(self):
        remote = FakeGitHub({})
        remote.upload_hook = lambda c, p: True
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'release_upload_unconfirmed'):
            self.reconcile(remote)

    def test_unrelated_assets_remain_untouched(self):
        remote = FakeGitHub({**self.data, 'notes.txt': b'extra attachment'})
        self.reconcile(remote)
        self.assertEqual(b'extra attachment', remote.files['notes.txt'])
        self.assertEqual([], remote.uploads)

    def test_local_modification_during_remote_check_blocks_upload(self):
        remote = FakeGitHub({self.wheel: self.data[self.wheel]})
        remote.after_download = lambda c: (self.root / self.source).write_bytes(b'changed')
        with self.assertRaises(subject.ReleaseAssetError):
            self.reconcile(remote)
        self.assertEqual([], remote.uploads)

    def test_invalid_checksum_file_blocks_all_network(self):
        (self.root / 'SHA256SUMS').write_bytes(b'incorrect')
        remote = FakeGitHub({})
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'invalid_local_checksums'):
            self.reconcile(remote)
        self.assertEqual(0, remote.snapshots)

    def test_invalid_identifiers_are_rejected(self):
        with self.assertRaises(subject.ReleaseAssetError):
            subject.GitHubCLI('owner/repo/other')
        with self.assertRaises(subject.ReleaseAssetError):
            subject.local_assets(self.root, '../0.2.65')
        with self.assertRaises(subject.ReleaseAssetError):
            subject.reconcile(FakeGitHub({}), dist=self.root, version=VERSION, expected_sha='main')

    def test_api_read_failure_cannot_authorize_upload(self):
        remote = FakeGitHub({})
        with patch.object(remote, 'snapshot', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                self.reconcile(remote)
        self.assertEqual([], remote.uploads)

    def test_download_failure_cannot_authorize_upload(self):
        remote = FakeGitHub({self.wheel: self.data[self.wheel]})
        with patch.object(remote, 'download', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                self.reconcile(remote)
        self.assertEqual([], remote.uploads)

    def test_final_metadata_change_is_detected(self):
        remote = FakeGitHub(self.data)
        def remove(c):
            if c.snapshots == 6:
                del c.files[self.source]
        remote.before_snapshot = remove
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'release_assets_changed'):
            self.reconcile(remote)

    def test_cli_upload_does_not_use_clobber(self):
        client = subject.GitHubCLI('miffyblueboo/memleaf')
        with patch.object(subject.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertFalse(client.upload('v' + VERSION, self.root / self.wheel))
        args = run.call_args.args[0]
        self.assertNotIn('--clobber', args)
        self.assertEqual(['gh', 'release', 'upload'], args[:3])

    def test_malformed_asset_names_are_controlled_errors(self):
        for name in (None, [], {}, 4):
            with self.subTest(name=name):
                remote = FakeGitHub({})
                remote.extra = [{'name': name}]
                with self.assertRaisesRegex(subject.ReleaseAssetError, 'invalid_release_metadata'):
                    self.reconcile(remote)
                self.assertEqual([], remote.uploads)

    def test_partial_upload_retry_only_fills_remaining_files(self):
        remote = FakeGitHub({})
        def interrupted(c, path):
            if path.name == self.source:
                return False
            c.files[path.name] = path.read_bytes()
            return True
        remote.upload_hook = interrupted
        with self.assertRaisesRegex(subject.ReleaseAssetError, 'release_upload_unconfirmed'):
            self.reconcile(remote)
        first_id = remote.files[self.wheel]
        remote.uploads.clear()
        remote.upload_hook = None
        self.reconcile(remote)
        self.assertEqual([self.source, 'SHA256SUMS'], remote.uploads)
        self.assertEqual(first_id, remote.files[self.wheel])

    def test_download_command_uses_binary_asset_id_and_exclusive_output(self):
        client = subject.GitHubCLI('miffyblueboo/memleaf')
        target = self.root / 'download'
        with patch.object(subject.subprocess, 'run') as run:
            client.download(1234, target)
        self.assertIn('Accept: application/octet-stream', run.call_args.args[0])
        self.assertTrue(run.call_args.args[0][-1].endswith('/releases/assets/1234'))
        with self.assertRaises(FileExistsError):
            client.download(1234, target)

    def test_cli_error_does_not_print_provider_exception(self):
        with patch.object(subject.GitHubCLI, 'snapshot', side_effect=OSError('secret-in-error')):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                code = subject.main(['--repository', 'miffyblueboo/memleaf', '--version', VERSION,
                                     '--commit', SHA, '--dist', str(self.root)])
        self.assertEqual(1, code)
        self.assertNotIn('secret-in-error', out.getvalue())
        self.assertIn('recheck_required', out.getvalue())


if __name__ == '__main__':
    unittest.main()
