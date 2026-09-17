"""Test built distributions in fresh interpreters, not an editable checkout.

Developer/CI tool only. No model configuration, user Vault, install switch, or
publication is accepted. A successful report is platform/Core evidence only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import venv
import zipfile

VERSION = 1
PACKAGE = 'memleaf'
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(root: Path) -> dict[str, str]:
    """Bind all implementation/resources plus actual public tests and fixtures."""
    result = {}
    for area in ('src/memleaf', 'tests_public', 'examples'):
        for path in sorted((root / area).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    for name in ('pyproject.toml', 'MANIFEST.in', 'scripts/verify_distribution.py'):
        if (root / name).is_file():
            result[name] = digest((root / name).read_bytes())
    if not any(p.startswith('tests_public/test_') for p in result):
        raise ValueError('no_public_tests')
    return result


def read_artifacts(dist: Path) -> tuple[Path, Path]:
    wheels, sources = sorted(dist.glob('memleaf-*.whl')), sorted(dist.glob('memleaf-*.tar.gz'))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError('expected_exactly_one_wheel_and_sdist')
    return wheels[0], sources[0]


def archive_files(path: Path) -> dict[str, bytes]:
    """Read bounded regular artifact entries; do not extract paths from input."""
    result: dict[str, bytes] = {}
    total = 0
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            entries = [(entry.filename, entry.file_size, entry) for entry in archive.infolist() if not entry.is_dir()]
            for name, size, entry in entries:
                total += size
                if total > MAX_ARCHIVE_BYTES or name in result:
                    raise ValueError('invalid_artifact_members')
                result[name] = archive.read(entry)
    else:
        with tarfile.open(path, 'r:gz') as archive:
            for entry in archive:
                if entry.isdir():
                    continue
                total += entry.size
                if not entry.isfile() or total > MAX_ARCHIVE_BYTES or entry.name in result:
                    raise ValueError('invalid_artifact_members')
                stream = archive.extractfile(entry)
                if stream is None:
                    raise ValueError('invalid_artifact_members')
                result[entry.name] = stream.read()
    return result


def validate_payloads(root: Path, wheel: Path, sdist: Path) -> dict[str, str]:
    expected = identity(root)
    wfiles, sfiles = archive_files(wheel), archive_files(sdist)
    prefixes = {name.split('/')[0] for name in sfiles}
    if len(prefixes) != 1:
        raise ValueError('invalid_sdist_root')
    prefix = next(iter(prefixes)) + '/'
    sfiles = {name[len(prefix):]: content for name, content in sfiles.items()}
    source_package = {name[len('src/'):]: h for name, h in expected.items() if name.startswith('src/memleaf/')}
    actual_wheel = {name: digest(content) for name, content in wfiles.items() if name.startswith('memleaf/')}
    if source_package != actual_wheel:
        raise ValueError('wheel_source_mismatch')
    # MANIFEST may deliberately omit some general examples. Bind every test and
    # every packaged fixture; acceptance's public suite is specifically required.
    required = {name: h for name, h in expected.items() if name.startswith(('src/memleaf/', 'tests_public/'))}
    required['pyproject.toml'] = expected['pyproject.toml']
    for name, h in expected.items():
        if name == 'examples/incremental_acceptance.json' or name.startswith('scripts/'):
            required[name] = h
    if any(digest(sfiles.get(name, b'')) != h for name, h in required.items()):
        raise ValueError('sdist_source_or_tests_mismatch')
    actual_python = {p for p in sfiles if p.startswith(('src/memleaf/', 'tests_public/')) and p.endswith('.py')}
    if actual_python != {p for p in required if p.startswith(('src/memleaf/', 'tests_public/')) and p.endswith('.py')}:
        raise ValueError('sdist_extra_implementation_or_test')
    return expected


def prepare(root: Path, dist: Path, commit: str) -> dict:
    wheel, sdist = read_artifacts(dist)
    payload = validate_payloads(root, wheel, sdist)
    version = tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    result = {'schema_version': VERSION, 'commit': commit, 'package_version': version,
              'artifacts': {p.name: digest(p.read_bytes()) for p in (wheel, sdist)},
              'source_identity': payload}
    target = dist/'verification-manifest.json'
    if target.exists():
        raise ValueError('manifest_already_exists')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return result


def command(args: list[str], *, cwd: Path, log: Path, env: dict[str, str], timeout: int = 600) -> None:
    with log.open('wb') as output:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'command_failed:{log.name}:{result.returncode}')


def test_install(python: Path, stage: Path, report: Path, env: dict[str, str], version: str) -> dict:
    """-I defeats checkout/user-site imports; children inherit no PYTHONPATH."""
    probe = stage/'_installed_probe.py'
    probe.write_text('''import importlib.metadata, json, pathlib, sys, memleaf
p=pathlib.Path(memleaf.__file__).resolve()
assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()), (str(p),sys.prefix)
assert memleaf.__version__ == importlib.metadata.version("memleaf") == sys.argv[1]
print(json.dumps({"package_version":memleaf.__version__,"import_from_environment":True}))
''', encoding='utf-8')
    command([str(python), '-I', str(probe), version], cwd=stage, log=report/'import.log', env=env)
    # Run the exact copied public suite and preserve skips as evidence, not a
    # "fully native" claim. Missing tests cannot silently become a green zero.
    runner = stage/'_run_public.py'
    runner.write_text('''import json, pathlib, sys, unittest
suite=unittest.defaultTestLoader.discover(str(pathlib.Path(__file__).parent/'tests_public'),pattern='test_*.py')
count=suite.countTestCases()
if not count: raise SystemExit('no_tests_discovered')
r=unittest.TextTestRunner(verbosity=2).run(suite)
pathlib.Path(sys.argv[1]).write_text(json.dumps({"tests":r.testsRun,"failures":len(r.failures),"errors":len(r.errors),"skips":[{"test":str(t),"reason":why} for t,why in r.skipped]}),encoding='utf-8')
raise SystemExit(0 if r.wasSuccessful() and r.testsRun==count else 1)
''', encoding='utf-8')
    command([str(python), '-I', str(runner), str(report/'tests.json')], cwd=stage, log=report/'tests.log', env=env)
    scripts = python.parent
    for name in ('memleaf', 'memleaf-mcp'):
        entry = scripts/(name+'.exe' if os.name == 'nt' else name)
        command([str(entry), '--version'], cwd=stage, log=report/(name+'.log'), env=env)
        if (report/(name+'.log')).read_text(encoding='utf-8').strip() != version:
            raise ValueError('entrypoint_version_mismatch')
    return json.loads((report/'tests.json').read_text(encoding='utf-8'))


def verify(root: Path, dist: Path, output: Path, commit: str) -> dict:
    manifest = json.loads((dist/'verification-manifest.json').read_text(encoding='utf-8'))
    wheel, sdist = read_artifacts(dist)
    if (manifest.get('schema_version') != VERSION or manifest.get('commit') != commit
            or manifest.get('artifacts') != {p.name: digest(p.read_bytes()) for p in (wheel, sdist)}
            or manifest.get('source_identity') != validate_payloads(root, wheel, sdist)):
        raise ValueError('artifact_provenance_mismatch')
    output.mkdir(parents=False, exist_ok=False)
    summary = {'schema_version': VERSION, 'commit': commit, 'platform': platform.system(),
               'python': platform.python_version(), 'artifacts': manifest['artifacts'], 'status':'running',
               'model_calls':0, 'semantic_status':'not_run', 'host_installation':'not_run',
               'switch_authorized':False, 'results':{}}
    started = time.monotonic()
    env = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'}
           and not k.startswith('MEMLEAF_')}
    # Protocol streams are explicitly UTF-8 in Core; do not force UTF-8 mode
    # here and accidentally conceal implicit filesystem-decoding bugs.
    try:
        with tempfile.TemporaryDirectory(prefix='memleaf artifact ') as temporary:
            temp = Path(temporary)
            for kind in ('wheel', 'sdist'):
                report = output/kind; report.mkdir()
                stage = temp/kind; stage.mkdir()
                for name in ('tests_public','examples','scripts'):
                    shutil.copytree(root/name, stage/name, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
                install = wheel
                if kind == 'sdist':
                    rebuilt = stage/'rebuilt';rebuilt.mkdir()
                    command([sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation',
                             '--no-index','--wheel-dir',str(rebuilt),str(sdist)],
                            cwd=stage, log=report/'build.log', env=env)
                    install = next(rebuilt.glob('memleaf-*.whl'))
                    original = {k:v for k,v in archive_files(wheel).items() if k.startswith('memleaf/')}
                    native = {k:v for k,v in archive_files(install).items() if k.startswith('memleaf/')}
                    if original != native:
                        raise ValueError('native_sdist_build_changed_implementation')
                venvpath = stage/'isolated environment'
                venv.EnvBuilder(with_pip=True).create(venvpath)
                python = venvpath/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
                command([str(python),'-I','-m','pip','install','--no-deps','--no-index',str(install)],
                        cwd=stage, log=report/'install.log', env=env)
                summary['results'][kind] = test_install(python,stage,report,env,manifest['package_version'])
            if summary['results']['wheel']['tests'] != summary['results']['sdist']['tests']:
                raise ValueError('test_count_mismatch')
        summary['status']='passed_with_skips' if any(r['skips'] for r in summary['results'].values()) else 'passed'
    except BaseException:
        summary['status']='failed'
        raise
    finally:
        summary['seconds']=time.monotonic()-started
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--dist',type=Path,required=True)
    parser.add_argument('--commit',required=True)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare',action='store_true')
    action.add_argument('--verify',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.verify and args.output is None:parser.error('--verify requires --output')
    root,dist=args.source.resolve(strict=True),args.dist.resolve(strict=True)
    result=prepare(root,dist,args.commit) if args.prepare else verify(root,dist,args.output.resolve(),args.commit)
    print(json.dumps(result if args.verify else {'status':'prepared','artifacts':result['artifacts']},ensure_ascii=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
