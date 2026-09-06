"""Runtime state layout and crash-safe migration from pre-0.2.28 vaults."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .locking import atomic_unlink, atomic_write_bytes, atomic_write_json, read_json


STATE_LAYOUT_VERSION = 1
_STATE_FILES = (
    "processed.json",
    "agents.json",
    "host_ingest.json",
    "retrieval_gate.json",
    "compaction.json",
)
_LEGACY_LOCK_FILES = ("vault.lock", "retrieval_gate.lock")
_STAGING_DIR = ".compaction-staging"


class StateLayoutError(RuntimeError):
    """The vault runtime-state layout cannot be migrated without data loss."""


def _json_value(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise StateLayoutError(f"unsafe {label} path")
    try:
        value = read_json(path)
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        raise StateLayoutError(f"invalid {label}") from error
    if not isinstance(value, dict):
        raise StateLayoutError(f"invalid {label}")
    if path.name == "processed.json":
        if value.get("version") != 1:
            raise StateLayoutError("invalid processed state version")
        for field, expected in (("event_keys", list), ("events", dict), ("sessions", dict)):
            if field in value and not isinstance(value[field], expected):
                raise StateLayoutError(f"invalid processed state {field}")
    elif path.name == "agents.json":
        if value.get("version") != 1 or not isinstance(value.get("agents", {}), dict):
            raise StateLayoutError("invalid agents state")
    elif path.name == "retrieval_gate.json":
        if value.get("version") != 1 or not isinstance(value.get("entries", {}), dict):
            raise StateLayoutError("invalid retrieval gate state")
    elif path.name == "host_ingest.json":
        if "hosts" in value and not isinstance(value.get("hosts"), dict):
            raise StateLayoutError("invalid host ingest state")
    elif path.name == "compaction.json":
        if value.get("version") != 1:
            raise StateLayoutError("invalid compaction journal state")
    return value


def _json_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _copy_json(old: Path, new: Path, label: str) -> None:
    old_value = _json_value(old, label)
    if new.exists() or new.is_symlink():
        new_value = _json_value(new, label)
        if _json_digest(new_value) != _json_digest(old_value):
            raise StateLayoutError(f"conflicting legacy and current {label}")
        return
    atomic_write_json(new, old_value, mode=0o600)
    copied = _json_value(new, label)
    if _json_digest(copied) != _json_digest(old_value):
        raise StateLayoutError(f"failed to verify migrated {label}")


def _file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise StateLayoutError("unsafe compaction staging file")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise StateLayoutError("cannot read compaction staging file") from error


def _tree_manifest(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise StateLayoutError("unsafe compaction staging root")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise StateLayoutError("unsafe compaction staging path")
        if path.is_dir():
            continue
        if not path.is_file():
            raise StateLayoutError("unsafe compaction staging entry")
        result[path.relative_to(root).as_posix()] = _file_digest(path)
    return result


def _copy_staging(old: Path, new: Path) -> None:
    old_manifest = _tree_manifest(old)
    if new.exists() or new.is_symlink():
        if _tree_manifest(new) != old_manifest:
            raise StateLayoutError("conflicting legacy and current compaction staging")
        return
    new.mkdir(parents=True, exist_ok=False)
    try:
        new.chmod(0o700)
    except OSError:
        pass
    for relative, expected in old_manifest.items():
        source = old / relative
        destination = new / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = source.read_bytes()
        except OSError as error:
            raise StateLayoutError("cannot read compaction staging file") from error
        atomic_write_bytes(destination, data, mode=0o600)
        if _file_digest(destination) != expected:
            raise StateLayoutError("failed to verify compaction staging file")
    if _tree_manifest(new) != old_manifest:
        raise StateLayoutError("failed to verify migrated compaction staging")


def _remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        raise StateLayoutError("unsafe legacy state directory")
    for child in list(path.iterdir()):
        if child.is_symlink():
            raise StateLayoutError("unsafe legacy state path")
        if child.is_dir():
            _remove_tree(child)
        else:
            atomic_unlink(child)
    try:
        path.rmdir()
    except OSError as error:
        raise StateLayoutError("cannot remove migrated legacy state directory") from error


def _read_layout(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    value = _json_value(path, "state layout marker")
    if value.get("version") != STATE_LAYOUT_VERSION or value.get("complete") is not True:
        raise StateLayoutError("invalid state layout marker")
    migrated = value.get("migrated")
    if not isinstance(migrated, list) or not all(isinstance(item, str) for item in migrated):
        raise StateLayoutError("invalid state layout marker")
    return value


def _cleanup_legacy(index_root: Path) -> None:
    for name in _STATE_FILES:
        path = index_root / name
        if path.is_symlink():
            raise StateLayoutError(f"unsafe legacy {name} path")
        if path.exists():
            atomic_unlink(path)
    staging = index_root / _STAGING_DIR
    if staging.exists() or staging.is_symlink():
        _remove_tree(staging)
    for name in _LEGACY_LOCK_FILES:
        path = index_root / name
        if path.is_symlink():
            raise StateLayoutError(f"unsafe legacy {name} path")
        if path.exists():
            try:
                atomic_unlink(path)
            except OSError as error:
                raise StateLayoutError(
                    "legacy memleaf process still owns the old vault lock; stop old processes and retry"
                ) from error


def migrate_state_layout(vault: Any) -> dict[str, Any]:
    """Migrate runtime correctness state out of ``_index`` exactly once.

    The caller must hold the current Vault lock. Before the completion marker
    exists, old/new coexistence is accepted only when values are equivalent.
    After the marker is durable, ``_state`` is authoritative and old copies are
    cleanup debris; they are never merged or replayed.
    """

    state_root = Path(vault.state_path)
    index_root = Path(vault.index_path)
    state_root.mkdir(parents=True, exist_ok=True)
    marker = Path(vault.state_layout_path)
    completed = _read_layout(marker)
    if completed is not None:
        for name in completed["migrated"]:
            if name == _STAGING_DIR:
                path = state_root / name
                if not path.exists() or path.is_symlink():
                    raise StateLayoutError("migrated compaction staging is missing")
                _tree_manifest(path)
            elif name in _STATE_FILES:
                path = state_root / name
                if not path.exists() or path.is_symlink():
                    raise StateLayoutError(f"migrated {name} is missing")
                _json_value(path, f"migrated {name}")
            else:
                raise StateLayoutError("invalid state layout marker")
        _cleanup_legacy(index_root)
        return completed

    migrated: list[str] = []
    for name in _STATE_FILES:
        old = index_root / name
        new = state_root / name
        if old.is_symlink() or new.is_symlink():
            raise StateLayoutError(f"unsafe {name} state path")
        if old.exists():
            _copy_json(old, new, name)
            migrated.append(name)
        elif new.exists():
            _json_value(new, name)

    old_staging = index_root / _STAGING_DIR
    new_staging = state_root / _STAGING_DIR
    if old_staging.is_symlink() or new_staging.is_symlink():
        raise StateLayoutError("unsafe compaction staging path")
    if old_staging.exists():
        _copy_staging(old_staging, new_staging)
        migrated.append(_STAGING_DIR)
    elif new_staging.exists():
        _tree_manifest(new_staging)

    layout = {
        "version": STATE_LAYOUT_VERSION,
        "complete": True,
        "migrated": sorted(migrated),
    }
    atomic_write_json(marker, layout, mode=0o600)
    verified = _read_layout(marker)
    if verified != layout:
        raise StateLayoutError("failed to verify state layout marker")
    _cleanup_legacy(index_root)
    return layout
