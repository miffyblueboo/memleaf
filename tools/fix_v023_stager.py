from pathlib import Path

path = Path("tools/apply_v023_core.py")
text = path.read_text(encoding="utf-8")
old1 = '    "            duplicate_id = request.get(\\\"duplicate_memory_id\\\")\\n",\n'
new1 = '    "            summary = request[\\\"summary\\\"]\\n            duplicate_id = request.get(\\\"duplicate_memory_id\\\")\\n",\n'
if text.count(old1) != 1:
    raise SystemExit(f"preflight anchor occurrences={text.count(old1)}")
text = text.replace(old1, new1, 1)
old2 = '    "        duplicate_id = request.get(\\\"duplicate_memory_id\\\")\\n",\n'
new2 = '    "        duplicate_id = request.get(\\\"duplicate_memory_id\\\")\\n        if duplicate_id is not None:\\n",\n'
if text.count(old2) != 1:
    raise SystemExit(f"write anchor occurrences={text.count(old2)}")
text = text.replace(old2, new2, 1)
path.write_text(text, encoding="utf-8")
print("v0.2.23 stager anchors repaired")
