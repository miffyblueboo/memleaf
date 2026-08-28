"""Antigravity central MCP JSON detection and atomic merge adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .base import (
    CommandRunner,
    ConfigureResult,
    Detection,
    HookMergeResult,
    absolute_vault,
    adapter_environment,
    adapter_home,
    atomic_replace_bytes,
    host_event_command,
    hook_activation_status as persisted_hook_activation_status,
    hook_definition_fingerprint,
    make_backup,
    merge_hook_config,
    result_from_detection,
)


_ANTIGRAVITY_HOOK_USER_ACTION = (
    "Fully quit and reopen Antigravity, then complete one test turn to activate the memleaf hooks."
)


class AntigravityAdapter:
    """Handle only the existing, valid central Antigravity JSON config."""

    agent = "antigravity"

    def __init__(
        self,
        home: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
        *,
        command_runner: CommandRunner | None = None,
        path: str | Sequence[str] | None = None,
    ) -> None:
        if runner is not None and command_runner is not None:
            raise ValueError("provide runner or command_runner, not both")
        self.env = adapter_environment(env)
        effective_home = home if home is not None else self.env.get("HOME")
        self.home = adapter_home(effective_home)
        if path is not None:
            self.env["PATH"] = (
                path if isinstance(path, str) else os.pathsep.join(path)
            )
        # Kept for a uniform injectable adapter surface; Antigravity has no
        # supported CLI in this stage, so it is intentionally unused.
        self.runner = runner or command_runner

    @property
    def config_path(self) -> Path:
        return self.home / ".gemini" / "config" / "mcp_config.json"

    @property
    def hooks_path(self) -> Path:
        return self.home / ".gemini" / "config" / "hooks.json"

    def detect(self) -> Detection:
        config = self.config_path
        config_value = str(config)
        if config.is_symlink():
            return Detection(
                agent=self.agent,
                detected=False,
                confidence="low",
                reason="central configuration is a symlink; refusing access",
                config_path=config_value,
                status="diagnostic",
            )
        if not config.exists():
            return Detection(
                agent=self.agent,
                detected=False,
                confidence="none",
                reason="central configuration was not found; no path guessed",
                config_path=config_value,
                status="not_detected",
            )
        if not config.is_file():
            return Detection(
                agent=self.agent,
                detected=False,
                confidence="low",
                reason="central configuration is not a regular file",
                config_path=config_value,
                status="diagnostic",
            )
        state, _ = _load_config(config)
        if state != "valid":
            return Detection(
                agent=self.agent,
                detected=False,
                confidence="low",
                reason="central configuration is not a valid MCP JSON object",
                config_path=config_value,
                status="diagnostic",
            )
        return Detection(
            agent=self.agent,
            detected=True,
            confidence="high",
            reason="valid central MCP JSON configuration found",
            config_path=config_value,
            status="detected",
        )

    def configure(
        self,
        detection: Detection | Path | str | None = None,
        vault: Path | str | None = None,
        *,
        dry_run: bool = False,
        attempt: bool = False,
    ) -> ConfigureResult:
        if detection is not None and not isinstance(detection, Detection) and vault is None:
            vault = detection
            detection = None
        detection = self._coerce_detection(detection, vault)
        if vault is None:
            raise ValueError("vault is required")
        if not detection.detected or detection.confidence != "high":
            return result_from_detection(
                detection,
                status="diagnostic" if attempt else "not_detected",
                reason=(
                    "host was not reliably detected; no configuration path guessed"
                    if attempt
                    else detection.reason
                ),
                dry_run=dry_run,
            )

        config = Path(detection.config_path) if detection.config_path else self.config_path
        if config.is_symlink():
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="central configuration is a symlink; unchanged",
                dry_run=dry_run,
            )
        if not config.is_file():
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="central configuration is not a regular file; unchanged",
                dry_run=dry_run,
            )
        state, document = _load_config(config)
        if state != "valid" or document is None:
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="central configuration is invalid; unchanged",
                dry_run=dry_run,
            )

        hook_definition = _antigravity_hook_definition(vault)
        hook_hash = hook_definition_fingerprint(hook_definition)
        activation_status = persisted_hook_activation_status(
            vault,
            self.agent,
            hook_hash,
            "pending_restart",
        )
        if not dry_run:
            hook_preflight = _configure_antigravity_hooks(self.hooks_path, vault, dry_run=True)
            if hook_preflight.status == "diagnostic":
                return result_from_detection(
                    detection,
                    status="diagnostic",
                    reason=hook_preflight.reason,
                    hook_activation_status="pending_restart",
                    hook_definition_hash=hook_hash,
                    user_action_required=True,
                    user_action=_ANTIGRAVITY_HOOK_USER_ACTION,
                )

        expected = {
            "command": "memleaf-mcp",
            "args": ["--vault", absolute_vault(vault)],
        }
        servers = document["mcpServers"]
        existing = servers.get("memleaf")
        if isinstance(existing, Mapping):
            if _entry_matches(existing, expected):
                mcp_changed = False
                backup = None
                mcp_reason = "existing memleaf entry is correct"
            else:
                return result_from_detection(
                    detection,
                    status="diagnostic",
                    reason="existing memleaf entry is conflicting; unchanged",
                    dry_run=dry_run,
                )
        elif existing is not None:
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="existing memleaf entry is conflicting; unchanged",
                dry_run=dry_run,
            )
        else:
            if dry_run:
                mcp_changed = False
                backup = None
                mcp_reason = "would atomically merge the central MCP JSON configuration"
            else:
                backup = None
                try:
                    backup = make_backup(config)
                except Exception:
                    return result_from_detection(
                        detection,
                        status="failure",
                        reason="could not create configuration backup; unchanged",
                    )

                merged = dict(document)
                merged_servers = dict(servers)
                merged_servers["memleaf"] = expected
                merged["mcpServers"] = merged_servers
                payload = (
                    json.dumps(merged, ensure_ascii=False, indent=2, separators=(",", ": "))
                    + "\n"
                ).encode("utf-8")
                try:
                    mode = config.stat().st_mode & 0o7777
                    atomic_replace_bytes(config, payload, mode=mode or 0o600)
                except Exception:
                    return result_from_detection(
                        detection,
                        status="failure",
                        reason="atomic configuration update failed; backup retained",
                        backup_path=backup,
                    )
                mcp_changed = True
                mcp_reason = "central MCP JSON configuration updated"

        hook_result = _configure_antigravity_hooks(self.hooks_path, vault, dry_run=dry_run)
        if hook_result.status in ("diagnostic", "failure"):
            return result_from_detection(
                detection,
                status=hook_result.status,
                reason=hook_result.reason,
                changed=mcp_changed,
                backup_path=hook_result.backup_path or backup,
                dry_run=dry_run,
                hook_activation_status="pending_restart",
                hook_definition_hash=hook_hash,
                user_action_required=True,
                user_action=_ANTIGRAVITY_HOOK_USER_ACTION,
            )
        return result_from_detection(
            detection,
            status=(
                "would_configure"
                if dry_run
                else "configured" if mcp_changed or hook_result.changed else "already_configured"
            ),
            reason=(
                "would configure MCP entry and lifecycle hooks"
                if dry_run
                else f"{mcp_reason}; lifecycle hooks configured"
                if mcp_changed and hook_result.changed
                else "lifecycle hooks configured"
                if hook_result.changed
                else mcp_reason
            ),
            changed=(mcp_changed or hook_result.changed) if not dry_run else False,
            backup_path=backup or hook_result.backup_path,
            dry_run=dry_run,
            hook_activation_status=activation_status,
            hook_definition_hash=hook_hash,
            user_action_required=activation_status != "active",
            user_action=(
                _ANTIGRAVITY_HOOK_USER_ACTION
                if activation_status != "active"
                else None
            ),
        )

    def _coerce_detection(
        self,
        detection: Detection | Path | str | None,
        vault: Path | str | None,
    ) -> Detection:
        if isinstance(detection, Detection) or detection is None:
            return detection or self.detect()
        if vault is None:
            return self.detect()
        return self.detect()


Antigravity = AntigravityAdapter


def _configure_antigravity_hooks(
    path: Path,
    vault: Path | str,
    *,
    dry_run: bool = False,
    interpreter: str | Path | None = None,
) -> HookMergeResult:
    definition = _antigravity_hook_definition(vault, interpreter=interpreter)
    return merge_hook_config(
        path,
        definition,
        container_key="memleaf",
        dry_run=dry_run,
    )


def _antigravity_hook_definition(
    vault: Path | str,
    *,
    interpreter: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    command = host_event_command("antigravity", "PreInvocation", vault, interpreter=interpreter)
    stop_command = host_event_command("antigravity", "Stop", vault, interpreter=interpreter)
    return {
        "PreInvocation": [{"type": "command", "command": command, "timeout": 600}],
        "Stop": [{"type": "command", "command": stop_command, "timeout": 600}],
    }


def _load_config(path: Path) -> tuple[str, dict[str, Any] | None]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, ValueError):
        return "invalid", None
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers"), dict):
        return "invalid", None
    return "valid", value


def _entry_matches(entry: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return (
        entry.get("command") == expected.get("command")
        and entry.get("args") == expected.get("args")
    )
