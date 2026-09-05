"""Stage-B2 processing: complete inbox turns, remember, updates, and cleanup."""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from .admission import (analyze_turn_evidence, admission_reason, read_only_turn,
    evidence_prompt, parse_coverage, split_gate_envelope, supporting_units)
from .capture import _safe_turn_id
from .config import save_config
from .index import EVENT_V2_BLOCK, event_key, extract_event_keys, turn_key
from .inbox import InboxEvent, InboxTurn, parse_inbox
from .llm import (
    MODEL_ERROR_CODES,
    MODEL_VALIDATION_REASONS,
    CallableBackend,
    ModelError,
    ModelUnavailable,
    ModelRouter,
)
from .locking import atomic_write_json, atomic_write_text, read_json
from .memory_writer import MemoryWriter
from .turn_plan import TurnPlan, content_digest
from .models import Memory, utc_now
from .native_index import NativeIndexer
from .prompts import (
    DUPLICATE_TARGET_CORRECTION,
    GATE_TYPE_CORRECTION,
    JSON_CORRECTION,
    MIXED_FUTURE_USE_CORRECTION,
    MIXED_PROJECT_SCOPES_CORRECTION,
    RELATIVE_TIME_CORRECTION,
    GATE_SYSTEM,
    SCOPE_GROUNDING_CORRECTION,
    SUMMARY_SCOPE_CORRECTION,
    SUMMARY_TARGET_CORRECTION,
    SUMMARY_TYPE_CORRECTION,
    SUMMARIZE_SYSTEM,
    TARGET_RELEVANCE_CORRECTION,
    UPDATE_TARGET_TYPE_CORRECTION,
    gate_prompt,
    summarize_prompt,
)
from .redaction import redact_text
from .retrieval import candidate_matches_query, filter_by_scope, normalize_term
from .scope_state import (
    ScopeError,
    normalize_scopes,
    project_scopes_for_domains,
    register_scope_nodes,
)
from .scope_maintenance import ScopeMaintainer, ScopeMaintenanceError, scope_registry_projection
from .validation import (
    MODEL_VALIDATION_DETAILS,
    ModelOutputError,
    NO_CHANGE_DECISION,
    _model_scope_grounding_evidence,
    parse_gate_output,
    parse_strict_json,
    parse_summarize_output,
    normalize_relative_calendar_text,
    is_aggregate_operational_text,
    is_attachment_followup_only_text,
    is_actionable_todo_text,
    is_mixed_future_use_text,
    is_project_plan_text,
    split_mixed_future_use_text,
)
from .vault import safe_component


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
_DIAGNOSTIC_GATE_ALLOWED = frozenset(("candidates",))
_DIAGNOSTIC_CANDIDATE_REQUIRED = frozenset(
    ("candidate_id", "memory", "evidence_event_ids", "duplicate", "worth", "type", "scopes", "scope_source")
)

_DERIVED_OVERDUE_RE = re.compile(
    r"(?:逾期|超期|overdue)\s*\d+(?:\.\d+)?\s*(?:天|日|days?)",
    re.IGNORECASE,
)
_EXECUTION_RECEIPT_RE = re.compile(
    r"(?:核验归档|归档核验|服务器已接受(?:提交|投递)|server accepted)",
    re.IGNORECASE,
)
_TODO_COMPLETION_RE = re.compile(
    r"(?:"
    r"(?:已|已经|刚刚|刚才)\s*(?:完成|做完|办完|搞定|处理完|交付|提交|反馈|发送|提供)(?:了|啦)?"
    r"(?:版|版本|文档|材料|文件|方案|附件|答复|回复|反馈)?|"
    r"(?:完成|做完|办完|搞定|处理完)(?:了|啦)?|"
    r"(?:交付|提交|反馈|发送|提供)了(?:版|版本|文档|材料|文件|方案|附件|答复|回复|反馈)?|"
    r"(?:给|交|发)了(?:版|版本|文档|材料|文件|方案|附件|答复|回复|反馈)?|"
    r"(?:closed|completed|finished|done)"
    r")",
    re.IGNORECASE,
)
_TODO_CANCEL_RE = re.compile(
    r"(?:取消|作废|撤销|不做|不用做|不需要做|cancel(?:led|ed)?|abort(?:ed)?)",
    re.IGNORECASE,
)
_TODO_NEGATION_MARKERS = (
    "还没", "没有", "未", "尚未", "没", "not", "never", "will", "would", "准备", "打算", "计划", "将要", "会", "要",
    "需要", "需", "待", "请", "必须", "务必", "尽快", "尚需", "还需", "等待",
)
_TODO_UNCERTAINTY_MARKERS = (
    "应该", "可能", "大概", "似乎", "不确定", "也许", "好像", "probably", "perhaps", "maybe", "seems", "not sure",
)
_TODO_CONFIRMATION_RE = re.compile(
    r"(?:对吗|对不对|是吗|正确吗|是不是|是否(?=(?:已|已经|完成|正确|这样|发|做|弄)))"
)
_TODO_QUERY_SUFFIXES = ("吗", "么", "？", "?", "呢")
_REWORK_ACTION_MARKERS = (
    "要改", "需要改", "需改", "还要", "重画", "重做", "重绘", "没画好", "标出来", "标明",
    "修改", "修复", "整改", "补充", "补上", "补齐", "调整", "改下", "改一下", "改成", "更改",
    "完善", "修订", "重新", "更新", "rework", "revise", "redraw", "fix", "update", "change",
)
_SCOPE_CORRECTION_MARKER_RE = re.compile(
    r"(?:不是|并非|不属于|归错|归属错误|错误归属|应属于|应该属于|改归|改为|纠正为|"
    r"wrong\s+(?:project|scope)|belongs?\s+to|correct\s+(?:project|scope))",
    re.IGNORECASE,
)

def _completion_match_is_declarative(folded: str, match: re.Match[str]) -> bool:
    """Keep rework recovery limited to an explicit completion statement."""

    start, end = match.span()
    prefix = folded[max(0, start - 8) : start]
    suffix = folded[end : end + 4]
    if any(value in prefix for value in _TODO_NEGATION_MARKERS + _TODO_UNCERTAINTY_MARKERS):
        return False
    if any(suffix.startswith(value) for value in _TODO_QUERY_SUFFIXES) or re.match(
        r"^(?:了|啦)?(?:吗|么|呢|[?？])", suffix
    ):
        return False
    if _TODO_CONFIRMATION_RE.search(prefix) or _TODO_CONFIRMATION_RE.search(folded[end:]):
        return False
    match_text = match.group(0).casefold()
    return bool(
        any(value in prefix for value in ("已", "已经", "刚刚", "刚才", "i ", "i've", "has ", "was "))
        or match_text.startswith(("已", "已经", "刚刚", "刚才"))
        or any(value in match_text for value in ("了", "啦"))
        or suffix.startswith(("了", "啦", ".", "!", "！", ",", "，", ";", "；"))
        or match_text in {"completed", "finished", "done", "closed", "cancelled", "canceled", "aborted"}
    )


def _project_scope_occurrences(
    text: str,
    scope_registry: Mapping[str, Any] | None,
) -> list[tuple[int, int, str]] | None:
    """Return ordered, candidate-local project-name matches.

    This is intentionally conservative: only names and aliases from the
    configured registry are considered, and an overlapping occurrence that
    maps to different projects makes deterministic splitting unsafe.
    """

    if not isinstance(text, str) or not text.strip() or not isinstance(scope_registry, Mapping):
        return []
    folded = text.casefold()
    terms: list[tuple[str, str]] = []
    for scope, node in scope_registry.items():
        if not isinstance(scope, str) or not scope.startswith("project:"):
            continue
        name = scope.partition(":")[2]
        raw_terms = [name, scope]
        if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
            raw_terms.extend(item for item in node["aliases"] if isinstance(item, str))
        for raw_term in raw_terms:
            term = raw_term.casefold().strip()
            if term:
                terms.append((term, scope))
    occurrences: list[tuple[int, int, str]] = []
    for term, scope in terms:
        start = 0
        while True:
            position = folded.find(term, start)
            if position < 0:
                break
            end = position + len(term)
            ascii_term = all(char.isascii() and (char.isalnum() or char in " _-") for char in term)
            if ascii_term:
                before = folded[position - 1] if position else ""
                after = folded[end] if end < len(folded) else ""
                if (before.isascii() and before.isalnum()) or (after.isascii() and after.isalnum()):
                    start = end
                    continue
            occurrences.append((position, end, scope))
            start = end
    if not occurrences:
        return []
    occurrences.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2].casefold()))
    selected: list[tuple[int, int, str]] = []
    for occurrence in occurrences:
        if selected and occurrence[0] < selected[-1][1]:
            previous = selected[-1]
            if occurrence[0] == previous[0] and occurrence[1] == previous[1] and occurrence[2].casefold() != previous[2].casefold():
                return None
            # Longest term wins for aliases nested in a canonical name.
            continue
        selected.append(occurrence)
    return selected


def _project_fragment_is_usable(
    fragment: str,
    scope: str,
    scope_registry: Mapping[str, Any] | None,
) -> bool:
    """Require enough project-local text before splitting an aggregate."""

    if not isinstance(fragment, str):
        return False
    terms = [scope, scope.partition(":")[2]]
    node = scope_registry.get(scope) if isinstance(scope_registry, Mapping) else None
    if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
        terms.extend(item for item in node["aliases"] if isinstance(item, str))
    residual = fragment
    for term in sorted({item for item in terms if item}, key=len, reverse=True):
        residual = re.sub(re.escape(term), "", residual, flags=re.IGNORECASE)
    residual = re.sub(r"[\s\t\r\n,，;；:：、和与及以及]+", "", residual)
    # A name-only mention ("A 和 B 均有更新") is not safely separable;
    # concrete task/plan text is normally longer than this threshold.
    return len(residual) >= 3


def _project_candidate_can_split(
    text: str,
    scope_registry: Mapping[str, Any] | None,
) -> bool:
    occurrences = _project_scope_occurrences(text, scope_registry)
    if occurrences is None or len({item[2].casefold() for item in occurrences}) < 2:
        return False
    for index, occurrence in enumerate(occurrences):
        start = 0 if index == 0 else occurrence[0]
        end = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(text)
        fragment = text[start:end].strip(" \t\r\n;；,，")
        if not _project_fragment_is_usable(fragment, occurrence[2], scope_registry):
            return False
    return True


def _split_model_project_candidate(
    candidate: Mapping[str, Any],
    scope_registry: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Split a final-retry aggregate into candidate-local project memories."""

    if (
        candidate.get("worth") is not True
        or candidate.get("scope_source") not in {"model", "insufficient_context"}
        or not isinstance(candidate.get("memory"), str)
    ):
        return [dict(candidate)]
    text = candidate["memory"]
    occurrences = _project_scope_occurrences(text, scope_registry)
    if occurrences is None or len({item[2].casefold() for item in occurrences}) < 2:
        if occurrences and len(occurrences) == 1:
            # A mixed/unknown scope label can still be recovered when this
            # candidate names exactly one project itself.
            item = dict(candidate)
            item["scopes"] = [occurrences[0][2]]
            item["scope_source"] = "model"
            item.pop("duplicate_memory_id", None)
            return [item]
        return [dict(candidate)]
    if not _project_candidate_can_split(text, scope_registry):
        return [dict(candidate)]

    groups: dict[str, list[str]] = {}
    for index, occurrence in enumerate(occurrences):
        start = 0 if index == 0 else occurrence[0]
        end = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(text)
        fragment = text[start:end].strip(" \t\r\n;；,，")
        if fragment:
            groups.setdefault(occurrence[2].casefold(), []).append(fragment)
    if len(groups) < 2:
        return [dict(candidate)]

    result: list[dict[str, Any]] = []
    base_id = str(candidate.get("candidate_id", "candidate"))
    for index, (scope_key, fragments) in enumerate(groups.items(), start=1):
        item = dict(candidate)
        scope = next(occurrence[2] for occurrence in occurrences if occurrence[2].casefold() == scope_key)
        item["candidate_id"] = f"{base_id}:project-{index}-{hashlib.sha256(scope_key.encode('utf-8')).hexdigest()[:8]}"
        item["memory"] = "；".join(fragments)
        item["scopes"] = [scope]
        item["scope_source"] = "model"
        # A target selected for the aggregate cannot safely be inherited by
        # every fragment.  Candidate-level lookup below can recover a unique
        # same-use target for each fragment.
        item.pop("duplicate_memory_id", None)
        item.pop("update_memory_id", None)
        item["duplicate"] = False
        if is_actionable_todo_text(item["memory"]):
            item["type"] = "todo"
        elif is_project_plan_text(item["memory"]):
            item["type"] = "project"
        elif item.get("type") == "project":
            # Gate validation may have promoted the aggregate because one
            # sibling contained a plan marker.  Re-infer this fragment rather
            # than leaking that sibling's type into an unrelated fact.
            item["type"] = "fact"
        result.append(item)
    return result or [dict(candidate)]


def _normalize_final_gate_raw(
    raw: str,
    scope_registry: Mapping[str, Any] | None,
) -> str:
    """Make a final-retry aggregate parseable without weakening validation.

    Gate validation must reject mixed project scopes on a single candidate.
    On the final bounded retry, mark only a candidate that names multiple
    registered projects as temporarily unscoped; the caller then attempts a
    deterministic candidate-local split.  Invalid JSON and all other schema
    shapes remain untouched and therefore retain the strict retry behavior.
    """

    try:
        parsed = parse_strict_json(raw)
    except ModelOutputError:
        return raw
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("candidates"), list):
        return raw
    changed = False
    candidates: list[Any] = []
    for candidate in parsed["candidates"]:
        if not isinstance(candidate, Mapping):
            candidates.append(candidate)
            continue
        item = dict(candidate)
        if (
            item.get("scope_source") == "model"
            and item.get("worth") is True
            and isinstance(item.get("memory"), str)
        ):
            selected_scopes = {
                value.casefold()
                for value in item.get("scopes", [])
                if isinstance(value, str) and value.partition(":")[0] == "project"
            }
            occurrences = _project_scope_occurrences(item["memory"], scope_registry)
            occurrence_scopes = {
                value.casefold()
                for _start, _end, value in (occurrences or [])
            }
            if len(occurrence_scopes) > 1 and _project_candidate_can_split(
                item["memory"],
                scope_registry,
            ):
                item["scopes"] = ["unscoped"]
                item["scope_source"] = "insufficient_context"
                changed = True
        candidates.append(item)
    if not changed:
        return raw
    normalized = dict(parsed)
    normalized["candidates"] = candidates
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _explicit_todo_state_change(
    events: Iterable[Mapping[str, Any]],
) -> tuple[str, list[str], str] | None:
    """Detect one explicit user todo completion/cancellation declaration.

    This intentionally reads user events only.  A completion marker is valid
    only when it is declarative (not negated, future, or a question), so an
    assistant recap or a trailing "what remains?" cannot manufacture a state
    transition.
    """

    states: list[tuple[str, str, str]] = []
    for event in events:
        if str(event.get("role", "")).casefold() != "user":
            continue
        content = event.get("content")
        event_key_value = event.get("event_key")
        timestamp = event.get("timestamp")
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(event_key_value, str) or not event_key_value:
            continue
        if not isinstance(timestamp, str) or _parse_time(timestamp) is None:
            continue
        folded = content.casefold()
        event_states: set[str] = set()
        for marker, state in (
            (_TODO_COMPLETION_RE, "completed"),
            (_TODO_CANCEL_RE, "cancelled"),
        ):
            for match in marker.finditer(folded):
                start, end = match.span()
                prefix = folded[max(0, start - 8) : start]
                suffix = folded[end : end + 4]
                if any(value in prefix for value in _TODO_NEGATION_MARKERS + _TODO_UNCERTAINTY_MARKERS):
                    continue
                if any(suffix.startswith(value) for value in _TODO_QUERY_SUFFIXES) or re.match(
                    r"^(?:了|啦)?(?:吗|么|呢|[?？])", suffix
                ):
                    continue
                # Confirmation questions may place the question marker after
                # a comma ("已经完成了，对吗？").  Keep a follow-up about
                # other work eligible; only direct confirmation wording is
                # rejected here.
                if _TODO_CONFIRMATION_RE.search(prefix) or _TODO_CONFIRMATION_RE.search(folded[end:]):
                    continue
                match_text = match.group(0).casefold()
                # A bare English "done"/"completed" is declarative.  For
                # Chinese verbs require either a completion particle or an
                # explicit completed-state prefix to avoid matching a future
                # action such as "完成计划".
                if state in {"completed", "cancelled"} and not (
                    any(value in prefix for value in ("已", "已经", "刚刚", "刚才", "i ", "i've", "has ", "was "))
                    or match_text.startswith(("已", "已经", "刚刚", "刚才"))
                    or suffix.startswith(("了", "啦", ".", "!", "！", ",", "，", ";", "；"))
                    or any(value in match_text for value in ("了", "啦"))
                    or match_text in {"completed", "finished", "done", "closed", "cancelled", "canceled", "aborted"}
                ):
                    continue
                event_states.add(state)
                break
        states.extend((state, event_key_value, timestamp) for state in sorted(event_states))

    if not states:
        return None
    distinct_states = {state for state, _event_key_value, _timestamp in states}
    if len(distinct_states) != 1:
        return None
    state = states[0][0]
    evidence_event_ids = list(dict.fromkeys(item[1] for item in states))
    # Preserve the event timestamp's absolute instant as completed_at.  The
    # parser accepts ISO-8601 strings; canonical UTC avoids offset ambiguity.
    anchor = _parse_time(states[-1][2])
    if anchor is None:
        return None
    completed_at = anchor.strftime("%Y-%m-%dT%H:%M:%SZ")
    return state, evidence_event_ids, completed_at


def _automatic_read_only_query(events: Iterable[Mapping[str, Any]]) -> bool:
    return read_only_turn(analyze_turn_evidence(events))








def _automatic_transient_memory(value: Any) -> bool:
    """Reject volatile counters and one-off execution receipts at write time."""

    if not isinstance(value, str):
        return False
    return bool(_DERIVED_OVERDUE_RE.search(value) or _EXECUTION_RECEIPT_RE.search(value))


_CANDIDATE_LOOKUP_SPLIT = re.compile(
    r"[;；。！？!?\n]+|[,，]\s*(?=(?:后续|今后|以后|每次|所有后续|going forward|from now on))"
    r"|(?:同时|另外|此外|其次|并且|并要求|还需|另需|in addition|separately)",
    re.IGNORECASE,
)


def _candidate_lookup_queries(value: Any) -> list[str]:
    """Return bounded candidate-level lookup variants for cross-turn dedupe."""

    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    values = [text]
    values.extend(part.strip() for part in _CANDIDATE_LOOKUP_SPLIT.split(text) if part.strip())
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = normalize_term(item)
        if len(normalized) < 4 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(item)
        if len(result) >= 8:
            break
    return result


def _automatic_create_conflicts(
    candidate: Mapping[str, Any],
    summary: Mapping[str, Any],
    related: Iterable[Mapping[str, Any]],
    *,
    ignore_memory_ids: Iterable[str] = (),
) -> bool:
    """Return true only when a finalized CREATE is already covered."""

    candidate_text = str(candidate.get("memory", "")).strip()
    queries = _candidate_lookup_queries(candidate_text)
    summary_body = normalize_term(str(summary.get("body", "")))
    candidate_type = candidate.get("type")
    ignored = {
        value.casefold() for value in ignore_memory_ids
        if isinstance(value, str) and value
    }
    for raw in related:
        if not isinstance(raw, Mapping):
            continue
        memory_id = raw.get("memory_id")
        if isinstance(memory_id, str) and memory_id.casefold() in ignored:
            # Requests already generated for this visible turn may have been
            # partially persisted by a failed transaction. Ignore them here
            # so replay can finish the remaining deterministic candidates.
            continue
        related_body = normalize_term(str(raw.get("body", "")))
        if raw.get("native") is True:
            related_text = normalize_term(
                f"{raw.get('title', '')}\n{raw.get('body', '')}"
            )
            for query in queries:
                normalized = normalize_term(query)
                if len(normalized) >= 8 and related_text and (
                    normalized in related_text
                    or (len(related_text) >= 8 and related_text in normalized)
                ):
                    return True
            continue
        if raw.get("type") != candidate_type:
            continue
        if summary_body and related_body == summary_body:
            return True
    return False


def _todo_state_recovery_candidate(
    events: Iterable[Mapping[str, Any]],
    related: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], str, str, str] | None:
    """Recover a missed todo transition from one explicit user declaration.

    This is deliberately narrower than general admission: it only considers
    active todo records already returned by the bounded related-memory query,
    and proceeds only when exactly one record is lexically relevant to the
    user's own completion/cancellation statement.
    """

    state_change = _explicit_todo_state_change(events)
    if state_change is None:
        return None
    state, evidence_event_ids, completed_at = state_change
    user_text = "\n".join(
        str(event.get("content", ""))
        for event in events
        if str(event.get("role", "")).casefold() == "user"
    ).strip()
    matches: list[Memory] = []
    seen_ids: set[str] = set()
    for item in related:
        if not isinstance(item, Mapping) or item.get("native") is True:
            continue
        if item.get("type") != "todo" or item.get("status") in {"completed", "cancelled"}:
            continue
        memory_id = item.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id or memory_id.casefold() in seen_ids:
            continue
        try:
            memory = Memory.from_mapping(item)
        except (TypeError, ValueError):
            continue
        if not candidate_matches_query(memory, user_text):
            continue
        seen_ids.add(memory_id.casefold())
        matches.append(memory)
    if len(matches) != 1:
        return None
    target = matches[0]
    candidate_id_material = "|".join(
        [target.memory_id, state, *sorted(item.casefold() for item in evidence_event_ids)]
    )
    candidate_id = "todo-state-" + hashlib.sha256(candidate_id_material.encode("utf-8")).hexdigest()[:16]
    scopes = list(target.scopes) or ["global"]
    scope_source = target.scope_source
    if scope_source not in {"model", "user", "session_context", "insufficient_context"}:
        scope_source = "model"
    # Carry the actual declaration through the common admission boundary.
    # A synthesized status label loses support for elliptical declarations.
    declaration = user_text
    for event in events:
        if event.get("event_key") not in evidence_event_ids:
            continue
        for unit in analyze_turn_evidence([event]):
            if unit.origin != "user_assertion":
                continue
            local_change = _explicit_todo_state_change([dict(event, content=unit.text)])
            if local_change is not None and local_change[0] == state:
                declaration = unit.text
                break
        else:
            continue
        break
    candidate = {
        "candidate_id": candidate_id,
        "memory": f"{target.title} 用户状态声明：{declaration}",
        "evidence_event_ids": evidence_event_ids,
        "duplicate": False,
        "worth": True,
        "type": "todo",
        "scopes": scopes,
        "scope_source": scope_source,
        "update_memory_id": target.memory_id,
    }
    return candidate, state, target.memory_id, completed_at


def _completion_rework_candidate(
    events: Iterable[Mapping[str, Any]],
    scope_registry: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract new work that follows an explicit completed-delivery statement.

    A single user message may close yesterday's todo and immediately report a
    new customer revision.  The model often collapses those into one
    candidate; keeping the deterministic extraction here prevents the close
    transition from swallowing the new action.
    """

    for event in events:
        if str(event.get("role", "")).casefold() != "user":
            continue
        content = event.get("content")
        event_key_value = event.get("event_key")
        if not isinstance(content, str) or not content.strip() or not isinstance(event_key_value, str):
            continue
        folded_content = content.casefold()
        matches = list(_TODO_COMPLETION_RE.finditer(folded_content))
        if not matches:
            continue
        for match in matches:
            if not _completion_match_is_declarative(folded_content, match):
                continue
            tail = content[match.end() :].strip(" \t\r\n，,；;。.!！?？:：")
            tail = re.sub(r"^[了啦]\s*[，,；;。.!！?？:：]?\s*", "", tail)
            tail = re.sub(
                r"^(?:了)?[，,；;]?\s*(?:他们|客户|对方)(?:上午|下午|今天|刚刚)?(?:回复|反馈|说)(?:是|有|称|说)?[：:，,；;]?",
                "",
                tail,
                flags=re.IGNORECASE,
            ).strip(" \t\r\n，,；;。.!！?？:：")
            folded = tail.casefold()
            if not tail or not any(marker in folded for marker in _REWORK_ACTION_MARKERS):
                continue
            occurrences = _project_scope_occurrences(content, scope_registry)
            if occurrences is None:
                continue
            scope_values = list(dict.fromkeys(item[2] for item in occurrences))
            if len(scope_values) != 1:
                continue
            scope = scope_values[0]
            scope_name = scope.partition(":")[2]
            if scope_name.casefold() not in tail.casefold():
                tail = f"{scope_name}：{tail}"
            candidate_id = "todo-rework-" + hashlib.sha256(
                f"{event_key_value}|{tail}".encode("utf-8")
            ).hexdigest()[:16]
            return {
                "candidate_id": candidate_id,
                "memory": tail,
                "evidence_event_ids": [event_key_value],
                "duplicate": False,
                "worth": True,
                "type": "todo",
                "scopes": [scope],
                "scope_source": "model",
                "_force_create": True,
            }
    return None


_DIAGNOSTIC_CANDIDATE_ALLOWED = _DIAGNOSTIC_CANDIDATE_REQUIRED | frozenset(
    ("reason", "duplicate_memory_id", "update_memory_id")
)
_DIAGNOSTIC_SUMMARY_REQUIRED = frozenset(("title", "body", "tags", "type", "scopes", "sources"))
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
        "completed_at",
        "due_date",
        "shadow_native_ids",
        "scope_operations",
    )
)
# Automatic model calls need more context than the host's Scope Map, but they
# still receive a bounded slice.  These limits are intentionally local to the
# processing prompts; they do not change the host's directory-only budget.
_RELATED_MAX_ITEMS = 6
_RELATED_MAX_BODY_CHARS = 1600
_RELATED_MAX_CHARS = 6000
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
    if stage not in {"gate", "summarize"}:
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
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3):
        attempt_count = None
    return code, stage, validation_reason, validation_detail, attempt_count


def _json_top_level_type(value: Any) -> str:
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    return "unknown"


def _model_output_statistics(raw: Any, purpose: str) -> dict[str, Any]:
    """Return structural-only model output statistics; never retain model text."""

    stats: dict[str, Any] = {
        "output_chars": len(raw) if isinstance(raw, str) else 0,
        "output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest() if isinstance(raw, str) else "",
        "top_level_type": "non_text" if not isinstance(raw, str) else "invalid",
        "candidate_count": 0,
        "missing_fields_count": 0,
        "unknown_fields_count": 0,
    }
    if not isinstance(raw, str):
        return stats
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return stats
    stats["top_level_type"] = _json_top_level_type(parsed)
    if not isinstance(parsed, Mapping):
        return stats
    if purpose == "gate":
        required = _DIAGNOSTIC_GATE_REQUIRED
        allowed = _DIAGNOSTIC_GATE_ALLOWED
    elif purpose == "summarize":
        required = _DIAGNOSTIC_SUMMARY_REQUIRED
        allowed = _DIAGNOSTIC_SUMMARY_ALLOWED
    else:
        return stats
    stats["missing_fields_count"] = len(required - set(parsed))
    stats["unknown_fields_count"] = len(set(parsed) - allowed)
    if purpose != "gate":
        return stats
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list):
        return stats
    stats["candidate_count"] = len(candidates)
    missing_count = stats["missing_fields_count"]
    unknown_count = stats["unknown_fields_count"]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        missing_count += len(_DIAGNOSTIC_CANDIDATE_REQUIRED - set(candidate))
        unknown_count += len(set(candidate) - _DIAGNOSTIC_CANDIDATE_ALLOWED)
    stats["missing_fields_count"] = missing_count
    stats["unknown_fields_count"] = unknown_count
    return stats


class _Snapshot:
    def __init__(self, turn: InboxTurn, token: str, state_key: str):
        self.turn = turn
        self.token = token
        self.state_key = state_key


def _empty_processed() -> dict[str, Any]:
    return {"version": 1, "event_keys": [], "events": {}, "sessions": {}}


def _read_processed(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.exists():
        return _empty_processed()
    try:
        value = read_json(path)
    except (OSError, UnicodeError, TypeError, ValueError):
        return _empty_processed()
    if not isinstance(value, dict):
        return _empty_processed()
    result = dict(value)
    result.setdefault("version", 1)
    result.setdefault("event_keys", [])
    result.setdefault("events", {})
    result.setdefault("sessions", {})
    if not isinstance(result.get("sessions"), dict):
        result["sessions"] = {}
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
            "timestamp": event.timestamp,
            "content": event.content,
            "tool_evidence": [dict(item) for item in event.tool_evidence],
        }
        for event in turn.events
    ]


def _summary_evidence_keys(summary: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    """Collect only current-turn evidence IDs that the summary explicitly names."""

    values: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str) or not value:
            return
        key = value.casefold()
        if key not in values:
            values.append(key)

    evidence = summary.get("evidence_event_ids")
    if isinstance(evidence, list):
        for value in evidence:
            add(value)
    sources = summary.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            add(source.get("event_key"))
            source_evidence = source.get("evidence_event_ids")
            if isinstance(source_evidence, list):
                for value in source_evidence:
                    add(value)
    if values:
        return values
    candidate_evidence = candidate.get("evidence_event_ids")
    if isinstance(candidate_evidence, list):
        for value in candidate_evidence:
            add(value)
    return values


def _summary_date_anchor(
    turn: InboxTurn,
    summary: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Optional[datetime]:
    """Return one unambiguous current-evidence timestamp for date rewriting."""

    event_timestamps = {
        event.event_key.casefold(): _parse_time(event.timestamp)
        for event in turn.events
        if isinstance(event.event_key, str)
    }
    evidence_keys = _summary_evidence_keys(summary, candidate)
    if not evidence_keys:
        return None
    timestamps: list[datetime] = []
    for evidence_key in evidence_keys:
        timestamp = event_timestamps.get(evidence_key)
        if timestamp is None:
            # An omitted, malformed, or foreign evidence timestamp is not a
            # safe anchor.  The strict parser will reject unresolved dates and
            # the caller can defer this candidate without advancing silently.
            return None
        timestamps.append(timestamp)
    calendar_dates = {timestamp.date() for timestamp in timestamps}
    if len(calendar_dates) != 1:
        # A summary can cite multiple events from different UTC dates, but a
        # single unlabelled relative phrase cannot be assigned safely to one
        # of them.
        return None
    return timestamps[0]


def _normalize_summary_dates(
    raw: Any,
    turn: InboxTurn,
    candidate: Mapping[str, Any],
) -> Any:
    """Normalize summary dates before strict validation when evidence permits."""

    if not isinstance(raw, str):
        return raw
    try:
        parsed = parse_strict_json(raw)
    except ModelOutputError:
        # Let the regular summary parser preserve its invalid-JSON metadata.
        return raw
    if not isinstance(parsed, Mapping):
        return raw
    anchor = _summary_date_anchor(turn, parsed, candidate)
    if anchor is None:
        return raw
    normalized = dict(parsed)
    changed = False
    for field in ("title", "body", "due_date"):
        value = normalized.get(field)
        if not isinstance(value, str):
            continue
        rewritten = normalize_relative_calendar_text(value, anchor)
        if rewritten is not None:
            normalized[field] = rewritten
            changed = changed or rewritten != value
    if not changed:
        return raw
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))



def _grounded_due_dates(turn: InboxTurn) -> set[str]:
    """Return absolute dates actually supported by current visible evidence."""

    result: set[str] = set()
    for event in turn.events:
        timestamp = _parse_time(event.timestamp)
        if timestamp is None or not isinstance(event.content, str):
            continue
        normalized = normalize_relative_calendar_text(event.content, timestamp) or event.content
        for value in re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", normalized):
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                continue
            result.add(parsed.isoformat())
        for year, month, day in re.findall(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?", event.content):
            try:
                parsed = datetime(
                    int(year) if year else timestamp.year,
                    int(month),
                    int(day),
                    tzinfo=timezone.utc,
                ).date()
            except ValueError:
                continue
            result.add(parsed.isoformat())
    return result


def _native_result(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        return []
    result: list[dict[str, Any]] = []
    allowed_fields = {
        "memory_id",
        "native_id",
        "native",
        "source",
        "native_source_id",
        "agent",
        "native_agent",
        "locator",
        "share",
        "title",
        "body",
        "tags",
        "type",
        "scopes",
        "aliases",
        "keywords",
        "status",
        "completed_at",
    }
    for item in values:
        if isinstance(item, Memory):
            value = item.to_dict()
            result.append({key: value[key] for key in allowed_fields if key in value})
        elif isinstance(item, Mapping):
            result.append({key: item[key] for key in allowed_fields if key in item})
        else:
            result.append({"body": str(item)})
    return result


def _merge_related(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_bodies: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        body = item.get("body")
        if not isinstance(body, str):
            continue
        normalized = normalize_term(body)
        if normalized and normalized in seen_bodies:
            continue
        if normalized:
            seen_bodies.add(normalized)
        result.append(dict(item))
    return result


def _invoke_native(reader: Any, query: str, scope: Any) -> list[dict[str, Any]]:
    if reader is None:
        return []
    callback = reader.search if hasattr(reader, "search") and callable(reader.search) else reader
    if not callable(callback):
        raise TypeError("native_memory_reader must be callable or expose search()")
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        value = callback(query)
    else:
        try:
            signature.bind(query, scope=scope)
        except TypeError:
            try:
                signature.bind(query)
            except TypeError as error:
                raise TypeError("native_memory_reader must accept query") from error
            value = callback(query)
        else:
            value = callback(query, scope=scope)
    return _native_result(value)


class Processor:
    def __init__(self, service: Any):
        self.service = service
        self.writer = MemoryWriter(service)
        # Candidate-level decisions are collected before the commit phase.
        # This small overlay lets a later pending turn in the same
        # process call see a memory planned by an earlier turn without
        # exposing an uncommitted body outside this processor.
        self._planned_related: list[dict[str, Any]] = []
        self._deferred_by_turn: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._dispositions_by_turn: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._evidence_by_turn: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def _record_disposition(
        self,
        turn_ref: tuple[str, str, str],
        candidate: Mapping[str, Any],
        disposition: str,
        *,
        reason: str | None = None,
        memory_id: str | None = None,
    ) -> None:
        """Record a compact, candidate-level processing outcome.

        The ledger deliberately keeps only the stable candidate id and the
        outcome metadata.  Candidate text belongs in the inbox/model
        diagnostics, not in the durable processing index.
        """

        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            return
        if disposition not in {"CREATE", "UPDATE", "NO_CHANGE", "DEFERRED"}:
            return
        value: dict[str, Any] = {
            "candidate_id": candidate_id,
            "disposition": disposition,
        }
        if candidate.get("evidence_unit_ids"):
            value["evidence_unit_ids"] = list(candidate["evidence_unit_ids"])
        if isinstance(reason, str) and reason:
            value["reason"] = reason
        if isinstance(memory_id, str) and memory_id:
            value["memory_id"] = memory_id
        values = self._dispositions_by_turn.setdefault(turn_ref, [])
        candidate_key = candidate_id.casefold()
        for index, previous in enumerate(values):
            if (
                isinstance(previous, Mapping)
                and isinstance(previous.get("candidate_id"), str)
                and previous["candidate_id"].casefold() == candidate_key
            ):
                values[index] = value
                return
        values.append(value)

    def _record_request_disposition(
        self,
        request: Mapping[str, Any],
        disposition: str,
        *,
        reason: str | None = None,
        memory_id: str | None = None,
    ) -> None:
        turn = request.get("turn")
        if not isinstance(turn, InboxTurn):
            return
        self._record_disposition(
            (turn.source, turn.session_id, turn.turn_key),
            request,
            disposition,
            reason=reason,
            memory_id=memory_id,
        )

    def _candidate_dispositions(
        self,
        snapshots: Iterable[_Snapshot] = (),
    ) -> list[dict[str, Any]]:
        """Return compact outcomes in the same order as processed turns."""

        result: list[dict[str, Any]] = []
        for snapshot in snapshots:
            turn = snapshot.turn
            turn_ref = (turn.source, turn.session_id, turn.turn_key)
            result.extend(
                dict(item)
                for item in self._dispositions_by_turn.get(turn_ref, [])
                if isinstance(item, Mapping)
            )
        return result

    def _resolve_backend(self, model: Any = None, router: Any = None) -> Any:
        backend = router if router is not None else model
        if backend is None:
            backend = getattr(self.service, "router", None)
        if backend is None:
            backend = ModelRouter.from_config(self.service.vault.config())
            self.service.router = backend
        if callable(backend) and not hasattr(backend, "complete"):
            backend = CallableBackend(backend)
        if not hasattr(backend, "complete"):
            raise ModelUnavailable("no model backend is configured")
        return backend

    def _auto_compact(self, *, model: Any = None, router: Any = None) -> dict[str, Any]:
        from .compaction import Compactor

        return Compactor(self.service).auto(model=model, router=router)

    def _complete(self, backend: Any, prompt: str, *, system: str, purpose: str) -> str:
        try:
            value = backend.complete(prompt, system=system, purpose=purpose, temperature=0.0)
        except ModelError as error:
            error.with_stage(purpose)
            raise
        except Exception as error:
            raise ModelError("model backend failed", stage=purpose) from error
        if not isinstance(value, str):
            raise ModelError(
                "model backend returned non-text output",
                code="model_invalid_response",
                stage=purpose,
                validation_reason="response_shape",
            )
        return value

    @staticmethod
    def _set_stage_diagnostics(error: BaseException, *, purpose: str, attempt_count: int) -> None:
        if isinstance(error, ModelError):
            error.with_stage(purpose)
            if error.code == "model_invalid_response" and (
                not isinstance(getattr(error, "validation_reason", None), str)
                or error.validation_reason not in MODEL_VALIDATION_REASONS
            ):
                error.validation_reason = "response_shape"
        elif isinstance(error, ModelOutputError):
            error.stage = purpose
            if (
                not isinstance(getattr(error, "validation_reason", None), str)
                or error.validation_reason not in MODEL_VALIDATION_REASONS
            ):
                error.validation_reason = "schema_violation"
        error.attempt_count = attempt_count

    @staticmethod
    def _retryable_json_error(error: BaseException) -> bool:
        return isinstance(error, ModelOutputError) or (
            isinstance(error, ModelError) and error.code == "model_invalid_response"
        )

    @staticmethod
    def _allows_next_json_attempt(error: BaseException, attempt_count: int) -> bool:
        # Invalid extraction output is safe to retry with the bounded
        # correction prompt.  Schema/shape violations are no less likely to
        # be transient than an empty response; allowing the same final
        # attempt prevents a single malformed JSON object from failing an
        # otherwise recoverable automatic process.  The caller still stops
        # after attempt three and preserves the final diagnostics.
        return Processor._retryable_json_error(error) and attempt_count < 3

    @staticmethod
    def _safe_correction_hint(error: BaseException) -> Optional[str]:
        detail = getattr(error, "validation_detail", None)
        if isinstance(detail, str) and detail in MODEL_VALIDATION_DETAILS:
            return detail
        reason = getattr(error, "validation_reason", None)
        if isinstance(error, ModelError) and error.code == "model_invalid_response":
            if isinstance(reason, str) and reason in MODEL_VALIDATION_REASONS:
                return reason
        return None

    @staticmethod
    def _correction_instruction(error: BaseException) -> Optional[str]:
        hint = Processor._safe_correction_hint(error)
        stage = getattr(error, "stage", None)
        if hint == "duplicate_update_target":
            return DUPLICATE_TARGET_CORRECTION
        if hint == "mixed_project_scopes":
            return MIXED_PROJECT_SCOPES_CORRECTION
        if hint == "mixed_future_use":
            return MIXED_FUTURE_USE_CORRECTION
        if stage == "gate" and hint == "update_target_type_mismatch":
            return UPDATE_TARGET_TYPE_CORRECTION
        if stage == "gate" and hint == "invalid_type":
            return GATE_TYPE_CORRECTION
        if stage == "gate" and hint == "scope_not_grounded":
            return SCOPE_GROUNDING_CORRECTION
        if stage == "gate" and hint == "target_not_relevant":
            return TARGET_RELEVANCE_CORRECTION
        if stage == "summarize" and hint == "scope_drift":
            return SUMMARY_SCOPE_CORRECTION
        if hint == "relative_time":
            return RELATIVE_TIME_CORRECTION
        if stage == "summarize" and hint == "invalid_update_target":
            return SUMMARY_TARGET_CORRECTION
        if stage == "summarize" and hint == "invalid_type":
            return SUMMARY_TYPE_CORRECTION
        if hint is not None:
            return f"Previous output violated: {hint}."
        return None

    def _diagnostic_enabled(self) -> bool:
        try:
            config = self.service.vault.config()
            llm = config.get("llm") if isinstance(config, Mapping) else None
            return isinstance(llm, Mapping) and type(llm.get("diagnostic_logging", False)) is bool and llm.get(
                "diagnostic_logging", False
            )
        except Exception:
            return False

    def _write_model_diagnostic(
        self,
        *,
        purpose: str,
        attempt_count: int,
        context: Mapping[str, Any] | None,
        raw: Any,
        error: BaseException | None,
    ) -> None:
        """Best-effort bounded JSONL diagnostics; never changes model outcome."""

        if not self._diagnostic_enabled():
            return
        context = context if isinstance(context, Mapping) else {}
        source = context.get("source", "")
        session_id = context.get("session_id", "")
        turn_index = context.get("turn_index")
        if not isinstance(source, str):
            source = ""
        if not isinstance(session_id, str):
            session_id = ""
        if isinstance(turn_index, bool) or not isinstance(turn_index, int):
            turn_index = None
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3):
            attempt_count = None
        failure_code = ""
        validation_reason = ""
        validation_detail = ""
        if error is not None:
            failure_code, _failure_stage, reason, detail, _attempt = _failure_metadata(error)
            validation_reason = reason or ""
            validation_detail = detail or ""
        entry = {
            "timestamp": utc_now(),
            "source": source,
            "session_id": session_id,
            "turn_index": turn_index,
            "stage": purpose,
            "attempt_count": attempt_count,
            "failure_code": failure_code,
            "validation_reason": validation_reason,
            "validation_detail": validation_detail,
            **_model_output_statistics(raw, purpose),
        }
        response_diagnostics = getattr(error, "response_diagnostics", None) if error is not None else None
        if isinstance(response_diagnostics, Mapping):
            allowed_diagnostics = {
                "finish_reason",
                "completion_tokens",
                "content_present",
                "content_chars",
                "reasoning_present",
                "reasoning_chars",
            }
            for key in allowed_diagnostics:
                value = response_diagnostics.get(key)
                if key == "finish_reason":
                    if isinstance(value, str) and value in {
                        "stop",
                        "length",
                        "tool_calls",
                        "function_call",
                        "content_filter",
                        "insufficient_system_resource",
                        "unknown",
                    }:
                        entry[key] = value
                elif key == "completion_tokens":
                    if value is None or (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and 0 <= value <= 1_000_000
                    ):
                        entry[key] = value
                elif key in {"content_present", "reasoning_present"}:
                    if isinstance(value, bool):
                        entry[key] = value
                elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                    entry[key] = value
        try:
            payload = (
                json.dumps(entry, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            if len(payload) > _DIAGNOSTIC_MAX_BYTES:
                return
            path = self.service.vault.logs_path / _DIAGNOSTIC_FILENAME
            rotated = path.with_name(f"{path.name}.1")
            with self.service.vault.lock():
                logs_path = self.service.vault.logs_path
                if logs_path.exists() and (logs_path.is_symlink() or not logs_path.is_dir()):
                    raise OSError("unsafe diagnostics directory")
                logs_path.mkdir(parents=True, exist_ok=True)
                os.chmod(logs_path, 0o700)
                if path.is_symlink():
                    raise OSError("unsafe diagnostics file")
                if rotated.is_symlink():
                    raise OSError("unsafe diagnostics rotation file")
                current_size = path.stat().st_size if path.exists() else 0
                if current_size + len(payload) > _DIAGNOSTIC_MAX_BYTES:
                    if rotated.exists():
                        rotated.unlink()
                    if path.exists():
                        os.replace(path, rotated)
                    current_size = 0
                with path.open("ab") as stream:
                    os.chmod(path, 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
        except Exception:
            return

    def _complete_json_stage(
        self,
        backend: Any,
        prompt: str,
        *,
        system: str,
        purpose: str,
        parser: Callable[[str], Any],
        diagnostic_context: Mapping[str, Any] | None = None,
    ) -> Any:
        correction_prompt = prompt + "\n\n" + JSON_CORRECTION
        for attempt_count in (1, 2, 3):
            raw: Any = None
            try:
                raw = self._complete(
                    backend,
                    prompt if attempt_count == 1 else correction_prompt,
                    system=system,
                    purpose=purpose,
                )
                parsed = parser(raw)
            except (ModelError, ModelOutputError) as error:
                self._set_stage_diagnostics(error, purpose=purpose, attempt_count=attempt_count)
                try:
                    self._write_model_diagnostic(
                        purpose=purpose,
                        attempt_count=attempt_count,
                        context=diagnostic_context,
                        raw=raw,
                        error=error,
                    )
                except Exception:
                    pass
                if self._allows_next_json_attempt(error, attempt_count):
                    correction_prompt = prompt + "\n\n" + JSON_CORRECTION
                    instruction = self._correction_instruction(error)
                    if instruction is not None:
                        correction_prompt += f"\n{instruction}"
                    continue
                raise
            try:
                self._write_model_diagnostic(
                    purpose=purpose,
                    attempt_count=attempt_count,
                    context=diagnostic_context,
                    raw=raw,
                    error=None,
                )
            except Exception:
                pass
            return parsed
        raise AssertionError("unreachable JSON stage retry")

    def _write_processed_unlocked(self, processed: Mapping[str, Any]) -> None:
        atomic_write_json(self.service.vault.processed_index_path, dict(processed))

    def _cleanup_hours(self) -> int:
        try:
            config = self.service.vault.config()
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise ProcessingError("cannot read process.inbox_cleanup_hours") from error
        process = config.get("process") if isinstance(config, Mapping) else None
        hours = process.get("inbox_cleanup_hours") if isinstance(process, Mapping) else None
        if type(hours) is not int or hours < 0:
            raise ProcessingError("invalid process.inbox_cleanup_hours")
        return hours

    @staticmethod
    def _owner_pid_status(owner_pid: Any) -> Optional[bool]:
        """Return whether a POSIX processing owner is alive.

        ``None`` means that the marker does not contain a usable PID or that
        the platform cannot answer the question.  In that case callers use
        the short legacy grace period rather than assuming ownership is gone.
        """

        if os.name != "posix" or isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
            return None
        if owner_pid <= 0:
            return False
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The process exists but is owned by another user.
            return True
        except OSError as error:
            if error.errno == errno.ESRCH:
                return False
            if error.errno == errno.EPERM:
                return True
            return None
        return True

    @classmethod
    def _processing_marker_live(cls, marker: Any, now: str) -> bool:
        if not isinstance(marker, Mapping) or marker.get("status") != _PROCESSING_STATUS:
            return False
        started = _parse_time(marker.get("started_at"))
        current = _parse_time(now)
        if started is None or current is None:
            return False
        owner_status = cls._owner_pid_status(marker.get("owner_pid"))
        if owner_status is False:
            # A killed MCP worker cannot keep a processing claim alive.  This
            # is what makes a timed-out provider call immediately retryable.
            return False
        lease = _PROCESSING_LEASE_SECONDS if owner_status is True else _LEGACY_PROCESSING_GRACE_SECONDS
        return (current - started).total_seconds() <= lease

    def _session_path_without_create(self, state_key: str) -> Optional[Path]:
        try:
            source, session_id = state_key.split("/", 1)
            safe_component(source, "source")
            safe_component(session_id, "session id")
        except (ValueError, AttributeError):
            return None
        path = self.service.vault.inbox_path / source / f"{session_id}.md"
        return path

    def _remove_turn_blocks(self, path: Path, entry: Mapping[str, Any]) -> bool:
        if not path.exists():
            return False
        if path.is_symlink():
            raise ProcessingError("unsafe inbox session path")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProcessingError("cannot read inbox session") from error
        target_keys = {
            item.casefold()
            for item in entry.get("event_keys", [])
            if isinstance(item, str)
        }
        target_turn_key = entry.get("turn_key")
        target_index = entry.get("turn_index")
        removed_keys: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            try:
                metadata = json.loads(match.group("meta"))
            except (TypeError, ValueError):
                return match.group(0)
            if not isinstance(metadata, Mapping):
                return match.group(0)
            key = metadata.get("event_key")
            matches = isinstance(key, str) and key.casefold() in target_keys
            matches = matches or (
                isinstance(target_turn_key, str)
                and metadata.get("turn_key") == target_turn_key
                and metadata.get("turn_index") == target_index
            )
            if matches:
                if isinstance(key, str):
                    removed_keys.add(key.casefold())
                return ""
            return match.group(0)

        updated = EVENT_V2_BLOCK.sub(replace, text)
        keys_for_legacy = removed_keys or target_keys
        for key in keys_for_legacy:
            updated = re.sub(
                rf"(?m)^<!--\s*memleaf:event-key:v1:{re.escape(key)}\s*-->[ \t]*(?:\r?\n|$)",
                "",
                updated,
            )
        if updated == text:
            return False
        if not extract_event_keys(updated):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_text(path, updated)
        return True

    def _cleanup_due_unlocked(self, processed: dict[str, Any], now: str, cleanup_hours: int) -> int:
        # ``cleanup_hours`` is read and validated by the caller even though
        # eligibility timestamps are written at successful commit time.  It
        # is accepted here to keep the lock-held cleanup boundary explicit.
        del cleanup_hours
        now_time = _parse_time(now)
        if now_time is None:
            return 0
        sessions = processed.get("sessions")
        if not isinstance(sessions, dict):
            return 0
        due_entries: list[tuple[str, int, dict[str, Any]]] = []
        for state_key, state_value in sessions.items():
            if not isinstance(state_key, str) or not isinstance(state_value, dict):
                continue
            if self._processing_marker_live(state_value.get("processing"), now):
                continue
            entries = state_value.get("processed_turns")
            if not isinstance(entries, list):
                continue
            path = self._session_path_without_create(state_key)
            for entry_index, raw_entry in enumerate(entries):
                if not isinstance(raw_entry, dict) or raw_entry.get("cleanup_done_at"):
                    continue
                due = _parse_time(raw_entry.get("eligible_cleanup_at"))
                if due is None or due > now_time:
                    continue
                if path is not None:
                    self._remove_turn_blocks(path, raw_entry)
                due_entries.append((state_key, entry_index, raw_entry))
        if not due_entries:
            return 0

        updated = deepcopy(processed)
        updated_sessions = updated.setdefault("sessions", {})
        for state_key, entry_index, _ in due_entries:
            state = updated_sessions.get(state_key)
            if not isinstance(state, dict):
                raise ProcessingError("cleanup session disappeared")
            entries = state.get("processed_turns")
            if not isinstance(entries, list) or entry_index >= len(entries):
                raise ProcessingError("cleanup entry disappeared")
            entry = entries[entry_index]
            if not isinstance(entry, dict):
                raise ProcessingError("cleanup entry is invalid")
            entry["cleanup_done_at"] = now

        # Inbox blocks are intentionally removed before the ledger is marked,
        # but the ledger is only committed after the derived index succeeds.
        # If any later write fails, the entry remains eligible and a retry can
        # safely observe the already-absent target block.
        try:
            self._write_processed_unlocked(updated)
            self.service._rebuild_index_unlocked()
        except Exception:
            try:
                self._write_processed_unlocked(processed)
            except Exception:
                pass
            raise
        processed.clear()
        processed.update(updated)
        return len(due_entries)

    def _turns_by_session(self) -> dict[str, list[InboxTurn]]:
        grouped: dict[str, list[InboxTurn]] = {}
        for turn in parse_inbox(self.service.vault):
            if not isinstance(turn.source, str) or not isinstance(turn.session_id, str):
                continue
            grouped.setdefault(_session_key(turn.source, turn.session_id), []).append(turn)
        for values in grouped.values():
            values.sort(key=lambda turn: (turn.turn_index or 0, turn.turn_key or ""))
        return grouped

    def _snapshot(
        self,
        *,
        source: str | None,
        session_id: str | None,
        now: str,
        cleanup_hours: int,
        scope: Any = None,
    ) -> tuple[list[_Snapshot], int]:
        with self.service.vault.lock():
            self.service._recover_compaction_unlocked()
            processed = _read_processed(self.service.vault.processed_index_path)
            cleaned = self._cleanup_due_unlocked(processed, now, cleanup_hours)
            grouped = self._turns_by_session()
            sessions = processed.setdefault("sessions", {})
            snapshots: list[_Snapshot] = []
            for state_key, turns in grouped.items():
                if source is not None and not state_key.startswith(f"{source}/"):
                    continue
                if session_id is not None and state_key != _session_key(source or state_key.split("/", 1)[0], session_id):
                    continue
                state = sessions.get(state_key)
                if not isinstance(state, dict):
                    state = {}
                processing = state.get("processing")
                if self._processing_marker_live(processing, now):
                    continue
                processed_entries = state.get("processed_turns", [])
                if not isinstance(processed_entries, list):
                    processed_entries = []
                processed_keys = {
                    entry.get("turn_key")
                    for entry in processed_entries
                    if isinstance(entry, Mapping) and isinstance(entry.get("turn_key"), str)
                }
                processed_indices = {
                    _as_int(entry.get("turn_index"), -1)
                    for entry in processed_entries
                    if isinstance(entry, Mapping)
                }
                watermark = max(
                    _as_int(state.get("watermark"), 0),
                    _as_int(state.get("processed_watermark"), 0),
                    *(index for index in processed_indices if index > 0),
                )
                by_index = {
                    turn.turn_index: turn
                    for turn in turns
                    if isinstance(turn.turn_index, int) and turn.turn_index > 0
                }
                next_index = watermark + 1
                selected: list[InboxTurn] = []
                # A deferred turn is already reflected in the watermark so
                # that later turns are not blocked.  An explicit scope on a
                # subsequent process call is the small, deterministic retry
                # signal; select those turns before ordinary pending work.
                can_retry_deferred = scope is not None and scope not in ("", [])
                if can_retry_deferred:
                    deferred = [
                        entry
                        for entry in processed_entries
                        if isinstance(entry, Mapping)
                        and isinstance(entry.get("deferred_candidates"), list)
                        and entry.get("deferred_candidates")
                    ]
                    deferred.sort(key=lambda entry: _as_int(entry.get("turn_index"), 0))
                    for entry in deferred:
                        turn_key_value = entry.get("turn_key")
                        turn = next(
                            (item for item in turns if item.turn_key == turn_key_value and item.complete),
                            None,
                        )
                        if turn is not None:
                            selected.append(turn)
                if not selected:
                    while next_index in by_index:
                        turn = by_index[next_index]
                        if not turn.complete:
                            break
                        if turn.turn_key in processed_keys or next_index in processed_indices:
                            next_index += 1
                            continue
                        selected.append(turn)
                        next_index += 1
                if not selected:
                    continue
                token = uuid.uuid4().hex
                state["processing"] = {
                    "status": _PROCESSING_STATUS,
                    "token": token,
                    "owner_pid": os.getpid(),
                    "turn_keys": [turn.turn_key for turn in selected],
                    "turn_indices": [turn.turn_index for turn in selected],
                    "started_at": now,
                }
                sessions[state_key] = state
                snapshots.extend(_Snapshot(turn, token, state_key) for turn in selected)
            if snapshots:
                self._write_processed_unlocked(processed)
            return snapshots, cleaned

    def _mark_failed(self, snapshots: list[_Snapshot], error: BaseException) -> None:
        try:
            now = _now_value(getattr(self.service, "clock", None))
            failure_code, failure_stage, validation_reason, validation_detail, attempt_count = _failure_metadata(error)
            with self.service.vault.lock():
                processed = _read_processed(self.service.vault.processed_index_path)
                sessions = processed.setdefault("sessions", {})
                for snapshot in snapshots:
                    state = sessions.get(snapshot.state_key)
                    if not isinstance(state, dict):
                        continue
                    marker = state.get("processing")
                    if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                        continue
                    failed_marker = {
                        "status": _FAILED_STATUS,
                        "token": snapshot.token,
                        "turn_keys": list(marker.get("turn_keys", [])),
                        "turn_indices": list(marker.get("turn_indices", [])),
                        "failed_at": now,
                    }
                    failed_marker["failure_code"] = failure_code
                    if failure_stage is not None:
                        failed_marker["failure_stage"] = failure_stage
                    if validation_reason is not None:
                        failed_marker["validation_reason"] = validation_reason
                    if validation_detail is not None:
                        failed_marker["validation_detail"] = validation_detail
                    if attempt_count is not None:
                        failed_marker["attempt_count"] = attempt_count
                    state["processing"] = failed_marker
                self._write_processed_unlocked(processed)
        except Exception:
            # Preserve the original safe error; a future call can recover an
            # orphaned processing marker by replacing it.
            return

    def _conversation_title(self, turn: InboxTurn) -> str:
        path = self._session_path_without_create(_session_key(turn.source, turn.session_id))
        if path is not None and path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("# Session "):
                        return line[2:].strip()
            except (OSError, UnicodeError):
                pass
        return f"{turn.source}/{turn.session_id}"

    def _scope_registry_projection(self) -> list[dict[str, Any]]:
        with self.service.vault.lock():
            try:
                config = self.service.vault.config()
                return scope_registry_projection(config)
            except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                raise ProcessingError("invalid scope registry") from error

    @staticmethod
    def _overlay_related(
        related: Iterable[Mapping[str, Any]],
        overlay: Iterable[Mapping[str, Any]] = (),
        *,
        query: str = "",
        scope: Any = None,
    ) -> list[dict[str, Any]]:
        """Overlay same-ID planned memories while retaining relevant results."""

        values = [dict(item) for item in related if isinstance(item, Mapping)]
        by_id: dict[str, dict[str, Any]] = {}
        without_id: list[dict[str, Any]] = []
        for item in values:
            memory_id = item.get("memory_id")
            if isinstance(memory_id, str) and item.get("native") is not True:
                by_id[memory_id.casefold()] = item
            else:
                without_id.append(item)
        for item in overlay:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            memory_id = value.get("memory_id")
            if isinstance(memory_id, str) and value.get("native") is not True:
                item_scopes = value.get("scopes")
                if isinstance(scope, str):
                    requested_scopes = {scope.casefold()}
                elif isinstance(scope, (list, tuple, set)):
                    requested_scopes = {
                        item.casefold()
                        for item in scope
                        if isinstance(item, str)
                    }
                else:
                    requested_scopes = set()
                if requested_scopes and isinstance(item_scopes, list):
                    available_scopes = {
                        item.casefold()
                        for item in item_scopes
                        if isinstance(item, str)
                    }
                    if "global" in requested_scopes:
                        if "global" not in available_scopes:
                            continue
                    elif not (
                        available_scopes.intersection(requested_scopes)
                        or "global" in available_scopes
                    ):
                        continue
                normalized_query = normalize_term(query)
                haystack = normalize_term(
                    " ".join(
                        str(value.get(field, ""))
                        for field in ("title", "body")
                    )
                )
                if normalized_query and haystack:
                    query_fragments: list[str] = []
                    for fragment in re.findall(
                        r"[\u4e00-\u9fff]{2,}|[a-z0-9]+",
                        normalized_query,
                        re.UNICODE,
                    ):
                        if re.fullmatch(r"[\u4e00-\u9fff]+", fragment):
                            query_fragments.extend(
                                fragment[index : index + 2]
                                for index in range(len(fragment) - 1)
                            )
                        else:
                            query_fragments.append(fragment)
                    if normalized_query not in haystack and query_fragments and not any(
                        fragment in haystack for fragment in query_fragments
                    ):
                        continue
                by_id[memory_id.casefold()] = value
            else:
                without_id.append(value)
        return _merge_related(list(by_id.values()) + without_id)

    def _related_query(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        query: str | Iterable[str],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
        strict_relevance: bool = False,
        priority_memory_ids: Iterable[str] = (),
        priority_only: bool = False,
        scope_records: Optional[list[Any]] = None,
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
    ]:
        if isinstance(query, str):
            query_value: str | list[str] = query.strip()
        else:
            query_value = [
                str(item).strip()
                for item in query
                if isinstance(item, str) and item.strip()
            ]
        visible = query_value if isinstance(query_value, str) else " ".join(query_value)
        scope = _safe_scope_background(state, explicit_scope)
        local: list[dict[str, Any]] = []
        indexed_native: list[dict[str, Any]] = []
        scope_fallback: Optional[tuple[list[Any], bool]] = None
        with self.service.vault.lock():
            priority_wanted = [
                value.casefold()
                for value in priority_memory_ids
                if isinstance(value, str) and value
            ]
            priority_records: list[Any] = []
            if priority_wanted:
                available = scope_records
                if available is None:
                    available, _ = self._scope_records_unlocked(scope)
                by_id = {
                    record.memory.memory_id.casefold(): record
                    for record in available
                }
                priority_records = [
                    by_id[value] for value in priority_wanted if value in by_id
                ]
            if priority_only:
                # A candidate already selected an active target from the
                # directory.  Reading that target is sufficient; a second
                # full-text search cannot change the candidate and only adds
                # cost (and unrelated context).
                records = priority_records
            else:
                records = self.service._search_unlocked(
                    query_value,
                    scope=scope if scope else None,
                    include_history=False,
                    todo_status="all",
                    limit=None,
                    # Processing needs the same candidate relevance boundary as
                    # the public directory search.  The legacy indexed-first
                    # lookup can return no record for an elliptical follow-up
                    # such as “this project's tasks”, even when the session scope
                    # identifies the project and its active memory is the only
                    # plausible maintenance target.
                    strict_candidates=True,
                ) if visible else []
                if visible and strict_relevance and self._has_specific_scope(scope):
                    records = [
                        record
                        for record in records
                        if candidate_matches_query(record.memory, query_value)
                    ]
                if visible and not records and not priority_records and self._has_specific_scope(scope):
                    scoped_records, ambiguous = self._scope_records_unlocked(scope)
                    scope_fallback = (scoped_records, ambiguous)
                    records = [] if ambiguous else scoped_records
                if priority_records:
                    priority_ids = {
                        record.memory.memory_id.casefold()
                        for record in priority_records
                    }
                    records = priority_records + [
                        record
                        for record in records
                        if record.memory.memory_id.casefold() not in priority_ids
                    ]
            local = _native_result([record.memory for record in records])
            if visible:
                if not priority_only:
                    indexed_native = NativeIndexer(self.service.vault).search_unlocked(
                        query_value,
                        target_agent=turn.source,
                        for_context=False,
                        limit=None,
                    )
        native = (
            _invoke_native(getattr(self.service, "native_memory_reader", None), visible, scope)
            if visible and not priority_only
            else []
        )
        related = self._overlay_related(
            _merge_related(local + indexed_native + native),
            overlay,
            query=visible,
            scope=scope,
        )
        related = self._bound_related(
            related,
            priority_memory_ids=priority_memory_ids,
        )
        native_refs = [
            {
                "source_id": item["native_source_id"],
                "native_id": item["native_id"],
            }
            for item in related
            if item.get("native") is True
            and isinstance(item.get("native_source_id"), str)
            and isinstance(item.get("native_id"), str)
        ]
        return related, scope, native_refs, scope_fallback

    @staticmethod
    def _has_specific_scope(scope: Any) -> bool:
        values = [scope] if isinstance(scope, str) else scope if isinstance(scope, (list, tuple, set)) else []
        return any(isinstance(value, str) and value not in {"", "global", "unscoped"} for value in values)

    @staticmethod
    def _single_specific_scope(scope: Any) -> bool:
        values = [scope] if isinstance(scope, str) else list(scope) if isinstance(scope, (list, tuple, set)) else []
        return len(values) == 1 and isinstance(values[0], str) and values[0] not in {"", "global", "unscoped"}

    @staticmethod
    def _related_payload_size(value: Mapping[str, Any]) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError, OverflowError):
            return -1

    @classmethod
    def _bound_related(
        cls,
        related: Iterable[Mapping[str, Any]],
        *,
        priority_memory_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Keep model related-memory context within the processing budget.

        Update/duplicate targets are placed first, but every body is still
        bounded.  The serialized payload limit also covers metadata, so a
        large tag/alias list cannot bypass the body budget.
        """

        priority = {
            value.casefold()
            for value in priority_memory_ids
            if isinstance(value, str) and value
        }
        values: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping):
                continue
            value = dict(item)
            memory_id = value.get("memory_id")
            if isinstance(memory_id, str):
                key = memory_id.casefold()
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            values.append(value)
        values.sort(
            key=lambda value: (
                isinstance(value.get("memory_id"), str)
                and value["memory_id"].casefold() in priority,
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        used = 2  # The surrounding JSON array brackets.
        for value in values:
            if len(selected) >= _RELATED_MAX_ITEMS:
                break
            body = value.get("body")
            if isinstance(body, str) and len(body) > _RELATED_MAX_BODY_CHARS:
                value["body"] = body[: _RELATED_MAX_BODY_CHARS - 1].rstrip() + "…"
            size = cls._related_payload_size(value)
            if size < 0:
                continue
            additional = size + (1 if selected else 0)
            if used + additional > _RELATED_MAX_CHARS:
                # A priority target still gets a minimal, bounded view when
                # oversized metadata leaves no room for its normal payload.
                memory_id = value.get("memory_id")
                if not (
                    isinstance(memory_id, str)
                    and memory_id.casefold() in priority
                ):
                    continue
                minimal = {
                    key: value[key]
                    for key in ("memory_id", "title", "body", "type", "scopes")
                    if key in value
                }
                size = cls._related_payload_size(minimal)
                if size < 0 or used + size + (1 if selected else 0) > _RELATED_MAX_CHARS:
                    continue
                value = minimal
                additional = size + (1 if selected else 0)
            selected.append(value)
            used += additional
        return selected

    def _scope_records_unlocked(self, scope: Any) -> tuple[list[Any], bool]:
        """Read and rank active records once for a scoped fallback."""

        active_records = self.service._read_memories_unlocked("knowledge")
        scoped = filter_by_scope(
            [record.memory for record in active_records],
            scope,
            self.service.vault.config(),
        )
        ranks = {memory.memory_id.casefold(): rank for memory, rank in scoped}
        records = [
            record
            for record in active_records
            if record.memory.memory_id.casefold() in ranks
        ]
        records.sort(
            key=lambda record: (
                ranks[record.memory.memory_id.casefold()],
                record.memory.updated,
                record.memory.memory_id,
            ),
            reverse=True,
        )
        return records, len(records) > 1

    @classmethod
    def _scope_directory_entry(cls, memory: Memory) -> tuple[dict[str, Any], bool]:
        title = memory.title
        title_truncated = len(title) > _SCOPE_DIRECTORY_MAX_TITLE_CHARS
        if title_truncated:
            title = title[: _SCOPE_DIRECTORY_MAX_TITLE_CHARS - 1].rstrip() + "…"
        return (
            {
                "memory_id": memory.memory_id,
                "title": title,
                "type": memory.type,
                "scopes": list(memory.scopes),
            },
            title_truncated,
        )

    @classmethod
    def _scope_directory(
        cls,
        records: Iterable[Any],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return a bounded metadata-only directory from scoped records."""

        records = list(records)
        complete = len(records) <= _SCOPE_DIRECTORY_MAX_ITEMS
        directory: list[dict[str, Any]] = []
        used = 2
        for record in records[:_SCOPE_DIRECTORY_MAX_ITEMS]:
            entry, title_truncated = cls._scope_directory_entry(record.memory)
            complete = complete and not title_truncated
            try:
                size = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            except (TypeError, ValueError, OverflowError):
                complete = False
                continue
            additional = size + (1 if directory else 0)
            if used + additional > _SCOPE_DIRECTORY_MAX_CHARS:
                complete = False
                break
            directory.append(entry)
            used += additional
        if len(records) > _SCOPE_DIRECTORY_MAX_ITEMS:
            complete = False
        return directory, complete

    def _related(
        self,
        turn: InboxTurn,
        state: Mapping[str, Any],
        explicit_scope: Any = None,
        *,
        overlay: Iterable[Mapping[str, Any]] = (),
    ) -> tuple[
        list[dict[str, Any]],
        Any,
        list[dict[str, str]],
        Optional[tuple[list[Any], bool]],
    ]:
        visible = " ".join(event.content for event in turn.events if isinstance(event.content, str)).strip()
        return self._related_query(
            turn,
            state,
            visible,
            explicit_scope,
            overlay=overlay,
            strict_relevance=True,
        )

    def _active_memory_by_id(self, memory_id: Any) -> Optional[Memory]:
        """Resolve one active memory, including the processor's write overlay."""

        if not isinstance(memory_id, str) or not memory_id:
            return None
        key = memory_id.casefold()
        try:
            with self.service.vault.lock():
                for record in self.service._read_memories_unlocked("knowledge"):
                    if record.memory.memory_id.casefold() == key:
                        return record.memory
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        for item in self._planned_related:
            if (
                isinstance(item, Mapping)
                and isinstance(item.get("memory_id"), str)
                and item["memory_id"].casefold() == key
            ):
                try:
                    return Memory.from_mapping(item)
                except (TypeError, ValueError):
                    return None
        return None

    def _turn_evidence_project_scope(
        self,
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> str | None:
        domains: list[str] = []
        for event in turn.events:
            for item in getattr(event, "tool_evidence", ()):
                if isinstance(item, Mapping) and isinstance(item.get("domain"), str):
                    domains.append(item["domain"])
        matches = project_scopes_for_domains(domains, config if "scopes" in config else {"scopes": config})
        return matches[0] if len(matches) == 1 else None

    def _scope_evidence_conflict(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> bool:
        if candidate.get("worth") is not True:
            return False
        units = supporting_units(candidate, analyze_turn_evidence(_event_payload(turn)))
        selected = {value.casefold() for value in candidate.get("scopes", [])
                    if isinstance(value, str) and value.startswith("project:")}
        registry = config.get("scopes", config)
        for unit in units:
            if unit.origin != "external_observation" and not unit.section_path:
                continue
            grounded = _project_scope_occurrences("\n".join((*unit.section_path, unit.text)), registry)
            if grounded is None:
                return True
            projects = {item[2].casefold() for item in grounded}
            if selected and projects and not selected.issubset(projects):
                return True
            if selected and unit.section_path and not projects:
                return True
            mapped = project_scopes_for_domains([unit.domain] if unit.domain else [], {"scopes": registry})
            if selected and mapped and (len(mapped) != 1 or mapped[0].casefold() not in selected):
                return True
        return False

    @staticmethod
    def _scope_terms_present(text: str, scope: str, config: Mapping[str, Any]) -> bool:
        terms = [scope, scope.partition(":")[2]]
        node = config.get("scopes", {}).get(scope) if isinstance(config.get("scopes", {}), Mapping) else None
        if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
            terms.extend(item for item in node["aliases"] if isinstance(item, str))
        folded = text.casefold()
        return any(term.casefold() in folded for term in terms if term)

    def _scope_correction_plan(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        config: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Authorize one explicit cross-project correction without guessing.

        The current user turn must name exactly two configured project scopes
        under explicit correction wording. A model-provided target is checked
        against that evidence; when it is omitted, Core may recover exactly one
        same-type, same-topic active memory from the explicitly named old
        scope. Zero or multiple matches stay deferred rather than becoming a
        cross-scope CREATE.
        """

        if candidate.get("worth") is not True or not isinstance(candidate.get("type"), str):
            return None
        new_projects = [
            value for value in candidate.get("scopes", [])
            if isinstance(value, str) and value.startswith("project:")
        ]
        if len(new_projects) != 1:
            return None
        new_scope = new_projects[0]
        user_text = " ".join(
            event.content for event in turn.events
            if event.role == "user" and isinstance(event.content, str)
        ).strip()
        if not user_text or not _SCOPE_CORRECTION_MARKER_RE.search(user_text):
            return None
        scopes = config.get("scopes", {}) if isinstance(config.get("scopes", {}), Mapping) else {}
        mentioned = [
            scope for scope in scopes
            if isinstance(scope, str)
            and scope.startswith("project:")
            and self._scope_terms_present(user_text, scope, config)
        ]
        mentioned = list(dict.fromkeys(mentioned))
        if len(mentioned) != 2 or all(scope.casefold() != new_scope.casefold() for scope in mentioned):
            return None
        old_scope = next(scope for scope in mentioned if scope.casefold() != new_scope.casefold())

        topic = str(candidate.get("memory") or "")
        removable_terms: list[str] = []
        for scope in (old_scope, new_scope):
            removable_terms.extend((scope, scope.partition(":")[2]))
            node = scopes.get(scope)
            if isinstance(node, Mapping) and isinstance(node.get("aliases"), list):
                removable_terms.extend(item for item in node["aliases"] if isinstance(item, str))
        for term in sorted({item for item in removable_terms if item}, key=len, reverse=True):
            topic = re.sub(re.escape(term), " ", topic, flags=re.IGNORECASE)
        topic = re.sub(r"[\s:：，,；;。.!！?？()（）\[\]【】_-]+", " ", topic).strip()
        if len(normalize_term(topic)) < 4:
            return {
                "target_memory_id": None,
                "old_scope": old_scope,
                "new_scope": new_scope,
                "survivor_memory_id": None,
                "ambiguous": True,
                "unresolved": True,
            }

        try:
            with self.service.vault.lock():
                records = self.service._read_memories_unlocked("knowledge")
        except (OSError, UnicodeError, ValueError, TypeError):
            return None
        eligible_old: list[Memory] = []
        eligible_new: list[Memory] = []
        for record in records:
            memory = record.memory
            if memory.type != candidate.get("type"):
                continue
            if filter_by_scope([memory], [old_scope], config) and candidate_matches_query(memory, topic):
                eligible_old.append(memory)
            if filter_by_scope([memory], [new_scope], config) and candidate_matches_query(memory, topic):
                eligible_new.append(memory)

        target_id = candidate.get("update_memory_id")
        target: Memory | None = None
        if isinstance(target_id, str) and target_id:
            selected = self._active_memory_by_id(target_id)
            if (
                selected is not None
                and selected.type == candidate.get("type")
                and any(memory.memory_id.casefold() == selected.memory_id.casefold() for memory in eligible_old)
            ):
                target = selected
            else:
                return {
                    "target_memory_id": None,
                    "old_scope": old_scope,
                    "new_scope": new_scope,
                    "survivor_memory_id": None,
                    "ambiguous": True,
                    "unresolved": True,
                }
        elif len(eligible_old) == 1:
            target = eligible_old[0]
        else:
            return {
                "target_memory_id": None,
                "old_scope": old_scope,
                "new_scope": new_scope,
                "survivor_memory_id": None,
                "ambiguous": True,
                "unresolved": True,
            }

        survivors = [
            memory for memory in eligible_new
            if memory.memory_id.casefold() != target.memory_id.casefold()
        ]
        return {
            "target_memory_id": target.memory_id,
            "old_scope": old_scope,
            "new_scope": new_scope,
            "survivor_memory_id": survivors[0].memory_id if len(survivors) == 1 else None,
            "ambiguous": len(survivors) > 1,
            "unresolved": False,
        }

    def _scope_correction_request(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        plan: Mapping[str, Any],
        *,
        conversation_title: str,
        native_refs: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        survivor_id = plan.get("survivor_memory_id")
        survivor = self._active_memory_by_id(survivor_id)
        if survivor is None:
            raise ProcessingError("scope correction survivor disappeared")
        summary = {
            "title": survivor.title,
            "body": survivor.body,
            "tags": list(survivor.tags),
            "type": survivor.type,
            "scopes": list(survivor.scopes),
            "scope_source": survivor.scope_source,
            "aliases": list(survivor.aliases),
            "keywords": list(survivor.keywords),
            "sources": [],
            "scope_operations": [],
            "status": survivor.status,
            "completed_at": survivor.completed_at,
            "due_date": survivor.due_date,
        }
        return {
            "summary": summary,
            "turn": turn,
            "candidate_id": str(candidate["candidate_id"]),
            "memory_id": survivor.memory_id,
            "event_key": turn.event_keys[0] if turn.event_keys else "",
            "turn_id": "",
            "conversation_title": conversation_title,
            "explicit_remember": False,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
            "scope_correction": dict(plan),
        }

    def _target_relation(
        self,
        candidate: Mapping[str, Any],
        *,
        turn: Optional[InboxTurn] = None,
        scope_directory: Optional[list[dict[str, Any]]] = None,
        scope_directory_complete: bool = True,
    ) -> str:
        """Classify a selected target as NOT_RELATED, SAME_USE, or UNKNOWN."""

        target_id = next(
            (
                candidate.get(field)
                for field in ("duplicate_memory_id", "update_memory_id")
                if isinstance(candidate.get(field), str) and candidate.get(field)
            ),
            None,
        )
        memory = candidate.get("memory")
        scopes = candidate.get("scopes")
        if not isinstance(target_id, str) or not isinstance(memory, str) or not isinstance(scopes, list):
            return _TARGET_UNKNOWN
        target = self._active_memory_by_id(target_id)
        if target is None:
            return _TARGET_UNKNOWN
        try:
            config = self.service.vault.config()
        except (OSError, UnicodeError, ValueError, TypeError):
            return _TARGET_UNKNOWN
        source = candidate.get("scope_source")
        if is_project_plan_text(memory) and self._is_adjacent_plan_record(target.title):
            return _TARGET_NOT_RELATED
        if scope_directory is not None and not scope_directory_complete:
            return _TARGET_UNKNOWN
        if not filter_by_scope([target], scopes, config):
            return _TARGET_NOT_RELATED

        scope_terms = self._project_scope_terms(scopes, config)

        if self._model_scope_is_elliptical(candidate, turn, config):
            # An inherited project scope can make an elliptical turn (for
            # example, "this project's task") unambiguous even when the
            # project name is absent from the current events.  The
            # candidate-local scope query still verifies the target's actual
            # membership before writing.
            return _TARGET_SAME_USE

        if source in {"user", "session_context"}:
            # These scopes are authoritative context.  Do not require title
            # wording to repeat the inherited project/entity name.
            return _TARGET_SAME_USE
        if source != "model":
            return _TARGET_UNKNOWN

        # A complete inherited-scope directory is an explicit, bounded target
        # selection.  The active target has already been resolved above; body
        # and topic relevance are resolved by the candidate-local context
        # below.  Detached candidates do not pass this directory and
        # therefore always use full retrieval.
        if scope_directory is not None and scope_directory_complete:
            selected = next(
                (
                    item
                    for item in scope_directory
                    if isinstance(item, Mapping)
                    and isinstance(item.get("memory_id"), str)
                    and item["memory_id"].casefold() == target.memory_id.casefold()
                ),
                None,
            )
            if selected is not None:
                return _TARGET_SAME_USE

        # Global and other non-project scopes do not expose a stable project
        # identity to compare.  The active, scope-matched target is the
        # relevant context; retain the established behavior for those targets.
        if not scope_terms:
            return _TARGET_SAME_USE

        # A plan candidate and a formal plan target represent the same
        # future-use object even when the candidate is phrased as a proposed
        # adjustment and shares few title tokens.  Ambiguous same-scope plans
        # are handled conservatively by _infer_update_target before this
        # classifier is reached; adjacent mail/meeting records were excluded
        # above.
        if is_project_plan_text(memory) and is_project_plan_text(target.title):
            return _TARGET_SAME_USE

        def without_scope_terms(value: str) -> str:
            result = value
            for term in sorted(set(scope_terms), key=len, reverse=True):
                result = re.sub(re.escape(term), "", result, flags=re.IGNORECASE)
            return result.strip()

        topic_query = without_scope_terms(memory)
        topic_title = without_scope_terms(target.title)
        if not topic_query or not topic_title:
            return _TARGET_UNKNOWN
        topic_memory = Memory(
            memory_id=target.memory_id,
            title=topic_query,
            body="",
            type=target.type,
            scopes=target.scopes,
        )
        return _TARGET_SAME_USE if candidate_matches_query(topic_memory, topic_title) else _TARGET_NOT_RELATED

    @staticmethod
    def _project_scope_terms(
        scopes: Any,
        config: Mapping[str, Any],
    ) -> list[str]:
        configured_scopes = config.get("scopes", {}) if isinstance(config, Mapping) else {}
        terms: list[str] = []
        if not isinstance(scopes, list):
            return terms
        for scope in scopes:
            if not isinstance(scope, str) or not scope.startswith("project:"):
                continue
            terms.append(scope.partition(":")[2])
            metadata = configured_scopes.get(scope) if isinstance(configured_scopes, Mapping) else None
            aliases = metadata.get("aliases") if isinstance(metadata, Mapping) else None
            if isinstance(aliases, list):
                terms.extend(alias for alias in aliases if isinstance(alias, str) and alias)
        return terms

    @classmethod
    def _model_scope_is_elliptical(
        cls,
        candidate: Mapping[str, Any],
        turn: Optional[InboxTurn],
        config: Mapping[str, Any],
    ) -> bool:
        if candidate.get("scope_source") != "model" or turn is None:
            return False
        terms = cls._project_scope_terms(candidate.get("scopes"), config)
        if not terms:
            return False
        visible_text = normalize_term(
            " ".join(
                event.content
                for event in turn.events
                if isinstance(event.content, str)
            )
        )
        return not any(normalize_term(term) in visible_text for term in terms)

    @staticmethod
    def _project_scope_keys(scopes: Any) -> set[str]:
        if not isinstance(scopes, list):
            return set()
        return {
            scope.casefold()
            for scope in scopes
            if isinstance(scope, str) and scope.startswith("project:")
        }

    @staticmethod
    def _is_project_plan_title(value: Any) -> bool:
        return is_project_plan_text(value)

    @staticmethod
    def _is_adjacent_plan_record(value: Any) -> bool:
        text = normalize_term(value) if isinstance(value, str) else ""
        return bool(text) and any(
            marker in text
            for marker in (
                "已发送", "发送", "邮件", "附件", "存档", "会议", "启动会", "纪要",
                "sent", "email", "mail", "attachment", "archive", "meeting", "minutes",
            )
        ) and not is_project_plan_text(value)

    @classmethod
    def _merge_additive_project_plan_update(
        cls,
        candidate: Mapping[str, Any],
        summary: Mapping[str, Any],
        target: Memory | None,
    ) -> dict[str, Any]:
        """Preserve a full active plan when current evidence only adds constraints."""

        result = dict(summary)
        if (
            target is None
            or target.type != "project"
            or not cls._is_project_plan_title(target.title)
        ):
            return result
        candidate_text = str(candidate.get("memory", "")).casefold()
        additive_markers = (
            "建议", "补充", "增加", "新增", "追加", "还需", "同时", "纳入", "完善",
            "suggest", "propose", "additional", "add ", "include",
        )
        replacement_markers = (
            "改为", "变更为", "替换", "取消", "删除", "不再", "更新为", "调整为", "推迟至", "提前至",
            "replace", "changed to", "no longer", "cancel", "remove", "instead", "moved to",
        )
        if not any(marker in candidate_text for marker in additive_markers):
            return result
        if any(marker in candidate_text for marker in replacement_markers):
            return result

        old_body = target.body.strip()
        new_body = str(result.get("body", "")).strip()
        normalized_old = " ".join(old_body.casefold().split())
        normalized_new = " ".join(new_body.casefold().split())
        if old_body and normalized_old not in normalized_new:
            result["body"] = f"{old_body}\n\n{new_body}" if new_body else old_body
        result["title"] = target.title
        for field in ("tags", "aliases", "keywords"):
            merged: list[str] = []
            for value in list(getattr(target, field)) + list(result.get(field, [])):
                if isinstance(value, str) and value not in merged:
                    merged.append(value)
            if merged or field in result:
                result[field] = merged
        return result

    def _infer_update_target(
        self,
        candidate: Mapping[str, Any],
        related: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Reuse one unambiguous same-scope active memory before CREATE.

        General same-type matching prevents a wrong gate target from escaping
        into a sibling CREATE. Project-plan candidates retain the specialized
        project-over-fact preference used by earlier maintenance behavior.
        """

        result = dict(candidate)
        if (
            not result.get("worth")
            or result.get("duplicate")
            or any(result.get(field) for field in ("duplicate_memory_id", "update_memory_id"))
            or not isinstance(result.get("memory"), str)
        ):
            return result
        project_keys = self._project_scope_keys(result.get("scopes"))
        if len(project_keys) != 1:
            return result

        candidate_type = result.get("type")
        candidate_text = result["memory"]
        same_type_matches: list[Memory] = []
        seen_same_type: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping) or item.get("native") is True:
                continue
            memory_id = item.get("memory_id")
            if (
                not isinstance(memory_id, str)
                or item.get("type") != candidate_type
                or self._project_scope_keys(item.get("scopes")) != project_keys
                or memory_id.casefold() in seen_same_type
            ):
                continue
            try:
                target = Memory.from_mapping(item)
            except (TypeError, ValueError):
                continue
            target_title = normalize_term(target.title)
            candidate_normalized = normalize_term(candidate_text)
            if (
                len(target_title) < 4
                or target_title not in candidate_normalized
                or not candidate_matches_query(target, candidate_text)
            ):
                continue
            seen_same_type.add(memory_id.casefold())
            same_type_matches.append(target)

        specialized_plan = (
            candidate_type in {"fact", "project"}
            and self._is_project_plan_title(candidate_text)
        )
        if not specialized_plan:
            if len(same_type_matches) > 1:
                result["_defer_reason"] = "ambiguous_update_target"
                return result
            if len(same_type_matches) == 1:
                result["update_memory_id"] = same_type_matches[0].memory_id
            return result

        project_matches: list[Memory] = []
        fact_matches: list[Memory] = []
        seen: set[str] = set()
        for item in related:
            if not isinstance(item, Mapping) or item.get("native") is True:
                continue
            memory_id = item.get("memory_id")
            item_type = item.get("type")
            item_scopes = item.get("scopes")
            if (
                not isinstance(memory_id, str)
                or not isinstance(item_type, str)
                or item_type not in {"fact", "project"}
                or self._project_scope_keys(item_scopes) != project_keys
                or memory_id.casefold() in seen
                or not self._is_project_plan_title(item.get("title"))
                or self._is_adjacent_plan_record(item.get("title"))
            ):
                continue
            try:
                target = Memory.from_mapping(item)
            except (TypeError, ValueError):
                continue
            seen.add(memory_id.casefold())
            if target.type == "project":
                project_matches.append(target)
            else:
                fact_matches.append(target)

        if len(project_matches) > 1 or (not project_matches and len(fact_matches) > 1):
            result["_defer_reason"] = "ambiguous_update_target"
            return result
        if project_matches:
            target = project_matches[0]
        elif len(fact_matches) == 1:
            target = fact_matches[0]
        else:
            return result
        result["update_memory_id"] = target.memory_id
        result["type"] = target.type
        return result

    def _defer_candidate(
        self,
        turn_ref: tuple[str, str, str],
        candidate: Mapping[str, Any],
        reason: str,
        *,
        scopes: Optional[Iterable[str]] = None,
        scope_source: Any = _UNSET,
    ) -> None:
        self._record_disposition(
            turn_ref,
            candidate,
            "DEFERRED",
            reason=reason,
            memory_id=(
                candidate.get("update_memory_id")
                if isinstance(candidate.get("update_memory_id"), str)
                else candidate.get("duplicate_memory_id")
                if isinstance(candidate.get("duplicate_memory_id"), str)
                else None
            ),
        )
        self._deferred_by_turn[turn_ref].append(
            {
                "candidate_id": str(candidate["candidate_id"]),
                "scopes": list(scopes if scopes is not None else candidate["scopes"]),
                "scope_source": (
                    candidate.get("scope_source")
                    if scope_source is _UNSET
                    else scope_source
                ),
                "reason": reason,
            }
        )

    @staticmethod
    def _planned_memory(request: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Project a not-yet-committed request into the next turn's lookup."""

        summary = request.get("summary")
        if not isinstance(summary, Mapping) or request.get("duplicate_memory_id") is not None:
            return None
        target_id = summary.get("update_memory_id")
        memory_id = target_id if isinstance(target_id, str) else request.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            return None
        body = summary.get("body")
        title = summary.get("title")
        scopes = summary.get("scopes")
        if not isinstance(body, str) or not isinstance(title, str) or not isinstance(scopes, list):
            return None
        value: dict[str, Any] = {
            "memory_id": memory_id,
            "title": title,
            "body": body,
            "tags": list(summary.get("tags", [])) if isinstance(summary.get("tags", []), list) else [],
            "type": summary.get("type", "other"),
            "scopes": list(scopes),
            "aliases": list(summary.get("aliases", [])) if isinstance(summary.get("aliases", []), list) else [],
            "keywords": list(summary.get("keywords", [])) if isinstance(summary.get("keywords", []), list) else [],
            "status": summary.get("status"),
            "completed_at": summary.get("completed_at"),
            "due_date": summary.get("due_date"),
        }
        return value

    def _request(
        self,
        summary: Mapping[str, Any],
        turn: InboxTurn,
        *,
        candidate_id: str,
        conversation_title: str,
        event_key_value: Optional[str] = None,
        turn_id: str = "",
        explicit_remember: bool = False,
        native_refs: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        evidence = summary.get("evidence_event_ids", [])
        if not isinstance(evidence, list) or not evidence:
            evidence = list(turn.event_keys)
        memory_id = MemoryWriter.deterministic_memory_id(
            source=turn.source,
            session_id=turn.session_id,
            turn_key=turn.turn_key or turn_key(turn_id or candidate_id),
            candidate_id=candidate_id,
            evidence_event_ids=evidence,
        )
        return {
            "summary": dict(summary),
            "turn": turn,
            "candidate_id": candidate_id,
            "memory_id": memory_id,
            "event_key": event_key_value or (turn.event_keys[0] if turn.event_keys else ""),
            "turn_id": turn_id,
            "conversation_title": conversation_title,
            "explicit_remember": explicit_remember,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
        }

    def _duplicate_request(
        self,
        candidate: Mapping[str, Any],
        turn: InboxTurn,
        *,
        conversation_title: str,
        native_refs: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Turn a validated duplicate candidate into a metadata-only write."""

        duplicate_id = candidate.get("duplicate_memory_id")
        if not isinstance(duplicate_id, str) or not duplicate_id:
            raise ProcessingError("invalid duplicate memory target")
        summary = {
            "title": "",
            "body": "",
            "tags": [],
            "type": candidate.get("type"),
            "scopes": list(candidate.get("scopes", [])),
            "scope_source": candidate.get("scope_source"),
            "sources": [],
            "scope_operations": [],
        }
        return {
            "summary": summary,
            "turn": turn,
            "candidate_id": str(candidate["candidate_id"]),
            "memory_id": duplicate_id,
            "duplicate_memory_id": duplicate_id,
            "event_key": turn.event_keys[0] if turn.event_keys else "",
            "turn_id": "",
            "conversation_title": conversation_title,
            "explicit_remember": False,
            "native_refs": [dict(item) for item in native_refs if isinstance(item, Mapping)],
        }

    def _collect_turn_outputs(
        self,
        backend: Any,
        turn: InboxTurn,
        state: Mapping[str, Any],
        *,
        explicit: bool = False,
        explicit_candidate: Optional[Mapping[str, Any]] = None,
        scope: Any = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        events = _event_payload(turn)
        evidence_units = analyze_turn_evidence(events)
        coverage_rows: dict[str, dict[str, Any]] = {}
        turn_ref = (turn.source, turn.session_id, turn.turn_key)
        self._deferred_by_turn.setdefault(turn_ref, [])
        related, scope_background, native_refs, scope_fallback = self._related(
            turn,
            state,
            scope,
            overlay=self._planned_related,
        )
        # When a compressed turn has no lexical hit but inherits one concrete
        # scope containing several active memories, expose only a bounded
        # metadata directory to the gate.  Bodies stay out of this first
        # decision; the selected ID is re-read for summarize below.
        scope_directory: Optional[list[dict[str, Any]]] = None
        scope_directory_complete = True
        scope_ambiguous = False
        scoped_records: Optional[list[Any]] = None
        gate_related = related
        related_native_ids = [item["native_id"] for item in native_refs]
        related_memory_ids = [
            item["memory_id"]
            for item in related
            if item.get("native") is not True and isinstance(item.get("memory_id"), str)
        ]
        gate_related_memory_ids = list(related_memory_ids)
        if scope_fallback is not None:
            scoped_records, scope_ambiguous = scope_fallback
            if scope_ambiguous and self._single_specific_scope(scope_background):
                scope_directory, scope_directory_complete = self._scope_directory(scoped_records)
                gate_related = []
                for entry in scope_directory:
                    memory_id = entry.get("memory_id")
                    if isinstance(memory_id, str) and memory_id.casefold() not in {
                        value.casefold() for value in gate_related_memory_ids
                    }:
                        gate_related_memory_ids.append(memory_id)
        scope_registry = self._scope_registry_projection()
        with self.service.vault.lock():
            validation_scope_registry = self.service.vault.config().get("scopes", {})
        title = self._conversation_title(turn)
        if explicit:
            candidate = dict(explicit_candidate or {})
            summary = self._complete_json_stage(
                backend,
                summarize_prompt(
                    candidate,
                    events,
                    explicit=True,
                    related_memories=related,
                    scope_background=scope_background,
                    scope_registry=scope_registry,
                ),
                system=SUMMARIZE_SYSTEM,
                purpose="summarize",
                parser=lambda raw: parse_summarize_output(
                    _normalize_summary_dates(raw, turn, candidate),
                    current_event_keys=turn.event_keys,
                    related_native_ids=related_native_ids,
                    related_memory_ids=related_memory_ids,
                    scope_registry=validation_scope_registry,
                    allowed_due_dates=_grounded_due_dates(turn),
                    allow_no_change=False,
                ),
                diagnostic_context={
                    "source": turn.source,
                    "session_id": turn.session_id,
                    "turn_index": turn.turn_index,
                },
            )
            return (
                [
                    self._request(
                        summary,
                        turn,
                        candidate_id=str(candidate["candidate_id"]),
                        conversation_title=title,
                        explicit_remember=True,
                        native_refs=native_refs,
                    )
                ],
                list(summary["scopes"]),
            )

        gate_attempt_count = 0
        detached_update_target_ids: dict[str, str] = {}
        deferred_type_mismatch_ids: set[str] = set()
        target_relations: dict[str, str] = {}
        unknown_target_ids: set[str] = set()
        candidate_level_target_ids: set[str] = set()
        scope_correction_plans: dict[str, dict[str, Any]] = {}

        def parse_gate(raw: str) -> dict[str, Any]:
            nonlocal gate_attempt_count, coverage_rows
            gate_attempt_count += 1
            detached_update_target_ids.clear()
            deferred_type_mismatch_ids.clear()
            target_relations.clear()
            unknown_target_ids.clear()
            candidate_level_target_ids.clear()
            scope_correction_plans.clear()
            raw, coverage_value = split_gate_envelope(raw)
            raw_for_parse = (
                _normalize_final_gate_raw(raw, validation_scope_registry)
                if gate_attempt_count >= 3
                else raw
            )
            parsed = parse_gate_output(
                raw_for_parse,
                current_event_keys=turn.event_keys,
                related_memory_ids=gate_related_memory_ids,
                scope_registry=validation_scope_registry,
                enforce_model_scope_grounding=gate_attempt_count < 3,
                allow_mixed_future_use=gate_attempt_count >= 3,
            )
            coverage_rows = (parse_coverage(coverage_value, evidence_units, parsed["candidates"])
                             if coverage_value is not None else {})
            if gate_attempt_count >= 3:
                candidates = []
                for candidate in parsed["candidates"]:
                    candidate_scopes = [
                        item
                        for item in candidate.get("scopes", [])
                        if isinstance(item, str)
                    ]
                    if (
                        candidate.get("scope_source") == "model"
                        and candidate.get("worth") is True
                        and any(item.partition(":")[0] == "project" for item in candidate_scopes)
                    ):
                        selected_owners, matches = _model_scope_grounding_evidence(
                            str(candidate.get("memory", "")),
                            candidate_scopes,
                            validation_scope_registry,
                        )
                        candidate = dict(candidate)
                        if not selected_owners:
                            candidates.append(candidate)
                            continue
                        if len(matches) == 1:
                            grounded_scope = next(iter(matches.values()))
                            if grounded_scope.casefold() not in set(selected_owners.values()):
                                non_project_scopes = [
                                    item
                                    for item in candidate_scopes
                                    if item.partition(":")[0] != "project"
                                ]
                                if all(
                                    item.casefold() != grounded_scope.casefold()
                                    for item in non_project_scopes
                                ):
                                    non_project_scopes.append(grounded_scope)
                                candidate["scopes"] = non_project_scopes
                        else:
                            candidate["scopes"] = ["unscoped"]
                            candidate["scope_source"] = "insufficient_context"
                    candidates.append(candidate)
                parsed = dict(parsed)
                parsed["candidates"] = candidates

            # A model may explicitly mark an aggregate as insufficiently
            # grounded on its first response.  It is already valid gate JSON,
            # so waiting for a retry would defer the whole turn unnecessarily.
            # Split only on project names found in this candidate's own text;
            # unknown or ambiguous fragments remain candidate-local deferred
            # work below.
            if gate_attempt_count >= 3 or any(
                candidate.get("scope_source") == "insufficient_context"
                for candidate in parsed["candidates"]
                if isinstance(candidate, Mapping)
            ):
                split_candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    split_candidates.extend(
                        _split_model_project_candidate(
                            candidate,
                            validation_scope_registry,
                        )
                    )
                parsed = dict(parsed)
                parsed["candidates"] = split_candidates

            prepared_candidates: list[dict[str, Any]] = []
            for candidate in parsed["candidates"]:
                item = dict(candidate)
                if self._scope_evidence_conflict(item, turn, validation_scope_registry):
                    item["_defer_reason"] = "scope_conflict"
                plan = self._scope_correction_plan(item, turn, validation_scope_registry)
                if plan is not None:
                    item.pop("duplicate_memory_id", None)
                    item["duplicate"] = False
                    if isinstance(plan.get("target_memory_id"), str):
                        item["update_memory_id"] = plan["target_memory_id"]
                    else:
                        item.pop("update_memory_id", None)
                    scope_correction_plans[str(item["candidate_id"]).casefold()] = plan
                prepared_candidates.append(item)
            parsed = dict(parsed)
            parsed["candidates"] = prepared_candidates

            invalid_targets: dict[str, set[str]] = {}
            type_mismatches: set[str] = set()
            for candidate in parsed["candidates"]:
                target_fields = {
                    field
                    for field in ("duplicate_memory_id", "update_memory_id")
                    if isinstance(candidate.get(field), str) and candidate.get(field)
                }
                if not target_fields:
                    continue
                candidate_id = candidate["candidate_id"].casefold()
                correction = scope_correction_plans.get(candidate_id)
                relation = (
                    _TARGET_SAME_USE
                    if correction is not None and not correction.get("ambiguous")
                    else self._target_relation(
                        candidate,
                        turn=turn,
                        scope_directory=scope_directory,
                        scope_directory_complete=scope_directory_complete,
                    )
                )
                target_relations[candidate_id] = relation
                if relation == _TARGET_NOT_RELATED:
                    if not (
                        target_fields == {"duplicate_memory_id"}
                        and self._model_scope_is_elliptical(
                            candidate,
                            turn,
                            validation_scope_registry,
                        )
                    ):
                        invalid_targets[candidate_id] = target_fields
                    else:
                        # Let the candidate-local scope query reject this
                        # duplicate target once, preserving the original
                        # one-response safety boundary for inherited scopes.
                        candidate_level_target_ids.add(candidate_id)
                elif relation == _TARGET_UNKNOWN:
                    unknown_target_ids.add(candidate_id)
                if "update_memory_id" in target_fields:
                    target = self._active_memory_by_id(candidate["update_memory_id"])
                    if target is None:
                        unknown_target_ids.add(candidate_id)
                    elif target.type != candidate.get("type"):
                        type_mismatches.add(candidate_id)

            if invalid_targets and gate_attempt_count < 3:
                raise ModelOutputError(
                    "selected target is not relevant to the candidate topic",
                    validation_detail="target_not_relevant",
                )

            # Check topic relevance before surfacing a type mismatch.  This
            # ordering matters for aggregate turns: an unrelated target must
            # reach the existing final-retry detach path instead of consuming
            # all three attempts at the schema boundary.
            if type_mismatches and gate_attempt_count < 3:
                raise ModelOutputError(
                    "candidate type does not match update target",
                    validation_detail="update_target_type_mismatch",
                )

            if invalid_targets or type_mismatches:
                # A persistently non-converging model must not hold an entire
                # inbox turn hostage.  An unrelated update target can safely
                # become an independent CREATE candidate; an unrelated
                # duplicate has no independent fact to write and is dropped.
                candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    candidate_key = candidate["candidate_id"].casefold()
                    fields = invalid_targets.get(candidate_key)
                    mismatch = candidate_key in type_mismatches
                    if candidate_key in scope_correction_plans:
                        fields = None
                        mismatch = False
                    if mismatch:
                        # A type mismatch is never repaired by changing the
                        # target type.  If the target still serves the same
                        # future use, retain the candidate for an explicit
                        # scoped retry; detaching it would create a sibling
                        # for the same topic.  A clearly different durable
                        # topic may safely continue as CREATE.
                        candidate_copy = dict(candidate)
                        relation = target_relations.get(
                            candidate["candidate_id"].casefold(),
                            _TARGET_UNKNOWN,
                        )
                        if relation == _TARGET_NOT_RELATED and candidate.get("worth") is True:
                            wrong_target_id = candidate_copy.get("update_memory_id")
                            if isinstance(wrong_target_id, str):
                                detached_update_target_ids[candidate["candidate_id"].casefold()] = wrong_target_id
                            candidate_copy.pop("update_memory_id", None)
                            candidates.append(candidate_copy)
                        else:
                            deferred_type_mismatch_ids.add(candidate["candidate_id"].casefold())
                            candidates.append(candidate_copy)
                        continue
                    if candidate["candidate_id"].casefold() in unknown_target_ids:
                        candidates.append(candidate)
                        continue
                    if not fields:
                        candidates.append(candidate)
                        continue
                    if "duplicate_memory_id" in fields:
                        continue
                    if "update_memory_id" in fields and candidate.get("worth"):
                        independent = dict(candidate)
                        wrong_target_id = independent.get("update_memory_id")
                        if isinstance(wrong_target_id, str):
                            detached_update_target_ids[candidate["candidate_id"].casefold()] = wrong_target_id
                        independent.pop("update_memory_id", None)
                        candidates.append(independent)
                parsed = dict(parsed)
                parsed["candidates"] = candidates
            if gate_attempt_count >= 3:
                marked_candidates: list[dict[str, Any]] = []
                for candidate in parsed["candidates"]:
                    item = dict(candidate)
                    if item.get("worth") and is_mixed_future_use_text(item.get("memory")):
                        split = split_mixed_future_use_text(item.get("memory"))
                        if split is None:
                            item["_defer_reason"] = "mixed_future_use"
                            marked_candidates.append(item)
                            continue
                        base_id = str(item.get("candidate_id", "candidate"))
                        for index, (fragment, fragment_type) in enumerate(split, start=1):
                            child = dict(item)
                            digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()[:8]
                            child["candidate_id"] = f"{base_id}:future-{index}-{digest}"
                            child["memory"] = fragment
                            child["type"] = fragment_type
                            child["duplicate"] = False
                            child.pop("duplicate_memory_id", None)
                            child.pop("update_memory_id", None)
                            child.pop("_defer_reason", None)
                            marked_candidates.append(child)
                        continue
                    marked_candidates.append(item)
                parsed = dict(parsed)
                parsed["candidates"] = marked_candidates
            return parsed

        gate = self._complete_json_stage(
            backend,
            gate_prompt(
                events,
                related_memories=gate_related,
                scope_directory=scope_directory,
                scope_directory_complete=scope_directory_complete,
                scope_background=scope_background,
                scope_registry=scope_registry,
            ) + evidence_prompt(evidence_units),
            system=GATE_SYSTEM,
            purpose="gate",
            parser=parse_gate,
            diagnostic_context={
                "source": turn.source,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            },
        )
        # A model may correctly treat the rest of a mixed turn as a query and
        # still miss the user's explicit completion update. Recover only one
        # uniquely related active todo; all other gate decisions stay intact.
        todo_state_recovery = _todo_state_recovery_candidate(events, related)
        recovery_by_candidate: dict[str, tuple[str, str, str]] = {}
        recovery_target_id: str | None = None
        recovery_candidate_value: dict[str, Any] | None = None
        if todo_state_recovery is not None:
            recovery_candidate, recovery_state, recovery_target_id, recovery_completed_at = todo_state_recovery
            recovery_candidate_value = dict(recovery_candidate)
            recovery_candidate_id = recovery_candidate["candidate_id"].casefold()
            recovery_by_candidate[recovery_candidate_id] = (
                recovery_state,
                recovery_target_id,
                recovery_completed_at,
            )

            # Always use the deterministic recovery candidate for the old
            # target.  A model candidate that points at that target may be a
            # project/fact or an aggregate containing the new customer
            # revision; retaining it would either trigger a type mismatch or
            # overwrite the completion with the rework.
            recovery_key = recovery_target_id.casefold()
            gate["candidates"] = [
                item
                for item in gate["candidates"]
                if not (
                    isinstance(item, Mapping)
                    and any(
                        isinstance(item.get(field), str)
                        and item[field].casefold() == recovery_key
                        for field in ("update_memory_id", "duplicate_memory_id")
                    )
                )
            ]
            gate["candidates"].insert(0, recovery_candidate_value)

        force_create_candidate_ids: set[str] = set()
        completion_rework = (
            _completion_rework_candidate(events, validation_scope_registry)
            if todo_state_recovery is not None
            else None
        )
        if completion_rework is not None:
            rework_text = normalize_term(completion_rework.get("memory", ""))
            rework_scope_keys = {
                value.casefold()
                for value in completion_rework.get("scopes", [])
                if isinstance(value, str) and value.partition(":")[0] == "project"
            }
            rework_evidence_keys = {
                value.casefold()
                for value in completion_rework.get("evidence_event_ids", [])
                if isinstance(value, str)
            }
            exact_matches: list[dict[str, Any]] = []
            fallback_matches: list[dict[str, Any]] = []
            for item in gate["candidates"]:
                if not isinstance(item, Mapping) or item.get("worth") is not True:
                    continue
                if str(item.get("candidate_id", "")).casefold() in recovery_by_candidate:
                    continue
                candidate_text = normalize_term(item.get("memory", ""))
                candidate_evidence = {
                    value.casefold()
                    for value in item.get("evidence_event_ids", [])
                    if isinstance(value, str)
                }
                candidate_scope_keys = {
                    value.casefold()
                    for value in item.get("scopes", [])
                    if isinstance(value, str) and value.partition(":")[0] == "project"
                }
                if (
                    not rework_text
                    or not rework_evidence_keys.intersection(candidate_evidence)
                    or candidate_scope_keys != rework_scope_keys
                ):
                    continue
                full_text_match = (
                    rework_text in candidate_text or candidate_text in rework_text
                )
                if full_text_match:
                    exact_matches.append(dict(item))
                elif (
                    item.get("type") == "todo"
                    or is_actionable_todo_text(item.get("memory"))
                ) and any(marker in candidate_text for marker in _REWORK_ACTION_MARKERS):
                    fallback_matches.append(dict(item))

            # Prefer one model candidate that clearly covers the deterministic
            # tail.  Otherwise accept one same-event/same-project actionable
            # todo; multiple ambiguous candidates are replaced by the
            # deterministic full tail so they cannot coexist as duplicates.
            matched_candidates = exact_matches or fallback_matches
            exact_ids = {
                str(value.get("candidate_id", "")).casefold()
                for value in exact_matches
            }
            fallback_ids = {
                str(value.get("candidate_id", "")).casefold()
                for value in fallback_matches
            }
            discard_ids = set(exact_ids)
            if len(exact_matches) == 1:
                selected_text = normalize_term(exact_matches[0].get("memory", ""))
                discard_ids.update(
                    str(value.get("candidate_id", "")).casefold()
                    for value in fallback_matches
                    if (
                        selected_text
                        and normalize_term(value.get("memory", ""))
                        and (
                            normalize_term(value.get("memory", "")) in selected_text
                            or selected_text in normalize_term(value.get("memory", ""))
                        )
                    )
                )
            elif len(exact_matches) > 1 or len(fallback_matches) > 1:
                discard_ids.update(fallback_ids)
            rework_candidates: list[dict[str, Any]] = []
            found_rework = len(matched_candidates) == 1
            selected_id = (
                str(matched_candidates[0].get("candidate_id", "")).casefold()
                if found_rework
                else ""
            )
            for item in gate["candidates"]:
                if not isinstance(item, Mapping):
                    continue
                candidate_item = dict(item)
                candidate_id_key = str(candidate_item.get("candidate_id", "")).casefold()
                if candidate_id_key in discard_ids:
                    if found_rework and candidate_id_key == selected_id:
                        candidate_item.pop("update_memory_id", None)
                        candidate_item.pop("duplicate_memory_id", None)
                        candidate_item["duplicate"] = False
                        candidate_item["type"] = "todo"
                        candidate_item["scopes"] = list(completion_rework["scopes"])
                        candidate_item["scope_source"] = completion_rework["scope_source"]
                        candidate_item.pop("_defer_reason", None)
                        candidate_item["_force_create"] = True
                        force_create_candidate_ids.add(candidate_id_key)
                        rework_candidates.append(candidate_item)
                    # Ambiguous model matches are discarded in favor of the
                    # deterministic fallback appended below.
                    continue
                rework_candidates.append(candidate_item)
            if not found_rework:
                rework_candidates.append(dict(completion_rework))
                force_create_candidate_ids.add(completion_rework["candidate_id"].casefold())
            # Keep the deterministic completion first, then any independent
            # rework candidate(s), followed by unrelated gate decisions.
            recovery_ids = set(recovery_by_candidate)
            gate["candidates"] = [
                item
                for item in rework_candidates
                if str(item.get("candidate_id", "")).casefold() not in recovery_ids
            ]
            if recovery_candidate_value is not None:
                gate["candidates"].insert(0, recovery_candidate_value)
        requests: list[dict[str, Any]] = []
        observed_scopes: list[str] = []
        current_turn_request_ids: set[str] = set()
        read_only_query = read_only_turn(evidence_units)
        covered_unit_ids: set[str] = set()
        seen_candidates: set[tuple[Any, ...]] = set()
        for candidate in gate["candidates"]:
            candidate = dict(candidate)
            unit_ids = [uid for uid, row in coverage_rows.items()
                        if candidate.get("candidate_id") in row.get("candidate_ids", [])]
            if unit_ids:
                candidate["_evidence_unit_ids"] = unit_ids
            reason, support = admission_reason(candidate, evidence_units)
            candidate["evidence_unit_ids"] = [u.unit_id for u in support]
            covered_unit_ids.update(candidate["evidence_unit_ids"])
            if reason is not None and candidate.get("worth"):
                if reason in {"read_only_query", "quoted_or_example"}:
                    self._record_disposition(turn_ref, candidate, "NO_CHANGE", reason=reason)
                else:
                    self._defer_candidate(turn_ref, candidate, reason, scopes=candidate.get("scopes", []))
                continue
            fingerprint = (normalize_term(str(candidate.get("memory", ""))),
                           candidate.get("type"), tuple(sorted(candidate.get("scopes", []))),
                           candidate.get("update_memory_id"), candidate.get("duplicate_memory_id"))
            if fingerprint in seen_candidates:
                self._record_disposition(turn_ref, candidate, "NO_CHANGE", reason="same_turn_duplicate")
                continue
            seen_candidates.add(fingerprint)
            # Recheck every origin here, including deterministic state recovery.
            if self._scope_evidence_conflict(candidate, turn, validation_scope_registry):
                self._defer_candidate(turn_ref, candidate, "scope_conflict", scopes=candidate.get("scopes", []))
                continue
            candidate_id_key = str(candidate.get("candidate_id", "")).casefold()
            force_create = (
                candidate_id_key in force_create_candidate_ids
                or candidate.get("_force_create") is True
            )
            if force_create:
                candidate = dict(candidate)
                candidate.pop("_force_create", None)
            recovery = recovery_by_candidate.get(candidate_id_key)
            correction_plan = scope_correction_plans.get(candidate_id_key)
            detached_update_target_id = detached_update_target_ids.get(candidate_id_key)
            defer_reason = candidate.get("_defer_reason")
            # A pure existing-memory query must not leave even a deferred
            # candidate behind when the model mislabels its recap.  Explicit
            # todo state recovery is the only write-eligible exception.
            if read_only_query and recovery is None:
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="read_only_query",
                )
                continue
            if isinstance(defer_reason, str) and defer_reason in {"mixed_future_use", "scope_conflict"}:
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    defer_reason,
                    scopes=candidate.get("scopes"),
                )
                continue
            if correction_plan is not None and correction_plan.get("ambiguous"):
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    (
                        "scope_correction_unresolved"
                        if correction_plan.get("unresolved")
                        else "scope_correction_ambiguous"
                    ),
                    scopes=candidate.get("scopes"),
                )
                continue
            if candidate.get("worth") and (
                _automatic_transient_memory(candidate.get("memory"))
            ):
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="transient",
                )
                continue
            # A combined mailbox/daily digest is not an atomic memory.  If a
            # concrete action was worth retaining, the gate must emit it as
            # its own candidate; the aggregate shell itself is NO_CHANGE.
            if candidate.get("worth") and is_aggregate_operational_text(candidate.get("memory")):
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="aggregate",
                )
                continue
            if candidate.get("worth") and is_attachment_followup_only_text(candidate.get("memory")):
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="attachment_followup",
                )
                continue
            candidate_scopes = list(candidate["scopes"])
            # An automatic candidate with no reliable project attribution is
            # retained as a retryable inbox turn, never silently promoted to
            # global knowledge.  The processed ledger records only a compact
            # marker; the complete candidate remains in the inbox.
            if candidate["worth"] and (
                candidate_scopes == ["unscoped"]
                or candidate.get("scope_source") == "insufficient_context"
            ):
                self._defer_candidate(turn_ref, candidate, "scope_required", scopes=candidate_scopes)
                continue

            has_target = any(
                isinstance(candidate.get(field), str) and candidate.get(field)
                for field in ("duplicate_memory_id", "update_memory_id")
            )
            if (
                scope_directory is not None
                and not scope_directory_complete
                and detached_update_target_id is None
                and correction_plan is None
                and (
                    candidate["worth"] or has_target
                )
            ):
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "scope_directory_incomplete",
                    scopes=candidate_scopes,
                )
                continue

            if (
                candidate["worth"]
                and scope_ambiguous
                and not has_target
                and detached_update_target_id is None
                and correction_plan is None
            ):
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "related_ambiguous",
                    scopes=candidate_scopes,
                )
                continue

            if candidate_id_key in unknown_target_ids:
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "target_unknown",
                    scopes=candidate_scopes,
                )
                continue

            if candidate_id_key in deferred_type_mismatch_ids:
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "update_target_type_mismatch",
                    scopes=candidate_scopes,
                )
                continue

            candidate_related, candidate_scope_background, candidate_native_refs, _ = self._related_query(
                turn,
                state,
                _candidate_lookup_queries(candidate.get("memory")),
                candidate_scopes,
                overlay=self._planned_related,
                priority_memory_ids=[
                    candidate.get("duplicate_memory_id"),
                    candidate.get("update_memory_id"),
                ],
                priority_only=(scope_directory is not None and detached_update_target_id is None),
                scope_records=(
                    scoped_records
                    if scope_directory is not None and detached_update_target_id is None
                    else None
                ),
            )
            if correction_plan is not None:
                priority_ids = [
                    correction_plan.get("target_memory_id"),
                    correction_plan.get("survivor_memory_id"),
                ]
                for priority_id in reversed(priority_ids):
                    memory = self._active_memory_by_id(priority_id)
                    if memory is None:
                        continue
                    if not any(
                        isinstance(item, Mapping)
                        and isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == memory.memory_id.casefold()
                        for item in candidate_related
                    ):
                        candidate_related.insert(0, memory.to_dict())
            if recovery is not None:
                recovery_target = self._active_memory_by_id(recovery[1])
                if recovery_target is not None and not any(
                    isinstance(item, Mapping)
                    and isinstance(item.get("memory_id"), str)
                    and item["memory_id"].casefold() == recovery_target.memory_id.casefold()
                    for item in candidate_related
                ):
                    candidate_related = [recovery_target.to_dict(), *candidate_related]
            if detached_update_target_id is not None:
                candidate_related = [
                    item
                    for item in candidate_related
                    if not (
                        isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == detached_update_target_id.casefold()
                    )
                ]
            if not force_create and correction_plan is None:
                candidate = self._infer_update_target(candidate, candidate_related)
            defer_reason = candidate.pop("_defer_reason", None)
            if defer_reason:
                self._defer_candidate(turn_ref, candidate, defer_reason)
                continue
            target_field = next(
                (
                    field
                    for field in ("duplicate_memory_id", "update_memory_id")
                    if isinstance(candidate.get(field), str) and candidate.get(field)
                ),
                None,
            )
            if target_field is not None:
                relation = (
                    _TARGET_SAME_USE
                    if correction_plan is not None and not correction_plan.get("ambiguous")
                    else self._target_relation(
                        candidate,
                        turn=turn,
                        scope_directory=(
                            scope_directory if detached_update_target_id is None else None
                        ),
                        scope_directory_complete=scope_directory_complete,
                    )
                )
                if relation == _TARGET_UNKNOWN:
                    self._defer_candidate(
                        turn_ref,
                        candidate,
                        "target_unknown",
                        scopes=candidate_scopes,
                    )
                    continue
                if relation == _TARGET_NOT_RELATED and not (
                    target_field == "duplicate_memory_id"
                    and candidate_id_key in candidate_level_target_ids
                ):
                    self._defer_candidate(
                        turn_ref,
                        candidate,
                        "target_not_relevant",
                        scopes=candidate_scopes,
                    )
                    continue
                if target_field == "update_memory_id":
                    active_target = self._active_memory_by_id(candidate[target_field])
                    if active_target is None:
                        self._defer_candidate(
                            turn_ref,
                            candidate,
                            "target_unknown",
                            scopes=candidate_scopes,
                        )
                        continue
                    if active_target.type != candidate.get("type"):
                        self._defer_candidate(
                            turn_ref,
                            candidate,
                            "update_target_type_mismatch",
                            scopes=candidate_scopes,
                        )
                        continue
            if correction_plan is not None and correction_plan.get("survivor_memory_id"):
                requests.append(
                    self._scope_correction_request(
                        candidate,
                        turn,
                        correction_plan,
                        conversation_title=title,
                        native_refs=candidate_native_refs,
                    )
                )
                for observed_scope in candidate.get("scopes", []):
                    if isinstance(observed_scope, str) and observed_scope != "unscoped" and observed_scope not in observed_scopes:
                        observed_scopes.append(observed_scope)
                continue
            candidate_native_ids = [item["native_id"] for item in candidate_native_refs]
            all_candidate_memory_ids: list[str] = []
            same_type_update_memory_ids: list[str] = []
            for item in candidate_related:
                if item.get("native") is True or not isinstance(item.get("memory_id"), str):
                    continue
                active_memory = self._active_memory_by_id(item["memory_id"])
                if active_memory is None:
                    continue
                all_candidate_memory_ids.append(active_memory.memory_id)
                if active_memory.type == candidate.get("type"):
                    same_type_update_memory_ids.append(active_memory.memory_id)
            all_candidate_id_set = {item.casefold() for item in all_candidate_memory_ids}
            same_type_update_id_set = {
                item.casefold() for item in same_type_update_memory_ids
            }
            duplicate_target = candidate.get("duplicate_memory_id")
            if duplicate_target is not None and (
                not isinstance(duplicate_target, str)
                or duplicate_target.casefold() not in all_candidate_id_set
            ):
                raise ModelOutputError(
                    "duplicate_memory_id is not a related active memory for this candidate",
                    validation_detail="invalid_duplicate_target",
                )
            update_target = candidate.get("update_memory_id")
            if update_target is not None and (
                not isinstance(update_target, str)
                or update_target.casefold() not in same_type_update_id_set
            ):
                raise ModelOutputError(
                    "update_memory_id is not a related active memory of the candidate type",
                    validation_detail="invalid_update_target",
                )

            if candidate["duplicate"] or not candidate["worth"]:
                duplicate_memory_id = candidate.get("duplicate_memory_id")
                if duplicate_memory_id is not None:
                    requests.append(
                        self._duplicate_request(
                            candidate,
                            turn,
                            conversation_title=title,
                            native_refs=candidate_native_refs,
                        )
                    )
                    # Automatic duplicate observations are metadata no-ops;
                    # only the already-active target's scopes are trustworthy
                    # session context, never a transient model-provided scope.
                    duplicate_scopes = next(
                        (
                            item.get("scopes")
                            for item in candidate_related
                            if isinstance(item, Mapping)
                            and isinstance(item.get("memory_id"), str)
                            and item["memory_id"].casefold() == duplicate_memory_id.casefold()
                            and isinstance(item.get("scopes"), list)
                        ),
                        [],
                    )
                    for observed_scope in duplicate_scopes:
                        if (
                            isinstance(observed_scope, str)
                            and observed_scope != "unscoped"
                            and observed_scope not in observed_scopes
                        ):
                            observed_scopes.append(observed_scope)
                    self._record_disposition(
                        turn_ref,
                        candidate,
                        "NO_CHANGE",
                        reason="duplicate",
                        memory_id=duplicate_memory_id,
                    )
                else:
                    self._record_disposition(
                        turn_ref,
                        candidate,
                        "NO_CHANGE",
                        reason="not_worthy",
                    )
                continue

            gate_update_target = candidate.get("update_memory_id")
            gate_target_type = None
            if isinstance(gate_update_target, str):
                gate_target_key = gate_update_target.casefold()
                gate_target_type = next(
                    (
                        item.get("type")
                        for item in candidate_related
                        if isinstance(item, Mapping)
                        and isinstance(item.get("memory_id"), str)
                        and item["memory_id"].casefold() == gate_target_key
                        and isinstance(item.get("type"), str)
                    ),
                    None,
                )

            try:
                def parse_summary(raw: str) -> dict[str, Any]:
                    if recovery is not None and isinstance(raw, str):
                        # Complete/cancel state is deterministic evidence from
                        # the user event. Inject it before strict validation so
                        # a model that omits status/completed_at cannot leave
                        # the update active or fail the required-field check.
                        parsed_raw = parse_strict_json(raw)
                        if isinstance(parsed_raw, Mapping) and "decision" not in parsed_raw:
                            state_change, target_id, completed_at = recovery
                            parsed_raw = dict(parsed_raw)
                            parsed_raw["update_memory_id"] = target_id
                            parsed_raw["status"] = state_change
                            if state_change == "completed":
                                parsed_raw["completed_at"] = completed_at
                            else:
                                parsed_raw.pop("completed_at", None)
                            raw = json.dumps(parsed_raw, ensure_ascii=False, separators=(",", ":"))
                    parsed = parse_summarize_output(
                        _normalize_summary_dates(raw, turn, candidate),
                        current_event_keys=turn.event_keys,
                        related_native_ids=candidate_native_ids,
                        related_memory_ids=same_type_update_memory_ids,
                        scope_registry=validation_scope_registry,
                        expected_scopes=candidate["scopes"],
                        expected_scope_source=candidate["scope_source"],
                        allowed_due_dates=_grounded_due_dates(turn),
                        allow_no_change=recovery is None,
                        # The summarize stage may not reinterpret a gate
                        # candidate, including CREATE candidates. Updates
                        # additionally retain the active target's immutable
                        # type below.
                        expected_type=candidate.get("type"),
                        expected_update_memory_id=gate_update_target,
                        expected_target_type=gate_target_type,
                    )
                    return parsed

                summary = self._complete_json_stage(
                    backend,
                    summarize_prompt(
                        candidate,
                        events,
                        related_memories=candidate_related,
                        scope_background=candidate_scope_background,
                        scope_registry=scope_registry,
                    ),
                    system=SUMMARIZE_SYSTEM,
                    purpose="summarize",
                    parser=parse_summary,
                    diagnostic_context={
                        "source": turn.source,
                        "session_id": turn.session_id,
                        "turn_index": turn.turn_index,
                    },
                )
            except ModelOutputError as error:
                if getattr(error, "validation_detail", None) != "relative_time":
                    raise
                # The candidate's source turn remains in inbox for an
                # explicit retry.  Other candidates from this same turn may
                # still commit safely in the same transaction.
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "relative_time",
                    scopes=candidate["scopes"],
                )
                continue
            if summary.get("decision") == NO_CHANGE_DECISION:
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="summary_no_change",
                    memory_id=(
                        summary.get("update_memory_id")
                        if isinstance(summary.get("update_memory_id"), str)
                        else None
                    ),
                )
                continue
            if is_attachment_followup_only_text(
                f"{summary.get('title', '')}\n{summary.get('body', '')}"
            ):
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="attachment_followup",
                )
                continue
            if _automatic_transient_memory(
                f"{summary.get('title', '')}\n{summary.get('body', '')}"
            ):
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="transient",
                )
                continue
            if gate_update_target is not None:
                summary_update_target = summary.get("update_memory_id")
                if summary_update_target is None:
                    summary = dict(summary)
                    summary["update_memory_id"] = gate_update_target
                elif (
                    not isinstance(summary_update_target, str)
                    or summary_update_target.casefold() != gate_update_target.casefold()
                ):
                    raise ModelOutputError(
                        "summary update target differs from gate target",
                        validation_detail="invalid_update_target",
                    )
                else:
                    summary = dict(summary)
                    summary["update_memory_id"] = gate_update_target
                target = self.service.read(gate_update_target, include_history=False)
                summary = self._merge_additive_project_plan_update(candidate, summary, target)
            if summary["scopes"] == ["unscoped"] or summary.get("scope_source") == "insufficient_context":
                self._defer_candidate(
                    turn_ref,
                    candidate,
                    "scope_required",
                    scopes=summary["scopes"],
                    scope_source=summary.get("scope_source"),
                )
                continue
            pending_request = self._request(
                summary,
                turn,
                candidate_id=str(candidate["candidate_id"]),
                conversation_title=title,
                native_refs=candidate_native_refs,
            )
            if correction_plan is not None:
                pending_request["scope_correction"] = dict(correction_plan)
            current_turn_request_ids.add(pending_request["memory_id"].casefold())
            summary_update_target = summary.get("update_memory_id")
            final_is_create = not (
                isinstance(summary_update_target, str) and summary_update_target
            )
            if final_is_create and _automatic_create_conflicts(
                candidate,
                summary,
                candidate_related,
                ignore_memory_ids=current_turn_request_ids,
            ):
                self._record_disposition(
                    turn_ref,
                    candidate,
                    "NO_CHANGE",
                    reason="already_covered",
                )
                continue
            same_request = next((r for r in requests if
                r.get("summary", {}).get("type") == summary.get("type")
                and r.get("summary", {}).get("scopes") == summary.get("scopes")
                and normalize_term(str(r.get("summary", {}).get("body", ""))) == normalize_term(summary["body"])
                and r.get("summary", {}).get("status") == summary.get("status")
                and r.get("summary", {}).get("due_date") == summary.get("due_date")), None)
            if same_request is not None:
                self._record_disposition(turn_ref, candidate, "NO_CHANGE", reason="same_turn_duplicate")
                continue
            if summary_update_target and any(r.get("summary", {}).get("update_memory_id") == summary_update_target for r in requests):
                self._defer_candidate(turn_ref, candidate, "same_turn_target_conflict", scopes=candidate_scopes)
                continue
            pending_request["evidence_unit_ids"] = list(candidate.get("evidence_unit_ids", []))
            requests.append(pending_request)
            self._record_disposition(
                turn_ref,
                candidate,
                "UPDATE" if not final_is_create else "CREATE",
                memory_id=(
                    summary_update_target
                    if not final_is_create
                    else pending_request["memory_id"]
                ),
            )
            for observed_scope in summary["scopes"]:
                if (
                    isinstance(observed_scope, str)
                    and observed_scope != "unscoped"
                    and observed_scope not in observed_scopes
                ):
                    observed_scopes.append(observed_scope)
        evidence_dispositions = []
        for unit in evidence_units:
            row = coverage_rows.get(unit.unit_id)
            if unit.unit_id in covered_unit_ids:
                decision, reason = "CANDIDATE", "candidate_checked"
            elif row is not None:
                decision, reason = row["decision"], row.get("reason", "coverage_unresolved")
            elif unit.eligible:
                decision, reason = "DEFERRED", "coverage_unresolved"
            elif unit.origin == "unknown":
                decision, reason = "DEFERRED", "incomplete_tool_evidence"
            else:
                decision, reason = "NO_CHANGE", unit.origin
            if decision == "DEFERRED":
                marker = {"candidate_id": "coverage:" + unit.unit_id,
                          "scopes": ["unscoped"], "scope_source": "insufficient_context"}
                self._defer_candidate(turn_ref, marker, reason)
            evidence_dispositions.append({"unit_id": unit.unit_id, "event_key": unit.event_key,
                                          "decision": decision, "reason": reason})
        self._evidence_by_turn[turn_ref] = evidence_dispositions
        return requests, observed_scopes

    def _state_for_snapshot_unlocked(self, snapshot: _Snapshot, processed: Mapping[str, Any]) -> Mapping[str, Any]:
        sessions = processed.get("sessions", {})
        state = sessions.get(snapshot.state_key) if isinstance(sessions, Mapping) else None
        if not isinstance(state, Mapping):
            return {}

        # Compression rotates the physical session before the next visible
        # turn.  Resolve only a missing child scope through the persisted
        # parent chain; an explicit child scope remains authoritative.
        child_scope = _safe_scope_background(state)
        if child_scope:
            return state
        current = state
        visited = {snapshot.state_key}
        for _ in range(_MAX_SESSION_LINEAGE_DEPTH):
            parent_session_id = current.get("lineage_parent_session_id")
            if not isinstance(parent_session_id, str) or not parent_session_id:
                break
            try:
                parent_session_id = safe_component(parent_session_id, "parent session id")
            except ValueError:
                break
            parent_key = _session_key(snapshot.turn.source, parent_session_id)
            if parent_key in visited:
                break
            visited.add(parent_key)
            parent_state = sessions.get(parent_key) if isinstance(sessions, Mapping) else None
            if not isinstance(parent_state, Mapping):
                break
            parent_scope = _safe_scope_background(parent_state)
            if parent_scope:
                inherited = dict(state)
                inherited["scopes"] = list(parent_scope) if isinstance(parent_scope, list) else parent_scope
                return inherited
            current = parent_state
        return state

    @staticmethod
    def _processed_memory_ids(processed: Mapping[str, Any], event_key_value: str) -> Optional[list[str]]:
        sessions = processed.get("sessions")
        if not isinstance(sessions, Mapping):
            return None
        for state in sessions.values():
            if not isinstance(state, Mapping):
                continue
            entries = state.get("processed_turns")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                keys = entry.get("event_keys")
                if isinstance(keys, list) and any(
                    isinstance(key, str) and key.casefold() == event_key_value.casefold()
                    for key in keys
                ):
                    ids = entry.get("memory_ids", [])
                    return [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []
        return None

    def _deferred_counts(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
    ) -> tuple[int, int]:
        """Return unresolved scope candidates in the requested session scope."""

        candidates = 0
        turns = 0
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_index_path)
        sessions = processed.get("sessions")
        if not isinstance(sessions, Mapping):
            return 0, 0
        for state_key, state in sessions.items():
            if not isinstance(state_key, str):
                continue
            if source is not None and not state_key.startswith(f"{source}/"):
                continue
            if session_id is not None:
                state_source, separator, state_session = state_key.partition("/")
                if not separator or state_session != session_id or (
                    source is not None and state_source != source
                ):
                    continue
            if not isinstance(state, Mapping):
                continue
            entries = state.get("processed_turns")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                deferred = entry.get("deferred_candidates")
                if isinstance(deferred, list) and deferred:
                    turns += 1
                    candidates += sum(1 for item in deferred if isinstance(item, Mapping))
        return candidates, turns

    def _commit_success(
        self,
        snapshots: list[_Snapshot],
        requests: list[Mapping[str, Any]],
        *,
        now: str,
        cleanup_hours: int,
        observed_scopes: Mapping[tuple[str, str, str], Iterable[str]] | None = None,
        deferred_candidates: Mapping[tuple[str, str, str], Iterable[Mapping[str, Any]]] | None = None,
    ) -> list[str]:
        if not snapshots:
            return []
        by_snapshot: dict[str, list[Mapping[str, Any]]] = {}
        for request in requests:
            turn = request["turn"]
            key = _session_key(turn.source, turn.session_id)
            by_snapshot.setdefault(key, []).append(request)
        with self.service.vault.lock():
            processed = _read_processed(self.service.vault.processed_index_path)
            sessions = processed.setdefault("sessions", {})
            for snapshot in snapshots:
                state = sessions.get(snapshot.state_key)
                marker = state.get("processing") if isinstance(state, Mapping) else None
                if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                    raise ProcessingError("processing ownership changed")
            written: list[Memory] = []
            all_requests = list(requests)
            claimed_native: dict[str, str] = {}
            for request in all_requests:
                summary = request.get("summary", {})
                shadow_ids = summary.get("shadow_native_ids", [])
                refs = request.get("native_refs", [])
                ref_ids = {
                    item.get("native_id").casefold()
                    for item in refs
                    if isinstance(item, Mapping) and isinstance(item.get("native_id"), str)
                }
                if not isinstance(shadow_ids, list) or any(
                    not isinstance(native_id, str) or native_id.casefold() not in ref_ids
                    for native_id in shadow_ids
                ):
                    raise ProcessingError("shadow native reference is not related to this turn")
                for native_id in shadow_ids:
                    key = native_id.casefold()
                    if key in claimed_native:
                        raise ProcessingError("native segment is shadowed more than once in one batch")
                    claimed_native[key] = request["memory_id"]

            scope_values: list[str] = []
            session_scope_updates: dict[str, list[str]] = {}
            scope_operations: list[Mapping[str, Any]] = []
            for snapshot in snapshots:
                scope_key = (
                    snapshot.turn.source,
                    snapshot.turn.session_id,
                    snapshot.turn.turn_key,
                )
                values = list((observed_scopes or {}).get(scope_key, []))
                if values:
                    session_scope_updates[snapshot.state_key] = values
                    for observed_scope in values:
                        if observed_scope not in scope_values:
                            scope_values.append(observed_scope)
            for request in all_requests:
                operations = request.get("summary", {}).get("scope_operations", [])
                if not isinstance(operations, list):
                    raise ProcessingError("invalid scope operations")
                scope_operations.extend(
                    operation for operation in operations if isinstance(operation, Mapping)
                )

            prepared_scopes = None
            if scope_operations:
                try:
                    prepared_scopes = ScopeMaintainer(self.service).prepare(
                        self.service.vault.config(),
                        operations=scope_operations,
                        observed_scopes=scope_values,
                        session_scopes=session_scope_updates,
                    )
                except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                    raise ProcessingError("scope maintenance preflight failed") from error
                # Feed the canonical post-operation scopes to the writer too.
                # Otherwise a retry could briefly rewrite an already-migrated
                # active memory with the old source scope and create history.
                for request in all_requests:
                    summary = request.get("summary")
                    if not isinstance(summary, dict):
                        continue
                    values = summary.get("scopes")
                    if not isinstance(values, list):
                        continue
                    migrated: list[str] = []
                    for value in values:
                        current = value
                        seen: set[str] = set()
                        while current in prepared_scopes.migrations:
                            if current in seen:
                                raise ProcessingError("scope migration contains a cycle")
                            seen.add(current)
                            current = prepared_scopes.migrations[current]
                        if current not in migrated:
                            migrated.append(current)
                    summary["scopes"] = migrated
            plan = TurnPlan.from_requests(all_requests)
            operations = processed.setdefault("pending_operations", {})
            previous_operation_ids = set(operations)
            for operation in plan.candidates:
                operations.setdefault(operation.operation_id, operation.to_dict())
            if plan.candidates:
                # Persist write intent before knowledge/history mutation. The
                # journal contains identifiers and digests, never source text.
                self._write_processed_unlocked(processed)
            if all_requests:
                written = self.writer.write_many_unlocked(all_requests, now=now)
            else:
                self.writer.last_metadata_merged = 0
                self.writer.last_noop_memory_ids = set()
            noop_memory_ids = self.writer.last_noop_memory_ids
            for request in all_requests:
                request_memory_id = request.get("memory_id")
                if request_memory_id not in noop_memory_ids:
                    continue
                duplicate_id = request.get("duplicate_memory_id")
                summary = request.get("summary")
                update_id = (
                    summary.get("update_memory_id")
                    if isinstance(summary, Mapping)
                    else None
                )
                target_id = (
                    duplicate_id
                    if isinstance(duplicate_id, str) and duplicate_id
                    else update_id
                    if isinstance(update_id, str) and update_id
                    else None
                )
                self._record_request_disposition(
                    request,
                    "NO_CHANGE",
                    reason=(
                        "duplicate"
                        if isinstance(duplicate_id, str) and duplicate_id
                        else "unchanged"
                    ),
                    memory_id=target_id,
                )
            if prepared_scopes is not None:
                try:
                    ScopeMaintainer(self.service).apply_unlocked(
                        processed,
                        prepared_scopes,
                        session_scope_updates,
                    )
                except (OSError, UnicodeError, ValueError, TypeError, ScopeMaintenanceError) as error:
                    raise ProcessingError("scope maintenance commit failed") from error
            else:
                self.service._rebuild_index_unlocked()
                if scope_values:
                    try:
                        config = self.service.vault.config()
                        updated_config = register_scope_nodes(config, scope_values)
                    except ScopeError as error:
                        raise ProcessingError("invalid observed session scope") from error
                    if updated_config != config:
                        save_config(self.service.vault.config_path, updated_config)

            native_indexer = NativeIndexer(self.service.vault)
            for request, memory in zip(all_requests, written):
                shadow_ids = request["summary"].get("shadow_native_ids", [])
                if not shadow_ids:
                    continue
                # A prior attempt may have persisted the memory but failed
                # while applying its native shadow.  Re-apply that side
                # effect on retry even when the memory request itself was
                # recognized as already applied.
                shadow_keys = {value.casefold() for value in shadow_ids}
                refs = [
                    item
                    for item in request.get("native_refs", [])
                    if isinstance(item, Mapping)
                    and isinstance(item.get("native_id"), str)
                    and item["native_id"].casefold() in shadow_keys
                ]
                native_indexer.apply_shadow_unlocked(refs, memory.memory_id)
            processed = _read_processed(self.service.vault.processed_index_path)
            sessions = processed.setdefault("sessions", {})
            memory_ids_by_turn: dict[tuple[str, str], list[str]] = {}
            for request, memory in zip(all_requests, written):
                if request.get("memory_id") in noop_memory_ids:
                    continue
                turn = request["turn"]
                memory_ids_by_turn.setdefault((turn.source, turn.session_id, turn.turn_key), []).append(memory.memory_id)
            for snapshot in snapshots:
                state = sessions.get(snapshot.state_key)
                if not isinstance(state, dict):
                    raise ProcessingError("processing session disappeared")
                marker = state.get("processing")
                if not isinstance(marker, Mapping) or marker.get("token") != snapshot.token:
                    raise ProcessingError("processing ownership changed")
                entries = state.get("processed_turns")
                if not isinstance(entries, list):
                    entries = []
                existing_entry = next(
                    (
                        entry
                        for entry in entries
                        if isinstance(entry, Mapping) and entry.get("turn_key") == snapshot.turn.turn_key
                    ),
                    None,
                )
                ids = memory_ids_by_turn.get(
                    (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key), []
                )
                if isinstance(existing_entry, dict):
                    entry = existing_entry
                    entry["memory_ids"] = sorted(set(entry.get("memory_ids", []) + ids))
                else:
                    entry = {
                        "turn_key": snapshot.turn.turn_key,
                        "turn_index": snapshot.turn.turn_index,
                        "event_keys": list(snapshot.turn.event_keys),
                        "processed_at": now,
                        "eligible_cleanup_at": _add_hours(now, cleanup_hours),
                        "memory_ids": sorted(set(ids)),
                    }
                    entries.append(entry)
                scope_key = (
                    snapshot.turn.source,
                    snapshot.turn.session_id,
                    snapshot.turn.turn_key,
                )
                deferred_values = list((deferred_candidates or {}).get(scope_key, []))
                deferred_values = [
                    dict(item)
                    for item in deferred_values
                    if isinstance(item, Mapping)
                ]
                if deferred_values:
                    # Keep the complete source turn available for a later
                    # explicit-scope retry.  A missing cleanup timestamp is
                    # intentional: deleting the inbox would discard the
                    # unresolved candidate before it can be retried.
                    entry["deferred_candidates"] = deferred_values
                    entry["eligible_cleanup_at"] = None
                    entry.pop("cleanup_done_at", None)
                else:
                    entry.pop("deferred_candidates", None)
                    if not entry.get("cleanup_done_at"):
                        entry["eligible_cleanup_at"] = _add_hours(now, cleanup_hours)
                operations = processed.get("pending_operations", {})
                for operation_id, operation in list(operations.items()):
                    if not isinstance(operation, Mapping) or (
                        operation.get("source"), operation.get("session_id"), operation.get("turn_key")
                    ) != scope_key:
                        continue
                    # Already under the Vault lock: do not re-enter the public
                    # reader (its file lock is intentionally non-reentrant).
                    active = None
                    try:
                        active_path = self.service.vault.memory_path(operation.get("memory_id"), "knowledge")
                        if active_path.is_file() and not active_path.is_symlink():
                            active = Memory.from_markdown(active_path.read_text(encoding="utf-8"), active_path)
                    except (OSError, UnicodeError, ValueError, TypeError):
                        pass
                    applied = (active is not None
                               and MemoryWriter._request_already_applied({"turn": snapshot.turn}, active)
                               and content_digest(active.to_dict()) == operation.get("digest"))
                    if applied:
                        self._record_disposition(scope_key, operation, operation["disposition"],
                                                 memory_id=operation["memory_id"])
                        for recorded in self._dispositions_by_turn.get(scope_key, []):
                            if recorded.get("candidate_id") == operation.get("candidate_id"):
                                recorded["operation_id"] = operation_id
                                recorded["replayed"] = operation_id in previous_operation_ids
                                recorded["already_applied"] = operation_id in previous_operation_ids
                        entry["memory_ids"] = sorted(set(entry.get("memory_ids", []) + [operation["memory_id"]]))
                    # Successful no-op intents are resolved too. A failed
                    # commit never reaches the final journal-clearing write.
                    operations.pop(operation_id, None)
                entry["candidate_dispositions"] = self._candidate_dispositions([snapshot])
                entry["evidence_dispositions"] = self._evidence_by_turn.get(scope_key, [])
                state["processed_turns"] = entries
                watermark = max(_as_int(state.get("watermark"), 0), _as_int(snapshot.turn.turn_index, 0))
                state["watermark"] = watermark
                state["processed_watermark"] = watermark
                if prepared_scopes is not None and snapshot.state_key in prepared_scopes.session_scopes:
                    current_scopes = list(prepared_scopes.session_scopes[snapshot.state_key])
                else:
                    current_scopes = list((observed_scopes or {}).get(scope_key, []))
                if current_scopes:
                    state["scopes"] = current_scopes
                sessions[snapshot.state_key] = state
            for state_key in {snapshot.state_key for snapshot in snapshots}:
                state = sessions.get(state_key)
                if isinstance(state, dict):
                    state["processing"] = {"status": _IDLE_STATUS, "last_processed_at": now}
                    sessions[state_key] = state
            self._write_processed_unlocked(processed)
            return [
                memory.memory_id
                for request, memory in zip(all_requests, written)
                if request.get("memory_id") not in noop_memory_ids
            ]

    def process(
        self,
        *,
        source: str | None = None,
        session_id: str | None = None,
        model: Any = None,
        router: Any = None,
        scope: Any = None,
    ) -> dict[str, Any]:
        if source is not None:
            source = safe_component(source, "source")
        if session_id is not None:
            session_id = safe_component(session_id, "session id")
        now = _now_value(getattr(self.service, "clock", None))
        cleanup_hours = self._cleanup_hours()
        snapshots, cleaned = self._snapshot(
            source=source,
            session_id=session_id,
            now=now,
            cleanup_hours=cleanup_hours,
            scope=scope,
        )
        if not snapshots:
            deferred_candidates, deferred_turns = self._deferred_counts(
                source=source,
                session_id=session_id,
            )
            return {
                "processed_turns": 0,
                "memories_written": 0,
                "memory_ids": [],
                "metadata_merged": 0,
                "cleaned_turns": cleaned,
                "deferred_candidates": deferred_candidates,
                "deferred_inbox_turns": deferred_turns,
                "compaction": self._auto_compact(model=model, router=router),
            }
        backend = self._resolve_backend(model=model, router=router)
        requests: list[dict[str, Any]] = []
        observed_scopes: dict[tuple[str, str, str], list[str]] = {}
        self._planned_related = []
        self._deferred_by_turn = {}
        self._dispositions_by_turn = {}
        self._evidence_by_turn = {}
        try:
            for snapshot in snapshots:
                with self.service.vault.lock():
                    processed = _read_processed(self.service.vault.processed_index_path)
                    state = self._state_for_snapshot_unlocked(snapshot, processed)
                turn_requests, turn_scopes = self._collect_turn_outputs(
                    backend, snapshot.turn, state, scope=scope
                )
                requests.extend(turn_requests)
                for request in turn_requests:
                    planned = self._planned_memory(request)
                    if planned is None:
                        continue
                    planned_id = planned.get("memory_id")
                    if isinstance(planned_id, str):
                        self._planned_related = [
                            item
                            for item in self._planned_related
                            if item.get("memory_id", "").casefold() != planned_id.casefold()
                        ]
                    self._planned_related.append(planned)
                observed_scopes[(snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key)] = turn_scopes
            ids = self._commit_success(
                snapshots,
                requests,
                now=_now_value(getattr(self.service, "clock", None)),
                cleanup_hours=cleanup_hours,
                observed_scopes=observed_scopes,
                deferred_candidates=self._deferred_by_turn,
            )
            compaction = self._auto_compact(model=backend)
            deferred_candidates, deferred_turns = self._deferred_counts(
                source=source,
                session_id=session_id,
            )
            return {
                "processed_turns": len(snapshots),
                "memories_written": len(ids),
                "memory_ids": ids,
                "metadata_merged": self.writer.last_metadata_merged,
                "cleaned_turns": cleaned,
                "deferred_candidates": deferred_candidates,
                "deferred_inbox_turns": deferred_turns,
                "compaction": compaction,
            }
        except Exception as error:
            self._mark_failed(snapshots, error)
            raise

    def _remember_turn(
        self,
        *,
        content: str,
        source: str,
        session_id: str,
        turn_id: str,
        event_key_value: str,
        scopes: Any,
        now: str,
        cleanup_hours: int,
    ) -> tuple[Optional[_Snapshot], Optional[Mapping[str, Any]], Optional[InboxTurn], int]:
        with self.service.vault.lock():
            self.service._recover_compaction_unlocked()
            processed = _read_processed(self.service.vault.processed_index_path)
            cleaned = self._cleanup_due_unlocked(processed, now, cleanup_hours)
            existing = self._processed_memory_ids(processed, event_key_value)
            if existing is not None:
                return None, {"memory_ids": existing}, None, cleaned
            sessions = processed.setdefault("sessions", {})
            state_key = _session_key(source, session_id)
            state = sessions.get(state_key)
            if not isinstance(state, dict):
                state = {}
            if self._processing_marker_live(state.get("processing"), now):
                raise ProcessingError("session is already being processed")
            turns = state.get("turns")
            if not isinstance(turns, dict):
                turns = {}
            stable_key = turn_key(turn_id)
            index = _as_int(turns.get(stable_key), 0)
            if index <= 0:
                index = max(_as_int(state.get("next_turn_index"), 1), 1)
                turns[stable_key] = index
                state["next_turn_index"] = index + 1
            state["turns"] = turns
            token = uuid.uuid4().hex
            state["processing"] = {
                "status": _PROCESSING_STATUS,
                "token": token,
                "owner_pid": os.getpid(),
                "turn_keys": [stable_key],
                "turn_indices": [index],
                "started_at": now,
            }
            sessions[state_key] = state
            processed["sessions"] = sessions
            self._write_processed_unlocked(processed)
        event = InboxEvent(
            source=source,
            session_id=session_id,
            turn_key=stable_key,
            turn_index=index,
            role="user",
            event_key=event_key_value,
            content=redact_text(content),
            turn_id=_safe_turn_id(turn_id),
            timestamp=now,
        )
        turn = InboxTurn(source, session_id, stable_key, index, (event,))
        candidate = {
            "candidate_id": f"remember-{event_key_value[:16]}",
            "memory": event.content,
            "evidence_event_ids": [event_key_value],
            "duplicate": False,
            "worth": True,
            "type": "other",
            "scopes": scopes if scopes is not None else ["global"],
            "scope_source": "user",
        }
        return _Snapshot(turn, token, state_key), candidate, turn, cleaned

    def remember(
        self,
        content: str | None = None,
        *,
        text: str | None = None,
        source: str = "memleaf",
        session_id: str = "remember",
        turn_id: str | None = None,
        event_id: str | None = None,
        scopes: Any = None,
        model: Any = None,
        router: Any = None,
    ) -> dict[str, Any]:
        value = content if content is not None else text
        if not isinstance(value, str) or not value.strip():
            raise ValueError("remember content is required")
        source = safe_component(source, "source")
        session_id = safe_component(session_id, "session id")
        normalized_scopes = None
        if scopes is not None:
            try:
                normalized_scopes = normalize_scopes(scopes, field="remember scopes")
            except ScopeError as error:
                raise ValueError("invalid remember scopes") from error
        raw_turn_id = turn_id or f"remember-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"
        if not isinstance(raw_turn_id, str) or not raw_turn_id or "\x00" in raw_turn_id or "\n" in raw_turn_id or "\r" in raw_turn_id:
            raise ValueError("invalid turn id")
        raw_event_id = event_id or f"remember/{source}/{session_id}/{raw_turn_id}"
        if not isinstance(raw_event_id, str) or not raw_event_id or "\x00" in raw_event_id or "\n" in raw_event_id or "\r" in raw_event_id:
            raise ValueError("invalid event id")
        stable_event_key = event_key(raw_event_id)
        now = _now_value(getattr(self.service, "clock", None))
        cleanup_hours = self._cleanup_hours()
        snapshot, candidate, turn, cleaned = self._remember_turn(
            content=value,
            source=source,
            session_id=session_id,
            turn_id=raw_turn_id,
            event_key_value=stable_event_key,
            scopes=normalized_scopes,
            now=now,
            cleanup_hours=cleanup_hours,
        )
        if snapshot is None:
            ids = list(candidate.get("memory_ids", [])) if isinstance(candidate, Mapping) else []
            return {
                "processed_turns": 0,
                "memories_written": 0,
                "memory_ids": ids,
                "metadata_merged": 0,
                "cleaned_turns": cleaned,
                "deferred_candidates": 0,
                "deferred_inbox_turns": 0,
                "compaction": self._auto_compact(model=model, router=router),
            }
        backend = self._resolve_backend(model=model, router=router)
        self._planned_related = []
        self._deferred_by_turn = {}
        try:
            with self.service.vault.lock():
                processed = _read_processed(self.service.vault.processed_index_path)
                state = self._state_for_snapshot_unlocked(snapshot, processed)
            requests, turn_scopes = self._collect_turn_outputs(
                backend,
                turn,
                state,
                explicit=True,
                explicit_candidate=candidate,
                scope=normalized_scopes,
            )
            if normalized_scopes is not None:
                turn_scopes = list(normalized_scopes)
            ids = self._commit_success(
                [snapshot],
                requests,
                now=_now_value(getattr(self.service, "clock", None)),
                cleanup_hours=cleanup_hours,
                observed_scopes={
                    (snapshot.turn.source, snapshot.turn.session_id, snapshot.turn.turn_key): turn_scopes
                },
            )
            return {
                "processed_turns": 1,
                "memories_written": len(ids),
                "memory_ids": ids,
                "metadata_merged": self.writer.last_metadata_merged,
                "cleaned_turns": cleaned,
                "deferred_candidates": 0,
                "deferred_inbox_turns": 0,
                "compaction": self._auto_compact(model=backend),
            }
        except Exception as error:
            self._mark_failed([snapshot], error)
            raise


def _add_hours(value: str, hours: int) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return value
    return (parsed + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["ProcessingError", "Processor"]
