from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def edit(path: str, old: str, new: str, *, minimum: int = 1) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f"{path}: follow-up pattern missing ({count}): {old[:100]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


# Keep durable project/risk/rule records as project. Only the known execution-plan
# adjustment shape is deterministically corrected from project -> todo; ordinary
# actionable fact/event/other candidates may still be normalized to todo.
edit(
    "src/memleaf/validation.py",
    '''        if (
            item["worth"]
            and candidate_type in {"fact", "event", "other", "project"}
            and is_actionable_todo_text(item["memory"])
        ):
            item["type"] = "todo"
            candidate_type = "todo"
''',
    '''        if (
            item["worth"]
            and (
                (
                    candidate_type in {"fact", "event", "other"}
                    and is_actionable_todo_text(item["memory"])
                )
                or (
                    candidate_type == "project"
                    and _ACTION_PLAN_ADJUST.search(item["memory"])
                )
            )
        ):
            item["type"] = "todo"
            candidate_type = "todo"
''',
)

# The public MCP read sanitizer must retain the new todo metadata emitted by Core.
edit(
    "src/memleaf/mcp_server.py",
    '''    return {name: _jsonable(value[name]) for name in fields if name in value}
''',
    '''    result = {name: _jsonable(value[name]) for name in fields if name in value}
    memory_type = value.get("type")
    status = value.get("status")
    due_date = value.get("due_date")
    if memory_type is not None and not isinstance(memory_type, str):
        raise ValueError("invalid read memory type")
    if status is not None and not isinstance(status, str):
        raise ValueError("invalid read todo status")
    if due_date is not None and not isinstance(due_date, str):
        raise ValueError("invalid read todo due date")
    if "type" in value:
        result["type"] = memory_type
    if "status" in value:
        result["status"] = status
    if "due_date" in value:
        result["due_date"] = due_date
    return result
''',
)

# Preserve legacy wording relied on by provider compatibility tests while making
# the all-todo exception explicit.
edit(
    "src/memleaf/hermes_provider/__init__.py",
    '''        "this map. Use list_todos instead of relevance search for global current-todo questions. "
        "Search/list_todos return directories; read selected memory bodies when needed. A no-match result is valid.\\n"
''',
    '''        "this map. Use list_todos instead of relevance search for global current-todo questions. "
        "Search/list_todos return directories; read only the selected memory when needed for ordinary "
        "relevance queries; for global todo queries, read every matching todo body. A no-match result is valid.\\n"
''',
)
edit(
    "src/memleaf/hermes_provider/__init__.py",
    '''            "Read more only if needed for ordinary relevance queries. When the user asks for current "
''',
    '''            "Read more only if needed; for ordinary relevance queries, do not read all entries to filter unrelated items. "
            "When the user asks for current "
''',
)

# Update compatibility tests for intentional public-contract changes.
keys_old = '{"memory_id", "title", "scopes", "body", "offset", "next_offset", "has_more", "total_chars", "version"}'
keys_new = '{"memory_id", "title", "scopes", "body", "offset", "next_offset", "has_more", "total_chars", "version", "type", "status", "due_date"}'
edit("tests/test_context_budget.py", keys_old, keys_new, minimum=2)

edit("tests/test_pypi_install.py", "version: 0.2.19", "version: 0.2.20")
edit("tests/test_stage_c3_packaging.py", '"0.2.19"', '"0.2.20")
edit("tests/test_stage_c1_mcp.py", '"version": "0.2.19"', '"version": "0.2.20"')
edit(
    "tests/test_stage_c1_mcp.py",
    '''                "scope_catalog",
                "search",
                "read",
''',
    '''                "scope_catalog",
                "search",
                "list_todos",
                "read",
''',
)
edit(
    "tests/test_stage_c1_mcp.py",
    'self.assertEqual(len(self.assert_modern_result(self, listed)["tools"]), 11)',
    'self.assertEqual(len(self.assert_modern_result(self, listed)["tools"]), 12)',
)
edit(
    "tests/test_stage_c1_mcp.py",
    '''            "search": {"query", "retrieval_id"},
            "read": {"memory_id", "retrieval_id"},
''',
    '''            "search": {"query", "retrieval_id"},
            "list_todos": {"retrieval_id"},
            "read": {"memory_id", "retrieval_id"},
''',
)
edit("tests/test_stage_c2_init.py", "Tools discovered: 11", "Tools discovered: 12")

print("v0.2.20 follow-up applied")
