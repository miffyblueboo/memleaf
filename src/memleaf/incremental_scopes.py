"""Bounded scope identity guards and append-only post-head registration.

Markdown remains authoritative. A new registry node is never published before
its associated head. Recovery merges into fresh config; journals hold only
scope keys/hashes, never credentials, paths or a whole config snapshot.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .config import save_config
from .models import Memory
from .scope_state import register_scope_nodes, validate_scope_key, validate_scope_registry

VERSION = 1
MAX_SCOPES = 256
_SPECIAL = {"global", "unscoped"}
_EMPTY = hashlib.sha256(b"{}").hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def registry_view(config: Mapping[str, Any]):
    registry = validate_scope_registry(config.get("scopes", {}))
    if len(registry) > MAX_SCOPES:
        raise ValueError("scope_catalog_limit")
    guard = {"version": VERSION, "nodes": {s: _digest(n) for s, n in registry.items()}}
    aliases = {s: list(n.get("aliases", [])) for s, n in registry.items() if n.get("aliases")}
    return registry, guard, aliases


def validate_guard(guard: Any) -> None:
    if (not isinstance(guard, dict) or set(guard) != {"version", "nodes"}
            or type(guard["version"]) is not int or guard["version"] != VERSION
            or not isinstance(guard["nodes"], dict) or len(guard["nodes"]) > MAX_SCOPES):
        raise ValueError("invalid_scope_guard")
    for key, value in guard["nodes"].items():
        validate_scope_key(key, allow_special=False)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("invalid_scope_guard")


def guard_matches(current: Any, original: Any, added=()) -> bool:
    """Only this work's applied, empty-node additions may extend a snapshot."""
    if original is None:
        # Merely upgrading an old empty scope view is not new semantic context.
        if current is None:
            return True
        validate_guard(current)
        return not current["nodes"]
    validate_guard(original)
    validate_guard(current)
    expected = dict(original["nodes"])
    for scope in added:
        if scope not in expected:
            expected[scope] = _EMPTY
    return current["nodes"] == expected


def applied_additions(work: Mapping[str, Any]) -> set[str]:
    return {s for op in work["operations"] if op["state"] in {"applied", "settled"}
            for s in op.get("scope_registration", {}).get("scopes", [])}


def resolve_scope(value: str, scopes: Mapping[str, str], aliases: Mapping[str, list[str]]) -> str | None:
    """Exact declared aliases/case variants only; never fuzzy project matching."""
    if value in scopes.values():
        return value
    if ":" not in value:
        return None
    prefix, name = value.split(":", 1)
    matches = {s for s in scopes.values() if s.startswith(prefix + ":") and
               (s.casefold() == value.casefold() or any(
                   a.casefold() in {name.casefold(), value.casefold()} for a in aliases.get(s, [])))}
    if len(matches) > 1:
        raise ValueError("ambiguous_scope_alias")
    return next(iter(matches)) if matches else None


def freeze_registration(op: dict[str, Any], state: Mapping[str, Any]) -> None:
    if op["action"] not in {"CREATE", "UPDATE"} or op["state"] != "prepared":
        return
    after = Memory.from_markdown(op["after"])
    guard = state.get("scope_guard")
    if guard is None:
        return  # Legacy frozen work retains its prior contract, not new authority.
    missing = sorted(s for s in after.scopes if s not in _SPECIAL and s not in guard["nodes"])
    if missing and after.validity == "valid":
        known = {s: s for s in guard["nodes"]}
        for scope in missing:
            canonical = resolve_scope(scope, known, state.get("scope_aliases", {}))
            if canonical is not None and canonical != scope:
                raise ValueError("scope_registration_identity_changed")
        op["scope_registration"] = {"version": VERSION, "scopes": missing, "state": "pending"}


def validate_registration(op: Mapping[str, Any], guard: Any) -> None:
    effect = op.get("scope_registration")
    if effect is None:
        return
    validate_guard(guard)
    if (not isinstance(effect, dict) or set(effect) != {"version", "scopes", "state"}
            or type(effect["version"]) is not int or effect["version"] != VERSION
            or not isinstance(effect["scopes"], list) or not 1 <= len(effect["scopes"]) <= MAX_SCOPES
            or any(not isinstance(s, str) for s in effect["scopes"])
            or len(set(effect["scopes"])) != len(effect["scopes"])
            or effect["state"] not in ("pending", "current", "cancelled")
            or op["action"] not in {"CREATE", "UPDATE"}):
        raise ValueError("invalid_scope_registration")
    for s in effect["scopes"]:
        validate_scope_key(s, allow_special=False)
        if s in guard["nodes"] and op["state"] in {"prepared", "applied"}:
            raise ValueError("invalid_scope_registration")
    if op["state"] in {"prepared", "applied"}:
        after = Memory.from_markdown(op["after"])
        if after.validity != "valid" or not set(effect["scopes"]) <= set(after.scopes):
            raise ValueError("invalid_scope_registration")
    if op["state"] == "settled" and effect["state"] == "pending":
        raise ValueError("invalid_scope_registration")


def finish_registration(service: Any, op: dict[str, Any]) -> None:
    """After a proven head write. A failure leaves the original op recoverable.

    Do not recreate a scope belonging only to a deleted/re-scoped/retracted
    head. Newly configured aliases are never overwritten to fit an old proposal.
    Uncooperative external editors are not covered by the Vault's file lock;
    compare bytes before replacement to detect ordinary edit races.
    """
    effect = op.get("scope_registration")
    if effect is None or effect["state"] != "pending":
        return
    try:
        matches = [r.memory for r in service._read_memories_unlocked("knowledge")
                   if r.memory.memory_id.casefold() == op["memory_id"].casefold()]
        if len(matches) > 1:
            raise ValueError("duplicate_memory_id")
        relevant = set(matches[0].scopes) if matches and matches[0].validity == "valid" else set()
        wanted = [s for s in effect["scopes"] if s in relevant]
        if wanted:
            path = service.vault.config_path
            if path.is_symlink() or not path.is_file():
                raise ValueError("unsafe_scope_config")
            raw = path.read_bytes()
            config = service.vault.config()
            registry, _, aliases = registry_view(config)
            for scope in wanted:
                resolved = resolve_scope(scope, {s: s for s in registry}, aliases)
                if resolved is not None and resolved != scope:
                    raise ValueError("scope_registration_identity_changed")
            updated = register_scope_nodes(config, wanted)
            registry_view(updated)
            if path.is_symlink() or path.read_bytes() != raw:
                raise ValueError("scope_config_changed_during_merge")
            if updated != config:
                save_config(path, updated)
        effect["state"] = "current"
        op.pop("scope_error", None)
    except (OSError, ValueError) as error:
        # The business head already exists. Never reclassify it as unwritten or
        # re-run semantic planning; resume the same operation after correction.
        code = str(error) if isinstance(error, ValueError) else "scope_config_io"
        op["scope_error"] = code if code in {
            "scope_registration_identity_changed", "scope_config_changed_during_merge",
            "duplicate_memory_id", "unsafe_scope_config", "scope_catalog_limit",
        } else "scope_config_unavailable"
        raise OSError("scope_registration_pending") from error
