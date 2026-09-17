from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

from incremental_test_support import IncrementalFixture
from test_incremental_execution import Backend, output
from memleaf.incremental_commit import _window
from memleaf.incremental_journal import owned_turn_keys
from memleaf.incremental_run_state import KEY, load_run
from memleaf.extraction_work_state import _budget_path
from memleaf.inbox import parse_inbox_file
from memleaf.index import turn_key
from memleaf.llm import ModelRouter, ModelError


class SelectedRetentionTests(IncrementalFixture):
    def refs(self):
        return self.s.preview_incremental(source="hermes", session_id="s", turn_id="t")["source_refs"]

    def args(self, **changes):
        value = dict(source="hermes", session_id="s", turn_id="t", intent_id="keep-1",
                     selected_source_refs=[self.refs()[0]["source_ref"]],
                     retention_request="Remember the task, not the acknowledgement.")
        value.update(changes)
        return value

    def retain(self, backend=None, **changes):
        return self.s.remember_incremental(model=backend, **self.args(**changes))

    def test_selection_is_new_and_other_message_is_context(self):
        backend = Backend(output(self.create()))
        result = self.retain(backend)
        self.assertEqual(result["execution_status"], "completed")
        request = json.loads(backend.calls[0][0])
        self.assertEqual(request["request_kind"], "explicit_remember")
        self.assertEqual([e["use"] for e in request["evidence"]], ["new", "context"])
        self.assertEqual(request["retention_request"], self.args()["retention_request"])
        self.assertEqual(result["intent_id"], "keep-1")
        self.assertEqual(result["reservations"], 1)
        self.assertEqual(result["commit"]["request_kind"], "explicit_remember")

    def test_selected_assistant_fact_keeps_its_role(self):
        backend = Backend(output({"action": "CREATE", "evidence": ["e2"], "memory":
                                  {"type": "fact", "scope": "global", "title": "Result", "body": "Reported outcome."}}))
        self.retain(backend, selected_source_refs=[self.refs()[1]["source_ref"]])
        evidence = json.loads(backend.calls[0][0])["evidence"]
        self.assertEqual([(e["role"], e["use"]) for e in evidence], [("user", "context"), ("assistant", "new")])

    def test_unselected_context_cannot_authorize_an_operation(self):
        row = self.create(); row["evidence"] = ["e2"]
        result = self.retain(Backend(output(row)))
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 0)
        self.assertTrue(result["commit"]["issues"])

    def test_selected_partial_block_request_is_passed_without_expansion(self):
        backend = Backend(output(self.create()))
        self.retain(backend, retention_request="只记第二点，不记其他建议。")
        self.assertEqual(json.loads(backend.calls[0][0])["retention_request"], "只记第二点，不记其他建议。")
        # The instruction is a semantic selector, not Core proof of perfect topic extraction.
        self.assertEqual(len(backend.calls), 1)

    def test_explicit_no_memory_is_not_accepted(self):
        result = self.retain(Backend(output(self.no_memory("e1"))))
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertEqual(result["commit"]["counts"]["no_memory"], 0)
        self.assertTrue(any(e["code"] == "explicit_retention_required" for e in result["commit"]["issues"]))

    def test_empty_items_do_not_settle_selected_content(self):
        result = self.retain(Backend(output()))
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 0)

    def test_deferred_remains_possible_without_fabricating_a_fact(self):
        result = self.retain(Backend(output({"action": "DEFERRED", "evidence": ["e1"],
                                            "reason": "missing_context", "need": "Which task?"})))
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 0)

    def test_same_intent_replay_uses_no_model(self):
        backend = Backend(output(self.create()))
        first = self.retain(backend)
        with patch("memleaf.incremental_runtime._resolve", side_effect=AssertionError("no new route")):
            second = self.retain()
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["model_calls_this_invocation"], 0)
        self.assertEqual(len(backend.calls), 1)

    def test_new_intent_has_new_work_and_exact_dedup(self):
        first = self.retain(Backend(output(self.create())))
        second = self.retain(Backend(output(self.create())), intent_id="keep-2")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["commit"]["counts"]["no_change"], 1)
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 1)

    def test_auto_no_memory_does_not_seal_later_explicit_authorization(self):
        auto = self.s.process_incremental(source="hermes", session_id="s", turn_id="t",
                                          model=Backend(output(self.no_memory("e1"), self.no_memory())))
        before = deepcopy(self.ledger()["sessions"]["hermes/s"]["processed_turns"])
        result = self.retain(Backend(output(self.create())))
        self.assertEqual(result["execution_status"], "completed")
        self.assertNotEqual(result["run_id"], auto["run_id"])
        self.assertEqual(self.ledger()["sessions"]["hermes/s"]["processed_turns"], before)

    def test_explicit_settlement_does_not_consume_automatic_turn(self):
        self.retain(Backend(output(self.create())))
        state = self.ledger()
        self.assertFalse(state["sessions"]["hermes/s"].get("processed_turns"))
        self.assertNotIn(turn_key("t"), owned_turn_keys(state, "hermes", "s"))
        auto = self.s.process_incremental(source="hermes", session_id="s", turn_id="t",
                                          model=Backend(output(self.create(), self.no_memory())))
        self.assertEqual(auto["execution_status"], "completed")
        self.assertEqual(len(self.s.vault.list_markdown("knowledge")), 1)

    def test_partial_explicit_does_not_claim_whole_automatic_turn(self):
        self.retain(Backend(output(self.no_memory("e1"))))
        self.assertNotIn(turn_key("t"), owned_turn_keys(self.ledger(), "hermes", "s"))

    def test_changed_request_under_same_intent_rejected_before_dispatch(self):
        self.retain(Backend(output(self.create())))
        backend = Backend(output(self.create()))
        with self.assertRaisesRegex(ValueError, "arguments_changed"):
            self.retain(backend, retention_request="Remember a different item.")
        self.assertFalse(backend.calls)
        self.assertEqual(len(self.ledger()[KEY]), 1)

    def test_changed_selection_under_same_intent_rejected(self):
        self.retain(Backend(output(self.create())))
        with self.assertRaisesRegex(ValueError, "arguments_changed"):
            self.retain(Backend(), selected_source_refs=[self.refs()[1]["source_ref"]])

    def test_changed_scope_under_same_intent_rejected(self):
        self.retain(Backend(output(self.create())))
        with self.assertRaisesRegex(ValueError, "arguments_changed"):
            self.retain(Backend(), scope="project:Atlas")

    def test_changed_turn_under_same_intent_rejected(self):
        self.retain(Backend(output(self.create())))
        self.capture("next", seq=3)
        with self.assertRaisesRegex(ValueError, "arguments_changed"):
            self.retain(Backend(), turn_id="next")

    def test_unknown_source_and_model_reference_rejected(self):
        for refs in (["f"*64], ["e1"], [], "e1", [1], [self.refs()[0]["source_ref"]]*2):
            with self.subTest(refs=refs):
                backend = Backend(output(self.create()))
                with self.assertRaises(ValueError): self.retain(backend, selected_source_refs=refs)
                self.assertFalse(backend.calls)
        self.assertNotIn(KEY, self.ledger())

    def test_context_from_another_turn_cannot_be_selected(self):
        self.capture("next", seq=3)
        events = parse_inbox_file(self.s.vault.session_path("hermes", "s"))
        foreign = next(t for t in events if t.turn_key == turn_key("next")).events[0].event_key
        with self.assertRaisesRegex(ValueError, "selected_source_unavailable"):
            self.retain(Backend(), selected_source_refs=[foreign])

    def test_source_refs_are_set_canonical_not_order_identity(self):
        refs = [r["source_ref"] for r in self.refs()]
        row = self.create(); row["evidence"] = ["e1", "e2"]; row["at"] = "e1"
        first = self.retain(Backend(output(row)), selected_source_refs=refs)
        again = self.retain(Backend(), selected_source_refs=list(reversed(refs)))
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertEqual(again["model_calls_this_invocation"], 0)

    def test_required_intent_and_request_validate_locally(self):
        for changes in ({"intent_id": ""}, {"intent_id": "bad\nvalue"}, {"retention_request": ""},
                        {"retention_request": "x"*8193}, {"retention_request": ["text"]}, {"intent_id": 1}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): self.retain(Backend(), **changes)
        self.assertNotIn(KEY, self.ledger())

    def test_pending_source_revision_blocks_without_rebasing(self):
        backend = Backend(ModelError("timeout", code="model_timeout"))
        args = self.args()
        result = self.s.remember_incremental(model=backend, **args)
        self.revise()
        result = self.s.remember_incremental(model=Backend(output(self.create())), recover=True, **args)
        self.assertEqual(result["execution_status"], "blocked")
        self.assertEqual(result["model_calls_this_invocation"], 0)
        self.assertEqual(result["reserved_requests"], 1)

    def test_scope_boundary_still_rejects_authorized_retention_outside_it(self):
        result = self.retain(Backend(output(self.create())), scope="project:Atlas")
        self.assertEqual(result["execution_status"], "completed_with_unresolved")
        self.assertFalse(self.s.vault.list_markdown("knowledge"))

    def test_completed_receipt_survives_source_removal(self):
        args = self.args()
        first = self.s.remember_incremental(model=Backend(output(self.create())), **args)
        self.s.vault.session_path("hermes", "s").unlink()
        again = self.s.remember_incremental(**args)
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertEqual(again["model_calls_this_invocation"], 0)
        with self.assertRaisesRegex(ValueError, "source_not_complete"):
            self.s.remember_incremental(model=Backend(), **{**args, "intent_id": "new-intent"})

    def test_no_authorization_plaintext_in_terminal_state(self):
        secret = "User-only selection instruction not used in a memory"
        result = self.retain(Backend(output(self.create())), retention_request=secret)
        self.assertNotIn(secret, self.s.vault.processed_state_path.read_text())
        run = load_run(self.ledger(), result["run_id"])
        self.assertNotIn("retention_request", run)
        self.assertIn("request_hash", run["arguments"]["selection"])

    def test_legacy_budget_does_not_charge_new_explicit_authorization(self):
        from memleaf.extraction_work_state import reserve_model_request
        tid = f"hermes/s/{turn_key('t')}"
        for _ in range(3): reserve_model_request(self.s.vault, work_id="job-old", turn_id=tid, request_limit=3)
        result = self.retain(Backend(output(self.create())))
        self.assertEqual(result["reservations"], 1)
        budget = json.loads(_budget_path(self.s.vault).read_text())
        self.assertEqual(budget["works"]["job-old"]["turns"][tid]["requests"], 3)

    def test_update_keeps_original_identity_and_deadline(self):
        self.target()
        result = self.retain(Backend(output(self.update())), priority_memory_ids=["mem-old"])
        self.assertEqual(result["commit"]["operations"][0]["memory_id"], "mem-old")
        current = self.s.read("mem-old")
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.due_date, "2026-09-20")
        self.assertEqual(current.extra["custom"], {"keep": True})

    def test_configured_route_reuses_existing_resolver(self):
        backend = Backend(output(self.create()))
        with patch("memleaf.incremental_runtime.ModelRouter.from_config", return_value=ModelRouter(mode="api", api=backend)):
            result = self.retain()
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(len(backend.calls), 1)
