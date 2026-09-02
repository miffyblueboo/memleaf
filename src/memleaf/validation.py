"""Strict, side-effect-free validation for stage-B1 model JSON."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .scope_state import (
    ScopeError,
    project_scope_matches_text,
    validate_scope_key,
    validate_scope_registry,
)


class ModelOutputError(ValueError):
    """The model response is not an accepted strict JSON contract."""

    def __init__(
        self,
        message: str = "invalid model output",
        *,
        validation_reason: str | None = None,
        validation_detail: str | None = None,
    ):
        super().__init__(message)
        self.validation_reason = (
            validation_reason
            if isinstance(validation_reason, str)
            and validation_reason in MODEL_VALIDATION_REASONS
            else None
        )
        self.validation_detail = (
            validation_detail
            if isinstance(validation_detail, str) and validation_detail in MODEL_VALIDATION_DETAILS
            else None
        )

    def with_detail(self, detail: str | None) -> "ModelOutputError":
        if isinstance(detail, str) and detail in MODEL_VALIDATION_DETAILS:
            self.validation_detail = detail
        return self


MEMORY_TYPES = frozenset(("preference", "fact", "project", "todo", "event", "identity", "other"))
SCOPE_SOURCES = frozenset(("model", "user", "session_context", "insufficient_context"))
TODO_STATUSES = frozenset(("active", "completed", "cancelled"))
NO_CHANGE_DECISION = "NO_CHANGE"
MODEL_VALIDATION_REASONS = frozenset(
    ("empty_content", "invalid_json", "schema_violation", "response_shape")
)
MODEL_VALIDATION_DETAILS = frozenset(
    (
        "root_shape",
        "missing_fields",
        "unknown_fields",
        "candidate_shape",
        "duplicate_candidate_id",
        "duplicate_update_target",
        "mixed_project_scopes",
        "update_target_type_mismatch",
        "target_not_relevant",
        "scope_not_grounded",
        "scope_drift",
        "invalid_evidence",
        "invalid_flags",
        "invalid_type",
        "invalid_duplicate_target",
        "invalid_update_target",
        "invalid_scope",
        "invalid_scope_source",
        "reason_too_long",
        "source_shape",
        "todo_fields",
        "relative_time",
        "other_schema_violation",
    )
)
_SCOPE_NAME = re.compile(r"^[^\s/\\:\x00\r\n]+$")
_RELATIVE_DATE_TOKEN = (
    r"(?:"
    r"(?<![A-Za-z])(?:today|tomorrow|yesterday)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:this|next|last)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?![A-Za-z])"
    r"|(?:本|这|下|上)(?:个)?(?:周|星期|礼拜)\s*"
    r"(?:(?:星期|礼拜)\s*)?(?:一|二|三|四|五|六|日|天|末|[1-7])"
    r"|(?:今天|明天|昨天|今日|明日|昨日)"
    r")"
)
_RELATIVE_CALENDAR_EXPRESSION = re.compile(_RELATIVE_DATE_TOKEN, re.IGNORECASE)
_RELATIVE_PARENTHESIZED_DATE = re.compile(
    rf"({_RELATIVE_DATE_TOKEN})([ \t]*)([（(])([^()\r\n]*)([）)])",
    re.IGNORECASE,
)
_NUMERIC_CALENDAR_DATE = re.compile(
    r"(?<![A-Za-z\d./-])(?:"
    r"\d{4}[/\-](?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])"
    r"|(?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])"
    r"|(?:\d{4}\s*年\s*)?(?:0?[1-9]|1[0-2])\s*月\s*(?:0?[1-9]|[12]\d|3[01])\s*日?"
    r")(?![A-Za-z\d./-])"
)
_ISO_CALENDAR_DATE = re.compile(
    r"(?<![A-Za-z\d./-])\d{4}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])"
    r"(?![A-Za-z\d./-])"
)
_EMPTY_ISO_DATE_PARENTHESIS = re.compile(
    r"(?P<date>\d{4}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01]))"
    r"[ \t]*(?:\([ \t]*[)）]|（[ \t]*[)）])"
)
_DUPLICATE_ISO_DATE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"\s*[,，、:]?\s*(?:(?:就是|即|即为|即是|也就是|是|为)|[/／])\s*"
    r"(?P=date)(?!\d)"
)
_ENGLISH_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_CHINESE_WEEKDAYS = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}
_RELATIVE_DAY_OFFSETS = {
    "today": 0,
    "tomorrow": 1,
    "yesterday": -1,
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "昨天": -1,
    "昨日": -1,
}
_SOURCE_FIELDS = frozenset(("event_key", "session_id", "turn_id", "conversation_title", "evidence_event_ids"))
_COMPACT_FIELDS = frozenset(
    (
        "title",
        "body",
        "tags",
        "type",
        "scopes",
        "scope_source",
        "aliases",
        "keywords",
        "source_memory_ids",
        "status",
        "completed_at",
    )
)
_MAX_SCOPE_OPERATION_COUNT = 16
_MAX_SCOPE_OPERATION_TEXT = 128


def _calendar_anchor_date(anchor: Any) -> date | None:
    """Return a UTC calendar date only for a parseable evidence timestamp."""

    if isinstance(anchor, datetime):
        parsed = anchor
    elif isinstance(anchor, date):
        return anchor
    elif isinstance(anchor, str) and anchor.strip():
        try:
            parsed = datetime.fromisoformat(anchor.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc).date()
    except (OverflowError, ValueError):
        return None


def _resolve_relative_date(token: str, anchor: date) -> str | None:
    """Resolve one supported strong relative expression from an anchor date."""

    normalized = token.strip().casefold()
    if normalized in _RELATIVE_DAY_OFFSETS:
        offset = _RELATIVE_DAY_OFFSETS[normalized]
        try:
            return (anchor + timedelta(days=offset)).isoformat()
        except OverflowError:
            return None

    english_match = re.fullmatch(
        r"(this|next|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        normalized,
        re.IGNORECASE,
    )
    if english_match:
        prefix, weekday = english_match.groups()
        week_offset = {"this": 0, "next": 1, "last": -1}[prefix.casefold()]
        try:
            monday = anchor - timedelta(days=anchor.weekday())
            return (monday + timedelta(days=week_offset * 7 + _ENGLISH_WEEKDAYS[weekday.casefold()])).isoformat()
        except (KeyError, OverflowError):
            return None

    chinese_match = re.fullmatch(
        r"(本|这|下|上)(?:个)?(?:周|星期|礼拜)\s*(?:(?:星期|礼拜)\s*)?(一|二|三|四|五|六|日|天|末|[1-7])",
        token.strip(),
    )
    if chinese_match:
        prefix, weekday = chinese_match.groups()
        # 周末 has no single safe calendar date; leave it for strict
        # validation/deferred-candidate handling instead of guessing Sunday.
        if weekday == "末":
            return None
        week_offset = {"本": 0, "这": 0, "下": 1, "上": -1}[prefix]
        try:
            monday = anchor - timedelta(days=anchor.weekday())
            return (monday + timedelta(days=week_offset * 7 + _CHINESE_WEEKDAYS[weekday])).isoformat()
        except (KeyError, OverflowError):
            return None
    return None


def _valid_calendar_date_token(value: str, fallback_year: int) -> bool:
    parts = [int(item) for item in re.findall(r"\d+", value)]
    if len(parts) == 2:
        year, month, day = fallback_year, parts[0], parts[1]
    elif len(parts) == 3:
        year, month, day = parts
    else:
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _strip_parenthesized_calendar_dates(value: str, resolved_date: str) -> str:
    """Remove duplicate numeric/ISO date spellings from a relative-date note."""

    try:
        fallback_year = int(resolved_date[:4])
    except (TypeError, ValueError):
        return value

    def remove_valid_date(match: re.Match[str]) -> str:
        return "" if _valid_calendar_date_token(match.group(0), fallback_year) else match.group(0)

    value = _NUMERIC_CALENDAR_DATE.sub(remove_valid_date, value)
    value = _ISO_CALENDAR_DATE.sub(remove_valid_date, value)
    value = value.replace(resolved_date, "")
    value = re.sub(r"[ \t]+", " ", value).strip()
    return value.strip(" \t,，;；:：")


def _collapse_duplicate_calendar_dates(value: str) -> str:
    """Collapse an explicit equivalence that repeats the same ISO date."""

    previous = None
    while previous != value:
        previous = value
        value = _DUPLICATE_ISO_DATE.sub(r"\g<date>", value)
    return value


def _strip_empty_iso_date_parenthesis(value: str) -> str:
    """Remove empty ASCII/Chinese parentheses attached to an ISO date."""

    if not isinstance(value, str):
        return value
    return _EMPTY_ISO_DATE_PARENTHESIS.sub(r"\g<date>", value)


def _normalize_relative_calendar_text(
    text: str,
    anchor: Any,
    *,
    _handle_parentheses: bool = True,
) -> tuple[str, bool]:
    """Normalize strong relative dates, reporting whether every one was safe."""

    anchor_date = _calendar_anchor_date(anchor)
    if anchor_date is None:
        return text, False

    if _handle_parentheses:
        parenthetical_safe = True

        def replace_parenthetical(match: re.Match[str]) -> str:
            nonlocal parenthetical_safe
            resolved = _resolve_relative_date(match.group(1), anchor_date)
            if resolved is None:
                parenthetical_safe = False
                return match.group(0)
            inner, inner_safe = _normalize_relative_calendar_text(
                match.group(4),
                anchor_date,
                _handle_parentheses=False,
            )
            if not inner_safe:
                parenthetical_safe = False
                return match.group(0)
            remainder = _strip_parenthesized_calendar_dates(inner, resolved)
            if not remainder:
                return resolved
            return f"{resolved}{match.group(3)}{remainder}{match.group(5)}"

        text = _RELATIVE_PARENTHESIZED_DATE.sub(replace_parenthetical, text)
        if not parenthetical_safe:
            return text, False

    safe = True
    relative_replaced = False

    def replace_token(match: re.Match[str]) -> str:
        nonlocal relative_replaced, safe
        resolved = _resolve_relative_date(match.group(0), anchor_date)
        if resolved is None:
            safe = False
            return match.group(0)
        relative_replaced = True
        return resolved

    normalized = _RELATIVE_CALENDAR_EXPRESSION.sub(replace_token, text)
    if relative_replaced:
        normalized = _collapse_duplicate_calendar_dates(normalized)
    normalized = _strip_empty_iso_date_parenthesis(normalized)
    return normalized, safe


def normalize_relative_calendar_text(text: str, anchor: Any) -> str | None:
    """Return text with safely resolvable one-off dates made absolute.

    ``None`` means the anchor is invalid or at least one strong expression is
    outside the deterministic subset.  Callers must then keep the strict
    relative-date validator active and defer/fail rather than guessing.
    """

    if not isinstance(text, str):
        return None
    normalized, safe = _normalize_relative_calendar_text(text, anchor)
    return normalized if safe else None


_AGGREGATE_DIGEST_MARKERS = (
    "日报",
    "汇总",
    "巡检",
    "收件箱",
    "邮箱巡检",
    "daily report",
    "digest",
    "watchlist",
    "mailbox sweep",
    "inbox sweep",
)
_AGGREGATE_COUNT = re.compile(
    r"(?:\d+\s*(?:条|项|个|件|封|tasks?|items?)|(?:完成|待受理|派发|新增|逾期)\s*\d+)"
    r"|(?:completed|pending|assigned|new|overdue)\s*[:：]?\s*\d+",
    re.IGNORECASE,
)


def is_aggregate_operational_text(value: Any) -> bool:
    """Recognize a combined operational digest, not one concrete work item.

    This deliberately requires multiple aggregate signals.  A single overdue
    task reported by a daily scan remains eligible for the model's normal
    future-use decision; a multi-count sweep must be split before persistence.
    """

    if not isinstance(value, str):
        return False
    folded = value.casefold()
    if not any(marker in folded for marker in _AGGREGATE_DIGEST_MARKERS):
        return False
    count_signals = len(_AGGREGATE_COUNT.findall(value))
    separator_count = len(re.findall(r"[;；]|[—-].*[;；]", value))
    return count_signals >= 2 or (count_signals >= 1 and separator_count >= 1)


def _reject_constant(value: str) -> None:
    raise ModelOutputError("non-finite JSON number is not allowed", validation_reason="invalid_json")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelOutputError("duplicate JSON key", validation_reason="invalid_json")
        result[key] = value
    return result


def parse_strict_json(raw: str) -> Any:
    """Parse exactly one JSON value; fences, duplicate keys, and tail text fail."""

    if not isinstance(raw, str) or not raw.strip():
        raise ModelOutputError("model output must be JSON text", validation_reason="empty_content")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except ModelOutputError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ModelOutputError("model output is not strict JSON", validation_reason="invalid_json") from error


def _event_key_set(event_keys: Iterable[Any]) -> set[str]:
    if isinstance(event_keys, Mapping):
        values = event_keys.keys()
    else:
        values = event_keys
    result = {
        (value if isinstance(value, str) else getattr(value, "event_key", "")).casefold()
        for value in values
        if isinstance(value, str) or isinstance(getattr(value, "event_key", None), str)
    }
    return result


def _require_keys(value: Mapping[str, Any], required: set[str], allowed: set[str]) -> None:
    missing = required - set(value)
    if missing:
        raise ModelOutputError("model output is missing required fields", validation_detail="missing_fields")
    unknown = set(value) - allowed
    if unknown:
        raise ModelOutputError("model output contains unknown fields", validation_detail="unknown_fields")


def _string(
    value: Any,
    field: str,
    *,
    nonempty: bool = True,
    multiline: bool = False,
    validation_detail: str = "other_schema_violation",
) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ModelOutputError(f"invalid {field}", validation_detail=validation_detail)
    if "\x00" in value or (not multiline and ("\n" in value or "\r" in value)):
        raise ModelOutputError(f"invalid {field}", validation_detail=validation_detail)
    return value


def _string_list(
    value: Any,
    field: str,
    *,
    nonempty: bool = False,
    validation_detail: str = "other_schema_violation",
) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ModelOutputError(f"invalid {field}", validation_detail=validation_detail)
    result = []
    for item in value:
        result.append(_string(item, field, validation_detail=validation_detail))
    return result


def _reject_relative_calendar_expression(item: Mapping[str, Any]) -> None:
    """Reject unresolved one-off calendar expressions without parsing dates.

    The event timestamp is supplied to the model in the prompt, where the
    model can resolve the expression.  This validator only catches the small
    set of strong relative forms; recurring language such as 每周三 and
    every Wednesday is intentionally outside the expression.
    """

    text = "\n".join(
        value
        for field in ("title", "body")
        if isinstance(value := item.get(field), str)
    )
    if _RELATIVE_CALENDAR_EXPRESSION.search(text):
        raise ModelOutputError(
            "summary contains an unresolved relative calendar date",
            validation_detail="relative_time",
        )


def _scopes(value: Any, scope_source: Any) -> list[str]:
    result = _string_list(value, "scopes", validation_detail="invalid_scope")
    for scope in result:
        if scope in ("global", "unscoped"):
            continue
        prefix, separator, name = scope.partition(":")
        if prefix not in ("domain", "portfolio", "project") or not separator or not _SCOPE_NAME.fullmatch(name):
            raise ModelOutputError("invalid scope", validation_detail="invalid_scope")
        if name in (".", ".."):
            raise ModelOutputError("invalid scope", validation_detail="invalid_scope")
    if "unscoped" in result and len(result) != 1:
        raise ModelOutputError("unscoped must be the only scope", validation_detail="invalid_scope")
    if scope_source is not None:
        _string(scope_source, "scope_source", validation_detail="invalid_scope_source")
        if scope_source not in SCOPE_SOURCES:
            raise ModelOutputError("invalid scope_source", validation_detail="invalid_scope_source")
    if not result:
        raise ModelOutputError("empty scopes must use unscoped", validation_detail="invalid_scope")
    if "unscoped" in result:
        if scope_source != "insufficient_context":
            raise ModelOutputError("unscoped requires insufficient_context", validation_detail="invalid_scope")
    elif scope_source == "insufficient_context":
        raise ModelOutputError(
            "insufficient_context requires unscoped",
            validation_detail="invalid_scope",
        )
    return result


def _reject_mixed_project_scopes(scopes: Iterable[str]) -> None:
    """Keep one worthy memory atomic across independent project scopes.

    Parent scopes (global/domain/portfolio) may accompany one project scope;
    only two or more distinct project scopes represent an ambiguous aggregate.
    """

    project_scopes = {
        scope.casefold()
        for scope in scopes
        if isinstance(scope, str) and scope.partition(":")[0] == "project"
    }
    if len(project_scopes) > 1:
        raise ModelOutputError(
            "one memory cannot cover multiple project scopes",
            validation_detail="mixed_project_scopes",
        )


def _model_scope_grounding_evidence(
    memory: str,
    scopes: Iterable[str],
    scope_registry: Mapping[str, Any] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return model scope ownership and memory-grounded matches.

    Returns a tuple of:
      - selected project scopes -> selected owner scope (casefold)
      - grounded project scope matches from candidate memory (casefold -> canonical)
    """

    selected = {
        scope.casefold(): scope
        for scope in scopes
        if isinstance(scope, str) and scope.partition(":")[0] == "project"
    }
    if not selected:
        return {}, {}
    try:
        registry = validate_scope_registry(scope_registry or {})
    except ScopeError as error:
        raise ModelOutputError("invalid scope registry", validation_detail="invalid_scope") from error

    # Resolve each selected project scope to its canonical owner when it is an
    # alias of a registered project scope.
    selected_owners: dict[str, str] = {}
    for selected_scope in selected.values():
        selected_owner = selected_scope
        for registered_scope, node in registry.items():
            if not registered_scope.startswith("project:"):
                continue
            terms = {
                registered_scope.casefold(),
                registered_scope.split(":", 1)[1].casefold(),
            }
            aliases = node.get("aliases", [])
            if isinstance(aliases, list):
                for alias in aliases:
                    if isinstance(alias, str):
                        alias_key = alias.casefold()
                        terms.add(alias_key)
                        if ":" not in alias:
                            terms.add(f"project:{alias_key}")
            if selected_scope.casefold() in terms:
                selected_owner = registered_scope
                break
        selected_owners[selected_scope.casefold()] = selected_owner.casefold()

    # Include unregistered selections as temporary nodes: new scopes must be
    # grounded by their own project name. All known projects are still checked
    # to reject ambiguous memory wording.
    candidate_registry = dict(registry)
    for selected_scope in selected.values():
        owner = selected_owners[selected_scope.casefold()]
        if owner not in {registered.casefold() for registered in registry}:
            candidate_registry.setdefault(selected_scope, {})

    matches = {
        scope.casefold(): scope
        for scope in project_scope_matches_text(memory, {"scopes": candidate_registry})
    }
    return selected_owners, matches


def _reject_ungrounded_project_scope(
    memory: str,
    scopes: Iterable[str],
    scope_registry: Mapping[str, Any] | None,
) -> None:
    """Require model-attributed project scopes to be named by this candidate.

    The gate's memory text is the only candidate-local attribution evidence.
    Session background, other events, and related-memory bodies are
    intentionally excluded so an aggregate mailbox turn cannot lend a
    project name to an unrelated candidate.
    """

    selected_owners, matches = _model_scope_grounding_evidence(
        memory,
        scopes,
        scope_registry,
    )
    if not selected_owners:
        return
    if len(matches) != 1 or matches.keys().isdisjoint(set(selected_owners.values())):
        raise ModelOutputError(
            "model project scope is not grounded by this candidate",
            validation_detail="scope_not_grounded",
        )


def _memory_id(value: Any, field: str) -> str:
    result = _string(value, field)
    if "/" in result or "\\" in result or result in (".", ".."):
        raise ModelOutputError(f"invalid {field}")
    return result


def _scope_operation_name(value: Any, field: str) -> str:
    result = _string(value, field)
    if len(result) > _MAX_SCOPE_OPERATION_TEXT:
        raise ModelOutputError(f"{field} is too long")
    try:
        validate_scope_key(result, allow_special=False)
    except ScopeError as error:
        raise ModelOutputError(f"invalid {field}") from error
    return result


def _scope_operation_aliases(value: Any) -> list[str]:
    aliases = _string_list(value, "scope operation aliases")
    if len(aliases) > 32:
        raise ModelOutputError("too many scope operation aliases")
    result: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if len(alias) > _MAX_SCOPE_OPERATION_TEXT:
            raise ModelOutputError("scope operation alias is too long")
        normalized = alias.strip()
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _scope_parent_allowed(child: str, parent: str) -> bool:
    if parent == "global":
        return True
    child_level = {"domain": 1, "portfolio": 2, "project": 3}.get(child.split(":", 1)[0], -1)
    parent_level = {"domain": 1, "portfolio": 2, "project": 3}.get(parent.split(":", 1)[0], -1)
    return parent_level >= 0 and child_level > parent_level and not (
        child_level == 1 and parent_level != 0
    )


def _scope_operations(
    value: Any,
    *,
    summary_scopes: Iterable[str],
    scope_registry: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_SCOPE_OPERATION_COUNT:
        raise ModelOutputError("scope_operations must be a list of at most 16 items")
    try:
        current = validate_scope_registry(scope_registry or {})
    except ScopeError as error:
        raise ModelOutputError("scope registry is invalid") from error
    current_keys = {item.casefold() for item in current}
    summary_keys = {item.casefold() for item in summary_scopes}
    operation_names: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ModelOutputError("each scope operation must be an object")
        if raw.get("op") == "upsert":
            operation_names.add(str(raw.get("scope", "")).casefold())
            parent = raw.get("parent")
            if isinstance(parent, str):
                operation_names.add(parent.casefold())
        elif raw.get("op") == "merge":
            operation_names.add(str(raw.get("target", "")).casefold())

    normalized: list[dict[str, Any]] = []
    for raw in value:
        op = raw.get("op") if isinstance(raw, Mapping) else None
        if op == "upsert":
            _require_keys(raw, {"op", "scope", "parent", "aliases"}, {"op", "scope", "parent", "aliases"})
            scope = _scope_operation_name(raw["scope"], "scope operation scope")
            parent = raw["parent"]
            if parent is not None:
                if not isinstance(parent, str) or parent == "unscoped":
                    raise ModelOutputError("invalid scope operation parent")
                if parent != "global":
                    parent = _scope_operation_name(parent, "scope operation parent")
                if not _scope_parent_allowed(scope, parent):
                    raise ModelOutputError("scope operation parent has an invalid level")
                if parent.casefold() not in current_keys and parent.casefold() not in operation_names and parent != "global":
                    raise ModelOutputError("scope operation parent is not registered or declared")
            normalized.append({
                "op": "upsert",
                "scope": scope,
                "parent": parent,
                "aliases": _scope_operation_aliases(raw["aliases"]),
            })
            continue
        if op == "merge":
            _require_keys(raw, {"op", "source", "target", "aliases"}, {"op", "source", "target", "aliases"})
            source = _scope_operation_name(raw["source"], "scope merge source")
            target = _scope_operation_name(raw["target"], "scope merge target")
            aliases = _scope_operation_aliases(raw["aliases"])
            if source.casefold() in ("global", "unscoped") or target.casefold() in ("global", "unscoped"):
                raise ModelOutputError("global and unscoped cannot be merged")
            if source.casefold() == target.casefold():
                raise ModelOutputError("scope cannot merge into itself")
            if source.split(":", 1)[0] != target.split(":", 1)[0]:
                raise ModelOutputError("scope merge types must match")
            if target.casefold() not in current_keys and target.casefold() not in summary_keys and target.casefold() not in operation_names:
                raise ModelOutputError("scope merge target is not registered or declared")
            target_node = next(
                (node for name, node in current.items() if name.casefold() == target.casefold()),
                None,
            )
            target_aliases = {
                alias.casefold()
                for alias in (target_node.get("aliases", []) if isinstance(target_node, Mapping) else [])
                if isinstance(alias, str)
            }
            already_applied = (
                source.casefold() not in current_keys
                and target.casefold() in current_keys
                and source.casefold() in target_aliases
            )
            if source.casefold() not in current_keys and not already_applied:
                raise ModelOutputError("scope merge source is not registered")
            normalized_item = {
                "op": "merge",
                "source": source,
                "target": target,
                "aliases": aliases,
            }
            if already_applied:
                normalized_item["already_applied"] = True
            normalized.append(normalized_item)
            continue
        raise ModelOutputError("unknown scope operation")

    merge_targets: dict[str, str] = {}
    upsert_parents: dict[str, str | None] = {}
    for item in normalized:
        if item["op"] == "upsert":
            scope_key = item["scope"].casefold()
            parent = item.get("parent")
            previous_parent = upsert_parents.get(scope_key, parent)
            if scope_key in upsert_parents and previous_parent != parent:
                raise ModelOutputError("scope operation moves one scope to conflicting parents")
            upsert_parents[scope_key] = parent
            continue
        source_key = item["source"].casefold()
        target_key = item["target"].casefold()
        if source_key in merge_targets:
            raise ModelOutputError("scope merge source is repeated")
        merge_targets[source_key] = target_key
    for source in merge_targets:
        seen: set[str] = set()
        current_name = source
        while current_name in merge_targets:
            if current_name in seen:
                raise ModelOutputError("scope merge operations contain a cycle")
            seen.add(current_name)
            current_name = merge_targets[current_name]
    return normalized


def validate_gate_output(
    value: Any,
    event_keys: Iterable[Any] | None = None,
    *,
    current_event_keys: Iterable[Any] | None = None,
    related_memory_ids: Iterable[Any] | None = None,
    related_memory_types: Mapping[str, Any] | None = None,
    scope_registry: Mapping[str, Any] | None = None,
    enforce_model_scope_grounding: bool = True,
) -> dict[str, Any]:
    """Validate and return a normalized gate object without writing anything."""

    if isinstance(value, str):
        value = parse_strict_json(value)
    if not isinstance(value, Mapping):
        raise ModelOutputError("gate output must be an object", validation_detail="root_shape")
    _require_keys(value, {"candidates"}, {"candidates"})
    candidates = value["candidates"]
    if not isinstance(candidates, list):
        raise ModelOutputError("gate candidates must be a list", validation_detail="root_shape")
    allowed_event_keys = _event_key_set(
        current_event_keys if current_event_keys is not None else (event_keys or [])
    )
    allowed = {
        "candidate_id",
        "memory",
        "evidence_event_ids",
        "duplicate",
        "worth",
        "type",
        "scopes",
        "scope_source",
        "reason",
        "duplicate_memory_id",
        "update_memory_id",
    }
    normalized: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    target_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ModelOutputError("each gate candidate must be an object", validation_detail="candidate_shape")
        _require_keys(candidate, allowed - {"reason", "duplicate_memory_id", "update_memory_id"}, allowed)
        item = dict(candidate)
        _string(item["candidate_id"], "candidate_id", validation_detail="candidate_shape")
        candidate_id = item["candidate_id"].casefold()
        if candidate_id in candidate_ids:
            raise ModelOutputError("duplicate candidate_id", validation_detail="duplicate_candidate_id")
        candidate_ids.add(candidate_id)
        _string(item["memory"], "memory", validation_detail="candidate_shape")
        evidence = _string_list(
            item["evidence_event_ids"],
            "evidence_event_ids",
            nonempty=True,
            validation_detail="invalid_evidence",
        )
        if any(event_id.casefold() not in allowed_event_keys for event_id in evidence):
            raise ModelOutputError("candidate evidence references another turn", validation_detail="invalid_evidence")
        if type(item["duplicate"]) is not bool or type(item["worth"]) is not bool:
            raise ModelOutputError("duplicate and worth must be booleans", validation_detail="invalid_flags")
        candidate_type = item["type"]
        if candidate_type is not None and (
            not isinstance(candidate_type, str) or candidate_type not in MEMORY_TYPES
        ):
            raise ModelOutputError("invalid candidate type", validation_detail="invalid_type")
        if item["duplicate"] and item["worth"]:
            raise ModelOutputError("duplicate candidate cannot be worth remembering", validation_detail="invalid_flags")
        if item["worth"] and candidate_type is None:
            raise ModelOutputError("worth candidate must have a type", validation_detail="invalid_type")
        if "update_memory_id" in item and "duplicate_memory_id" in item:
            raise ModelOutputError(
                "a candidate cannot set both duplicate_memory_id and update_memory_id",
                validation_detail="invalid_update_target",
            )
        if "duplicate_memory_id" in item:
            if not item["duplicate"] or item["worth"]:
                raise ModelOutputError(
                    "duplicate_memory_id requires duplicate=true and worth=false",
                    validation_detail="invalid_duplicate_target",
                )
            try:
                duplicate_id = _memory_id(item["duplicate_memory_id"], "duplicate_memory_id")
            except ModelOutputError as error:
                error.validation_detail = "invalid_duplicate_target"
                raise
            related_ids = {
                candidate.casefold()
                for candidate in (related_memory_ids or [])
                if isinstance(candidate, str)
            }
            if duplicate_id.casefold() not in related_ids:
                raise ModelOutputError(
                    "duplicate_memory_id is not a related active memory",
                    validation_detail="invalid_duplicate_target",
                )
            item["duplicate_memory_id"] = duplicate_id
        if "update_memory_id" in item:
            if item["duplicate"] or not item["worth"] or "duplicate_memory_id" in item:
                raise ModelOutputError(
                    "update_memory_id requires duplicate=false and worth=true",
                    validation_detail="invalid_update_target",
                )
            try:
                update_id = _memory_id(item["update_memory_id"], "update_memory_id")
            except ModelOutputError as error:
                error.validation_detail = "invalid_update_target"
                raise
            related_ids = {
                candidate.casefold()
                for candidate in (related_memory_ids or [])
                if isinstance(candidate, str)
            }
            if update_id.casefold() not in related_ids:
                raise ModelOutputError(
                    "update_memory_id is not a related active memory",
                    validation_detail="invalid_update_target",
                )
            target_type = None
            if isinstance(related_memory_types, Mapping):
                target_type = next(
                    (
                        value
                        for key, value in related_memory_types.items()
                        if isinstance(key, str) and key.casefold() == update_id.casefold()
                    ),
                    None,
                )
            if target_type is not None and candidate_type != target_type:
                raise ModelOutputError(
                    "candidate type does not match update target",
                    validation_detail="update_target_type_mismatch",
                )
            item["update_memory_id"] = update_id
        for target_field in ("duplicate_memory_id", "update_memory_id"):
            target = item.get(target_field)
            if isinstance(target, str):
                target_key = target.casefold()
                if target_key in target_ids:
                    raise ModelOutputError(
                        "multiple gate candidates reference the same memory target; merge them into one candidate",
                        validation_detail="duplicate_update_target",
                    )
                target_ids.add(target_key)
        if not isinstance(item["scope_source"], str) or item["scope_source"] not in SCOPE_SOURCES:
            raise ModelOutputError("invalid scope_source", validation_detail="invalid_scope_source")
        item["scopes"] = _scopes(item["scopes"], item["scope_source"])
        if item["worth"]:
            _reject_mixed_project_scopes(item["scopes"])
            if item["scope_source"] == "model":
                if enforce_model_scope_grounding:
                    _reject_ungrounded_project_scope(item["memory"], item["scopes"], scope_registry)
        if "reason" in item:
            _string(item["reason"], "reason", nonempty=False)
            if len(item["reason"]) > 30:
                raise ModelOutputError("reason is too long", validation_detail="reason_too_long")
        item["evidence_event_ids"] = evidence
        normalized.append(item)
    return {"candidates": normalized}


def parse_gate_output(
    raw: str,
    event_keys: Iterable[Any] | None = None,
    *,
    current_event_keys: Iterable[Any] | None = None,
    related_memory_ids: Iterable[Any] | None = None,
    related_memory_types: Mapping[str, Any] | None = None,
    scope_registry: Mapping[str, Any] | None = None,
    enforce_model_scope_grounding: bool = True,
) -> dict[str, Any]:
    try:
        parsed = parse_strict_json(raw)
    except ModelOutputError as error:
        if error.validation_detail is None:
            error.validation_detail = "other_schema_violation"
        raise
    try:
        return validate_gate_output(
            parsed,
            event_keys,
            current_event_keys=current_event_keys,
            related_memory_ids=related_memory_ids,
            related_memory_types=related_memory_types,
            scope_registry=scope_registry,
            enforce_model_scope_grounding=enforce_model_scope_grounding,
        )
    except ModelOutputError as error:
        if error.validation_reason is None:
            error.validation_reason = "schema_violation"
        if error.validation_detail is None:
            error.validation_detail = "other_schema_violation"
        raise


def _source_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ModelOutputError("sources must be a non-empty list", validation_detail="source_shape")
    result: list[dict[str, Any]] = []
    for source in value:
        if not isinstance(source, Mapping):
            raise ModelOutputError("each source must be an object", validation_detail="source_shape")
        if set(source) - set(_SOURCE_FIELDS):
            raise ModelOutputError("source contains unknown fields", validation_detail="unknown_fields")
        item = {}
        for key, child in source.items():
            _string(key, "source field", validation_detail="source_shape")
            if key == "event_key":
                item[key] = _string(child, "source event_key", validation_detail="source_shape")
            elif key == "evidence_event_ids":
                item[key] = _string_list(
                    child,
                    "source evidence_event_ids",
                    nonempty=True,
                    validation_detail="source_shape",
                )
            elif isinstance(child, str):
                item[key] = _string(child, f"source {key}", validation_detail="source_shape")
            else:
                raise ModelOutputError("source fields must be strings or event-key lists", validation_detail="source_shape")
        result.append(item)
    return result


def validate_summarize_output(
    value: Any,
    current_event_keys: Iterable[Any] | None = None,
    *,
    related_native_ids: Iterable[Any] | None = None,
    related_memory_ids: Iterable[Any] | None = None,
    scope_registry: Mapping[str, Any] | None = None,
    expected_type: str | None = None,
    expected_update_memory_id: str | None = None,
    expected_target_type: str | None = None,
    expected_scopes: Iterable[Any] | None = None,
    expected_scope_source: str | None = None,
    allow_no_change: bool = False,
) -> dict[str, Any]:
    """Validate one atomic memory summary; this function has no filesystem effects."""

    if isinstance(value, str):
        value = parse_strict_json(value)
    if not isinstance(value, Mapping):
        raise ModelOutputError("summarize output must be an object", validation_detail="root_shape")
    if "decision" in value:
        if allow_no_change and dict(value) == {"decision": NO_CHANGE_DECISION}:
            return {"decision": NO_CHANGE_DECISION}
        raise ModelOutputError(
            "NO_CHANGE is only valid as the exact automatic no-write response",
            validation_detail="unknown_fields",
        )
    allowed = {
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
        "completed_at",
        "shadow_native_ids",
        "scope_operations",
    }
    _require_keys(value, {"title", "body", "tags", "type", "scopes", "sources"}, allowed)
    item = dict(value)
    for field in ("title", "body"):
        if isinstance(item.get(field), str):
            item[field] = _strip_empty_iso_date_parenthesis(item[field])
    _string(item["title"], "title")
    _string(item["body"], "body", multiline=True)
    _reject_relative_calendar_expression(item)
    item["tags"] = _string_list(item["tags"], "tags")
    candidate_type = item["type"]
    if not isinstance(candidate_type, str) or candidate_type not in MEMORY_TYPES:
        raise ModelOutputError("invalid memory type", validation_detail="invalid_type")
    if expected_type is not None and candidate_type != expected_type:
        raise ModelOutputError(
            "summary type differs from gate candidate",
            validation_detail="invalid_type",
        )
    if expected_target_type is not None and candidate_type != expected_target_type:
        raise ModelOutputError(
            "summary type does not match update target",
            validation_detail="invalid_type",
        )
    summary_scope_source = item.get("scope_source")
    if "scope_source" not in item and expected_scope_source is not None:
        summary_scope_source = expected_scope_source
        item["scope_source"] = summary_scope_source
    item["scopes"] = _scopes(item["scopes"], summary_scope_source)
    _reject_mixed_project_scopes(item["scopes"])
    if expected_scopes is not None:
        expected_scope_values = list(expected_scopes)
        if item["scopes"] != expected_scope_values:
            raise ModelOutputError(
                "summary scopes differ from gate candidate",
                validation_detail="scope_drift",
            )
    if expected_scope_source is not None and summary_scope_source != expected_scope_source:
        raise ModelOutputError(
            "summary scope source differs from gate candidate",
            validation_detail="scope_drift",
        )
    item["scope_operations"] = _scope_operations(
        item.get("scope_operations"),
        summary_scopes=item["scopes"],
        scope_registry=scope_registry,
    )
    allowed_event_keys = _event_key_set(current_event_keys or [])
    item["sources"] = _source_items(item["sources"])
    for source in item["sources"]:
        if "event_key" in source and source["event_key"].casefold() not in allowed_event_keys:
            raise ModelOutputError("source references another turn", validation_detail="invalid_evidence")
        if "evidence_event_ids" in source and any(
            event_id.casefold() not in allowed_event_keys
            for event_id in source["evidence_event_ids"]
        ):
            raise ModelOutputError("source evidence references another turn", validation_detail="invalid_evidence")
    if "memory_id" in item:
        item["memory_id"] = _memory_id(item["memory_id"], "memory_id")
    if "update_memory_id" in item:
        item["update_memory_id"] = _memory_id(item["update_memory_id"], "update_memory_id")
        if (
            expected_update_memory_id is not None
            and (
                not isinstance(expected_update_memory_id, str)
                or item["update_memory_id"].casefold() != expected_update_memory_id.casefold()
            )
        ):
            raise ModelOutputError(
                "summary update target differs from gate target",
                validation_detail="invalid_update_target",
            )
        if related_memory_ids is not None:
            related_ids = {
                memory_id.casefold()
                for memory_id in related_memory_ids
                if isinstance(memory_id, str)
            }
            if item["update_memory_id"].casefold() not in related_ids:
                raise ModelOutputError(
                    "update_memory_id is not a related active memory",
                    validation_detail="invalid_update_target",
                )
    for field in ("aliases", "keywords"):
        if field in item:
            item[field] = _string_list(item[field], field)
    if "evidence_event_ids" in item:
        item["evidence_event_ids"] = _string_list(item["evidence_event_ids"], "evidence_event_ids", nonempty=True)
        if any(event_id.casefold() not in allowed_event_keys for event_id in item["evidence_event_ids"]):
            raise ModelOutputError("summary evidence references another turn", validation_detail="invalid_evidence")
    native_ids = {
        item.casefold()
        for item in (related_native_ids or [])
        if isinstance(item, str)
    }
    shadow_ids = item.get("shadow_native_ids", [])
    item["shadow_native_ids"] = _string_list(shadow_ids, "shadow_native_ids")
    shadow_keys = [value.casefold() for value in item["shadow_native_ids"]]
    if len(shadow_keys) != len(set(shadow_keys)):
        raise ModelOutputError("duplicate shadow native id")
    if any(value not in native_ids for value in shadow_keys):
        raise ModelOutputError("shadow native id is not a related native memory")
    if "scope_source" in item and (
        not isinstance(item["scope_source"], str) or item["scope_source"] not in SCOPE_SOURCES
    ):
        raise ModelOutputError("invalid scope_source", validation_detail="invalid_scope_source")

    if "status" in item:
        if candidate_type != "todo" or item["status"] not in TODO_STATUSES:
            raise ModelOutputError("invalid todo status", validation_detail="todo_fields")
    if "completed_at" in item:
        _string(item["completed_at"], "completed_at")
        if candidate_type != "todo" or item.get("status") != "completed":
            raise ModelOutputError("completed_at requires completed todo", validation_detail="todo_fields")
    if item.get("status") == "completed" and "completed_at" not in item:
        raise ModelOutputError("completed todo requires completed_at", validation_detail="todo_fields")
    return item


def parse_summarize_output(
    raw: str,
    current_event_keys: Iterable[Any] | None = None,
    *,
    related_native_ids: Iterable[Any] | None = None,
    related_memory_ids: Iterable[Any] | None = None,
    scope_registry: Mapping[str, Any] | None = None,
    expected_type: str | None = None,
    expected_update_memory_id: str | None = None,
    expected_target_type: str | None = None,
    expected_scopes: Iterable[Any] | None = None,
    expected_scope_source: str | None = None,
    allow_no_change: bool = False,
) -> dict[str, Any]:
    try:
        parsed = parse_strict_json(raw)
    except ModelOutputError as error:
        if error.validation_detail is None:
            error.validation_detail = "other_schema_violation"
        raise
    try:
        return validate_summarize_output(
            parsed,
            current_event_keys,
            related_native_ids=related_native_ids,
            related_memory_ids=related_memory_ids,
            scope_registry=scope_registry,
            expected_type=expected_type,
            expected_update_memory_id=expected_update_memory_id,
            expected_target_type=expected_target_type,
            expected_scopes=expected_scopes,
            expected_scope_source=expected_scope_source,
            allow_no_change=allow_no_change,
        )
    except ModelOutputError as error:
        if error.validation_reason is None:
            error.validation_reason = "schema_violation"
        if error.validation_detail is None:
            error.validation_detail = "other_schema_violation"
        raise


def validate_compact_output(
    value: Any,
    current_memory_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate one complete, side-effect-free compaction response.

    The model may only describe replacement content and which supplied active
    memories it consumes.  Provenance, timestamps, counters, and IDs are
    generated by the core after this validation succeeds.
    """

    if isinstance(value, str):
        value = parse_strict_json(value)
    if not isinstance(value, Mapping):
        raise ModelOutputError("compact output must be an object")
    _require_keys(value, {"memories"}, {"memories"})
    memories = value["memories"]
    if not isinstance(memories, list):
        raise ModelOutputError("compact memories must be a list")
    allowed_ids = {
        item
        for item in (current_memory_ids or [])
        if isinstance(item, str) and item
    }
    consumed: set[str] = set()
    normalized: list[dict[str, Any]] = []
    required = {
        "title",
        "body",
        "tags",
        "type",
        "scopes",
        "scope_source",
        "aliases",
        "keywords",
        "source_memory_ids",
    }
    for memory in memories:
        if not isinstance(memory, Mapping):
            raise ModelOutputError("each compact replacement must be an object")
        _require_keys(memory, required, set(_COMPACT_FIELDS))
        item = dict(memory)
        _string(item["title"], "compact title")
        _string(item["body"], "compact body", multiline=True)
        item["tags"] = _string_list(item["tags"], "compact tags")
        item["aliases"] = _string_list(item["aliases"], "compact aliases")
        item["keywords"] = _string_list(item["keywords"], "compact keywords")
        if item["type"] not in MEMORY_TYPES:
            raise ModelOutputError("invalid compact memory type")
        if item["scope_source"] not in SCOPE_SOURCES:
            raise ModelOutputError("invalid compact scope_source")
        item["scopes"] = _scopes(item["scopes"], item["scope_source"])
        source_ids = item["source_memory_ids"]
        if not isinstance(source_ids, list) or not source_ids:
            raise ModelOutputError("compact source_memory_ids must be non-empty")
        if not all(isinstance(source_id, str) and source_id for source_id in source_ids):
            raise ModelOutputError("compact source_memory_ids must contain strings")
        source_keys = [source_id.casefold() for source_id in source_ids]
        if len(source_keys) != len(set(source_keys)):
            raise ModelOutputError("compact source_memory_ids contain duplicates")
        if any(source_id not in allowed_ids for source_id in source_ids):
            raise ModelOutputError("compact source references a non-candidate memory")
        if consumed.intersection(source_keys):
            raise ModelOutputError("compact source memory is consumed more than once")
        consumed.update(source_keys)
        if "status" in item:
            if item["type"] != "todo" or item["status"] not in TODO_STATUSES:
                raise ModelOutputError("invalid compact todo status")
        if "completed_at" in item:
            _string(item["completed_at"], "compact completed_at")
            if item.get("type") != "todo" or item.get("status") != "completed":
                raise ModelOutputError("compact completed_at requires completed todo")
        if item.get("status") == "completed" and "completed_at" not in item:
            raise ModelOutputError("compact completed todo requires completed_at")
        normalized.append(item)
    return {"memories": normalized}


def parse_compact_output(
    raw: str,
    current_memory_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    return validate_compact_output(parse_strict_json(raw), current_memory_ids)


# Short aliases make the contract convenient without introducing a second
# implementation or any memory-writing behavior.
validate_gate = validate_gate_output
parse_gate = parse_gate_output
validate_summary_output = validate_summarize_output
parse_summary_output = parse_summarize_output


__all__ = [
    "MEMORY_TYPES",
    "MODEL_VALIDATION_DETAILS",
    "MODEL_VALIDATION_REASONS",
    "ModelOutputError",
    "NO_CHANGE_DECISION",
    "SCOPE_SOURCES",
    "TODO_STATUSES",
    "parse_gate",
    "parse_gate_output",
    "parse_compact_output",
    "parse_strict_json",
    "is_aggregate_operational_text",
    "normalize_relative_calendar_text",
    "parse_summarize_output",
    "parse_summary_output",
    "validate_gate",
    "validate_gate_output",
    "validate_compact_output",
    "validate_summarize_output",
    "validate_summary_output",
]
