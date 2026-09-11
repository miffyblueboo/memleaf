"""Hermes memory-provider adapter for the local memleaf MCP service.

The adapter deliberately talks to the public stdio MCP boundary instead of
importing Hermes internals or reaching into memleaf's private implementation.
This keeps the provider installable as a Hermes user plugin while reusing the
same local ``~/.memleaf`` vault as the standalone MCP server.
"""

from __future__ import annotations

import json
import importlib.util
import logging
import os
import re
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from hashlib import sha256
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

from agent.memory_provider import MemoryProvider, RecallStatus

try:
    from .evidence_budget import apply_evidence_budget
except (ImportError, ValueError):
    # Hermes installs this file as a standalone plugin directory.  Load the
    # adjacent copied module directly so the provider never imports Core (or
    # Hermes' package initializer) just to apply the capture budget.
    _BUDGET_SPEC = importlib.util.spec_from_file_location(
        "_memleaf_hermes_evidence_budget",
        Path(__file__).with_name("evidence_budget.py"),
    )
    if _BUDGET_SPEC is None or _BUDGET_SPEC.loader is None:
        raise ImportError("Hermes evidence budget module is unavailable")
    _BUDGET_MODULE = importlib.util.module_from_spec(_BUDGET_SPEC)
    sys.modules[_BUDGET_SPEC.name] = _BUDGET_MODULE
    _BUDGET_SPEC.loader.exec_module(_BUDGET_MODULE)
    apply_evidence_budget = _BUDGET_MODULE.apply_evidence_budget

logger = logging.getLogger(__name__)

_DEFAULT_VAULT = "~/.memleaf"
_DEFAULT_COMMAND = "memleaf-mcp"
_DEFAULT_TIMEOUT = 5.0
_MAX_TIMEOUT = 30.0
_DEFAULT_PROCESS_TIMEOUT = 300.0
_MAX_PROCESS_TIMEOUT = 900.0
_UPDATE_COMMAND = "python -m pip install -U memleaf && python -m memleaf install"
_MAX_SCOPE_ITEMS = 20
_MAX_SCOPE_CHARS = 2000
_SCOPE_MAP_INCOMPLETE = (
    "Scope Map preview incomplete; fetch scope_catalog from the first page "
    "before assuming a scope is absent."
)
_SCOPE_MAP_INVALID_NOTICE = (
    "<memleaf-scope-status>\n"
    "The memleaf scope map was unavailable or malformed; retrieval was not "
    "verified for this turn. Do not claim that memory was checked.\n"
    "</memleaf-scope-status>"
)
_MAX_PENDING_TURN_NUMBERS = 128
_MAX_SESSION_ALIASES = 128
_MAX_LINEAGE_RETRIES = 2
_MAX_DEFERRED_PROCESS_SESSIONS = 128
_MAX_OBSERVED_TOOL_CALL_KEYS = 2048
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PROVIDER_VERSION_RE = re.compile(r"^version:\s*([^\s#]+)\s*(?:#.*)?$", re.MULTILINE)
_ASCII_QUERY_TERM_RE = re.compile(r"[a-z0-9]+(?:[ ._-][a-z0-9]+)*")
_DISABLED_PLATFORMS = frozenset({"cron"})
_DISABLED_AGENT_CONTEXTS = frozenset({"cron", "flush", "subagent"})
_MODEL_ERROR_CODES = frozenset(
    {
        "model_timeout",
        "model_auth_failed",
        "model_rate_limited",
        "model_http_error",
        "model_network_error",
        "model_invalid_response",
        "model_unavailable",
        "model_failed",
    }
)
_MODEL_ERROR_STAGES = frozenset({"gate", "summarize"})
_MODEL_VALIDATION_REASONS = frozenset(
    {"empty_content", "invalid_json", "schema_violation", "response_shape"}
)
_MODEL_VALIDATION_DETAILS = frozenset(
    {
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
        "mixed_future_use",
        "other_schema_violation",
    }
)
_CALL_FAILED = object()
_PROCESS_PENDING = object()
_MISSING_TOOL_RESULT = object()
_MCP_PIPE_EOF = object()
_MAX_TOOL_RESULT_CHARS = 64 * 1024
_MAX_TOOL_RESULT_LAYERS = 4
_UNTRUSTED_TOOL_RESULT_TAG = "<untrusted_tool_result"
_UNTRUSTED_TOOL_RESULT_END = "</untrusted_tool_result>"


class _MCPToolError(RuntimeError):
    """Safe structured model error returned by the local MCP boundary."""

    def __init__(
        self,
        code: str = "model_failed",
        stage: Optional[str] = None,
        validation_reason: Optional[str] = None,
        attempt_count: Optional[int] = None,
        validation_detail: Optional[str] = None,
    ):
        self.code = code if isinstance(code, str) and code in _MODEL_ERROR_CODES else "model_failed"
        self.stage = stage if isinstance(stage, str) and stage in _MODEL_ERROR_STAGES else None
        self.validation_reason = (
            validation_reason
            if isinstance(validation_reason, str) and validation_reason in _MODEL_VALIDATION_REASONS
            else None
        )
        self.validation_detail = (
            validation_detail
            if isinstance(validation_detail, str) and validation_detail in _MODEL_VALIDATION_DETAILS
            else "other_schema_violation"
            if self.code == "model_invalid_response" and self.validation_reason == "schema_violation"
            else None
        )
        self.attempt_count = (
            attempt_count
            if isinstance(attempt_count, int) and not isinstance(attempt_count, bool) and attempt_count in (1, 2, 3, 4)
            else None
        )
        super().__init__("MCP tool failed")


def _mcp_error_fields(
    value: Any,
) -> Optional[tuple[str, Optional[str], Optional[str], Optional[int], Optional[str]]]:
    if not isinstance(value, Mapping):
        return None
    structured = value.get("structuredContent") if value.get("isError") else value
    if not isinstance(structured, Mapping):
        return None
    error = structured.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    stage = error.get("stage")
    safe_code = code if isinstance(code, str) and code in _MODEL_ERROR_CODES else "model_failed"
    safe_stage = stage if isinstance(stage, str) and stage in _MODEL_ERROR_STAGES else None
    reason = error.get("validation_reason")
    safe_reason = reason if isinstance(reason, str) and reason in _MODEL_VALIDATION_REASONS else None
    detail = error.get("validation_detail")
    safe_detail = (
        detail
        if isinstance(detail, str) and detail in _MODEL_VALIDATION_DETAILS
        else "other_schema_violation"
        if safe_code == "model_invalid_response" and safe_reason == "schema_violation"
        else None
    )
    attempt_count = error.get("attempt_count")
    safe_attempt_count = attempt_count if isinstance(attempt_count, int) and not isinstance(attempt_count, bool) and attempt_count in (1, 2, 3, 4) else None
    return safe_code, safe_stage, safe_reason, safe_attempt_count, safe_detail


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_COMPONENT_RE.sub("_", value or "").strip("._-")
    return (cleaned or fallback)[:160]


def _scope_catalog_is_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("scopes"), list):
        return False
    has_more = value.get("has_more")
    cursor = value.get("next_cursor")
    if not isinstance(has_more, bool) or (cursor is not None and not isinstance(cursor, str)):
        return False
    if isinstance(cursor, str) and ("\n" in cursor or "\r" in cursor):
        return False
    if (has_more and not cursor) or (not has_more and cursor is not None):
        return False
    for item in value["scopes"]:
        if not isinstance(item, Mapping):
            return False
        scope = item.get("scope")
        parent = item.get("parent")
        aliases = item.get("aliases")
        if not isinstance(scope, str) or not scope or "\n" in scope or "\r" in scope:
            return False
        if parent is not None and (not isinstance(parent, str) or "\n" in parent or "\r" in parent):
            return False
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias and "\n" not in alias and "\r" not in alias
            for alias in aliases
        ):
            return False
    return True


def _query_term_matches(query: str, term: str) -> bool:
    normalized_term = " ".join(term.casefold().strip().split())
    if not normalized_term:
        return False
    if _ASCII_QUERY_TERM_RE.fullmatch(normalized_term):
        return bool(
            re.search(
                r"(?<![a-z0-9])" + re.escape(normalized_term) + r"(?![a-z0-9])",
                query,
            )
        )
    return normalized_term in query


def _unique_query_scope(query: Any, catalog: Any) -> Optional[str]:
    """Find one unambiguous project scope named by the visible user text."""

    if not isinstance(query, str) or not query.strip() or not _scope_catalog_is_valid(catalog):
        return None
    query_text = " ".join(query.casefold().strip().split())
    matches: set[str] = set()
    for item in catalog.get("scopes", []):
        scope = item.get("scope")
        if not isinstance(scope, str) or not scope.startswith("project:"):
            continue
        terms = [scope, scope.split(":", 1)[1]]
        aliases = item.get("aliases", [])
        if isinstance(aliases, list):
            terms.extend(alias for alias in aliases if isinstance(alias, str))
        if any(_query_term_matches(query_text, term) for term in terms):
            matches.add(scope)
    return next(iter(matches)) if len(matches) == 1 else None


def _scope_context(
    value: Any,
    *,
    retrieval_id: Optional[str] = None,
    scope_hint: Optional[str] = None,
) -> tuple[str, int]:
    """Render a bounded Scope Map without per-memory identifiers or text."""

    is_mapping = isinstance(value, Mapping)
    scopes = value.get("scopes") if is_mapping else value
    if not isinstance(scopes, (list, tuple)):
        return "", 0
    if is_mapping and not _scope_catalog_is_valid(value):
        # Do not turn a malformed response into a misleading empty map.
        return "", 0
    has_more = value.get("has_more") if is_mapping else False
    next_cursor = value.get("next_cursor") if is_mapping else None
    cursor_text = (
        next_cursor
        if isinstance(next_cursor, str)
        and next_cursor
        and "\n" not in next_cursor
        and "\r" not in next_cursor
        else None
    )
    prefix = (
        "<memleaf-scope-map>\n"
        "Available memleaf memory scopes. For every visible user turn, call "
        "memleaf MCP search at least once using the current conversation and "
        "this map. Use list_todos instead of relevance search for global current-todo questions. "
        "Search/list_todos return directories; read only the selected memory when needed for ordinary "
        "relevance queries; for global todo queries, read every matching todo item. A no-match result is valid.\n"
    )
    if isinstance(retrieval_id, str) and retrieval_id:
        prefix = (
            prefix
            + f"For this turn, pass retrieval_id={retrieval_id} to memleaf "
            "search/read exactly as supplied; do not invent or reuse another "
            "turn's token.\n"
        )
    if isinstance(scope_hint, str) and scope_hint:
        prefix += (
            "The visible user text names one unique project scope; pass "
            f"scope={scope_hint} to search. Build the query from the user's "
            "business subject words, omitting MCP/tool/function names and "
            "generic workflow words.\n"
        )
    incomplete = bool(has_more)
    if has_more:
        pagination = "More scopes are available; fetch the next scope_catalog page"
        if cursor_text is not None:
            candidate = f"{pagination} with next_cursor={cursor_text}.\n"
            # Preserve the opaque cursor in full whenever the bounded preview
            # can carry it.  If a malformed/oversized cursor cannot fit, keep
            # the pagination state visible and mark the preview incomplete;
            # never emit a truncated cursor that cannot be used for paging.
            if len(prefix) + len(candidate) + len("\n</memleaf-scope-map>") <= _MAX_SCOPE_CHARS:
                pagination = candidate
            else:
                pagination = f"{pagination}; next_cursor is unavailable within this preview budget.\n"
                incomplete = True
        else:
            pagination = f"{pagination}; next_cursor is unavailable.\n"
            incomplete = True
        prefix += pagination
    elif is_mapping:
        prefix += "scope_catalog page has_more=False; next_cursor=None.\n"
    suffix = "\n</memleaf-scope-map>"
    lines: list[str] = []
    used = len(prefix) + len(suffix)
    marker_reserve = len(_SCOPE_MAP_INCOMPLETE) + 1
    for item in scopes:
        if len(lines) >= _MAX_SCOPE_ITEMS:
            incomplete = True
            break
        if not isinstance(item, Mapping):
            incomplete = True
            continue
        scope = item.get("scope")
        parent = item.get("parent")
        aliases = item.get("aliases")
        if not isinstance(scope, str) or not scope or "\n" in scope or "\r" in scope:
            incomplete = True
            continue
        # Scope identifiers are lookup keys.  Keep them byte-for-byte intact;
        # only omit the whole item when its complete ID cannot fit the budget.
        parent_text = ""
        if parent is None:
            pass
        elif isinstance(parent, str) and "\n" not in parent and "\r" not in parent:
            parent_text = parent
            if len(parent_text) > 96:
                parent_text = ""
                incomplete = True
        else:
            incomplete = True
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, (list, tuple)):
            incomplete = True
            aliases = []
        alias_text = []
        for alias in aliases:
            if not isinstance(alias, str) or not alias or "\n" in alias or "\r" in alias:
                incomplete = True
                continue
            if len(alias) <= 96:
                alias_text.append(alias)
            else:
                incomplete = True
        line = f"- {scope}"
        if parent_text:
            line += f" (parent: {parent_text})"
        if alias_text:
            line += f" [aliases: {', '.join(dict.fromkeys(alias_text))}]"
        addition = len(line) + 1
        if used + addition + marker_reserve > _MAX_SCOPE_CHARS:
            # Optional hierarchy/aliases may be too large even when the full
            # scope ID itself fits. Keep the ID and explicitly flag what was
            # omitted instead of dropping the entry silently.
            base_line = f"- {scope}"
            base_addition = len(base_line) + 1
            if line != base_line and used + base_addition + marker_reserve <= _MAX_SCOPE_CHARS:
                line = base_line
                addition = base_addition
                incomplete = True
            else:
                incomplete = True
                continue
        if used + addition > _MAX_SCOPE_CHARS:
            incomplete = True
            continue
        lines.append(line)
        used += addition
    if not lines:
        if scopes:
            incomplete = True
        else:
            lines.append("- (no registered scopes; search without a scope if appropriate)")
    if incomplete:
        addition = len(_SCOPE_MAP_INCOMPLETE) + 1
        if used + addition <= _MAX_SCOPE_CHARS:
            lines.append(_SCOPE_MAP_INCOMPLETE)
    return prefix + "\n".join(lines) + suffix, len(lines)


def _hermes_search_status(value: Any) -> str:
    """Classify an observed MCP search result without retaining its content."""

    if value is _CALL_FAILED or value is None:
        return "error"
    decoded: Any = _decode_hermes_tool_result(value)
    for _ in range(_MAX_TOOL_RESULT_LAYERS):
        if decoded is _MISSING_TOOL_RESULT:
            return "error"
        if isinstance(decoded, str):
            decoded = _decode_hermes_tool_result(decoded)
            continue
        if isinstance(decoded, list):
            text_item = next(
                (
                    item
                    for item in decoded
                    if isinstance(item, Mapping) and item.get("type") == "text"
                ),
                None,
            )
            if text_item is None:
                return "error"
            decoded = _decode_hermes_tool_result(text_item.get("text"))
            continue
        if not isinstance(decoded, Mapping):
            return "error"
        if decoded.get("isError") is True or decoded.get("error") is not None:
            return "error"

        status = decoded.get("status")
        results = decoded.get("results")
        if status is not None or results is not None:
            if status not in {"found", "no_match"} or not isinstance(results, list):
                return "error"
            valid_results = all(
                isinstance(item, Mapping)
                and set(item) in ({"memory_id", "title"}, {"memory_id", "title", "due_date"})
                and isinstance(item.get("memory_id"), str)
                and bool(item.get("memory_id"))
                and isinstance(item.get("title"), str)
                and bool(item.get("title"))
                and ("due_date" not in item or item.get("due_date") is None or isinstance(item.get("due_date"), str))
                for item in results
            )
            if not valid_results:
                return "error"
            if status == "found" and results:
                return "found"
            if status == "no_match" and not results:
                return "no_match"
            return "error"

        nested = next(
            (
                decoded[key]
                for key in ("structuredContent", "result", "content")
                if isinstance(decoded.get(key), (Mapping, list, str))
            ),
            _MISSING_TOOL_RESULT,
        )
        if nested is _MISSING_TOOL_RESULT:
            return "error"
        decoded = _decode_hermes_tool_result(nested)
    return "error"


def _decode_tool_value(value: Any) -> Any:
    """Decode the small JSON wrappers used by Hermes tool messages."""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _unwrap_untrusted_tool_result(value: str) -> tuple[Any, bool]:
    """Extract only the data section of Hermes' untrusted result wrapper."""

    text = value.strip()
    if not text.startswith(_UNTRUSTED_TOOL_RESULT_TAG):
        return value, False
    tag_tail = text[len(_UNTRUSTED_TOOL_RESULT_TAG) :]
    if tag_tail and tag_tail[0] not in " \t\r\n>":
        return value, False
    open_end = text.find(">", len(_UNTRUSTED_TOOL_RESULT_TAG))
    close_start = text.rfind(_UNTRUSTED_TOOL_RESULT_END)
    if open_end < 0 or open_end > 1024 or close_start <= open_end:
        return _MISSING_TOOL_RESULT, True
    if text[close_start + len(_UNTRUSTED_TOOL_RESULT_END) :].strip():
        return _MISSING_TOOL_RESULT, True
    inner = text[open_end + 1 : close_start]
    if len(inner) > _MAX_TOOL_RESULT_CHARS:
        return _MISSING_TOOL_RESULT, True
    sections = re.split(r"\r?\n[ \t]*\r?\n", inner, maxsplit=1)
    payload = sections[1] if len(sections) == 2 else ""
    payload = payload.strip()
    if not payload:
        return _MISSING_TOOL_RESULT, True
    return payload, True


def _decode_hermes_tool_result(value: Any) -> Any:
    """Boundedly decode JSON and the outer Hermes result safety wrapper."""

    decoded = value
    for _ in range(_MAX_TOOL_RESULT_LAYERS):
        if not isinstance(decoded, str):
            return decoded
        if len(decoded) > _MAX_TOOL_RESULT_CHARS:
            return None
        decoded, wrapped = _unwrap_untrusted_tool_result(decoded)
        if wrapped:
            if decoded is _MISSING_TOOL_RESULT:
                return decoded
            continue
        try:
            decoded = json.loads(decoded)
        except (TypeError, ValueError):
            return None
    return None if isinstance(decoded, str) else decoded


def _tool_call_parts(value: Any) -> Optional[dict[str, Any]]:
    """Return a normalized visible tool call without retaining its payload."""

    if not isinstance(value, Mapping):
        return None
    function = value.get("function")
    if not isinstance(function, Mapping):
        function = value
    name = function.get("name")
    arguments = function.get("arguments")
    call_id = value.get("id") or value.get("call_id") or function.get("id")
    if not isinstance(name, str):
        return None
    if name == "tool_call":
        wrapper = _decode_tool_value(arguments)
        if not isinstance(wrapper, Mapping):
            return None
        name = wrapper.get("name")
        arguments = wrapper.get("arguments")
    if not isinstance(name, str):
        return None
    arguments = _decode_tool_value(arguments)
    return {
        "name": name,
        "arguments": arguments if isinstance(arguments, Mapping) else None,
        "call_id": call_id if isinstance(call_id, str) and call_id else None,
    }


def _visible_tool_calls(messages: Optional[List[Dict[str, Any]]]) -> list[dict[str, Any]]:
    """Normalize chat-completions and Responses-style visible calls."""

    if not isinstance(messages, list):
        return []
    calls: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        candidates: list[Any] = []
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            candidates.extend(tool_calls)
        content = message.get("content")
        if isinstance(content, list):
            candidates.extend(
                item
                for item in content
                if isinstance(item, Mapping)
                and (
                    item.get("type") in {"function_call", "tool_call"}
                    or "function" in item
                )
            )
        if message.get("type") in {"function_call", "tool_call"}:
            candidates.append(message)
        for candidate in candidates:
            parts = _tool_call_parts(candidate)
            if parts is not None:
                calls.append(parts)
    return calls


def _visible_tool_results(messages: Optional[List[Dict[str, Any]]]) -> list[dict[str, Any]]:
    """Normalize visible tool outputs while keeping their payload in memory only."""

    if not isinstance(messages, list):
        return []
    results: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        candidates: list[Mapping[str, Any]] = []
        if message.get("role") == "tool" or message.get("type") in {
            "function_call_output",
            "tool_result",
        }:
            candidates.append(message)
        content = message.get("content")
        if isinstance(content, list):
            candidates.extend(
                item
                for item in content
                if isinstance(item, Mapping)
                and item.get("type") in {"function_call_output", "tool_result"}
            )
        tool_results = message.get("tool_results")
        if isinstance(tool_results, list):
            candidates.extend(item for item in tool_results if isinstance(item, Mapping))
        for candidate in candidates:
            call_id = (
                candidate.get("tool_call_id")
                or candidate.get("tool_use_id")
                or candidate.get("call_id")
                or candidate.get("id")
            )
            if not isinstance(call_id, str) or not call_id:
                call_id = None
            if "output" in candidate:
                payload = candidate.get("output")
            else:
                payload = candidate.get("content")
            results.append({
                "call_id": call_id,
                "name": candidate.get("name") if isinstance(candidate.get("name"), str) else None,
                "payload": payload,
            })
    return results


def _tool_result_for_call(
    call: Mapping[str, Any],
    calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
    used: set[int],
) -> Any:
    """Match one visible result to a call without exposing either payload."""

    call_id = call.get("call_id")
    if isinstance(call_id, str) and call_id:
        for index, result in enumerate(results):
            if index not in used and result.get("call_id") == call_id:
                used.add(index)
                return result.get("payload")
    name = call.get("name")
    for index, result in enumerate(results):
        if index in used:
            continue
        result_name = result.get("name")
        if result_name is not None and result_name != name:
            continue
        result_id = result.get("call_id")
        if result_id is not None and any(
            other.get("call_id") == result_id and other is not call for other in calls
        ):
            continue
        used.add(index)
        return result.get("payload")
    return _CALL_FAILED


def _tool_observation_key(call: Mapping[str, Any], ordinal: int) -> str:
    """Build a stable in-memory identity for one visible tool call.

    Hermes can pass the complete conversation back on every turn.  The
    retrieval token and result status are turn-relative, so they must not be
    part of the identity: otherwise an old successful read is reclassified as
    ``uncontrolled_success`` when a newer token is observed.  Call ids are
    stable when Hermes supplies them; the ordinal/arguments fallback keeps
    anonymous calls distinct within the cumulative message list.
    """

    arguments = call.get("arguments")
    try:
        arguments_text = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        arguments_text = ""
    call_id = call.get("call_id") if isinstance(call.get("call_id"), str) else ""
    name = call.get("name") if isinstance(call.get("name"), str) else ""
    if call_id:
        identity = f"{name}\x00call-id\x00{call_id}"
    else:
        identity = f"{name}\x00anonymous\x00{ordinal}\x00{arguments_text}"
    return sha256(identity.encode("utf-8")).hexdigest()


def _record_tool_observation(seen: Any, key: str) -> bool:
    """Record one observation key and report whether it was new."""

    if seen is None:
        return True
    if key in seen:
        return False
    if isinstance(seen, set):
        seen.add(key)
    else:
        seen[key] = None
    return True


def _hermes_read_status(value: Any) -> str:
    """Classify a visible read result without inspecting it in diagnostics."""

    if value is _CALL_FAILED or value is None:
        return "missing_result"
    decoded = _decode_hermes_tool_result(value)
    if decoded is _MISSING_TOOL_RESULT:
        return "missing_result"
    for _ in range(_MAX_TOOL_RESULT_LAYERS):
        if decoded is _MISSING_TOOL_RESULT:
            return "missing_result"
        if isinstance(decoded, str):
            decoded = _decode_hermes_tool_result(decoded)
            continue
        if isinstance(decoded, list):
            text_item = next(
                (
                    item
                    for item in decoded
                    if isinstance(item, Mapping) and item.get("type") == "text"
                ),
                None,
            )
            if text_item is None:
                return "error"
            decoded = _decode_hermes_tool_result(text_item.get("text"))
            continue
        if not isinstance(decoded, Mapping):
            return "error"
        if decoded.get("isError") is True or decoded.get("error") is not None:
            return "error"
        if isinstance(decoded.get("body"), str) and isinstance(decoded.get("memory_id"), str):
            return "ok"
        nested = next(
            (
                decoded[key]
                for key in ("structuredContent", "result", "content")
                if isinstance(decoded.get(key), (Mapping, list, str))
            ),
            _MISSING_TOOL_RESULT,
        )
        if nested is _MISSING_TOOL_RESULT:
            return "error"
        decoded = _decode_hermes_tool_result(nested)
    return "error"


def _file_tool_name(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    normalized = name.rsplit("__", 1)[-1].rsplit(".", 1)[-1].casefold()
    return normalized in {"search_files", "read_file", "file_search", "file_read"}


def _path_from_tool_arguments(arguments: Any) -> Optional[str]:
    if not isinstance(arguments, Mapping):
        return None
    for key in ("path", "file_path", "filepath", "file", "filename"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None



def _has_document_arguments(value: Any, depth: int = 0) -> bool:
    """Classify structural document handles in the standalone copied provider.

    Kept dependency-free; contract tests compare this adapter projection with
    Core's document_arguments. Tool names and shell command text are not used.
    """
    if depth > 4:
        return False
    if isinstance(value, Mapping):
        for key, item in list(value.items())[:32]:
            if key in {"path", "file", "file_path", "filepath", "filename", "file_id"}:
                if isinstance(item, str) and item.strip():
                    return True
            if key == "uri" and isinstance(item, str) and item.startswith("file://"):
                return True
            if isinstance(item, (Mapping, list, tuple)) and _has_document_arguments(item, depth + 1):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_document_arguments(item, depth + 1) for item in value[:32])
    return False


def _has_attachment_arguments(value: Any, depth: int = 0) -> bool:
    """Classify only explicit attachment handles in the standalone provider."""
    if depth > 4:
        return False
    if isinstance(value, Mapping):
        for key, item in list(value.items())[:32]:
            if key == "attachment_id" and isinstance(item, str) and item.strip():
                return True
            if isinstance(item, (Mapping, list, tuple)) and _has_attachment_arguments(item, depth + 1):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_attachment_arguments(item, depth + 1) for item in value[:32])
    return False


def _path_is_within(root: Optional[Path], value: Optional[str]) -> Optional[bool]:
    if root is None or value is None:
        return None
    try:
        candidate = Path(value).expanduser().resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _config_path(hermes_home: str | Path) -> Path:
    return Path(hermes_home).expanduser() / "memleaf.json"


def _version_value(value: Any) -> Optional[str]:
    """Return a bounded, log-safe version string or ``None``."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 64 or any(char.isspace() for char in value):
        return None
    return value


def _provider_manifest_version() -> Optional[str]:
    """Read the version from this copied provider's adjacent manifest."""

    try:
        text = Path(__file__).with_name("plugin.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeError, RuntimeError):
        return None
    match = _PROVIDER_VERSION_RE.search(text)
    return _version_value(match.group(1)) if match else None


def _default_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"




def _as_bool(value: Any, default: bool = True) -> bool:
    """Read a provider boolean without treating the string ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _memory_session_enabled(platform: Any, agent_context: Any) -> bool:
    """Return whether Hermes should expose this provider for the session."""

    normalized_platform = str(platform or "").strip().casefold()
    normalized_context = str(agent_context or "").strip().casefold()
    return (
        normalized_platform not in _DISABLED_PLATFORMS
        and normalized_context not in _DISABLED_AGENT_CONTEXTS
    )


def _bounded_timeout(value: Any, default: float, maximum: float) -> float:
    try:
        return max(1.0, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _error_type(error: BaseException) -> str:
    """Classify MCP failures without including their potentially sensitive text."""

    if isinstance(error, _MCPToolError):
        return "MCPToolError"
    if isinstance(error, TimeoutError):
        return "TimeoutError"
    if isinstance(error, (BrokenPipeError, EOFError, ConnectionError)):
        return "ProcessExited"
    message = str(error).casefold()
    if "process exited" in message or "process is not running" in message:
        return "ProcessExited"
    if "mcp tool" in message or "mcp error" in message:
        return "MCPToolError"
    return type(error).__name__


def _visible_message_text(message: Any) -> str:
    """Extract only explicit visible user text for a turn-start fingerprint."""

    if isinstance(message, str):
        return message
    if not isinstance(message, Mapping):
        return ""
    role = message.get("role")
    if role is not None and str(role).casefold() not in {"user", "human"}:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _visible_fingerprint(user_content: str) -> str:
    return sha256(user_content.encode("utf-8")).hexdigest()[:16]


def _turn_id(turn_number: Optional[int], user_content: str, assistant_content: str) -> str:
    digest = sha256(f"{user_content}\x00{assistant_content}".encode("utf-8")).hexdigest()[:16]
    if isinstance(turn_number, int) and not isinstance(turn_number, bool) and turn_number > 0:
        return f"turn-{turn_number:06d}-{digest}"
    return f"turn-fallback-{digest}"


def _load_config(hermes_home: str | Path) -> dict[str, Any]:
    config = {
        "vault": _DEFAULT_VAULT,
        "command": _DEFAULT_COMMAND,
        "timeout": _DEFAULT_TIMEOUT,
        "process_timeout": _DEFAULT_PROCESS_TIMEOUT,
        "auto_process": True,
    }
    path = _config_path(hermes_home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        raw = {}
    if isinstance(raw, Mapping):
        config.update({key: value for key, value in raw.items() if value is not None})

    vault = str(config.get("vault") or _DEFAULT_VAULT).strip() or _DEFAULT_VAULT
    command = str(config.get("command") or _DEFAULT_COMMAND).strip() or _DEFAULT_COMMAND
    timeout = _bounded_timeout(config.get("timeout", _DEFAULT_TIMEOUT), _DEFAULT_TIMEOUT, _MAX_TIMEOUT)
    process_timeout = _bounded_timeout(
        config.get("process_timeout", _DEFAULT_PROCESS_TIMEOUT),
        _DEFAULT_PROCESS_TIMEOUT,
        _MAX_PROCESS_TIMEOUT,
    )
    return {
        "vault": vault,
        "command": command,
        "timeout": timeout,
        "process_timeout": process_timeout,
        "auto_process": _as_bool(config.get("auto_process"), True),
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _mcp_creationflags() -> int:
    """Hide the provider-owned MCP child console on Windows only."""

    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

__all__ = [name for name in globals() if not name.startswith('__')]
