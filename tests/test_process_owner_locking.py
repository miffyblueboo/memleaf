"""Run on all CI operating systems; use native child processes, not OS mocks."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from memleaf.locking import VaultLock
from memleaf.process_journal import ProcessJournal
from memleaf.process_owner import windows_pid_status


class ProcessOwnerTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(ProcessJournal._owner_pid_status(os.getpid()))

    def test_invalid_owner_never_probes_os(self):
        for value in (None, True, False, "123", 1.5):
            with self.subTest(value=value):
                self.assertIsNone(ProcessJournal._owner_pid_status(value))
        for value in (0, -1):
            self.assertFalse(ProcessJournal._owner_pid_status(value))

    def test_liveness_probe_does_not_terminate_child_and_detects_exit(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(ProcessJournal._owner_pid_status(child.pid))
            self.assertIsNone(child.poll(), "a liveness probe must not terminate its target")
        finally:
            child.terminate()
            child.wait(timeout=10)
        self.assertFalse(ProcessJournal._owner_pid_status(child.pid))

    def test_windows_handle_wait_states_and_close(self):
        for wait_result, expected in ((0, False), (258, True), (0xffffffff, None)):
            with self.subTest(wait_result=wait_result):
                kernel = Mock()
                kernel.OpenProcess.return_value = 0x100000001
                kernel.WaitForSingleObject.return_value = wait_result
                with patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
                    self.assertIs(windows_pid_status(1234), expected)
                kernel.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
                kernel.CloseHandle.assert_called_once_with(0x100000001)

    def test_windows_inaccessible_or_missing_owner(self):
        for error, expected in ((87, False), (5, True), (6, None)):
            with self.subTest(error=error):
                kernel = Mock()
                kernel.OpenProcess.return_value = 0
                with (patch.object(ctypes, "WinDLL", return_value=kernel, create=True),
                      patch.object(ctypes, "get_last_error", return_value=error, create=True)):
                    self.assertIs(windows_pid_status(1234), expected)
                kernel.CloseHandle.assert_not_called()


class CrossProcessVaultLockTests(unittest.TestCase):
    def test_child_waits_for_parent_then_acquires_same_lock(self):
        with tempfile.TemporaryDirectory(prefix="memleaf process lock ") as directory:
            lock_path = Path(directory) / "vault.lock"
            marker = Path(directory) / "acquired"
            code = (
                "from pathlib import Path\n"
                "import sys\n"
                "from memleaf.locking import VaultLock\n"
                "print('ready', flush=True)\n"
                "with VaultLock(Path(sys.argv[1])):\n"
                " Path(sys.argv[2]).write_text('acquired', encoding='utf-8')\n"
            )
            child = None
            try:
                with VaultLock(lock_path):
                    child = subprocess.Popen(
                        [sys.executable, "-u", "-c", code, str(lock_path), str(marker)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=0.3)
                    self.assertFalse(marker.exists(), "separate hosts must not enter together")
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stdout + stderr)
                self.assertEqual(marker.read_text(encoding="utf-8"), "acquired")
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
