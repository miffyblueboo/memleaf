"""Hermes memory-provider adapter for the local memleaf MCP service.

The adapter deliberately talks to the public stdio MCP boundary instead of
importing Hermes internals or reaching into memleaf's private implementation.
This keeps the provider installable as a Hermes user plugin while reusing the
same local ``~/.memleaf`` vault as the standalone MCP server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import queue
import shutil
import subprocess
import threading
import time
from collections import OrderedDict, deque
from hashlib import sha256
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

from agent.memory_provider import MemoryProvider, RecallStatus

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
            if isinstance(attempt_count, int) and not isinstance(attempt_count, bool) and attempt_count in (1, 2, 3)
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
    safe_attempt_count = attempt_count if isinstance(attempt_count, int) and not isinstance(attempt_count, bool) and attempt_count in (1, 2, 3) else None
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


class _MCPClient:
    """Small synchronous JSON-RPC client for memleaf's stdio MCP server."""

    def __init__(
        self,
        command: str,
        vault: str,
        timeout: float,
        process_timeout: float = _DEFAULT_PROCESS_TIMEOUT,
    ) -> None:
        self.command = command
        self.vault = vault
        self.timeout = _bounded_timeout(timeout, _DEFAULT_TIMEOUT, _MAX_TIMEOUT)
        self.process_timeout = _bounded_timeout(
            process_timeout,
            _DEFAULT_PROCESS_TIMEOUT,
            _MAX_PROCESS_TIMEOUT,
        )
        self._process: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: Optional[queue.Queue[object]] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._next_id = 1
        self._lock = threading.RLock()
        self.server_version: Optional[str] = None

    def _resolve_command(self) -> str:
        path = Path(self.command).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        resolved = shutil.which(self.command)
        if resolved:
            return resolved
        known = Path.home() / ".local" / "bin" / "memleaf-mcp"
        if known.is_file() and os.access(known, os.X_OK):
            return str(known)
        raise FileNotFoundError("memleaf-mcp executable is unavailable")

    def _start_stdout_reader_locked(self, process: subprocess.Popen[str]) -> None:
        """Read child stdout on a thread so Windows pipes can use timeouts."""

        output_queue: queue.Queue[object] = queue.Queue()
        self._stdout_queue = output_queue

        def reader() -> None:
            stream = process.stdout
            if stream is None:
                output_queue.put(_MCP_PIPE_EOF)
                return
            try:
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    output_queue.put(line)
            except (OSError, ValueError):
                pass
            finally:
                output_queue.put(_MCP_PIPE_EOF)

        thread = threading.Thread(
            target=reader,
            name="memleaf-mcp-stdout",
            daemon=True,
        )
        self._stdout_thread = thread
        thread.start()

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._close_locked()
        self._process = subprocess.Popen(
            [self._resolve_command(), "--vault", str(Path(self.vault).expanduser().resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
        )
        self._start_stdout_reader_locked(self._process)
        initialize_result = self._request_locked(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hermes-memleaf", "version": "0.1.0"},
            },
        )
        server_info = (
            initialize_result.get("serverInfo")
            if isinstance(initialize_result, Mapping)
            else None
        )
        if isinstance(server_info, Mapping):
            self.server_version = _version_value(server_info.get("version"))
        self._send_locked({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send_locked(self, message: Mapping[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("memleaf MCP process is not running")
        self._process.stdin.write(json.dumps(dict(message), ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    def _read_response_locked(self, request_id: int, timeout: Optional[float] = None) -> dict[str, Any]:
        if self._process is None or self._stdout_queue is None:
            raise RuntimeError("memleaf MCP process is not running")
        output_queue = self._stdout_queue
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("memleaf MCP request timed out")
            try:
                line = output_queue.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError("memleaf MCP request timed out") from error
            if line is _MCP_PIPE_EOF:
                raise RuntimeError("memleaf MCP process exited unexpectedly")
            if not isinstance(line, str):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), Mapping):
                raise RuntimeError("MCP error")
            result = message.get("result")
            return result if isinstance(result, dict) else {"result": result}

    def _request_locked(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send_locked({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        return self._read_response_locked(request_id, timeout if timeout is not None else self.timeout)

    def _close_locked(self) -> None:
        process = self._process
        thread = self._stdout_thread
        self._process = None
        self._stdout_queue = None
        self._stdout_thread = None
        self.server_version = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        with self._lock:
            try:
                self._start_locked()
                result = self._request_locked(
                    "tools/call",
                    {"name": name, "arguments": dict(arguments)},
                    timeout=self.process_timeout if name == "process" else self.timeout,
                )
            except Exception:
                self._close_locked()
                raise

            if result.get("isError"):
                error_fields = _mcp_error_fields(result) or ("model_failed", None, None, None, None)
                raise _MCPToolError(*error_fields)
            structured = result.get("structuredContent")
            if isinstance(structured, Mapping) and "result" in structured:
                return structured["result"]
            if structured is not None:
                return structured
            content = result.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, Mapping) and item.get("type") == "text":
                        try:
                            return json.loads(str(item.get("text", "")))
                        except ValueError:
                            break
            return None


def _resolve_vault(config: Mapping[str, Any]) -> Path:
    return Path(str(config.get("vault") or _DEFAULT_VAULT)).expanduser().resolve()


def _resolve_command(config: Mapping[str, Any]) -> Optional[str]:
    candidate = str(config.get("command") or _DEFAULT_COMMAND).strip()
    path = Path(candidate).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return shutil.which(candidate) or (
        str(Path.home() / ".local" / "bin" / "memleaf-mcp")
        if (Path.home() / ".local" / "bin" / "memleaf-mcp").is_file()
        else None
    )


class MemleafMemoryProvider(MemoryProvider):
    """Use the local memleaf vault as Hermes' single external provider."""

    def __init__(self) -> None:
        self._hermes_home = ""
        self._session_id = ""
        self._client: Optional[_MCPClient] = None
        self._write_enabled = True
        self._auto_process = True
        self._sync_lock = threading.RLock()
        self._pending_turn_numbers: "OrderedDict[str, Deque[Tuple[str, int]]]" = OrderedDict()
        self._pending_turn_count = 0
        self._turn_ids_by_pair: "OrderedDict[Tuple[str, str], str]" = OrderedDict()
        self._last_recall: Optional[RecallStatus] = None
        self._last_call_error: Optional[dict[str, str]] = None
        # The provider runs capture/process in a background worker. Keep the
        # last automatic-sync outcome so the next user turn can distinguish a
        # pending failed process from a successful extraction. This is control
        # state only; it never becomes memory content.
        self._last_auto_process_failure: Optional[dict[str, str]] = None
        self._last_auto_process_deferred: Optional[dict[str, int]] = None
        # Hermes exposes no final-answer blocking hook.  Keep this adapter's
        # retrieval state only when it was initialized from an explicit local
        # provider config; tests and disabled/manual instances must not request
        # managed MCP tokens merely because ``on_turn_start`` is called.
        self._gate_enabled = False
        # Hermes cannot enforce a final-answer gate and must not import Core
        # from its independent plugin environment.  The token returned by
        # scope_catalog is therefore retained only as bounded provider state
        # and echoed back in the visible MCP instructions for this turn.
        self._retrieval_ids_by_turn: "OrderedDict[Tuple[str, int], str]" = OrderedDict()
        self._gate_turn_ids: "OrderedDict[Tuple[str, int], str]" = OrderedDict()
        self._active_turn_numbers: "OrderedDict[str, int]" = OrderedDict()
        self._active_retrieval_ids: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._observed_tool_call_keys: "OrderedDict[str, None]" = OrderedDict()
        self._version_warning_emitted = False
        # Hermes keeps one provider instance alive while compression rotates
        # the physical session id.  Keep only a bounded alias chain so a
        # callback already queued for the parent can still resolve to the
        # live continuation.
        self._session_aliases: "OrderedDict[str, str]" = OrderedDict()
        # A compression child may be visible before the control RPC can be
        # persisted.  Keep that child captureable, but never process it until
        # its parent link is confirmed.
        self._pending_lineage: Deque[dict[str, Any]] = deque()
        # A physical session can be captured while lineage is pending.  Keep
        # those sessions separate from aliases so their inbox is processed
        # under the original session id after the chain is restored.
        self._deferred_process_sessions: "OrderedDict[str, None]" = OrderedDict()
        self._last_retrieval_observation = "unknown"
        self._last_retrieval_audit = "SEARCH_UNKNOWN"

    @property
    def name(self) -> str:
        return "memleaf"

    def _config(self) -> dict[str, Any]:
        return _load_config(self._hermes_home or _default_hermes_home())

    def _gate_id_for_turn(self, session_id: str, turn_number: Any) -> Optional[str]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        if isinstance(turn_number, bool) or not isinstance(turn_number, int) or turn_number <= 0:
            return None
        return self._retrieval_ids_by_turn.get((session_id, turn_number))

    def _current_gate_id(self, session_id: str) -> Optional[str]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        return self._active_retrieval_ids.get(session_id)

    def _current_turn_number(self, session_id: str) -> Optional[int]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        return self._active_turn_numbers.get(session_id)

    def _gate_turn_id(self, session_id: str, turn_number: Any) -> Optional[str]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        if isinstance(turn_number, bool) or not isinstance(turn_number, int) or turn_number <= 0:
            return None
        return self._gate_turn_ids.get((session_id, turn_number))

    def _canonical_session_id(self, session_id: Any) -> str:
        """Resolve a compression continuation to its current session id."""

        candidate = _safe_component(str(session_id or ""), "hermes-session")
        with self._sync_lock:
            seen: set[str] = set()
            while candidate not in seen:
                seen.add(candidate)
                successor = self._session_aliases.get(candidate)
                if not isinstance(successor, str) or not successor:
                    break
                candidate = successor
            return candidate

    def _migrate_session_state(self, old_session_id: str, new_session_id: str) -> None:
        """Move in-flight turn state across a non-reset Hermes rotation."""

        if not old_session_id or not new_session_id or old_session_id == new_session_id:
            return
        self._session_aliases[old_session_id] = new_session_id
        self._session_aliases.move_to_end(old_session_id)
        while len(self._session_aliases) > _MAX_SESSION_ALIASES:
            self._session_aliases.popitem(last=False)

        for fingerprint, queue in list(self._pending_turn_numbers.items()):
            migrated: deque[Tuple[str, int]] = deque()
            seen: set[Tuple[str, int]] = set()
            for queued_session, queued_number in queue:
                target_session = new_session_id if queued_session == old_session_id else queued_session
                item = (target_session, queued_number)
                if item not in seen:
                    migrated.append(item)
                    seen.add(item)
            if migrated:
                self._pending_turn_numbers[fingerprint] = migrated
            else:
                del self._pending_turn_numbers[fingerprint]
        self._pending_turn_count = sum(len(queue) for queue in self._pending_turn_numbers.values())

        for pair_key in list(self._turn_ids_by_pair):
            if pair_key[0] != old_session_id:
                continue
            target_key = (new_session_id, pair_key[1])
            value = self._turn_ids_by_pair.pop(pair_key)
            self._turn_ids_by_pair.setdefault(target_key, value)
            self._turn_ids_by_pair.move_to_end(target_key)

        for state_map in (self._retrieval_ids_by_turn, self._gate_turn_ids):
            for key in list(state_map):
                if key[0] != old_session_id:
                    continue
                target_key = (new_session_id, key[1])
                value = state_map.pop(key)
                state_map.setdefault(target_key, value)
                state_map.move_to_end(target_key)

        turn_number = self._active_turn_numbers.pop(old_session_id, None)
        if turn_number is not None:
            self._active_turn_numbers.setdefault(new_session_id, turn_number)
        retrieval_id = self._active_retrieval_ids.pop(old_session_id, None)
        if retrieval_id is not None:
            if self._active_retrieval_ids.get(new_session_id) is None:
                self._active_retrieval_ids[new_session_id] = retrieval_id
        if new_session_id in self._active_turn_numbers:
            self._active_turn_numbers.move_to_end(new_session_id)
        if new_session_id in self._active_retrieval_ids:
            self._active_retrieval_ids.move_to_end(new_session_id)

        for attribute in ("_last_auto_process_failure", "_last_auto_process_deferred"):
            value = getattr(self, attribute)
            if isinstance(value, Mapping) and value.get("session_id") == old_session_id:
                updated = dict(value)
                updated["session_id"] = new_session_id
                setattr(self, attribute, updated)

    def _drop_session_aliases(self, *session_ids: str) -> None:
        targets = {value for value in session_ids if value}
        changed = True
        while changed:
            changed = False
            for key, value in self._session_aliases.items():
                if key in targets or value in targets:
                    before = len(targets)
                    targets.update((key, value))
                    changed = len(targets) != before
        for key, value in list(self._session_aliases.items()):
            if key in targets or value in targets:
                del self._session_aliases[key]

    @staticmethod
    def _lineage_result_valid(result: Any, arguments: Mapping[str, Any]) -> bool:
        if not isinstance(result, Mapping):
            return False
        if arguments.get("reset") is True:
            return (
                result.get("session_id") == arguments.get("session_id")
                and isinstance(result.get("cleared"), bool)
            )
        return (
            result.get("linked") is True
            and result.get("session_id") == arguments.get("session_id")
            and result.get("parent_session_id") == arguments.get("parent_session_id")
        )

    def _remember_pending_lineage(self, arguments: Mapping[str, Any], attempts: int) -> bool:
        pending = dict(arguments)
        pending["attempts"] = attempts
        with self._sync_lock:
            if len(self._pending_lineage) >= _MAX_SESSION_ALIASES:
                logger.warning(
                    "memleaf provider session lineage queue is full; automatic process remains deferred",
                )
                return False
            self._pending_lineage.append(pending)
            return True

    def _defer_process_session(self, session_id: str) -> None:
        if not session_id:
            return
        with self._sync_lock:
            if session_id in self._deferred_process_sessions:
                return
            if len(self._deferred_process_sessions) >= _MAX_DEFERRED_PROCESS_SESSIONS:
                logger.warning(
                    "memleaf provider deferred process queue is full; session remains retryable in inbox",
                )
                return
            self._deferred_process_sessions[session_id] = None

    def _record_auto_process_failure(self, session_id: str) -> None:
        with self._sync_lock:
            error = self._last_call_error or {}
            self._last_auto_process_failure = {
                "session_id": session_id,
                "error_code": str(error.get("error_code") or "model_failed"),
                "error_stage": str(error.get("error_stage") or "process"),
            }
            self._last_auto_process_deferred = None

    def _process_session(self, session_id: str, *, turn_id: str = "") -> Any:
        """Process one physical session and keep failed work retryable."""

        processed = self._call(
            "process",
            {"source": "hermes", "session_id": session_id},
            stage="process",
            session_id=session_id,
            turn_id=turn_id,
        )
        if processed is _CALL_FAILED:
            if turn_id:
                self._defer_process_session(session_id)
            self._record_auto_process_failure(session_id)
            logger.warning(
                "memleaf provider auto-process failed for hermes/%s; queue retained",
                session_id,
            )
            return _CALL_FAILED
        with self._sync_lock:
            self._deferred_process_sessions.pop(session_id, None)
        return processed

    def _process_deferred_sessions(self, current_session: str, turn_id: str) -> None:
        """Process deferred physical sessions before the current continuation."""

        with self._sync_lock:
            queued_sessions = [
                session_id
                for session_id in self._deferred_process_sessions
                if session_id != current_session
            ]
        for physical_session in queued_sessions:
            if self._process_session(physical_session) is _CALL_FAILED:
                return

        processed = self._process_session(current_session, turn_id=turn_id)
        if processed is _CALL_FAILED:
            return
        with self._sync_lock:
            deferred = self._process_deferred_counts(processed)
            self._last_auto_process_failure = None
            if deferred is None or (deferred[0] <= 0 and deferred[1] <= 0):
                self._last_auto_process_deferred = None
            else:
                self._last_auto_process_deferred = {
                    "session_id": current_session,
                    "deferred_candidates": deferred[0],
                    "deferred_inbox_turns": deferred[1],
                }
        if deferred is not None and (deferred[0] > 0 or deferred[1] > 0):
            logger.info(
                "memleaf provider auto-process deferred scope work for hermes/%s: candidates=%d inbox_turns=%d",
                current_session,
                deferred[0],
                deferred[1],
            )

    def _retry_pending_lineage(self, session_id: str) -> bool:
        while True:
            with self._sync_lock:
                pending_queue = self._pending_lineage
                if not pending_queue:
                    return True
                # The tail is always the current compression continuation.
                # A non-continuous session switch clears the queue before this
                # point.  If it does not match, fail closed rather than letting
                # any unresolved link reach automatic process.
                if pending_queue[-1].get("session_id") != session_id:
                    logger.warning(
                        "memleaf provider session lineage belongs to another hermes session; automatic process deferred",
                    )
                    return False
                pending = dict(pending_queue[0])
                attempts = pending.get("attempts", 0)
                if isinstance(attempts, bool) or not isinstance(attempts, int):
                    attempts = 0
                if attempts > _MAX_LINEAGE_RETRIES:
                    logger.warning(
                        "memleaf provider session lineage retries exhausted for hermes/%s; automatic process deferred",
                        pending.get("session_id", session_id),
                    )
                    return False
                arguments = {
                    key: value
                    for key, value in pending.items()
                    if key in {"source", "session_id", "parent_session_id", "reset"}
                }
                next_attempt = attempts + 1

            result = self._call(
                "session_lineage",
                arguments,
                stage="session_lineage_retry",
                session_id=str(arguments.get("session_id") or session_id),
            )
            succeeded = result is not _CALL_FAILED and self._lineage_result_valid(result, arguments)
            with self._sync_lock:
                current = self._pending_lineage
                if not current or not all(
                    current[0].get(key) == value for key, value in arguments.items()
                ) or current[0].get("attempts") != attempts:
                    return False
                if succeeded:
                    current.popleft()
                    if not current:
                        return True
                    continue
                current[0]["attempts"] = next_attempt
            logger.warning(
                "memleaf provider session lineage pending for hermes/%s; automatic process deferred",
                arguments.get("session_id", session_id),
            )
            return False

    def is_available(self) -> bool:
        config = _load_config(_default_hermes_home())
        return _resolve_command(config) is not None and _resolve_vault(config).is_dir()

    def unavailable_reason(self) -> str:
        config = _load_config(_default_hermes_home())
        if _resolve_command(config) is None:
            return "Install memleaf and expose the memleaf-mcp executable."
        if not _resolve_vault(config).is_dir():
            return f"memleaf vault does not exist: {_resolve_vault(config)}"
        return ""

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "vault",
                "description": "memleaf vault directory",
                "default": _DEFAULT_VAULT,
            },
            {
                "key": "auto_process",
                "description": "Automatically process each complete visible Hermes turn",
                "default": True,
                "choices": [True, False],
            },
            {
                "key": "timeout",
                "description": "Short MCP timeout for capture, stats, and context requests",
                "default": _DEFAULT_TIMEOUT,
            },
            {
                "key": "process_timeout",
                "description": "MCP timeout for the model-backed process request",
                "default": _DEFAULT_PROCESS_TIMEOUT,
            },
        ]

    def save_config(self, values: Mapping[str, Any], hermes_home: str) -> None:
        path = _config_path(hermes_home)
        existing: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            pass
        vault = str(values.get("vault") or existing.get("vault") or _DEFAULT_VAULT).strip()
        auto_process = _as_bool(values.get("auto_process", existing.get("auto_process")), True)
        timeout = _bounded_timeout(
            values.get("timeout", existing.get("timeout", _DEFAULT_TIMEOUT)),
            _DEFAULT_TIMEOUT,
            _MAX_TIMEOUT,
        )
        process_timeout = _bounded_timeout(
            values.get("process_timeout", existing.get("process_timeout", _DEFAULT_PROCESS_TIMEOUT)),
            _DEFAULT_PROCESS_TIMEOUT,
            _MAX_PROCESS_TIMEOUT,
        )
        _write_json(
            path,
            {
                **existing,
                "vault": vault or _DEFAULT_VAULT,
                "auto_process": auto_process,
                "timeout": timeout,
                "process_timeout": process_timeout,
            },
        )

    def get_status_config(self, provider_config: Mapping[str, Any]) -> dict[str, Any]:
        config = _load_config(self._hermes_home or _default_hermes_home())
        return {
            "vault": str(_resolve_vault(config)),
            "mcp_command": _resolve_command(config) or str(config.get("command", _DEFAULT_COMMAND)),
            "auto_process": config["auto_process"],
            "timeout": config["timeout"],
            "process_timeout": config["process_timeout"],
        }

    @staticmethod
    def _log_stage(
        stage: str,
        *,
        started_at: float,
        status: str,
        session_id: str = "",
        turn_id: str = "",
        error_type: str = "none",
        error_code: str = "",
        error_stage: str = "",
        validation_reason: str = "",
        validation_detail: str = "",
        attempt_count: Optional[int] = None,
    ) -> None:
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3):
            attempt_count = None
        logger.info(
            "memleaf stage=%s duration_ms=%d status=%s error_type=%s error_code=%s error_stage=%s validation_reason=%s validation_detail=%s attempt_count=%s source=hermes session=%s turn=%s",
            stage,
            max(0, int((time.monotonic() - started_at) * 1000)),
            status,
            error_type,
            error_code or "none",
            error_stage or "none",
            validation_reason or "none",
            validation_detail or "none",
            attempt_count if attempt_count is not None else "none",
            _safe_component(session_id, "none") if session_id else "none",
            _safe_component(turn_id, "none") if turn_id else "none",
        )

    def _check_version_sync(self) -> None:
        """Warn when the copied provider and MCP core came from different releases."""

        client = self._client
        if client is None:
            return
        # Test doubles and older host integrations may not expose the MCP
        # initialize metadata.  The real client always does, so only suppress
        # this check for a double that has no version attribute at all.
        client_type = _MCPClient
        is_real_client = isinstance(client_type, type) and isinstance(client, client_type)
        client_fields = getattr(client, "__dict__", {})
        if not is_real_client and not (
            isinstance(client_fields, Mapping) and "server_version" in client_fields
        ):
            return
        provider_version = _provider_manifest_version()
        core_version = _version_value(getattr(client, "server_version", None))
        if provider_version is None or core_version is None:
            if not self._version_warning_emitted:
                logger.warning(
                    "memleaf provider/core version check unavailable "
                    "(provider=%s core=%s); do not assume they are synchronized. "
                    "Run: %s",
                    provider_version or "unknown",
                    core_version or "unknown",
                    _UPDATE_COMMAND,
                )
                self._version_warning_emitted = True
            return
        if provider_version == core_version:
            return
        if not self._version_warning_emitted:
            logger.warning(
                "memleaf provider/core version mismatch (provider=%s core=%s). "
                "Run: %s",
                provider_version,
                core_version,
                _UPDATE_COMMAND,
            )
            self._version_warning_emitted = True

    def _queue_turn_number(self, turn_number: Any, message: Any) -> None:
        if isinstance(turn_number, bool) or not isinstance(turn_number, int) or turn_number <= 0:
            return
        visible_user = _visible_message_text(message)
        if not visible_user.strip():
            return
        fingerprint = _visible_fingerprint(visible_user)
        session_id = self._canonical_session_id(self._session_id)
        with self._sync_lock:
            queue = self._pending_turn_numbers.get(fingerprint)
            if queue is None:
                queue = deque()
                self._pending_turn_numbers[fingerprint] = queue
            queue.append((session_id, turn_number))
            self._pending_turn_numbers.move_to_end(fingerprint)
            self._pending_turn_count += 1
            while self._pending_turn_count > _MAX_PENDING_TURN_NUMBERS:
                _, removed = self._pending_turn_numbers.popitem(last=False)
                self._pending_turn_count -= len(removed)

    def _take_turn_number(self, session_id: str, user_content: str) -> Optional[int]:
        session_id = self._canonical_session_id(session_id)
        fingerprint = _visible_fingerprint(user_content)
        with self._sync_lock:
            queue = self._pending_turn_numbers.get(fingerprint)
            if not queue:
                return None
            selected_index = None
            selected_number = None
            for index, (queued_session, queued_number) in enumerate(queue):
                if queued_session == session_id:
                    selected_index = index
                    selected_number = queued_number
                    break
            if selected_index is None:
                return None
            values = list(queue)
            del values[selected_index]
            self._pending_turn_count -= 1
            if values:
                self._pending_turn_numbers[fingerprint] = deque(values)
                self._pending_turn_numbers.move_to_end(fingerprint)
            else:
                del self._pending_turn_numbers[fingerprint]
            return selected_number

    def _discard_turn_number(self, session_id: str, user_content: str, turn_number: int) -> None:
        session_id = self._canonical_session_id(session_id)
        fingerprint = _visible_fingerprint(user_content)
        with self._sync_lock:
            queue = self._pending_turn_numbers.get(fingerprint)
            if not queue:
                return
            values = list(queue)
            for index, (queued_session, queued_number) in enumerate(values):
                if queued_session == session_id and queued_number == turn_number:
                    del values[index]
                    self._pending_turn_count -= 1
                    break
            else:
                return
            if values:
                self._pending_turn_numbers[fingerprint] = deque(values)
                self._pending_turn_numbers.move_to_end(fingerprint)
            else:
                del self._pending_turn_numbers[fingerprint]

    def _clear_session_turn_state(self, session_id: str) -> None:
        if not session_id:
            return
        for fingerprint in list(self._pending_turn_numbers):
            queue = self._pending_turn_numbers[fingerprint]
            values = [item for item in queue if item[0] != session_id]
            self._pending_turn_count -= len(queue) - len(values)
            if values:
                self._pending_turn_numbers[fingerprint] = deque(values)
            else:
                del self._pending_turn_numbers[fingerprint]
        for pair_key in list(self._turn_ids_by_pair):
            if pair_key[0] == session_id:
                del self._turn_ids_by_pair[pair_key]

    def _resolve_turn_id(
        self,
        session_id: str,
        turn_number: Optional[int],
        user_content: str,
        assistant_content: str,
    ) -> str:
        pair_digest = sha256(f"{user_content}\x00{assistant_content}".encode("utf-8")).hexdigest()[:16]
        pair_key = (session_id, pair_digest)
        existing = self._turn_ids_by_pair.get(pair_key)
        if existing is not None and turn_number is None:
            self._turn_ids_by_pair.move_to_end(pair_key)
            return existing
        resolved = _turn_id(turn_number, user_content, assistant_content)
        if existing is not None and existing == resolved:
            self._turn_ids_by_pair.move_to_end(pair_key)
            return existing
        self._turn_ids_by_pair[pair_key] = resolved
        self._turn_ids_by_pair.move_to_end(pair_key)
        while len(self._turn_ids_by_pair) > _MAX_PENDING_TURN_NUMBERS:
            self._turn_ids_by_pair.popitem(last=False)
        return resolved

    def on_turn_start(self, turn_number: Any, message: Any = None, **kwargs: Any) -> None:
        """Remember only a bounded user-text fingerprint for later ``sync_turn``."""

        del kwargs
        if not self._write_enabled:
            return
        if self._gate_enabled:
            if isinstance(turn_number, int) and not isinstance(turn_number, bool) and turn_number > 0:
                # The token is created by the MCP server during this turn's
                # scope_catalog call.  Clearing the active value here prevents
                # a skipped prefetch from reusing the previous turn's token.
                self._active_turn_numbers[self._session_id] = turn_number
                self._active_turn_numbers.move_to_end(self._session_id)
                self._active_retrieval_ids[self._session_id] = None
                self._active_retrieval_ids.move_to_end(self._session_id)
                visible_user = _visible_message_text(message)
                self._gate_turn_ids[(self._session_id, turn_number)] = (
                    f"turn-{turn_number:06d}-{_visible_fingerprint(visible_user)}"
                )
                self._gate_turn_ids.move_to_end((self._session_id, turn_number))
                while len(self._active_turn_numbers) > _MAX_PENDING_TURN_NUMBERS:
                    self._active_turn_numbers.popitem(last=False)
                while len(self._active_retrieval_ids) > _MAX_PENDING_TURN_NUMBERS:
                    self._active_retrieval_ids.popitem(last=False)
                while len(self._gate_turn_ids) > _MAX_PENDING_TURN_NUMBERS:
                    self._gate_turn_ids.popitem(last=False)
                self._last_retrieval_observation = "not_observed"
        self._queue_turn_number(turn_number, message)

    def on_session_switch(
        self,
        new_session_id: str,
        reset: Any = False,
        rewound: Any = False,
        **kwargs: Any,
    ) -> None:
        """Update session identity and discard state only for reset/rewind."""

        reason = kwargs.get("reason")
        parent_session_id = kwargs.get("parent_session_id")
        old_session_id = self._session_id
        next_session_id = _safe_component(str(new_session_id or ""), "hermes-session")
        parent_session = (
            _safe_component(str(parent_session_id), "hermes-session")
            if parent_session_id
            else ""
        )
        lineage_args: Optional[dict[str, Any]] = None
        with self._sync_lock:
            continuous = (
                not _as_bool(reset, False)
                and not _as_bool(rewound, False)
                and next_session_id != old_session_id
                and (
                    parent_session == old_session_id
                    or (reason == "compression" and not parent_session)
                )
            )
            if continuous:
                self._migrate_session_state(parent_session or old_session_id, next_session_id)
            self._session_id = next_session_id
            if not continuous:
                self._pending_lineage.clear()
                self._deferred_process_sessions.clear()
                self._observed_tool_call_keys.clear()
            if _as_bool(reset, False) or _as_bool(rewound, False):
                self._clear_session_turn_state(old_session_id or next_session_id)
                for key in list(self._retrieval_ids_by_turn):
                    if key[0] == (old_session_id or next_session_id):
                        del self._retrieval_ids_by_turn[key]
                for key in list(self._gate_turn_ids):
                    if key[0] == (old_session_id or next_session_id):
                        del self._gate_turn_ids[key]
                self._active_turn_numbers.pop(old_session_id or next_session_id, None)
                self._active_retrieval_ids.pop(old_session_id or next_session_id, None)
                self._drop_session_aliases(old_session_id, next_session_id)
            if self._gate_enabled:
                if not continuous:
                    self._active_turn_numbers.pop(next_session_id, None)
                    self._active_retrieval_ids.pop(next_session_id, None)
                    for key in list(self._gate_turn_ids):
                        if key[0] == next_session_id:
                            del self._gate_turn_ids[key]
            if self._gate_enabled and continuous and (parent_session or old_session_id):
                lineage_args = {
                    "source": "hermes",
                    "session_id": next_session_id,
                    "parent_session_id": parent_session or old_session_id,
                }
            elif self._gate_enabled and (_as_bool(reset, False) or _as_bool(rewound, False)):
                lineage_args = {
                    "source": "hermes",
                    "session_id": next_session_id,
                    "reset": True,
                }
        if lineage_args is not None:
            with self._sync_lock:
                has_pending = bool(self._pending_lineage)
            if has_pending:
                # Preserve the ordered parent link.  A grandchild must not
                # overwrite a failed child link or bypass it on the next sync.
                queued = self._remember_pending_lineage(lineage_args, 0)
                logger.warning(
                    "memleaf provider session lineage %s for hermes/%s; parent link is pending",
                    "queued" if queued else "not queued because the queue is full",
                    next_session_id,
                )
            else:
                linked = self._call(
                    "session_lineage",
                    lineage_args,
                    stage="session_lineage",
                    session_id=next_session_id,
                )
                if linked is not _CALL_FAILED and self._lineage_result_valid(linked, lineage_args):
                    return
                self._remember_pending_lineage(lineage_args, 1)
                logger.warning(
                    "memleaf provider session lineage update failed for hermes/%s; automatic process deferred",
                    next_session_id,
                )

    def initialize(self, session_id: str, **kwargs) -> None:
        started_at = time.monotonic()
        self._hermes_home = str(kwargs.get("hermes_home") or _default_hermes_home())
        self._session_id = _safe_component(session_id, "hermes-session")
        with self._sync_lock:
            self._last_call_error = None
            self._last_auto_process_failure = None
            self._last_auto_process_deferred = None
        self._write_enabled = _memory_session_enabled(
            kwargs.get("platform", ""), kwargs.get("agent_context", "")
        )
        self._last_recall = None
        self._gate_enabled = False
        self._retrieval_ids_by_turn.clear()
        self._gate_turn_ids.clear()
        self._active_turn_numbers.clear()
        self._active_retrieval_ids.clear()
        self._observed_tool_call_keys.clear()
        self._session_aliases.clear()
        self._pending_lineage.clear()
        self._deferred_process_sessions.clear()
        self._last_retrieval_observation = "unknown"
        self._last_retrieval_audit = "SEARCH_UNKNOWN"
        self._version_warning_emitted = False
        if self._client is not None:
            self._client.close()
            self._client = None
        if not self._write_enabled:
            self._log_stage(
                "initialize",
                started_at=started_at,
                status="disabled",
                session_id=self._session_id,
                error_type="ExcludedSession",
            )
            return
        config = self._config()
        self._auto_process = bool(config["auto_process"])
        command = _resolve_command(config)
        if command is None:
            self._log_stage(
                "initialize",
                started_at=started_at,
                status="unavailable",
                session_id=self._session_id,
                error_type="ClientUnavailable",
            )
            return
        self._client = _MCPClient(
            command,
            str(_resolve_vault(config)),
            float(config["timeout"]),
            float(config["process_timeout"]),
        )
        # A real provider config is the explicit opt-in boundary for the
        # short-lived gate ledger.  Do not create gate state for hand-built
        # test providers or an unconfigured default path.
        if _config_path(self._hermes_home).is_file():
            self._gate_enabled = True
        self._log_stage(
            "initialize",
            started_at=started_at,
            status="ready",
            session_id=self._session_id,
        )
        self._call("stats", {}, stage="stats", session_id=self._session_id)
        self._check_version_sync()

    def system_prompt_block(self) -> str:
        if not self._write_enabled:
            return ""
        return (
            "# Memleaf Memory\n"
            "Memleaf is the active local-first memory provider. Each visible user "
            "turn must call the configured memleaf MCP search tool at least once "
            "before answering, including ordinary greetings; a no-match result is "
            "valid, but an MCP error must remain an error. The Scope Map supplied "
            "for the turn tells you where to search. Search returns only a light "
            "directory; read the best project/identifier match with memleaf MCP "
            "read(memory_id, retrieval_id) when its body is needed, carrying the "
            "current turn's retrieval_id exactly as supplied; a missing or mismatched "
            "token is a read failure, never a reason to fall back to a file tool. "
            "Read more only if needed; for ordinary relevance queries, do not read all entries to filter unrelated items. "
            "When the user asks for current "
            "todos, all unfinished work, urgent work, or work due in a time range, call memleaf MCP "
            "list_todos instead of relevance search; omit scope for a global query, follow every "
            "next_cursor until has_more=false, and read every matching todo body with the same retrieval_id. "
            "Never exclude a todo because another Hermes session or another Agent created it. Hermes has a soft "
            "observer only: do not claim a search happened unless the visible tool "
            "messages show it. Visible Hermes "
            "turns are durably captured into the local memleaf inbox. Automatic "
            "capture → process is authoritative: if process fails, report that "
            "automatic processing failed and leave the inbox retryable. Do not use "
            "terminal, search_files, read_file, Python, or direct filesystem "
            "operations to search or read the memleaf Vault; those tools remain "
            "available for ordinary project/wiki files. Do not use direct vault "
            "file writes to simulate success, and do not infer automatic success merely because "
            "active or history files exist. Automatic recall is a directory of "
            "scope identifiers, hierarchy, and aliases only; it never contains "
            "memory IDs, titles, or bodies. Use deliberate remember/forget tools "
            "only when the user explicitly asks for that operation."
        )

    def _auto_process_failure_notice(self, session_id: str) -> str:
        """Return a safe next-turn notice for an unfinished auto process.

        Only bounded status fields are retained. In particular, neither model
        output nor MCP error text is copied into the prompt. The notice is
        deliberately separate from recalled memories so a process failure can
        never be mistaken for a durable memory.
        """
        with self._sync_lock:
            failure = self._last_auto_process_failure
            if not isinstance(failure, Mapping) or failure.get("session_id") != session_id:
                return ""
            code = str(failure.get("error_code") or "model_failed")
            stage = str(failure.get("error_stage") or "process")
        return (
            "<memleaf-process-status>\n"
            f"Automatic memleaf processing for the previous visible turn failed "
            f"at {stage} ({code}). The captured turn remains pending and automatic "
            "memory extraction has not succeeded. Report the failure if relevant; "
            "do not write or rewrite the vault through terminal, read_file, Python, "
            "or filesystem operations, and do not claim success from active/history "
            "files alone.\n"
            "</memleaf-process-status>"
        )

    def _auto_process_deferred_notice(self, session_id: str) -> str:
        """Return a safe notice when process left scope work for later."""

        with self._sync_lock:
            deferred = self._last_auto_process_deferred
            if not isinstance(deferred, Mapping) or deferred.get("session_id") != session_id:
                return ""
            candidates = int(deferred.get("deferred_candidates", 0) or 0)
            turns = int(deferred.get("deferred_inbox_turns", 0) or 0)
        if candidates <= 0 and turns <= 0:
            return ""
        return (
            "<memleaf-process-status>\n"
            f"Automatic memleaf processing completed with {candidates} deferred "
            f"candidate(s) across {turns} pending inbox turn(s) awaiting scope "
            "clarification. Memory extraction is not fully complete; do not claim "
            "that every captured turn was processed.\n"
            "</memleaf-process-status>"
        )

    @staticmethod
    def _process_deferred_counts(value: Any) -> tuple[int, int] | None:
        if not isinstance(value, Mapping):
            return None
        counts: list[int] = []
        for key in ("deferred_candidates", "deferred_inbox_turns"):
            count = value.get(key, 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                return None
            counts.append(count)
        return counts[0], counts[1]

    def _call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        stage: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> Any:
        started_at = time.monotonic()
        resolved_stage = stage or name
        with self._sync_lock:
            self._last_call_error = None
        if self._client is None:
            self._log_stage(
                resolved_stage,
                started_at=started_at,
                status="unavailable",
                session_id=session_id,
                turn_id=turn_id,
                error_type="ClientUnavailable",
            )
            return _CALL_FAILED
        try:
            result = self._client.call_tool(name, arguments)
        except Exception as error:
            with self._sync_lock:
                self._last_call_error = {
                    "error_code": str(getattr(error, "code", "") or "mcp_failed"),
                    "error_stage": str(getattr(error, "stage", "") or resolved_stage),
                }
            self._log_stage(
                resolved_stage,
                started_at=started_at,
                status="error",
                session_id=session_id,
                turn_id=turn_id,
                error_type=_error_type(error),
                error_code=getattr(error, "code", "") if isinstance(error, _MCPToolError) else "",
                error_stage=getattr(error, "stage", "") if isinstance(error, _MCPToolError) else "",
                validation_reason=getattr(error, "validation_reason", "") if isinstance(error, _MCPToolError) else "",
                validation_detail=getattr(error, "validation_detail", "") if isinstance(error, _MCPToolError) else "",
                attempt_count=getattr(error, "attempt_count", None) if isinstance(error, _MCPToolError) else None,
            )
            return _CALL_FAILED
        error_fields = _mcp_error_fields(result)
        if error_fields is not None:
            error_code, error_stage, validation_reason, attempt_count, validation_detail = error_fields
            with self._sync_lock:
                self._last_call_error = {
                    "error_code": error_code,
                    "error_stage": error_stage or resolved_stage,
                }
            self._log_stage(
                resolved_stage,
                started_at=started_at,
                status="error",
                session_id=session_id,
                turn_id=turn_id,
                error_type="MCPToolError",
                error_code=error_code,
                error_stage=error_stage or "",
                validation_reason=validation_reason or "",
                validation_detail=validation_detail or "",
                attempt_count=attempt_count,
            )
            return _CALL_FAILED
        self._log_stage(
            resolved_stage,
            started_at=started_at,
            status="ok",
            session_id=session_id,
            turn_id=turn_id,
        )
        return result

    def _capture_visible(self, *, session_id: str, turn_id: str, role: str, content: str) -> bool:
        result = self._call(
            "capture",
            {
                "source": "hermes",
                "session_id": session_id,
                "turn_id": turn_id,
                "role": role,
                "content": content,
                "record": True,
                "visible": True,
            },
            stage=f"capture_{role}",
            session_id=session_id,
            turn_id=turn_id,
        )
        if isinstance(result, Mapping) and (result.get("stored") is True or result.get("duplicate") is True):
            return True
        logger.warning(
            "memleaf stage=capture_%s status=invalid_result source=hermes session=%s turn=%s",
            role,
            session_id,
            turn_id,
        )
        return False

    @staticmethod
    def _observe_search_messages(
        messages: Optional[List[Dict[str, Any]]],
        retrieval_id: Optional[str],
        *,
        session_id: str = "",
        turn_id: str = "",
        vault_root: Optional[Path] = None,
        seen_call_keys: Any = None,
        audit_state: Optional[dict[str, Any]] = None,
    ) -> str:
        """Observe explicit host MCP calls in public messages.

        The provider's own stdio calls (stats/capture/process/scope_catalog)
        are not evidence that Hermes' main agent performed a search or read.
        Hermes has no public pre-final hook, so this is deliberately
        diagnostic and fail-open when the message contract does not expose a
        tool result.  Read and file-tool diagnostics never write Core state.
        """

        calls = _visible_tool_calls(messages)
        results = _visible_tool_results(messages)
        statuses: list[str] = []
        search_results_used: set[int] = set()
        search_ordinal = 0
        for call in calls:
            if call.get("name") not in {"mcp__memleaf__search", "mcp__memleaf__list_todos"}:
                continue
            search_ordinal += 1
            arguments = call.get("arguments")
            if not isinstance(arguments, Mapping) or arguments.get("retrieval_id") != retrieval_id:
                continue
            payload = _tool_result_for_call(call, calls, results, search_results_used)
            if payload is not _CALL_FAILED:
                status = _hermes_search_status(payload)
                observation_key = _tool_observation_key(call, search_ordinal)
                if not _record_tool_observation(seen_call_keys, observation_key):
                    continue
                # Hermes has no public write-back hook for this soft observer.
                # A valid current-turn result is enough to mark the local
                # provider diagnostic; no Core ledger or body is touched here.
                statuses.append(status)

        read_results_used: set[int] = set()
        read_sequence = 0
        controlled_reads = 0
        read_ordinal = 0
        for call in calls:
            if call.get("name") != "mcp__memleaf__read":
                continue
            read_ordinal += 1
            arguments = call.get("arguments")
            retrieval_present = isinstance(arguments, Mapping) and "retrieval_id" in arguments
            retrieval_match = bool(
                retrieval_present
                and isinstance(retrieval_id, str)
                and arguments.get("retrieval_id") == retrieval_id
            )
            payload = _tool_result_for_call(call, calls, results, read_results_used)
            result_status = _hermes_read_status(payload)
            if result_status == "ok" and not retrieval_match:
                # A compatibility read may return body text despite missing or
                # mismatched gate binding.  Do not make that look like a
                # controlled success in the diagnostic stream.
                result_status = "uncontrolled_success"
            observation_key = _tool_observation_key(call, read_ordinal)
            if not _record_tool_observation(seen_call_keys, observation_key):
                continue
            read_sequence += 1
            if result_status == "ok" and retrieval_match:
                controlled_reads += 1
            logger.info(
                "memleaf retrieval-read source=hermes session=%s turn=%s read_seq=%d retrieval_present=%s retrieval_match=%s result=%s",
                _safe_component(session_id, "none") if session_id else "none",
                _safe_component(turn_id, "none") if turn_id else "none",
                read_sequence,
                retrieval_present,
                retrieval_match,
                result_status,
            )

        file_sequence = 0
        for call in calls:
            if not _file_tool_name(call.get("name")):
                continue
            path = _path_from_tool_arguments(call.get("arguments"))
            bypass = _path_is_within(vault_root, path)
            if bypass is None:
                continue
            file_sequence += 1
            logger.info(
                "memleaf file-tool source=hermes session=%s turn=%s file_seq=%d bypass=%s",
                _safe_component(session_id, "none") if session_id else "none",
                _safe_component(turn_id, "none") if turn_id else "none",
                file_sequence,
                "detected" if bypass else "not_detected",
            )
        if not statuses:
            search_status = "unknown"
            audit_status = "SEARCH_UNKNOWN"
        elif "found" in statuses:
            search_status = "found"
            # Hermes exposes no public pre-final hook and no reliable signal
            # that proves whether an answer used historical memory when no
            # controlled read occurred. Do not fabricate FOUND_NOT_USED or
            # FOUND_REQUIRED_READ_MISSING; record the uncertainty explicitly.
            audit_status = "FOUND_READ" if controlled_reads else "FOUND_NO_READ_UNDETERMINED"
        elif "no_match" in statuses:
            search_status = "no_match"
            audit_status = "NO_MATCH"
        else:
            search_status = "error"
            audit_status = "ERROR"
        if isinstance(audit_state, dict):
            audit_state.clear()
            audit_state.update(
                {
                    "status": audit_status,
                    "search_status": search_status,
                    "controlled_reads": controlled_reads,
                }
            )
        logger.info(
            "memleaf retrieval-audit source=hermes session=%s turn=%s status=%s search=%s controlled_reads=%d",
            _safe_component(session_id, "none") if session_id else "none",
            _safe_component(turn_id, "none") if turn_id else "none",
            audit_status,
            search_status,
            controlled_reads,
        )
        return search_status

    @staticmethod
    def _catalog_retrieval_id(value: Any) -> Optional[str]:
        """Extract only the MCP-issued opaque token from scope_catalog."""

        if not isinstance(value, Mapping):
            return None
        token = value.get("retrieval_id")
        if (
            not isinstance(token, str)
            or not token.startswith("rtv-")
            or len(token) > 80
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in token)
        ):
            return None
        return token

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = None
        if not self._write_enabled or not query:
            return ""
        safe_session = self._canonical_session_id(session_id or self._session_id)
        failure_notice = self._auto_process_failure_notice(safe_session)
        deferred_notice = self._auto_process_deferred_notice(safe_session)
        turn_number = self._current_turn_number(safe_session)
        scope_args: dict[str, Any] = {"limit": _MAX_SCOPE_ITEMS}
        if self._gate_enabled and isinstance(turn_number, int) and turn_number > 0:
            gate_turn_id = self._gate_turn_id(safe_session, turn_number)
            if gate_turn_id is None:
                # A missing on_turn_start is not enough evidence to create a
                # managed Hermes turn.  Keep this soft path unbound rather
                # than risking reuse of another visible conversation turn.
                gate_turn_id = ""
            if gate_turn_id:
                scope_args.update(
                    {
                        "source": "hermes",
                        "session_id": safe_session,
                        "turn_id": gate_turn_id,
                    }
                )
        catalog = self._call(
            "scope_catalog",
            scope_args,
            stage="scope_catalog",
            session_id=safe_session,
        )
        if catalog is _CALL_FAILED:
            notices = [notice for notice in (failure_notice, deferred_notice) if notice]
            notices.append(_SCOPE_MAP_INVALID_NOTICE)
            return "\n\n".join(notices)
        if not _scope_catalog_is_valid(catalog):
            notices = [notice for notice in (failure_notice, deferred_notice) if notice]
            notices.append(_SCOPE_MAP_INVALID_NOTICE)
            return "\n\n".join(notices)
        retrieval_id = self._catalog_retrieval_id(catalog)
        if retrieval_id is not None and isinstance(turn_number, int) and turn_number > 0:
            key = (safe_session, turn_number)
            self._retrieval_ids_by_turn[key] = retrieval_id
            self._retrieval_ids_by_turn.move_to_end(key)
            while len(self._retrieval_ids_by_turn) > _MAX_PENDING_TURN_NUMBERS:
                self._retrieval_ids_by_turn.popitem(last=False)
            self._active_retrieval_ids[safe_session] = retrieval_id
            self._active_retrieval_ids.move_to_end(safe_session)
        context, _ = _scope_context(
            catalog,
            retrieval_id=retrieval_id,
            scope_hint=_unique_query_scope(query, catalog),
        )
        if not context:
            notices = [notice for notice in (failure_notice, deferred_notice) if notice]
            return "\n\n".join(notices)
        # This provider injects a map, not recalled memory entries.  Do not
        # report it as N memories in Hermes' indicator.
        self._last_recall = None
        notices = [notice for notice in (failure_notice, deferred_notice) if notice]
        return "\n\n".join([*notices, context]) if notices else context

    def recall_status(self) -> Optional[RecallStatus]:
        if not self._write_enabled:
            return None
        return self._last_recall

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        turn_number: Optional[int] = None,
    ) -> None:
        # ``messages`` may contain system prompts, tool calls/results, and
        # attachment parts.  Hermes already supplies the visible user and
        # assistant strings separately; derive only a bounded search status
        # from an explicit public memleaf tool result below.  Never capture
        # the raw message list as business conversation content.
        if not self._write_enabled or self._client is None:
            return
        visible_events = [
            ("user", user_content),
            ("assistant", assistant_content),
        ]
        if any(not isinstance(content, str) or not content.strip() for _, content in visible_events):
            return

        effective_session = self._canonical_session_id(session_id or self._session_id)
        # Hermes serializes provider sync work, but this lock also protects
        # direct/plugin-level concurrent calls and makes process one-shot per
        # captured turn within this provider instance.
        with self._sync_lock:
            try:
                resolved_turn_number = turn_number
                if resolved_turn_number is None:
                    resolved_turn_number = self._take_turn_number(effective_session, user_content)
                elif isinstance(resolved_turn_number, int) and not isinstance(resolved_turn_number, bool):
                    self._discard_turn_number(effective_session, user_content, resolved_turn_number)
                turn_id = self._resolve_turn_id(
                    effective_session,
                    resolved_turn_number,
                    user_content,
                    assistant_content,
                )
                retrieval_id = self._gate_id_for_turn(effective_session, resolved_turn_number)
                if retrieval_id is None and resolved_turn_number is None:
                    retrieval_id = self._current_gate_id(effective_session)
                if self._gate_enabled:
                    audit_state: dict[str, Any] = {}
                    observation = self._observe_search_messages(
                        messages,
                        retrieval_id,
                        session_id=effective_session,
                        turn_id=turn_id,
                        vault_root=_resolve_vault(self._config()),
                        seen_call_keys=self._observed_tool_call_keys,
                        audit_state=audit_state,
                    )
                    while len(self._observed_tool_call_keys) > _MAX_OBSERVED_TOOL_CALL_KEYS:
                        self._observed_tool_call_keys.popitem(last=False)
                    self._last_retrieval_observation = observation
                    self._last_retrieval_audit = str(audit_state.get("status") or "SEARCH_UNKNOWN")
                lineage_ready = self._retry_pending_lineage(effective_session)
                for role, content in visible_events:
                    if not self._capture_visible(
                        session_id=effective_session,
                        turn_id=turn_id,
                        role=role,
                        content=content,
                    ):
                        return
                if not self._auto_process:
                    return
                if not lineage_ready:
                    self._defer_process_session(effective_session)
                    logger.warning(
                        "memleaf provider automatic process deferred for hermes/%s; session lineage is pending",
                        effective_session,
                    )
                    return
                self._process_deferred_sessions(effective_session, turn_id)
            except Exception as error:
                # A provider failure must not fail the user's Hermes turn.
                # Core process owns the transaction and leaves inbox/state
                # retryable when model, parsing, or persistence fails.
                logger.warning(
                    "memleaf provider sync failed stage=sync_turn source=hermes session=%s error_type=%s; inbox retained",
                    effective_session,
                    _error_type(error),
                )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # memleaf-mcp remains configured separately for deliberate search,
        # remember, and forget operations. The native provider owns automatic
        # recall/capture only, avoiding duplicate tool names in Hermes.
        return []

    def shutdown(self) -> None:
        with self._sync_lock:
            self._pending_lineage.clear()
            self._deferred_process_sessions.clear()
        if self._client is not None:
            self._client.close()
        self._client = None


def register(ctx) -> None:
    ctx.register_memory_provider(MemleafMemoryProvider())
