from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from memleaf import Memleaf
from memleaf.locking import atomic_write_json


class _FailIfCalledBackend:
    provider = "test"
    model = "must-not-run"
    single_pass_safe = True

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, purpose, temperature
        self.calls += 1
        raise AssertionError("read-only turn must not call the model")


class ReadOnlyZeroCallV2Tests(unittest.TestCase):
    @staticmethod
    def _capture_no_write(service, session_id):
        service.capture(
            "hermes",
            session_id,
            "turn-1",
            "user",
            "不要修改记忆",
            event_id=f"{session_id}-user",
        )
        service.capture(
            "hermes",
            session_id,
            "turn-1",
            "assistant",
            "好的，我只回答当前问题。",
            event_id=f"{session_id}-assistant",
        )

    def test_explicit_memory_write_disable_is_settled_without_model(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-read-only-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            backend = _FailIfCalledBackend()
            self._capture_no_write(service, "read-only")

            result = service.process(
                source="hermes",
                session_id="read-only",
                model=backend,
            )

            self.assertEqual(result["processed_turns"], 1)
            self.assertEqual(result["memories_written"], 0)
            self.assertEqual(result["memory_ids"], [])
            self.assertEqual(result["model_metrics"]["total"]["call_count"], 0)
            self.assertEqual(backend.calls, 0)
            self.assertEqual(service._read_memories_unlocked("knowledge"), [])

            replay = service.process(
                source="hermes",
                session_id="read-only",
                model=backend,
            )
            self.assertEqual(replay["processed_turns"], 0)
            self.assertEqual(replay["model_metrics"]["total"]["call_count"], 0)
            self.assertEqual(backend.calls, 0)

    def test_background_no_write_turn_does_not_depend_on_request_budget_state(self):
        with tempfile.TemporaryDirectory(prefix="memleaf-read-only-worker-") as tempdir:
            service = Memleaf(Path(tempdir) / "vault")
            backend = _FailIfCalledBackend()
            session_id = "read-only-worker"
            job_id = "job-read-only-worker"
            self._capture_no_write(service, session_id)
            atomic_write_json(
                service.vault.state_path / "process_jobs.json",
                {
                    "version": 1,
                    "jobs": {
                        job_id: {
                            "job_id": job_id,
                            "source": "hermes",
                            "session_id": session_id,
                            "status": "running",
                            "owner_pid": os.getpid(),
                        }
                    },
                    "order": [job_id],
                    "active_job_id": job_id,
                },
                mode=0o600,
            )
            budget_path = service.vault.state_path / "extraction_request_budget.json"
            budget_path.write_text("{corrupt-budget", encoding="utf-8")

            result = service.process(
                source="hermes",
                session_id=session_id,
                model=backend,
            )

            self.assertEqual(result["processed_turns"], 1)
            self.assertEqual(result["memories_written"], 0)
            self.assertEqual(result["model_metrics"]["total"]["call_count"], 0)
            self.assertEqual(backend.calls, 0)
            self.assertEqual(budget_path.read_text(encoding="utf-8"), "{corrupt-budget")


if __name__ == "__main__":
    unittest.main()
