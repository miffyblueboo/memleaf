"""Host-native source registration helpers.

This module owns only configuration of read-only native sources.  It never
creates, writes, renames, or removes a host's native memory files.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .config import save_config
from .native_index import NativeIndexer, validate_native_sources


_HERMES_SOURCES = (
    ("hermes_memory", "hermes_memory_builtin", "MEMORY.md"),
    ("hermes_user", "hermes_user_builtin", "USER.md"),
)


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False


def _desired(path: Path) -> dict[str, Any]:
    return {
        "agent": "hermes",
        "path": str(path.expanduser().resolve(strict=False)),
        "share": False,
        "format": "markdown",
    }


def _safe_existing_for_hermes(source: Mapping[str, Any], path: Path) -> bool:
    return (
        source.get("agent") == "hermes"
        and source.get("share") is False
        and source.get("format", "markdown") == "markdown"
        and _same_path(str(source.get("path", "")), path)
    )


def ensure_hermes_native_sources(vault: Any, hermes_home: Path | str) -> dict[str, Any]:
    """Idempotently register Hermes MEMORY.md and USER.md as private sources.

    Existing user-defined native sources are preserved.  A semantic conflict
    fails closed instead of silently changing the user's source or exposing a
    private Hermes file to another agent.
    """

    home = Path(hermes_home).expanduser().resolve(strict=False)
    targets = [(canonical, fallback, home / "memories" / filename) for canonical, fallback, filename in _HERMES_SOURCES]

    with vault.lock():
        config = vault.config()
        existing_value = config.get("native_sources", {})
        if not isinstance(existing_value, Mapping):
            raise RuntimeError("invalid native_sources configuration")
        sources: dict[str, dict[str, Any]] = {
            str(source_id): deepcopy(dict(source))
            for source_id, source in existing_value.items()
            if isinstance(source_id, str) and isinstance(source, Mapping)
        }
        # Validate before touching anything.  This also catches duplicate paths
        # already present in a manually edited configuration.
        validate_native_sources(sources, base_dir=vault.root)
        changed = False
        registered: dict[str, str] = {}

        for canonical, fallback, path in targets:
            desired = _desired(path)
            path_owner = next(
                (
                    source_id
                    for source_id, source in sources.items()
                    if _same_path(str(source.get("path", "")), path)
                ),
                None,
            )
            if path_owner is not None:
                current = sources[path_owner]
                if not _safe_existing_for_hermes(current, path):
                    raise RuntimeError(f"unsafe Hermes native source conflict: {path_owner}")
                registered[canonical] = path_owner
                continue

            target_id = canonical
            if target_id in sources:
                if _safe_existing_for_hermes(sources[target_id], path):
                    registered[canonical] = target_id
                    continue
                target_id = fallback
                if target_id in sources and not _safe_existing_for_hermes(sources[target_id], path):
                    raise RuntimeError(f"Hermes native source id conflict: {canonical}")

            if target_id not in sources:
                sources[target_id] = desired
                changed = True
            registered[canonical] = target_id

        if changed:
            updated = deepcopy(dict(config))
            updated["native_sources"] = sources
            save_config(vault.config_path, updated)

        # Refresh the derived index under the same vault lock. Missing native
        # files are a valid state and are represented as unavailable entries;
        # this function never creates them.
        index_summary = NativeIndexer(vault).refresh_unlocked()

    return {
        "changed": changed,
        "sources": registered,
        "index": index_summary,
    }


__all__ = ["ensure_hermes_native_sources"]
