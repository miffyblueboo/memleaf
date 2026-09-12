from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memleaf import Memleaf
from memleaf.extraction_budget import ExtractionWorkBudget, SinglePassBudgetBackend
from memleaf.extraction_work_state import (
    active_background_work_id,
    begin_turn_budget,
    complete_turn_budget,
    reserve_model_request,
)
from memleaf.llm import ModelError
from memleaf.locking import atomic_write_json


class _Backend:
    provider = "test"
    model = "restart-budget"
    single_pass_safe = True

    def __init__(self):
        self.calls = 0
        self.timeout_caps = []

    def set_call_timeout(self, seconds):
        self.timeout_caps.append(seconds)

    def clear_call_timeout(self):
        pass

    def complete(self, prompt, *, system="", purpose="", temperature=0.0):
        del prompt, system, purpose, temperature
        self.calls += 1
        return "{}"


class WorkerRestartBudgetV2Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-worker-budget-")
        self.service = Memleaf(Path(self.tempdir.name) / "vault")
        self.vault = self.service.vault
        self.job_id = "job-restart-budget"
        self.turn_id = "hermes/session/turn-key"
        atomic_write_json(
            self.vault.state_path / "process_jobs.json",
            {
                "version": 1,
                "jobs": {
                    self.job_id: {
                        "job_id": self.job_id,
                        "source": "hermes",
                        "session_id": "session",
                        "status": "running",
                        "owner_pid": os.getpid(),
                    }
                },
                "order": [self.job_id],
                "active_job_id": self.job_id,
            },
            mode=0o600,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def reserve(self):
        return reserve_model_request(
            self.vault,
            work_id=self.job_id,
            turn_id=self.turn_id,
        )

    def begin(self, now):
        return begin_turn_budget(
            self.vault,
            work_id=self.job_id,
            turn_id=self.turn_id,
            wall_clock=lambda: now,
        )

    def test_active_worker_identity_is_stable_process_job_id(self):
        self.assertEqual(
            active_background_work_id(
                self.vault,
                source="hermes",
                session_id="session",
            ),
            self.job_id,
        )
        self.assertIsNone(
            active_background_work_id(
                self.vault,
                source="hermes",
                session_id="other-session",
            )
        )

    def test_fresh_wrapper_after_restart_only_gets_remaining_request(self):
        backend = _Backend()

        first_worker = SinglePassBudgetBackend(
            backend,
            reserve_request=self.reserve,
        )
        first_worker.complete("primary", purpose="single_pass")

        # Simulate a killed/restarted worker by creating a fresh in-memory
        # wrapper while preserving the process-job ID and durable ledger.
        second_worker = SinglePassBudgetBackend(
            backend,
            reserve_request=self.reserve,
        )
        second_worker.complete("after restart", purpose="single_pass")

        third_worker = SinglePassBudgetBackend(
            backend,
            reserve_request=self.reserve,
        )
        with self.assertRaises(ModelError) as caught:
            third_worker.complete("must not dispatch", purpose="single_pass")

        self.assertEqual(caught.exception.code, "model_timeout")
        self.assertEqual(backend.calls, 2)
        self.assertEqual(len(backend.timeout_caps), 2)
        self.assertLessEqual(backend.timeout_caps[0], 6.0)
        # Without a restored wall budget this direct wrapper knows only the
        # durable ordinal; production processing additionally restores elapsed
        # wall time before constructing its wrapper.
        self.assertGreater(backend.timeout_caps[1], 6.0)

    def test_restart_restores_elapsed_wall_time_into_monotonic_budget(self):
        backend = _Backend()
        self.assertEqual(self.begin(100.0), 0.0)
        first_budget = ExtractionWorkBudget(clock=lambda: 50.0, elapsed_seconds=0.0)
        first_budget.wrap_backend(backend, reserve_request=self.reserve).complete(
            "primary", purpose="single_pass"
        )

        elapsed = self.begin(105.0)
        self.assertEqual(elapsed, 5.0)
        second_budget = ExtractionWorkBudget(clock=lambda: 80.0, elapsed_seconds=elapsed)
        second_budget.wrap_backend(backend, reserve_request=self.reserve).complete(
            "repair", purpose="single_pass"
        )

        self.assertEqual(backend.calls, 2)
        # Five seconds were already consumed by the prior worker, leaving only
        # three seconds of the shared eight-second model window.
        self.assertLessEqual(backend.timeout_caps[-1], 3.0)
        self.assertGreater(backend.timeout_caps[-1], 0.0)

    def test_expired_restarted_work_cannot_dispatch_or_commit(self):
        backend = _Backend()
        self.assertEqual(self.begin(200.0), 0.0)
        elapsed = self.begin(210.5)
        budget = ExtractionWorkBudget(clock=lambda: 10.0, elapsed_seconds=elapsed)
        wrapped = budget.wrap_backend(backend, reserve_request=self.reserve)

        with self.assertRaises(ModelError):
            wrapped.complete("late", purpose="single_pass")
        with self.assertRaises(ModelError):
            budget.ensure_before_commit()

        self.assertEqual(backend.calls, 0)
        self.assertEqual(self.reserve(), 1)

    def test_processor_restores_background_deadline_before_outbound(self):
        self.service.capture(
            "hermes", "session", "turn-1", "user", "Remember Alpha uses PostgreSQL.",
            event_id="worker-budget-user",
        )
        self.service.capture(
            "hermes", "session", "turn-1", "assistant", "Noted.",
            event_id="worker-budget-assistant",
        )
        backend = _Backend()

        with patch("memleaf.processing.begin_turn_budget", return_value=11.0):
            with self.assertRaises(ModelError) as caught:
                self.service.process(
                    source="hermes",
                    session_id="session",
                    model=backend,
                )

        self.assertEqual(caught.exception.code, "model_timeout")
        self.assertEqual(backend.calls, 0)
        self.assertEqual(backend.timeout_caps, [])

    def test_wall_clock_rollback_fails_closed(self):
        self.assertEqual(self.begin(300.0), 0.0)
        self.assertEqual(self.begin(299.0), 10.0)

    def test_successful_turn_commit_cleanup_allows_state_to_shrink(self):
        self.assertEqual(self.reserve(), 1)
        self.assertTrue(
            complete_turn_budget(
                self.vault,
                work_id=self.job_id,
                turn_id=self.turn_id,
            )
        )
        state = json.loads(
            (self.vault.state_path / "extraction_request_budget.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(state["works"], {})
        self.assertEqual(state["order"], [])

    def test_corrupt_budget_state_fails_closed_without_outbound_request(self):
        path = self.vault.state_path / "extraction_request_budget.json"
        path.write_text("{not-json", encoding="utf-8")
        backend = _Backend()
        budgeted = SinglePassBudgetBackend(
            backend,
            reserve_request=self.reserve,
        )

        with self.assertRaises(ModelError) as caught:
            budgeted.complete("blocked", purpose="single_pass")

        self.assertEqual(caught.exception.code, "model_timeout")
        self.assertEqual(backend.calls, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")


if __name__ == "__main__":
    unittest.main()
