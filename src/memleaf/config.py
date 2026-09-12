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
DEFAULT_MODEL_CONCURRENCY = 3
MIN_MODEL_CONCURRENCY = 1
MAX_MODEL_CONCURRENCY = 8
THINKING_PURPOSES = ("gate", "summarize", "compact", "single_pass")
THINKING_MODES = frozenset({"default", "disabled", "low", "high", "max"})
DEFAULT_THINKING = {purpose: "low" for purpose in THINKING_PURPOSES}
# Unified extraction is latency-sensitive and keeps deterministic validation in
# Core.  Make the single-pass path non-thinking by default without changing
# the established policy for maintenance/legacy stages.
DEFAULT_THINKING["single_pass"] = "disabled"


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


def _normalize_model_concurrency(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid memleaf process.model_concurrency")
    if not MIN_MODEL_CONCURRENCY <= value <= MAX_MODEL_CONCURRENCY:
        raise ValueError("invalid memleaf process.model_concurrency")
    return value


def _normalize_thinking_settings(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("invalid memleaf llm.thinking settings")
    if set(value) - set(THINKING_PURPOSES):
        raise ValueError("invalid memleaf llm.thinking settings")
    result = dict(DEFAULT_THINKING)
    for purpose, mode in value.items():
        if not isinstance(mode, str) or mode not in THINKING_MODES:
            raise ValueError("invalid memleaf llm.thinking settings")
        result[purpose] = mode
    return result


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
        "model_concurrency": DEFAULT_MODEL_CONCURRENCY,
    },
    "history": {
        "policy": "bounded",
        "retention_days": 3650,
        "max_versions_per_memory": 32,
    },
    "capture": {
        "visible_messages_only": True,
        "tool_evidence_mode": "off",
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
        "thinking": dict(DEFAULT_THINKING),
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
    capture_present = "capture" in normalized
    capture = normalized.get("capture")
    if capture is not None and not isinstance(capture, Mapping):
        raise ValueError("invalid memleaf capture settings")
    if not capture_present:
        # A persisted config without a capture section has no explicit
        # evidence opt-out. Use the current bounded default; explicit legacy
        # false and current metadata/off settings are preserved below.
        normalized["capture"] = {"tool_evidence_mode": "off"}
    elif isinstance(capture, Mapping):
        current = dict(capture)
        legacy_present = "include_tool_output" in current
        explicit_present = "tool_evidence_mode" in current
        if legacy_present:
            legacy = current.pop("include_tool_output")
            if type(legacy) is not bool:
                raise ValueError("invalid legacy memleaf capture.include_tool_output")
            migrated_mode = "bounded" if legacy else "metadata"
            explicit = current.get("tool_evidence_mode")
            if explicit is not None and explicit != migrated_mode:
                raise ValueError("conflicting legacy and current tool evidence settings")
            current["tool_evidence_mode"] = migrated_mode
        elif not explicit_present:
            # A partial capture section was written by older installers before
            # the body-retention mode existed. Keep an explicitly supplied
            # mode (or the legacy boolean above) authoritative, while letting
            # the current default restore normal bounded evidence capture.
            current["tool_evidence_mode"] = "off"
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
    _normalize_model_concurrency(
        process.get("model_concurrency", DEFAULT_MODEL_CONCURRENCY)
        if isinstance(process, Mapping)
        else DEFAULT_MODEL_CONCURRENCY
    )
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
    llm["thinking"] = _normalize_thinking_settings(llm.get("thinking"))
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
    process = normalized.get("process")
    if not isinstance(process, Mapping):
        raise ValueError("invalid memleaf process settings")
    normalized_process = dict(process)
    normalized_process["model_concurrency"] = _normalize_model_concurrency(
        normalized_process.get("model_concurrency", DEFAULT_MODEL_CONCURRENCY)
    )
    normalized["process"] = normalized_process
    llm = normalized.get("llm")
    if not isinstance(llm, Mapping):
        raise ValueError("invalid memleaf llm settings")
    normalized_llm = dict(llm)
    normalized_llm["request_timeout"] = _normalize_request_timeout(
        normalized_llm.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
    )
    normalized_llm["thinking"] = _normalize_thinking_settings(normalized_llm.get("thinking"))
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
