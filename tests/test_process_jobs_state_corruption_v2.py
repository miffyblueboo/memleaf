from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf.process_jobs import (
    ProcessJobStateError,
    _read_state,
    enqueue,
    status,
)
from memleaf.vault import Vault


class ProcessJobsStateCorruptionV2Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-process-job-state-")
        self.vault = Vault(Path(self.tempdir.name) / "vault")
        self.vault.ensure()
        self.path = self.vault.state_path / "process_jobs.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_missing_state_is_the_only_case_that_initializes_empty(self):
        self.assertFalse(self.path.exists())
        state = _read_state(self.vault)
        self.assertEqual(
            state,
            {"version": 1, "jobs": {}, "order": [], "active_job_id": None},
        )
        # A read of absent state is not itself a mutation.
        self.assertFalse(self.path.exists())

    def test_malformed_json_is_preserved_and_enqueue_fails_before_launch(self):
        original = "{not-json\n"
        self.path.write_text(original, encoding="utf-8")

        with patch("memleaf.process_jobs._launch") as launch:
            with self.assertRaises(ProcessJobStateError):
                enqueue(self.vault.root, source="hermes", session_id="session")

        launch.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_wrong_version_is_not_treated_as_an_empty_queue(self):
        original = {
            "version": 999,
            "jobs": {},
            "order": [],
            "active_job_id": None,
        }
        self.path.write_text(json.dumps(original), encoding="utf-8")

        with self.assertRaises(ProcessJobStateError):
            _read_state(self.vault)

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)

    def test_broken_order_is_not_silently_repaired(self):
        original = {
            "version": 1,
            "jobs": {
                "job-a": {
                    "job_id": "job-a",
                    "source": "hermes",
                    "session_id": "session",
                    "status": "pending",
                }
            },
            "order": [],
            "active_job_id": None,
        }
        self.path.write_text(json.dumps(original), encoding="utf-8")

        with self.assertRaises(ProcessJobStateError):
            _read_state(self.vault)

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)

    def test_invalid_active_job_is_not_downgraded_to_unknown_job(self):
        original = {
            "version": 1,
            "jobs": {
                "job-a": {
                    "job_id": "job-a",
                    "source": "hermes",
                    "session_id": "session",
                    "status": "running",
                }
            },
            "order": ["job-a"],
            "active_job_id": "job-missing",
        }
        self.path.write_text(json.dumps(original), encoding="utf-8")

        with self.assertRaises(ProcessJobStateError):
            status(self.vault.root, job_id="job-a")

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), original)


if __name__ == "__main__":
    unittest.main()
