"""Bounded batch transport for independent automatic semantic reviews.

The semantic contract and per-item summary parser remain the existing single-review
ones. Batching changes only transport granularity: up to four independent reviews
share one model call. A malformed associated row is retried as one legacy single
review; an un-associable batch envelope falls back to legacy singles for the whole
chunk. No Vault or audit mutation occurs here.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .llm import ModelError
from .update_review import (
    CREATE_SEMANTIC_REVIEW_SYSTEM,
    UPDATE_SEMANTIC_REVIEW_SYSTEM,
    _MAX_PROMPT_BYTES,
    _complete_json_stage_compat,
    _json_safe,
    _source_projection,
    _target_projection,
    parse_update_review_output,
    review_create,
    review_update,
)
from .validation import ModelOutputError, parse_strict_json

MAX_REVIEW_BATCH_ITEMS = 4

_CREATE_BATCH_SYSTEM = CREATE_SEMANTIC_REVIEW_SYSTEM + """

BATCH MODE: the single-review semantic rules above apply independently to each
input row. This mode changes only the outer transport shape. Return exactly
{"reviews":[...]} with one row per supplied review_id. Each row must copy its
review_id exactly and then contain exactly the fields of one legal single-review
response. Do not transfer facts, decisions, Scope, targets, or evidence between
rows. Row order is irrelevant; review_id is the association key. Return JSON only.
"""

_UPDATE_BATCH_SYSTEM = UPDATE_SEMANTIC_REVIEW_SYSTEM + """

BATCH MODE: the single-review semantic rules above apply independently to each
input row. This mode changes only the outer transport shape. Return exactly
{"reviews":[...]} with one row per supplied review_id. Each row must copy its
review_id exactly and then contain exactly the fields of one legal single-review
response. Do not transfer facts, decisions, Scope, targets, or evidence between
rows. Row order is irrelevant; review_id is the association key. Return JSON only.
"""


def _validated_specs(items: Iterable[Mapping[str, Any]], *, update: bool) -> list[dict[str, Any]]:
    specs = [dict(item) for item in items]
    if not specs or len(specs) > MAX_REVIEW_BATCH_ITEMS:
        raise ValueError("review batch must contain between one and four items")
    seen: set[str] = set()
    for spec in specs:
        review_id = spec.get("review_id")
        if not isinstance(review_id, str) or not review_id or review_id in seen:
            raise ValueError("review batch requires unique non-empty review_id values")
        seen.add(review_id)
        if not callable(spec.get("parse_summary")):
            raise ValueError("review batch item requires parse_summary")
        if not isinstance(spec.get("proposed_summary"), Mapping):
            raise ValueError("review batch item requires proposed_summary")
        source = spec.get("admitted_source")
        if isinstance(source, (str, bytes)) or source is None:
            raise ValueError("review batch item requires admitted_source")
        if update and spec.get("target") is None:
            raise ValueError("update review batch item requires target")
    return specs


def _batch_payload(specs: list[dict[str, Any]], *, update: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in specs:
        row = {
            "review_id": spec["review_id"],
            "admitted_source": _source_projection(spec["admitted_source"]),
            "proposed_summary": _json_safe(dict(spec["proposed_summary"])),
        }
        if update:
            row["active_target"] = _target_projection(spec["target"])
        rows.append(row)
    return rows


def _build_batch_prompt(specs: list[dict[str, Any]], *, update: bool) -> str:
    mode = "UPDATE" if update else "CREATE"
    payload = {"reviews": _batch_payload(specs, update=update)}
    prompt = (
        f"{mode}_SEMANTIC_REVIEW_BATCH\n"
        + json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n\nReview each row independently and return the strict batch object."
    )
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ModelOutputError(
            "semantic review batch input exceeds prompt budget",
            validation_detail="other_schema_violation",
        )
    return prompt


def _parse_batch_output(raw: Any, specs: list[dict[str, Any]]) -> dict[str, Any]:
    value = parse_strict_json(raw) if isinstance(raw, str) else raw
    if not isinstance(value, Mapping) or set(value) != {"reviews"} or not isinstance(value["reviews"], list):
        raise ModelOutputError("invalid semantic review batch envelope", validation_detail="root_shape")
    by_id = {spec["review_id"]: spec for spec in specs}
    seen: set[str] = set()
    outcomes: dict[str, dict[str, Any]] = {}
    retry_ids: list[str] = []
    for row in value["reviews"]:
        if not isinstance(row, Mapping):
            raise ModelOutputError("unassociated semantic review batch row", validation_detail="root_shape")
        review_id = row.get("review_id")
        if not isinstance(review_id, str) or review_id not in by_id or review_id in seen:
            raise ModelOutputError("invalid semantic review batch membership", validation_detail="invalid_evidence")
        seen.add(review_id)
        single = {key: item for key, item in row.items() if key != "review_id"}
        try:
            outcome = parse_update_review_output(
                single,
                parse_summary=by_id[review_id]["parse_summary"],
            )
        except ModelOutputError:
            retry_ids.append(review_id)
        else:
            outcomes[review_id] = outcome
    for spec in specs:
        if spec["review_id"] not in seen:
            retry_ids.append(spec["review_id"])
    return {"outcomes": outcomes, "retry_ids": retry_ids}


def _single(model_executor: Any, backend: Any, spec: Mapping[str, Any], *, update: bool) -> dict[str, Any]:
    kwargs = {
        "admitted_source": spec["admitted_source"],
        "proposed_summary": spec["proposed_summary"],
        "parse_summary": spec["parse_summary"],
        "diagnostic_context": spec.get("diagnostic_context"),
    }
    if update:
        return review_update(model_executor, backend, target=spec["target"], **kwargs)
    return review_create(model_executor, backend, **kwargs)


def _review_batch(
    model_executor: Any,
    backend: Any,
    items: Iterable[Mapping[str, Any]],
    *,
    update: bool,
) -> list[dict[str, Any]]:
    specs = _validated_specs(items, update=update)
    if len(specs) == 1:
        return [_single(model_executor, backend, specs[0], update=update)]
    try:
        prompt = _build_batch_prompt(specs, update=update)
        parsed = _complete_json_stage_compat(
            model_executor,
            backend,
            prompt,
            system=_UPDATE_BATCH_SYSTEM if update else _CREATE_BATCH_SYSTEM,
            purpose="summarize",
            parser=lambda raw: _parse_batch_output(raw, specs),
            diagnostic_context=specs[0].get("diagnostic_context"),
            metric_stage="semantic_review",
        )
        if not isinstance(parsed, Mapping):
            raise ModelOutputError("invalid semantic review batch result", validation_detail="root_shape")
        outcomes = parsed.get("outcomes")
        retry_ids = parsed.get("retry_ids")
        if not isinstance(outcomes, Mapping) or not isinstance(retry_ids, list):
            raise ModelOutputError("invalid semantic review batch result", validation_detail="root_shape")
    except (ModelError, ModelOutputError, TypeError, ValueError):
        return [_single(model_executor, backend, spec, update=update) for spec in specs]

    resolved = {key: dict(value) for key, value in outcomes.items() if isinstance(key, str) and isinstance(value, Mapping)}
    retry_set = {value for value in retry_ids if isinstance(value, str)}
    for spec in specs:
        review_id = spec["review_id"]
        if review_id in retry_set or review_id not in resolved:
            resolved[review_id] = _single(model_executor, backend, spec, update=update)
    return [resolved[spec["review_id"]] for spec in specs]


def review_create_batch(model_executor: Any, backend: Any, items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Review up to four independent CREATE requests with per-row fallback."""

    return _review_batch(model_executor, backend, items, update=False)


def review_update_batch(model_executor: Any, backend: Any, items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Review up to four independent UPDATE requests with per-row fallback."""

    return _review_batch(model_executor, backend, items, update=True)


__all__ = [
    "MAX_REVIEW_BATCH_ITEMS",
    "review_create_batch",
    "review_update_batch",
]
