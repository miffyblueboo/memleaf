from __future__ import annotations

import json

from memleaf.prompts import _json, summarize_prompt


def sample_related_memories() -> list[dict]:
    return [
        {
            "memory_id": "m-unrelated-a",
            "title": "unrelated A",
            "body": "P2_UNRELATED_A " + ("alpha " * 260),
            "type": "fact",
            "scopes": ["global"],
        },
        {
            "memory_id": "m-target",
            "title": "fixed target",
            "body": "P2_FIXED_TARGET " + ("target " * 260),
            "type": "fact",
            "scopes": ["global"],
        },
        {
            "memory_id": "m-unrelated-b",
            "title": "unrelated B",
            "body": "P2_UNRELATED_B " + ("beta " * 260),
            "type": "fact",
            "scopes": ["global"],
        },
        {
            "native": True,
            "native_id": "native-1",
            "title": "native comparison",
            "body": "P2_NATIVE_CONTEXT " + ("native " * 120),
        },
    ]


def sample_candidate(*, update: bool) -> dict:
    value = {
        "candidate_id": "c-summary",
        "memory": "P2_SUMMARY_EVIDENCE changed durable state",
        "type": "fact",
        "scopes": ["global"],
        "scope_source": "model",
    }
    if update:
        value["update_memory_id"] = "M-TARGET"
    return value


def sample_evidence() -> list[dict]:
    return [
        {
            "event_key": "p2/summary/user",
            "role": "user",
            "content": "P2_SUMMARY_EVIDENCE changed durable state",
            "evidence_origin": "user_assertion",
            "unit_id": "summary-unit",
        }
    ]


def legacy_summary_prompt(
    candidate: dict,
    events: list[dict],
    *,
    explicit: bool = False,
    related_memories: list[dict] | None = None,
    scope_background: object = None,
    scope_registry: list[dict] | None = None,
) -> str:
    """Reproduce corrected-B0/P2-phase1 Summary user-envelope behavior."""

    mode = "explicit remember; worth is already granted" if explicit else "candidate passed the Gate"
    operation = "UPDATE" if candidate.get("update_memory_id") else "CREATE"
    parts = [
        "Mode: " + mode,
        "Gate operation: " + operation,
        "Candidate:\n" + _json(candidate),
        "Evidence (the only conversation content visible to this call):\n" + _json(events),
        "Relevant existing memleaf/native memories:\n" + _json(related_memories or []),
        "Session scope background:\n" + _json(scope_background if scope_background is not None else []),
        "Current scope registry (safe projection; no paths):\n" + _json(scope_registry if scope_registry is not None else []),
    ]
    if operation == "CREATE":
        parts.append("CREATE is fixed: omit update_memory_id and do not select a target.")
    else:
        parts.append(
            "UPDATE target is fixed. Preserve still-valid target content; if admitted Evidence makes no semantic change, "
            'return exactly {"decision":"NO_CHANGE"}.'
        )
    parts.append(
        "Return one summary JSON object"
        + ("." if explicit else ' or exactly {"decision":"NO_CHANGE"}.')
    )
    return "\n\n".join(parts)


def _metrics(*, update: bool) -> dict:
    candidate = sample_candidate(update=update)
    evidence = sample_evidence()
    related = sample_related_memories()
    scope_background = {"marker": "P2_SCOPE_BACKGROUND"}
    scope_registry = [{"scope": "project:synthetic", "marker": "P2_SCOPE_REGISTRY"}]
    legacy = legacy_summary_prompt(
        candidate,
        evidence,
        related_memories=related,
        scope_background=scope_background,
        scope_registry=scope_registry,
    )
    current = summarize_prompt(
        candidate,
        evidence,
        related_memories=related,
        scope_background=scope_background,
        scope_registry=scope_registry,
    )
    legacy_bytes = len(legacy.encode("utf-8"))
    current_bytes = len(current.encode("utf-8"))
    removed = legacy_bytes - current_bytes
    return {
        "legacy_bytes": legacy_bytes,
        "p2_bytes": current_bytes,
        "bytes_removed": removed,
        "reduction_ratio": round(removed / legacy_bytes, 6),
        "unrelated_a_occurrences": current.count("P2_UNRELATED_A"),
        "unrelated_b_occurrences": current.count("P2_UNRELATED_B"),
        "fixed_target_occurrences": current.count("P2_FIXED_TARGET"),
        "native_occurrences": current.count("P2_NATIVE_CONTEXT"),
        "scope_background_occurrences": current.count("P2_SCOPE_BACKGROUND"),
        "scope_registry_occurrences": current.count("P2_SCOPE_REGISTRY"),
    }


def build_report() -> dict:
    return {
        "schema_version": 1,
        "measurement": "synthetic_static_utf8_bytes_zero_model_calls",
        "automatic_create": _metrics(update=False),
        "automatic_update": _metrics(update=True),
    }


def main() -> int:
    print(json.dumps(build_report(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
