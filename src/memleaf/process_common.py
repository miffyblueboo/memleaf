"""Pure shared values and compatibility helpers for processing components.

No file writes or service construction occur at import time.
"""
from __future__ import annotations
import hashlib
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from .admission import analyze_turn_evidence, read_only_turn
from .inbox import InboxTurn
from .llm import MODEL_ERROR_CODES, MODEL_VALIDATION_REASONS, ModelUnavailable
from .locking import read_json
from .models import Memory, utc_now
from .retrieval import candidate_matches_query, normalize_term
from .validation import MODEL_VALIDATION_DETAILS, ModelOutputError, parse_strict_json, normalize_relative_calendar_text, is_actionable_todo_text, is_project_plan_text

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


def _add_hours(value: str, hours: int) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return value
    return (parsed + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")
