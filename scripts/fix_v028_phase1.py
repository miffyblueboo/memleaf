from pathlib import Path

root = Path(__file__).resolve().parents[1]
state_test = root / "tests" / "test_state_layout_v028.py"
text = state_test.read_text(encoding="utf-8")
text = text.replace(
    '(self.root / "_state" / "processed.json").write_text("{broken", encoding="utf-8")',
    '(self.root / "_index" / "processed.json").write_text("{broken", encoding="utf-8")',
)
text = text.replace(
    'atomic_write_json(self.root / "_state" / "processed.json", old_value)',
    'atomic_write_json(self.root / "_index" / "processed.json", old_value)',
)
state_test.write_text(text, encoding="utf-8")

config_test = root / "tests" / "test_config_migrations_v028.py"
text = config_test.read_text(encoding="utf-8")
text = text.replace(
    'self.assertIn("tool_evidence_mode: metadata", written)',
    'self.assertIn(\'tool_evidence_mode: "metadata"\', written)',
)
config_test.write_text(text, encoding="utf-8")

for test_path in (root / "tests").rglob("test_*.py"):
    if test_path.name == "test_state_layout_v028.py":
        continue
    text = test_path.read_text(encoding="utf-8")
    text = text.replace(
        'self.vault.index_path / "retrieval_gate.json"',
        'self.vault.retrieval_gate_state_path',
    )
    text = text.replace(
        'self.service.vault.index_path / "retrieval_gate.json"',
        'self.service.vault.retrieval_gate_state_path',
    )
    test_path.write_text(text, encoding="utf-8")

print("v0.2.28 phase-1 test fixture fixes applied")
