from __future__ import annotations
import json
import unittest
from copy import deepcopy

from memleaf import Memory
from memleaf.incremental_protocol import PlanningSnapshot, compile_incremental
from memleaf.turn_plan import revision_digest


def evidence(ref="e1", text="明天下班前完成任务", *, seq=2, use="new", time="2026-09-17T10:00:00+08:00", role="user"):
    return {"ref": ref, "use": use, "role": role, "text": text, "source": "hermes", "session_id": "s",
            "event_key": ref + "-event", "source_time": time, "source_sequence": seq}


def memory(**kw):
    data = dict(memory_id="mem-1", title="task", body="Deliver the result.", type="todo", status="active",
                scopes=["project:Atlas"], due_date="2026-09-20", assignee="user:1", waiting_on="review",
                custom="preserve", field_basis={"status": {"source": "hermes", "session_id": "s", "source_sequence": 1}})
    data.update(kw)
    return Memory.new(**data)


class IncrementalProtocolTests(unittest.TestCase):
    def setUp(self):
        self.target = memory()
        self.snapshot = PlanningSnapshot.build(evidence=[evidence()], targets={"m1": self.target},
                                               scopes={"s1": "project:Atlas", "s2": "project:Beacon"})

    def run_rows(self, *rows, snapshot=None):
        return compile_incremental(json.dumps({"items": list(rows)}, ensure_ascii=False), snapshot or self.snapshot)

    def update(self, **patch):
        return {"action": "UPDATE", "target": "m1", "evidence": ["e1"], "patch": patch}

    def create(self, **fields):
        value = {"type": "fact", "scope": "s1", "title": "Known requirement", "body": "Keep this requirement."}
        value.update(fields)
        return {"action": "CREATE", "evidence": ["e1"], "memory": value}

    def test_status_patch_preserves_whole_target(self):
        out = self.run_rows(self.update(status="completed"))
        self.assertFalse(out["issues"])
        op = out["operations"][0]
        self.assertEqual(op["target"], "mem-1")
        self.assertEqual(op["expected_revision"], revision_digest(self.target))
        for key in ("body", "scopes", "due_date", "assignee", "waiting_on", "custom", "type", "created"):
            self.assertEqual(op["memory"][key], self.target.to_dict()[key])
        self.assertNotIn("completed_at", op["memory"])
        self.assertEqual(self.target.status, "active")

    def test_explicit_null_is_not_omission(self):
        result = self.run_rows(self.update(assignee=None, waiting_on=None))["operations"][0]["memory"]
        self.assertIsNone(result["assignee"])
        self.assertIsNone(result["waiting_on"])
        self.assertEqual(result["due_date"], "2026-09-20")

    def test_create_allocates_no_permanent_id(self):
        op = self.run_rows(self.create())["operations"][0]
        self.assertEqual(op["memory"]["validity"], "valid")
        self.assertNotIn("memory_id", op["memory"])
        self.assertNotIn("operation_id", op)

    def test_type_aliases_and_unknown_strings_are_core_normalized(self):
        cases = {
            "decision": "fact",
            "Decision": "fact",
            "task": "todo",
            "action-item": "todo",
            "note": "other",
            "custom_model_label": "other",
        }
        for offered, expected in cases.items():
            with self.subTest(offered=offered):
                row = self.create(type=offered, scope="global")
                if expected == "todo":
                    row["memory"]["status"] = "active"
                out = self.run_rows(row)
                self.assertFalse(out["issues"])
                self.assertEqual(out["operations"][0]["memory"]["type"], expected)

    def test_non_string_type_still_fails_closed(self):
        for offered in (None, 7, [], {}):
            with self.subTest(offered=offered):
                out = self.run_rows(self.create(type=offered, scope="global"))
                self.assertEqual(out["issues"][0]["code"], "invalid_type")

    def test_exact_live_decision_shape_is_normalized(self):
        snap = PlanningSnapshot.build(evidence=[
            evidence("e1", text="那就用A吧。", seq=3, role="user"),
            evidence("e2", text="好的，Orion数据库就使用MySQL。", seq=4, role="assistant"),
        ])
        row = {
            "action": "CREATE",
            "evidence": ["e1", "e2"],
            "memory": {
                "type": "decision",
                "scope": "project:Orion",
                "title": "Orion 数据库选型",
                "body": "Orion 项目的数据库确定使用 MySQL（用户选择方案 A）。",
                "status": "active",
            },
        }
        out = self.run_rows(row, snapshot=snap)
        self.assertFalse(out["issues"])
        op = out["operations"][0]
        self.assertEqual(op["memory"]["type"], "fact")
        self.assertEqual(op["memory"]["scopes"], ["unscoped"])
        self.assertNotIn("status", op["memory"])

    def test_new_todo_defaults_active(self):
        out = self.run_rows(self.create(type="todo"))
        self.assertFalse(out["issues"])
        self.assertEqual(out["operations"][0]["memory"]["status"], "active")

    def test_new_todo_accepts_optional_fields(self):
        op = self.run_rows(self.create(type="todo", status="active", assignee="user:1", waiting_on=None))["operations"][0]
        self.assertEqual(op["memory"]["assignee"], "user:1")

    def test_target_type_cannot_be_reclassified_in_patch(self):
        out = self.run_rows(self.update(type="fact", status="completed"))
        self.assertFalse(out["operations"])
        self.assertEqual(out["issues"][0]["code"], "invalid_fields")

    def test_unknown_reference_does_not_become_create(self):
        row = self.update(status="completed"); row["target"] = "m77"
        out = self.run_rows(row)
        self.assertFalse(out["operations"])
        self.assertEqual(out["issues"][0]["code"], "invalid_reference")

    def test_evidence_and_memory_namespaces_do_not_mix(self):
        row = self.update(status="completed"); row["evidence"] = ["m1"]
        out = self.run_rows(row)
        self.assertFalse(out["operations"])
        self.assertEqual(out["coverage"]["unresolved_evidence"], ["e1"])

    def test_numeric_reference_not_guessed(self):
        row = self.update(status="completed"); row["evidence"] = [1]
        self.assertEqual(self.run_rows(row)["issues"][0]["code"], "invalid_reference")

    def test_unambiguous_whitespace_and_case_normalization(self):
        row = self.update(status="completed"); row.update(action="update", target=" m1 ", evidence=[" e1 "])
        self.assertFalse(self.run_rows(row)["issues"])

    def test_invalid_sibling_does_not_erase_valid_no_memory(self):
        snap = PlanningSnapshot.build(evidence=[evidence(), evidence("e2")])
        out = self.run_rows({"action": "NO_MEMORY", "evidence": ["e1"]},
                            {"action": "UPDATE", "target": "missing", "evidence": ["e2"], "patch": {"body": "bad"}}, snapshot=snap)
        self.assertEqual(out["operations"], [{"action": "NO_MEMORY", "evidence": ["e1"]}])
        self.assertEqual(out["coverage"]["unresolved_evidence"], ["e2"])

    def test_one_invalid_row_poison_entire_target_group(self):
        out = self.run_rows(self.update(status="completed"), self.update(unknown="bad"), self.create())
        self.assertEqual([op["action"] for op in out["operations"]], ["CREATE"])
        self.assertIn("invalid_target_group", [r["code"] for r in out["issues"]])

    def test_compatible_target_rows_compile_once(self):
        out = self.run_rows(self.update(status="completed"), self.update(body="Deliver the result."))
        self.assertEqual(len(out["operations"]), 1)
        self.assertEqual(out["operations"][0]["memory"]["status"], "completed")

    def test_conflicting_target_rows_never_use_last_value(self):
        out = self.run_rows(self.update(status="completed"), self.update(status="cancelled"))
        self.assertFalse(out["operations"])
        self.assertEqual(out["issues"][0]["code"], "conflicting_target_patch")

    def test_no_change_needs_valid_target(self):
        out = self.run_rows({"action": "NO_CHANGE", "evidence": ["e1"], "target": "m1"})
        self.assertEqual(out["operations"][0]["target"], "mem-1")
        self.assertNotIn("memory", out["operations"][0])

    def test_turn_level_no_memory_needs_no_fake_id_or_evidence(self):
        out = self.run_rows({"action": "NO_MEMORY"})
        self.assertEqual(out["coverage"]["status"], "complete")
        self.assertEqual(out["operations"][0]["evidence"], ["e1"])
        self.assertNotIn("target", out["operations"][0])

    def test_legacy_evidence_bound_no_memory_remains_accepted(self):
        out = self.run_rows({"action": "NO_MEMORY", "evidence": ["e1"]})
        self.assertEqual(out["coverage"]["status"], "complete")

    def test_turn_level_no_memory_cannot_coexist_with_memory_action(self):
        out = self.run_rows(self.create(), {"action": "NO_MEMORY"})
        self.assertEqual([o["action"] for o in out["operations"]], ["CREATE"])
        self.assertEqual(out["coverage"]["status"], "partial")
        self.assertIn("conflicting_turn_disposition", [i["code"] for i in out["issues"]])

    def test_unreferenced_assistant_message_does_not_make_turn_partial(self):
        snap = PlanningSnapshot.build(evidence=[
            evidence(), evidence("e2", text="已了解。", seq=3, role="assistant")
        ])
        out = self.run_rows(self.create(scope="global"), snapshot=snap)
        self.assertFalse(out["issues"])
        self.assertEqual(out["coverage"]["status"], "complete")

    def test_deferred_requires_concrete_need(self):
        out = self.run_rows({"action": "DEFERRED", "evidence": ["e1"], "reason": "conflict", "need": "Which task?"})
        self.assertEqual(out["coverage"]["status"], "partial")
        bad = self.run_rows({"action": "DEFERRED", "evidence": ["e1"], "reason": "protocol", "need": "x"})
        self.assertEqual(bad["issues"][0]["code"], "invalid_reason")

    def test_explicit_remember_cannot_be_automatic_discard(self):
        snap = PlanningSnapshot.build(evidence=[evidence()], request_kind="explicit_remember")
        out = self.run_rows({"action": "NO_MEMORY"}, snapshot=snap)
        self.assertEqual(out["issues"][0]["code"], "explicit_retention_required")

    def test_context_only_cannot_assert_new_fact(self):
        snap = PlanningSnapshot.build(evidence=[evidence(), evidence("e2", use="context")])
        row = self.create(scope="global"); row["evidence"] = ["e2"]
        self.assertEqual(self.run_rows(row, snapshot=snap)["issues"][0]["code"], "missing_new_evidence")

    def test_multiple_new_do_not_require_at(self):
        snap = PlanningSnapshot.build(evidence=[
            evidence("e1", seq=2, role="user"),
            evidence("e2", text="已确认。", seq=3, role="assistant"),
        ])
        row = self.create(scope="global"); row["evidence"] = ["e1", "e2"]
        out = self.run_rows(row, snapshot=snap)
        self.assertFalse(out["issues"])
        self.assertEqual(out["operations"][0]["memory"]["field_basis"]["content"]["source_sequence"], 2)

    def test_explicit_invalid_at_is_still_rejected(self):
        snap = PlanningSnapshot.build(evidence=[evidence(), evidence("e2")])
        row = self.create(scope="global"); row.update(evidence=["e1", "e2"], at="missing")
        self.assertEqual(self.run_rows(row, snapshot=snap)["issues"][0]["code"], "invalid_at")

    def test_non_todo_default_active_is_schema_noise_not_failure(self):
        row = self.create(type="project", scope="global", status="active")
        out = self.run_rows(row)
        self.assertFalse(out["issues"])
        self.assertNotIn("status", out["operations"][0]["memory"])

    def test_non_todo_meaningful_lifecycle_fields_remain_rejected(self):
        for fields in ({"status":"completed"}, {"assignee":"user:1"}, {"deadline":{"ref":"e1","text":"明天"}}):
            with self.subTest(fields=fields):
                out = self.run_rows(self.create(type="project", scope="global", **fields))
                self.assertEqual(out["issues"][0]["code"], "todo_fields_on_non_todo")

    def test_live_orion_shape_is_normalized_without_losing_memory(self):
        snap = PlanningSnapshot.build(evidence=[
            evidence("e1", text="那就用A吧。", seq=3, role="user"),
            evidence("e2", text="好的，Orion数据库就使用MySQL。", seq=4, role="assistant"),
        ])
        row = {
            "action": "CREATE",
            "evidence": ["e1", "e2"],
            "memory": {
                "type": "project",
                "scope": "project:Orion",
                "title": "Orion 数据库选型",
                "body": "Orion 项目的数据库确定使用 MySQL（用户选择方案 A）。",
                "status": "active",
            },
        }
        out = self.run_rows(row, snapshot=snap)
        self.assertFalse(out["issues"])
        op = out["operations"][0]
        self.assertEqual(op["memory"]["body"], "Orion 项目的数据库确定使用 MySQL（用户选择方案 A）。")
        self.assertEqual(op["memory"]["scopes"], ["unscoped"])
        self.assertNotIn("status", op["memory"])
        self.assertEqual(op["memory"]["field_basis"]["content"]["source_sequence"], 3)

    def test_explicit_scope_is_write_boundary(self):
        snap = PlanningSnapshot.build(evidence=[evidence()], scopes={"s1": "project:Atlas", "s2": "project:Beacon"}, write_scopes=["project:Atlas"])
        row = self.create(scope="s2")
        out = self.run_rows(row, snapshot=snap)
        self.assertEqual(out["issues"][0]["code"], "blocked_scope")
        self.assertEqual(row["memory"]["scope"], "s2")

    def test_scope_move_requires_both_boundaries(self):
        snap = PlanningSnapshot.build(evidence=[evidence()], targets={"m1": self.target}, scopes={"s2": "project:Beacon"}, write_scopes=["project:Beacon"])
        out = self.run_rows(self.update(scope="s2"), snapshot=snap)
        self.assertEqual(out["issues"][0]["code"], "blocked_scope")

    def test_unscoped_is_not_global(self):
        op = self.run_rows(self.create(scope="unscoped"))["operations"][0]
        self.assertEqual(op["memory"]["scopes"], ["unscoped"])

    def test_new_create_scope_without_permission_falls_back_unscoped(self):
        out = self.run_rows(self.create(scope="project:Fresh"))
        self.assertFalse(out["issues"])
        self.assertEqual(out["operations"][0]["memory"]["scopes"], ["unscoped"])
        snap = PlanningSnapshot.build(evidence=[evidence()], allow_new_scopes=True)
        self.assertEqual(self.run_rows(self.create(scope="project:Fresh"), snapshot=snap)["operations"][0]["memory"]["scopes"], ["project:Fresh"])

    def test_update_scope_without_permission_still_fails_closed(self):
        row = self.update(scope="project:Fresh")
        out = self.run_rows(row)
        self.assertEqual(out["issues"][0]["code"], "invalid_scope")

    def test_read_only_target_allows_no_change_not_update(self):
        snap = PlanningSnapshot.build(evidence=[evidence()], targets={"m1": self.target}, writable={"m1": False})
        self.assertEqual(self.run_rows(self.update(status="completed"), snapshot=snap)["issues"][0]["code"], "read_only_target")
        out = self.run_rows({"action": "NO_CHANGE", "evidence": ["e1"], "target": "m1"}, snapshot=snap)
        self.assertFalse(out["issues"])

    def test_incomplete_context_blocks_unmatched_create(self):
        snap = PlanningSnapshot.build(evidence=[evidence()], context_complete=False)
        self.assertEqual(self.run_rows(self.create(scope="global"), snapshot=snap)["issues"][0]["code"], "blocked_context")

    def test_late_independent_fact_not_rejected_just_for_old_date(self):
        snap = PlanningSnapshot.build(evidence=[evidence(time="2020-01-01T10:00:00+08:00")])
        self.assertFalse(self.run_rows(self.create(scope="global"), snapshot=snap)["issues"])

    def test_stale_field_invalidates_whole_target(self):
        snap = PlanningSnapshot.build(evidence=[evidence(seq=0)], targets={"m1": self.target})
        out = self.run_rows(self.update(status="completed"), self.update(body="new text"), snapshot=snap)
        self.assertFalse(out["operations"])
        self.assertIn("stale_observation", [i["code"] for i in out["issues"]])

    def test_reopen_needs_flag_and_later_basis(self):
        target = memory(status="completed")
        snap = PlanningSnapshot.build(evidence=[evidence()], targets={"m1": target})
        row = self.update(status="active")
        self.assertEqual(self.run_rows(row, snapshot=snap)["issues"][0]["code"], "explicit_reopen_required")
        row["reopen"] = True
        self.assertFalse(self.run_rows(row, snapshot=snap)["issues"])

    def test_retract_uses_same_id_and_clears_body(self):
        out = self.run_rows(self.update(validity="retracted"))
        self.assertEqual(out["operations"][0]["memory"]["body"], "")
        self.assertEqual(out["operations"][0]["target"], "mem-1")
        self.assertEqual(self.target.body, "Deliver the result.")

    def test_retracted_head_requires_new_explicit_restore(self):
        target = memory(body="", validity="retracted", field_basis={"validity": {"source": "hermes", "session_id": "s", "source_sequence": 1}})
        snap = PlanningSnapshot.build(evidence=[evidence()], targets={"m1": target})
        self.assertEqual(self.run_rows(self.update(body="revived"), snapshot=snap)["issues"][0]["code"], "explicit_restore_required")
        self.assertFalse(self.run_rows(self.update(validity="valid", body="new confirmed fact"), snapshot=snap)["issues"])

    def test_retract_cannot_keep_assertion_body(self):
        self.assertEqual(self.run_rows(self.update(validity="retracted", body="still a fact"))["issues"][0]["code"], "retracted_body_must_be_empty")

    def test_empty_items_is_not_implicit_no_memory(self):
        out = self.run_rows()
        self.assertEqual(out["coverage"]["unresolved_evidence"], ["e1"])
        self.assertEqual(out["issues"][0]["code"], "missing_turn_disposition")

    def test_strict_envelope_rejects_unparseable_results(self):
        for raw in ('', '```json\n{}\n```', '{"items":[],"items":[]}', '{"items":NaN}', '{"items": {}}', '{"items":[],"deferred":[]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                compile_incremental(raw, self.snapshot)

    def test_invalid_container_values_isolate_rows(self):
        for field, value in (("status", []), ("validity", {}), ("body", False), ("assignee", 3), ("deadline", [])):
            with self.subTest(field=field):
                out = self.run_rows(self.update(**{field: value}))
                self.assertFalse(out["operations"])
                self.assertTrue(out["issues"])

    def test_snapshot_is_independent_of_caller_mutation(self):
        before = self.snapshot.snapshot_id
        self.target.body = "mutated"
        state = self.snapshot.state(); state["evidence"][0]["text"] = "mutated"
        self.assertEqual(before, self.snapshot.snapshot_id)
        self.assertEqual(self.snapshot.state()["targets"]["m1"]["memory"]["body"], "Deliver the result.")

    def test_model_view_keeps_validity_not_control_ids(self):
        view = self.snapshot.model_input()
        row = view["memories"][0]
        self.assertEqual(row["validity"], "valid")
        self.assertNotIn("memory_id", row)
        self.assertNotIn("revision", row)
        self.assertNotIn("event_key", view["evidence"][0])

    def test_duplicate_memory_identity_rejected_in_context(self):
        with self.assertRaisesRegex(ValueError, "duplicate_memory_id"):
            PlanningSnapshot.build(evidence=[evidence()], targets={"m1": self.target, "m2": deepcopy(self.target)})

    def test_oversize_snapshot_does_not_truncate(self):
        target = memory(body="汉" * 60000)
        with self.assertRaisesRegex(ValueError, "blocked_context"):
            PlanningSnapshot.build(evidence=[evidence()], targets={"m1": target})

    def test_staged_prompt_size_and_json_example(self):
        import re
        from memleaf.incremental_prompts import INCREMENTAL_SYSTEM
        self.assertLessEqual(len(INCREMENTAL_SYSTEM),1861)
        self.assertLessEqual(len(INCREMENTAL_SYSTEM.encode("utf-8")),3881)
        self.assertIn("explicit_remember",INCREMENTAL_SYSTEM)
        self.assertIn("validity=valid/retracted",INCREMENTAL_SYSTEM)
        example=re.search(r'(?m)^\{"items":.*$',INCREMENTAL_SYSTEM).group(0)
        self.assertFalse(compile_incremental(example,self.snapshot)["issues"])
