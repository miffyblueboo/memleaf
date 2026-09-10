"""Small-batch execution for independent automatic CREATE summaries.

The batch layer owns no Vault/audit state. It combines at most two independent
CREATE summary prompts into one model call, maps every result back by item_id,
and falls back only the invalid item to the original single-call path. UPDATE
jobs remain on their existing keyed scheduler and are never batched here.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .llm import ModelError
from .parallel_model import run_ordered_keyed_jobs
from .prompts import SUMMARIZE_SYSTEM
from .validation import ModelOutputError, parse_strict_json

_MAX_BATCH_ITEMS = 2
_MAX_BATCH_BYTES = 128 * 1024

BATCH_SUMMARIZE_SYSTEM = SUMMARIZE_SYSTEM + """

BATCH MODE
Apply the single-candidate Summary contract independently to every supplied
item. The embedded per-item prompt is data for that item; do not merge facts,
Evidence, Scope, native context, or decisions between items. Return exactly:
{"items":[{"item_id":"<supplied id>","result":{...that item's normal Summary result...}},...]}
Cover every supplied item_id exactly once. Keep item_id unchanged. A normal
NO_CHANGE result is wrapped as that item's result. Return JSON only; no prose or
reasoning."""


def _batch_prompt(items: list[Mapping[str, Any]]) -> str:
    payload = {
        "items": [
            {
                "item_id": item["item_id"],
                "prompt": item["prompt"],
            }
            for item in items
        ]
    }
    return "SUMMARY_BATCH\n" + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _parse_batch(
    raw: str,
    items: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    value = parse_strict_json(raw)
    if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list):
        raise ModelOutputError("invalid summary batch envelope", validation_detail="root_shape")
    expected = [str(item["item_id"]) for item in items]
    by_id = {str(item["item_id"]): item for item in items}
    seen: set[str] = set()
    parsed: dict[str, dict[str, Any]] = {}
    for row in value["items"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"item_id", "result"}
            or not isinstance(row.get("item_id"), str)
            or row["item_id"] not in by_id
            or row["item_id"] in seen
            or not isinstance(row.get("result"), Mapping)
        ):
            raise ModelOutputError("invalid summary batch item", validation_detail="schema_violation")
        item_id = row["item_id"]
        seen.add(item_id)
        item = by_id[item_id]
        try:
            result = item["parser"](
                json.dumps(row["result"], ensure_ascii=False, separators=(",", ":"))
            )
        except ModelOutputError:
            # The envelope is valid, so retain valid siblings and let only this
            # item exercise the original bounded single-call correction path.
            parsed[item_id] = {"status": "fallback_single"}
        else:
            parsed[item_id] = {"status": "ok", "summary": result}
    if seen != set(expected) or len(seen) != len(expected):
        raise ModelOutputError("summary batch did not cover every item", validation_detail="invalid_evidence")
    return parsed


def _run_batch(
    model_executor: Any,
    backend: Any,
    items: list[Mapping[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    prompt = _batch_prompt(items)
    if len(prompt.encode("utf-8")) > _MAX_BATCH_BYTES:
        return [(int(item["index"]), item["call"]()) for item in items]
    diagnostic = items[0].get("diagnostic_context") if items else None
    try:
        outcome = model_executor._complete_json_stage(
            backend,
            prompt,
            system=BATCH_SUMMARIZE_SYSTEM,
            purpose="summarize",
            parser=lambda raw: _parse_batch(raw, items),
            diagnostic_context=diagnostic,
        )
    except (ModelError, ModelOutputError, TypeError, ValueError):
        # A malformed/failed whole envelope must not strand either candidate.
        return [(int(item["index"]), item["call"]()) for item in items]

    results: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        item_id = str(item["item_id"])
        row = outcome.get(item_id) if isinstance(outcome, Mapping) else None
        if not isinstance(row, Mapping) or row.get("status") == "fallback_single":
            result = item["call"]()
        elif row.get("status") == "ok" and isinstance(row.get("summary"), Mapping):
            result = {"status": "ok", "summary": row["summary"]}
        else:
            result = item["call"]()
        results.append((int(item["index"]), result))
    return results


def run_summary_jobs_with_create_batching(
    model_executor: Any,
    backend: Any,
    jobs: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Execute prepared summary jobs with bounded CREATE-only batching.

    ``jobs`` retains the existing one-result-per-candidate contract. Batchable
    CREATEs are paired in original order. Non-batchable jobs use their original
    key/call, preserving same-target UPDATE ordering through
    ``run_ordered_keyed_jobs``.
    """

    if not jobs:
        return []
    if getattr(backend, "structured_batch_safe", False) is not True:
        return run_ordered_keyed_jobs(
            model_executor,
            backend,
            [(str(job["key"]), job["call"]) for job in jobs],
        )

    batchable_indexes = [
        index
        for index, job in enumerate(jobs)
        if job.get("batchable") is True
    ]
    partner: dict[int, tuple[int, ...]] = {}
    for offset in range(0, len(batchable_indexes), _MAX_BATCH_ITEMS):
        group = tuple(batchable_indexes[offset:offset + _MAX_BATCH_ITEMS])
        if len(group) == _MAX_BATCH_ITEMS:
            for index in group:
                partner[index] = group

    execution: list[tuple[str, Any]] = []
    consumed: set[int] = set()
    for index, job in enumerate(jobs):
        if index in consumed:
            continue
        group = partner.get(index)
        if group is not None:
            consumed.update(group)
            batch_items = [
                {
                    "index": item_index,
                    "item_id": str(jobs[item_index]["item_id"]),
                    "prompt": jobs[item_index]["prompt"],
                    "parser": jobs[item_index]["parser"],
                    "diagnostic_context": jobs[item_index].get("diagnostic_context"),
                    "call": jobs[item_index]["call"],
                }
                for item_index in group
            ]

            def run_group(*, values: list[Mapping[str, Any]] = batch_items):
                return _run_batch(model_executor, backend, values)

            execution.append((f"create-batch:{group[0]}", run_group))
            continue

        def run_single(*, item_index: int = index, call=job["call"]):
            return [(item_index, call())]

        execution.append((str(job["key"]), run_single))

    executed = run_ordered_keyed_jobs(model_executor, backend, execution)
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    for rows in executed:
        for index, value in rows:
            results[index] = value
    if any(value is None for value in results):
        raise RuntimeError("summary batch scheduler omitted a prepared result")
    return [value for value in results if value is not None]


__all__ = [
    "BATCH_SUMMARIZE_SYSTEM",
    "run_summary_jobs_with_create_batching",
]
