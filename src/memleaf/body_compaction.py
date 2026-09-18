"""Body-only contract for explicit compaction; no new execution or recovery path."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

from .models import Memory
from .validation import ModelOutputError, parse_strict_json, validate_compact_output

BODY_COMPACT_SYSTEM = """Shorten the supplied memory bodies while preserving their meaning, including conditions, negation and uncertainty. Context fields are read-only.
Return JSON only: {"memories":[{"source_memory_ids":["supplied-id"],"body":"shorter body"}]}.
Each item refers to exactly one supplied memory. Keep independent memories separate; omit bodies that cannot be shortened safely. Return {"memories":[]} when nothing needs changing. Core preserves identity and all other fields.
"""


def body_compact_prompt(memories: list[dict[str, Any]]) -> str:
    import json
    return "Supplied memories (context is read-only):\n" + json.dumps(
        memories, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_body_compact_output(raw: str, current_memory_ids: Iterable[str]) -> dict[str, Any]:
    """Accept the minimal wire format and unchanged legacy full-field echoes.

    Legacy field echoes never confer write authority. Their equality to the
    frozen source is checked by ``replacement_memory`` before any commit.
    """
    value = parse_strict_json(raw)
    if not isinstance(value, Mapping) or set(value) != {"memories"} or not isinstance(value["memories"], list):
        raise ModelOutputError("compact output must contain only a memories array")
    allowed = set(current_memory_ids)
    seen: set[str] = set()
    result = []
    for row in value["memories"]:
        if not isinstance(row, Mapping):
            raise ModelOutputError("compact replacement must be an object")
        if set(row) != {"body", "source_memory_ids"}:
            # Retain the existing strict legacy parser; do not introduce a
            # second interpretation of optional fields or accept unknown keys.
            try:
                row = validate_compact_output({"memories": [row]}, allowed)["memories"][0]
            except (TypeError, KeyError) as error:
                raise ModelOutputError("invalid legacy compact fields") from error
        ids = row.get("source_memory_ids")
        body = row.get("body")
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or ids[0] not in allowed:
            raise ModelOutputError("body compaction requires one supplied memory ID")
        if not isinstance(body, str) or not body.strip():
            raise ModelOutputError("compact body must be nonempty text")
        key = ids[0].casefold()
        if key in seen:
            raise ModelOutputError("compact source memory is repeated")
        seen.add(key)
        result.append(deepcopy(dict(row)))
    return {"memories": result}


def replacement_memory(source: Memory, row: Mapping[str, Any], *, now: str) -> Memory:
    """Copy a complete current record, changing only body and maintenance stamps."""
    if source.validity != "valid" or row.get("source_memory_ids") != [source.memory_id]:
        raise ModelOutputError("invalid body compaction source")
    body = row.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ModelOutputError("compact body must be nonempty text")
    original = deepcopy(source.to_dict())
    context = dict(original)
    for field in ("aliases", "keywords", "scope_source", "status", "completed_at", "due_date"):
        context[field] = getattr(source, field)
    for field, value in row.items():
        if field not in {"body", "source_memory_ids"} and (field not in context or context[field] != value):
            # Explicitly reject drift instead of silently ignoring a request
            # whose new body might rely on the changed business fields.
            raise ModelOutputError("body compaction cannot change protected fields")
    original["body"] = body
    original["updated"] = now
    original["compacted_at"] = now
    original.setdefault("compaction_source_ids", [source.memory_id])
    return Memory.from_mapping(original)
