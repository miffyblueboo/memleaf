#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


BENCHMARK = Path("scripts/benchmark_long_run.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"unexpected {label} occurrence count: {text.count(old)}")
    return text.replace(old, new, 1)


def main() -> int:
    text = BENCHMARK.read_text(encoding="utf-8")

    new_decision = '''def _decision(results: list[dict[str, Any]]) -> dict[str, Any]:
    largest = max(results, key=lambda item: item["memory_count"])
    search_names = ("search_candidate_warm", "fulltext_search", "scope_filtered_search")
    stress_reference_misses = {
        name: largest["metrics"][name]["p95_ms"]
        for name in search_names
        if largest["metrics"][name]["p95_ms"] > SEARCH_P95_TARGET_MS
    }
    rebuild_ms = largest["metrics"]["rebuild_index"]["p95_ms"]
    rss = largest.get("peak_rss_mib")
    return {
        "evaluated_memory_count": largest["memory_count"],
        "workload_classification": "stress_boundary",
        "normal_steady_state_assumed": False,
        "stress_reference_search_p95_ms": SEARCH_P95_TARGET_MS,
        "stress_reference_misses": stress_reference_misses,
        "rebuild_p95_reference_ms": REBUILD_TARGET_MS,
        "peak_rss_reference_mib": RSS_TARGET_MIB,
        "rebuild_reference_missed": rebuild_ms > REBUILD_TARGET_MS,
        "rss_reference_missed": isinstance(rss, (int, float)) and rss > RSS_TARGET_MIB,
        "architecture_change_required": False,
        "reason": (
            "50k active memories is retained as an extreme stress boundary, not a normal steady-state assumption. "
            "The normal retrieval path is Scope Map -> scope-constrained search -> read, while UPDATE/NO_CHANGE, "
            "todo retirement, bounded history, and compaction are expected to control active-memory growth. "
            "Stress misses are recorded for capacity visibility and do not justify adding another storage/search backend."
        ),
    }


'''
    pattern = r"def _decision\(results: list\[dict\[str, Any\]\]\) -> dict\[str, Any\]:\n.*?\n\ndef _render_markdown"
    text, replaced = re.subn(pattern, new_decision + "def _render_markdown", text, count=1, flags=re.S)
    if replaced != 1:
        raise SystemExit("failed to replace benchmark decision")

    text = replace_once(text, '"schema_version": 1,', '"schema_version": 2,', "schema version")

    old = '"The hosted benchmark reports a process-cold first search and warm repeated searches. It does **not** claim a true OS cold-cache measurement because hosted CI cannot safely or reproducibly drop the kernel page cache.",'
    new = old + '\n        "",\n        "The 50,000-active-memory dataset is deliberately an extreme stress boundary. It is not an assumption that a healthy Vault should normally retain 50,000 useful active memories. The dataset has 20% global memories and spreads the rest across 24 project scopes, so even one project scope contains roughly 1.6k-1.7k active records at 50k total.",\n        "",\n        "Normal product retrieval remains `Scope Map -> scope-constrained search -> read`; full-vault full-text search is a fallback/special case rather than the primary injection path.",'
    text = replace_once(text, old, new, "methodology paragraph")

    text = replace_once(
        text,
        '"## Performance target and SQLite FTS decision",',
        '"## Scale interpretation",',
        "performance section title",
    )
    text = replace_once(
        text,
        '"The decision gate is explicit: at 50,000 active memories, candidate/tag search, full-text search, and scope-filtered search should each remain at or below 2,000 ms p95. Rebuild has a 120 s p95 guardrail and peak RSS a 1,024 MiB guardrail, but those two alone do not justify FTS.",',
        '"The 2,000 ms search, 120 s rebuild, and 1,024 MiB RSS values are retained as stress references only. A miss at 50,000 active memories is capacity evidence, not a release blocker and not a reason to introduce a new database/search backend.",',
        "stress reference paragraph",
    )
    text = replace_once(
        text,
        'f"**SQLite FTS recommended: {\'yes\' if decision[\'sqlite_fts_recommended\'] else \'no\'}.** {decision[\'reason\']}",',
        'f"**Architecture change required from the 50k stress result: {\'yes\' if decision[\'architecture_change_required\'] else \'no\'}.** {decision[\'reason\']}",',
        "decision rendering",
    )
    text = replace_once(
        text,
        '"If SQLite is introduced after this decision, it is a deletable `_index/` acceleration layer only: Markdown remains the sole source of truth, runtime/recovery state stays in `_state/`, and `rebuild-index` must recreate the database completely.",',
        '"If a real Vault grows into the tens of thousands of active memories, first audit CREATE-vs-UPDATE/NO_CHANGE behavior, todo retirement, history retention, duplicate control, and compaction. The product remains Markdown-only in v0.2.28.",\n        "",\n        "## Active-memory lifecycle health",\n        "",\n        "Active memory count is a health signal, not an archival counter. Repeated facts should update existing canonical memories, unchanged observations should be NO_CHANGE, completed/cancelled todos retire from active memory, historical versions are bounded, and compaction reduces redundant active material. A real Vault approaching the 50k stress dataset should therefore trigger lifecycle/quality investigation before search-backend expansion.",',
        "lifecycle interpretation",
    )

    lowered = text.lower()
    if "sqlite_fts_recommended" in text or "sqlite fts" in lowered or "fts5" in lowered:
        raise SystemExit("SQLite/FTS decision language remains in benchmark script")

    BENCHMARK.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
