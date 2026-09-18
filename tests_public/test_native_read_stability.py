"""Stable path/descriptor observations must not equate different ctime meanings."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from memleaf import incremental_native as native


def stamp_copy(stamp, **overrides):
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')
    values = {name: getattr(stamp, name) for name in fields}
    values.update(overrides)
    return SimpleNamespace(**values)


class NativeReadStabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memleaf-native-observation-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '原生 notes.md'
        self.payload = '真实内容\r\n'.encode('utf-8')
        self.path.write_bytes(self.payload)

    @contextmanager
    def observed(self, *, path_first=None, path_last=None, fd_first=None, fd_last=None):
        """Real file bytes; modify only the selected metadata observation."""
        original_lstat, original_fstat = Path.lstat, os.fstat
        path_calls, fd_calls = 0, 0
        def lstat(path, *args, **kwargs):
            nonlocal path_calls
            result = original_lstat(path, *args, **kwargs)
            if path == self.path:
                delta = path_first if path_calls == 0 else path_last
                path_calls += 1
                return stamp_copy(result, **(delta or {}))
            return result
        def fstat(fd):
            nonlocal fd_calls
            result = original_fstat(fd)
            delta = fd_first if fd_calls == 0 else fd_last
            fd_calls += 1
            return stamp_copy(result, **(delta or {}))
        with patch.object(Path, 'lstat', lstat), patch.object(native.os, 'fstat', fstat):
            yield

    @contextmanager
    def reader(self, *, before=None, after=None):
        original = Path.open
        sizes = []
        class Stream:
            def __init__(stream, raw):
                stream.raw = raw
            def __enter__(stream):
                stream.raw.__enter__()
                return stream
            def __exit__(stream, *args):
                return stream.raw.__exit__(*args)
            def fileno(stream):
                return stream.raw.fileno()
            def read(stream, size):
                sizes.append(size)
                if before:
                    before()
                data = stream.raw.read(size)
                if after:
                    after()
                return data
        def opened(path, *args, **kwargs):
            raw = original(path, *args, **kwargs)
            return Stream(raw) if path == self.path and args == ('rb',) else raw
        with patch.object(Path, 'open', opened):
            yield sizes

    def assert_changed(self):
        with self.assertRaisesRegex(ValueError, '^native_source_changed_during_read$'):
            native._read_file(self.path)

    def test_different_path_and_descriptor_ctime_is_not_a_content_change(self):
        with self.observed(path_first={'st_ctime_ns': 10}, path_last={'st_ctime_ns': 10},
                           fd_first={'st_ctime_ns': 20}, fd_last={'st_ctime_ns': 20}):
            self.assertEqual(native._read_file(self.path), self.payload)

    def test_descriptor_ctime_change_still_rejected(self):
        with self.observed(path_first={'st_ctime_ns': 10}, path_last={'st_ctime_ns': 10},
                           fd_first={'st_ctime_ns': 20}, fd_last={'st_ctime_ns': 21}):
            self.assert_changed()

    def test_path_ctime_change_still_rejected(self):
        with self.observed(path_first={'st_ctime_ns': 10}, path_last={'st_ctime_ns': 11},
                           fd_first={'st_ctime_ns': 20}, fd_last={'st_ctime_ns': 20}):
            self.assert_changed()

    def test_path_timestamp_change_is_not_masked_by_unchanged_handle(self):
        with self.observed(path_last={'st_mtime_ns': 123}):
            self.assert_changed()

    def test_handle_timestamp_change_is_not_masked_by_unchanged_path(self):
        with self.observed(fd_last={'st_mtime_ns': 123}):
            self.assert_changed()

    def test_mtime_disagreement_at_open_is_not_ignored(self):
        with self.observed(fd_first={'st_mtime_ns': 123}, fd_last={'st_mtime_ns': 123}):
            self.assert_changed()

    def test_identity_disagreement_at_open_is_not_ignored(self):
        with self.observed(fd_first={'st_ino': 0}, fd_last={'st_ino': 0}):
            self.assert_changed()

    def test_path_replacement_after_read_is_rejected(self):
        with self.observed(path_last={'st_ino': 0}):
            self.assert_changed()

    def test_handle_identity_change_is_rejected(self):
        with self.observed(fd_last={'st_ino': 0}):
            self.assert_changed()

    def test_nonregular_path_is_not_opened(self):
        with self.observed(path_first={'st_mode': stat.S_IFLNK}):
            with patch.object(Path, 'open', side_effect=AssertionError('must not open')):
                with self.assertRaisesRegex(ValueError, '^native_source_unsafe$'):
                    native._read_file(self.path)

    def test_nonregular_open_handle_is_rejected(self):
        with self.observed(fd_first={'st_mode': stat.S_IFIFO}):
            with self.assertRaisesRegex(ValueError, '^native_source_unsafe$'):
                native._read_file(self.path)

    def test_nonregular_final_path_is_rejected(self):
        with self.observed(path_last={'st_mode': stat.S_IFLNK}):
            self.assert_changed()

    def test_size_changed_at_open_is_rejected_before_allocation(self):
        with self.observed(fd_first={'st_size': native.MAX_NATIVE_BYTES + 1}):
            with self.reader() as sizes:
                self.assert_changed()
            self.assertEqual(sizes, [])

    def test_small_read_uses_observed_size_and_sentinel(self):
        with self.reader() as sizes:
            self.assertEqual(native._read_file(self.path), self.payload)
        self.assertEqual(sizes, [len(self.payload) + 1])

    def test_empty_file_still_reads_growth_sentinel(self):
        self.path.write_bytes(b'')
        with self.reader() as sizes:
            self.assertEqual(native._read_file(self.path), b'')
        self.assertEqual(sizes, [1])

    def test_growth_during_read_not_returned_as_valid_prefix(self):
        with self.reader(before=lambda: self.path.write_bytes(self.payload + b'longer')):
            self.assert_changed()

    def test_truncation_during_read_is_rejected(self):
        with self.reader(before=lambda: self.path.write_bytes(b'x')):
            self.assert_changed()

    def test_growth_after_read_is_rejected(self):
        with self.reader(after=lambda: self.path.write_bytes(self.payload + b'longer')):
            self.assert_changed()

    def test_binary_invalid_utf8_bytes_are_not_mislabelled_as_changed(self):
        self.path.write_bytes(b'\xff\xfe')
        with self.observed(path_first={'st_ctime_ns': 10}, path_last={'st_ctime_ns': 10},
                           fd_first={'st_ctime_ns': 20}, fd_last={'st_ctime_ns': 20}):
            self.assertEqual(native._read_file(self.path), b'\xff\xfe')

    def test_actual_rewritten_file_with_changed_times_remains_readable(self):
        self.path.write_bytes(b'changed native source')
        os.utime(self.path, ns=(1700000000000000000, 1700000000000000000))
        self.assertEqual(native._read_file(self.path), b'changed native source')

    def test_file_at_limit_is_accepted_and_over_limit_rejected(self):
        with patch.object(native, 'MAX_NATIVE_BYTES', len(self.payload)):
            self.assertEqual(native._read_file(self.path), self.payload)
            self.path.write_bytes(self.payload + b'x')
            with self.assertRaisesRegex(ValueError, '^native_source_too_large$'):
                native._read_file(self.path)

    def test_missing_and_io_failure_keep_distinct_codes(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private path')):
            with self.assertRaisesRegex(ValueError, '^native_source_unreadable$'):
                native._read_file(self.path)
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, '^native_source_missing$'):
            native._read_file(self.path)


if __name__ == '__main__':
    unittest.main()
