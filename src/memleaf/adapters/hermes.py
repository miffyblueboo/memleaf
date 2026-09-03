"""Hermes CLI detection and conservative MCP configuration."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .base import (
    CommandRunner,
    ConfigureResult,
    Detection,
    adapter_environment,
    adapter_home,
    command_is_missing,
    make_backup,
    resolve_executable,
    result_from_detection,
    run_argv,
)


_MCP_IDLE_TIMEOUT_SECONDS = 60
MCP_EXPECTED_TOOL_COUNT = 12
_MCP_EXPECTED_TOOL_COUNT = MCP_EXPECTED_TOOL_COUNT


def hermes_home_for_platform(
    home: Path,
    env: Mapping[str, str],
    *,
    platform: str | None = None,
) -> Path:
    """Resolve Hermes' data/config home using the host's official defaults."""

    raw = env.get("HERMES_HOME")
    if raw:
        configured = Path(raw).expanduser()
        if not configured.is_absolute():
            configured = home / configured
        return configured.resolve()

    effective_platform = os.name if platform is None else platform
    if effective_platform == "nt":
        local_appdata = env.get("LOCALAPPDATA")
        if local_appdata:
            return (Path(local_appdata).expanduser() / "hermes").resolve()
    return (home / ".hermes").resolve()


def hermes_known_executables(
    home: Path,
    hermes_home: Path,
    *,
    platform: str | None = None,
) -> tuple[Path, ...]:
    """Return official Hermes launcher locations in preference order."""

    effective_platform = os.name if platform is None else platform
    if effective_platform == "nt":
        return (
            hermes_home / "bin" / "hermes.exe",
            hermes_home / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
            hermes_home / "bin" / "hermes.cmd",
        )
    return (home / ".local" / "bin" / "hermes",)


class HermesAdapter:
    """Use Hermes' CLI and read-only diagnostics instead of rewriting YAML."""

    agent = "hermes"

    def __init__(
        self,
        home: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
        *,
        command_runner: CommandRunner | None = None,
        path: str | Sequence[str] | None = None,
        memleaf_command: Path | str | None = None,
        hermes_home: Path | str | None = None,
        platform: str | None = None,
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
        self.runner = runner or command_runner
        self.platform = os.name if platform is None else platform
        if hermes_home is None:
            self.hermes_home = hermes_home_for_platform(
                self.home, self.env, platform=self.platform
            )
        else:
            configured_home = Path(hermes_home).expanduser()
            if not configured_home.is_absolute():
                configured_home = self.home / configured_home
            self.hermes_home = configured_home.resolve()
        if memleaf_command is None:
            self.memleaf_command = None
        else:
            configured_command = Path(memleaf_command).expanduser()
            if not configured_command.is_absolute():
                configured_command = Path.cwd() / configured_command
            self.memleaf_command = str(configured_command)

    @property
    def config_path(self) -> Path:
        return self.hermes_home / "config.yaml"

    @property
    def known_executable(self) -> Path:
        """Backward-compatible first known launcher path."""

        return self.known_executables[0]

    @property
    def known_executables(self) -> tuple[Path, ...]:
        return hermes_known_executables(
            self.home, self.hermes_home, platform=self.platform
        )

    def detect(self) -> Detection:
        config = self.config_path
        executable = resolve_executable(
            "hermes", env=self.env, known_paths=self.known_executables
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
        list_command = [executable, "mcp", "list"]
        configured_command = self.memleaf_command or "memleaf-mcp"
        add_command = [
            executable,
            "mcp",
            "add",
            "memleaf",
            "--command",
            configured_command,
            "--args",
            "--vault",
            str(Path(vault).expanduser().resolve()),
        ]
        if dry_run:
            return result_from_detection(
                detection,
                status="would_configure",
                reason="would query existing entry and invoke official CLI add",
                command=add_command,
                dry_run=True,
            )

        config = Path(detection.config_path) if detection.config_path else self.config_path
        if config.is_symlink():
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="configuration path is a symlink; unchanged",
                command=list_command,
            )
        if config.exists() and not config.is_file():
            return result_from_detection(
                detection,
                status="diagnostic",
                reason="configuration path is not a regular file; unchanged",
                command=list_command,
            )

        try:
            listed = run_argv(self.runner, list_command, env=self.env)
        except Exception:
            return result_from_detection(
                detection,
                status="failure",
                reason="could not query existing MCP entries",
                command=list_command,
            )

        state = _state_from_listing(listed.stdout, vault, configured_command)
        if listed.returncode == 0:
            if state == "correct":
                return result_from_detection(
                    detection,
                    status="already_configured",
                    reason="existing memleaf entry is correct",
                    command=list_command,
                )
            config_state = _state_from_config(config, vault, configured_command)
            if state == "conflict" and config_state == "correct":
                return result_from_detection(
                    detection,
                    status="already_configured",
                    reason="existing memleaf entry is correct",
                    command=list_command,
                )
            if config_state == "correct":
                return result_from_detection(
                    detection,
                    status="already_configured",
                    reason="existing memleaf entry is correct",
                    command=list_command,
                )
            if config_state in ("conflict", "unknown") and not self._can_reconfigure_existing_entry(config, vault):
                return result_from_detection(
                    detection,
                    status="diagnostic",
                    reason="existing configuration is unknown or conflicting; unchanged",
                    command=list_command,
                )
        elif not command_is_missing(listed):
            return result_from_detection(
                detection,
                status="failure",
                reason="could not establish whether memleaf is already configured",
                command=list_command,
            )
        else:
            config_state = _state_from_config(config, vault, configured_command)
            if config_state == "correct":
                return result_from_detection(
                    detection,
                    status="already_configured",
                    reason="existing memleaf entry is correct",
                    command=list_command,
                )
            if config_state in ("conflict", "unknown") and not self._can_reconfigure_existing_entry(config, vault):
                return result_from_detection(
                    detection,
                    status="diagnostic",
                    reason="existing configuration is unknown or conflicting; unchanged",
                    command=list_command,
                )

        backup: Path | None = None
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
            # Hermes prompts after discovery; an empty line means "enable all"
            # for this one official CLI invocation.  No other host receives
            # stdin input here, and the payload is never logged or returned.
            add_input = "y\n\n" if self._can_reconfigure_existing_entry(config, vault) else "\n"
            added = run_argv(
                self.runner,
                add_command,
                env=self.env,
                input_text=add_input,
            )
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
        try:
            postcheck_list = run_argv(self.runner, list_command, env=self.env)
        except Exception:
            postcheck_list = None
        confirmed = bool(
            postcheck_list is not None
            and postcheck_list.returncode == 0
            and _state_from_listing(postcheck_list.stdout, vault, configured_command) == "correct"
        )
        if not confirmed:
            confirmed = _state_from_config(config, vault, configured_command) == "correct"
        if not confirmed:
            return result_from_detection(
                detection,
                status="failure",
                reason="official CLI add returned success but configuration was not confirmed; backup retained",
                backup_path=backup,
                command=add_command,
            )
        return result_from_detection(
            detection,
            status="configured",
            reason="MCP entry added by official CLI",
            changed=True,
            backup_path=backup,
            command=add_command,
        )

    def configure_mcp_lifecycle(
        self,
        detection: Detection | None = None,
        *,
        idle_timeout_seconds: int = _MCP_IDLE_TIMEOUT_SECONDS,
        dry_run: bool = False,
    ) -> bool:
        """Set Hermes' supported lazy/recycle options through its CLI."""

        detection = detection or self.detect()
        executable = detection.executable if detection.detected else None
        if detection.confidence != "high" or not executable:
            return False
        if isinstance(idle_timeout_seconds, bool) or idle_timeout_seconds < 1:
            return False
        if dry_run:
            return True
        commands = (
            [executable, "config", "set", "mcp_servers.memleaf.lazy", "true"],
            [
                executable,
                "config",
                "set",
                "mcp_servers.memleaf.idle_timeout_seconds",
                str(idle_timeout_seconds),
            ],
        )
        for command in commands:
            try:
                result = run_argv(self.runner, command, env=self.env)
            except Exception:
                return False
            if result.returncode != 0:
                return False
        return True

    def test_mcp(
        self,
        detection: Detection | None = None,
        *,
        expected_tools: int = MCP_EXPECTED_TOOL_COUNT,
    ) -> bool:
        """Run the official MCP test and require the expected tool count."""

        detection = detection or self.detect()
        executable = detection.executable if detection.detected else None
        if detection.confidence != "high" or not executable:
            return False
        if isinstance(expected_tools, bool) or expected_tools < 0:
            return False
        try:
            result = run_argv(
                self.runner,
                [executable, "mcp", "test", "memleaf"],
                env=self.env,
            )
        except Exception:
            return False
        return result.returncode == 0 and _mcp_test_confirms_tools(
            result.stdout,
            expected_tools,
        )

    def _can_reconfigure_existing_entry(self, path: Path, vault: Path | str) -> bool:
        """Allow only a known old memleaf entry for this exact vault to update."""

        if self.memleaf_command is None or not path.exists() or path.is_symlink() or not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        parsed = _parse_json_or_yaml_mcp(text)
        if not isinstance(parsed, Mapping):
            return False
        entry = parsed.get("memleaf")
        if not isinstance(entry, Mapping):
            return False
        if set(entry) - {"command", "args", "enabled"}:
            return False
        if "enabled" in entry and not _is_boolish_true(entry["enabled"]):
            return False
        command = entry.get("command")
        if not isinstance(command, str) or not _is_known_memleaf_command(
            command,
            home=self.home,
            configured=self.memleaf_command,
        ):
            return False
        return entry.get("args") == [
            "--vault",
            str(Path(vault).expanduser().resolve()),
        ]

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


Hermes = HermesAdapter


def _state_from_listing(
    text: str,
    vault: Path | str,
    command: str = "memleaf-mcp",
) -> str:
    """Classify Hermes' human-readable list output without logging it."""

    if not _contains_memleaf_name(text):
        return "absent"
    if command in text and "--vault" in text and str(
        Path(vault).expanduser().resolve()
    ) in text:
        return "correct"
    return "conflict"


def _contains_memleaf_name(text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_.-])memleaf(?![A-Za-z0-9_.-])", text) is not None


def _is_known_memleaf_command(
    command: str,
    *,
    home: Path,
    configured: str | None,
) -> bool:
    if command == "memleaf-mcp":
        return True
    try:
        candidate = Path(command).expanduser()
    except (TypeError, ValueError):
        return False
    if not candidate.is_absolute():
        return False
    known = {(home / ".local" / "bin" / "memleaf-mcp").resolve()}
    if configured is not None:
        known.add(Path(configured).expanduser().resolve())
    return candidate.resolve() in known


def _is_boolish_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "on"}


def _state_from_config(
    path: Path,
    vault: Path | str,
    command: str = "memleaf-mcp",
) -> str:
    if not path.exists():
        return "absent"
    if path.is_symlink() or not path.is_file():
        return "unknown"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "unknown"
    parsed = _parse_json_or_yaml_mcp(text)
    if parsed is None:
        return "unknown"
    entry = parsed.get("memleaf")
    if entry is None:
        return "absent"
    if not isinstance(entry, Mapping):
        return "conflict"
    if _entry_matches(entry, vault, command):
        return "correct"
    return "conflict"


def _parse_json_or_yaml_mcp(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return _parse_yaml_mcp_servers(text)
    if not isinstance(value, Mapping):
        return None
    servers = value.get("mcp_servers")
    if not isinstance(servers, Mapping):
        return None
    return dict(servers)


def _parse_yaml_mcp_servers(text: str) -> dict[str, Any] | None:
    lines = text.lstrip("\ufeff").splitlines()
    root_index: int | None = None
    root_indent = 0
    root_value = ""
    for index, raw in enumerate(lines):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            key, value = _yaml_key_value(raw.strip())
            if key == "mcp_servers":
                root_index = index
                root_indent = indent
                root_value = value
                break
    if root_index is None:
        return {}
    if root_value:
        parsed = _parse_scalar(root_value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
        if parsed in ({}, None):
            return {}
        return None

    child_indent: int | None = None
    entry_lines: list[str] = []
    found = False
    for raw in lines[root_index + 1 :]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= root_indent:
            break
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            if found:
                entry_lines.append(raw)
            continue
        key, value = _yaml_key_value(raw.strip())
        if key == "memleaf":
            found = True
            if value:
                parsed = _parse_scalar(value)
                if isinstance(parsed, Mapping):
                    return {"memleaf": dict(parsed)}
                return {"memleaf": None}
            entry_lines = []
        elif found:
            break
    if not found:
        return {}
    entry = _parse_yaml_entry(entry_lines, child_indent or 0)
    return {"memleaf": entry}


def _parse_yaml_entry(lines: list[str], child_indent: int) -> dict[str, Any] | None:
    command: Any = None
    args: Any = None
    entry: dict[str, Any] = {}
    entry_indent = child_indent + 2
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip() or raw.lstrip().startswith("#"):
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= child_indent:
            break
        if indent != entry_indent:
            index += 1
            continue
        key, value = _yaml_key_value(raw.strip())
        if key == "command":
            command = _parse_scalar(value)
        elif key == "args":
            if value:
                args = _parse_scalar(value)
            else:
                values: list[Any] = []
                next_index = index + 1
                while next_index < len(lines):
                    nested = lines[next_index]
                    if not nested.strip() or nested.lstrip().startswith("#"):
                        next_index += 1
                        continue
                    nested_indent = len(nested) - len(nested.lstrip(" "))
                    if nested_indent <= indent:
                        break
                    stripped = nested.strip()
                    if not stripped.startswith("-"):
                        break
                    values.append(_parse_scalar(stripped[1:].strip()))
                    next_index += 1
                args = values
                index = next_index - 1
        else:
            entry[key] = _parse_scalar(value) if value else {}
        index += 1
    if not isinstance(command, str) or not isinstance(args, list):
        return None
    entry.update({"command": command, "args": args})
    return entry


def _yaml_key_value(value: str) -> tuple[str, str]:
    match = re.match(r"^([^:]+):(?:\s*(.*))?$", value)
    if match is None:
        return value.strip(), ""
    return match.group(1).strip().strip("'\""), (match.group(2) or "").strip()


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if value in ("null", "Null", "NULL", "~"):
        return None
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        if not body:
            return []
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return parsed
        except (SyntaxError, ValueError):
            pass
        return [_parse_scalar(part) for part in _split_flow_items(body)]
    if value.startswith("{") and value.endswith("}"):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, dict):
                return parsed
        except (SyntaxError, ValueError):
            return None
    return value


def _split_flow_items(value: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for character in value:
        if character in ("'", '"'):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        if character == "," and quote is None:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if current:
        items.append("".join(current).strip())
    return items


def _entry_matches(
    entry: Mapping[str, Any],
    vault: Path | str,
    command: str = "memleaf-mcp",
) -> bool:
    return entry.get("command") == command and entry.get("args") == [
        "--vault",
        str(Path(vault).expanduser().resolve()),
    ]


def _mcp_test_confirms_tools(text: str, expected_tools: int) -> bool:
    """Recognize Hermes' human MCP test summary without returning its output."""

    normalized = text.casefold()
    count = re.escape(str(expected_tools))
    return bool(
        re.search(rf"\btools?\s+discovered\s*:\s*{count}\b", normalized)
        or re.search(rf"\b{count}\s+tools?\b", normalized)
    )
