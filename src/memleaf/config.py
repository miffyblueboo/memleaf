"""Vault configuration using memleaf's restricted YAML subset."""

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
    "agents": {
        "codex": True,
        "hermes": True,
        "antigravity": False,
    },
    "scopes": {},
    "native_sources": {},
    "process": {
        "memory_compact_threshold_tokens": 100000,
        "memory_compact_candidate_ratio": 0.30,
        "inbox_cleanup_hours": 24,
    },
    "capture": {
        "visible_messages_only": True,
        "include_tool_output": False,
        "include_attachments": False,
        "redact_secrets": True,
    },
    "inject": {
        "mode": "tag_full",
        "abnormal_guard": True,
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
    merged = _merge_defaults(parsed, default_config(vault))
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
    normalized = deepcopy(dict(config))
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
