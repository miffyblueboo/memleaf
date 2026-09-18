"""Regression for observed Windows lstat/fstat ctime-domain differences."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from memleaf.incremental_native import _read_file


def observation(value, **changes):
    fields = {key: getattr(value, key) for key in
              ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")}
    fields.update(changes)
    return SimpleNamespace(**fields)


class NativeFileObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"native.md"
        self.path.write_bytes(b"Native read-only note.\n")
        self.initial=self.path.lstat()

    def test_distinct_stable_ctime_domains_are_not_a_source_edit(self):
        handle=observation(self.initial, st_ctime_ns=self.initial.st_ctime_ns+1234567)
        with patch("memleaf.incremental_native.os.fstat", return_value=handle) as check:
            self.assertEqual(_read_file(self.path),self.path.read_bytes())
        self.assertEqual(check.call_count,2)

    def test_handle_ctime_change_is_still_detected(self):
        a=observation(self.initial,st_ctime_ns=100)
        b=observation(self.initial,st_ctime_ns=101)
        with patch("memleaf.incremental_native.os.fstat",side_effect=[a,b]):
            with self.assertRaisesRegex(ValueError,"changed_during_read"):_read_file(self.path)

    def test_path_ctime_change_is_still_detected(self):
        a=observation(self.initial);b=observation(self.initial,st_ctime_ns=self.initial.st_ctime_ns+1)
        with patch.object(Path,"lstat",side_effect=[a,b]):
            with self.assertRaisesRegex(ValueError,"changed_during_read"):_read_file(self.path)

    def test_identity_mismatch_between_path_and_handle_is_rejected(self):
        for field in ("st_dev","st_ino"):
            value=observation(self.initial,**{field:getattr(self.initial,field)+1})
            with self.subTest(field=field),patch("memleaf.incremental_native.os.fstat",return_value=value):
                with self.assertRaisesRegex(ValueError,"changed_during_read"):_read_file(self.path)

    def test_size_and_mtime_must_agree_across_all_observations(self):
        for field in ("st_size","st_mtime_ns"):
            value=observation(self.initial,**{field:getattr(self.initial,field)+1})
            with self.subTest(field=field),patch("memleaf.incremental_native.os.fstat",return_value=value):
                with self.assertRaisesRegex(ValueError,"changed_during_read"):_read_file(self.path)

    def test_handle_growth_is_not_hidden_by_stable_path_stat(self):
        a=observation(self.initial);b=observation(self.initial,st_size=self.initial.st_size+1)
        with patch("memleaf.incremental_native.os.fstat",side_effect=[a,b]):
            with self.assertRaisesRegex(ValueError,"changed_during_read"):_read_file(self.path)

    def test_rewritten_stable_file_is_read_exactly(self):
        self.assertEqual(_read_file(self.path),self.path.read_bytes())
        self.path.write_bytes(b"A new stable note.\n")
        self.assertEqual(_read_file(self.path),b"A new stable note.\n")

    def test_empty_file_is_not_missing_or_changed(self):
        self.path.write_bytes(b"")
        self.assertEqual(_read_file(self.path),b"")


if __name__=="__main__":unittest.main()
