"""Pure standard-library evidence capture budget primitives.

The Core package and the copied Hermes provider use this module as the one
shared retention boundary.  Budgets are measured in UTF-8 bytes for bodies;
the loss marker is metadata and is deliberately outside both budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


DEFAULT_MAX_RECORDS = 64
DEFAULT_MAX_RECORD_BYTES = 32 * 1024
DEFAULT_MAX_TOTAL_BYTES = 128 * 1024

_OVERFLOW_TOOL = "evidence.inventory"
_OVERFLOW_CALL_IDS = frozenset({"overflow", "retention-overflow"})
_LOSS_MARKER_CONTENT = "Additional tool observations exceeded the capture budget."
_MARKER_FIELDS = frozenset({
    "call_id", "execution_status", "schema_version", "retention",
})



@dataclass(frozen=True)
class EvidenceBudget:
    """The bounded evidence shape shared by Core and the Hermes copy."""

    max_records: int = DEFAULT_MAX_RECORDS
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        for field in ("max_records", "max_record_bytes", "max_total_bytes"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"invalid evidence budget {field}")


DEFAULT_BUDGET = EvidenceBudget()


def utf8_size(value: str) -> int:
    """Return the exact UTF-8 byte size of one body string."""

    if not isinstance(value, str):
        raise TypeError("evidence body must be text")
    return len(value.encode("utf-8"))


def truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    """Safely truncate text to ``limit`` UTF-8 bytes at a codepoint boundary."""

    if not isinstance(value, str):
        raise TypeError("evidence body must be text")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("invalid UTF-8 byte limit")
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    # ``ignore`` removes at most the incomplete final codepoint created by the
    # byte slice.  It never removes a complete codepoint before the boundary.
    return encoded[:limit].decode("utf-8", "ignore"), True


def is_overflow_marker(value: Mapping[str, Any]) -> bool:
    """Recognize the canonical and legacy loss markers without body matching."""

    if not isinstance(value, Mapping):
        return False
    if value.get("tool_name") == _OVERFLOW_TOOL:
        call_id = value.get("call_id")
        if call_id in _OVERFLOW_CALL_IDS:
            return True
    return value.get("record_id") == "overflow" and (
        value.get("omitted_count") is not None or value.get("omitted_bytes") is not None
    )


def marker_count(value: Mapping[str, Any]) -> int:
    """Return the audited number of observations represented by a marker."""

    raw = value.get("omitted_count", "0")
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return int(raw)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def marker_bytes(value: Mapping[str, Any]) -> int:
    """Return the audited number of omitted body bytes represented by a marker."""

    raw = value.get("omitted_bytes", "0")
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return int(raw)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def logical_observation_count(value: Mapping[str, Any]) -> int:
    """Count one ordinary record or the observations named by a loss marker."""

    return marker_count(value) if is_overflow_marker(value) else 1


def _body_bytes(value: Mapping[str, Any]) -> int:
    body = value.get("content")
    return utf8_size(body) if isinstance(body, str) else 0


def _marker_from(
    markers: Sequence[Mapping[str, Any]],
    *,
    omitted_count: int,
    omitted_bytes: int,
) -> dict[str, str]:
    """Merge loss markers while preserving the first marker's audit identity."""

    marker: dict[str, str] = {}
    if markers:
        first = markers[0]
        for field in _MARKER_FIELDS:
            value = first.get(field)
            if isinstance(value, str) and value:
                marker[field] = value[:320]
        marker["call_id"] = str(first.get("call_id") or "overflow")[:320]
    else:
        marker["call_id"] = "overflow"
    total_count = sum(marker_count(item) for item in markers) + omitted_count
    total_bytes = sum(marker_bytes(item) for item in markers) + omitted_bytes
    # A marker is always an untrusted inventory diagnostic, even if a caller
    # supplied an ``overflow`` row with authority-looking fields.  Keep only a
    # small fixed identity and force the non-authoritative state.
    marker["tool_name"] = _OVERFLOW_TOOL
    marker["record_id"] = "overflow"
    marker["kind"] = "unknown"
    marker["result_status"] = "truncated"
    marker["completeness"] = "partial"
    marker["omitted_count"] = str(total_count)
    if total_bytes or any("omitted_bytes" in item for item in markers):
        marker["omitted_bytes"] = str(total_bytes)
    # Metadata mode deliberately removes the body.  Do not recreate one on a
    # later normalization pass; bounded markers use one fixed diagnostic body.
    if markers and any("content" in item for item in markers) or omitted_count or omitted_bytes:
        marker["content"] = _LOSS_MARKER_CONTENT
    return marker


def apply_evidence_budget(
    value: Sequence[Mapping[str, Any]],
    *,
    budget: EvidenceBudget = DEFAULT_BUDGET,
) -> list[dict[str, str]]:
    """Apply one idempotent record/body/aggregate evidence budget.

    The input is expected to contain already validated records.  This helper
    only owns budget accounting: ordinary metadata stays untouched, body text
    is bounded in UTF-8 bytes, and loss markers are excluded from both record
    and body budgets.  Passing its output through again is stable.
    """

    if not isinstance(value, (list, tuple)):
        raise ValueError("tool evidence must be a list")
    if not isinstance(budget, EvidenceBudget):
        raise TypeError("budget must be an EvidenceBudget")

    markers: list[Mapping[str, Any]] = []
    records: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        if is_overflow_marker(raw):
            markers.append(raw)
        else:
            records.append(dict(raw))

    omitted_count = 0
    omitted_bytes = 0
    if len(records) > budget.max_records:
        for row in records[budget.max_records :]:
            omitted_count += logical_observation_count(row)
            omitted_bytes += _body_bytes(row)
        records = records[: budget.max_records]

    retained: list[dict[str, str]] = []
    remaining = budget.max_total_bytes
    for row in records:
        body = row.get("content")
        if not isinstance(body, str):
            retained.append(row)
            continue
        # Core historically strips evidence fields before persistence.  Keep
        # that canonical boundary in the shared helper so a provider pass and
        # a Core pass cannot disagree at a trailing-space truncation edge.
        body = body.strip()
        if not body:
            row.pop("content", None)
            retained.append(row)
            continue
        original_bytes = utf8_size(body)
        body, per_record_loss = truncate_utf8(body, budget.max_record_bytes)
        if per_record_loss:
            canonical_body = body.strip()
            body = canonical_body
            row["content"] = body
            row["result_status"] = "truncated"
            row["completeness"] = "partial"
            omitted_bytes += original_bytes - utf8_size(body)
            if not body:
                omitted_count += logical_observation_count(row)
                continue

        body_bytes = utf8_size(body)
        if body_bytes <= remaining:
            row["content"] = body
            retained.append(row)
            remaining -= body_bytes
            continue

        if remaining > 0:
            body, aggregate_loss = truncate_utf8(body, remaining)
            canonical_body = body.strip()
            body = canonical_body
            if not body:
                omitted_count += logical_observation_count(row)
                omitted_bytes += body_bytes
                continue
            row["content"] = body
            if aggregate_loss:
                row["result_status"] = "truncated"
                row["completeness"] = "partial"
            omitted_bytes += body_bytes - utf8_size(body)
            retained.append(row)
            remaining = 0
        else:
            omitted_count += logical_observation_count(row)
            omitted_bytes += body_bytes

    if markers or omitted_count or omitted_bytes:
        # Keep distinct host call markers distinct so a later repeated hook
        # can be deduplicated by ``call_id``.  Markers with the same identity
        # are merged.  Marker identities have their own fixed allowance: keep
        # at most one row per ordinary record slot and summarize every excess
        # marker into one global row.  This prevents an unbounded stream of
        # distinct host call IDs from bypassing the capture budget.
        groups: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
        for marker in markers:
            key = (marker.get("call_id"), marker.get("record_id"))
            groups.setdefault(key, []).append(marker)
        global_groups: list[Mapping[str, Any]] = []
        call_groups: list[list[Mapping[str, Any]]] = []
        for key, group in groups.items():
            if key[0] in _OVERFLOW_CALL_IDS:
                global_groups.extend(group)
            else:
                call_groups.append(group)
        retained_groups = call_groups[:budget.max_records]
        dropped_groups = call_groups[budget.max_records:]
        dropped_count = sum(marker_count(item) for group in dropped_groups for item in group)
        dropped_bytes = sum(marker_bytes(item) for group in dropped_groups for item in group)
        marker_rows = [
            _marker_from(group, omitted_count=0, omitted_bytes=0)
            for group in retained_groups
        ]
        if global_groups or dropped_groups or omitted_count or omitted_bytes:
            marker_rows.append(
                _marker_from(
                    global_groups,
                    omitted_count=dropped_count + omitted_count,
                    omitted_bytes=dropped_bytes + omitted_bytes,
                )
            )
        retained.extend(marker_rows)
    return retained


__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_MAX_RECORD_BYTES",
    "DEFAULT_MAX_RECORDS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "EvidenceBudget",
    "apply_evidence_budget",
    "is_overflow_marker",
    "logical_observation_count",
    "marker_bytes",
    "marker_count",
    "truncate_utf8",
    "utf8_size",
]
