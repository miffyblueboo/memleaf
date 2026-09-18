"""Build-job artifact binding and isolated wheel/sdist installation verification.

Only synthetic public test assets are used. No Vault, credential or release API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

MAX_BYTES = 256 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_FILES = 10000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    name = name.rstrip('/')
    parts = name.split('/')
    if (not name or name.startswith('/') or '\\' in name or ':' in name
            or any(p in ('', '.', '..') for p in parts)):
        raise ValueError('invalid_archive_name')
    return name


def record(target, name, data):
    if name in target:
        raise ValueError('duplicate_archive_name')
    if len(data) > MAX_FILE:
        raise ValueError('archive_file_too_large')
    target[name] = data
    if len(target) > MAX_FILES or sum(map(len, target.values())) > MAX_BYTES:
        raise ValueError('archive_too_large')


def archive_files(path):
    result = {}
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_FILES:
                raise ValueError('archive_too_large')
            for entry in archive.infolist():
                name = safe_name(entry.filename)
                if entry.is_dir():
                    continue
                if entry.file_size > MAX_FILE or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('invalid_archive_member')
                record(result, name, archive.read(entry))
    else:
        with tarfile.open(path, 'r:gz') as archive:
            for index, entry in enumerate(archive):
                if index >= MAX_FILES:
                    raise ValueError('archive_too_large')
                name = safe_name(entry.name)
                if entry.isdir():
                    continue
                if not entry.isfile() or entry.size > MAX_FILE:
                    raise ValueError('invalid_archive_member')
                stream = archive.extractfile(entry)
                if stream is None:
                    raise ValueError('missing_archive_member')
                record(result, name, stream.read(MAX_FILE + 1))
        roots = {PurePosixPath(name).parts[0] for name in result}
        if len(roots) != 1 or any('/' not in name for name in result):
            raise ValueError('invalid_sdist_root')
        result = {name.split('/', 1)[1]: data for name, data in result.items()}
    return result


def package_files(files, *, sdist=False):
    prefix = 'src/memleaf/' if sdist else 'memleaf/'
    values = {name[len(prefix):]: digest(data) for name, data in files.items() if name.startswith(prefix)}
    if '__init__.py' not in values or not values:
        raise ValueError('package_missing')
    return values


def assert_same_package(wheel_files, source_files):
    wheel = package_files(wheel_files)
    source = package_files(source_files, sdist=True)
    if wheel != source:
        raise ValueError('distribution_payload_mismatch')
    for required in ('hermes_provider/plugin.yaml', 'hermes_provider/README.md',
                     'hermes_provider/_provider.py', 'hermes_provider/evidence_budget.py'):
        if required not in wheel:
            raise ValueError('provider_resource_missing')
    return wheel


def suite_assets(files):
    return {name: data for name, data in files.items()
            if name.startswith(('tests_public/', 'examples/'))}


def file_manifest(path):
    return {'name': path.name, 'bytes': path.stat().st_size, 'sha256': digest(path.read_bytes())}


def single(path, pattern):
    matches = list(path.glob(pattern))
    if len(matches) != 1:
        raise ValueError('ambiguous_distribution')
    return matches[0]


def manifest(dist, source_report, revision):
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('invalid_commit')
    wheel, sdist = single(dist, '*.whl'), single(dist, '*.tar.gz')
    wf, sf = archive_files(wheel), archive_files(sdist)
    payload = assert_same_package(wf, sf)
    report = json.loads(source_report.read_text(encoding='utf-8'))
    if report['status'] != 'passed' or not report['tests'] or report['skipped']:
        raise ValueError('source_tests_not_passed')
    if not any(n.startswith('tests_public/test_') for n in sf):
        raise ValueError('sdist_tests_missing')
    return {'schema_version': 1, 'commit': revision, 'source_report': report,
            'wheel': file_manifest(wheel), 'sdist': file_manifest(sdist),
            'test_dependencies': [file_manifest(p) for p in sorted((dist/'test-dependencies').glob('*.whl'))],
            'package_files': payload,
            'test_assets': {n: digest(v) for n, v in suite_assets(sf).items()},
            'switch_authorized': False}


def verify_binding(dist, expected_hash, revision):
    raw = (dist/'verification-manifest.json').read_bytes()
    if len(raw) > MAX_FILE or digest(raw) != expected_hash:
        raise ValueError('artifact_manifest_mismatch')
    value = json.loads(raw)
    if value.get('schema_version') != 1 or value.get('commit') != revision:
        raise ValueError('artifact_commit_mismatch')
    for kind in ('wheel', 'sdist'):
        name = safe_name(value[kind]['name'])
        if '/' in name or file_manifest(dist/name) != value[kind]:
            raise ValueError('artifact_bytes_mismatch')
    for dependency in value.get('test_dependencies', []):
        name = safe_name(dependency['name'])
        if '/' in name or not name.startswith('tzdata-') or file_manifest(dist/'test-dependencies'/name) != dependency:
            raise ValueError('test_dependency_mismatch')
    sf, wf = archive_files(dist/value['sdist']['name']), archive_files(dist/value['wheel']['name'])
    if assert_same_package(wf, sf) != value['package_files']:
        raise ValueError('package_manifest_mismatch')
    if {n: digest(v) for n, v in suite_assets(sf).items()} != value['test_assets']:
        raise ValueError('suite_manifest_mismatch')
    return value, sf


def write_files(root, files):
    root.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        path = root / safe_name(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)


def clean_env(home):
    # The test harness controls encoding; dedicated tests override it explicitly.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('PYTHON', 'MEMLEAF', 'OPENAI_', 'DEEPSEEK_', 'ANTHROPIC_', 'GEMINI_'))}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1')
    return env


def command(args, cwd, env, log):
    with log.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps([str(a) for a in args], ensure_ascii=True) + '\n')
        stream.flush()
        proc = subprocess.run([str(a) for a in args], cwd=cwd, env=env,
                              stdout=stream, stderr=subprocess.STDOUT, timeout=900)
    if proc.returncode:
        raise RuntimeError('verification_command_failed: ' + log.name)


def verify(dist, output, expected_hash, revision):
    value, sf = verify_binding(dist, expected_hash, revision)
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'commit': revision, 'status': 'running',
              'manifest_sha256': expected_hash, 'system': platform.system(),
              'python': platform.python_version(), 'machine': platform.machine(),
              'stages': {}, 'artifact_checks': {}, 'semantic_acceptance': 'not_run',
              'installed_hermes_acceptance': 'not_run', 'switch_authorized': False}
    try:
        with tempfile.TemporaryDirectory(prefix='ml-installed-') as tmp:
            root = Path(tmp)
            env = clean_env(root/'home'); (root/'home').mkdir()
            source = root/'sdist'
            write_files(source, sf)
            fixtures = root/'fixtures'
            write_files(fixtures, suite_assets(sf))
            runner = fixtures/'tests_public/run_contracts.py'
            expected = root/'expected.json'
            expected.write_text(json.dumps(value['source_report']), encoding='utf-8')
            rebuilt = root/'rebuilt'
            command([sys.executable, '-I', '-c',
                     'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])',
                     rebuilt], source, env, output/'sdist-build.log')
            rebuilt_wheel = single(rebuilt, '*.whl')
            if package_files(archive_files(rebuilt_wheel)) != value['package_files']:
                raise ValueError('rebuilt_payload_mismatch')
            # No source package remains in the execution workspace. Even subprocesses
            # spawned by crash tests can only import the installed package.
            shutil.rmtree(source)
            for stage, wheel in (('wheel', dist/value['wheel']['name']), ('sdist', rebuilt_wheel)):
                environment = root/(stage+'-env')
                venv.EnvBuilder(with_pip=True).create(environment)
                scripts = environment/('Scripts' if os.name == 'nt' else 'bin')
                python = scripts/('python.exe' if os.name == 'nt' else 'python')
                stage_env = {**env, 'VIRTUAL_ENV': str(environment),
                             'PATH': str(scripts)+os.pathsep+env.get('PATH', '')}
                dependencies = [dist/'test-dependencies'/d['name'] for d in value.get('test_dependencies', [])]
                if dependencies:
                    command([python, '-I', '-m', 'pip', 'install', '--no-deps', '--no-index', *dependencies],
                            fixtures, stage_env, output/(stage+'-test-dependencies.log'))
                command([python, '-I', '-m', 'pip', 'install', '--no-deps', '--no-index', wheel],
                        fixtures, stage_env, output/(stage+'-install.log'))
                stage_report = output/(stage+'-tests.json')
                command([python, '-I', '-X', 'utf8', runner, '--tests', fixtures/'tests_public', '--expected', expected,
                         '--report', stage_report], fixtures, stage_env, output/(stage+'-tests.log'))
                report['stages'][stage] = json.loads(stage_report.read_text(encoding='utf-8'))
                command([python, '-I', '-X', 'utf8', fixtures/'tests_public/platform_smoke.py',
                         '--scripts', scripts], fixtures, stage_env, output/(stage+'-mcp-smoke.log'))
                # Invoke actual generated console entry points, not module substitutes.
                for entry in ('memleaf', 'memleaf-mcp'):
                    executable = scripts/(entry+'.exe' if os.name == 'nt' else entry)
                    command([executable, '--version'], fixtures, stage_env, output/(stage+'-'+entry+'.log'))
                command([python, '-I', '-c',
                    'from pathlib import Path; import memleaf, hashlib, json, sys; '
                    'r=Path(memleaf.__file__).parent; expected=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); '
                    'actual={p.relative_to(r).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() '
                    'for p in r.rglob("*") if p.is_file() and "__pycache__" not in p.parts}; '
                    'assert actual==expected["package_files"], "installed_payload_mismatch"',
                    dist/'verification-manifest.json'], fixtures, stage_env, output/(stage+'-bytes.log'))
                report['artifact_checks'][stage] = {'installed_payload': 'passed', 'console_scripts': 'passed',
                    'mcp_restart_utf8': 'passed', 'packaged_transport_with_host_stub': 'passed'}
            report['status'] = 'passed'
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        report['status'] = 'failed'
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        for stage in ('wheel', 'sdist'):
            stage_report = output/(stage+'-tests.json')
            if stage_report.is_file():
                try:
                    report['stages'][stage] = json.loads(stage_report.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    report['status'] = 'failed'
                    report['stages'][stage] = {'status': 'unavailable'}
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=True, indent=2)+'\n', encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('manifest')
    prepare.add_argument('--dist', type=Path, required=True)
    prepare.add_argument('--source-report', type=Path, required=True)
    prepare.add_argument('--commit', required=True)
    run = sub.add_parser('verify')
    run.add_argument('--dist', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--manifest-sha256', required=True)
    run.add_argument('--commit', required=True)
    args = parser.parse_args(argv)
    if args.action == 'manifest':
        value = manifest(args.dist.resolve(), args.source_report, args.commit)
        raw = (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)+'\n').encode()
        (args.dist/'verification-manifest.json').write_bytes(raw)
        print(digest(raw))
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                stream.write('manifest_sha256='+digest(raw)+'\n')
        return 0
    return verify(args.dist.resolve(), args.output.resolve(), args.manifest_sha256, args.commit)


if __name__ == '__main__':
    raise SystemExit(main())
