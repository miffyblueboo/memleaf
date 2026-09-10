from pathlib import Path


def replace_exact(path: str, old: str, new: str) -> None:
    file = Path(path)
    source = file.read_text(encoding="utf-8")
    count = source.count(old)
    assert count == 1, (path, count)
    file.write_text(source.replace(old, new, 1), encoding="utf-8")


# Evidence rows are unit-level. Multiple bound spans from the same visible
# event are legal; unit_id, not event_key, is the row identity.
replace_exact(
    "src/memleaf/maintenance_plan_stage.py",
    '''    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ModelOutputError("P4 evidence row must be an object", validation_detail="source_shape")
        event_key = raw.get("event_key")
        role = raw.get("role")
        content = raw.get("content")
        if not isinstance(event_key, str) or not event_key or event_key in seen:
            raise ModelOutputError("P4 evidence requires unique event_key", validation_detail="invalid_evidence")
        if role not in {"user", "assistant"}:
            raise ModelOutputError("P4 evidence must be visible conversation content", validation_detail="invalid_evidence")
        if not isinstance(content, str) or not content:
            raise ModelOutputError("P4 evidence requires content", validation_detail="invalid_evidence")
        seen.add(event_key)
''',
    '''    result: list[dict[str, Any]] = []
    seen_unit_ids: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ModelOutputError("P4 evidence row must be an object", validation_detail="source_shape")
        unit_id = raw.get("unit_id")
        event_key = raw.get("event_key")
        role = raw.get("role")
        content = raw.get("content")
        if not isinstance(unit_id, str) or not unit_id or unit_id in seen_unit_ids:
            raise ModelOutputError("P4 evidence requires unique unit_id", validation_detail="invalid_evidence")
        if not isinstance(event_key, str) or not event_key:
            raise ModelOutputError("P4 evidence requires event_key", validation_detail="invalid_evidence")
        if role not in {"user", "assistant"}:
            raise ModelOutputError("P4 evidence must be visible conversation content", validation_detail="invalid_evidence")
        if not isinstance(content, str) or not content:
            raise ModelOutputError("P4 evidence requires content", validation_detail="invalid_evidence")
        seen_unit_ids.add(unit_id)
''',
)

# P4 UPDATE outer target and the existing Summary contract must remain aligned.
replace_exact(
    "src/memleaf/maintenance_plan_adapter.py",
    '''        if decision == "UPDATE" and not isinstance(target, str):
            raise ModelOutputError("UPDATE requires a target", validation_detail="invalid_update_target")
        parser = parser_factory(candidate_id, decision, target)
''',
    '''        if decision == "UPDATE" and not isinstance(target, str):
            raise ModelOutputError("UPDATE requires a target", validation_detail="invalid_update_target")
        if decision == "UPDATE":
            summary_target = summary.get("update_memory_id")
            if (
                not isinstance(summary_target, str)
                or summary_target.casefold() != target.casefold()
            ):
                raise ModelOutputError(
                    "UPDATE summary must keep the canonical maintenance target",
                    validation_detail="invalid_update_target",
                )
        parser = parser_factory(candidate_id, decision, target)
''',
)

# Existing stage tests now identify every evidence row by unit_id and prove that
# one event can legally contribute multiple unit-level spans.
path = Path("tests/test_p4_maintenance_plan_stage.py")
source = path.read_text(encoding="utf-8")
anchor = '''    return [{
        "event_key": f"event-{candidate_id}",
'''
assert source.count(anchor) == 1
source = source.replace(
    anchor,
    '''    return [{
        "unit_id": f"unit-{candidate_id}",
        "event_key": f"event-{candidate_id}",
''',
    1,
)
marker = "    def test_tool_role_cannot_enter_admitted_evidence(self):\n"
assert source.count(marker) == 1
case = '''    def test_multiple_units_from_same_event_are_allowed_but_unit_ids_are_unique(self):
        rows = evidence("c1")
        rows.append({
            "unit_id": "unit-c1-second",
            "event_key": rows[0]["event_key"],
            "role": "user",
            "content": "Second exact span from the same message.",
            "evidence_origin": "user_assertion",
        })
        prompt, _, _ = build_maintenance_plan_prompt(
            candidates=[candidate("c1")],
            evidence_by_candidate={"c1": rows},
            lookup_states={"c1": lookup("complete_no_target")},
            related_memories_by_candidate={"c1": []},
        )
        self.assertIn("unit-c1-second", prompt)
        rows[1]["unit_id"] = rows[0]["unit_id"]
        with self.assertRaises(ModelOutputError):
            build_maintenance_plan_prompt(
                candidates=[candidate("c1")],
                evidence_by_candidate={"c1": rows},
                lookup_states={"c1": lookup("complete_no_target")},
                related_memories_by_candidate={"c1": []},
            )

'''
source = source.replace(marker, case + marker, 1)
path.write_text(source, encoding="utf-8")

# Adapter tests require the UPDATE summary to retain the canonical outer target.
path = Path("tests/test_p4_maintenance_plan_adapter.py")
source = path.read_text(encoding="utf-8")
old_call = '''        update = validator("c2", "UPDATE", "Mem-2", {"title": "C", "body": "D"})
'''
assert source.count(old_call) == 1
source = source.replace(
    old_call,
    '''        update = validator(
            "c2",
            "UPDATE",
            "Mem-2",
            {"update_memory_id": "mem-2", "title": "C", "body": "D"},
        )
''',
    1,
)
old_tail = '''        with self.assertRaises(ModelOutputError):
            validator("c3", "UPDATE", None, {"title": "E", "body": "F"})

'''
assert source.count(old_tail) == 1
source = source.replace(
    old_tail,
    '''        with self.assertRaises(ModelOutputError):
            validator("c3", "UPDATE", None, {"title": "E", "body": "F"})
        with self.assertRaises(ModelOutputError):
            validator("c3", "UPDATE", "m3", {"title": "E", "body": "F"})
        with self.assertRaises(ModelOutputError):
            validator(
                "c3",
                "UPDATE",
                "m3",
                {"update_memory_id": "wrong", "title": "E", "body": "F"},
            )

''',
    1,
)
path.write_text(source, encoding="utf-8")
