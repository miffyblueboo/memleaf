from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# Core may sort deterministic todo fields, but it must not infer business
# urgency from title/body/tag/keyword text.
path = "src/memleaf/service.py"
text = read(path)
text, count = re.subn(
    r'\n    @staticmethod\n    def _todo_is_asap\(memory: Memory\) -> bool:\n        text = "\\n"\.join\(\[memory\.title, memory\.body, \*memory\.tags, \*memory\.keywords\]\)\.casefold\(\)\n        return any\(marker in text for marker in \("尽快", "紧急", "优先处理", "asap", "urgent"\)\)\n',
    "",
    text,
    count=1,
)
if count != 1:
    raise SystemExit(f"todo semantic classifier removal count={count}")
text = text.replace(
    "                    bucket = 3 if self._todo_is_asap(memory) else 4\n"
    "                    sort_key = (bucket, date.max, memory.title.casefold(), memory.memory_id)\n",
    "                    sort_key = (3, date.max, memory.title.casefold(), memory.memory_id)\n",
    1,
)
if "_todo_is_asap" in text or any(marker in text for marker in ("尽快", "紧急", "优先处理")):
    raise SystemExit("todo text-derived urgency rule still present")
write(path, text)

# Remove obsolete architectural stage labels without touching current
# gate/summarize runtime stage names.
replacements = {
    "src/memleaf/llm/__init__.py": (
        '"""Model backends and explicit routing for stage B."""',
        '"""Model backends and explicit routing."""',
    ),
    "src/memleaf/llm/base.py": (
        '"""Small injectable model backend interfaces used by stage B."""',
        '"""Small injectable model backend interfaces."""',
    ),
    "src/memleaf/memory_writer.py": (
        '"""Deterministic Markdown writes for the stage-B processing slice."""',
        '"""Deterministic Markdown writes for memory processing."""',
    ),
    "src/memleaf/validation.py": (
        '"""Strict, side-effect-free validation for stage-B1 model JSON."""',
        '"""Strict, side-effect-free validation for model JSON."""',
    ),
}
for target, (old, new) in replacements.items():
    value = read(target)
    if old not in value:
        raise SystemExit(f"obsolete stage label changed unexpectedly: {target}")
    write(target, value.replace(old, new, 1))

# Host activation is state, not a derived index. Retain the two old init flags
# as explicitly finite no-op CLI compatibility rather than silently breaking
# old setup scripts in a 0.2.x release.
path = "src/memleaf/cli.py"
text = read(path)
text = text.replace(
    'help="compatibility no-op; use install --host codex for explicit Codex setup",',
    'help="deprecated compatibility no-op through 0.2.x; use install --host codex (removal planned for 0.3)",',
    1,
)
text = text.replace(
    'help="accepted for compatibility; Antigravity is currently unsupported",',
    'help="deprecated compatibility no-op through 0.2.x; Antigravity is unsupported (removal planned for 0.3)",',
    1,
)
text = text.replace("agents index not written:", "agents state not written:")
text = text.replace("agents index:", "agents state:")
write(path, text)

# Document the bounded external CLI compatibility decision.
path = "docs/config-migrations.md"
text = read(path)
marker = "## Automatic migration\n"
compatibility = (
    "## CLI compatibility sunset\n\n"
    "`memleaf init --no-codex` and `memleaf init --no-antigravity` are retained only as deprecated no-op argument compatibility for existing 0.2.x setup scripts. They do not select runtime behavior and are scheduled for removal in v0.3. The legacy `init --json` host result slots are likewise retained through 0.2.x so automation does not break during this maintenance release. New integrations must use `memleaf install --host ...` and the current host-state fields.\n\n"
)
if compatibility not in text:
    if marker not in text:
        raise SystemExit("config migration document insertion point missing")
    text = text.replace(marker, compatibility + marker, 1)
write(path, text)

# The indexed and project-filtered benchmark query must actually have scoped
# matches. Use the target memory's own tag/project instead of an incompatible
# hard-coded modular pair.
path = "scripts/benchmark_long_run.py"
text = read(path)
needle = (
    '    target_id = f"bench-{query_index:06d}"\n'
    '    target_token = f"needle-{query_index:06d}"\n'
    '    project = f"project:p{query_index % PROJECT_COUNT:02d}"\n'
)
replacement = (
    '    target_id = f"bench-{query_index:06d}"\n'
    '    target_token = f"needle-{query_index:06d}"\n'
    '    indexed_query = f"bench-tag-{query_index % 64:02d}"\n'
    '    project = f"project:p{query_index % PROJECT_COUNT:02d}"\n'
)
if needle not in text:
    raise SystemExit("benchmark query setup changed unexpectedly")
text = text.replace(needle, replacement, 1)
old_count = text.count('"bench-tag-07"')
if old_count < 3:
    raise SystemExit(f"benchmark expected at least 3 hard-coded query uses, found {old_count}")
text = text.replace('"bench-tag-07"', "indexed_query")
write(path, text)

# Explicit regression: unscheduled todos are sorted only by deterministic
# metadata/title/id, not by business-language urgency markers.
test = r'''from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memleaf.models import Memory
from memleaf.service import Memleaf


class SourceNeutralTodoOrderingV028Tests(unittest.TestCase):
    def test_unscheduled_todo_order_does_not_parse_urgency_words(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-source-neutral-todos-") as temporary:
            service = Memleaf(Path(temporary) / "vault")
            memories = [
                Memory.new(
                    memory_id="todo-z-urgent",
                    title="Z urgent cleanup",
                    body="Please handle this ASAP and 紧急处理",
                    type="todo",
                    scopes=["global"],
                    status="active",
                    sources=[],
                ),
                Memory.new(
                    memory_id="todo-a-regular",
                    title="A regular cleanup",
                    body="Ordinary unscheduled todo",
                    type="todo",
                    scopes=["global"],
                    status="active",
                    sources=[],
                ),
            ]
            for memory in memories:
                service.vault.memory_path(memory.memory_id, "knowledge").write_text(
                    memory.to_markdown(), encoding="utf-8"
                )
            service.rebuild_index()
            result = service.list_todos(status="active", include_unscheduled=True, limit=20)
            self.assertEqual(result["status"], "found")
            self.assertEqual(
                [item["memory_id"] for item in result["results"]],
                ["todo-a-regular", "todo-z-urgent"],
            )


if __name__ == "__main__":
    unittest.main()
'''
write("tests/test_source_neutral_todos_v028.py", test)

print("v0.2.28 phase-3 cleanup applied")
