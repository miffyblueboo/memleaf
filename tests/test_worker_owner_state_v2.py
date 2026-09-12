from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.extraction_work_state import (
    ExtractionWorkStateError,
    active_background_work_id,
)


class WorkerOwnerStateV2Tests(unittest.TestCase):
    def test_wrong_process_job_state_version_does_not_fall_back_to_fresh_budget(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-owner-state-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            path = service.vault.state_path / "process_jobs.json"
            original = {
                "version": 999,
                "jobs": {
                    "job-a": {
                        "job_id": "job-a",
                        "source": "hermes",
                        "session_id": "session",
                        "status": "running",
                    }
                },
                "order": ["job-a"],
                "active_job_id": "job-a",
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            with self.assertRaises(ExtractionWorkStateError):
                active_background_work_id(
                    service.vault,
                    source="hermes",
                    session_id="session",
                )

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_missing_process_job_state_still_means_synchronous_caller(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-owner-state-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            self.assertIsNone(
                active_background_work_id(
                    service.vault,
                    source="hermes",
                    session_id="session",
                )
            )


if __name__ == "__main__":
    unittest.main()
