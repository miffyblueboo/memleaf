from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one occurrence, found {count}: {old[:80]!r}")
    write(path, text.replace(old, new, 1))


STATE_LAYOUT = r'''"""Runtime state layout and crash-safe migration from pre-0.2.28 vaults."""

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
'''


VAULT = r'''"""Filesystem layout and path safety for a memleaf vault."""

from __future__ import annotations

import os
from pathlib import Path

from .config import default_config, load_config, save_config
from .locking import VaultLock, atomic_write_json, atomic_write_text


_VAULT_README = """# memleaf vault

This directory is managed by memleaf. Markdown in `knowledge/` is the source
of truth. Files under `_index/` are disposable derived indexes and can be
rebuilt. Files under `_state/` are runtime correctness/recovery state and must
not be deleted as part of index maintenance.
"""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"unsafe {label}")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError(f"unsafe {label}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"unsafe {label}")
    return value


class Vault:
    """A local memleaf vault with predictable subdirectories."""

    def __init__(self, path: Path | str | None = None, *, create: bool = True):
        raw_path = Path(path).expanduser() if path is not None else Path.home() / ".memleaf"
        self.root = _resolved(raw_path)
        if create:
            self.ensure()

    @classmethod
    def initialize(cls, path: Path | str | None = None) -> "Vault":
        return cls(path, create=True)

    @property
    def config_path(self) -> Path:
        return self._inside("config.yaml")

    @property
    def inbox_path(self) -> Path:
        return self._inside("inbox")

    @property
    def knowledge_path(self) -> Path:
        return self._inside("knowledge")

    @property
    def history_path(self) -> Path:
        return self._inside("history")

    @property
    def index_path(self) -> Path:
        return self._inside("_index")

    @property
    def state_path(self) -> Path:
        return self._inside("_state")

    @property
    def logs_path(self) -> Path:
        return self._inside("logs")

    @property
    def tags_index_path(self) -> Path:
        return self._inside("_index", "tags.json")

    @property
    def native_sources_index_path(self) -> Path:
        return self._inside("_index", "native_sources.json")

    @property
    def native_index_path(self) -> Path:
        """Compatibility alias for the native sources derived index."""
        return self.native_sources_index_path

    @property
    def processed_state_path(self) -> Path:
        return self._inside("_state", "processed.json")

    @property
    def processed_index_path(self) -> Path:
        """Deprecated compatibility alias; runtime state moved in v0.2.28."""
        return self.processed_state_path

    @property
    def agents_state_path(self) -> Path:
        return self._inside("_state", "agents.json")

    @property
    def agents_index_path(self) -> Path:
        """Deprecated compatibility alias; host activation is runtime state."""
        return self.agents_state_path

    @property
    def host_ingest_path(self) -> Path:
        return self._inside("_state", "host_ingest.json")

    @property
    def retrieval_gate_state_path(self) -> Path:
        return self._inside("_state", "retrieval_gate.json")

    @property
    def retrieval_gate_lock_path(self) -> Path:
        return self._inside("_state", "retrieval_gate.lock")

    @property
    def lock_path(self) -> Path:
        return self._inside("_state", "vault.lock")

    @property
    def state_layout_path(self) -> Path:
        return self._inside("_state", "layout.json")

    @property
    def compaction_journal_path(self) -> Path:
        return self._inside("_state", "compaction.json")

    @property
    def compaction_staging_root(self) -> Path:
        return self._inside("_state", ".compaction-staging")

    def compaction_staging_dir(self, transaction_id: str) -> Path:
        _safe_component(transaction_id, "compaction transaction id")
        return self._inside("_state", ".compaction-staging", transaction_id)

    def _inside(self, *parts: str) -> Path:
        candidate = self.root.joinpath(*parts)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ValueError("path escapes memleaf vault")
        return candidate

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            self.root = self.root.resolve()
        for directory in (
            self.inbox_path,
            self.knowledge_path,
            self.history_path,
            self.index_path,
            self.state_path,
        ):
            self._ensure_directory(directory)

        with self.lock():
            from .state_layout import migrate_state_layout
            migrate_state_layout(self)

            empty_tags = {
                "version": 1,
                "tags": {},
                "aliases": {},
                "keywords": {},
                "wikilinks": {},
                "history": {
                    "tags": {},
                    "aliases": {},
                    "keywords": {},
                    "wikilinks": {},
                },
            }
            empty_processed = {"version": 1, "event_keys": [], "events": {}, "sessions": {}}
            empty_agents = {"version": 1, "agents": {}}
            from .native_index import empty_native_index

            for path, value in (
                (self.tags_index_path, empty_tags),
                (self.native_sources_index_path, empty_native_index()),
                (self.processed_state_path, empty_processed),
                (self.agents_state_path, empty_agents),
            ):
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("unsafe vault managed path")
                else:
                    atomic_write_json(path, value)
            if not self.config_path.exists():
                save_config(self.config_path, default_config(self.root))
            if not self._inside("README.md").exists():
                atomic_write_text(self._inside("README.md"), _VAULT_README)

    def _ensure_directory(self, directory: Path) -> None:
        if directory.exists() and directory.is_symlink():
            raise ValueError("unsafe vault directory")
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    def config(self) -> dict:
        return load_config(self.config_path, vault=self.root)

    def lock(self) -> VaultLock:
        return VaultLock(self.lock_path)

    def memory_path(self, memory_id: str, area: str = "knowledge") -> Path:
        _safe_component(memory_id, "memory id")
        if area == "knowledge":
            base = self.knowledge_path
        elif area == "history":
            base = self.history_path
        else:
            raise ValueError("invalid memory area")
        return self._inside(str(base.relative_to(self.root)), f"{memory_id}.md")

    def history_version_path(self, memory_id: str, version: str) -> Path:
        _safe_component(memory_id, "memory id")
        _safe_component(version, "history version")
        if version.endswith(".md"):
            version = version[:-3]
            _safe_component(version, "history version")
        return self._inside("history", f"{memory_id}--{version}.md")

    def session_path(self, source: str, session_id: str) -> Path:
        _safe_component(source, "source")
        _safe_component(session_id, "session id")
        source_directory = self._inside("inbox", source)
        if source_directory.exists() and source_directory.is_symlink():
            raise ValueError("unsafe inbox source directory")
        source_directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(source_directory, 0o700)
        except OSError:
            pass
        return self._inside("inbox", source, f"{session_id}.md")

    def list_markdown(self, area: str) -> list[Path]:
        if area == "knowledge":
            base = self.knowledge_path
        elif area == "history":
            base = self.history_path
        elif area == "inbox":
            base = self.inbox_path
        else:
            raise ValueError("invalid markdown area")
        if not base.exists():
            return []
        paths: list[Path] = []
        for path in sorted(base.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(self.root)
            except ValueError:
                continue
            paths.append(path)
        return paths


def safe_component(value: str, label: str = "path component") -> str:
    """Public validation helper used by capture and tests."""
    return _safe_component(value, label)
'''


CONFIG = r'''"""Vault configuration using memleaf's restricted YAML subset."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any, Mapping

from .frontmatter import FrontmatterError, dump_yaml, load_yaml
from .locking import atomic_write_text
from .native_index import NativeConfigError, validate_native_sources
from .scope_state import ScopeError, validate_scope_registry


DEFAULT_REQUEST_TIMEOUT = 120
MIN_REQUEST_TIMEOUT = 1
MAX_REQUEST_TIMEOUT = 240


def _normalize_request_timeout(value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError("invalid memleaf llm.request_timeout")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError("invalid memleaf llm.request_timeout") from None
    if not math.isfinite(parsed) or not MIN_REQUEST_TIMEOUT <= parsed <= MAX_REQUEST_TIMEOUT:
        raise ValueError("invalid memleaf llm.request_timeout")
    return int(parsed) if parsed.is_integer() else parsed


DEFAULT_CONFIG: dict[str, Any] = {
    "vault": "~/.memleaf",
    "agents": {"codex": True, "hermes": True, "antigravity": False},
    "scopes": {},
    "native_sources": {},
    "process": {
        "memory_compact_threshold_tokens": 100000,
        "memory_compact_candidate_ratio": 0.30,
        "inbox_cleanup_hours": 24,
        "closed_todo_retention_days": 30,
    },
    "history": {
        "policy": "bounded",
        "retention_days": 3650,
        "max_versions_per_memory": 32,
    },
    "capture": {
        "visible_messages_only": True,
        "tool_evidence_mode": "bounded",
        "include_attachments": False,
        "redact_secrets": True,
    },
    "llm": {
        "mode": "auto",
        "provider": "",
        "protocol": "openai",
        "base_url": "",
        "api_key": "",
        "api_key_env": "",
        "model": "",
        "context_window": 200000,
        "request_timeout": DEFAULT_REQUEST_TIMEOUT,
        "diagnostic_logging": False,
    },
}


def _merge_defaults(value: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = deepcopy(dict(defaults))
    for key, item in value.items():
        if isinstance(item, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_defaults(item, merged[key])
        else:
            merged[key] = item
    return merged


def _normalize_legacy_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """Translate supported pre-v0.2.28 fields once, then forget their names."""
    normalized = deepcopy(dict(value))
    if "inject" in normalized:
        if not isinstance(normalized["inject"], Mapping):
            raise ValueError("invalid legacy memleaf inject settings")
        normalized.pop("inject", None)
    capture = normalized.get("capture")
    if capture is not None and not isinstance(capture, Mapping):
        raise ValueError("invalid memleaf capture settings")
    if isinstance(capture, Mapping):
        current = dict(capture)
        if "include_tool_output" in current:
            legacy = current.pop("include_tool_output")
            if type(legacy) is not bool:
                raise ValueError("invalid legacy memleaf capture.include_tool_output")
            migrated_mode = "bounded" if legacy else "metadata"
            explicit = current.get("tool_evidence_mode")
            if explicit is not None and explicit != migrated_mode:
                raise ValueError("conflicting legacy and current tool evidence settings")
            current["tool_evidence_mode"] = migrated_mode
        normalized["capture"] = current
    return normalized


def default_config(vault: Path | str | None = None) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    if vault is not None:
        config["vault"] = str(Path(vault).expanduser())
    return config


def load_config(path: Path | str, *, vault: Path | str | None = None) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return default_config(vault)
    try:
        parsed = load_yaml(config_path.read_text(encoding="utf-8"))
    except (OSError, FrontmatterError) as error:
        raise ValueError("invalid memleaf config.yaml") from error
    if not isinstance(parsed, dict):
        raise ValueError("invalid memleaf config.yaml")
    parsed = _normalize_legacy_config(parsed)
    merged = _merge_defaults(parsed, default_config(vault))
    from .evidence_policy import capture_settings
    merged["capture"] = capture_settings(merged)
    if not isinstance(merged.get("vault"), str):
        raise ValueError("invalid memleaf vault setting")
    capture = merged.get("capture")
    if not isinstance(capture, Mapping) or not isinstance(capture.get("redact_secrets"), bool):
        raise ValueError("invalid memleaf capture settings")
    process = merged.get("process")
    threshold = process.get("memory_compact_threshold_tokens") if isinstance(process, Mapping) else None
    ratio = process.get("memory_compact_candidate_ratio") if isinstance(process, Mapping) else None
    if type(threshold) is not int or threshold <= 0:
        raise ValueError("invalid memleaf process.memory_compact_threshold_tokens")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio)):
        raise ValueError("invalid memleaf process.memory_compact_candidate_ratio")
    if not 0 < float(ratio) <= 1:
        raise ValueError("invalid memleaf process.memory_compact_candidate_ratio")
    cleanup_hours = process.get("inbox_cleanup_hours") if isinstance(process, Mapping) else None
    if type(cleanup_hours) is not int or cleanup_hours < 0:
        raise ValueError("invalid memleaf process.inbox_cleanup_hours")
    closed_todo_days = process.get("closed_todo_retention_days") if isinstance(process, Mapping) else None
    if type(closed_todo_days) is not int or closed_todo_days < 0:
        raise ValueError("invalid memleaf process.closed_todo_retention_days")
    history = merged.get("history")
    if not isinstance(history, Mapping):
        raise ValueError("invalid memleaf history settings")
    if history.get("policy") not in {"bounded", "keep_all"}:
        raise ValueError("invalid memleaf history.policy")
    if type(history.get("retention_days")) is not int or history["retention_days"] < 1:
        raise ValueError("invalid memleaf history.retention_days")
    if type(history.get("max_versions_per_memory")) is not int or history["max_versions_per_memory"] < 1:
        raise ValueError("invalid memleaf history.max_versions_per_memory")
    llm = merged.get("llm")
    if not isinstance(llm, Mapping):
        raise ValueError("invalid memleaf llm settings")
    llm = dict(llm)
    llm["request_timeout"] = _normalize_request_timeout(llm.get("request_timeout", DEFAULT_REQUEST_TIMEOUT))
    if type(llm.get("diagnostic_logging", False)) is not bool:
        raise ValueError("invalid memleaf llm.diagnostic_logging")
    merged["llm"] = llm
    try:
        merged["scopes"] = validate_scope_registry(merged.get("scopes", {}))
    except ScopeError as error:
        raise ValueError("invalid memleaf scopes registry") from error
    try:
        validate_native_sources(merged.get("native_sources", {}), base_dir=config_path.parent)
    except NativeConfigError as error:
        raise ValueError("invalid memleaf native_sources") from error
    return merged


def save_config(path: Path | str, config: Mapping[str, Any]) -> None:
    if not isinstance(config, Mapping):
        raise ValueError("config must be a mapping")
    normalized = _normalize_legacy_config(config)
    from .evidence_policy import capture_settings
    normalized["capture"] = capture_settings(normalized)
    try:
        normalized["scopes"] = validate_scope_registry(normalized.get("scopes", {}))
    except ScopeError as error:
        raise ValueError("invalid memleaf scopes registry") from error
    try:
        validate_native_sources(normalized.get("native_sources", {}), base_dir=Path(path).parent)
    except NativeConfigError as error:
        raise ValueError("invalid memleaf native_sources") from error
    llm = normalized.get("llm")
    if not isinstance(llm, Mapping):
        raise ValueError("invalid memleaf llm settings")
    normalized_llm = dict(llm)
    normalized_llm["request_timeout"] = _normalize_request_timeout(
        normalized_llm.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
    )
    diagnostic_logging = normalized_llm.get("diagnostic_logging", False)
    if type(diagnostic_logging) is not bool:
        raise ValueError("invalid memleaf llm.diagnostic_logging")
    normalized_llm["diagnostic_logging"] = diagnostic_logging
    normalized["llm"] = normalized_llm
    try:
        text = dump_yaml(normalized)
    except FrontmatterError as error:
        raise ValueError("config cannot be serialized") from error
    atomic_write_text(Path(path), text)
'''


EVIDENCE_POLICY = r'''"""One capture-retention policy for direct, Hermes and hook-based ingress.

This module does not infer business meaning. Adapters label document payloads
from structural file arguments; no tool-name or domain keyword grants consent.
The policy applies before pending-cache and inbox writes. Already committed
knowledge and already captured inbox events are not retroactively deleted.
"""
from __future__ import annotations

from typing import Any, Mapping
from .provenance import normalize_tool_evidence


MODES = frozenset({"bounded", "metadata", "off"})


def capture_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the current capture settings; legacy names are migrated in config."""
    settings = value.get("capture", {})
    if not isinstance(settings, Mapping):
        raise ValueError("invalid memleaf capture settings")
    settings = dict(settings)
    for field in ("include_attachments", "redact_secrets", "visible_messages_only"):
        if field in settings and type(settings[field]) is not bool:
            raise ValueError("invalid memleaf capture." + field)
    mode = settings.get("tool_evidence_mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("invalid memleaf capture.tool_evidence_mode")
    settings.setdefault("include_attachments", False)
    return settings


def document_arguments(value: Any, depth: int = 0) -> bool:
    """Recognize structural file/attachment handles, not business semantics."""
    if depth > 4:
        return False
    if isinstance(value, Mapping):
        for key, item in list(value.items())[:32]:
            if key in {"path", "file", "file_path", "filepath", "filename", "attachment_id", "file_id"}:
                if isinstance(item, str) and item.strip():
                    return True
            if key == "uri" and isinstance(item, str) and item.startswith("file://"):
                return True
            if isinstance(item, (Mapping, list, tuple)) and document_arguments(item, depth + 1):
                return True
    elif isinstance(value, (list, tuple)):
        return any(document_arguments(item, depth + 1) for item in value[:32])
    return False


def retain_tool_evidence(value: Any, config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Normalize/redact, then apply the same permission to every storage path."""
    policy = capture_settings(config)
    if policy["tool_evidence_mode"] == "off":
        return []
    output = []
    for record in normalize_tool_evidence(value):
        record = dict(record)
        excluded = (
            policy["tool_evidence_mode"] == "metadata"
            or record.get("retention") == "metadata"
            or (record.get("source_type") == "document" and not policy["include_attachments"])
        )
        if excluded:
            record.pop("content", None)
            record["retention"] = "metadata"
            record["completeness"] = "missing"
        output.append(record)
    return output
'''


STATE_TEST = r'''from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memleaf import Memleaf
from memleaf.host_runtime import HostRuntime
from memleaf.locking import atomic_write_json
from memleaf.retrieval_gate import begin_turn, validate_turn
from memleaf.state_layout import StateLayoutError
from memleaf.vault import Vault


class StateLayoutV028Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="memleaf-state-layout-")
        self.root = Path(self.tempdir.name) / "vault"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _legacy_processed() -> dict:
        return {
            "version": 1,
            "event_keys": ["a" * 64],
            "events": {"a" * 64: {"event_key": "a" * 64}},
            "sessions": {
                "codex/s1": {
                    "watermark": 7,
                    "processed_watermark": 7,
                    "lineage_parent_session_id": "parent",
                    "processed_turns": [{"turn_key": "t", "turn_index": 7, "event_keys": ["a" * 64]}],
                }
            },
            "pending_turn_plans": {"plan": {"status": "pending"}},
            "pending_operations": {"op": {"status": "prepared"}},
        }

    def _make_legacy(self) -> None:
        index = self.root / "_index"
        index.mkdir(parents=True, exist_ok=True)
        (self.root / "knowledge").mkdir(exist_ok=True)
        (self.root / "history").mkdir(exist_ok=True)
        (self.root / "inbox").mkdir(exist_ok=True)
        atomic_write_json(index / "processed.json", self._legacy_processed())
        atomic_write_json(index / "agents.json", {"version": 1, "agents": {"codex": {"hook_activation_status": "active"}}})
        atomic_write_json(index / "host_ingest.json", {"version": 2, "hosts": {"codex": {"s1": {"process_pending": True}}}, "transcripts": {}})
        atomic_write_json(index / "retrieval_gate.json", {"version": 1, "entries": {"rtv-old": {"status": "FOUND"}}})
        atomic_write_json(index / "compaction.json", {"version": 1, "transaction_id": "tx", "phase": "staged", "sources": [], "replacements": [], "histories": []})
        staging = index / ".compaction-staging" / "tx"
        staging.mkdir(parents=True)
        (staging / "original.md").write_text("original", encoding="utf-8")

    def test_old_vault_migrates_all_runtime_state(self) -> None:
        self._make_legacy()
        vault = Vault(self.root)
        self.assertEqual(json.loads(vault.processed_state_path.read_text()), self._legacy_processed())
        self.assertEqual(json.loads(vault.agents_state_path.read_text())["agents"]["codex"]["hook_activation_status"], "active")
        self.assertTrue(vault.host_ingest_path.is_file())
        self.assertTrue(vault.retrieval_gate_state_path.is_file())
        self.assertTrue(vault.compaction_journal_path.is_file())
        self.assertEqual((vault.compaction_staging_root / "tx" / "original.md").read_text(), "original")
        for name in ("processed.json", "agents.json", "host_ingest.json", "retrieval_gate.json", "compaction.json"):
            self.assertFalse((vault.index_path / name).exists())
        self.assertFalse((vault.index_path / ".compaction-staging").exists())
        self.assertTrue(vault.state_layout_path.is_file())

    def test_migration_crash_before_marker_is_idempotently_recovered(self) -> None:
        self._make_legacy()
        real_atomic = atomic_write_json

        def fail_marker(path, value, mode=0o600):
            if Path(path).name == "layout.json":
                raise OSError("simulated crash")
            return real_atomic(path, value, mode=mode)

        with mock.patch("memleaf.state_layout.atomic_write_json", side_effect=fail_marker):
            with self.assertRaises(OSError):
                Vault(self.root)
        self.assertTrue((self.root / "_state" / "processed.json").is_file())
        self.assertTrue((self.root / "_index" / "processed.json").is_file())
        vault = Vault(self.root)
        self.assertEqual(json.loads(vault.processed_state_path.read_text()), self._legacy_processed())
        self.assertFalse((vault.index_path / "processed.json").exists())

    def test_repeat_migration_is_idempotent(self) -> None:
        self._make_legacy()
        first = Vault(self.root)
        before = first.processed_state_path.read_bytes()
        second = Vault(self.root)
        self.assertEqual(before, second.processed_state_path.read_bytes())
        self.assertTrue(second.state_layout_path.exists())

    def test_pre_marker_old_and_new_conflict_fails_closed(self) -> None:
        self._make_legacy()
        state = self.root / "_state"
        state.mkdir(parents=True)
        different = self._legacy_processed()
        different["sessions"]["codex/s1"]["watermark"] = 99
        atomic_write_json(state / "processed.json", different)
        with self.assertRaisesRegex(StateLayoutError, "conflicting legacy and current"):
            Vault(self.root)

    def test_corrupt_old_state_fails_closed(self) -> None:
        self._make_legacy()
        (self.root / "_index" / "processed.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(StateLayoutError, "invalid processed.json"):
            Vault(self.root)

    def test_completed_marker_makes_new_state_authoritative(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "_index").mkdir()
        (self.root / "_state").mkdir()
        new_value = self._legacy_processed()
        old_value = self._legacy_processed()
        old_value["sessions"]["codex/s1"]["watermark"] = 999
        atomic_write_json(self.root / "_state" / "processed.json", new_value)
        atomic_write_json(self.root / "_index" / "processed.json", old_value)
        atomic_write_json(self.root / "_state" / "layout.json", {"version": 1, "complete": True, "migrated": ["processed.json"]})
        vault = Vault(self.root)
        self.assertEqual(json.loads(vault.processed_state_path.read_text()), new_value)
        self.assertFalse((vault.index_path / "processed.json").exists())

    def test_fresh_install_uses_separate_index_and_state(self) -> None:
        vault = Vault(self.root)
        self.assertTrue(vault.index_path.is_dir())
        self.assertTrue(vault.state_path.is_dir())
        self.assertEqual({p.name for p in vault.index_path.iterdir() if p.is_file()}, {"tags.json", "native_sources.json"})
        self.assertTrue(vault.processed_state_path.is_file())
        self.assertTrue(vault.agents_state_path.is_file())
        self.assertTrue(vault.lock_path.parent == vault.state_path)

    def test_rebuild_index_never_rewrites_runtime_state(self) -> None:
        service = Memleaf(self.root)
        service.create_memory(memory_id="m1", title="One", body="body", tags=["tag"])
        service.capture("codex", "s1", "t1", "user", "remember me")
        service.session_lineage("codex", "child", parent_session_id="s1")
        processed = json.loads(service.vault.processed_state_path.read_text())
        processed["pending_turn_plans"] = {"p": {"status": "pending"}}
        processed["pending_operations"] = {"o": {"status": "prepared"}}
        atomic_write_json(service.vault.processed_state_path, processed)
        retrieval_id = begin_turn(service.vault, "codex", "s1", "t1")
        self.assertEqual(validate_turn(service.vault, retrieval_id)["status"], "NOT_SEARCHED")
        HostRuntime(service, "codex")._set_process_pending("s1", True)
        before = {
            path.name: path.read_bytes()
            for path in service.vault.state_path.iterdir()
            if path.is_file() and path.name not in {"vault.lock", "retrieval_gate.lock"}
        }
        service.rebuild_index()
        after = {
            path.name: path.read_bytes()
            for path in service.vault.state_path.iterdir()
            if path.is_file() and path.name not in {"vault.lock", "retrieval_gate.lock"}
        }
        self.assertEqual(before, after)

    def test_deleting_index_then_rebuild_preserves_state(self) -> None:
        service = Memleaf(self.root)
        service.create_memory(memory_id="m1", title="One", body="body", tags=["tag"])
        service.capture("hermes", "s", "t", "user", "x")
        before = service.vault.processed_state_path.read_bytes()
        shutil.rmtree(service.vault.index_path)
        result = service.rebuild_index()
        self.assertEqual(result["knowledge"], 1)
        self.assertEqual(before, service.vault.processed_state_path.read_bytes())
        self.assertTrue(service.vault.tags_index_path.is_file())
        self.assertTrue(service.vault.native_sources_index_path.is_file())


if __name__ == "__main__":
    unittest.main()
'''


CONFIG_TEST = r'''from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memleaf.config import default_config, load_config, save_config
from memleaf.frontmatter import dump_yaml


class ConfigMigrationV028Tests(unittest.TestCase):
    def test_legacy_inject_and_tool_output_normalize_then_save_current_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-config-migration-") as temporary:
            path = Path(temporary) / "config.yaml"
            value = default_config(Path(temporary) / "vault")
            value["inject"] = {"mode": "tag_full", "abnormal_guard": True}
            value["capture"].pop("tool_evidence_mode")
            value["capture"]["include_tool_output"] = False
            path.write_text(dump_yaml(value), encoding="utf-8")
            loaded = load_config(path)
            self.assertNotIn("inject", loaded)
            self.assertNotIn("include_tool_output", loaded["capture"])
            self.assertEqual(loaded["capture"]["tool_evidence_mode"], "metadata")
            save_config(path, loaded)
            written = path.read_text(encoding="utf-8")
            self.assertNotIn("inject:", written)
            self.assertNotIn("include_tool_output", written)
            self.assertIn("tool_evidence_mode: metadata", written)

    def test_conflicting_legacy_and_current_capture_settings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memleaf-config-migration-") as temporary:
            path = Path(temporary) / "config.yaml"
            value = default_config(Path(temporary) / "vault")
            value["capture"]["include_tool_output"] = False
            value["capture"]["tool_evidence_mode"] = "bounded"
            path.write_text(dump_yaml(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting legacy and current"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
'''


CONFIG_DOC = r'''# Configuration migrations

This document describes the configuration and Vault-layout compatibility boundary starting with memleaf v0.2.28.

## Current configuration

The current persisted top-level sections are `vault`, `agents`, `scopes`, `native_sources`, `process`, `history`, `capture`, and `llm`. Retrieval remains Scope Map -> search -> read; there is no configurable legacy injection mode.

`capture.tool_evidence_mode` is the current tool-evidence retention setting and accepts `bounded`, `metadata`, or `off`. `capture.include_attachments` is independent and defaults to `false`.

## Deprecated fields

- Top-level `inject` (`mode`, `abnormal_guard`) belonged to the pre-Scope-Map injection path. v0.2.28 reads and discards this section; it is not written again.
- `capture.include_tool_output` is replaced by `capture.tool_evidence_mode`.
- `Vault.processed_index_path` and `Vault.agents_index_path` remain narrow Python compatibility aliases for v0.2.28, but both resolve to `_state/`; internal code uses the new state names. They are candidates for removal in a future major cleanup.

## Automatic migration

When reading an older configuration, `capture.include_tool_output: true` becomes `tool_evidence_mode: bounded`; `false` becomes `metadata`. Saving the normalized configuration writes only the current field. If both legacy and current fields are present but disagree, loading fails closed instead of guessing.

The obsolete `inject` section is removed during normalization. No current runtime component consumes it.

On first v0.2.28 Vault use, runtime correctness state is migrated from `_index/` to `_state/` by one centralized layout owner. The new copy is atomically written and verified before a durable `_state/layout.json` completion marker is written; legacy files are removed only after that marker. Before the marker, old/new coexistence must be equivalent or startup fails closed. After the marker, `_state/` is authoritative and stale `_index/` state is cleanup debris, never merged or replayed.

## Incompatible cases

Migration fails closed for malformed runtime JSON, unsafe symlinks, divergent pre-marker old/new state, an invalid layout marker, or an old process still holding a legacy lock strongly enough to prevent cleanup. Stop older memleaf/Hermes/Codex processes and retry; do not delete `_state/` to force an upgrade.

## User action

Normal users do not need to edit their Vault or configuration. For a live upgrade, stop older processes that are using the Vault before starting v0.2.28. Backups remain recommended for any software upgrade. `_index/` is disposable and rebuildable; `_state/` is not.
'''

write("src/memleaf/state_layout.py", STATE_LAYOUT)
write("src/memleaf/vault.py", VAULT)
write("src/memleaf/config.py", CONFIG)
write("src/memleaf/evidence_policy.py", EVIDENCE_POLICY)
write("tests/test_state_layout_v028.py", STATE_TEST)
write("tests/test_config_migrations_v028.py", CONFIG_TEST)
write("docs/config-migrations.md", CONFIG_DOC)

for path in sorted((ROOT / "src" / "memleaf").rglob("*.py")):
    if path.name == "vault.py":
        continue
    text = path.read_text(encoding="utf-8")
    updated = text.replace(".processed_index_path", ".processed_state_path")
    updated = updated.replace(".agents_index_path", ".agents_state_path")
    if updated != text:
        path.write_text(updated, encoding="utf-8")

for path in sorted((ROOT / "tests").rglob("test_*.py")):
    text = path.read_text(encoding="utf-8")
    updated = text.replace(".processed_index_path", ".processed_state_path")
    updated = updated.replace(".agents_index_path", ".agents_state_path")
    updated = updated.replace('/ "_index" / "processed.json"', '/ "_state" / "processed.json"')
    updated = updated.replace('/ "_index" / "agents.json"', '/ "_state" / "agents.json"')
    updated = updated.replace('/ "_index" / "retrieval_gate.json"', '/ "_state" / "retrieval_gate.json"')
    if updated != text:
        path.write_text(updated, encoding="utf-8")

path = "src/memleaf/retrieval_gate.py"
text = read(path)
text = text.replace('path = root.index_path / "retrieval_gate.json"', 'path = root.retrieval_gate_state_path')
text = text.replace('path = root.index_path / "retrieval_gate.lock"', 'path = root.retrieval_gate_lock_path')
if text.count("root.index_path"):
    raise SystemExit("retrieval_gate.py still contains direct index_path state access")
write(path, text)

path = "src/memleaf/adapters/base.py"
text = read(path)
old = 'return Path(root).expanduser().resolve() / "_index" / "agents.json"'
if old not in text:
    raise SystemExit("adapters/base.py agents path pattern changed")
text = text.replace(old, 'return Path(root).expanduser().resolve() / "_state" / "agents.json"', 1)
text = text.replace('"Return the agents index path without creating or changing the vault."', '"Return the host activation state path without creating or changing the vault."')
write(path, text)

path = "src/memleaf/service.py"
text = read(path)
text = text.replace('"""The stage-A local core API."""', '"""The local Markdown memory core API."""', 1)
text = text.replace("    build_processed_index,\n", "")
pattern = re.compile(r"    def _rebuild_index_unlocked\(self\) -> dict\[str, int\]:\n.*?\n    def rebuild_index\(", re.S)
replacement = '''    def _rebuild_index_unlocked(self) -> dict[str, int]:
        """Rebuild only data that is fully derivable from source files."""
        knowledge = self._read_memories_unlocked("knowledge")
        history = self._read_memories_unlocked("history")
        atomic_write_json(
            self.vault.tags_index_path,
            build_tags_index(
                [record.memory for record in knowledge],
                [record.memory for record in history],
            ),
        )
        event_keys: set[str] = set()
        for path in self.vault.list_markdown("inbox"):
            try:
                event_keys.update(extract_event_keys(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError):
                continue
        return {
            "knowledge": len(knowledge),
            "history": len(history),
            "events": len(event_keys),
        }

    def rebuild_index('''
text, count = pattern.subn(replacement, text, count=1)
if count != 1:
    raise SystemExit(f"service rebuild replacement count={count}")
write(path, text)

path = "src/memleaf/index.py"
text = read(path)
for other in (ROOT / "src" / "memleaf").rglob("*.py"):
    if other.name == "index.py":
        continue
    if "build_processed_index" in other.read_text(encoding="utf-8"):
        raise SystemExit(f"build_processed_index still referenced by {other}")
text = text.replace("from copy import deepcopy\n", "")
text, count = re.subn(r"\n\ndef build_processed_index\(.*\Z", "\n", text, flags=re.S)
if count != 1:
    raise SystemExit(f"build_processed_index removal count={count}")
write(path, text)

path = "src/memleaf/capture.py"
text = read(path)
mail_block = re.compile(r'\n\n_MAIL_EVIDENCE_FIELDS = .*?_MAX_MAIL_EVIDENCE_TEXT = 320\n', re.S)
text, count = mail_block.subn("\n", text, count=1)
if count != 1:
    raise SystemExit(f"capture dead mail block removal count={count}")
write(path, text)

path = "src/memleaf/compaction.py"
text = read(path).replace('"""Local, deterministic active-memory compaction for stage B3a."""', '"""Local, deterministic active-memory compaction."""', 1)
write(path, text)

path = "tests/test_stage_a.py"
text = read(path)
text = text.replace('("inbox", "knowledge", "history", "_index")', '("inbox", "knowledge", "history", "_index", "_state")', 1)
text = text.replace('self.assertTrue((self.vault_path / "_index" / "processed.json").is_file())', 'self.assertTrue((self.vault_path / "_state" / "processed.json").is_file())')
text = text.replace('json.loads((self.vault_path / "_index" / "processed.json").read_text())', 'json.loads((self.vault_path / "_state" / "processed.json").read_text())')
write(path, text)

for path in sorted((ROOT / "src" / "memleaf").rglob("*.py")):
    if path.name == "state_layout.py":
        continue
    text = path.read_text(encoding="utf-8")
    for forbidden in (
        '_index" / "processed.json', '_index" / "agents.json', '_index" / "host_ingest.json',
        '_index" / "retrieval_gate.json', '_index" / "compaction.json',
        '"_index", "processed.json"', '"_index", "agents.json"', '"_index", "host_ingest.json"',
        '"_index", "retrieval_gate.json"', '"_index", "compaction.json"',
    ):
        if forbidden in text:
            raise SystemExit(f"runtime state still rooted in _index: {path}: {forbidden}")

print("v0.2.28 phase-1 patch applied")
