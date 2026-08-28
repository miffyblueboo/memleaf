"""Small, lock-held scope taxonomy and metadata maintenance helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import save_config
from .locking import atomic_write_json, atomic_write_text
from .models import Memory
from .scope_state import ScopeError, validate_scope_key, validate_scope_registry


class ScopeMaintenanceError(RuntimeError):
    """Scope metadata could not be validated or durably projected."""


@dataclass
class PreparedScopeChanges:
    config: dict[str, Any]
    migrations: dict[str, str]
    session_scopes: dict[str, list[str]]


def scope_registry_projection(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the model-safe registry view; paths and local filesystem data stay out."""

    try:
        registry = validate_scope_registry(config.get("scopes", {}))
    except (ScopeError, AttributeError, TypeError) as error:
        raise ScopeMaintenanceError("invalid scope registry") from error
    result: list[dict[str, Any]] = []
    for scope in sorted(registry):
        node = registry[scope]
        result.append(
            {
                "scope": scope,
                "aliases": list(node.get("aliases", [])),
                "parent": node.get("parent"),
                "children": list(node.get("children", [])),
            }
        )
    return result


def _scope_prefix(scope: str) -> str:
    return scope.split(":", 1)[0]


def _parent_allowed(child: str, parent: str) -> bool:
    if parent == "global":
        return True
    levels = {"domain": 1, "portfolio": 2, "project": 3}
    child_level = levels.get(_scope_prefix(child), -1)
    parent_level = levels.get(_scope_prefix(parent), -1)
    return parent_level >= 0 and child_level > parent_level


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _scope_value(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = list(value)
    else:
        raise ScopeMaintenanceError(f"invalid processed {field}")
    result: list[str] = []
    for item in values:
        try:
            scope = validate_scope_key(item)
        except ScopeError as error:
            raise ScopeMaintenanceError(f"invalid processed {field}") from error
        result.append(scope)
    result = _dedupe(result)
    if "unscoped" in result and len(result) != 1:
        raise ScopeMaintenanceError(f"invalid processed {field}")
    return result


def _resolve(scope: str, migrations: Mapping[str, str]) -> str:
    seen: set[str] = set()
    current = scope
    while current in migrations:
        if current in seen:
            raise ScopeMaintenanceError("scope migration contains a cycle")
        seen.add(current)
        current = migrations[current]
    return current


def _migrate_values(values: Iterable[str], migrations: Mapping[str, str]) -> list[str]:
    migrated = [_resolve(value, migrations) for value in values]
    migrated = _dedupe(migrated)
    if "unscoped" in migrated and len(migrated) != 1:
        raise ScopeMaintenanceError("scope migration produced invalid unscoped values")
    return migrated


def _aliases(node: Mapping[str, Any]) -> list[str]:
    values = node.get("aliases", [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ScopeMaintenanceError("scope aliases are invalid")
    return _dedupe(values)


def _remove_child(registry: dict[str, dict[str, Any]], parent: str, child: str) -> None:
    node = registry.get(parent)
    if not isinstance(node, dict):
        return
    children = node.get("children")
    if isinstance(children, list):
        node["children"] = [item for item in children if item != child]


def _attach_child(registry: dict[str, dict[str, Any]], parent: str, child: str) -> None:
    if parent == "global":
        return
    node = registry.setdefault(parent, {})
    children = node.setdefault("children", [])
    if child not in children:
        children.append(child)


def _set_parent(registry: dict[str, dict[str, Any]], child: str, parent: str | None) -> None:
    node = registry.setdefault(child, {})
    old_parent = node.get("parent")
    if isinstance(old_parent, str):
        _remove_child(registry, old_parent, child)
    if parent is None:
        node.pop("parent", None)
    else:
        node["parent"] = parent
        _attach_child(registry, parent, child)


class ScopeMaintainer:
    """Prepare and apply taxonomy changes without introducing history versions."""

    def __init__(self, service: Any):
        self.service = service

    def prepare(
        self,
        config: Mapping[str, Any],
        operations: Iterable[Mapping[str, Any]] = (),
        observed_scopes: Iterable[str] = (),
        session_scopes: Mapping[str, Iterable[str]] | None = None,
    ) -> PreparedScopeChanges:
        if not isinstance(config, Mapping):
            raise ScopeMaintenanceError("config is invalid")
        try:
            registry = validate_scope_registry(config.get("scopes", {}))
        except (ScopeError, TypeError) as error:
            raise ScopeMaintenanceError("invalid scope registry") from error
        original_scopes = set(registry)
        working = deepcopy(registry)

        observed: list[str] = []
        for raw_scope in observed_scopes:
            try:
                scope = validate_scope_key(raw_scope)
            except ScopeError as error:
                raise ScopeMaintenanceError("invalid observed scope") from error
            if scope not in ("global", "unscoped"):
                observed.append(scope)

        normalized_ops = [dict(item) for item in operations]
        merge_sources: set[str] = set()
        upsert_scopes: set[str] = set()
        operation_targets: set[str] = set()
        for item in normalized_ops:
            if not isinstance(item, Mapping):
                raise ScopeMaintenanceError("invalid scope operation")
            op = item.get("op")
            if op == "upsert":
                scope = item.get("scope")
                if not isinstance(scope, str):
                    raise ScopeMaintenanceError("invalid upsert scope")
                try:
                    scope = validate_scope_key(scope, allow_special=False)
                except ScopeError as error:
                    raise ScopeMaintenanceError("invalid upsert scope") from error
                parent = item.get("parent")
                if parent is not None:
                    if not isinstance(parent, str) or parent == "unscoped":
                        raise ScopeMaintenanceError("invalid upsert parent")
                    try:
                        parent = validate_scope_key(parent)
                    except ScopeError as error:
                        raise ScopeMaintenanceError("invalid upsert parent") from error
                    if not _parent_allowed(scope, parent):
                        raise ScopeMaintenanceError("upsert parent has an invalid level")
                item["scope"] = scope
                item["parent"] = parent
                aliases = item.get("aliases", [])
                if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                    raise ScopeMaintenanceError("invalid upsert aliases")
                item["aliases"] = _dedupe(aliases)
                previous = next(
                    (old for old in normalized_ops if old.get("op") == "upsert" and old.get("scope") == scope),
                    None,
                )
                if previous is not None and previous.get("parent") != parent:
                    raise ScopeMaintenanceError("scope operation moves one scope to conflicting parents")
                upsert_scopes.add(scope)
                operation_targets.add(scope)
            elif op == "merge":
                source = item.get("source")
                target = item.get("target")
                if not isinstance(source, str) or not isinstance(target, str):
                    raise ScopeMaintenanceError("invalid scope merge")
                try:
                    source = validate_scope_key(source, allow_special=False)
                    target = validate_scope_key(target, allow_special=False)
                except ScopeError as error:
                    raise ScopeMaintenanceError("invalid scope merge") from error
                if source in ("global", "unscoped") or target in ("global", "unscoped"):
                    raise ScopeMaintenanceError("special scopes cannot be merged")
                if _scope_prefix(source) != _scope_prefix(target) or source == target:
                    raise ScopeMaintenanceError("scope merge types or target are invalid")
                aliases = item.get("aliases", [])
                if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                    raise ScopeMaintenanceError("invalid merge aliases")
                item["source"] = source
                item["target"] = target
                item["aliases"] = _dedupe(aliases)
                already_applied = bool(item.get("already_applied"))
                if source not in original_scopes:
                    target_node = working.get(target)
                    target_aliases = _aliases(target_node) if isinstance(target_node, Mapping) else []
                    marker_matches = target in working and source.casefold() in {
                        alias.casefold() for alias in target_aliases
                    }
                    if marker_matches:
                        # Validation normally records this marker, but the
                        # maintainer also infers it for direct/retry callers.
                        already_applied = True
                        item["already_applied"] = True
                    if not already_applied or not marker_matches:
                        raise ScopeMaintenanceError("scope merge source is not registered")
                if source in merge_sources:
                    raise ScopeMaintenanceError("scope merge source is repeated")
                merge_sources.add(source)
                operation_targets.add(target)
                if _scope_prefix(source) != _scope_prefix(target):
                    raise ScopeMaintenanceError("scope merge types must match")
                if source == target:
                    raise ScopeMaintenanceError("scope cannot merge into itself")
            else:
                raise ScopeMaintenanceError("unknown scope operation")

        merge_targets = {
            item["source"]: item["target"]
            for item in normalized_ops
            if item.get("op") == "merge"
        }
        for source in merge_targets:
            seen: set[str] = set()
            current = source
            while current in merge_targets:
                if current in seen:
                    raise ScopeMaintenanceError("scope merge operations contain a cycle")
                seen.add(current)
                current = merge_targets[current]

        # Resolve completed merges before projecting observed scopes into the
        # working registry.  A retried summary can still observe the old
        # source scope, but an already-applied merge must not recreate that
        # source node after the successful config write removed it.
        completed_sources = {
            item["source"]
            for item in normalized_ops
            if item.get("op") == "merge" and item.get("already_applied")
        }
        for scope in observed:
            if scope in completed_sources:
                continue
            working.setdefault(scope, {})

        known_for_parent = original_scopes | set(working) | upsert_scopes | operation_targets
        for item in normalized_ops:
            if item.get("op") != "upsert":
                continue
            scope = item["scope"]
            parent = item.get("parent")
            if parent is not None:
                if parent != "global" and parent not in known_for_parent:
                    raise ScopeMaintenanceError("upsert parent is not registered or declared")
            # A merge and an explicit upsert of its source are ambiguous.
            if scope in merge_sources:
                raise ScopeMaintenanceError("scope cannot be upserted and merged in one batch")

        migrations: dict[str, str] = {}
        for item in normalized_ops:
            op = item["op"]
            if op == "upsert":
                scope = item["scope"]
                node = working.setdefault(scope, {})
                old_aliases = _aliases(node)
                aliases = item.get("aliases", [])
                if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                    raise ScopeMaintenanceError("invalid upsert aliases")
                merged_aliases = _dedupe(old_aliases + aliases)
                if merged_aliases:
                    node["aliases"] = merged_aliases
                elif "aliases" in node:
                    node["aliases"] = []
                _set_parent(working, scope, item.get("parent"))
                continue

            source = item["source"]
            target = item["target"]
            if item.get("already_applied"):
                target_node = working.get(target)
                if not isinstance(target_node, Mapping) or source.casefold() not in {
                    alias.casefold() for alias in _aliases(target_node)
                }:
                    raise ScopeMaintenanceError("already-applied scope merge marker is invalid")
                if source not in original_scopes:
                    # ``observed_scopes`` may still contain the old value from
                    # the retried summary.  Do not recreate a source node that
                    # the successful config write already removed.
                    working.pop(source, None)
                migrations[source] = target
                continue
            if source not in working:
                raise ScopeMaintenanceError("scope merge source is unavailable")
            if target in working and _scope_prefix(target) != _scope_prefix(source):
                raise ScopeMaintenanceError("scope merge types must match")
            source_node = dict(working[source])
            target_node = dict(working.get(target, {}))
            if target == source:
                raise ScopeMaintenanceError("scope cannot merge into itself")
            source_children = list(source_node.get("children", []))
            target_children = list(target_node.get("children", []))
            if target in source_children:
                raise ScopeMaintenanceError("scope merge would create a cycle")
            source_parent = source_node.get("parent")
            target_parent = target_node.get("parent")
            if target_parent == source:
                raise ScopeMaintenanceError("scope merge would create a cycle")
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                raise ScopeMaintenanceError("invalid merge aliases")
            target_node["aliases"] = _dedupe(
                _aliases(target_node)
                + _aliases(source_node)
                + [source, source.rsplit(":", 1)[1]]
                + aliases
            )
            if target_parent is None and isinstance(source_parent, str):
                target_parent = source_parent
            target_node["children"] = _dedupe(target_children + source_children)
            working.pop(source, None)
            for node in working.values():
                if node.get("parent") == source:
                    node["parent"] = target
                children = node.get("children")
                if isinstance(children, list):
                    node["children"] = [target if child == source else child for child in children]
                    node["children"] = _dedupe(node["children"])
            working[target] = target_node
            if isinstance(target_parent, str):
                _set_parent(working, target, target_parent)
            elif target_parent is None:
                _set_parent(working, target, None)
            for child in source_children:
                if child in working:
                    _set_parent(working, child, target)
            migrations[source] = target

        try:
            canonical = validate_scope_registry(working)
        except ScopeError as error:
            raise ScopeMaintenanceError("scope operations produce an invalid registry") from error

        def migrate_session_values(values: Iterable[str]) -> list[str]:
            return _migrate_values(values, migrations)

        normalized_sessions: dict[str, list[str]] = {}
        for state_key, values in (session_scopes or {}).items():
            if not isinstance(state_key, str):
                raise ScopeMaintenanceError("invalid session scope key")
            raw_values = _scope_value(values, "scopes")
            normalized_sessions[state_key] = migrate_session_values(raw_values)

        updated_config = deepcopy(dict(config))
        updated_config["scopes"] = canonical
        return PreparedScopeChanges(
            config=updated_config,
            migrations=migrations,
            session_scopes=normalized_sessions,
        )

    def _strict_memories(self, area: str) -> list[tuple[Path, Memory]]:
        base = self.service.vault.knowledge_path if area == "knowledge" else self.service.vault.history_path
        result: list[tuple[Path, Memory]] = []
        try:
            paths = sorted(base.rglob("*.md"))
        except OSError as error:
            raise ScopeMaintenanceError(f"cannot list {area} memories") from error
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise ScopeMaintenanceError(f"unsafe {area} memory path")
            try:
                memory = Memory.from_markdown(path.read_text(encoding="utf-8"), path)
            except (OSError, UnicodeError, ValueError) as error:
                raise ScopeMaintenanceError(f"invalid {area} memory") from error
            result.append((path, memory))
        return result

    def apply_unlocked(
        self,
        processed: Mapping[str, Any],
        prepared: PreparedScopeChanges,
        session_scope_updates: Mapping[str, Iterable[str]] | None = None,
    ) -> dict[str, list[str]]:
        if not isinstance(processed, Mapping):
            raise ScopeMaintenanceError("processed index is invalid")
        sessions = processed.get("sessions")
        if not isinstance(sessions, dict):
            raise ScopeMaintenanceError("processed sessions are invalid")

        writes: list[tuple[Path, Memory]] = []
        for area in ("knowledge", "history"):
            for path, memory in self._strict_memories(area):
                new_scopes = _migrate_values(memory.scopes, prepared.migrations)
                if new_scopes == memory.scopes:
                    continue
                value = memory.to_dict()
                value["scopes"] = new_scopes
                writes.append((path, Memory.from_mapping(value)))
        for path, _ in writes:
            if path.is_symlink():
                raise ScopeMaintenanceError("unsafe memory path")

        updated = deepcopy(dict(processed))
        updated_sessions = updated.get("sessions")
        if not isinstance(updated_sessions, dict):
            raise ScopeMaintenanceError("processed sessions are invalid")
        for state_key, state in updated_sessions.items():
            if not isinstance(state, dict):
                raise ScopeMaintenanceError("processed session state is invalid")
            for field in ("scopes", "scope_background", "scope"):
                if field in state:
                    state[field] = _migrate_values(_scope_value(state[field], field), prepared.migrations)
        for state_key, values in (session_scope_updates or {}).items():
            if state_key not in updated_sessions or not isinstance(updated_sessions[state_key], dict):
                raise ScopeMaintenanceError("processed session disappeared")
            updated_sessions[state_key]["scopes"] = _migrate_values(
                _scope_value(values, "scopes"), prepared.migrations
            )

        # Forward-recoverable order: source Markdown, processed session
        # metadata, derived indexes, then config last.
        for path, memory in writes:
            atomic_write_text(path, memory.to_markdown())
        atomic_write_json(self.service.vault.processed_index_path, updated)
        self.service._rebuild_index_unlocked()
        if prepared.config != self.service.vault.config():
            save_config(self.service.vault.config_path, prepared.config)
        return {
            key: list(value)
            for key, value in (session_scope_updates or {}).items()
        }


__all__ = ["PreparedScopeChanges", "ScopeMaintainer", "ScopeMaintenanceError", "scope_registry_projection"]
