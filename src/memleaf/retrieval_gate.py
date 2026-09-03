"""Small, host-scoped state for the v2 retrieval protocol.

The gate is deliberately separate from ``processed.json`` and the memory
store.  It records only the lifecycle of one host turn: whether a search was
observed, how many continuation attempts were requested, and the bounded
amount of memory text read by that turn.  It never stores a prompt, query,
tool result, or memory body.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .locking import atomic_write_json, read_json
from .vault import Vault


MAX_GATE_RETRIES = 2
# Deprecated compatibility symbols. They are audit-only and no longer enforce a per-turn cap.
MAX_READ_ITEMS = None
MAX_READ_CHARS = None
MAX_READ_PAGE_CHARS = 2000
GATE_TTL_SECONDS = 24 * 60 * 60
MAX_LEDGER_ENTRIES = 256

_LEDGER_VERSION = 1
_STATUSES = frozenset({"NOT_SEARCHED", "FOUND", "NO_MATCH", "ERROR", "DEGRADED"})
_SEARCH_STATUSES = frozenset({"found", "no_match", "error"})
_SAFE_ERROR_MESSAGES = {
    "retrieval_id_required": "retrieval turn id is required for memory read",
    "retrieval_id_invalid": "retrieval turn is unknown or expired",
    "retrieval_identity_invalid": "retrieval turn identity is invalid",
    "retrieval_turn_mismatch": "retrieval turn does not match the current host turn",
    "retrieval_status_invalid": "retrieval status is invalid",
    "retrieval_search_required": "a successful search is required before reading memory",
    "retrieval_call_id_invalid": "retrieval tool call id is invalid",
    "retrieval_read_budget_exceeded": "retrieval read budget exceeded",
    "retrieval_todo_pagination_mismatch": "todo pagination does not match the current retrieval chain",
    "retrieval_full_view_forbidden": "use directory search followed by bounded read",
    "retrieval_reader_invalid": "retrieval reader returned an invalid result",
    "retrieval_ledger_unavailable": "retrieval gate state is unavailable",
}


class RetrievalGateError(RuntimeError):
    """An error with a bounded, safe code/message pair for host adapters."""

    def __init__(self, code: str, message: str | None = None) -> None:
        safe_code = code if code in _SAFE_ERROR_MESSAGES else "retrieval_ledger_unavailable"
        self.code = safe_code
        self.message = message if message in _SAFE_ERROR_MESSAGES.values() else _SAFE_ERROR_MESSAGES[safe_code]
        super().__init__(self.message)


def _coerce_vault(value: Vault | Path | str) -> Vault:
    return value if isinstance(value, Vault) else Vault(value)


def _ledger_path(vault: Vault | Path | str) -> Path:
    root = _coerce_vault(vault)
    path = root.index_path / "retrieval_gate.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RetrievalGateError("retrieval_ledger_unavailable")
    return path


def _lock_path(vault: Vault | Path | str) -> Path:
    """Use a lock independent from the main vault lock.

    The MCP adapter can call the gate around a core read.  A distinct lock
    avoids taking the vault lock recursively while the reader accesses the
    Markdown store.
    """

    root = _coerce_vault(vault)
    path = root.index_path / "retrieval_gate.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RetrievalGateError("retrieval_ledger_unavailable")
    return path


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise RetrievalGateError("retrieval_identity_invalid")
    if len(value) > 256:
        raise RetrievalGateError("retrieval_identity_invalid")
    return value


def _retrieval_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("rtv-") or len(value) > 80:
        raise RetrievalGateError("retrieval_id_invalid")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in value):
        raise RetrievalGateError("retrieval_id_invalid")
    return value


def _empty_ledger() -> dict[str, Any]:
    return {"version": _LEDGER_VERSION, "entries": {}}


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_ledger()
    try:
        value = read_json(path)
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        raise RetrievalGateError("retrieval_ledger_unavailable") from error
    if not isinstance(value, Mapping) or value.get("version") != _LEDGER_VERSION:
        raise RetrievalGateError("retrieval_ledger_unavailable")
    entries = value.get("entries")
    if not isinstance(entries, Mapping):
        raise RetrievalGateError("retrieval_ledger_unavailable")
    return {"version": _LEDGER_VERSION, "entries": dict(entries)}


def _write_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    try:
        atomic_write_json(path, ledger, mode=0o600)
    except (OSError, TypeError, ValueError) as error:
        raise RetrievalGateError("retrieval_ledger_unavailable") from error


def _prune_entries(entries: dict[str, Any], now: float) -> None:
    expired = []
    for retrieval_id, entry in entries.items():
        if not isinstance(entry, Mapping):
            expired.append(retrieval_id)
            continue
        expires_at = entry.get("expires_at")
        if not isinstance(expires_at, (int, float)) or expires_at <= now:
            expired.append(retrieval_id)
    for retrieval_id in expired:
        entries.pop(retrieval_id, None)
    if len(entries) <= MAX_LEDGER_ENTRIES:
        return
    ordered = sorted(
        entries.items(),
        key=lambda item: float(item[1].get("created_at", 0)) if isinstance(item[1], Mapping) else 0,
    )
    for retrieval_id, _ in ordered[: len(entries) - MAX_LEDGER_ENTRIES]:
        entries.pop(retrieval_id, None)


def _with_lock(vault: Vault | Path | str):
    root = _coerce_vault(vault)
    root.ensure()
    from .locking import VaultLock

    return VaultLock(_lock_path(root))


def _entry_for(ledger: Mapping[str, Any], retrieval_id: str) -> dict[str, Any]:
    entries = ledger.get("entries")
    entry = entries.get(retrieval_id) if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping):
        raise RetrievalGateError("retrieval_id_invalid")
    normalized = dict(entry)
    if normalized.get("status") not in _STATUSES:
        raise RetrievalGateError("retrieval_ledger_unavailable")
    expires_at = normalized.get("expires_at")
    if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
        raise RetrievalGateError("retrieval_id_invalid")
    return normalized


def _public_state(retrieval_id: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    read_ids = entry.get("read_ids")
    read_count = len(read_ids) if isinstance(read_ids, list) else 0
    return {
        "retrieval_id": retrieval_id,
        "source": entry.get("source"),
        "session_id": entry.get("session_id"),
        "turn_id": entry.get("turn_id"),
        "status": entry.get("status"),
        "search_attempts": int(entry.get("search_attempts", 0) or 0),
        "gate_retries": int(entry.get("gate_retries", 0) or 0),
        "read_count": read_count,
        "read_chars": int(entry.get("read_chars", 0) or 0),
        "todo_list_pending": entry.get("todo_list_pending") is True,
        "todo_list_pages": int(entry.get("todo_list_pages", 0) or 0),
        "expires_at": entry.get("expires_at"),
    }


def begin_turn(
    vault: Vault | Path | str,
    source: str,
    session_id: str,
    turn_id: str,
) -> str:
    """Create or reuse one opaque state record for a host-visible turn."""

    source = _identity(source, "source")
    session_id = _identity(session_id, "session_id")
    turn_id = _identity(turn_id, "turn_id")
    now = time.time()
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entries = ledger["entries"]
        _prune_entries(entries, now)
        for retrieval_id, raw_entry in entries.items():
            if not isinstance(raw_entry, Mapping):
                continue
            if (
                raw_entry.get("source") == source
                and raw_entry.get("session_id") == session_id
                and raw_entry.get("turn_id") == turn_id
            ):
                _write_ledger(path, ledger)
                return _retrieval_id(retrieval_id)
        retrieval_id = f"rtv-{uuid.uuid4().hex}"
        entries[retrieval_id] = {
            "source": source,
            "session_id": session_id,
            "turn_id": turn_id,
            "created_at": now,
            "expires_at": now + GATE_TTL_SECONDS,
            "status": "NOT_SEARCHED",
            "search_attempts": 0,
            "gate_retries": 0,
            "read_ids": [],
            "read_chars": 0,
            "seen_call_hashes": [],
            "continuation_pending": False,
            "continuation_marker": "",
            "turn_aliases": [],
            "todo_list_pending": False,
            "todo_list_pages": 0,
            "todo_list_filter_hash": "",
            "todo_list_expected_cursor_hash": "",
        }
        _prune_entries(entries, now)
        _write_ledger(path, ledger)
        return retrieval_id


def find_turn(
    vault: Vault | Path | str,
    source: str,
    session_id: str,
    turn_id: str,
) -> str | None:
    """Find an existing turn without creating a replacement record."""

    source = _identity(source, "source")
    session_id = _identity(session_id, "session_id")
    turn_id = _identity(turn_id, "turn_id")
    now = time.time()
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entries = ledger["entries"]
        before = len(entries)
        _prune_entries(entries, now)
        if len(entries) != before:
            _write_ledger(path, ledger)
        for retrieval_id, entry in entries.items():
            aliases = entry.get("turn_aliases") if isinstance(entry, Mapping) else []
            if not isinstance(aliases, list):
                aliases = []
            if isinstance(entry, Mapping) and (
                entry.get("source") == source
                and entry.get("session_id") == session_id
                and (entry.get("turn_id") == turn_id or turn_id in aliases)
            ):
                try:
                    return _retrieval_id(retrieval_id)
                except RetrievalGateError:
                    continue
    return None


def bind_turn_alias(vault: Vault | Path | str, retrieval_id: str, turn_id: str) -> None:
    """Bind an official Stop continuation's new turn id to its old gate."""

    retrieval_id = _retrieval_id(retrieval_id)
    turn_id = _identity(turn_id, "turn_id")
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        aliases = entry.get("turn_aliases")
        if not isinstance(aliases, list):
            aliases = []
        aliases = [item for item in aliases if isinstance(item, str) and item != entry.get("turn_id")]
        if turn_id not in aliases and turn_id != entry.get("turn_id"):
            aliases.append(turn_id)
        entry["turn_aliases"] = aliases[-8:]
        ledger["entries"][retrieval_id] = entry
        _write_ledger(path, ledger)


def find_pending_continuation(
    vault: Vault | Path | str,
    source: str,
    session_id: str,
    prompt: Any,
) -> str | None:
    """Match a synthetic Stop continuation without storing its prompt text."""

    source = _identity(source, "source")
    session_id = _identity(session_id, "session_id")
    if not isinstance(prompt, str) or not prompt:
        return None
    now = time.time()
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entries = ledger["entries"]
        before = len(entries)
        _prune_entries(entries, now)
        if len(entries) != before:
            _write_ledger(path, ledger)
        for retrieval_id, raw_entry in entries.items():
            if not isinstance(raw_entry, Mapping):
                continue
            if raw_entry.get("source") != source or raw_entry.get("session_id") != session_id:
                continue
            if raw_entry.get("continuation_pending") is not True:
                continue
            marker = raw_entry.get("continuation_marker")
            if isinstance(marker, str) and marker and marker in prompt:
                try:
                    return _retrieval_id(retrieval_id)
                except RetrievalGateError:
                    continue
    return None


def validate_turn(vault: Vault | Path | str, retrieval_id: str) -> dict[str, Any]:
    """Validate an opaque id and return only safe counters and identity."""

    retrieval_id = _retrieval_id(retrieval_id)
    now = time.time()
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entries = ledger["entries"]
        before = len(entries)
        _prune_entries(entries, now)
        if len(entries) != before:
            _write_ledger(path, ledger)
        entry = _entry_for(ledger, retrieval_id)
        return _public_state(retrieval_id, entry)


def _is_current_entry(
    entries: Mapping[str, Any],
    retrieval_id: str,
    entry: Mapping[str, Any],
    source: str,
) -> bool:
    """Return whether an entry is the newest live turn for one host session."""

    if entry.get("source") != source:
        return False
    session_id = entry.get("session_id")
    if not isinstance(session_id, str):
        return False
    newest_id: str | None = None
    newest_created = float("-inf")
    for candidate_id, raw_candidate in entries.items():
        if not isinstance(raw_candidate, Mapping):
            continue
        if raw_candidate.get("source") != source or raw_candidate.get("session_id") != session_id:
            continue
        expires_at = raw_candidate.get("expires_at")
        created_at = raw_candidate.get("created_at")
        if (
            not isinstance(expires_at, (int, float))
            or expires_at <= time.time()
            or not isinstance(created_at, (int, float))
        ):
            continue
        if float(created_at) >= newest_created:
            newest_id = candidate_id
            newest_created = float(created_at)
    return newest_id == retrieval_id


def validate_current_turn(
    vault: Vault | Path | str,
    retrieval_id: str,
    source: str,
) -> dict[str, Any]:
    """Validate a token belongs to the newest live turn for its host session.

    MCP uses this for every host token before search/read.  Ordinary
    ``validate_turn`` remains available for Stop continuation and historical
    inspection where "current turn" is not the question.
    """

    retrieval_id = _retrieval_id(retrieval_id)
    source = _identity(source, "source")
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entries = ledger["entries"]
        before = len(entries)
        _prune_entries(entries, time.time())
        if len(entries) != before:
            _write_ledger(path, ledger)
        entry = _entry_for(ledger, retrieval_id)
        if not _is_current_entry(entries, retrieval_id, entry, source):
            raise RetrievalGateError("retrieval_turn_mismatch")
        return _public_state(retrieval_id, entry)


def observe_search(
    vault: Vault | Path | str,
    retrieval_id: str,
    status: str,
    call_id: str,
    *,
    current_source: str | None = None,
) -> None:
    """Record one real search result, de-duplicating the host tool call."""

    retrieval_id = _retrieval_id(retrieval_id)
    if status not in _SEARCH_STATUSES:
        raise RetrievalGateError("retrieval_status_invalid")
    _identity(call_id, "call_id")
    if current_source is not None:
        current_source = _identity(current_source, "source")
    call_hash = hashlib.sha256(call_id.encode("utf-8")).hexdigest()
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        if current_source is not None and not _is_current_entry(
            ledger["entries"], retrieval_id, entry, current_source
        ):
            raise RetrievalGateError("retrieval_turn_mismatch")
        seen = entry.get("seen_call_hashes")
        if not isinstance(seen, list):
            seen = []
        if call_hash in seen:
            return
        seen = [item for item in seen if isinstance(item, str)][-255:]
        seen.append(call_hash)
        entry["seen_call_hashes"] = seen
        entry["search_attempts"] = int(entry.get("search_attempts", 0) or 0) + 1
        entry["status"] = {"found": "FOUND", "no_match": "NO_MATCH", "error": "ERROR"}[status]
        entry["continuation_pending"] = False
        ledger["entries"][retrieval_id] = entry
        _write_ledger(path, ledger)



def todo_filter_key(arguments: Mapping[str, Any]) -> str:
    """Return a stable, non-secret description of list_todos filters for chain validation."""

    payload = {
        "status": arguments.get("status", "active"),
        "scope": arguments.get("scope"),
        "due_from": arguments.get("due_from"),
        "due_to": arguments.get("due_to"),
        "include_overdue": arguments.get("include_overdue", True),
        "include_unscheduled": arguments.get("include_unscheduled", True),
    }
    import json

    return json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _optional_hash(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        raise RetrievalGateError("retrieval_todo_pagination_mismatch")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def observe_todo_list(
    vault: Vault | Path | str,
    retrieval_id: str,
    status: str,
    call_id: str,
    *,
    filter_key: str,
    cursor: str | None,
    has_more: bool,
    next_cursor: str | None,
    current_source: str | None = None,
) -> None:
    """Record one real list_todos page and enforce a single current-turn cursor chain."""

    retrieval_id = _retrieval_id(retrieval_id)
    if status not in _SEARCH_STATUSES or type(has_more) is not bool or not isinstance(filter_key, str):
        raise RetrievalGateError("retrieval_status_invalid")
    _identity(call_id, "call_id")
    if current_source is not None:
        current_source = _identity(current_source, "source")
    call_hash = hashlib.sha256(call_id.encode("utf-8")).hexdigest()
    filter_hash = hashlib.sha256(filter_key.encode("utf-8")).hexdigest()
    cursor_hash = _optional_hash(cursor)
    next_hash = _optional_hash(next_cursor)
    if status == "no_match" and has_more:
        raise RetrievalGateError("retrieval_status_invalid")
    if status == "found" and has_more and not next_hash:
        raise RetrievalGateError("retrieval_status_invalid")
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        if current_source is not None and not _is_current_entry(ledger["entries"], retrieval_id, entry, current_source):
            raise RetrievalGateError("retrieval_turn_mismatch")
        seen = entry.get("seen_call_hashes")
        if not isinstance(seen, list):
            seen = []
        if call_hash in seen:
            return
        pending = entry.get("todo_list_pending") is True
        previous_filter = entry.get("todo_list_filter_hash") if isinstance(entry.get("todo_list_filter_hash"), str) else ""
        expected_cursor = entry.get("todo_list_expected_cursor_hash") if isinstance(entry.get("todo_list_expected_cursor_hash"), str) else ""
        if status != "error":
            if cursor_hash:
                if not pending or previous_filter != filter_hash or expected_cursor != cursor_hash:
                    raise RetrievalGateError("retrieval_todo_pagination_mismatch")
            else:
                if pending and previous_filter and previous_filter != filter_hash:
                    raise RetrievalGateError("retrieval_todo_pagination_mismatch")
                entry["todo_list_pages"] = 0
            entry["todo_list_filter_hash"] = filter_hash
            entry["todo_list_pages"] = int(entry.get("todo_list_pages", 0) or 0) + 1
            entry["todo_list_pending"] = bool(has_more)
            entry["todo_list_expected_cursor_hash"] = next_hash if has_more else ""
        entry["status"] = {"found": "FOUND", "no_match": "NO_MATCH", "error": "ERROR"}[status]
        seen = [item for item in seen if isinstance(item, str)][-255:]
        seen.append(call_hash)
        entry["seen_call_hashes"] = seen
        entry["search_attempts"] = int(entry.get("search_attempts", 0) or 0) + 1
        entry["continuation_pending"] = False
        ledger["entries"][retrieval_id] = entry
        _write_ledger(path, ledger)


def request_gate_retry(vault: Vault | Path | str, retrieval_id: str) -> int:
    """Record one Stop continuation request and return its 1-based count."""

    retrieval_id = _retrieval_id(retrieval_id)
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        retries = int(entry.get("gate_retries", 0) or 0)
        if retries < MAX_GATE_RETRIES:
            retries += 1
            entry["gate_retries"] = retries
            entry["continuation_pending"] = True
            entry["continuation_marker"] = (
                "ml-gate-"
                + hashlib.sha256(f"{retrieval_id}:{retries}".encode("utf-8")).hexdigest()[:16]
            )
            ledger["entries"][retrieval_id] = entry
            _write_ledger(path, ledger)
        return retries


def continuation_marker(vault: Vault | Path | str, retrieval_id: str) -> str | None:
    """Return the current opaque marker used only to correlate a continuation."""

    retrieval_id = _retrieval_id(retrieval_id)
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        marker = entry.get("continuation_marker")
        return marker if isinstance(marker, str) and marker else None


def consume_continuation(vault: Vault | Path | str, retrieval_id: str) -> bool:
    """Consume a pending synthetic continuation without creating a user turn."""

    retrieval_id = _retrieval_id(retrieval_id)
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        pending = entry.get("continuation_pending") is True
        if pending:
            entry["continuation_pending"] = False
            ledger["entries"][retrieval_id] = entry
            _write_ledger(path, ledger)
        return pending


def mark_degraded(vault: Vault | Path | str, retrieval_id: str) -> dict[str, Any]:
    """Allow a final answer after bounded retrieval-gate failure."""

    retrieval_id = _retrieval_id(retrieval_id)
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        entry["status"] = "DEGRADED"
        entry["continuation_pending"] = False
        ledger["entries"][retrieval_id] = entry
        _write_ledger(path, ledger)
        return _public_state(retrieval_id, entry)


def guarded_read(
    vault: Vault | Path | str,
    retrieval_id: str,
    memory_id: str,
    reader: Callable[[int], Mapping[str, Any] | None],
    *,
    current_source: str | None = None,
) -> Mapping[str, Any] | None:
    """Read one bounded page while keeping per-turn read counters for audit only."""

    retrieval_id = _retrieval_id(retrieval_id)
    memory_id = _identity(memory_id, "memory_id")
    if not callable(reader):
        raise RetrievalGateError("retrieval_reader_invalid")
    root = _coerce_vault(vault)
    path = _ledger_path(root)
    with _with_lock(root):
        ledger = _read_ledger(path)
        entry = _entry_for(ledger, retrieval_id)
        if current_source is not None:
            source = _identity(current_source, "source")
            if not _is_current_entry(ledger["entries"], retrieval_id, entry, source):
                raise RetrievalGateError("retrieval_turn_mismatch")
        if entry.get("status") != "FOUND":
            raise RetrievalGateError("retrieval_search_required")
        read_ids = entry.get("read_ids")
        if not isinstance(read_ids, list):
            read_ids = []
        read_ids = [item for item in read_ids if isinstance(item, str)]
        read_chars = int(entry.get("read_chars", 0) or 0)
        result = reader(MAX_READ_PAGE_CHARS)
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise RetrievalGateError("retrieval_reader_invalid")
        body = result.get("body")
        if not isinstance(body, str) or len(body) > MAX_READ_PAGE_CHARS:
            raise RetrievalGateError("retrieval_reader_invalid")
        if body:
            if memory_id not in read_ids:
                read_ids.append(memory_id)
            entry["read_ids"] = read_ids
            entry["read_chars"] = read_chars + len(body)
        ledger["entries"][retrieval_id] = entry
        _write_ledger(path, ledger)
        return dict(result)


__all__ = [
    "GATE_TTL_SECONDS",
    "MAX_GATE_RETRIES",
    "MAX_LEDGER_ENTRIES",
    "MAX_READ_CHARS",
    "MAX_READ_ITEMS",
    "MAX_READ_PAGE_CHARS",
    "RetrievalGateError",
    "begin_turn",
    "bind_turn_alias",
    "consume_continuation",
    "continuation_marker",
    "find_pending_continuation",
    "find_turn",
    "guarded_read",
    "mark_degraded",
    "observe_search",
    "observe_todo_list",
    "todo_filter_key",
    "request_gate_retry",
    "validate_current_turn",
    "validate_turn",
]
