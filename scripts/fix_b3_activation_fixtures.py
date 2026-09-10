from pathlib import Path

path = Path("tests/test_b3_single_pass_plan.py")
text = path.read_text(encoding="utf-8")
old = '    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None):\n'
new = '    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None, max_attempts=None):\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one single-pass FakeExecutor signature, found {text.count(old)}")
text = text.replace(old, new, 1)
old = '        self.assertEqual(executor.calls[0]["purpose"], "gate")\n'
new = '        self.assertEqual(executor.calls[0]["purpose"], "single_pass")\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one old B3 purpose assertion, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

path = Path("tests/test_b3_single_pass_memory_planner.py")
text = path.read_text(encoding="utf-8")
# The B3 integration suite intentionally exercises the safe path. Production
# fallback behavior is tested separately in test_b3_activation.
anchor = 'TURN_KEY = "a" * 64\n'
if text.count(anchor) != 1:
    raise SystemExit("TURN_KEY anchor missing")
text = text.replace(anchor, anchor + 'SAFE_BACKEND = SimpleNamespace(single_pass_safe=True)\n', 1)
for signature in (
    '    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None):\n',
):
    count = text.count(signature)
    if count != 2:
        raise SystemExit(f"expected 2 planner fake executor signatures, found {count}")
    text = text.replace(
        signature,
        '    def _complete_json_stage(self, backend, prompt, *, system, purpose, parser, diagnostic_context=None, max_attempts=None):\n',
    )
count = text.count('_collect_turn_outputs("backend",')
if count < 1:
    raise SystemExit("no B3 planner backend fixtures found")
text = text.replace('_collect_turn_outputs("backend",', '_collect_turn_outputs(SAFE_BACKEND,')
text = text.replace('            "backend",\n            turn(', '            SAFE_BACKEND,\n            turn(')
old = '        self.assertEqual(model.calls[0][0], "gate")\n'
new = '        self.assertEqual(model.calls[0][0], "single_pass")\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one old planner stage assertion, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
