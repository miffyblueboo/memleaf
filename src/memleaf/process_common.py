"""Pure shared values and compatibility helpers for processing components.

No file writes or service construction occur at import time.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from .inbox import InboxTurn
from .llm import MODEL_ERROR_CODES, MODEL_VALIDATION_REASONS, ModelUnavailable
from .models import utc_now
from .retrieval import candidate_matches_query
from .validation import (
    MODEL_EVIDENCE_CHECKS, MODEL_VALIDATION_DETAILS, ModelOutputError, parse_strict_json, safe_evidence_context, safe_schema_context,
)

_PROCESSING_LEASE_SECONDS = 3600


_LEGACY_PROCESSING_GRACE_SECONDS = 600


_PROCESSING_STATUS = "processing"


_IDLE_STATUS = "idle"


_FAILED_STATUS = "failed"


_DIAGNOSTIC_MAX_BYTES = 256 * 1024


_DIAGNOSTIC_FILENAME = "model-diagnostics.jsonl"


_TARGET_NOT_RELATED = "NOT_RELATED"


_TARGET_SAME_USE = "SAME_USE"


_TARGET_UNKNOWN = "UNKNOWN"


_DIAGNOSTIC_GATE_REQUIRED = frozenset(("candidates",))


_DIAGNOSTIC_GATE_ALLOWED = frozenset(("candidates", "coverage", "evidence_bindings"))


_DIAGNOSTIC_CANDIDATE_REQUIRED = frozenset(
    ("candidate_id", "memory", "evidence_event_ids", "duplicate", "worth", "type", "scopes", "scope_source")
)


_DEADLINE_PREFIX_RE = re.compile(
    r"(?:截止(?:日期|时间)?|截至(?:日期|时间)?|不晚于|不得晚于|"
    r"计划(?:于|在|为)?|目标(?:于|在|为)?|安排(?:于|在)?|"
    r"(?:需(?:要)?|须|必须)(?:于|在)?|赶(?:在)?|"
    r"\b(?:scheduled|planned)\s+(?:for|on)\b|\btarget(?:ed)?(?:\s+date)?\b|"
    r"\bdeadline\b|\bdue(?:\s+date)?\b|\bby\b|\bbefore\b)"
    r"\s*(?:为|是|[:：])?\s*$",
    re.IGNORECASE,
)
_DEADLINE_SUFFIX_RE = re.compile(
    r"^\s*(?:(?:[01]?\d|2[0-3]):[0-5]\d\s*|(?:上午|下午|晚上|下班|中午|凌晨|傍晚|晚间)(?:\d{1,2}(?:点|时))?\s*)?(?:前|之前|为止|截止|截至|不晚于|不得晚于|before\b|deadline\b|due\b)",
    re.IGNORECASE,
)
_EXPLICIT_PROJECT_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9_])project\s*:\s*(?P<colon>[^\s,，;；。！？:：]{1,64})"
    r"|(?<![A-Za-z0-9_])project\s+(?P<english>[^\s,，;；。！？:：]{1,64})\s*[:：—–-]"
    r"|(?:^|[\s(（\[{,，])项目\s*[:：]\s*(?P<chinese>[^\s,，;；。！？:：]{1,64})"
    r"|(?:^|[\s(（\[{,，])(?P<suffix>[^\s,，;；。！？:：]{1,64})\s*项目\s*[:：]",
    re.IGNORECASE,
)




_SCOPE_CORRECTION_MARKER_RE = re.compile(
    r"(?:不是|并非|不属于|归错|归属错误|错误归属|应属于|应该属于|应该(?:是|说)|改归|改为|纠正为|"
    r"wrong\s+(?:project|scope)|belongs?\s+to|correct\s+(?:project|scope))",
    re.IGNORECASE,
)


_CANDIDATE_LOOKUP_SPLIT = re.compile(
    r"[;；。！？!?\n]+|[,，]\s*(?=(?:后续|今后|以后|每次|所有后续|going forward|from now on))"
    r"|(?:同时|另外|此外|其次|并且|并要求|还需|另需|in addition|separately)",
    re.IGNORECASE,
)


_DIAGNOSTIC_CANDIDATE_ALLOWED = _DIAGNOSTIC_CANDIDATE_REQUIRED | frozenset(
    ("reason", "duplicate_memory_id", "update_memory_id")
)


_DIAGNOSTIC_SUMMARY_REQUIRED = frozenset(("title", "body", "tags", "type", "scopes", "sources"))


_DIAGNOSTIC_SINGLE_PASS_REQUIRED = frozenset(("protocol_version", "items", "no_memory"))
_DIAGNOSTIC_SINGLE_PASS_ALLOWED = _DIAGNOSTIC_SINGLE_PASS_REQUIRED
_DIAGNOSTIC_SINGLE_PASS_ITEM_REQUIRED = frozenset(("candidate_id", "decision", "evidence"))
_DIAGNOSTIC_SINGLE_PASS_ITEM_ALLOWED = frozenset((
    "candidate_id", "decision", "evidence", "type", "scopes", "scope_source",
    "memory", "target_memory_id", "reason",
))


_DIAGNOSTIC_SUMMARY_ALLOWED = frozenset(
    (
        "memory_id",
        "update_memory_id",
        "title",
        "body",
        "tags",
        "aliases",
        "keywords",
        "type",
        "scopes",
        "scope_source",
        "sources",
        "evidence_event_ids",
        "status",
        "actionable",
        "completed_at",
        "due_date",
        "shadow_native_ids",
        "scope_operations",
    )
)


# The comparison catalog is the only part of the B3 prompt that grows with the
# Vault, so it needs a ceiling -- but the ceiling must be a safety net for the
# prompt's own hard limit, never a constraint that decides whether a turn may
# write.
#
# It used to be six items, which contradicted the retrieval design outright: a
# Vault whose memories all carry one scope (``global``, the common personal
# case, and the only scope in an empty registry) correctly resolves that scope
# to the whole library, and six rows then hid most of it.  Withholding a local
# record is also what marks the lookup unprovable, so a small item cap turned
# ordinary growth into failing turns.
#
# The ceiling is now derived from the prompt budget instead of chosen as a round
# number.  ``MAX_PROMPT_BYTES`` is 192 KiB; a catalog row measures about 1.6
# UTF-8 bytes per character in practice, so 40000 characters is roughly 64 KiB,
# leaving well over 100 KiB for the contract, the current evidence and the
# native catalog.  That admits roughly 180 memories at the observed row size --
# far beyond the point where compaction is meant to bring the active set down.
_RELATED_MAX_CHARS = 40000


# A single pathological body must not consume the whole catalog budget.  This
# only ever fires on a memory far longer than an atomic fact.
_RELATED_MAX_BODY_CHARS = 1600


_SCOPE_DIRECTORY_MAX_ITEMS = 8


_SCOPE_DIRECTORY_MAX_TITLE_CHARS = 180


_SCOPE_DIRECTORY_MAX_CHARS = 2400


_MAX_SESSION_LINEAGE_DEPTH = 32


_UNSET = object()


class ProcessingError(RuntimeError):
    """A process operation could not be committed safely."""


def _failure_metadata(
    error: BaseException,
) -> tuple[str, Optional[str], Optional[str], Optional[str], Optional[int]]:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or code not in MODEL_ERROR_CODES:
        if isinstance(error, ModelOutputError):
            code = "model_invalid_response"
        elif isinstance(error, ModelUnavailable):
            code = "model_unavailable"
        else:
            code = "model_failed"
    stage = getattr(error, "stage", None)
    if stage not in {"gate", "summarize", "single_pass"}:
        stage = None
    validation_reason = getattr(error, "validation_reason", None)
    if not isinstance(validation_reason, str) or validation_reason not in MODEL_VALIDATION_REASONS:
        validation_reason = "schema_violation" if isinstance(error, ModelOutputError) else None
        if code == "model_invalid_response" and validation_reason is None:
            validation_reason = "response_shape"
    validation_detail = getattr(error, "validation_detail", None)
    if not isinstance(validation_detail, str) or validation_detail not in MODEL_VALIDATION_DETAILS:
        validation_detail = "other_schema_violation" if isinstance(error, ModelOutputError) else None
    attempt_count = getattr(error, "attempt_count", None)
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3, 4):
        attempt_count = None
    return code, stage, validation_reason, validation_detail, attempt_count


def _safe_evidence_check(error: BaseException) -> Optional[str]:
    """Return only the bounded evidence diagnostic code, never raw output."""

    value = getattr(error, "evidence_check", None)
    return value if isinstance(value, str) and value in MODEL_EVIDENCE_CHECKS else None


def _safe_evidence_diagnostics(error: BaseException) -> dict[str, Any]:
    """Return allowlisted structural evidence details for local diagnostics."""

    return {**safe_evidence_context(error), **safe_schema_context(error)}


class _Snapshot:
    def __init__(self, turn: InboxTurn, token: str, state_key: str):
        self.turn = turn
        self.token = token
        self.state_key = state_key


def _empty_processed() -> dict[str, Any]:
    return {"version": 1, "event_keys": [], "events": {}, "sessions": {}}


def _read_processed(path: Path) -> dict[str, Any]:
    """Read control state without converting damaged authority into a fresh Vault.

    Absence is the historical first-use case. Existing invalid bytes, links and
    unsupported shapes are not equivalent to absence; leave them for recovery.
    Payload-specific checksums and versions remain the individual journal's job.
    """
    if path.is_symlink():
        raise ValueError("invalid_processed_state")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        from .state_layout import control_required
        if control_required(path):
            raise ValueError("required_processed_state_missing") from None
        return _empty_processed()
    try:
        value = parse_strict_json(raw)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("invalid_processed_state") from error
    if not isinstance(value, dict):
        raise ValueError("invalid_processed_state")
    result = dict(value)
    result.setdefault("version", 1)
    if type(result["version"]) is not int or result["version"] != 1:
        raise ValueError("unsupported_processed_state")
    result.setdefault("event_keys", [])
    result.setdefault("events", {})
    result.setdefault("sessions", {})
    if (not isinstance(result["event_keys"], list)
            or any(not isinstance(k, str) for k in result["event_keys"])
            or any(not isinstance(result[k], dict) for k in ("events", "sessions"))
            or any(not isinstance(row, dict) for name in ("events", "sessions")
                   for row in result[name].values())):
        raise ValueError("invalid_processed_state")
    return result


def _now_value(clock: Any = None) -> str:
    value: Any = None
    try:
        if clock is None:
            value = None
        elif hasattr(clock, "now") and callable(clock.now):
            value = clock.now()
        elif callable(clock):
            value = clock()
        else:
            value = clock
    except Exception as error:
        raise ProcessingError("clock failed") from error
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value
    raise ProcessingError("clock must return an ISO timestamp")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _session_key(source: str, session_id: str) -> str:
    return f"{source}/{session_id}"


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _safe_scope_background(state: Mapping[str, Any], explicit: Any = None) -> Any:
    value = explicit
    if value is None:
        value = state.get("scopes", state.get("scope_background", state.get("scope")))
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return []


def _event_payload(turn: InboxTurn) -> list[dict[str, Any]]:
    return [
        {
            "event_key": event.event_key,
            "role": event.role,
            "turn_id": event.turn_id or "",
            "message_id": event.message_id,
            "message_revision": event.message_revision,
            "previous_message_revision": event.previous_message_revision,
            "source_sequence": event.source_sequence,
            "previous_message_id": event.previous_message_id,
            "source_time": event.source_time,
            "captured_at": event.captured_at,
            "final": event.final,
            "timestamp": event.timestamp,
            "content": event.content,
            "tool_evidence": [dict(item) for item in event.tool_evidence],
        }
        for event in turn.events
    ]


def _add_hours(value: str, hours: int) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return value
    return (parsed + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")
