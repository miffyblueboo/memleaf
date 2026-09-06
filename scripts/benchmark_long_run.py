#!/usr/bin/env python3
"""Reproducible long-run benchmark for Markdown-source memleaf Vaults.

The dataset is synthetic but uses real memleaf Markdown/frontmatter and the
real Core retrieval/write/lifecycle paths.  It deliberately does not call an
LLM, because this benchmark measures Vault/index/storage behavior rather than
provider latency or semantic model quality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from memleaf.compaction import Compactor
from memleaf.config import save_config
from memleaf.memory_writer import MemoryWriter
from memleaf.models import Memory, utc_now
from memleaf.retention import RetentionManager
from memleaf.service import Memleaf
from memleaf.vault import Vault


SEARCH_P95_TARGET_MS = 2_000.0
REBUILD_TARGET_MS = 120_000.0
RSS_TARGET_MIB = 1_024.0
PROJECT_COUNT = 24


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3),
    }


def _measure(function: Callable[[], Any], samples: int) -> tuple[dict[str, Any], Any]:
    elapsed: list[float] = []
    last: Any = None
    for _ in range(samples):
        started = time.perf_counter()
        last = function()
        elapsed.append((time.perf_counter() - started) * 1000.0)
    return _summary(elapsed), last


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _peak_rss_mib() -> float | None:
    try:
        import resource
        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # macOS reports bytes; Linux/BSD hosted runners report KiB.
        if sys.platform == "darwin":
            return round(value / (1024.0 * 1024.0), 3)
        return round(value / 1024.0, 3)
    except Exception:
        return None


def _provenance(index: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sources = [
        {
            "event_key": hashlib.sha256(f"event-{index}-{revision}".encode()).hexdigest(),
            "source": "codex" if revision % 2 == 0 else "hermes",
            "session_id": f"session-{index % 97}",
            "turn_id": f"turn-{index}-{revision}",
        }
        for revision in range(8)
    ]
    digest = hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return sources, {
        "source_count": 64 + (index % 17),
        "source_digest": digest,
        "sources_omitted": True,
    }


def _active_memory(index: int) -> Memory:
    memory_id = f"bench-{index:06d}"
    project = f"project:p{index % PROJECT_COUNT:02d}"
    scopes = ["global"] if index % 5 == 0 else [project]
    language = "中文长期记忆" if index % 2 == 0 else "English long-run memory"
    unique = f"needle-{index:06d}"
    repeated = "shared-benchmark-token"
    wikilink = f"[[topic-{index % 128:03d}]]"
    body = f"{language}. {unique} {repeated} {wikilink}."
    if index % 41 == 0:
        body += " " + ("long body segment 长文本片段 " * 120)
    sources, extra = _provenance(index)
    is_todo = index % 20 == 0
    return Memory(
        memory_id=memory_id,
        title=f"Benchmark memory {index:06d}",
        body=body,
        tags=[f"bench-tag-{index % 64:02d}", f"group-{index % 11:02d}"],
        type="todo" if is_todo else "other",
        scopes=scopes,
        aliases=[f"alias-{index % 128:03d}"],
        keywords=[f"keyword-{index % 256:03d}", repeated],
        sources=sources,
        created="2026-01-01T00:00:00Z",
        updated=f"2026-06-{(index % 28) + 1:02d}T12:00:00Z",
        status="active" if is_todo else None,
        due_date=f"2027-{(index % 12) + 1:02d}-{(index % 27) + 1:02d}" if is_todo else None,
        extra=extra,
    )


def _history_memory(index: int, active_count: int) -> Memory:
    active_index = index % active_count
    source = _active_memory(active_index)
    return Memory(
        memory_id=f"bench-hist-{index:06d}",
        title=source.title + " historical",
        body=source.body + f" historical-version-{index}",
        tags=list(source.tags),
        type=source.type,
        scopes=list(source.scopes),
        aliases=list(source.aliases),
        keywords=list(source.keywords),
        sources=list(source.sources),
        created=source.created,
        updated="2026-05-01T00:00:00Z",
        status=source.status,
        due_date=source.due_date,
        extra={
            **source.extra,
            "active_memory_id": source.memory_id,
            "archived_at": f"2026-05-{(index % 28) + 1:02d}T00:00:00Z",
        },
    )


def _prepare_dataset(root: Path, count: int) -> None:
    vault = Vault(root)
    config = vault.config()
    config["scopes"] = {
        f"project:p{index:02d}": {"aliases": [f"project-{index:02d}"]}
        for index in range(PROJECT_COUNT)
    }
    save_config(vault.config_path, config)
    for index in range(count):
        memory = _active_memory(index)
        vault.memory_path(memory.memory_id, "knowledge").write_text(
            memory.to_markdown(), encoding="utf-8"
        )
    history_count = max(1, count // 10)
    for index in range(history_count):
        memory = _history_memory(index, count)
        vault.memory_path(memory.memory_id, "history").write_text(
            memory.to_markdown(), encoding="utf-8"
        )


def _request(
    *,
    request_id: str,
    source: str,
    session_id: str,
    turn_id: str,
    summary: dict[str, Any],
    duplicate_memory_id: str | None = None,
) -> dict[str, Any]:
    event_key = hashlib.sha256(f"{source}/{session_id}/{turn_id}".encode()).hexdigest()
    event = SimpleNamespace(event_key=event_key, turn_id=turn_id)
    turn = SimpleNamespace(
        source=source,
        session_id=session_id,
        turn_key=hashlib.sha256(turn_id.encode()).hexdigest(),
        events=[event],
    )
    value: dict[str, Any] = {
        "memory_id": request_id,
        "summary": summary,
        "turn": turn,
        "conversation_title": "benchmark",
        "event_key": event_key,
        "turn_id": turn_id,
    }
    if duplicate_memory_id is not None:
        value["duplicate_memory_id"] = duplicate_memory_id
    return value


def _writer_operation(service: Memleaf, request: dict[str, Any], now: str) -> Any:
    writer = MemoryWriter(service)
    with service._mutation_boundary():
        result = writer.write_many_unlocked([request], now=now)
        service._rebuild_index_unlocked()
    return result


def _benchmark_one(base_dir: Path, count: int) -> dict[str, Any]:
    root = base_dir / f"vault-{count}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    generation_started = time.perf_counter()
    _prepare_dataset(root, count)
    generation_ms = (time.perf_counter() - generation_started) * 1000.0

    init_stats, service = _measure(lambda: Memleaf(root), 3)
    assert isinstance(service, Memleaf)

    rebuild_stats, rebuild_result = _measure(service.rebuild_index, 3 if count < 50_000 else 2)
    query_index = min(count - 1, 123)
    target_id = f"bench-{query_index:06d}"
    target_token = f"needle-{query_index:06d}"
    indexed_query = f"bench-tag-{query_index % 64:02d}"
    project = f"project:p{query_index % PROJECT_COUNT:02d}"

    cold_stats, cold_result = _measure(
        lambda: Memleaf(root).search_candidates(indexed_query, limit=20), 1
    )
    search_stats, search_result = _measure(
        lambda: service.search_candidates(indexed_query, limit=20), 3
    )
    exact_stats, exact_result = _measure(
        lambda: service.search_candidates(target_id, limit=5), 3
    )
    fulltext_stats, fulltext_result = _measure(
        lambda: service.search_candidates(target_token, limit=5), 3
    )
    scope_stats, scope_result = _measure(
        lambda: service.search_candidates(indexed_query, scope=project, limit=20), 3
    )
    todo_stats, todo_result = _measure(
        lambda: service.list_todos(status="active", limit=20), 3
    )
    read_stats, read_result = _measure(lambda: service.read(target_id), 3)

    mutation_samples = 3
    create_times: list[float] = []
    update_times: list[float] = []
    no_change_times: list[float] = []
    history_times: list[float] = []
    now = utc_now()
    for sample in range(mutation_samples):
        create_summary = {
            "title": f"Benchmark create {sample}",
            "body": f"created benchmark body {sample}",
            "tags": ["benchmark-create"],
            "type": "other",
            "scopes": ["global"],
            "aliases": [],
            "keywords": ["benchmark"],
            "scope_source": None,
        }
        request = _request(
            request_id=f"bench-create-{count}-{sample}",
            source="codex",
            session_id=f"bench-create-{count}",
            turn_id=f"create-{sample}",
            summary=create_summary,
        )
        started = time.perf_counter()
        _writer_operation(service, request, now)
        create_times.append((time.perf_counter() - started) * 1000.0)

        update_target = f"bench-{sample:06d}"
        update_summary = {
            "title": f"Benchmark memory {sample:06d} updated",
            "body": f"updated benchmark body {sample} {now}",
            "tags": ["benchmark-update"],
            "type": "other" if sample % 20 else "todo",
            "scopes": ["global"] if sample % 5 == 0 else [f"project:p{sample % PROJECT_COUNT:02d}"],
            "aliases": [],
            "keywords": ["benchmark"],
            "scope_source": None,
            "update_memory_id": update_target,
        }
        if sample % 20 == 0:
            update_summary["status"] = "active"
            update_summary["due_date"] = "2027-01-01"
        request = _request(
            request_id=f"bench-update-request-{count}-{sample}",
            source="hermes",
            session_id=f"bench-update-{count}",
            turn_id=f"update-{sample}",
            summary=update_summary,
        )
        started = time.perf_counter()
        _writer_operation(service, request, now)
        update_times.append((time.perf_counter() - started) * 1000.0)

        current = service.read(update_target)
        assert current is not None
        no_change_summary = {
            "title": current.title,
            "body": current.body,
            "tags": list(current.tags),
            "type": current.type,
            "scopes": list(current.scopes),
            "aliases": list(current.aliases),
            "keywords": list(current.keywords),
            "scope_source": current.scope_source,
            **({"status": current.status, "due_date": current.due_date} if current.type == "todo" else {}),
        }
        request = _request(
            request_id=f"bench-nochange-request-{count}-{sample}",
            source="codex",
            session_id=f"bench-nochange-{count}",
            turn_id=f"nochange-{sample}",
            summary=no_change_summary,
            duplicate_memory_id=update_target,
        )
        started = time.perf_counter()
        _writer_operation(service, request, now)
        no_change_times.append((time.perf_counter() - started) * 1000.0)

        history_source = service.read(f"bench-{sample + 10:06d}")
        assert history_source is not None
        writer = MemoryWriter(service)
        started = time.perf_counter()
        with service._mutation_boundary():
            writer._write_history(
                history_source,
                superseded_by=history_source.memory_id,
                archived_at=now,
                invalidated_reason="benchmark",
            )
        history_times.append((time.perf_counter() - started) * 1000.0)

    retention = RetentionManager(service)
    retirement_times: list[float] = []
    pruning_times: list[float] = []
    retention_now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    for sample in range(3):
        todo = Memory.new(
            memory_id=f"bench-closed-{count}-{sample}",
            title=f"Closed todo {sample}",
            body="closed todo benchmark",
            type="todo",
            scopes=["global"],
            status="completed",
            completed_at="2026-01-01T00:00:00Z",
            sources=[],
        )
        service.vault.memory_path(todo.memory_id, "knowledge").write_text(todo.to_markdown(), encoding="utf-8")
        started = time.perf_counter()
        with service._mutation_boundary():
            retention._retire_closed_todos_unlocked(retention_now, 30)
            service._rebuild_index_unlocked()
        retirement_times.append((time.perf_counter() - started) * 1000.0)

        group = f"bench-prune-group-{count}-{sample}"
        for version in range(33):
            memory = Memory.new(
                memory_id=f"bench-prune-{count}-{sample}-{version:02d}",
                title="Prune history",
                body=f"history {version}",
                scopes=["global"],
                sources=[],
                updated=(retention_now - timedelta(days=version)).isoformat().replace("+00:00", "Z"),
                active_memory_id=group,
                archived_at=(retention_now - timedelta(days=version)).isoformat().replace("+00:00", "Z"),
            )
            service.vault.memory_path(memory.memory_id, "history").write_text(memory.to_markdown(), encoding="utf-8")
        started = time.perf_counter()
        with service._mutation_boundary():
            retention._prune_history_unlocked(retention_now, "bounded", 3650, 32)
            service._rebuild_index_unlocked()
        pruning_times.append((time.perf_counter() - started) * 1000.0)

    compaction_stats, compaction_result = _measure(
        lambda: Compactor(service)._snapshot(1, 0.30), 3
    )

    real_lock = service.vault.lock
    lock_holds: list[float] = []

    @contextmanager
    def tracking_lock():
        with real_lock():
            started = time.perf_counter()
            try:
                yield
            finally:
                lock_holds.append((time.perf_counter() - started) * 1000.0)

    service.vault.lock = tracking_lock  # type: ignore[method-assign]
    try:
        service.search_candidates(indexed_query, limit=20)
    finally:
        service.vault.lock = real_lock  # type: ignore[method-assign]

    peak_rss = _peak_rss_mib()
    disk = {
        "vault_bytes": _dir_size(root),
        "knowledge_bytes": _dir_size(service.vault.knowledge_path),
        "history_bytes": _dir_size(service.vault.history_path),
        "index_bytes": _dir_size(service.vault.index_path),
        "state_bytes": _dir_size(service.vault.state_path),
    }

    metrics = {
        "vault_initialization": init_stats,
        "rebuild_index": rebuild_stats,
        "search_candidate_process_cold": cold_stats,
        "search_candidate_warm": search_stats,
        "exact_memory_id_lookup": exact_stats,
        "fulltext_search": fulltext_stats,
        "scope_filtered_search": scope_stats,
        "list_todos": todo_stats,
        "read": read_stats,
        "create": _summary(create_times),
        "update": _summary(update_times),
        "no_change": _summary(no_change_times),
        "history_write": _summary(history_times),
        "closed_todo_retirement": _summary(retirement_times),
        "history_pruning": _summary(pruning_times),
        "compaction_snapshot": compaction_stats,
        "vault_lock_hold_search": _summary(lock_holds),
    }
    return {
        "memory_count": count,
        "history_seed_count": max(1, count // 10),
        "generation_ms": round(generation_ms, 3),
        "metrics": metrics,
        "peak_rss_mib": peak_rss,
        "disk": disk,
        "sanity": {
            "rebuild": rebuild_result,
            "search_status": search_result.get("status") if isinstance(search_result, dict) else None,
            "cold_search_status": cold_result.get("status") if isinstance(cold_result, dict) else None,
            "exact_status": exact_result.get("status") if isinstance(exact_result, dict) else None,
            "fulltext_status": fulltext_result.get("status") if isinstance(fulltext_result, dict) else None,
            "scope_status": scope_result.get("status") if isinstance(scope_result, dict) else None,
            "todos_status": todo_result.get("status") if isinstance(todo_result, dict) else None,
            "read_found": read_result is not None,
            "compaction_selected": len(compaction_result[0]) if isinstance(compaction_result, tuple) else None,
        },
    }


def _decision(results: list[dict[str, Any]]) -> dict[str, Any]:
    largest = max(results, key=lambda item: item["memory_count"])
    search_names = ("search_candidate_warm", "fulltext_search", "scope_filtered_search")
    failed = {
        name: largest["metrics"][name]["p95_ms"]
        for name in search_names
        if largest["metrics"][name]["p95_ms"] > SEARCH_P95_TARGET_MS
    }
    rebuild_ms = largest["metrics"]["rebuild_index"]["p95_ms"]
    rss = largest.get("peak_rss_mib")
    recommend = bool(failed)
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


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Performance and long-run scale",
        "",
        "This benchmark measures memleaf's local Markdown Vault and derived indexes. It does not call an LLM, so model/provider latency is intentionally excluded.",
        "",
        "## Methodology",
        "",
        "Datasets contain global plus 24 project scopes, tags, aliases, keywords, wikilinks, active todos, historical versions, bounded high-frequency provenance, Chinese and English text, and mixed short/long bodies. CREATE/UPDATE/NO_CHANGE use `MemoryWriter` plus the real derived-index rebuild boundary; lifecycle measurements use the real retention and compaction primitives.",
        "",
        "The hosted benchmark reports a process-cold first search and warm repeated searches. It does **not** claim a true OS cold-cache measurement because hosted CI cannot safely or reproducibly drop the kernel page cache.",
        "",
        f"Platform: `{payload['platform']}`  ",
        f"Python: `{payload['python_version']}`  ",
        f"Run UTC: `{payload['run_utc']}`",
        "",
        "## Latency results",
        "",
        "All latency values are milliseconds.",
        "",
        "| Active memories | Metric | median | p95 | max | samples |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    order = [
        "vault_initialization", "rebuild_index", "search_candidate_process_cold",
        "search_candidate_warm", "exact_memory_id_lookup", "fulltext_search",
        "scope_filtered_search", "list_todos", "read", "create", "update",
        "no_change", "history_write", "closed_todo_retirement", "history_pruning",
        "compaction_snapshot", "vault_lock_hold_search",
    ]
    for result in payload["results"]:
        for name in order:
            metric = result["metrics"][name]
            lines.append(
                f"| {result['memory_count']:,} | `{name}` | {metric['median_ms']:.3f} | {metric['p95_ms']:.3f} | {metric['max_ms']:.3f} | {metric['samples']} |"
            )
    lines.extend([
        "",
        "## Resource footprint",
        "",
        "| Active memories | Peak RSS MiB | Vault MiB | `_index/` MiB | `_state/` MiB |",
        "|---:|---:|---:|---:|---:|",
    ])
    for result in payload["results"]:
        rss = result.get("peak_rss_mib")
        rss_text = f"{rss:.3f}" if isinstance(rss, (int, float)) else "n/a"
        lines.append(
            f"| {result['memory_count']:,} | {rss_text} | {result['disk']['vault_bytes']/1048576:.3f} | {result['disk']['index_bytes']/1048576:.3f} | {result['disk']['state_bytes']/1048576:.3f} |"
        )
    decision = payload["decision"]
    lines.extend([
        "",
        "## Performance target and SQLite FTS decision",
        "",
        "The decision gate is explicit: at 50,000 active memories, candidate/tag search, full-text search, and scope-filtered search should each remain at or below 2,000 ms p95. Rebuild has a 120 s p95 guardrail and peak RSS a 1,024 MiB guardrail, but those two alone do not justify FTS.",
        "",
        f"**SQLite FTS recommended: {'yes' if decision['sqlite_fts_recommended'] else 'no'}.** {decision['reason']}",
        "",
        "If SQLite is introduced after this decision, it is a deletable `_index/` acceleration layer only: Markdown remains the sole source of truth, runtime/recovery state stays in `_state/`, and `rebuild-index` must recreate the database completely.",
        "",
        "## Runtime state growth audit",
        "",
        "The retrieval ledger is already TTL- and count-bounded, and per-session pending tool evidence/injection bookkeeping is bounded. The processed-turn/session ledger retains replay/idempotency evidence and is therefore intentionally not destructively pruned in v0.2.28: deleting it without a new durable replay checkpoint would risk duplicate replay. Future state compaction must first define a verified checkpoint that proves older replay evidence is no longer needed.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", nargs="+", type=int, default=[1_000, 10_000, 50_000])
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=Path("benchmarks/results/v0.2.28-linux.json"))
    parser.add_argument("--markdown-out", type=Path, default=Path("docs/performance.md"))
    args = parser.parse_args()
    if any(value < 1 for value in args.counts):
        parser.error("counts must be positive")

    managed_temp: tempfile.TemporaryDirectory[str] | None = None
    if args.work_dir is None:
        managed_temp = tempfile.TemporaryDirectory(prefix="memleaf-long-run-")
        work_dir = Path(managed_temp.name)
    else:
        work_dir = args.work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)

    try:
        results = []
        for count in args.counts:
            print(f"benchmark: preparing {count:,} active memories", flush=True)
            result = _benchmark_one(work_dir, count)
            results.append(result)
            print(
                f"benchmark: {count:,} search p95={result['metrics']['search_candidate_warm']['p95_ms']:.3f}ms "
                f"fulltext p95={result['metrics']['fulltext_search']['p95_ms']:.3f}ms",
                flush=True,
            )
        payload = {
            "schema_version": 1,
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "counts": list(args.counts),
            "results": results,
        }
        payload["decision"] = _decision(results)
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(_render_markdown(payload), encoding="utf-8")
        print(json.dumps(payload["decision"], ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    finally:
        if managed_temp is not None:
            managed_temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
