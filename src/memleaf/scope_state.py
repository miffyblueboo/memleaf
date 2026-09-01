"""Small, deterministic helpers for session scopes and the local scope registry."""

from __future__ import annotations

from copy import deepcopy
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


class ScopeError(ValueError):
    """A scope or scope registry value is outside the local contract."""


_SCOPE_KEY = re.compile(r"^(domain|portfolio|project):([^\s/:\\\x00\r\n]+)$")
_ASCII_TERM = re.compile(r"[a-z0-9]+(?:[ ._-][a-z0-9]+)*")
_NODE_FIELDS = frozenset(("aliases", "paths", "parent", "children"))


def validate_scope_key(value: Any, *, allow_special: bool = True) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ScopeError("invalid scope key")
    if allow_special and value in ("global", "unscoped"):
        return value
    match = _SCOPE_KEY.fullmatch(value)
    if match is None or match.group(2) in (".", ".."):
        raise ScopeError("invalid scope key")
    return value


def normalize_scopes(value: Any, *, field: str = "scopes") -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ScopeError(f"invalid {field}")
    result: list[str] = []
    for item in values:
        try:
            scope = validate_scope_key(item)
        except ScopeError as error:
            raise ScopeError(f"invalid {field}") from error
        if scope not in result:
            result.append(scope)
    if not result:
        raise ScopeError(f"{field} cannot be empty")
    if "unscoped" in result and len(result) != 1:
        raise ScopeError("unscoped must be the only scope")
    return result


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ScopeError(f"scope {field} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or "\x00" in item or "\n" in item or "\r" in item:
            raise ScopeError(f"invalid scope {field}")
        if item not in result:
            result.append(item)
    return result


def _validate_node(scope: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ScopeError("scope node must be a mapping")
    if scope in ("global", "unscoped"):
        raise ScopeError("special scope cannot be a registry node")
    node = dict(raw)
    aliases = _string_list(node["aliases"], "aliases") if "aliases" in node else []
    paths = _string_list(node["paths"], "paths") if "paths" in node else []
    parent = node.get("parent")
    if parent is not None:
        try:
            validate_scope_key(parent)
        except ScopeError as error:
            raise ScopeError("invalid scope parent") from error
        if parent == scope:
            raise ScopeError("scope parent cannot point to itself")
    children = node.get("children", [])
    if isinstance(children, str) or not isinstance(children, list):
        raise ScopeError("scope children must be a list")
    normalized_children: list[str] = []
    for child in children:
        try:
            child_value = validate_scope_key(child, allow_special=False)
        except ScopeError as error:
            raise ScopeError("invalid scope child") from error
        if child_value == scope:
            raise ScopeError("scope children cannot point to itself")
        if child_value not in normalized_children:
            normalized_children.append(child_value)
    if "aliases" in node:
        node["aliases"] = aliases
    if "paths" in node:
        node["paths"] = paths
    if "parent" in node:
        node["parent"] = parent
    if "children" in node:
        node["children"] = normalized_children
    return node


def _scope_level(scope: str) -> int:
    if scope == "global":
        return 0
    prefix = scope.split(":", 1)[0]
    return {"domain": 1, "portfolio": 2, "project": 3}.get(prefix, -1)


def _parent_allowed(child: str, parent: str) -> bool:
    if parent == "global":
        return True
    child_level = _scope_level(child)
    parent_level = _scope_level(parent)
    if child_level < 0 or parent_level < 0:
        return False
    # Domain is rooted at global; portfolio may be rooted at domain/global;
    # projects may be rooted at portfolio/domain/global.
    return parent_level < child_level and not (child_level == 1 and parent_level != 0)


def validate_scope_registry(value: Any) -> dict[str, dict[str, Any]]:
    """Validate and canonicalize a registry into child -> parent edges.

    ``parent`` is authoritative when both sides agree.  ``children`` is
    accepted as the legacy/readable form and is rebuilt from the resulting
    child-to-parent map, so declaring both sides consistently is not treated
    as a cycle.
    """

    if not isinstance(value, Mapping):
        raise ScopeError("scopes registry must be a mapping")
    raw_registry: dict[str, dict[str, Any]] = {}
    for raw_scope, raw_node in value.items():
        try:
            scope = validate_scope_key(raw_scope, allow_special=False)
        except ScopeError as error:
            raise ScopeError("invalid scope registry key") from error
        raw_registry[scope] = _validate_node(scope, raw_node)

    parent_of: dict[str, str] = {}
    for scope, node in raw_registry.items():
        parent = node.get("parent")
        if parent is not None:
            if not isinstance(parent, str) or parent == "unscoped":
                raise ScopeError("invalid scope parent")
            try:
                validate_scope_key(parent)
            except ScopeError as error:
                raise ScopeError("invalid scope parent") from error
            if not _parent_allowed(scope, parent):
                raise ScopeError("scope parent has an invalid level")
            parent_of[scope] = parent

    declared_children: dict[str, list[str]] = {}
    for parent, node in raw_registry.items():
        children = node.get("children", [])
        declared_children[parent] = list(children)
        for child in children:
            if child == "unscoped" or not _parent_allowed(child, parent):
                raise ScopeError("scope child has an invalid level")
            previous = parent_of.get(child)
            if previous is not None and previous != parent:
                raise ScopeError("scope parent and children conflict")
            parent_of[child] = parent

    # Follow only child -> parent edges.  The reverse children projection is
    # derived below and therefore cannot turn a consistent declaration into a
    # false cycle.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(scope: str) -> None:
        if scope in visiting:
            raise ScopeError("scope registry contains a cycle")
        if scope in visited:
            return
        visiting.add(scope)
        parent = parent_of.get(scope)
        if parent is not None:
            visit(parent)
        visiting.remove(scope)
        visited.add(scope)

    for scope in set(raw_registry) | set(parent_of):
        visit(scope)

    children_by_parent: dict[str, list[str]] = {}
    for child, parent in parent_of.items():
        children_by_parent.setdefault(parent, []).append(child)

    registry: dict[str, dict[str, Any]] = {}
    for scope, raw_node in raw_registry.items():
        node = dict(raw_node)
        if scope in parent_of:
            node["parent"] = parent_of[scope]
        elif "parent" in node:
            node.pop("parent", None)

        projected_children = children_by_parent.get(scope, [])
        if "children" in raw_node or projected_children:
            original = list(raw_node.get("children", []))
            node["children"] = original + [
                child for child in sorted(projected_children) if child not in original
            ]
        else:
            node.pop("children", None)
        registry[scope] = node
    return registry


def register_scope_nodes(config: Mapping[str, Any], observed_scopes: Iterable[str]) -> dict[str, Any]:
    """Return config with only missing observed nodes added as empty mappings."""

    if not isinstance(config, Mapping):
        raise ScopeError("config must be a mapping")
    current = validate_scope_registry(config.get("scopes", {}))
    additions: list[str] = []
    for raw_scope in observed_scopes:
        scope = validate_scope_key(raw_scope)
        if scope in ("global", "unscoped") or scope in current or scope in additions:
            continue
        additions.append(scope)
    if not additions:
        return deepcopy(dict(config))
    updated = deepcopy(dict(config))
    registry = deepcopy(current)
    for scope in additions:
        # Do not manufacture parent/children relationships.  A later explicit
        # registry edit or a future model-backed scope operation may do that.
        registry[scope] = {}
    validate_scope_registry(registry)
    updated["scopes"] = registry
    return updated


def _query_text(query: Any) -> str:
    if isinstance(query, str):
        return " ".join(query.casefold().strip().split())
    if isinstance(query, Iterable):
        return " ".join(str(item) for item in query).casefold().strip()
    return ""


def _term_matches(query: str, term: str) -> bool:
    normalized = " ".join(term.casefold().strip().split())
    if not normalized:
        return False
    if _ASCII_TERM.fullmatch(normalized):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(normalized) + r"(?![a-z0-9])", query))
    return normalized in query


def resolve_query_project_scope(query: Any, config: Mapping[str, Any]) -> str | None:
    """Return one unambiguous project scope named or aliased by ``query``."""

    matches = project_scope_matches_text(query, config)
    return matches[0] if len(matches) == 1 else None


def project_scope_matches_text(query: Any, config: Mapping[str, Any]) -> list[str]:
    """Return registered project scopes named or aliased by text.

    Matching uses the same token/substring rules as query scope resolution,
    but retains every match so callers can reject ambiguous model attribution.
    """

    query_value = _query_text(query)
    if not query_value:
        return []
    registry = validate_scope_registry(config.get("scopes", {}) if isinstance(config, Mapping) else {})
    matches: set[str] = set()
    for scope, node in registry.items():
        if not scope.startswith("project:"):
            continue
        name = scope.split(":", 1)[1]
        terms = [name, scope]
        aliases = node.get("aliases", [])
        if isinstance(aliases, list):
            terms.extend(item for item in aliases if isinstance(item, str))
        if any(_term_matches(query_value, term) for term in terms):
            matches.add(scope)
    return sorted(matches, key=str.casefold)


def _resolved_path(value: str | Path, *, base_dir: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve(strict=False)


def resolve_project_path_scope(
    project_path: str | Path | None,
    config: Mapping[str, Any],
    *,
    base_dir: Path | None = None,
) -> str | None:
    """Match only configured project paths, preferring the longest boundary."""

    if project_path is None:
        return None
    try:
        actual = _resolved_path(project_path, base_dir=base_dir)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    registry = validate_scope_registry(config.get("scopes", {}) if isinstance(config, Mapping) else {})
    matches: list[tuple[int, str]] = []
    for scope, node in registry.items():
        if not scope.startswith("project:"):
            continue
        paths = node.get("paths", [])
        if not isinstance(paths, list):
            continue
        for raw_path in paths:
            try:
                configured = _resolved_path(raw_path, base_dir=base_dir)
                actual.relative_to(configured)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            matches.append((len(configured.parts), scope))
    if not matches:
        return None
    longest = max(length for length, _ in matches)
    scopes = sorted({scope for length, scope in matches if length == longest})
    return scopes[0] if len(scopes) == 1 else None


__all__ = [
    "ScopeError",
    "normalize_scopes",
    "project_scope_matches_text",
    "register_scope_nodes",
    "resolve_project_path_scope",
    "resolve_query_project_scope",
    "validate_scope_key",
    "validate_scope_registry",
]
