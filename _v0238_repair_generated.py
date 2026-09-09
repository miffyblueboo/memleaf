from __future__ import annotations

from pathlib import Path


def replace_between(path: str, start_marker: str, end_marker: str, replacement: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    file.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


replace_between(
    "src/memleaf/memory_planner.py",
    "                    correction_raw = self.model._complete(",
    "                    correction_raw, correction_bindings = split_semantic_envelope(correction_raw)",
    '''                    correction_raw = self.model._complete(
                        backend,
                        coverage_repair_prompt(
                            evidence_prompt(
                                missing,
                                batch_index=batch_index,
                                batch_count=batch_count,
                                todo_witnesses=todo_witnesses,
                            ),
                            related_memories=gate_related,
                            scope_background=scope_background,
                            scope_registry=scope_registry,
                            already_handled_candidate_ids=[
                                item["candidate_id"] for item in batch_gate["candidates"]
                            ],
                        ),
                        system=GATE_COVERAGE_SYSTEM,
                        purpose="gate",
                        metric_stage="gate",
                        metric_operation="gate_coverage_repair",
                    )
''',
)

replace_between(
    "src/memleaf/prompts.py",
    "def coverage_repair_prompt(",
    "# Same policy for the INNER summary",
    '''def coverage_repair_prompt(
    evidence_text: str,
    *,
    related_memories: list[dict] | None = None,
    scope_background: object = None,
    scope_registry: list[dict] | None = None,
    already_handled_candidate_ids: list[str] | None = None,
) -> str:
    """Build the narrow second-pass prompt for unresolved coverage only."""

    return (
        "Mode: coverage repair only. Classify only the unresolved units below.\\n"
        + evidence_text
        + "\\nNecessary related-memory comparison context:\\n"
        + _json(related_memories or [])
        + "\\nSession scope background:\\n"
        + _json(scope_background if scope_background is not None else [])
        + "\\nCurrent scope registry (safe projection; no paths):\\n"
        + _json(scope_registry if scope_registry is not None else [])
        + "\\nAlready handled candidate IDs (do not re-emit):\\n"
        + _json(already_handled_candidate_ids or [])
        + "\\n"
        + COVERAGE_CORRECTION
        + "\\n"
        + COVERAGE_ALREADY_COMPLETED_CORRECTION
        + "\\nReturn the strict coverage-repair Gate JSON object."
    )


''',
)

Path(__file__).unlink(missing_ok=True)
