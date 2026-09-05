"""Small lifecycle bridges for hosts that can run command hooks.

The bridge only translates host events into the existing Core API.  It never
parses hidden reasoning or tool payloads and it always returns a safe,
non-blocking response when a hook cannot be handled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .adapters.base import mark_hook_active
from .budget import (
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_ITEMS,
    MAX_DIRECTORY_SCOPE_CHARS,
    MAX_DIRECTORY_TITLE_CHARS,
)
from .locking import atomic_write_json, read_json
from .retrieval_gate import (
    MAX_GATE_RETRIES,
    RetrievalGateError,
    bind_turn_alias,
    begin_turn,
    consume_continuation,
    continuation_marker,
    find_turn,
    find_pending_continuation,
    mark_degraded,
    observe_search,
    todo_filter_key,
    request_gate_retry,
    validate_turn,
)
from .host_runtime import HostRuntime
from .service import Memleaf
from .vault import Vault, safe_component


_MAX_TRANSCRIPT_CHUNK = 8 * 1024 * 1024
_MAX_TRANSCRIPT_LINE = 2 * 1024 * 1024
_EMPTY_INGEST = {"version": 2, "hosts": {}, "codex": {}, "transcripts": {}}
_ANTIGRAVITY_STOP_RESPONSE = {"decision": "stop"}
_CODEX_SCOPE_ITEMS = 20
_CODEX_SCOPE_CHARS = 2000
_CODEX_GATE_RETRY_REASON = (
    "Before producing the final answer, call the memleaf search tool once for this "
    "turn. Use the current conversation and the Scope Map to choose the query; "
    "a no-match result is acceptable."
)
_CODEX_GATE_ERROR_REASON = (
    "The memleaf search did not complete. Retry the memleaf search once before "
    "answering; do not treat this error as no match."
)
_CODEX_GATE_DEGRADED_MESSAGE = (
    "memleaf retrieval was unavailable after the allowed retries; continuing in "
    "degraded mode without claiming a successful search."
)
_CODEX_GATE_UNVERIFIED_MESSAGE = (
    "memleaf retrieval was not verified for this turn; continuing in degraded "
    "mode without claiming a successful search."
)
_CODEX_SCOPE_MAP_UNAVAILABLE_MESSAGE = (
    "memleaf scope map was unavailable; retrieval was not verified for this "
    "turn. Retry the turn or use the memory tools only when they are available."
)
_CODEX_PROCESS_FAILED_MESSAGE = (
    "memleaf automatic processing failed; the captured turn remains pending and "
    "has not been reported as successfully extracted."
)
_CODEX_PROCESS_DEFERRED_MESSAGE = (
    "memleaf automatic processing left deferred scope work; scope information is "
    "still needed and automatic memory extraction is not fully complete."
)
_SCOPE_MAP_INCOMPLETE = (
    "Scope Map preview incomplete; fetch scope_catalog from the first page "
    "before assuming a scope is absent."
)


def handle_event(
    host: str,
    event: Mapping[str, Any],
    *,
    vault: Vault | Path | str | None = None,
    event_name: str | None = None,
) -> dict[str, Any]:
    """Handle one command-hook payload and return that host's JSON response."""

    try:
        if not isinstance(event, Mapping):
            if host == "antigravity" and _is_antigravity_stop_event(event, event_name):
                return _antigravity_stop_response()
            return {}
        if host == "codex":
            return _handle_codex(event, _coerce_vault(vault), event_name=event_name)
        if host == "antigravity":
            return _handle_antigravity(event, _coerce_vault(vault), event_name=event_name)
    except Exception:
        # A memory hook must never block the host conversation.  Deliberately
        # do not expose exception text: it may contain a path or credential.
        if host == "antigravity" and _is_antigravity_stop_event(event, event_name):
            return _antigravity_stop_response()
        if host == "codex":
            codex_event_name = _normalize_event_name(
                event_name or _field(event, "hook_event_name", "hookEventName")
            )
            if codex_event_name == "Stop":
                return {"systemMessage": _CODEX_GATE_UNVERIFIED_MESSAGE}
            if codex_event_name in {"UserPromptSubmit", "SessionStart"}:
                return {"systemMessage": _CODEX_SCOPE_MAP_UNAVAILABLE_MESSAGE}
        return {}
    return {}


def _coerce_vault(value: Vault | Path | str | None) -> Vault:
    return value if isinstance(value, Vault) else Vault(value)


def _field(event: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in event:
            return event[name]
    return None


def _antigravity_stop_response() -> dict[str, str]:
    """Return the required non-blocking response for an Antigravity Stop hook."""

    return dict(_ANTIGRAVITY_STOP_RESPONSE)


def _is_antigravity_stop_event(event: Any, event_name: str | None) -> bool:
    """Identify a Stop invocation without making invalid payloads blocking."""

    if event_name is not None:
        return _normalize_event_name(event_name) == "Stop"
    if not isinstance(event, Mapping):
        return False
    explicit_name = _normalize_event_name(_field(event, "hook_event_name", "hookEventName"))
    if explicit_name is not None:
        return explicit_name == "Stop"
    # Antigravity invokes each configured command with no event name.  Its
    # PreInvocation payload carries invocationNum; the remaining payload shape
    # is the Stop hook, including malformed/missing-field payloads.
    return "invocationNum" not in event


def _safe_identifier(value: Any, fallback: str) -> str | None:
    if isinstance(value, str) and value and "\x00" not in value and "\n" not in value and "\r" not in value:
        try:
            return safe_component(value, fallback)
        except ValueError:
            pass
    if value is None:
        return None
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"{fallback}-{digest}"


def _codex_identity(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    session_id = _safe_identifier(_field(event, "session_id", "sessionId"), "session")
    turn_id = _safe_identifier(_field(event, "turn_id", "turnId"), "turn")
    return session_id, turn_id


def _is_subagent(event: Mapping[str, Any]) -> bool:
    # The main Codex turn hooks do not carry these fields.  If a caller does,
    # it is safer to exclude the subagent than to mix its transcript into the
    # user's main session.
    return any(
        event.get(name) not in (None, "", False)
        for name in ("agent_id", "agentId", "agent_type", "agentType", "subagent", "is_subagent")
    )


def _handle_codex(
    event: Mapping[str, Any],
    vault: Vault,
    *,
    event_name: str | None,
) -> dict[str, Any]:
    """Translate Codex hook events into the shared host lifecycle runtime."""

    if _is_subagent(event):
        return {}
    name = _normalize_event_name(event_name or _field(event, "hook_event_name", "hookEventName"))
    if not isinstance(name, str):
        return {}
    session_id, turn_id = _codex_identity(event)
    if session_id is None:
        return {}

    runtime = HostRuntime(Memleaf(vault), "codex")

    if name == "SessionStart":
        source = _field(event, "source")
        if source != "compact":
            return {}
        catalog = runtime.scope_catalog(limit=_CODEX_SCOPE_ITEMS)
        if not _valid_scope_catalog(catalog):
            return {"systemMessage": _CODEX_SCOPE_MAP_UNAVAILABLE_MESSAGE}
        context = _format_scope_catalog(catalog)
        if not context:
            return {}
        mark_hook_active(vault.root, "codex")
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }

    if name == "UserPromptSubmit":
        prompt = _field(event, "prompt")
        if not isinstance(prompt, str) or not prompt.strip() or turn_id is None:
            return {}
        opened = runtime.open_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_content=prompt,
            allow_continuation=True,
        )
        if opened.continuation or opened.injection_delivered:
            return {}

        # Automatic injection remains scope-only.  HostRuntime owns the turn
        # lifecycle; this bridge only renders the existing Codex wire format.
        catalog = runtime.scope_catalog(limit=_CODEX_SCOPE_ITEMS)
        if not _valid_scope_catalog(catalog):
            return {"systemMessage": _CODEX_SCOPE_MAP_UNAVAILABLE_MESSAGE}
        context = _format_scope_catalog(catalog)
        if not runtime.mark_injection_delivered(session_id, turn_id):
            return {}
        mark_hook_active(vault.root, "codex")
        if not context:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }

    if name == "PreToolUse":
        return _handle_codex_pre_tool(runtime, event, session_id, turn_id)

    if name == "PostToolUse":
        return _handle_codex_post_tool(runtime, event, session_id, turn_id)

    if name != "Stop" or turn_id is None:
        return {}

    retrieval_present = _codex_event_retrieval_id(vault, session_id, turn_id) is not None
    completion = runtime.complete_turn(
        session_id=session_id,
        turn_id=turn_id,
        assistant_content=_field(event, "last_assistant_message", "lastAssistantMessage"),
        auto_process=True,
    )
    if completion.retry_required and completion.retry_reason:
        return {"decision": "block", "reason": completion.retry_reason}

    if completion.captured and not completion.process_failed:
        mark_hook_active(vault.root, "codex")

    notices = []
    if completion.degraded:
        notices.append(
            _CODEX_GATE_DEGRADED_MESSAGE
            if retrieval_present
            else _CODEX_GATE_UNVERIFIED_MESSAGE
        )
    if completion.process_failed:
        notices.append(_CODEX_PROCESS_FAILED_MESSAGE)
    if completion.process_deferred:
        notices.append(_CODEX_PROCESS_DEFERRED_MESSAGE)
    if notices:
        return {"systemMessage": " ".join(notices)}
    return {}

def _codex_memleaf_tool(tool_name: Any, suffix: str) -> bool:
    return isinstance(tool_name, str) and tool_name == f"mcp__memleaf__{suffix}"


def _codex_event_retrieval_id(
    vault: Vault,
    session_id: str | None,
    turn_id: str | None,
) -> str | None:
    if session_id is None or turn_id is None:
        return None
    try:
        return find_turn(vault, "codex", session_id, turn_id)
    except RetrievalGateError:
        return None


def _handle_codex_pre_tool(
    runtime: HostRuntime,
    event: Mapping[str, Any],
    session_id: str | None,
    turn_id: str | None,
) -> dict[str, Any]:
    tool_name = _field(event, "tool_name", "toolName")
    if not (
        _codex_memleaf_tool(tool_name, "search")
        or _codex_memleaf_tool(tool_name, "list_todos")
        or _codex_memleaf_tool(tool_name, "read")
    ):
        return {}
    if session_id is None or turn_id is None:
        return {}
    prepared = runtime.prepare_memory_tool(
        session_id=session_id,
        turn_id=turn_id,
        arguments=_field(event, "tool_input", "toolInput"),
    )
    if not prepared.allowed or prepared.arguments is None:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": prepared.reason or "memleaf retrieval unavailable",
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            # Codex only applies updatedInput when the hook explicitly allows
            # the call.  Omitting this field makes the rewrite invalid and
            # would drop memleaf's current-turn retrieval_id binding.
            "permissionDecision": "allow",
            "updatedInput": prepared.arguments,
        }
    }

def _json_tool_result(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("isError") is True:
        return value
    nested = value.get("structuredContent")
    if isinstance(nested, Mapping):
        if isinstance(nested.get("result"), Mapping):
            return nested["result"]
        return nested
    content = value.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                continue
            try:
                decoded = json.loads(str(item.get("text", "")))
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, Mapping):
                return decoded
    return value


def _codex_search_status(value: Any) -> str:
    if not isinstance(value, Mapping) or value.get("isError") is True or "error" in value:
        return "error"
    result = _json_tool_result(value)
    if not isinstance(result, Mapping) or result.get("isError") is True or "error" in result:
        return "error"
    status = result.get("status")
    results = result.get("results")
    if status not in {"found", "no_match"} or not isinstance(results, list):
        return "error"
    if not all(
        isinstance(item, Mapping)
        and set(item) == {"memory_id", "title"}
        and isinstance(item.get("memory_id"), str)
        and bool(item.get("memory_id"))
        and isinstance(item.get("title"), str)
        and bool(item.get("title"))
        for item in results
    ):
        return "error"
    if status == "found" and results:
        return "found"
    if status == "no_match" and not results:
        return "no_match"
    return "error"



def _codex_todo_result(value: Any) -> tuple[str, bool, str | None]:
    result = _json_tool_result(value)
    if not isinstance(result, Mapping) or result.get("isError") is True or "error" in result:
        return "error", False, None
    status = result.get("status")
    items = result.get("results")
    has_more = result.get("has_more")
    next_cursor = result.get("next_cursor")
    if status not in {"found", "no_match"} or not isinstance(items, list) or not isinstance(has_more, bool):
        return "error", False, None
    if not all(
        isinstance(item, Mapping)
        and set(item) == {"memory_id", "title", "due_date"}
        and isinstance(item.get("memory_id"), str) and bool(item.get("memory_id"))
        and isinstance(item.get("title"), str) and bool(item.get("title"))
        and (item.get("due_date") is None or isinstance(item.get("due_date"), str))
        for item in items
    ):
        return "error", False, None
    if (has_more and not isinstance(next_cursor, str)) or (not has_more and next_cursor is not None):
        return "error", False, None
    if status == "found" and not items:
        return "error", False, None
    if status == "no_match" and items:
        return "error", False, None
    return status, has_more, next_cursor if isinstance(next_cursor, str) else None


def _handle_codex_post_tool(
    runtime: HostRuntime,
    event: Mapping[str, Any],
    session_id: str | None,
    turn_id: str | None,
) -> dict[str, Any]:
    tool_name = _field(event, "tool_name", "toolName")
    is_search = _codex_memleaf_tool(tool_name, "search")
    is_todos = _codex_memleaf_tool(tool_name, "list_todos")
    if session_id is None or turn_id is None:
        return {}
    if not (is_search or is_todos):
        call_id = _field(event, "tool_use_id", "toolUseId")
        if isinstance(tool_name, str) and isinstance(call_id, str):
            runtime.observe_external_tool(session_id=session_id, turn_id=turn_id,
                                          tool_name=tool_name, call_id=call_id,
                                          payload=_field(event, "tool_response", "toolResponse"),
                                          tool_input=_field(event, "tool_input", "toolInput"))
        return {}
    tool_input = _field(event, "tool_input", "toolInput")
    call_id = _field(event, "tool_use_id", "toolUseId")
    if not isinstance(tool_input, Mapping) or not isinstance(call_id, str):
        return {}
    if is_search:
        runtime.observe_search(
            session_id=session_id,
            turn_id=turn_id,
            status=_codex_search_status(_field(event, "tool_response", "toolResponse")),
            call_id=call_id,
            supplied_retrieval_id=tool_input.get("retrieval_id"),
        )
        return {}
    status, has_more, next_cursor = _codex_todo_result(_field(event, "tool_response", "toolResponse"))
    runtime.observe_todo_list(
        session_id=session_id,
        turn_id=turn_id,
        status=status,
        call_id=call_id,
        supplied_retrieval_id=tool_input.get("retrieval_id"),
        filter_key=todo_filter_key(tool_input),
        cursor=tool_input.get("cursor") if isinstance(tool_input.get("cursor"), str) else None,
        has_more=has_more,
        next_cursor=next_cursor,
    )
    return {}

@dataclass(frozen=True)
class _TranscriptRecord:
    type: str
    step_index: int
    content: str
    has_tool_calls: bool = False
    start_offset: int = 0
    end_offset: int = 0


def _handle_antigravity(
    event: Mapping[str, Any],
    vault: Vault,
    *,
    event_name: str | None,
) -> dict[str, Any]:
    name = _normalize_event_name(event_name or _field(event, "hook_event_name", "hookEventName"))
    if not isinstance(name, str):
        # Antigravity's payload does not need to include its hook name when the
        # command is configured separately for each event.
        name = "PreInvocation" if "invocationNum" in event else "Stop"
    transcript = _transcript_path(event)
    if transcript is None:
        return _antigravity_stop_response() if name == "Stop" else {}
    session_id = _antigravity_session(event, transcript)
    if session_id is None:
        return _antigravity_stop_response() if name == "Stop" else {}
    key = _transcript_key(session_id, transcript)
    state = _read_ingest_state(vault)
    entry = _transcript_state(state, key, transcript)
    service = Memleaf(vault)
    runtime = HostRuntime(service, "antigravity")

    if name == "PreInvocation":
        first_read = not entry.get("initialized", False)
        previous_offset = entry["pre_offset"]
        records, offset, valid = _read_records(transcript, previous_offset)
        if not valid:
            return {}
        if first_read:
            # Do not backfill completed history on the first hook call.  Keep
            # only the still-open segment after the last visible assistant;
            # several user rows in that segment are one logical turn.
            user_records = _open_segment_users(records)
            if user_records:
                entry["capture_offset"] = min(record.start_offset for record in user_records)
            else:
                entry["capture_offset"] = offset
        entry["initialized"] = True
        new_users = [record for record in records if _is_user_record(record)]
        if first_read:
            new_users = user_records
        injected = set(entry["injected_users"])
        pending = _pending_groups(entry)
        assistants = [record for record in records if _is_assistant_record(record)]
        pending = _merge_user_groups(session_id, pending, new_users, assistants)
        captured_steps: set[int] = set()
        capture_failed = False
        for record in new_users:
            group = next(
                (item for item in pending if record.step_index in item["user_steps"]),
                None,
            )
            try:
                runtime.capture(
                    source="antigravity",
                    session_id=session_id,
                    turn_id=(
                        group["turn_id"]
                        if group is not None
                        else _antigravity_turn_id(session_id, record.step_index)
                    ),
                    role="user",
                    content=record.content,
                    event_id=_antigravity_event_id(session_id, record.step_index, "user"),
                )
            except Exception:
                capture_failed = True
                continue
            captured_steps.add(record.step_index)
        entry["injected_users"] = sorted(injected)[-256:]
        entry["pending"] = _trim_groups(pending)
        entry["pre_offset"] = (
            min((record.start_offset for record in new_users), default=offset)
            if capture_failed
            else offset
        )
        _write_ingest_state(vault, state, transcript_key=key)
        candidates = [record for record in new_users if record.step_index in captured_steps and record.step_index not in injected]
        latest = max(candidates, key=lambda record: record.step_index) if candidates else None
        if latest is None:
            return {}
        try:
            memories = service.context(
                latest.content,
                source="antigravity",
                session_id=session_id,
                project_path=_workspace_path(event),
            )
        except Exception:
            if latest is not None:
                entry["pre_offset"] = min(entry["pre_offset"], latest.start_offset)
                _write_ingest_state(vault, state, transcript_key=key)
            return {}
        context = _format_context(memories)
        entry["injected_users"] = sorted(set(entry["injected_users"]) | {latest.step_index})[-256:]
        _write_ingest_state(vault, state, transcript_key=key)
        if captured_steps and not capture_failed:
            mark_hook_active(vault.root, "antigravity")
        return {"injectSteps": [{"ephemeralMessage": context}]} if context else {}

    if name != "Stop":
        return {}
    if not _normal_antigravity_stop(event):
        return _antigravity_stop_response()
    records, offset, valid = _read_records(transcript, entry["capture_offset"])
    if not valid:
        return _antigravity_stop_response()
    if not entry.get("initialized", False):
        entry["initialized"] = True
        users = _open_segment_users(records)
        if users:
            entry["capture_offset"] = min(record.start_offset for record in users)
        entry["pre_offset"] = offset
    else:
        users = [record for record in records if _is_user_record(record)]

    pending = _pending_groups(entry)
    assistants = [record for record in records if _is_assistant_record(record)]
    pending = _merge_user_groups(session_id, pending, users, assistants)
    capture_failed = False
    hook_succeeded = False
    failed_user_steps: set[int] = set()
    for group in pending:
        turn_id = group["turn_id"]
        for step_index in group["user_steps"]:
            record = next((item for item in users if item.step_index == step_index), None)
            if record is None:
                continue
            try:
                runtime.capture(
                    source="antigravity",
                    session_id=session_id,
                    turn_id=turn_id,
                    role="user",
                    content=record.content,
                    event_id=_antigravity_event_id(session_id, step_index, "user"),
                )
            except Exception:
                capture_failed = True
                failed_user_steps.add(step_index)
            else:
                hook_succeeded = True

    complete = 0
    remaining: list[dict[str, Any]] = []
    ordered_pending = sorted(pending, key=lambda item: item["user_steps"][0])
    for index, group in enumerate(ordered_pending):
        next_step = ordered_pending[index + 1]["user_steps"][0] if index + 1 < len(ordered_pending) else None
        assistant = _assistant_for_group(group, assistants, next_step)
        if assistant is None:
            remaining.append(group)
            continue
        if any(step in failed_user_steps for step in group["user_steps"]):
            remaining.append(group)
            continue
        try:
            runtime.capture(
                source="antigravity",
                session_id=session_id,
                turn_id=group["turn_id"],
                role="assistant",
                content=assistant.content,
                event_id=_antigravity_event_id(session_id, assistant.step_index, "assistant"),
            )
        except Exception:
            remaining.append(group)
            capture_failed = True
            continue
        hook_succeeded = True
        complete += 1

    entry["pending"] = _trim_groups(remaining)
    if capture_failed:
        entry["capture_offset"] = min(
            entry["capture_offset"],
            *(record.start_offset for record in users),
        ) if users else entry["capture_offset"]
        entry["pre_offset"] = min(entry["pre_offset"], entry["capture_offset"])
    else:
        entry["capture_offset"] = offset
        entry["pre_offset"] = offset
    if complete:
        entry["process_pending"] = True
    _write_ingest_state(vault, state, transcript_key=key)

    if entry.get("process_pending") is True:
        try:
            runtime.process(source="antigravity", session_id=session_id)
        except Exception:
            return _antigravity_stop_response()
        entry["process_pending"] = False
        _write_ingest_state(vault, state, transcript_key=key)
        hook_succeeded = True
    if hook_succeeded and not capture_failed:
        mark_hook_active(vault.root, "antigravity")
    return _antigravity_stop_response()


def _transcript_path(event: Mapping[str, Any]) -> Path | None:
    value = _field(event, "transcriptPath", "transcript_path")
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        return None
    return path


def _normalize_event_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    aliases = {
        "user-prompt": "UserPromptSubmit",
        "user_prompt_submit": "UserPromptSubmit",
        "userpromptsubmit": "UserPromptSubmit",
        "session-start": "SessionStart",
        "session_start": "SessionStart",
        "sessionstart": "SessionStart",
        "pre-invocation": "PreInvocation",
        "pre_invocation": "PreInvocation",
        "preinvocation": "PreInvocation",
        "post-invocation": "PostInvocation",
        "post_invocation": "PostInvocation",
        "postinvocation": "PostInvocation",
        "stop": "Stop",
    }
    return aliases.get(value.casefold(), value)


def _antigravity_session(event: Mapping[str, Any], transcript: Path) -> str | None:
    value = _field(event, "conversationId", "conversation_id", "session_id")
    session = _safe_identifier(value, "session")
    if session is not None:
        return session
    return f"session-{hashlib.sha256(str(transcript).encode('utf-8')).hexdigest()[:32]}"


def _workspace_path(event: Mapping[str, Any]) -> str | None:
    values = _field(event, "workspacePaths", "workspace_paths")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value:
                return value
    return None


def _normal_antigravity_stop(event: Mapping[str, Any]) -> bool:
    reason = _field(event, "terminationReason", "termination_reason")
    fully_idle = _field(event, "fullyIdle", "fully_idle")
    return reason == "model_stop" and fully_idle is True


def _antigravity_turn_id(session_id: str, step_index: int) -> str:
    digest = hashlib.sha256(f"{session_id}:{step_index}".encode("utf-8")).hexdigest()[:32]
    return f"turn-{digest}"


def _antigravity_event_id(session_id: str, step_index: int, role: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{step_index}:{role}".encode("utf-8")).hexdigest()[:32]
    return f"antigravity-{role}-{digest}"


def _is_user_record(record: _TranscriptRecord) -> bool:
    return record.type == "USER_INPUT" and bool(record.content.strip())


def _is_assistant_record(record: _TranscriptRecord) -> bool:
    # A visible planner response may carry a separate ``thinking`` field.
    # Only an explicit tool-call row is an intermediate step; the parser never
    # persists the other hidden fields.
    return record.type == "PLANNER_RESPONSE" and bool(record.content.strip()) and not record.has_tool_calls


def _open_segment_users(records: list[_TranscriptRecord]) -> list[_TranscriptRecord]:
    users = [record for record in records if _is_user_record(record)]
    if not users:
        return []
    last_user = max(record.step_index for record in users)
    assistants = [record for record in records if _is_assistant_record(record)]
    last_assistant = max(
        (record.step_index for record in assistants if record.step_index < last_user),
        default=-1,
    )
    return [
        record
        for record in users
        if last_assistant < record.step_index <= last_user
    ]


def _assistant_between(
    assistants: list[_TranscriptRecord], lower: int, upper: int
) -> bool:
    return any(lower < record.step_index < upper for record in assistants)


def _merge_user_groups(
    session_id: str,
    pending: list[dict[str, Any]],
    users: list[_TranscriptRecord],
    assistants: list[_TranscriptRecord],
) -> list[dict[str, Any]]:
    groups = [
        {"turn_id": item["turn_id"], "user_steps": list(item["user_steps"])}
        for item in pending
        if item.get("user_steps")
    ]
    groups.sort(key=lambda item: item["user_steps"][0])
    merged_groups: list[dict[str, Any]] = []
    for group in groups:
        previous = merged_groups[-1] if merged_groups else None
        if previous is not None and not _assistant_between(
            assistants,
            previous["user_steps"][-1],
            group["user_steps"][0],
        ):
            previous["user_steps"].extend(group["user_steps"])
        else:
            merged_groups.append(group)
    groups = merged_groups
    assigned = {step for item in groups for step in item["user_steps"]}
    for record in sorted(users, key=lambda item: item.step_index):
        step = record.step_index
        if step in assigned:
            continue
        previous = groups[-1] if groups else None
        if previous is not None:
            previous_step = previous["user_steps"][-1]
            same_segment = not _assistant_between(assistants, previous_step, step)
        else:
            same_segment = False
        if previous is not None and same_segment:
            previous["user_steps"].append(step)
        else:
            groups.append(
                {
                    "turn_id": _antigravity_turn_id(session_id, step),
                    "user_steps": [step],
                }
            )
        assigned.add(step)
    return [
        {"turn_id": item["turn_id"], "user_steps": sorted(set(item["user_steps"]))}
        for item in sorted(groups, key=lambda value: value["user_steps"][0])
        if item.get("user_steps")
    ]


def _assistant_for_group(
    group: Mapping[str, Any],
    assistants: list[_TranscriptRecord],
    next_group_step: int | None,
) -> _TranscriptRecord | None:
    steps = group.get("user_steps")
    if not isinstance(steps, list) or not steps:
        return None
    last_user = max(step for step in steps if type(step) is int)
    candidates = [
        record
        for record in assistants
        if record.step_index > last_user
        and (next_group_step is None or record.step_index < next_group_step)
    ]
    return max(candidates, key=lambda record: record.step_index) if candidates else None


def _trim_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"turn_id": item["turn_id"], "user_steps": sorted(set(item["user_steps"]))[-256:]}
        for item in sorted(groups, key=lambda value: value["user_steps"][0])[-256:]
    ]


def _read_records(path: Path, offset: int) -> tuple[list[_TranscriptRecord], int, bool]:
    try:
        size = path.stat().st_size
        if offset < 0 or offset > size:
            return [], offset, False
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(_MAX_TRANSCRIPT_CHUNK)
    except (OSError, UnicodeError):
        return [], offset, False
    complete: list[bytes] = []
    consumed = 0
    for line in data.splitlines(keepends=True):
        if not line.endswith((b"\n", b"\r")):
            break
        complete.append(line)
        consumed += len(line)
    records: list[_TranscriptRecord] = []
    cursor = offset
    for raw in complete:
        start_offset = cursor
        cursor += len(raw)
        if len(raw) > _MAX_TRANSCRIPT_LINE:
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            continue
        if not isinstance(value, Mapping):
            continue
        record_type = value.get("type")
        source = value.get("source")
        status = value.get("status")
        step_index = value.get("step_index")
        if step_index is None:
            step_index = value.get("stepIndex", value.get("stepIdx"))
        content = value.get("content")
        if (
            record_type not in ("USER_INPUT", "PLANNER_RESPONSE")
            or source not in ("USER_EXPLICIT", "MODEL")
            or status != "DONE"
            or type(step_index) is not int
            or step_index < 0
            or not isinstance(content, str)
        ):
            # Other record types are expected in Antigravity's transcript.
            # A malformed explicit user row is different: stop before its
            # offset so a schema change cannot silently advance the cursor.
            if record_type == "USER_INPUT" and source == "USER_EXPLICIT" and status == "DONE":
                return records, start_offset, False
            continue
        records.append(
            _TranscriptRecord(
                type=record_type,
                step_index=step_index,
                content=content,
                has_tool_calls="tool_calls" in value,
                start_offset=start_offset,
                end_offset=cursor,
            )
        )
    return records, offset + consumed, True


def _read_ingest_state(vault: Vault) -> dict[str, Any]:
    return _read_ingest_file(vault.host_ingest_path)


def _read_ingest_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.exists():
        return json.loads(json.dumps(_EMPTY_INGEST))
    try:
        value = read_json(path)
    except (OSError, UnicodeError, TypeError, ValueError):
        return json.loads(json.dumps(_EMPTY_INGEST))
    if not isinstance(value, dict):
        return json.loads(json.dumps(_EMPTY_INGEST))
    state = json.loads(json.dumps(_EMPTY_INGEST))
    for key in ("hosts", "codex", "transcripts"):
        if isinstance(value.get(key), dict):
            state[key] = dict(value[key])
    return state


def _write_ingest_state(
    vault: Vault,
    state: Mapping[str, Any],
    *,
    codex_session: str | None = None,
    transcript_key: str | None = None,
) -> None:
    path = vault.host_ingest_path
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return
    try:
        with vault.lock():
            # Read and merge while holding the same vault lock as the atomic
            # write.  A stale caller state may update one session, but it must
            # never erase another session written between its read and write.
            current = _read_ingest_file(path)
            merged = dict(current)
            if codex_session is not None:
                incoming_codex = state.get("codex")
                if isinstance(incoming_codex, Mapping) and codex_session in incoming_codex:
                    bucket = merged.get("codex")
                    if not isinstance(bucket, dict):
                        bucket = {}
                    bucket[codex_session] = incoming_codex[codex_session]
                    merged["codex"] = bucket
            elif transcript_key is not None:
                incoming_transcripts = state.get("transcripts")
                if isinstance(incoming_transcripts, Mapping) and transcript_key in incoming_transcripts:
                    bucket = merged.get("transcripts")
                    if not isinstance(bucket, dict):
                        bucket = {}
                    bucket[transcript_key] = incoming_transcripts[transcript_key]
                    merged["transcripts"] = bucket
            else:
                for key in ("hosts", "codex", "transcripts"):
                    incoming = state.get(key)
                    if not isinstance(incoming, Mapping):
                        continue
                    bucket = merged.get(key)
                    if not isinstance(bucket, dict):
                        bucket = {}
                    bucket.update(dict(incoming))
                    merged[key] = bucket
            atomic_write_json(path, merged, mode=0o600)
    except Exception:
        return


def _transcript_key(session_id: str, path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]
    return f"{session_id}/{digest}"


def _transcript_state(state: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    transcripts = state.setdefault("transcripts", {})
    current = transcripts.get(key)
    if not isinstance(current, dict) or current.get("path_hash") != hashlib.sha256(str(path).encode("utf-8")).hexdigest():
        current = {
            "path_hash": hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
            "pre_offset": 0,
            "capture_offset": 0,
            "injected_users": [],
            "pending": [],
            "process_pending": False,
            "initialized": False,
        }
        transcripts[key] = current
    for field in ("pre_offset", "capture_offset"):
        if type(current.get(field)) is not int or current[field] < 0:
            current[field] = 0
    injected = current.get("injected_users")
    if not isinstance(injected, list):
        current["injected_users"] = []
    else:
        current["injected_users"] = [item for item in injected if type(item) is int and item >= 0][-256:]
    if not isinstance(current.get("pending"), list):
        current["pending"] = []
    if not isinstance(current.get("process_pending"), bool):
        current["process_pending"] = False
    if not isinstance(current.get("initialized"), bool):
        current["initialized"] = False
    return current


def _pending_groups(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    values = entry.get("pending", [])
    if not isinstance(values, list):
        return result
    for item in values:
        if not isinstance(item, Mapping):
            continue
        turn_id = item.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        steps = item.get("user_steps")
        if not isinstance(steps, list):
            # Migrate the original one-user pending shape in memory.  The
            # next write stores the grouped shape without losing its turn.
            step = item.get("step_index")
            steps = [step] if type(step) is int else []
        normalized = sorted({step for step in steps if type(step) is int and step >= 0})
        if normalized:
            result.append({"turn_id": turn_id, "user_steps": normalized})
    return sorted(result, key=lambda item: item["user_steps"][0])


def _codex_pending(vault: Vault, session_id: str) -> bool:
    state = _read_ingest_state(vault)
    value = state.get("codex", {}).get(session_id) if isinstance(state.get("codex"), dict) else None
    if isinstance(value, Mapping):
        return value.get("process_pending") is True
    return value is True


def _set_codex_pending(vault: Vault, session_id: str, value: bool) -> None:
    state = _read_ingest_state(vault)
    codex = state.setdefault("codex", {})
    codex[session_id] = _codex_entry(codex.get(session_id), process_pending=bool(value))
    _write_ingest_state(vault, state, codex_session=session_id)


def _codex_entry(value: Any, *, process_pending: bool | None = None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        pending = value.get("process_pending") is True
        injected = value.get("injected_turn_ids")
        if not isinstance(injected, list):
            injected = []
        injected = [item for item in injected if isinstance(item, str) and item][-256:]
    else:
        pending = value is True
        injected = []
    if process_pending is not None:
        pending = process_pending
    return {"process_pending": pending, "injected_turn_ids": injected}


def _codex_injection_consumed(vault: Vault, session_id: str, turn_id: str) -> bool:
    state = _read_ingest_state(vault)
    codex = state.get("codex")
    value = codex.get(session_id) if isinstance(codex, dict) else None
    entry = _codex_entry(value)
    return turn_id in entry["injected_turn_ids"]


def _consume_codex_injection(vault: Vault, session_id: str, turn_id: str) -> bool:
    path = vault.host_ingest_path
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return False
    try:
        with vault.lock():
            state = _read_ingest_file(path)
            codex = state.setdefault("codex", {})
            entry = _codex_entry(codex.get(session_id))
            if turn_id in entry["injected_turn_ids"]:
                return False
            entry["injected_turn_ids"] = (entry["injected_turn_ids"] + [turn_id])[-256:]
            codex[session_id] = entry
            atomic_write_json(path, state, mode=0o600)
            return True
    except Exception:
        return False


def _format_context(memories: Any) -> str:
    if not isinstance(memories, (list, tuple)):
        return ""
    header = (
        "Relevant memleaf memory directory. First read only the best project/identifier match; "
        "read more only if needed or for explicit comparison; do not read all entries to filter "
        "unrelated items. IDs are leads; use memleaf MCP read(memory_id) before relying on past facts:"
    )
    lines = [header]
    used = len(header)
    selected = 0
    for memory in memories:
        if selected >= MAX_CONTEXT_ITEMS:
            break
        if isinstance(memory, Mapping):
            memory_id = memory.get("memory_id")
            title = memory.get("title")
            scopes = memory.get("scopes")
        else:
            memory_id = getattr(memory, "memory_id", None)
            title = getattr(memory, "title", None)
            scopes = getattr(memory, "scopes", None)
        if not isinstance(memory_id, str) or not memory_id or "\n" in memory_id or "\r" in memory_id:
            continue
        if not isinstance(title, str):
            title = ""
        title = " ".join(title.split())
        if len(title) > MAX_DIRECTORY_TITLE_CHARS:
            title = title[: MAX_DIRECTORY_TITLE_CHARS - 1].rstrip() + "…"
        if isinstance(scopes, str):
            scopes = [scopes]
        if not isinstance(scopes, (list, tuple)):
            scopes = []
        display_scopes = []
        for scope in scopes:
            if not isinstance(scope, str):
                continue
            normalized_scope = " ".join(scope.split())
            if normalized_scope:
                display_scopes.append(normalized_scope[:MAX_DIRECTORY_SCOPE_CHARS])
        projects = [scope for scope in display_scopes if scope.startswith("project:")]
        scope_text = ",".join(dict.fromkeys(projects or display_scopes)) or "global"
        line = f"- {memory_id} | {title} | {scope_text}"
        addition = len(line) + 1
        if used + addition > MAX_CONTEXT_CHARS:
            # Keep the complete ID so a caller can still issue read(id), while
            # shortening only the optional display title.
            prefix = f"- {memory_id} | "
            suffix = f" | {scope_text}"
            title_budget = MAX_CONTEXT_CHARS - used - 1 - len(prefix) - len(suffix)
            if title_budget > 1:
                shortened = title[: title_budget - 1].rstrip() + "…"
                line = f"{prefix}{shortened}{suffix}"
                addition = len(line) + 1
        if used + addition > MAX_CONTEXT_CHARS:
            # A pathological identifier may not fit in the remaining budget;
            # skip that entry rather than truncating its ID.
            continue
        lines.append(line)
        used += addition
        selected += 1
    return "\n".join(lines) if selected else ""


def _valid_scope_catalog(catalog: Any) -> bool:
    if not isinstance(catalog, Mapping) or not isinstance(catalog.get("scopes"), list):
        return False
    has_more = catalog.get("has_more")
    cursor = catalog.get("next_cursor")
    if not isinstance(has_more, bool) or (cursor is not None and not isinstance(cursor, str)):
        return False
    if isinstance(cursor, str) and ("\n" in cursor or "\r" in cursor):
        return False
    if (has_more and not cursor) or (not has_more and cursor is not None):
        return False
    for item in catalog["scopes"]:
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


def _format_scope_catalog(catalog: Any) -> str:
    """Render only the v2 Scope Map for Codex's developer context.

    This intentionally accepts the core's mapping envelope as well as a
    plain list for small compatibility fixtures.  It never renders memory
    identifiers, titles, bodies, or any other per-memory field.
    """

    is_mapping = isinstance(catalog, Mapping)
    values = catalog.get("scopes") if is_mapping else catalog
    if not isinstance(values, (list, tuple)):
        return ""
    if is_mapping and not _valid_scope_catalog(catalog):
        # Never turn a malformed catalog into the empty-scope instruction.
        return ""
    has_more = catalog.get("has_more") if is_mapping else False
    next_cursor = catalog.get("next_cursor") if is_mapping else None
    cursor_text = (
        next_cursor
        if isinstance(next_cursor, str)
        and next_cursor
        and "\n" not in next_cursor
        and "\r" not in next_cursor
        else None
    )
    header = (
        "<memleaf-scope-map>\n"
        "Available memleaf memory scopes. For this turn, call memleaf MCP "
        "search at least once; choose the scope and query from the current "
        "conversation. Search returns a directory; read only the selected "
        "memory when needed. A no-match result is valid.\n"
    )
    incomplete = bool(has_more)
    if has_more:
        pagination = "More scopes are available; fetch the next scope_catalog page"
        if cursor_text is not None:
            candidate = f"{pagination} with next_cursor={cursor_text}.\n"
            if len(header) + len(candidate) + len("\n</memleaf-scope-map>") <= _CODEX_SCOPE_CHARS:
                pagination = candidate
            else:
                pagination = f"{pagination}; next_cursor is unavailable within this preview budget.\n"
                incomplete = True
        else:
            pagination = f"{pagination}; next_cursor is unavailable.\n"
            incomplete = True
        header += pagination
    elif is_mapping:
        header += "scope_catalog page has_more=False; next_cursor=None.\n"
    suffix = "\n</memleaf-scope-map>"
    lines: list[str] = []
    used = len(header) + len(suffix)
    marker_reserve = len(_SCOPE_MAP_INCOMPLETE) + 1
    for item in values:
        if len(lines) >= _CODEX_SCOPE_ITEMS:
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
        # Scope identifiers are lookup keys. Never shorten or normalize the
        # identifier; drop the complete item only if it cannot fit.
        if parent is None:
            parent = ""
        elif not isinstance(parent, str) or "\n" in parent or "\r" in parent:
            incomplete = True
            parent = ""
        else:
            if len(parent) > MAX_DIRECTORY_SCOPE_CHARS:
                parent = ""
                incomplete = True
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, (list, tuple)):
            incomplete = True
            aliases = []
        clean_aliases = []
        for alias in aliases:
            if not isinstance(alias, str) or not alias or "\n" in alias or "\r" in alias:
                incomplete = True
                continue
            alias = " ".join(alias.split())
            if len(alias) <= MAX_DIRECTORY_SCOPE_CHARS:
                clean_aliases.append(alias)
            else:
                incomplete = True
        line = f"- {scope}"
        if parent:
            line += f" (parent: {parent})"
        if clean_aliases:
            line += f" [aliases: {', '.join(dict.fromkeys(clean_aliases))}]"
        addition = len(line) + 1
        if used + addition + marker_reserve > _CODEX_SCOPE_CHARS:
            base_line = f"- {scope}"
            base_addition = len(base_line) + 1
            if line != base_line and used + base_addition + marker_reserve <= _CODEX_SCOPE_CHARS:
                line = base_line
                addition = base_addition
                incomplete = True
            else:
                incomplete = True
                continue
        if used + addition > _CODEX_SCOPE_CHARS:
            incomplete = True
            continue
        lines.append(line)
        used += addition
    if not lines:
        if values:
            incomplete = True
        else:
            lines.append("- (no registered scopes; search without a scope if appropriate)")
    if incomplete:
        addition = len(_SCOPE_MAP_INCOMPLETE) + 1
        if used + addition <= _CODEX_SCOPE_CHARS:
            lines.append(_SCOPE_MAP_INCOMPLETE)
    return header + "\n".join(lines) + suffix


def process_host_event(
    host: str,
    event: Mapping[str, Any],
    *,
    vault: Vault | Path | str | None = None,
    event_name: str | None = None,
) -> dict[str, Any]:
    """Alias kept as a readable entry point for integrations and tests."""

    return handle_event(host, event, vault=vault, event_name=event_name)
