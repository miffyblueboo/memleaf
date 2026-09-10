from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping

from memleaf import Memleaf
from memleaf.models import Memory

SAFE_RESULT_FIELDS = (
    "processed_turns", "memories_written", "metadata_merged", "cleaned_turns",
    "deferred_candidates", "deferred_inbox_turns", "unresolved_evidence_count",
    "retryable_deferred_turns", "coverage_status", "external_evidence_status",
)
_SAFE_ERROR_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def revision(memory: Memory) -> str:
    value = memory.to_dict()
    value.pop("hit_count", None)
    value.pop("last_hit_at", None)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def memory_projection(memory: Memory) -> dict[str, Any]:
    return {
        "memory_id": memory.memory_id,
        "title": memory.title,
        "body": memory.body,
        "type": memory.type,
        "scopes": list(memory.scopes),
        "tags": list(memory.tags),
        "aliases": list(memory.aliases),
        "keywords": list(memory.keywords),
        "scope_source": memory.scope_source,
        "status": memory.status,
        "completed_at": memory.completed_at,
        "due_date": memory.due_date,
        "sources": [
            {key: item[key] for key in ("event_key", "session_id", "turn_id", "conversation_title") if key in item and isinstance(item[key], str)}
            for item in memory.sources if isinstance(item, Mapping)
        ],
        "revision": revision(memory),
    }


def snapshot(service: Memleaf) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"knowledge": [], "history": []}
    for area in ("knowledge", "history"):
        for path in service.vault.list_markdown(area):
            memory = Memory.from_markdown(path.read_text(encoding="utf-8"), path)
            result[area].append(memory_projection(memory))
        result[area].sort(key=lambda item: item["memory_id"].casefold())
    return result


def safe_process_result(result: Mapping[str, Any]) -> dict[str, Any]:
    projected = {key: result[key] for key in SAFE_RESULT_FIELDS if key in result and isinstance(result[key], (str, int, bool))}
    metrics = result.get("model_metrics")
    if isinstance(metrics, Mapping):
        projected["model_metrics"] = deepcopy(dict(metrics))
    ids = result.get("memory_ids")
    if isinstance(ids, list):
        projected["memory_ids"] = [item for item in ids if isinstance(item, str)]
    return projected


def safe_failure(error: BaseException) -> dict[str, Any]:
    """Project only structural failure labels; never persist exception text."""

    result: dict[str, Any] = {"error_type": type(error).__name__}
    for output_key, attribute in (
        ("error_code", "code"),
        ("stage", "stage"),
        ("validation_reason", "validation_reason"),
        ("validation_detail", "validation_detail"),
        ("evidence_check", "evidence_check"),
    ):
        item = getattr(error, attribute, None)
        if isinstance(item, str) and _SAFE_ERROR_LABEL.fullmatch(item):
            result[output_key] = item
    metrics = getattr(error, "model_metrics", None)
    if isinstance(metrics, Mapping):
        result["model_metrics"] = deepcopy(dict(metrics))
    return result


def call_graph(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    metrics = result.get("model_metrics")
    calls = metrics.get("calls") if isinstance(metrics, Mapping) else None
    if not isinstance(calls, list):
        return []
    fields = (
        "call_index", "stage", "operation", "retry", "failed", "invalid_output",
        "request_duration_ms", "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens",
        "thinking_mode", "thinking_effective", "thinking_control",
    )
    return [{key: raw[key] for key in fields if key in raw} for raw in calls if isinstance(raw, Mapping)]
