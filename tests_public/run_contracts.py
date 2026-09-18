"""Run the complete public suite with verifiable import and discovery provenance.

CI helper, not a Memleaf service or a real-model acceptance certificate.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import time
import unittest


def test_ids(suite):
    result = []
    for entry in suite:
        if isinstance(entry, unittest.TestSuite):
            result.extend(test_ids(entry))
        else:
            result.append(entry.id())
    return sorted(result)


def inventory_digest(ids):
    return hashlib.sha256(json.dumps(ids, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def verified_package(package_file: str, expected: Path) -> bool:
    return Path(package_file).resolve().parent == expected.resolve()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tests', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--expected', type=Path, help='build-job test inventory')
    args = parser.parse_args(argv)
    tests = args.tests.resolve(strict=True)
    if args.source_root is not None:
        source = args.source_root.resolve(strict=True)
        sys.path.insert(0, str(source))
        os.environ['PYTHONPATH'] = str(source)  # propagated to crash/restart tests
        expected_package = source / 'memleaf'
        mode = 'source'
    else:
        import sysconfig
        if sys.prefix == sys.base_prefix:
            parser.error('installed tests require a dedicated virtual environment')
        os.environ.pop('PYTHONPATH', None)
        expected_package = Path(sysconfig.get_path('purelib')) / 'memleaf'
        mode = 'installed'
    import memleaf
    if not verified_package(memleaf.__file__, expected_package):
        raise RuntimeError('unexpected_memleaf_import_root')
    if mode == 'installed' and importlib.metadata.version('memleaf') != memleaf.__version__:
        raise RuntimeError('installed_version_mismatch')
    # Provider imports need the host base API; its dedicated tests supply that
    # interface stub. Artifact hashes independently check all packaged resources.
    suite = unittest.defaultTestLoader.discover(str(tests), pattern='test_*.py')
    ids = test_ids(suite)
    digest = inventory_digest(ids)
    expected = json.loads(args.expected.read_text(encoding='utf-8')) if args.expected else None
    inventory_matches = bool(ids) and (expected is None or
        (len(ids) == expected['tests'] and digest == expected['inventory_sha256']))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    import_errors = []
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, '__file__', None)
        if (name == 'memleaf' or name.startswith('memleaf.')) and filename:
            if not Path(filename).resolve().is_relative_to(expected_package.resolve()):
                import_errors.append(name)
    passed = (inventory_matches and not import_errors and result.wasSuccessful()
              and result.testsRun == len(ids) and not result.skipped and not result.expectedFailures)
    report = {
        'schema_version': 1, 'status': 'passed' if passed else 'failed', 'mode': mode,
        'tests': result.testsRun, 'discovered': len(ids), 'inventory_sha256': digest,
        'inventory_matches': inventory_matches, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': [{'id': t.id(), 'reason': r} for t, r in result.skipped],
        'expected_failures': [t.id() for t, _ in result.expectedFailures],
        'unexpected_successes': [t.id() for t in result.unexpectedSuccesses],
        'import_errors': import_errors, 'package_root': str(expected_package.resolve()),
        'package_version': memleaf.__version__, 'python': platform.python_version(),
        'system': platform.system(), 'machine': platform.machine(),
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'semantic_acceptance': 'not_run', 'installed_hermes_acceptance': 'not_run',
        'switch_authorized': False,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'tests', 'mode', 'system', 'python')}, sort_keys=True))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
