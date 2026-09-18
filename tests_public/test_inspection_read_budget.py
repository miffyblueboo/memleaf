"""Inspection limits are ceilings, not allocations; changed files stay errors."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from memleaf import inspection


class InspectionReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memleaf-snapshot-budget-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "库 snapshot"
        self.root.mkdir()
        (self.root / "config.yaml").write_bytes(b"{}\n")
        (self.root / "knowledge").mkdir()
        self.path = self.root / "knowledge" / "record.md"
        self.path.write_bytes(b"hello\r\n")
        self.total = 3 + len(self.path.read_bytes())

    @contextmanager
    def streams(self, before=None, after=None):
        """Observe real regular-file reads; race callbacks target only the record."""
        original = os.fdopen
        reads = []
        target = self.path.stat()

        class Stream:
            def __init__(self, fd, *args, **kwargs):
                self.raw = original(fd, *args, **kwargs)
                stamp = os.fstat(fd)
                self.target = (stamp.st_dev, stamp.st_ino) == (target.st_dev, target.st_ino)
            def __enter__(self):
                self.raw.__enter__()
                return self
            def __exit__(self, *args):
                return self.raw.__exit__(*args)
            def fileno(self):
                return self.raw.fileno()
            def read(self, size):
                reads.append((self.target, size))
                if self.target and before:
                    before()
                data = self.raw.read(size)
                if self.target and after:
                    after()
                return data

        with patch.object(inspection.os, "fdopen", Stream):
            yield reads

    def assert_changed(self):
        with self.assertRaisesRegex(inspection.InspectionError, "changed"):
            inspection._snapshot(self.root)

    def test_small_files_request_only_observed_bytes_and_sentinel(self):
        with self.streams() as reads:
            result = inspection._snapshot(self.root)
        self.assertEqual(result, {"config.yaml": b"{}\n", "knowledge/record.md": b"hello\r\n"})
        self.assertEqual(reads, [(False, 4), (True, 8)])

    def test_empty_file_is_checked_for_growth_not_skipped(self):
        self.path.write_bytes(b"")
        with self.streams() as reads:
            result = inspection._snapshot(self.root)
        self.assertEqual(result["knowledge/record.md"], b"")
        self.assertEqual(reads[-1], (True, 1))

    def test_exact_total_limit_remains_inclusive(self):
        with patch.object(inspection, "MAX_SNAPSHOT_BYTES", self.total):
            result = inspection._snapshot(self.root)
        self.assertEqual(sum(map(len, result.values())), self.total)

    def test_over_budget_is_rejected_before_reading_record(self):
        with self.streams() as reads, patch.object(inspection, "MAX_SNAPSHOT_BYTES", self.total - 1):
            with self.assertRaises(inspection.InspectionError):
                inspection._snapshot(self.root)
        self.assertFalse(any(target for target, _ in reads))

    def test_growth_between_stat_and_open_is_not_accepted(self):
        original = os.open
        def opened(path, *args, **kwargs):
            if Path(path) == self.path:
                self.path.write_bytes(b"longer record\r\n")
            return original(path, *args, **kwargs)
        with patch.object(inspection.os, "open", side_effect=opened):
            self.assert_changed()

    def test_truncation_between_stat_and_open_is_not_accepted(self):
        original = os.open
        def opened(path, *args, **kwargs):
            if Path(path) == self.path:
                self.path.write_bytes(b"h")
            return original(path, *args, **kwargs)
        with patch.object(inspection.os, "open", side_effect=opened):
            self.assert_changed()

    def test_growth_during_read_is_not_a_complete_prefix(self):
        with self.streams(before=lambda: self.path.write_bytes(b"hello\r\nappended")):
            self.assert_changed()

    def test_truncation_during_read_is_not_a_complete_snapshot(self):
        with self.streams(before=lambda: self.path.write_bytes(b"h")):
            self.assert_changed()

    def test_growth_after_read_is_detected_by_descriptor_recheck(self):
        with self.streams(after=lambda: self.path.write_bytes(b"hello\r\nappended")):
            self.assert_changed()

    def test_equal_size_edit_after_read_is_not_success(self):
        stamp = self.path.stat()
        def edit():
            self.path.write_bytes(b"other\r\n")
            os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
        with self.streams(after=edit):
            self.assert_changed()

    def test_empty_file_growing_during_read_is_not_empty_success(self):
        self.path.write_bytes(b"")
        with self.streams(before=lambda: self.path.write_bytes(b"new")):
            self.assert_changed()

    def test_second_scan_rereads_all_bytes(self):
        with self.streams() as reads:
            first = inspection._checked_snapshot(self.root)
        self.assertEqual(reads, [(False, 4), (True, 8)] * 2)
        self.assertEqual(first["knowledge/record.md"], b"hello\r\n")

    def test_same_size_same_mtime_edit_between_scans_still_fails(self):
        original = inspection._snapshot
        stamp = self.path.stat()
        calls = 0
        def snapshot(root):
            nonlocal calls
            result = original(root)
            calls += 1
            if calls == 1:
                self.path.write_bytes(b"other\r\n")
                os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            return result
        with patch.object(inspection, "_snapshot", side_effect=snapshot):
            with self.assertRaises(inspection.InspectionError):
                inspection._checked_snapshot(self.root)
        self.assertEqual(calls, 2)

    def test_binary_bytes_and_fingerprint_do_not_change(self):
        self.path.write_bytes(b"\x00\xff\xfe\r\n")
        expected = {"config.yaml": b"{}\n", "knowledge/record.md": b"\x00\xff\xfe\r\n"}
        observed = inspection._checked_snapshot(self.root)
        self.assertEqual(observed, expected)
        self.assertEqual(inspection._fingerprint(observed), inspection._fingerprint(expected))

    def test_read_failure_is_not_an_empty_file_or_omission(self):
        def fail():
            raise OSError("injected read failure")
        with self.streams(before=fail):
            with self.assertRaises(OSError):
                inspection._snapshot(self.root)

    def test_many_small_files_do_not_multiply_the_vault_allocation(self):
        for i in range(80):
            (self.root / "knowledge" / f"small-{i:03}.dat").write_bytes(bytes([i]))
        with self.streams() as reads:
            result = inspection._snapshot(self.root)
        self.assertEqual(len(reads), 82)
        self.assertEqual(sum(size for _, size in reads), sum(map(len, result.values())) + len(result))
