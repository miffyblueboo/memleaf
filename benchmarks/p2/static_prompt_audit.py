from __future__ import annotations

import json

from memleaf.admission import analyze_turn_evidence, evidence_prompt
from memleaf.prompts import GATE_SYSTEM, SUMMARIZE_SYSTEM, _json, gate_prompt


def sample_events() -> list[dict]:
    return [
        {
            "event_key": "p2/e1",
            "role": "user",
            "turn_id": "t1",
            "timestamp": "2026-09-10T06:00:00Z",
            "content": "P2_UNIQUE_USER " + ("alpha " * 220),
            "tool_evidence": [{"content": "P2_TOOL_BODY_MUST_NOT_APPEAR"}],
        },
        {
            "event_key": "p2/e2",
            "role": "assistant",
            "turn_id": "t1",
            "timestamp": "2026-09-10T06:00:05Z",
            "content": "P2_UNIQUE_ASSISTANT " + ("beta " * 220),
            "tool_evidence": [],
        },
    ]


def legacy_gate_prompt(events: list[dict]) -> str:
    """Reproduce the corrected-B0 Gate user envelope before P2 input dedup."""

    parts = [
        "Mode: automatic capture/process.",
        "Complete turn events (the only conversation content visible to this call):\n" + _json(events),
        "Relevant existing memleaf/native memories:\n" + _json([]),
        "Session scope background:\n" + _json([]),
        "Current scope registry (safe projection; no paths):\n" + _json([]),
        "Evidence units and coverage enums follow. Return the strict Gate JSON object.",
    ]
    return "\n\n".join(parts)


def build_report() -> dict:
    events = sample_events()
    units = analyze_turn_evidence(events)
    evidence = evidence_prompt(units, batch_index=0, batch_count=1, todo_witnesses={})
    legacy = legacy_gate_prompt(events) + evidence
    current = gate_prompt(events) + evidence
    legacy_bytes = len(legacy.encode("utf-8"))
    current_bytes = len(current.encode("utf-8"))
    removed = legacy_bytes - current_bytes
    return {
        "schema_version": 1,
        "measurement": "synthetic_static_utf8_bytes_zero_model_calls",
        "legacy_gate_user_prompt_bytes": legacy_bytes,
        "p2_gate_user_prompt_bytes": current_bytes,
        "bytes_removed": removed,
        "reduction_ratio": round(removed / legacy_bytes, 6),
        "gate_system_bytes_phase2": len(GATE_SYSTEM.encode("utf-8")),
        "summary_system_bytes_phase2": len(SUMMARIZE_SYSTEM.encode("utf-8")),
        "legacy_user_marker_occurrences": legacy.count("P2_UNIQUE_USER"),
        "p2_user_marker_occurrences": current.count("P2_UNIQUE_USER"),
        "legacy_assistant_marker_occurrences": legacy.count("P2_UNIQUE_ASSISTANT"),
        "p2_assistant_marker_occurrences": current.count("P2_UNIQUE_ASSISTANT"),
        "tool_body_occurrences_p2": current.count("P2_TOOL_BODY_MUST_NOT_APPEAR"),
    }


def main() -> int:
    print(json.dumps(build_report(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
