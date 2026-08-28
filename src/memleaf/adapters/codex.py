"""Codex CLI detection and conservative MCP configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import pwd
except ImportError:  # pragma: no cover - Windows has no POSIX passwd DB.
    pwd = None

from .base import (
    CommandRunner,
    ConfigureResult,
    Detection,
    HookMergeResult,
    adapter_environment,
    adapter_home,
    absolute_vault,
    command_is_missing,
    host_event_command,
    hook_activation_status as persisted_hook_activation_status,
    hook_definition_fingerprint,
    make_backup,
    merge_hook_config,
    mcp_command,
    resolve_executable,
    result_from_detection,
    run_argv,
)


CODEX_EXECUTABLE = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
_CODEX_HOOK_TRUST_STATUS = "pending_user_review"
_CODEX_HOOK_USER_ACTION = "Open Codex and run /hooks to review and trust the memleaf hooks."


class CodexAdapter:
    """Use the official Codex CLI for user-level MCP configuration.

    The adapter never edits TOML itself.  It asks ``codex mcp get`` what is
    already configured, and only then invokes the documented ``mcp add``
    command for a missing entry.
    """

    agent = "codex"

    def __init__(
        self,
        home: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
        *,
        command_runner: CommandRunner | None = None,
        path: str | Sequence[str] | None = None,
        known_paths: Sequence[Path | str] | None = None,
    ) -> None:
        if runner is not None and command_runner is not None:
            raise ValueError("provide runner or command_runner, not both")
        home_was_explicit = home is not None or (env is not None and "HOME" in env)
        self.env = adapter_environment(env)
        effective_home = home if home is not None else self.env.get("HOME")
        self.home = adapter_home(effective_home)
        if known_paths is None:
            # An explicit home marks an injected/sandbox boundary.  Do not let
            # a system installation escape that boundary when init is tested
            # with an isolated HOME.  The default adapter may still inspect
            # the documented application path.
            try:
                if pwd is None:
                    raise OSError
                login_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
            except (KeyError, OSError, RuntimeError):
                login_home = None
            self.known_paths = (
                (CODEX_EXECUTABLE,)
                if not home_was_explicit or self.home == login_home
                else ()
            )
        else:
            self.known_paths = tuple(known_paths)
        if path is not None:
            self.env["PATH"] = (
                path if isinstance(path, str) else os.pathsep.join(path)
            )
        self.runner = runner or command_runner

    @property
    def config_path(self) -> Path:
        return self.home / ".codex" / "config.toml"

    @property
    def hooks_path(self) -> Path:
        return self.home / ".codex" / "hooks.json"

    def detect(self) -> Detection:
        config = self.config_path
        executable = resolve_executable(
            "codex", env=self.env, known_paths=self.known_paths
        )
        config_value = str(config)
        if executable is not None:
            return Detection(
                agent=self.agent,
                detected=True,
                confidence="high",
                reason="executable found",
                executable=executable,
                config_path=config_value,
                status="detected",
            )
        if config.is_file() and not config.is_symlink():
            return Detection(
                agent=self.agent,
                detected=True,
                confidence="medium",
                reason="user configuration found but executable is unavailable",
                executable=None,
                config_path=config_value,
                status="diagnostic",
            )
        if config.is_symlink():
            return Detection(
                agent=self.agent,
                detected=False,
                confidence="low",
                reason="configuration path is a symlink; refusing automatic access",
                executable=None,
                config_path=config_value,
                status="diagnostic",
            )
        return Detection(
            agent=self.agent,
            detected=False,
            confidence="none",
            reason="executable and user configuration were not found",
            executable=None,
            config_path=config_value,
            status="not_detected",
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
                    "host was not reliably detected; no executable path guessed"
                    if attempt
                    else detection.reason
                ),
                dry_run=dry_run,
            )
        if not detection.executable:
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="detected host has no executable path",
                dry_run=dry_run,
            )

        executable = detection.executable
        get_command = [executable, "mcp", "get", "memleaf", "--json"]
        add_command = [
            executable,
            "mcp",
            "add",
            "memleaf",
            "--",
            *mcp_command(vault),
        ]
        hook_definition = _codex_hook_definition(vault)
        hook_hash = hook_definition_fingerprint(hook_definition)
        activation_status = persisted_hook_activation_status(
            vault,
            self.agent,
            hook_hash,
            _CODEX_HOOK_TRUST_STATUS,
        )
        hook_trust_status = "trusted" if activation_status == "active" else _CODEX_HOOK_TRUST_STATUS
        if dry_run:
            hook_result = _configure_codex_hooks(self.hooks_path, vault, dry_run=True)
            if hook_result.status == "diagnostic":
                return result_from_detection(
                    detection,
                    status="diagnostic",
                    reason=hook_result.reason,
                    command=add_command,
                    dry_run=True,
                    hook_trust_status=_CODEX_HOOK_TRUST_STATUS,
                    hook_activation_status=_CODEX_HOOK_TRUST_STATUS,
                    hook_definition_hash=hook_hash,
                    user_action_required=True,
                    user_action=_CODEX_HOOK_USER_ACTION,
                )
            return result_from_detection(
                detection,
                status="would_configure",
                reason="would configure MCP entry and lifecycle hooks",
                command=add_command,
                dry_run=True,
                hook_trust_status=hook_trust_status,
                hook_activation_status=activation_status,
                hook_definition_hash=hook_hash,
                user_action_required=activation_status != "active",
                user_action=_CODEX_HOOK_USER_ACTION if activation_status != "active" else None,
            )

        hook_preflight = _configure_codex_hooks(self.hooks_path, vault, dry_run=True)
        if hook_preflight.status == "diagnostic":
            return result_from_detection(
                detection,
                status="diagnostic",
                reason=hook_preflight.reason,
                command=get_command,
                hook_trust_status=_CODEX_HOOK_TRUST_STATUS,
                hook_activation_status=_CODEX_HOOK_TRUST_STATUS,
                hook_definition_hash=hook_hash,
                user_action_required=True,
                user_action=_CODEX_HOOK_USER_ACTION,
            )

        config = Path(detection.config_path) if detection.config_path else self.config_path
        if config.is_symlink():
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="configuration path is a symlink; unchanged",
                command=get_command,
            )
        if config.exists() and not config.is_file():
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="configuration path is not a regular file; unchanged",
                command=get_command,
            )

        try:
            queried = run_argv(self.runner, get_command, env=self.env)
        except Exception:
            return result_from_detection(
                detection,
                status="failure",
                reason="could not query existing MCP entry",
                command=get_command,
            )
        if queried.returncode == 0:
            entry = _entry_from_json(queried.stdout)
            if entry is not None and _entry_matches(entry, vault):
                mcp_changed = False
                backup = None
                mcp_reason = "existing memleaf entry is correct"
            else:
                return result_from_detection(
                    detection,
                    status="diagnostic",
                    reason="existing memleaf entry is unknown or conflicting; unchanged",
                    command=get_command,
                )
        elif command_is_missing(queried):
            backup = None
            try:
                backup = make_backup(config)
            except Exception:
                return result_from_detection(
                    detection,
                    status="failure",
                    reason="could not create configuration backup; unchanged",
                    command=add_command,
                )
            try:
                added = run_argv(self.runner, add_command, env=self.env)
            except Exception:
                return result_from_detection(
                    detection,
                    status="failure",
                    reason="official CLI add failed; backup retained",
                    backup_path=backup,
                    command=add_command,
                )
            if added.returncode != 0:
                return result_from_detection(
                    detection,
                    status="failure",
                    reason="official CLI add failed; backup retained",
                    backup_path=backup,
                    command=add_command,
                )
            mcp_changed = True
            mcp_reason = "MCP entry added by official CLI"
        else:
            return result_from_detection(
                detection,
                status="failure",
                reason="could not establish whether memleaf is already configured",
                command=get_command,
            )

        hook_result = _configure_codex_hooks(self.hooks_path, vault)
        if hook_result.status in ("diagnostic", "failure"):
            return result_from_detection(
                detection,
                status=hook_result.status,
                reason=hook_result.reason,
                changed=mcp_changed,
                backup_path=hook_result.backup_path or backup,
                command=add_command,
                hook_trust_status=_CODEX_HOOK_TRUST_STATUS,
                hook_activation_status=_CODEX_HOOK_TRUST_STATUS,
                hook_definition_hash=hook_hash,
                user_action_required=True,
                user_action=_CODEX_HOOK_USER_ACTION,
            )
        return result_from_detection(
            detection,
            # Preserve the adapter's historical status for an existing MCP
            # entry; ``changed`` still reports that lifecycle hooks were added.
            status="configured" if mcp_changed else "already_configured",
            reason=(
                f"{mcp_reason}; lifecycle hooks configured"
                if mcp_changed and hook_result.changed
                else "lifecycle hooks configured"
                if hook_result.changed
                else mcp_reason
            ),
            changed=mcp_changed or hook_result.changed,
            backup_path=backup or hook_result.backup_path,
            command=add_command,
            hook_trust_status=hook_trust_status,
            hook_activation_status=activation_status,
            hook_definition_hash=hook_hash,
            user_action_required=activation_status != "active",
            user_action=_CODEX_HOOK_USER_ACTION if activation_status != "active" else None,
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


Codex = CodexAdapter


def _configure_codex_hooks(
    path: Path,
    vault: Path | str,
    *,
    dry_run: bool = False,
    interpreter: str | Path | None = None,
) -> HookMergeResult:
    definition = _codex_hook_definition(vault, interpreter=interpreter)
    return merge_hook_config(
        path,
        definition,
        container_key="hooks",
        dry_run=dry_run,
    )


def _codex_hook_definition(
    vault: Path | str,
    *,
    interpreter: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    command = host_event_command("codex", "UserPromptSubmit", vault, interpreter=interpreter)
    pre_tool_command = host_event_command("codex", "PreToolUse", vault, interpreter=interpreter)
    post_tool_command = host_event_command("codex", "PostToolUse", vault, interpreter=interpreter)
    stop_command = host_event_command("codex", "Stop", vault, interpreter=interpreter)
    return {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command, "timeout": 600}]}],
        "PreToolUse": [
            {
                "matcher": r"^mcp__memleaf__(search|read)$",
                "hooks": [{"type": "command", "command": pre_tool_command, "timeout": 30}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": r"^mcp__memleaf__search$",
                "hooks": [{"type": "command", "command": post_tool_command, "timeout": 30}],
            }
        ],
        "Stop": [{"hooks": [{"type": "command", "command": stop_command, "timeout": 600}]}],
    }


def _entry_from_json(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return _find_named_entry(value, "memleaf")


def _find_named_entry(value: Any, name: str) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        if "command" in value and "args" in value:
            return dict(value)
        direct = value.get(name)
        if isinstance(direct, Mapping):
            return dict(direct)
        if value.get("name") == name:
            return dict(value)
        for key in ("mcpServers", "mcp_servers", "servers", "server", "mcp"):
            nested = value.get(key)
            if isinstance(nested, Mapping):
                found = _find_named_entry(nested, name)
                if found is not None:
                    return found
        for nested in value.values():
            if isinstance(nested, Mapping):
                if nested.get("name") == name:
                    return dict(nested)
                if "command" in nested and "args" in nested:
                    return dict(nested)
    elif isinstance(value, list):
        for nested in value:
            found = _find_named_entry(nested, name)
            if found is not None:
                return found
    return None


def _entry_matches(entry: Mapping[str, Any], vault: Path | str) -> bool:
    candidate: Mapping[str, Any] = entry
    transport = entry.get("transport")
    if transport is not None:
        if not isinstance(transport, Mapping) or transport.get("type") != "stdio":
            return False
        candidate = transport
    elif candidate.get("type") not in (None, "stdio"):
        return False
    return candidate.get("command") == "memleaf-mcp" and candidate.get("args") == [
        "--vault",
        str(Path(vault).expanduser().resolve()),
    ]
