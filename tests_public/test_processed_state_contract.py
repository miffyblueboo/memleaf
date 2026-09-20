"""Corrupt control authority must never be silently recreated by another route."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.process_common import _read_processed
from test_incremental_execution import Backend


class ProcessedStateReadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.s = Memleaf.initialize(Path(tmp.name) / 'vault')
        self.path = self.s.vault.processed_state_path
        self.path.unlink(missing_ok=True)

    def test_missing_and_minimal_legacy_state_can_be_read_without_writes(self):
        with self.assertRaises(ValueError):
            _read_processed(self.path)
        virgin = self.path.parent.parent.parent / "unused" / "processed.json"
        self.assertEqual(_read_processed(virgin)['version'], 1)
        self.assertFalse(self.path.exists())
        self.assertFalse(virgin.exists())
        raw = '{"sessions":{},"custom":{"preserve":true}}'
        self.path.write_text(raw)
        result = _read_processed(self.path)
        self.assertEqual(result['events'], {})
        self.assertEqual(result['custom'], {'preserve': True})
        self.assertEqual(self.path.read_text(), raw)

    def test_invalid_json_and_control_shapes_preserve_original_bytes(self):
        cases = ['{bad', '[]', 'null', '', '{"sessions":{},"sessions":{}}',
                 '{"x":NaN}', '{"version":2}', '{"version":true}',
                 '{"sessions":[]}', '{"sessions":{"private":"bad"}}',
                 '{"events":[]}', '{"events":{"x":null}}', '{"event_keys":[1]}']
        for raw in cases:
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises(ValueError):
                    _read_processed(self.path)
                self.assertEqual(self.path.read_text(), raw)

    def test_invalid_utf8_is_not_an_empty_ledger(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(UnicodeError):
            _read_processed(self.path)
        self.assertEqual(self.path.read_bytes(), b'\xff')

    def test_symlink_or_directory_is_not_first_use(self):
        target = self.path.with_name('other.json')
        target.write_text('{}')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            _read_processed(self.path)
        self.assertEqual(target.read_text(), '{}')
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(OSError):
            _read_processed(self.path)

    def test_ordinary_capture_cannot_reset_corrupt_state(self):
        raw = '{bad'
        self.path.write_text(raw)
        with self.assertRaises(ValueError):
            self.s.capture('hermes', 'session', 'turn', 'user', 'Remember this action.')
        self.assertEqual(self.path.read_text(), raw)
        self.assertFalse(self.s.vault.list_markdown('inbox'))

    def test_incremental_remember_preserves_corrupt_control_and_legacy_is_rejected(self):
        raw = '{"sessions":[]}'
        self.path.write_text(raw)

        backend = Backend()
        with self.assertRaises(ValueError):
            self.s.remember('A stable statement.', pipeline='incremental', model=backend,
                            intent_id='same-authorization')
        self.assertFalse(backend.calls)
        self.assertEqual(self.path.read_text(), raw)

        legacy = Backend()
        with self.assertRaisesRegex(ValueError, 'legacy_pipeline_removed'):
            self.s.remember('A stable statement.', pipeline='legacy', model=legacy,
                            intent_id='same-authorization')
        self.assertFalse(legacy.calls)
        self.assertEqual(self.path.read_text(), raw)
        self.assertFalse(self.s.vault.list_markdown('knowledge'))

    def test_nested_run_integrity_stays_with_the_run_validator(self):
        raw = json.dumps({'incremental_runs': {'wrong-key': {}}})
        self.path.write_text(raw)
        self.assertEqual(_read_processed(self.path)['incremental_runs'], {'wrong-key': {}})
        self.assertEqual(self.path.read_text(), raw)
