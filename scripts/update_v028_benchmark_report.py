from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "scripts/benchmark_long_run.py"
text = path.read_text(encoding="utf-8")

old = '''    return {
        "memory_count": count,
        "history_seed_count": max(1, count // 10),
'''
new = '''    return {
        "memory_count": count,
        "search_backend": (
            "sqlite_fts5"
            if hasattr(service.vault, "search_index_path")
            and service.vault.search_index_path.is_file()
            else "markdown_json_index"
        ),
        "history_seed_count": max(1, count // 10),
'''
if text.count(old) != 1:
    raise SystemExit("benchmark result insertion point changed")
text = text.replace(old, new, 1)

old = '''    recommend = bool(failed)
    return {
        "evaluated_memory_count": largest["memory_count"],
        "search_p95_target_ms": SEARCH_P95_TARGET_MS,
        "rebuild_p95_target_ms": REBUILD_TARGET_MS,
        "peak_rss_target_mib": RSS_TARGET_MIB,
        "search_targets_missed": failed,
        "rebuild_target_missed": rebuild_ms > REBUILD_TARGET_MS,
        "rss_target_missed": isinstance(rss, (int, float)) and rss > RSS_TARGET_MIB,
        "sqlite_fts_recommended": recommend,
        "reason": (
            "At least one 50k-scale search path exceeds the 2s p95 interactive target. "
            "A rebuildable stdlib SQLite FTS index should be evaluated; Markdown remains authoritative."
            if recommend
            else "All measured 50k-scale search paths meet the 2s p95 target; SQLite FTS is not justified."
        ),
    }
'''
new = '''    recommend = bool(failed)
    backend = largest.get("search_backend", "markdown_json_index")
    sqlite_active = backend == "sqlite_fts5"
    if recommend and sqlite_active:
        reason = (
            "The active rebuildable SQLite FTS derived index still misses at least one "
            "50k-scale 2s p95 search target; the current acceleration design is not sufficient."
        )
    elif recommend:
        reason = (
            "At least one 50k-scale search path exceeds the 2s p95 interactive target. "
            "A rebuildable stdlib SQLite FTS index should be evaluated; Markdown remains authoritative."
        )
    elif sqlite_active:
        reason = (
            "With the rebuildable SQLite FTS derived index active, all measured 50k-scale "
            "search paths meet the 2s p95 target; Markdown remains authoritative."
        )
    else:
        reason = (
            "All measured 50k-scale search paths meet the 2s p95 target without SQLite FTS; "
            "additional FTS acceleration is not justified."
        )
    return {
        "evaluated_memory_count": largest["memory_count"],
        "search_backend": backend,
        "sqlite_fts_active": sqlite_active,
        "search_p95_target_ms": SEARCH_P95_TARGET_MS,
        "rebuild_p95_target_ms": REBUILD_TARGET_MS,
        "peak_rss_target_mib": RSS_TARGET_MIB,
        "search_targets_missed": failed,
        "rebuild_target_missed": rebuild_ms > REBUILD_TARGET_MS,
        "rss_target_missed": isinstance(rss, (int, float)) and rss > RSS_TARGET_MIB,
        "sqlite_fts_recommended": recommend,
        "reason": reason,
    }
'''
if text.count(old) != 1:
    raise SystemExit("benchmark decision block changed")
text = text.replace(old, new, 1)

old = '''        "The decision gate is explicit: at 50,000 active memories, candidate/tag search, full-text search, and scope-filtered search should each remain at or below 2,000 ms p95. Rebuild has a 120 s p95 guardrail and peak RSS a 1,024 MiB guardrail, but those two alone do not justify FTS.",
        "",
        f"**SQLite FTS recommended: {'yes' if decision['sqlite_fts_recommended'] else 'no'}.** {decision['reason']}",
'''
new = '''        "The decision gate is explicit: at 50,000 active memories, candidate/tag search, full-text search, and scope-filtered search should each remain at or below 2,000 ms p95. Rebuild has a 120 s p95 guardrail and peak RSS a 1,024 MiB guardrail, but those two alone do not justify FTS.",
        "",
        f"Measured search backend: `{decision.get('search_backend', 'unknown')}`.",
        "",
        f"**Additional SQLite FTS evaluation required: {'yes' if decision['sqlite_fts_recommended'] else 'no'}.** {decision['reason']}",
'''
if text.count(old) != 1:
    raise SystemExit("benchmark markdown decision block changed")
text = text.replace(old, new, 1)

old = '''        payload["decision"] = _decision(results)
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
'''
new = '''        payload["decision"] = _decision(results)
        payload["search_backend"] = payload["decision"].get("search_backend")
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
'''
if text.count(old) != 1:
    raise SystemExit("benchmark payload insertion point changed")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("updated benchmark backend reporting")
