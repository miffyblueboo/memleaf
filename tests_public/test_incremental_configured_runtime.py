from __future__ import annotations

import json
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf import Memleaf
from memleaf.incremental_execution import IncrementalRunError
from memleaf.incremental_run_state import KEY, load_run
from memleaf.llm import ModelRouter, ModelError, ModelUnavailable


class ConfiguredRuntimeTests(IncrementalFixture):
    def process(self, **kwargs):
        return self.s.process_incremental(source="hermes", session_id="s", turn_id="t", **kwargs)

    def test_facade_uses_the_same_runner_and_budget(self):
        backend = Backend(output(self.create(), self.no_memory()))
        result = self.process(model=backend)
        again = self.s.run_incremental(source="hermes", session_id="s", turn_id="t", backend=backend)
        self.assertEqual(result["run_id"], again["run_id"])
        self.assertEqual(len(self.ledger()[KEY]), 1)
        self.assertEqual(result["reservations"], 1)
        self.assertEqual(len(backend.calls), 1)

    def test_configured_fixed_api_is_selected(self):
        backend = Backend(output(self.no_memory("e1"), self.no_memory()))
        with patch("memleaf.incremental_runtime.ModelRouter.from_config", return_value=ModelRouter(mode="api", api=backend)):
            self.assertEqual(self.process()["execution_status"], "completed")
        self.assertEqual(len(backend.calls), 1)

    def test_explicit_recovery_required_after_timeout(self):
        backend = Backend(ModelError("timeout", code="model_timeout"), output(self.create(), self.no_memory()))
        first = self.process(model=backend)
        self.assertTrue(first["retry_available"])
        second = self.process(model=backend)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(self.process(model=backend, recover=True)["execution_status"], "completed")
        self.assertEqual(len(backend.calls), 2)

    def test_route_is_not_resolved_for_completed_replay(self):
        self.process(model=Backend(output(self.create(), self.no_memory())))
        with patch("memleaf.incremental_runtime._resolve", side_effect=AssertionError("should not resolve")):
            self.assertEqual(self.process()["model_calls_this_invocation"], 0)

    def test_terminal_receipt_survives_source_cleanup(self):
        first = self.process(model=Backend(output(self.create(), self.no_memory())))
        self.s.vault.session_path("hermes", "s").unlink()
        self.assertEqual(self.process()["run_id"], first["run_id"])

    def test_ambiguous_cleaned_revisions_are_not_guessed(self):
        self.process(model=Backend(output(self.create(), self.no_memory())))
        self.revise()
        self.s.capture("hermes", "s", "t", "assistant", "Acknowledged revision", message_id="t-a", message_revision="2", previous_message_revision="1", source_sequence=2, final=True)
        self.process(model=Backend(output(self.no_memory("e1"), self.no_memory())))
        self.s.vault.session_path("hermes", "s").unlink()
        with self.assertRaisesRegex(ValueError, "ambiguous_receipt"):
            self.process()

    def test_safe_backend_required(self):
        with self.assertRaises(ModelUnavailable):
            self.process(model=object())
        self.assertNotIn(KEY, self.ledger())

    def test_host_auto_fallback_rejected(self):
        router = ModelRouter(mode="auto", host=lambda *_a, **_kw: "{}", api=Backend("{}"))
        with self.assertRaises(ModelUnavailable):
            self.process(router=router)
        self.assertNotIn(KEY, self.ledger())

    def test_ambiguous_route_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous_model_route"):
            self.process(model=Backend(), router=Backend())

    def test_recover_boolean_is_strict(self):
        with self.assertRaisesRegex(ValueError, "invalid_recover"):
            self.process(recover="false")

    def test_changed_scope_does_not_create_new_allowance(self):
        self.process(model=Backend(output(self.create(), self.no_memory())))
        with self.assertRaisesRegex(ValueError, "arguments_changed"):
            self.process(scope="project:Atlas")

    def test_saved_response_resumes_without_backend(self):
        backend = Backend(output(self.create(), self.no_memory()))
        with patch("memleaf.incremental_execution.apply_incremental", side_effect=OSError("fixture")):
            with self.assertRaises(IncrementalRunError):
                self.process(model=backend)
        with patch("memleaf.incremental_runtime._resolve", side_effect=AssertionError("no route needed")):
            result = self.process()
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(len(backend.calls), 1)

    def test_partial_does_not_retry(self):
        backend = Backend(output(self.create()))
        first = self.process(model=backend)
        self.assertEqual(first["execution_status"], "completed_with_unresolved")
        self.assertFalse(first["retry_available"])
        self.process(model=backend, recover=True)
        self.assertEqual(len(backend.calls), 1)

    def test_whole_invalid_response_still_uses_at_most_two(self):
        backend = Backend("{", "{")
        result = self.process(model=backend)
        self.assertEqual(result["execution_status"], "failed")
        self.process(model=backend, recover=True)
        self.assertEqual(len(backend.calls), 2)
