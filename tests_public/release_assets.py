"""Reconcile existing GitHub Release assets without replacing published bytes.

Release-only CI helper. No tag creation, asset deletion or --clobber operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


class ReleaseAssetError(RuntimeError):
    pass


def _fingerprint(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseAssetError('invalid_local_asset')
    h = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def local_assets(dist: Path, version: str) -> dict[str, tuple[int, str]]:
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)', version):
        raise ReleaseAssetError('invalid_version')
    names = [f'memleaf-{version}-py3-none-any.whl', f'memleaf-{version}.tar.gz']
    values = {name: _fingerprint(dist / name) for name in names}
    expected = ''.join(f'{values[name][1]}  {name}\n' for name in names).encode('ascii')
    if (dist / 'SHA256SUMS').is_symlink() or (dist / 'SHA256SUMS').read_bytes() != expected:
        raise ReleaseAssetError('invalid_local_checksums')
    values['SHA256SUMS'] = _fingerprint(dist / 'SHA256SUMS')
    return values


class GitHubCLI:
    def __init__(self, repository: str):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ReleaseAssetError('invalid_repository')
        self.repository = repository

    def _json(self, endpoint):
        result = subprocess.run(['gh', 'api', endpoint], capture_output=True,
                                check=True, timeout=120)
        return json.loads(result.stdout)

    def snapshot(self, tag):
        commit = self._json(f'repos/{self.repository}/commits/{tag}')
        release = self._json(f'repos/{self.repository}/releases/tags/{tag}')
        return commit['sha'], release['id'], release['assets']

    def download(self, asset_id, destination):
        with destination.open('xb') as stream:
            subprocess.run(['gh', 'api', '-H', 'Accept: application/octet-stream',
                            f'repos/{self.repository}/releases/assets/{asset_id}'],
                           stdout=stream, stderr=subprocess.PIPE, check=True, timeout=120)

    def upload(self, tag, path):
        # A concurrent uploader may win; caller independently re-reads and checks.
        result = subprocess.run(['gh', 'release', 'upload', tag, str(path),
                                 '--repo', self.repository], capture_output=True, timeout=120)
        return result.returncode == 0


def reconcile(client, *, dist: Path, version: str, expected_sha: str) -> dict:
    """Verify all existing expected assets before any create-only upload.

    Same tag/commit with different compressed bytes is a conflict, even when the
    unpacked Python source might be equivalent. Unrelated attachments are kept.
    """
    if not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
        raise ReleaseAssetError('invalid_commit')
    dist = Path(dist)
    expected = local_assets(dist, version)
    tag = 'v' + version
    release_id = None
    uploaded = []

    def snapshot():
        nonlocal release_id
        sha, identity, assets = client.snapshot(tag)
        if sha != expected_sha:
            raise ReleaseAssetError('release_target_changed')
        if type(identity) is not int or identity <= 0 or not isinstance(assets, list):
            raise ReleaseAssetError('invalid_release_metadata')
        if release_id is None:
            release_id = identity
        if release_id != identity:
            raise ReleaseAssetError('release_identity_changed')
        found = {}
        for item in assets:
            if not isinstance(item, dict):
                raise ReleaseAssetError('invalid_release_metadata')
            name = item.get('name')
            if not isinstance(name, str):
                raise ReleaseAssetError('invalid_release_metadata')
            if name not in expected:
                continue
            if name in found:
                raise ReleaseAssetError('duplicate_release_asset')
            if (type(item.get('id')) is not int or item['id'] <= 0
                    or type(item.get('size')) is not int or item['size'] < 0
                    or item.get('state') != 'uploaded'):
                raise ReleaseAssetError('incomplete_release_asset')
            found[name] = (item['id'], item['size'])
        return found

    def verify(found):
        for name, (asset_id, size) in found.items():
            if size != expected[name][0]:
                raise ReleaseAssetError('published_asset_conflict')
            with tempfile.TemporaryDirectory(prefix='memleaf-release-check-') as temp:
                target = Path(temp) / 'asset'
                client.download(asset_id, target)
                if _fingerprint(target) != expected[name]:
                    raise ReleaseAssetError('published_asset_conflict')
        if local_assets(dist, version) != expected:
            raise ReleaseAssetError('local_assets_changed')

    initial = snapshot()
    verify(initial)  # Never upload one missing asset before checking the others.
    for name in expected:
        observed = snapshot()
        verify(observed)
        if name in observed:
            continue
        # Verify local bytes again before handing a path to the CLI.
        if _fingerprint(dist / name) != expected[name]:
            raise ReleaseAssetError('local_assets_changed')
        succeeded = client.upload(tag, dist / name)
        after = snapshot()
        if name not in after:
            raise ReleaseAssetError('release_upload_unconfirmed')
        verify(after)  # Includes race/unknown upload outcomes; never delete/retry.
        if succeeded:
            uploaded.append(name)
    final = snapshot()
    if set(final) != set(expected):
        raise ReleaseAssetError('release_incomplete')
    verify(final)
    if snapshot() != final:
        raise ReleaseAssetError('release_assets_changed')
    return {'status': 'verified', 'release_id': release_id, 'tag': tag,
            'commit': expected_sha, 'verified_assets': list(expected),
            'uploaded_assets': uploaded, 'replacement_allowed': False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository', required=True)
    p.add_argument('--version', required=True)
    p.add_argument('--commit', required=True)
    p.add_argument('--dist', type=Path, required=True)
    args = p.parse_args(argv)
    try:
        result = reconcile(GitHubCLI(args.repository), dist=args.dist,
                           version=args.version, expected_sha=args.commit)
    except (ReleaseAssetError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        code = str(error) if isinstance(error, ReleaseAssetError) else 'release_verification_failed'
        print(json.dumps({'status': 'failed', 'code': code,
                          'replacement_allowed': False, 'remote_outcome': 'recheck_required'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
