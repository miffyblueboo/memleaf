from pathlib import Path

path = Path("tests/test_b3_single_pass_memory_planner.py")
text = path.read_text(encoding="utf-8")
old = '    def config(self): return {"scopes": self.scopes}\n'
new = '    def config(self): return {"scopes": self.scopes, "capture": {"tool_evidence_mode": "off"}}\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one Vault.config fixture anchor, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
