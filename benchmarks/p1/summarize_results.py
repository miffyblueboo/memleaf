#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
)


def _nonnegative_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _percentile(values: Iterable[float | int], fraction: float) -> float | int | None:
    ordered = sorted(value for value in values if _nonnegative_number(value) is not None)
    if not ordered:
        return None
    # Nearest-rank percentile. This is deterministic for the small exploratory
    # sample and avoids implying smooth-distribution precision that is not present.
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _calls(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = result.get("call_graph")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _wall_ms(result: Mapping[str, Any]) -> int | float | None:
    return _nonnegative_number(result.get("process_wall_ms"))


def _call_count_stats(results: list[Mapping[str, Any]]) -> dict[str, Any]:
    counts = [len(_calls(result)) for result in results]
    return {
        "total": sum(counts),
        "per_process_p50": _percentile(counts, 0.50),
        "per_process_p95": _percentile(counts, 0.95),
        "per_process_max": max(counts) if counts else None,
    }


def _token_availability(calls: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    total_calls = len(calls)
    for field in _TOKEN_FIELDS:
        values = [_nonnegative_number(call.get(field)) for call in calls]
        reported = [value for value in values if value is not None]
        complete = total_calls > 0 and len(reported) == total_calls
        output[field] = {
            "reported_calls": len(reported),
            "total_calls": total_calls,
            "complete": complete,
            # Missing provider fields are never silently treated as zero.
            "total": sum(reported) if complete else None,
        }
    return output


def _request_duration_summary(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    values = [
        value for value in (_nonnegative_number(call.get("request_duration_ms")) for call in calls)
        if value is not None
    ]
    return {
        "reported_calls": len(values),
        "total_calls": len(calls),
        "complete": bool(calls) and len(values) == len(calls),
        "sum_ms": sum(values) if values else None,
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
    }


def _scenario(case_id: str, results: list[Mapping[str, Any]]) -> dict[str, Any]:
    walls = [value for value in (_wall_ms(result) for result in results) if value is not None]
    calls = [call for result in results for call in _calls(result)]
    rubrics: list[list[Any]] = []
    for result in results:
        rubric = result.get("expected_rubric")
        if isinstance(rubric, list) and rubric not in rubrics:
            rubrics.append(rubric)
    visibility = [
        result.get("fresh_instance_visibility_consistent")
        for result in results
        if isinstance(result.get("fresh_instance_visibility_consistent"), bool)
    ]
    return {
        "case_id": case_id,
        "category": next(
            (result.get("category") for result in results if isinstance(result.get("category"), str)),
            None,
        ),
        "process_count": len(results),
        "success_count": sum(result.get("run_status") == "success" for result in results),
        "error_count": sum(result.get("run_status") != "success" for result in results),
        "process_wall_ms": {
            "reported_processes": len(walls),
            "p50": _percentile(walls, 0.50),
            "p95": _percentile(walls, 0.95),
        },
        "model_calls": _call_count_stats(results),
        "fresh_instance_visibility_consistent": {
            "reported_processes": len(visibility),
            "true_count": sum(value is True for value in visibility),
            "all_true": bool(visibility) and all(visibility),
        },
        "expected_rubrics": rubrics,
        # Rubrics are evidence for a human/independent semantic grader; this
        # structural summarizer must not convert them into an automatic ACCEPT.
        "semantic_grade_status": "PENDING_INDEPENDENT_GRADING",
        "call_stages": dict(sorted(Counter(
            call.get("stage") for call in calls if isinstance(call.get("stage"), str)
        ).items())),
    }


def summarize(document: Mapping[str, Any]) -> dict[str, Any]:
    raw_results = document.get("results")
    results = [item for item in raw_results if isinstance(item, Mapping)] if isinstance(raw_results, list) else []
    calls = [call for result in results for call in _calls(result)]
    walls = [value for value in (_wall_ms(result) for result in results) if value is not None]
    by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for result in results:
        case_id = result.get("case_id")
        if isinstance(case_id, str) and case_id:
            by_case[case_id].append(result)

    stage_counts = Counter(
        call.get("stage") for call in calls if isinstance(call.get("stage"), str)
    )
    operation_counts = Counter(
        call.get("operation") for call in calls if isinstance(call.get("operation"), str)
    )

    process_results = [
        result.get("process_result")
        for result in results
        if isinstance(result.get("process_result"), Mapping)
    ]
    deferred_candidates = [
        value
        for item in process_results
        if (value := _nonnegative_number(item.get("deferred_candidates"))) is not None
    ]
    unresolved_evidence = [
        value
        for item in process_results
        if (value := _nonnegative_number(item.get("unresolved_evidence_count"))) is not None
    ]

    return {
        "schema_version": 1,
        "source_schema_version": document.get("schema_version"),
        "arm": document.get("arm"),
        "provider": document.get("provider"),
        "protocol": document.get("protocol"),
        "model": document.get("model"),
        "thinking": document.get("thinking"),
        "completed_plan": document.get("completed_plan"),
        "stopped_reason": document.get("stopped_reason"),
        "processes": {
            "count": len(results),
            "success_count": sum(result.get("run_status") == "success" for result in results),
            "error_count": sum(result.get("run_status") != "success" for result in results),
            "wall_ms_reported": len(walls),
            "wall_ms_p50": _percentile(walls, 0.50),
            "wall_ms_p95": _percentile(walls, 0.95),
        },
        "model_calls": {
            **_call_count_stats(results),
            "stage_counts": dict(sorted(stage_counts.items())),
            "operation_counts": dict(sorted(operation_counts.items())),
            "retry_calls": sum(call.get("retry") is True for call in calls),
            "failed_calls": sum(call.get("failed") is True for call in calls),
            "invalid_output_calls": sum(call.get("invalid_output") is True for call in calls),
            "request_duration": _request_duration_summary(calls),
            "tokens": _token_availability(calls),
        },
        "queue_and_resolution": {
            "deferred_candidates": {
                "reported_processes": len(deferred_candidates),
                "total": sum(deferred_candidates) if deferred_candidates else None,
            },
            "unresolved_evidence_count": {
                "reported_processes": len(unresolved_evidence),
                "total": sum(unresolved_evidence) if unresolved_evidence else None,
            },
        },
        "scenarios": [_scenario(case_id, by_case[case_id]) for case_id in sorted(by_case)],
        "quality_status": "PENDING_INDEPENDENT_GRADING",
        "quality_note": (
            "This report summarizes structural execution evidence only. "
            "It does not auto-accept semantic quality from expected rubrics or model self-reports."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize a memleaf P1 result JSON without model calls")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    document = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise SystemExit("input must contain a JSON object")
    report = summarize(document)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
