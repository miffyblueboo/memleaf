from pathlib import Path

path = Path("tests/test_stage_c1_mcp.py")
text = path.read_text(encoding="utf-8")
old = "from memleaf import Memleaf\n"
new = "from memleaf import Memleaf, __version__\n"
if text.count(old) != 1:
    raise SystemExit(f"stage c1 import marker count={text.count(old)}")
text = text.replace(old, new, 1)
old = '{"name": "memleaf", "version": "0.2.22"},'
new = '{"name": "memleaf", "version": __version__},'
if text.count(old) != 1:
    raise SystemExit(f"stage c1 version marker count={text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("release version assertions patched")
