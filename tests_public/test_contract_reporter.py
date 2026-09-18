"""Negative tests for the test gate itself, in independent Python processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest

import memleaf


class ContractReporterTests(unittest.TestCase):
    def report(self, body, expected=None):
        with tempfile.TemporaryDirectory(prefix='ml-reporter-') as tmp:
            root = Path(tmp)
            tests = root / 'checks'
            tests.mkdir()
            (tests / 'test_gate_fixture.py').write_text(
                'import unittest\n' + body, encoding='utf-8')
            result = root / 'result.json'
            command = [sys.executable, '-I', '-X', 'utf8',
                       str(Path(__file__).with_name('run_contracts.py')),
                       '--tests', str(tests), '--report', str(result)]
            if Path(memleaf.__file__).resolve().parent != (Path(sysconfig.get_path('purelib')) / 'memleaf').resolve():
                command += ['--source-root', str(Path(memleaf.__file__).parent.parent)]
            if expected is not None:
                path = root / 'expected.json'
                path.write_text(json.dumps(expected), encoding='utf-8')
                command += ['--expected', str(path)]
            env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
            child = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=30)
            self.assertTrue(result.is_file(), child.stderr.decode('utf-8', errors='replace'))
            return child.returncode, json.loads(result.read_text(encoding='utf-8'))

    def test_passing_suite_retains_count_and_inventory(self):
        code, value = self.report('class Checks(unittest.TestCase):\n def test_ok(self): pass\n')
        self.assertEqual(code, 0)
        self.assertEqual(value['tests'], 1)
        self.assertTrue(value['inventory_matches'])
        self.assertFalse(value['switch_authorized'])

    def test_skip_is_not_success(self):
        code, value = self.report("class Checks(unittest.TestCase):\n @unittest.skip('unavailable primitive')\n def test_skip(self): pass\n")
        self.assertEqual(code, 1)
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(len(value['skipped']), 1)

    def test_expected_failure_is_not_success(self):
        code, value = self.report('class Checks(unittest.TestCase):\n @unittest.expectedFailure\n def test_failure(self): self.fail()\n')
        self.assertEqual(code, 1)
        self.assertEqual(len(value['expected_failures']), 1)

    def test_wrong_inventory_is_not_success(self):
        code, value = self.report('class Checks(unittest.TestCase):\n def test_ok(self): pass\n',
                                  {'tests': 2, 'inventory_sha256': '0' * 64})
        self.assertEqual(code, 1)
        self.assertFalse(value['inventory_matches'])

    def test_import_rebound_outside_installation_is_not_success(self):
        code, value = self.report("class Checks(unittest.TestCase):\n def test_changed(self):\n  import memleaf\n  memleaf.__file__ = '/different/memleaf/__init__.py'\n")
        self.assertEqual(code, 1)
        self.assertIn('memleaf', value['import_errors'])
