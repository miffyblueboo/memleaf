"""Shared, dependency-free primitives for host adapters.

The adapters deliberately expose only paths, statuses, and commands in their
results.  Command output is kept private to the adapter so configuration
contents and possible secrets cannot accidentally end up in ``agents.json``.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..locking import VaultLock, atomic_write_json, read_json


@dataclass(frozen=True)
class CommandResult:
    """Small normalized representation of one argv invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class Detection:
    """Safe, serializable evidence about one supported host."""

    agent: str
    detected: bool
    confidence: str = "unknown"
    reason: str = ""
    executable: str | None = None
    config_path: str | None = None
    status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "detected": bool(self.detected),
            "confidence": self.confidence,
            "reason": self.reason,
            "executable": self.executable,
            "config_path": self.config_path,
            "status": self.status,
        }

    as_dict = to_dict


@dataclass
class ConfigureResult:
    """Safe, serializable outcome of one adapter configuration attempt."""

    agent: str
    detected: bool = False
    confidence: str = "unknown"
    reason: str = ""
    executable: str | None = None
    config_path: str | None = None
    status: str = "skipped"
    changed: bool = False
    backup_path: str | None = None
    command: list[str] | None = None
    dry_run: bool = False
    hook_trust_status: str | None = None
    hook_activation_status: str | None = None
    hook_definition_hash: str | None = None
    user_action_required: bool | None = None
    user_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "agent": self.agent,
            "detected": bool(self.detected),
            "confidence": self.confidence,
            "reason": self.reason,
            "executable": self.executable,
            "config_path": self.config_path,
            "status": self.status,
            "changed": bool(self.changed),
            "backup_path": self.backup_path,
            "command": list(self.command) if self.command is not None else None,
            "dry_run": bool(self.dry_run),
        }
        if self.hook_trust_status is not None:
            result["hook_trust_status"] = self.hook_trust_status
        if self.hook_activation_status is not None:
            result["hook_activation_status"] = self.hook_activation_status
        if self.hook_definition_hash is not None:
            result["hook_definition_hash"] = self.hook_definition_hash
        if self.user_action_required is not None:
            result["user_action_required"] = bool(self.user_action_required)
        if self.user_action is not None:
            result["user_action"] = self.user_action
        return result

    as_dict = to_dict


@dataclass(frozen=True)
class HookMergeResult:
    """Safe result from merging one host hook configuration."""

    status: str
    reason: str
    changed: bool = False
    backup_path: Path | None = None


CommandRunner = Callable[..., Any]


def adapter_home(home: Path | str | None = None) -> Path:
    """Return an absolute home path without changing the process environment."""

    return (Path(home).expanduser() if home is not None else Path.home()).resolve()


def adapter_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy an injectable environment for executable discovery and runners."""

    return dict(os.environ if env is None else env)


def resolve_executable(
    name: str,
    *,
    env: Mapping[str, str],
    known_paths: Sequence[Path | str] = (),
) -> str | None:
    """Resolve an executable from the injected PATH, then known locations."""

    path_value = env.get("PATH")
    from_path = shutil.which(name, path=path_value)
    candidates: list[Path] = []
    if from_path:
        candidates.append(Path(from_path))
    candidates.extend(Path(candidate).expanduser() for candidate in known_paths)
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            windows_launcher = os.name == "nt" and candidate.suffix.casefold() in {
                ".exe",
                ".cmd",
                ".bat",
                ".com",
            }
            if windows_launcher or os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def absolute_vault(vault: Path | str) -> str:
    """Build the one supported MCP command argument without creating a vault."""

    return str(Path(vault).expanduser().resolve())


def mcp_command(vault: Path | str) -> list[str]:
    return ["memleaf-mcp", "--vault", absolute_vault(vault)]


def hook_definition_fingerprint(definition: Mapping[str, Any]) -> str:
    """Return a stable, non-reversible identity for one host hook definition."""

    payload = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def agent_index_path(vault: Path | str) -> Path:
    """Return the agents index path without creating or changing the vault."""

    root = vault if isinstance(vault, (str, os.PathLike)) else getattr(vault, "root", vault)
    return Path(root).expanduser().resolve() / "_index" / "agents.json"


def _read_agents_index(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return None
    if not path.exists():
        return {"version": 1, "agents": {}}
    try:
        value = read_json(path)
    except (OSError, UnicodeError, TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    agents = value.get("agents")
    if agents is None:
        agents = {}
    if not isinstance(agents, dict):
        return None
    result = dict(value)
    result["agents"] = dict(agents)
    return result


def hook_activation_status(
    vault: Path | str,
    agent: str,
    definition_hash: str,
    pending_status: str,
) -> str:
    """Keep ``active`` only when it belongs to the current hook definition."""

    index = _read_agents_index(agent_index_path(vault))
    if index is None:
        return pending_status
    entry = index["agents"].get(agent)
    if not isinstance(entry, Mapping):
        return pending_status
    if (
        entry.get("hook_activation_status") == "active"
        and entry.get("hook_definition_hash") == definition_hash
    ):
        return "active"
    return pending_status


def update_agents_index(
    path: Path | str,
    updates: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Atomically merge agent entries while retaining other index data."""

    target = Path(path)
    if target.is_symlink() or target.parent.is_symlink():
        return False
    lock_path = target.parent / "vault.lock"
    try:
        with VaultLock(lock_path):
            current = _read_agents_index(target)
            if current is None:
                return False
            agents = dict(current["agents"])
            changed = False
            for agent, update in updates.items():
                if not isinstance(agent, str) or not agent or not isinstance(update, Mapping):
                    return False
                previous = agents.get(agent)
                merged = dict(previous) if isinstance(previous, Mapping) else {}
                merged.update(dict(update))
                if merged.get("hook_activation_status") == "active":
                    merged["user_action_required"] = False
                    merged.pop("user_action", None)
                if merged != previous:
                    changed = True
                agents[agent] = merged
            if not changed:
                return True
            current["agents"] = agents
            mode = target.stat().st_mode & 0o7777 if target.exists() else 0o600
            atomic_write_json(target, current, mode=mode or 0o600)
            return True
    except Exception:
        return False


def mark_hook_active(vault: Path | str, agent: str) -> bool:
    """Record a successful real hook invocation without touching hook trust."""

    target = agent_index_path(vault)
    if target.is_symlink() or target.parent.is_symlink():
        return False
    lock_path = target.parent / "vault.lock"
    try:
        with VaultLock(lock_path):
            current = _read_agents_index(target)
            if current is None:
                return False
            agents = dict(current["agents"])
            existing = agents.get(agent)
            if not isinstance(existing, Mapping):
                return False
            updated = dict(existing)
            updated["hook_activation_status"] = "active"
            updated["user_action_required"] = False
            updated.pop("user_action", None)
            if updated == existing:
                return True
            agents[agent] = updated
            current["agents"] = agents
            mode = target.stat().st_mode & 0o7777 if target.exists() else 0o600
            atomic_write_json(target, current, mode=mode or 0o600)
            return True
    except Exception:
        return False


def host_event_command(
    host: str,
    event: str,
    vault: Path | str,
    *,
    interpreter: str | Path | None = None,
) -> str:
    """Return a shell-safe hook command using the installed interpreter.

    GUI-launched hosts do not necessarily inherit the user's interactive PATH,
    so the hook must not depend on the ``memleaf`` console script being found.
    ``sys.executable`` is kept as-is (including a venv symlink) and every
    argument is quoted for the host's command runner.
    """

    value = str(interpreter if interpreter is not None else sys.executable)
    executable = Path(value).expanduser()
    if not executable.is_absolute():
        executable = Path.cwd() / executable
    args = (
        str(executable),
        "-m",
        "memleaf.cli",
        "host-event",
        host,
        event,
        "--vault",
        absolute_vault(vault),
    )
    return " ".join(shlex.quote(argument) for argument in args)


def run_argv(
    runner: CommandRunner | None,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    input_text: str | None = None,
) -> CommandResult:
    """Call an injected runner or subprocess with an argv list only.

    A few small runner signatures are accepted to keep adapters convenient to
    test: ``runner(argv, env=env)``, ``runner(argv, env)``, and ``runner(argv)``.
    Runners that explicitly accept ``input_text`` can inspect the optional
    stdin payload as well.  No fallback invokes a shell or retries a callable.
    """

    command = list(argv)

    # Keep the injected-runner contract backwards compatible: only a runner
    # that explicitly exposes input_text receives it.  The real subprocess
    # path is the only default that needs to create a pipe for stdin.
    if runner is None:
        return normalize_command_result(
            _subprocess_runner(command, env=env, input_text=input_text)
        )

    actual_runner = runner
    call_args: tuple[Any, ...] = (command,)
    call_kwargs: dict[str, Any] = {"env": env}
    try:
        signature = inspect.signature(actual_runner)
    except (TypeError, ValueError):
        # If a callable does not expose a signature, use one convention only;
        # a TypeError raised by its body must never cause a second invocation.
        value = actual_runner(command, env=env)
    else:
        candidates = []
        if input_text is not None:
            candidates.append(((command,), {"env": env, "input_text": input_text}))
        candidates.extend(
            (
                ((command,), {"env": env}),
                ((command, env), {}),
                ((command,), {}),
            )
        )
        for candidate_args, candidate_kwargs in candidates:
            try:
                signature.bind(*candidate_args, **candidate_kwargs)
            except TypeError:
                continue
            call_args = candidate_args
            call_kwargs = candidate_kwargs
            break
        value = actual_runner(*call_args, **call_kwargs)
    return normalize_command_result(value)


def _subprocess_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    input_text: str | None = None,
) -> Any:
    # Host commands are always argv lists; no shell is involved.
    options: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "text": True,
        "env": dict(env),
    }
    if input_text is not None:
        options["input"] = input_text
    return subprocess.run(list(argv), **options)


def normalize_command_result(value: Any) -> CommandResult:
    """Normalize common fake-runner and ``subprocess`` return shapes."""

    if isinstance(value, CommandResult):
        return value
    if hasattr(value, "returncode"):
        return CommandResult(
            _as_returncode(getattr(value, "returncode", 1)),
            _as_text(getattr(value, "stdout", "")),
            _as_text(getattr(value, "stderr", "")),
        )
    if isinstance(value, Mapping):
        return CommandResult(
            _as_returncode(value.get("returncode", value.get("code", 1))),
            _as_text(value.get("stdout", "")),
            _as_text(value.get("stderr", "")),
        )
    if isinstance(value, (tuple, list)):
        values = list(value)
        return CommandResult(
            _as_returncode(values[0] if values else 1),
            _as_text(values[1] if len(values) > 1 else ""),
            _as_text(values[2] if len(values) > 2 else ""),
        )
    if isinstance(value, int):
        return CommandResult(value)
    return CommandResult(1)


def _as_returncode(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def command_is_missing(result: CommandResult) -> bool:
    """Recognize explicit not-found responses without exposing their text."""

    if result.returncode == 0:
        return False
    text = f"{result.stdout} {result.stderr}".lower()
    markers = (
        "not found",
        "not configured",
        "does not exist",
        "no such",
        "unknown mcp",
        "unknown server",
        "server not found",
        "no mcp server",
    )
    return any(marker in text for marker in markers)


def make_backup(path: Path | str) -> Path | None:
    """Create a same-directory timestamped backup, refusing symlinks."""

    source = Path(path)
    if not source.exists():
        return None
    if source.is_symlink() or not source.is_file():
        raise OSError("unsafe configuration path")
    mode = source.stat().st_mode & 0o7777
    timestamp = _datetime.datetime.now(_datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    for _ in range(8):
        backup = source.with_name(
            f"{source.name}.memleaf.bak.{timestamp}.{uuid.uuid4().hex[:10]}"
        )
        if backup.exists() or backup.is_symlink():
            continue
        shutil.copy2(source, backup)
        try:
            os.chmod(backup, mode)
        except OSError:
            pass
        _fsync_file(backup)
        _fsync_directory(backup.parent)
        return backup
    raise OSError("could not create configuration backup")


def atomic_replace_bytes(path: Path | str, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace one regular file with fsync and a same-dir temp."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def merge_hook_config(
    path: Path | str,
    additions: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    container_key: str | None = None,
    dry_run: bool = False,
) -> HookMergeResult:
    """Merge command handlers into a host hook JSON file.

    ``container_key`` is used by Antigravity's named-hook format.  Codex uses
    a top-level ``hooks`` object and passes ``container_key="hooks"``.
    Existing handlers are left untouched; a matching memleaf command is
    idempotent and a different memleaf command is treated as a conflict.
    """

    target = Path(path)
    if target.is_symlink():
        return HookMergeResult("diagnostic", "hook configuration is a symlink; unchanged")
    # Check the host-specific directory levels without rejecting normal
    # system path aliases such as macOS's /var -> /private/var.
    if any(parent.is_symlink() for parent in (target.parent, target.parent.parent)):
        return HookMergeResult("diagnostic", "hook configuration parent is a symlink; unchanged")
    if target.exists() and not target.is_file():
        return HookMergeResult("diagnostic", "hook configuration is not a regular file; unchanged")

    if target.exists():
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return HookMergeResult("diagnostic", "hook configuration is invalid JSON; unchanged")
        if not isinstance(value, dict):
            return HookMergeResult("diagnostic", "hook configuration is not a JSON object; unchanged")
        document: dict[str, Any] = dict(value)
    else:
        document = {}

    if container_key is None:
        container: dict[str, Any] = document
    else:
        current = document.get(container_key)
        if current is None:
            current = {}
        if not isinstance(current, Mapping):
            return HookMergeResult("diagnostic", "hook event container is not a JSON object; unchanged")
        container = dict(current)
        if container_key != "hooks" and container.get("enabled") is False:
            return HookMergeResult("diagnostic", "existing memleaf hook is disabled; unchanged")

    updated = False
    for event, handlers in additions.items():
        if not isinstance(event, str) or not event or not isinstance(handlers, Sequence):
            return HookMergeResult("diagnostic", "invalid hook definition; unchanged")
        existing = container.get(event)
        if existing is None:
            existing_items: list[Any] = []
        elif isinstance(existing, list):
            existing_items = list(existing)
        else:
            return HookMergeResult("diagnostic", "hook event is not an array; unchanged")

        for handler in handlers:
            if not isinstance(handler, Mapping):
                return HookMergeResult("diagnostic", "invalid hook handler; unchanged")
            requested = handler.get("hooks") if container_key == "hooks" else [handler]
            if not isinstance(requested, list) or not requested:
                return HookMergeResult("diagnostic", "invalid hook handler; unchanged")
            group_has_new = False
            for requested_handler in requested:
                if not isinstance(requested_handler, Mapping):
                    return HookMergeResult("diagnostic", "invalid hook handler; unchanged")
                command = requested_handler.get("command")
                if not isinstance(command, str) or not command:
                    return HookMergeResult("diagnostic", "hook command is invalid; unchanged")
                matching, conflict = _hook_command_state(existing_items, command, event, container_key)
                if conflict:
                    return HookMergeResult("diagnostic", "existing memleaf hook conflicts; unchanged")
                if not matching:
                    group_has_new = True
            if group_has_new:
                existing_items.append(dict(handler))
                updated = True
        container[event] = existing_items

    if not updated:
        return HookMergeResult("already_configured", "memleaf hooks are already configured")
    if container_key is not None:
        document[container_key] = container
    if dry_run:
        return HookMergeResult("would_configure", "would merge memleaf lifecycle hooks")

    backup: Path | None = None
    try:
        backup = make_backup(target)
        payload = (
            json.dumps(document, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n"
        ).encode("utf-8")
        mode = target.stat().st_mode & 0o7777 if target.exists() else 0o600
        atomic_replace_bytes(target, payload, mode=mode or 0o600)
    except Exception:
        return HookMergeResult("failure", "hook configuration update failed; backup retained", backup_path=backup)
    return HookMergeResult(
        "configured",
        "memleaf lifecycle hooks configured",
        changed=True,
        backup_path=backup,
    )


def _hook_command_state(
    existing_items: Sequence[Any],
    command: str,
    event: str,
    container_key: str | None,
) -> tuple[bool, bool]:
    """Return ``(matching, conflict)`` for one requested command."""

    requested_identity = _host_event_identity(command)
    for item in existing_items:
        handlers: Any = item.get("hooks") if isinstance(item, Mapping) and container_key == "hooks" else [item]
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            if not isinstance(handler, Mapping):
                continue
            current = handler.get("command")
            if not isinstance(current, str):
                continue
            if current == command:
                return True, False
            if requested_identity is not None and _host_event_identity(current) == requested_identity:
                return False, True
    return False, False


def _host_event_identity(command: str) -> tuple[str, str] | None:
    """Recognize old and current memleaf host-event command forms."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token != "host-event" or index < 1 or index + 2 >= len(tokens):
            continue
        old_form = tokens[index - 1] == "memleaf"
        module_form = index >= 2 and tokens[index - 1] == "memleaf.cli" and tokens[index - 2] == "-m"
        if old_form or module_form:
            host, hook_event = tokens[index + 1], tokens[index + 2]
            if host in ("codex", "antigravity") and hook_event:
                return host, hook_event
    return None


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def result_from_detection(
    detection: Detection,
    *,
    status: str,
    reason: str,
    changed: bool = False,
    backup_path: Path | str | None = None,
    command: Sequence[str] | None = None,
    dry_run: bool = False,
    hook_trust_status: str | None = None,
    hook_activation_status: str | None = None,
    hook_definition_hash: str | None = None,
    user_action_required: bool | None = None,
    user_action: str | None = None,
) -> ConfigureResult:
    return ConfigureResult(
        agent=detection.agent,
        detected=detection.detected,
        confidence=detection.confidence,
        reason=reason,
        executable=detection.executable,
        config_path=detection.config_path,
        status=status,
        changed=changed,
        backup_path=str(backup_path) if backup_path is not None else None,
        command=list(command) if command is not None else None,
        dry_run=dry_run,
        hook_trust_status=hook_trust_status,
        hook_activation_status=hook_activation_status,
        hook_definition_hash=hook_definition_hash,
        user_action_required=user_action_required,
        user_action=user_action,
    )
