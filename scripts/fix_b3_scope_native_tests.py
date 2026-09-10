from pathlib import Path

ctx = Path("tests/test_b3_planning_context.py")
text = ctx.read_text(encoding="utf-8")
old = 'turn = SimpleNamespace(events=[SimpleNamespace(content="Alpha changed")])'
new = 'turn = SimpleNamespace(events=[SimpleNamespace(role="user", content="Alpha changed")])'
if text.count(old) != 1:
    raise SystemExit(f"expected one simple turn fixture, found {text.count(old)}")
ctx.write_text(text.replace(old, new, 1), encoding="utf-8")

planner = Path("tests/test_b3_single_pass_memory_planner.py")
text = planner.read_text(encoding="utf-8")
old = '            assistant_uid = payload["current_evidence"][1]["unit_id"]\n            return {\n                "protocol_version": PROTOCOL_VERSION,\n                "items": [{\n                    "candidate_id": "c1",\n                    "decision": "UPDATE",\n                    "target_memory_id": "m-old",\n                    "scopes": ["project:New"],\n                    "scope_source": "model",\n                    "evidence": [item_claim(prompt, "New流程要求仍是双人复核，之前归错到Old。")],'
new = '            assistant_uid = next(row["unit_id"] for row in payload["current_evidence"] if row["role"] == "assistant")\n            return {\n                "protocol_version": PROTOCOL_VERSION,\n                "items": [{\n                    "candidate_id": "c1",\n                    "decision": "UPDATE",\n                    "target_memory_id": "m-old",\n                    "scopes": ["project:New"],\n                    "scope_source": "model",\n                    "evidence": [{"unit_id": row["unit_id"], "whole_unit": True, "role": "assertion"} for row in payload["current_evidence"] if row["role"] == "user"],'
if text.count(old) != 1:
    raise SystemExit(f"expected one scope correction response fixture, found {text.count(old)}")
planner.write_text(text.replace(old, new, 1), encoding="utf-8")
