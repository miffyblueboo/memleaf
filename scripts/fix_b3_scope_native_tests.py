from pathlib import Path

ctx = Path("tests/test_b3_planning_context.py")
text = ctx.read_text(encoding="utf-8")
old = 'turn = SimpleNamespace(events=[SimpleNamespace(content="Alpha changed")])'
new = 'turn = SimpleNamespace(events=[SimpleNamespace(role="user", content="Alpha changed")])'
if text.count(old) != 3:
    raise SystemExit(f"expected 3 simple turn fixtures, found {text.count(old)}")
ctx.write_text(text.replace(old, new), encoding="utf-8")

planner = Path("tests/test_b3_single_pass_memory_planner.py")
text = planner.read_text(encoding="utf-8")
old = '"evidence": [item_claim(prompt, "New流程要求仍是双人复核，之前归错到Old。")],'
new = '"evidence": [{"unit_id": payload["current_evidence"][0]["unit_id"], "whole_unit": True, "role": "assertion"}],'
if text.count(old) != 1:
    raise SystemExit(f"expected one scope correction claim fixture, found {text.count(old)}")
planner.write_text(text.replace(old, new, 1), encoding="utf-8")
