"""A dependency-free stdio MCP adapter for the public Memleaf API.

The adapter deliberately contains protocol and argument plumbing only.  All
memory behavior remains in :class:`memleaf.service.Memleaf`.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .host_runtime import HostRuntime
from .llm import MODEL_ERROR_CODES, MODEL_VALIDATION_REASONS, ModelError, ModelUnavailable
from .models import CaptureResult, ForgetAboutResult, Memory, MemoryVersionError
from .retrieval import RetrievalError
from .retrieval_gate import (
    RetrievalGateError,
    begin_turn,
    guarded_read,
    observe_search,
    observe_todo_list,
    todo_filter_key,
    validate_current_turn,
    validate_turn,
)
from .service import Memleaf
from .validation import MODEL_VALIDATION_DETAILS, ModelOutputError


SERVER_INFO = {"name": "memleaf", "version": __version__}
MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
MODERN_PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
MODERN_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
DISCOVERY_TTL_MS = 60_000
INSTRUCTIONS = (
    "memleaf stores local Markdown memory in the configured vault. "
    "When Hermes host integration is active, it supplies only a Memory Scope Map and "
    "owns capture and process; do not repeat those calls through MCP. For every ordinary visible "
    "user turn, including greetings, call memleaf search at least once before answering. Use your "
    "complete conversation context to select scope and query; do not add a needs-memory gate. "
    "Fetch further scope_catalog pages when the map has_more; never assume an omitted scope is absent. "
    "Search returns status=found or no_match and lightweight results (memory_id, title), "
    "not bodies. Errors are not no_match: correct scope_mismatch or retry a failed search at most "
    "twice, then honestly report degraded retrieval; do not claim memory was checked successfully. "
    "Treat directory entries as leads, never as verified facts; do not infer facts from a title. "
    "Use read(memory_id) on the best matching entry before relying on a past fact. Read more only "
    "when needed, not every entry to filter unrelated items. You may refine the query/scope and "
    "search again. Pass the host-provided retrieval_id to search and read; never invent or replace "
    "it. Managed reads have no aggregate ID/character quota; read every relevant memory needed for "
    "the user's question while keeping each read page at 2000 characters. MCP read requires retrieval_id "
    "and a current FOUND search or list_todos result; NO_MATCH, ERROR, and DEGRADED turns cannot read. "
    "For global current-todo questions use list_todos rather than relevance search, omit scope to cover "
    "all scopes, follow next_cursor until has_more=false, then read every matching todo body. "
    "Legacy context is an explicit compatibility interface, not an alternative to this "
    "scope/search/read flow. Managed MCP search is directory-only and rejects view=full; "
    "Python full-result search remains compatible. Without host integration, tools remain an explicit fallback only; "
    "do not claim automatic recall, recording or final-answer enforcement succeeded. "
    "Hermes and Codex are supported hosts; Hermes uses a Soft Gate, not a hard guarantee. "
    "When explicitly calling legacy context, pass source and session_id together and avoid "
    "duplicating native memory that the target host already loads. "
    "Capture only user-visible and assistant-visible text. Never capture system or developer "
    "messages, hidden reasoning, raw tool output, or attachment bodies. "
    "Process only complete user+assistant turns; do not process incomplete turns. "
    "Use remember only when the user explicitly asks to remember something. If the user has "
    "previously or currently explicitly said not to record corresponding text, skip capture for it. "
    "For text already persisted, use forget_memory or forget_about only when its target is "
    "reliably identified; a do-not-remember or forget request takes precedence. Use "
    "include_history=true only for an explicit request "
    "about historical memory. MCP read returns at most 2000 body characters per page; when "
    "has_more is true, continue with next_offset only as needed and pass the returned version "
    "as expected_version. If memory_version_changed is reported, restart at offset 0 without "
    "expected_version and use the new version for continuation. If a tool fails, report the "
    "failure honestly; never use terminal, read_file, Python, or direct vault files as a fallback "
    "and claim that MCP succeeded."
)

_MAX_READ_PAGE_CHARS = 2000
_MCP_PROCESS_NAMESPACE = uuid.uuid4().hex


class _InvalidParams(Exception):
    """Internal marker for JSON-RPC invalid-params responses."""


def _text_or_texts_schema() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]
    }


def _object_schema(
    properties: Mapping[str, Any],
    *,
    required: list[str] | None = None,
    any_of: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "required": list(required or []),
        "additionalProperties": False,
    }
    if any_of:
        schema["anyOf"] = any_of
    return schema


_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "capture",
        "description": "Capture one visible conversation event into the inbox.",
        "inputSchema": _object_schema(
            {
                "source": {"type": "string"},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "role": {"type": "string"},
                "content": {"type": "string"},
                "event_id": {"type": "string"},
                "record": {"type": "boolean"},
                "visible": {"type": "boolean"},
                "tool_evidence": {
                    "type": "array",
                    "items": _object_schema(
                        {
                            "message_id": {"type": "string"},
                            "subject": {"type": "string"},
                            "sender": {"type": "string"},
                            "domain": {"type": "string"},
                            **{key: {"type": "string"} for key in ("tool_name", "call_id", "record_id", "title", "kind", "result_status", "content", "result_digest")},
                        }
                    ),
                },
            },
            required=["source", "session_id", "turn_id", "role", "content"],
        ),
    },
    {
        "name": "context",
        "description": (
            "Return only a bounded directory of relevant memory IDs, short titles, and project "
            "scopes: at most 3 complete entries and 600 characters of directory data. "
            "Directory entries are leads, not facts; read the needed ID to verify past facts."
        ),
        "inputSchema": _object_schema(
            {
                "query": _text_or_texts_schema(),
                "scope": _text_or_texts_schema(),
                "source": {"type": "string"},
                "session_id": {"type": "string"},
                "project_path": {"type": "string"},
                "include_history": {"type": "boolean"},
                "todo_status": {
                    "type": "string",
                    "enum": ["active", "completed", "cancelled", "all"],
                },
                "limit": {"type": "integer", "minimum": 0},
            },
            required=["query"],
        ),
    },
    {
        "name": "scope_catalog",
        "description": (
            "List only scope IDs, parents and aliases, never memory entries or bodies. "
            "Continue with next_cursor when has_more; each page is capped at 20 scopes/2000 characters."
        ),
        "inputSchema": _object_schema(
            {
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
                "source": {"type": "string", "enum": ["hermes"]},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
            },
        ),
    },
    {
        "name": "search",
        "description": (
            "Search once per ordinary user turn. Return found/no_match and paged lightweight "
            "results (memory_id, title), at most 20/4000 characters, without changing hits. "
            "retrieval_id is required and must be the host-provided current-turn token. Errors are "
            "failures, never no_match. Managed MCP search is directory-only; view=full is rejected. "
            "Python search(view='full') remains the compatibility interface."
        ),
        "inputSchema": _object_schema(
            {
                "query": _text_or_texts_schema(),
                "scope": _text_or_texts_schema(),
                "include_history": {"type": "boolean"},
                "todo_status": {
                    "type": "string",
                    "enum": ["active", "completed", "cancelled", "all"],
                },
                "limit": {"type": "integer", "minimum": 1},
                "view": {"type": "string", "enum": ["directory", "full"]},
                "cursor": {"type": "string"},
                "retrieval_id": {"type": "string"},
            },
            required=["query", "retrieval_id"],
        ),
    },
    {
        "name": "list_todos",
        "description": (
            "Enumerate current memleaf todo memories by status/date across all scopes by default. "
            "This is not relevance search. Continue with next_cursor until has_more=false, then read "
            "the matching todo bodies with the same retrieval_id."
        ),
        "inputSchema": _object_schema(
            {
                "status": {"type": "string", "enum": ["active", "completed", "cancelled", "all"]},
                "scope": _text_or_texts_schema(),
                "due_from": {"type": "string"},
                "due_to": {"type": "string"},
                "include_overdue": {"type": "boolean"},
                "include_unscheduled": {"type": "boolean"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
                "retrieval_id": {"type": "string"},
            },
            required=["retrieval_id"],
        ),
    },
    {
        "name": "read",
        "description": (
            "Read one memory by exact memory_id in pages. Each page is capped at 2000 body "
            "characters and contains only identifiers, scopes, body, and paging/version fields; "
            "retrieval_id is required, and the retrieval turn must have a current FOUND search; "
            "NO_MATCH, ERROR, and DEGRADED turns are rejected. "
            "continue with next_offset only when has_more is true, passing the returned version "
            "as expected_version. If the version changes, restart at offset=0 without it."
        ),
        "inputSchema": _object_schema(
            {
                "memory_id": {"type": "string"},
                "include_history": {"type": "boolean"},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1},
                "expected_version": {"type": "string"},
                "retrieval_id": {"type": "string"},
            },
            required=["memory_id", "retrieval_id"],
        ),
    },
    {
        "name": "process",
        "description": "Process complete inbox turns using the configured Core model route.",
        "inputSchema": _object_schema(
            {
                "source": {"type": "string"},
                "session_id": {"type": "string"},
                "scope": _text_or_texts_schema(),
            }
        ),
    },
    {
        "name": "remember",
        "description": "Explicitly remember text using the configured Core model route.",
        "inputSchema": _object_schema(
            {
                "content": {"type": "string"},
                "text": {"type": "string"},
                "source": {"type": "string"},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "event_id": {"type": "string"},
                "scopes": _text_or_texts_schema(),
            },
            any_of=[{"required": ["content"]}, {"required": ["text"]}],
        ),
    },
    {
        "name": "forget_memory",
        "description": "Forget one memory by its exact memory id.",
        "inputSchema": _object_schema(
            {"memory_id": {"type": "string"}},
            required=["memory_id"],
        ),
    },
    {
        "name": "forget_about",
        "description": "Forget one unambiguous memory topic or return candidates.",
        "inputSchema": _object_schema(
            {"query": {"type": "string"}},
            required=["query"],
        ),
    },
    {
        "name": "rebuild_index",
        "description": "Rebuild all derived local memory indexes.",
        "inputSchema": _object_schema({}),
    },
    {
        "name": "stats",
        "description": "Return local vault counts and diagnostic statistics.",
        "inputSchema": _object_schema({}),
    },
)

_INTERNAL_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "session_lineage",
        "description": "Persist or clear host compression lineage without exposing a model tool.",
        "inputSchema": _object_schema(
            {
                "source": {"type": "string", "enum": ["hermes"]},
                "session_id": {"type": "string"},
                "parent_session_id": {"type": "string"},
                "reset": {"type": "boolean"},
            },
            required=["source", "session_id"],
        ),
    },
)

_TOOL_BY_NAME = {tool["name"]: tool for tool in (*_TOOLS, *_INTERNAL_TOOLS)}


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return type(value) is int
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _schema_matches(value: Any, schema: Mapping[str, Any]) -> bool:
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        if not any(isinstance(item, Mapping) and _schema_matches(value, item) for item in alternatives):
            return False
    expected = schema.get("type")
    if isinstance(expected, str) and not _json_type_matches(value, expected):
        return False
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and (not isinstance(value, (int, float)) or value < minimum):
        return False
    items = schema.get("items")
    if isinstance(items, Mapping) and isinstance(value, list):
        if not all(_schema_matches(item, items) for item in value):
            return False
    return True


def _validate_tool_arguments(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise _InvalidParams
    schema = _TOOL_BY_NAME[name]["inputSchema"]
    required = schema.get("required", [])
    if isinstance(required, list) and any(key not in arguments for key in required):
        raise _InvalidParams
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        if not any(
            isinstance(item, Mapping)
            and isinstance(item.get("required", []), list)
            and all(key in arguments for key in item["required"])
            for item in alternatives
        ):
            raise _InvalidParams
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise _InvalidParams
    if schema.get("additionalProperties") is False:
        if any(key not in properties for key in arguments):
            raise _InvalidParams
    for key, value in arguments.items():
        property_schema = properties.get(key)
        if isinstance(property_schema, Mapping) and not _schema_matches(value, property_schema):
            raise _InvalidParams
    return dict(arguments)


def _field_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _directory_item(value: Any) -> dict[str, Any] | None:
    """Keep only the public directory fields at the MCP boundary."""

    memory_id = _field_value(value, "memory_id")
    title = _field_value(value, "title")
    scopes = _field_value(value, "scopes")
    if not isinstance(memory_id, str) or not memory_id:
        return None
    if not isinstance(title, str):
        title = ""
    if isinstance(scopes, str):
        scopes = [scopes]
    if not isinstance(scopes, (list, tuple)) or not all(isinstance(item, str) for item in scopes):
        scopes = []
    return {
        "memory_id": memory_id,
        "title": title,
        "scopes": list(scopes),
    }


def _directory_result(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        directory_item = _directory_item(item)
        if directory_item is not None:
            result.append(directory_item)
    return result


def _read_page_result(value: Any) -> dict[str, Any] | None:
    """Keep MCP read responses to the bounded page contract."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("invalid read page result")
    fields = (
        "memory_id",
        "title",
        "scopes",
        "body",
        "offset",
        "next_offset",
        "has_more",
        "total_chars",
        "version",
    )
    if any(name not in value for name in fields):
        raise ValueError("incomplete read page")
    if any(not isinstance(value[name], str) or not value[name] for name in ("memory_id", "title", "version")):
        raise ValueError("invalid read identifiers")
    if not isinstance(value["scopes"], list) or not all(isinstance(scope, str) for scope in value["scopes"]):
        raise ValueError("invalid read scopes")
    if not isinstance(value["body"], str) or len(value["body"]) > _MAX_READ_PAGE_CHARS:
        raise ValueError("invalid read body")
    if any(type(value[name]) is not int or value[name] < 0 for name in ("offset", "total_chars")):
        raise ValueError("invalid read offsets")
    end = value["offset"] + len(value["body"])
    if end > value["total_chars"] or type(value["has_more"]) is not bool:
        raise ValueError("invalid read length")
    if value["has_more"]:
        if not value["body"] or type(value["next_offset"]) is not int or value["next_offset"] != end or end >= value["total_chars"]:
            raise ValueError("invalid read continuation")
    elif value["next_offset"] is not None or end != value["total_chars"]:
        raise ValueError("invalid read final page")
    result = {name: _jsonable(value[name]) for name in fields if name in value}
    memory_type = value.get("type")
    status = value.get("status")
    due_date = value.get("due_date")
    if memory_type is not None and not isinstance(memory_type, str):
        raise ValueError("invalid read memory type")
    if status is not None and not isinstance(status, str):
        raise ValueError("invalid read todo status")
    if due_date is not None and not isinstance(due_date, str):
        raise ValueError("invalid read todo due date")
    if "type" in value:
        result["type"] = memory_type
    if "status" in value:
        result["status"] = status
    if "due_date" in value:
        result["due_date"] = due_date
    return result


def _catalog_result(value: Any) -> dict[str, Any]:
    """Keep scope discovery separate from concrete memory discovery."""

    if not isinstance(value, Mapping) or not isinstance(value.get("scopes"), list):
        raise ValueError("invalid scope catalog")
    entries = []
    for entry in value["scopes"]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("scope"), str):
            raise ValueError("invalid scope catalog entry")
        parent, aliases = entry.get("parent"), entry.get("aliases")
        if parent is not None and not isinstance(parent, str):
            raise ValueError("invalid scope catalog parent")
        if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
            raise ValueError("invalid scope aliases")
        entries.append({"scope": entry["scope"], "parent": parent, "aliases": aliases})
    return {"scopes": entries, **_paging_fields(value)}


def _paging_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    has_more, cursor = value.get("has_more"), value.get("next_cursor")
    if not isinstance(has_more, bool) or (cursor is not None and not isinstance(cursor, str)):
        raise ValueError("invalid paging result")
    if has_more != bool(cursor):
        raise ValueError("inconsistent paging result")
    return {"has_more": has_more, "next_cursor": cursor}


def _search_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("status") not in {"found", "no_match"}:
        raise ValueError("invalid search status")
    entries = value.get("results")
    if not isinstance(entries, list):
        raise ValueError("invalid search results")
    results: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("invalid search entry")
        memory_id = entry.get("memory_id")
        title = entry.get("title")
        if not isinstance(memory_id, str) or not memory_id or not isinstance(title, str) or not title:
            raise ValueError("invalid search identifiers")
        # Search is intentionally narrower than the legacy directory helper:
        # do not leak per-memory scope/tags/aliases/keywords into candidates.
        results.append({"memory_id": memory_id, "title": title})
    if bool(results) != (value["status"] == "found"):
        raise ValueError("inconsistent search results")
    paging = _paging_fields(value)
    if not results and paging["has_more"]:
        raise ValueError("empty page cannot have more results")
    return {"status": value["status"], "results": results, **paging}


def _jsonable(value: Any) -> Any:
    """Convert Core results to JSON-safe values without stringifying errors."""

    if isinstance(value, Memory):
        return _jsonable(value.to_dict())
    if isinstance(value, CaptureResult):
        return {
            "event_id": value.event_id,
            "stored": value.stored,
            "duplicate": value.duplicate,
            "path": _jsonable(value.path),
            "content": value.content,
        }
    if isinstance(value, ForgetAboutResult):
        return {
            "status": value.status,
            "deleted": _jsonable(value.deleted),
            "candidates": _jsonable(value.candidates),
        }
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _jsonable(dataclasses.asdict(value))
        except Exception:
            return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        if isinstance(value, (set, frozenset)):
            values.sort(key=lambda item: repr(item))
        return [_jsonable(item) for item in values]
    return None


def _json_text(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    structured = _jsonable(value)
    if not isinstance(structured, dict):
        structured = {"result": structured}
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": _json_text(structured)}],
        "structuredContent": structured,
        "isError": bool(is_error),
    }
    return result


def _safe_model_diagnostics(error: BaseException, *, default_reason: str | None = None) -> dict[str, Any]:
    reason = getattr(error, "validation_reason", None)
    if not isinstance(reason, str) or reason not in MODEL_VALIDATION_REASONS:
        reason = default_reason
    attempt_count = getattr(error, "attempt_count", None)
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3):
        attempt_count = None
    detail = getattr(error, "validation_detail", None)
    if not isinstance(detail, str) or detail not in MODEL_VALIDATION_DETAILS:
        detail = "other_schema_violation" if isinstance(error, ModelOutputError) else None
    fields: dict[str, Any] = {}
    if reason is not None:
        fields["validation_reason"] = reason
    if detail is not None:
        fields["validation_detail"] = detail
    if attempt_count is not None:
        fields["attempt_count"] = attempt_count
    return fields


def _tool_error(error: BaseException) -> dict[str, Any]:
    if isinstance(error, (RetrievalError, RetrievalGateError)):
        return _tool_result(
            {"status": "error", "error": {"code": error.code, "message": str(error)}},
            is_error=True,
        )
    if isinstance(error, MemoryVersionError):
        return _tool_result(
            {
                "error": {
                    "code": "memory_version_changed",
                    "message": (
                        "Memory changed; restart read at offset=0 without expected_version, "
                        "then use the new version for continuation."
                    ),
                }
            },
            is_error=True,
        )
    if isinstance(error, (ModelUnavailable, ModelError)):
        code = getattr(error, "code", "model_failed")
        if code not in MODEL_ERROR_CODES:
            code = "model_failed"
        message = {
            "model_timeout": "model request timed out",
            "model_auth_failed": "model authentication failed",
            "model_rate_limited": "model request was rate limited",
            "model_http_error": "model HTTP request failed",
            "model_network_error": "model network request failed",
            "model_invalid_response": "model returned an invalid response",
            "model_unavailable": "model unavailable",
            "model_failed": "model failed",
        }[code]
        payload: dict[str, Any] = {"code": code, "message": message}
        stage = getattr(error, "stage", None)
        if stage in {"gate", "summarize"}:
            payload["stage"] = stage
        payload.update(
            _safe_model_diagnostics(
                error,
                default_reason="response_shape" if code == "model_invalid_response" else None,
            )
        )
        return _tool_result({"error": payload}, is_error=True)
    elif isinstance(error, ModelOutputError):
        payload = {
            "code": "model_invalid_response",
            "message": "model returned an invalid response",
        }
        stage = getattr(error, "stage", None)
        if stage in {"gate", "summarize"}:
            payload["stage"] = stage
        payload.update(_safe_model_diagnostics(error, default_reason="schema_violation"))
        return _tool_result({"error": payload}, is_error=True)
    elif isinstance(error, (ValueError, TypeError)):
        message = "tool rejected request"
        code = "tool_rejected_request"
    else:
        message = "tool failed"
        code = "tool_failed"
    return _tool_result({"error": {"code": code, "message": message}}, is_error=True)


def _mcp_search_call_id(request_id: Any) -> str:
    """Build a fixed-length, process-scoped id for one JSON-RPC request.

    Hermes does not expose its outer model tool-use id to this stdio server.
    Hashing the server-process namespace together with the JSON-RPC id gives
    retries of the same request a stable deduplication key while ensuring a
    newly spawned server cannot collide with an old process's key.  The raw
    request id is never persisted or returned.
    """

    try:
        encoded_id = json.dumps(
            request_id,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        encoded_id = type(request_id).__name__
    digest = hashlib.sha256(
        f"{_MCP_PROCESS_NAMESPACE}\x00{encoded_id}".encode("utf-8")
    ).hexdigest()
    return f"mcp-{digest}"


def _managed_search_state(
    service: Memleaf,
    retrieval_id: str | None,
) -> dict[str, Any] | None:
    """Validate the current host turn; Hermes additionally records MCP results."""

    if retrieval_id is None:
        return None
    state = validate_turn(service.vault, retrieval_id)
    source = state.get("source")
    if not isinstance(source, str) or not source:
        raise RetrievalGateError("retrieval_identity_invalid")
    current = validate_current_turn(service.vault, retrieval_id, source)
    # Codex records real search outcomes from PostToolUse so the stdio server
    # must not double-count them. Current-turn validation is authoritative for
    # every host token before the search itself is allowed to run.
    return current if source == "hermes" else None


def _observe_mcp_search(
    service: Memleaf,
    state: Mapping[str, Any] | None,
    value: Mapping[str, Any] | None,
    request_id: Any,
) -> None:
    if state is None:
        return
    status = value.get("status") if isinstance(value, Mapping) else None
    if status not in {"found", "no_match"}:
        status = "error"
    observe_search(
        service.vault,
        state["retrieval_id"],
        status,
        _mcp_search_call_id(request_id),
        current_source="hermes",
    )



def _observe_mcp_todos(
    service: Memleaf,
    state: Mapping[str, Any] | None,
    value: Mapping[str, Any] | None,
    request_id: Any,
    arguments: Mapping[str, Any],
) -> None:
    if state is None:
        return
    status = value.get("status") if isinstance(value, Mapping) else "error"
    if status not in {"found", "no_match"}:
        status = "error"
    has_more = value.get("has_more") if isinstance(value, Mapping) else False
    next_cursor = value.get("next_cursor") if isinstance(value, Mapping) else None
    observe_todo_list(
        service.vault,
        state["retrieval_id"],
        status,
        _mcp_search_call_id(request_id),
        filter_key=todo_filter_key(arguments),
        cursor=arguments.get("cursor") if isinstance(arguments.get("cursor"), str) else None,
        has_more=has_more if isinstance(has_more, bool) else False,
        next_cursor=next_cursor if isinstance(next_cursor, str) else None,
        current_source="hermes",
    )


def _invoke_tool(
    service: Memleaf,
    name: str,
    arguments: Any,
    *,
    request_id: Any = None,
) -> dict[str, Any]:
    if name not in _TOOL_BY_NAME:
        raise _InvalidParams
    if name in {"read", "search", "list_todos"} and (
        not isinstance(arguments, dict) or "retrieval_id" not in arguments
    ):
        return _tool_error(RetrievalGateError("retrieval_id_required"))
    try:
        args = _validate_tool_arguments(name, arguments)
    except _InvalidParams:
        return _tool_result(
            {"error": {"code": "invalid_arguments", "message": "invalid tool arguments"}},
            is_error=True,
        )
    try:
        if name == "capture":
            source = args.get("source")
            runtime = HostRuntime(service, source if isinstance(source, str) and source else "mcp")
            value = runtime.capture(**args)
        elif name == "context":
            value = service.context(**args)
            value = _directory_result(value)
        elif name == "scope_catalog":
            identity = {key: args.pop(key) for key in ("source", "session_id", "turn_id") if key in args}
            if identity and len(identity) != 3:
                raise ValueError("scope catalog host identity must be supplied together")
            if identity:
                runtime = HostRuntime(service, identity["source"])
                value = _catalog_result(runtime.scope_catalog(**args))
                # Independent host plugins obtain the same shared-runtime turn
                # binding over MCP instead of importing Core internals.
                value["retrieval_id"] = runtime.open_retrieval_turn(
                    identity["session_id"], identity["turn_id"]
                )
            else:
                value = _catalog_result(service.scope_catalog(**args))
        elif name == "session_lineage":
            value = service.session_lineage(**args)
        elif name == "search":
            view = args.pop("view", "directory")
            retrieval_id = args.pop("retrieval_id", None)
            managed_state = _managed_search_state(service, retrieval_id)
            try:
                if retrieval_id is not None and view == "full":
                    raise RetrievalGateError(
                        "retrieval_full_view_forbidden", "Use directory search followed by bounded read."
                    )
                if view == "full":
                    if "cursor" in args:
                        raise ValueError("full search does not accept a cursor")
                    value = service.search(**args, view="full")
                else:
                    value = _search_result(service.search_candidates(**args))
            except Exception as error:
                try:
                    _observe_mcp_search(service, managed_state, None, request_id)
                except Exception as observe_error:
                    return _tool_error(observe_error)
                return _tool_error(error)
            try:
                _observe_mcp_search(service, managed_state, value, request_id)
            except Exception as error:
                # A managed result is not successful until the gate records it.
                return _tool_error(error)
        elif name == "list_todos":
            retrieval_id = args.pop("retrieval_id", None)
            managed_state = _managed_search_state(service, retrieval_id)
            observed_args = dict(args)
            try:
                value = service.list_todos(**args)
            except Exception as error:
                try:
                    _observe_mcp_todos(service, managed_state, None, request_id, observed_args)
                except Exception as observe_error:
                    return _tool_error(observe_error)
                return _tool_error(error)
            try:
                _observe_mcp_todos(service, managed_state, value, request_id, observed_args)
            except Exception as error:
                return _tool_error(error)
        elif name == "read":
            # Keep the protocol boundary hard-capped even if a caller sends a
            # larger value.  Core read_page performs the same authoritative
            # validation; this prevents a future adapter regression from
            # widening the MCP page.
            requested_max_chars = args.get("max_chars", _MAX_READ_PAGE_CHARS)
            if isinstance(requested_max_chars, int) and not isinstance(requested_max_chars, bool):
                args["max_chars"] = min(requested_max_chars, _MAX_READ_PAGE_CHARS)
            else:
                args["max_chars"] = _MAX_READ_PAGE_CHARS
            retrieval_id = args.pop("retrieval_id")

            def read_page(allowed_chars: int) -> dict[str, Any] | None:
                page_args = {**args, "max_chars": min(args["max_chars"], allowed_chars)}
                return _read_page_result(service.read_page(**page_args))

            state = validate_turn(service.vault, retrieval_id)
            current_source = state.get("source")
            if not isinstance(current_source, str) or not current_source:
                raise RetrievalGateError("retrieval_identity_invalid")
            value = guarded_read(
                service.vault,
                retrieval_id,
                args["memory_id"],
                read_page,
                current_source=current_source,
            )
        elif name == "process":
            source = args.get("source")
            if isinstance(source, str) and source:
                value = HostRuntime(service, source).process(**args)
            else:
                value = service.process(**args)
        elif name == "remember":
            value = service.remember(**args)
        elif name == "forget_memory":
            value = service.forget_memory(**args)
        elif name == "forget_about":
            value = service.forget_about(**args)
        elif name == "rebuild_index":
            value = service.rebuild_index(**args)
        elif name == "stats":
            value = service.stats(**args)
        else:  # pragma: no cover - guarded by the name lookup above
            raise _InvalidParams
    except Exception as error:
        return _tool_error(error)
    return _tool_result(value)


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _success_response(request_id: Any, result: Any, *, modern: bool) -> dict[str, Any]:
    if modern:
        payload = dict(result) if isinstance(result, Mapping) else {"result": result}
        payload["resultType"] = "complete"
        meta = payload.get("_meta")
        response_meta = dict(meta) if isinstance(meta, Mapping) else {}
        response_meta[MODERN_SERVER_INFO_META] = dict(SERVER_INFO)
        payload["_meta"] = response_meta
    else:
        payload = result
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _modern_request(message: Mapping[str, Any]) -> bool:
    params = message.get("params")
    if not isinstance(params, Mapping):
        return False
    meta = params.get("_meta")
    return isinstance(meta, Mapping) and meta.get(MODERN_PROTOCOL_META) == MODERN_VERSION


def _params_object(message: Mapping[str, Any]) -> dict[str, Any]:
    if "params" not in message:
        return {}
    params = message.get("params")
    if not isinstance(params, dict):
        raise _InvalidParams
    return dict(params)


def _strip_meta(params: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(params)
    result.pop("_meta", None)
    return result


def _negotiated_version(params: Mapping[str, Any]) -> str:
    requested = params.get("protocolVersion")
    if isinstance(requested, str) and requested in LEGACY_VERSIONS:
        return requested
    return LEGACY_VERSIONS[0]


def _dispatch(message: Any, service: Memleaf) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _error_response(None, -32600, "Invalid Request")

    has_id = "id" in message
    request_id = message.get("id")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error_response(request_id, -32600, "Invalid Request")

    method = message["method"]
    if method == "notifications/initialized":
        return None
    notification = not has_id
    modern = _modern_request(message)

    def fail(code: int, text: str) -> dict[str, Any] | None:
        return None if notification else _error_response(request_id, code, text)

    try:
        if method == "initialize":
            if modern:
                return fail(-32601, "Method not found")
            params = _params_object(message)
            result = {
                "protocolVersion": _negotiated_version(params),
                "capabilities": {"tools": {}},
                "serverInfo": dict(SERVER_INFO),
                "instructions": INSTRUCTIONS,
            }
            return None if notification else _success_response(request_id, result, modern=False)

        if method == "server/discover":
            if not modern:
                return fail(-32601, "Method not found")
            _params_object(message)
            result = {
                "supportedVersions": [MODERN_VERSION],
                "capabilities": {"tools": {}},
                "ttlMs": DISCOVERY_TTL_MS,
                "cacheScope": "private",
                "instructions": INSTRUCTIONS,
            }
            return None if notification else _success_response(request_id, result, modern=True)

        if method == "ping":
            _params_object(message)
            return None if notification else _success_response(request_id, {}, modern=modern)

        if method == "tools/list":
            params = _params_object(message)
            _strip_meta(params)
            result = {"tools": list(_TOOLS)}
            return None if notification else _success_response(request_id, result, modern=modern)

        if method == "tools/call":
            params = _params_object(message)
            if any(key not in {"name", "arguments", "_meta"} for key in params):
                raise _InvalidParams
            name = params.get("name")
            if not isinstance(name, str) or name not in _TOOL_BY_NAME:
                raise _InvalidParams
            arguments = params.get("arguments", {})
            result = _invoke_tool(service, name, arguments, request_id=request_id)
            return None if notification else _success_response(request_id, result, modern=modern)

        return fail(-32601, "Method not found")
    except _InvalidParams:
        return fail(-32602, "Invalid params")
    except Exception:
        return fail(-32603, "Internal error")


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _parse_line(raw_line: bytes | str) -> Any:
    if isinstance(raw_line, bytes):
        text = raw_line.decode("utf-8")
    else:
        text = raw_line
    return json.loads(text, parse_constant=_reject_json_constant)


def _write_message(output: TextIO, message: Mapping[str, Any]) -> bool:
    try:
        output.write(_json_text(message) + "\n")
        output.flush()
    except (BrokenPipeError, OSError, UnicodeError):
        return False
    return True


def _configure_stdio_utf8() -> None:
    """Force the stdio protocol channel to UTF-8 on every supported platform."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            continue


def serve(service: Memleaf, *, input_stream: Any = None, output_stream: TextIO = sys.stdout) -> int:
    """Serve newline-delimited JSON-RPC messages until EOF or a broken pipe."""

    stream = input_stream if input_stream is not None else getattr(sys.stdin, "buffer", sys.stdin)
    for raw_line in stream:
        if not raw_line or not raw_line.strip():
            continue
        try:
            message = _parse_line(raw_line)
        except (UnicodeDecodeError, ValueError, TypeError):
            if not _write_message(output_stream, _error_response(None, -32700, "Parse error")):
                return 0
            continue
        try:
            response = _dispatch(message, service)
        except Exception:
            response = _error_response(
                message.get("id") if isinstance(message, dict) else None,
                -32603,
                "Internal error",
            )
        if response is not None and not _write_message(output_stream, response):
            return 0
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memleaf-mcp")
    parser.add_argument("--vault", metavar="PATH", help="memleaf vault path")
    parser.add_argument("--version", action="version", version=SERVER_INFO["version"])
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    args = _parser().parse_args(argv)
    vault = args.vault if args.vault is not None else os.environ.get("MEMLEAF_VAULT") or None
    try:
        service = Memleaf(vault)
        return serve(service)
    except (BrokenPipeError, OSError):
        return 0
    except Exception:
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "serve"]
