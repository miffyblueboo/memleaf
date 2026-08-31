"""Codex CLI detection and conservative MCP configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tomllib
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
        platform: str | None = None,
        interpreter: str | Path | None = None,
    ) -> None:
        if runner is not None and command_runner is not None:
            raise ValueError("provide runner or command_runner, not both")
        home_was_explicit = home is not None or (env is not None and "HOME" in env)
        self.env = adapter_environment(env)
        effective_home = home if home is not None else self.env.get("HOME")
        self.home = adapter_home(effective_home)
        codex_home_value = self.env.get("CODEX_HOME")
        self.codex_home = (
            Path(codex_home_value).expanduser().resolve()
            if isinstance(codex_home_value, str) and codex_home_value.strip()
            else self.home / ".codex"
        )
        self.platform = os.name if platform is None else platform
        self.interpreter = interpreter if interpreter is not None else sys.executable
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
            allow_system_paths = (
                self.platform == "nt"
                or not home_was_explicit
                or self.home == login_home
            )
            self.known_paths = (
                _known_codex_paths(self.home, self.env, self.platform)
                if allow_system_paths
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
        return self.codex_home / "config.toml"

    @property
    def hooks_path(self) -> Path:
        return self.codex_home / "hooks.json"

    def detect(self) -> Detection:
        config = self.config_path
        executable = None
        explicit = self.env.get("CODEX_CLI_PATH")
        if isinstance(explicit, str) and explicit.strip():
            executable = resolve_executable(explicit.strip(), env=self.env)
            if executable is None:
                return Detection(
                    agent=self.agent,
                    detected=False,
                    confidence="low",
                    reason="CODEX_CLI_PATH does not point to an executable file",
                    executable=None,
                    config_path=str(config),
                    status="diagnostic",
                )
        if executable is None:
            # Native Windows installs commonly expose codex.exe, while the
            # official npm package exposes codex.cmd on PATH.  Accept both
            # before falling back to documented bundled/runtime locations.
            path_names = ("codex.exe", "codex.cmd", "codex") if self.platform == "nt" else ("codex",)
            for name in path_names:
                executable = resolve_executable(name, env=self.env)
                if executable is not None:
                    break
            if executable is None:
                executable = resolve_executable(
                    "codex.exe" if self.platform == "nt" else "codex",
                    env={**self.env, "PATH": ""},
                    known_paths=self.known_paths,
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
            *mcp_command(vault, interpreter=self.interpreter),
        ]
        hook_definition = _codex_hook_definition(vault, interpreter=self.interpreter)
        hook_hash = hook_definition_fingerprint(hook_definition)
        activation_status = persisted_hook_activation_status(
            vault,
            self.agent,
            hook_hash,
            _CODEX_HOOK_TRUST_STATUS,
        )
        hook_trust_status = "trusted" if activation_status == "active" else _CODEX_HOOK_TRUST_STATUS
        if dry_run:
            inline_reason = _inline_hooks_diagnostic(self.config_path)
            if inline_reason is not None:
                return result_from_detection(
                    detection,
                    status="diagnostic",
                    reason=inline_reason,
                    command=add_command,
                    dry_run=True,
                    hook_trust_status=_CODEX_HOOK_TRUST_STATUS,
                    hook_activation_status=_CODEX_HOOK_TRUST_STATUS,
                    hook_definition_hash=hook_hash,
                    user_action_required=True,
                    user_action="Move or review inline Codex hooks before installing memleaf hooks.",
                )
            hook_result = _configure_codex_hooks(
                self.hooks_path,
                vault,
                dry_run=True,
                interpreter=self.interpreter,
            )
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

        inline_reason = _inline_hooks_diagnostic(self.config_path)
        if inline_reason is not None:
            return result_from_detection(
                detection,
                status="diagnostic",
                reason=inline_reason,
                command=get_command,
                hook_trust_status=_CODEX_HOOK_TRUST_STATUS,
                hook_activation_status=_CODEX_HOOK_TRUST_STATUS,
                hook_definition_hash=hook_hash,
                user_action_required=True,
                user_action="Move or review inline Codex hooks before installing memleaf hooks.",
            )
        hook_preflight = _configure_codex_hooks(
            self.hooks_path,
            vault,
            dry_run=True,
            interpreter=self.interpreter,
        )
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
            if entry is not None and _entry_matches(
                entry,
                vault,
                interpreter=self.interpreter,
            ):
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

        hook_result = _configure_codex_hooks(
            self.hooks_path,
            vault,
            interpreter=self.interpreter,
        )
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

    def configured_vault(self, detection: Detection | None = None) -> Path | None:
        """Return the Vault from a supported existing memleaf MCP entry."""

        current = detection or self.detect()
        if not current.executable:
            return None
        command = [current.executable, "mcp", "get", "memleaf", "--json"]
        result = run_argv(self.runner, command, env=self.env)
        if command_is_missing(result):
            return None
        if result.returncode != 0:
            raise RuntimeError("could not query existing Codex memleaf MCP entry")
        entry = _entry_from_json(result.stdout)
        vault = _entry_vault(entry) if entry is not None else None
        if vault is None:
            raise RuntimeError("existing Codex memleaf MCP entry is unsupported or conflicting")
        return vault


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
    def command(event: str) -> dict[str, Any]:
        return {
            "type": "command",
            "command": host_event_command(
                "codex", event, vault, interpreter=interpreter, platform="posix"
            ),
            "commandWindows": host_event_command(
                "codex", event, vault, interpreter=interpreter, platform="nt"
            ),
        }

    session_start = {**command("SessionStart"), "timeout": 30}
    user_prompt = {**command("UserPromptSubmit"), "timeout": 600}
    pre_tool = {**command("PreToolUse"), "timeout": 30}
    post_tool = {**command("PostToolUse"), "timeout": 30}
    stop = {**command("Stop"), "timeout": 600}
    return {
        "SessionStart": [{"matcher": "compact", "hooks": [session_start]}],
        "UserPromptSubmit": [{"hooks": [user_prompt]}],
        "PreToolUse": [
            {
                "matcher": r"^mcp__memleaf__(search|read)$",
                "hooks": [pre_tool],
            }
        ],
        "PostToolUse": [
            {
                "matcher": r"^mcp__memleaf__search$",
                "hooks": [post_tool],
            }
        ],
        "Stop": [{"hooks": [stop]}],
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


def _entry_candidate(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidate: Mapping[str, Any] = entry
    transport = entry.get("transport")
    if transport is not None:
        if not isinstance(transport, Mapping) or transport.get("type") != "stdio":
            return None
        candidate = transport
    elif candidate.get("type") not in (None, "stdio"):
        return None
    return candidate


def _entry_vault(entry: Mapping[str, Any]) -> Path | None:
    candidate = _entry_candidate(entry)
    if candidate is None:
        return None
    command = candidate.get("command")
    args = candidate.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return None
    if command == "memleaf-mcp" and len(args) == 2 and args[0] == "--vault":
        raw_vault = args[1]
    elif (
        len(args) == 4
        and args[:3] == ["-m", "memleaf.mcp_server", "--vault"]
        and Path(command).expanduser().is_absolute()
    ):
        raw_vault = args[3]
    else:
        return None
    if not isinstance(raw_vault, str) or not raw_vault.strip():
        return None
    return Path(raw_vault).expanduser().resolve()


def _entry_matches(
    entry: Mapping[str, Any],
    vault: Path | str,
    *,
    interpreter: str | Path | None = None,
) -> bool:
    candidate = _entry_candidate(entry)
    if candidate is None:
        return False
    expected = mcp_command(vault, interpreter=interpreter)
    return candidate.get("command") == expected[0] and candidate.get("args") == expected[1:]


def _inline_hooks_diagnostic(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return "Codex config.toml could not be checked for inline hooks; unchanged"
    if isinstance(value.get("hooks"), Mapping):
        return "inline Codex hooks were found in config.toml; unchanged"
    return None


def _known_codex_paths(
    home: Path,
    env: Mapping[str, str],
    platform: str,
) -> tuple[Path, ...]:
    if platform != "nt":
        return (CODEX_EXECUTABLE,)
    local = env.get("LOCALAPPDATA")
    if not isinstance(local, str) or not local.strip():
        local_root = home / "AppData" / "Local"
    else:
        local_root = Path(local).expanduser()
    return (
        local_root / "Programs" / "Codex" / "codex.exe",
        local_root / "Programs" / "ChatGPT" / "resources" / "codex.exe",
        local_root / "Codex" / "bin" / "codex.exe",
        home / ".codex" / "bin" / "codex.exe",
    )
