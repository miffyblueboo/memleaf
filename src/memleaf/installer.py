"""Explicit, conservative host installers for the PyPI package."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import json
import os
from pathlib import Path
import re
import shutil
import site
import stat
import subprocess
import sys
import sysconfig
import tempfile
from typing import Any

from . import __version__
from .adapters.base import (
    ConfigureResult,
    atomic_replace_bytes,
    make_backup,
    result_from_detection,
    run_argv,
    update_agents_index,
)
from .adapters.codex import CodexAdapter
from .adapters.hermes import (
    HermesAdapter,
    MCP_EXPECTED_TOOL_COUNT,
    hermes_home_for_platform,
)
from .cli import _home_from_environment, _prepare_model_route
from .hermes_runtime import (
    HermesMcpInspection,
    inspect_hermes_mcp,
    is_absolute_memleaf_command,
)
from .locking import atomic_write_json
from .native_registration import ensure_hermes_native_sources
from .vault import Vault


_PROVIDER_VERSION_RE = re.compile(r"^version:\s*([^\s#]+)\s*(?:#.*)?$", re.MULTILINE)
_MCP_RUNTIME_POLICIES = frozenset({"auto", "current", "existing"})


@dataclass(frozen=True)
class _PathSnapshot:
    path: Path
    kind: str
    stored: Path | None = None
    link_target: str | None = None
    link_is_directory: bool = False
    mode: int = 0o600


class _HermesInstallFailure(Exception):
    """Expected host-transaction failure with safe, structured diagnostics."""

    def __init__(
        self,
        stage: str,
        reason: str,
        *,
        mcp: dict[str, Any] | None = None,
        user_action: str | None = None,
        recovery_commands: list[list[str]] | None = None,
        mark_mcp_failed: bool = False,
    ) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.mcp = mcp
        self.user_action = user_action
        self.recovery_commands = recovery_commands
        self.mark_mcp_failed = mark_mcp_failed


def _hermes_home(
    home: Path,
    *,
    env: dict[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    return hermes_home_for_platform(
        home,
        os.environ if env is None else env,
        platform=platform,
    )


def _resolve_vault_path(value: str, home: Path) -> Path:
    raw = os.path.expandvars(value.strip())
    if raw == "~":
        candidate = home
    elif raw.startswith("~/") or raw.startswith("~\\"):
        candidate = home / raw[2:]
    else:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = home / candidate
    return candidate.resolve()


def _existing_hermes_vault(hermes_home: Path, home: Path) -> Path | None:
    """Read the Vault used by an existing Hermes memleaf installation."""

    path = hermes_home / "memleaf.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"unsafe existing Hermes memleaf config: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError(f"invalid existing Hermes memleaf config: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"existing Hermes memleaf config must be an object: {path}")
    raw_vault = value.get("vault")
    if raw_vault is None:
        return None
    if not isinstance(raw_vault, str) or not raw_vault.strip():
        raise RuntimeError(f"existing Hermes memleaf Vault path is invalid: {path}")
    return _resolve_vault_path(raw_vault, home)


def _select_vault_path(
    *,
    home: Path,
    hermes_home: Path,
    vault_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """Select the installation Vault without losing an existing custom path."""

    if vault_path is not None:
        candidate = vault_path.expanduser()
        if not candidate.is_absolute():
            candidate = home / candidate
        return candidate.resolve(), "explicit"

    existing = _existing_hermes_vault(hermes_home, home)
    if existing is not None:
        return existing, "hermes_config"

    environment = os.environ if env is None else env
    configured = environment.get("MEMLEAF_VAULT")
    if isinstance(configured, str) and configured.strip():
        return _resolve_vault_path(configured, home), "environment"
    return (home / ".memleaf").resolve(), "default"


def _vault_paths_equivalent(left: Path, right: Path) -> bool:
    """Return whether two host Vault paths identify the same location."""

    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        pass
    try:
        left_value = os.path.normcase(str(left.expanduser().resolve()))
        right_value = os.path.normcase(str(right.expanduser().resolve()))
    except (OSError, RuntimeError):
        return False
    return left_value == right_value


def _select_codex_vault_path(
    *,
    home: Path,
    hermes_home: Path,
    adapter: CodexAdapter,
    detection: Any,
    vault_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """Select one shared Vault for an explicit Codex installation."""

    if vault_path is not None:
        candidate = vault_path.expanduser()
        if not candidate.is_absolute():
            candidate = home / candidate
        return candidate.resolve(), "explicit"

    existing: list[tuple[str, Path]] = []
    hermes_vault = _existing_hermes_vault(hermes_home, home)
    if hermes_vault is not None:
        existing.append(("hermes_config", hermes_vault))
    codex_vault = adapter.configured_vault(detection)
    if codex_vault is not None:
        existing.append(("codex_config", codex_vault))
    if existing:
        reference = existing[0][1]
        if any(not _vault_paths_equivalent(reference, path) for _, path in existing[1:]):
            raise RuntimeError("vault_conflict: existing memleaf hosts use different Vaults")
        return reference, "+".join(source for source, _ in existing)

    environment = os.environ if env is None else env
    configured = environment.get("MEMLEAF_VAULT")
    if isinstance(configured, str) and configured.strip():
        return _resolve_vault_path(configured, home), "environment"
    return (home / ".memleaf").resolve(), "default"


def _memleaf_mcp_command() -> Path:
    name = "memleaf-mcp.exe" if os.name == "nt" else "memleaf-mcp"
    user_scripts = Path(site.USER_BASE) / ("Scripts" if os.name == "nt" else "bin")
    # The current interpreter wins over PATH. PATH may contain a separate
    # editable/source virtualenv, which must be detected as a second runtime.
    candidates = [Path(sysconfig.get_path("scripts")) / name, user_scripts / name]
    found = shutil.which("memleaf-mcp")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    raise RuntimeError("memleaf-mcp console entry point was not found after package installation")


def _copy_provider(hermes_home: Path) -> Path:
    plugins = hermes_home / "plugins"
    target = plugins / "memleaf"
    if hermes_home.is_symlink():
        raise RuntimeError(f"refusing symlinked Hermes home: {hermes_home}")
    if plugins.exists() and (plugins.is_symlink() or not plugins.is_dir()):
        raise RuntimeError(f"unsafe Hermes plugin directory: {plugins}")
    if target.is_symlink():
        try:
            old_target = target.resolve(strict=True)
            old_text = (old_target / "plugin.yaml").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"refusing unverified symlinked Hermes provider path: {target}") from error
        if "name: memleaf" not in old_text:
            raise RuntimeError(f"refusing non-memleaf Hermes provider symlink: {target}")
        target.unlink()
    plugins.mkdir(parents=True, exist_ok=True)

    package = resources.files("memleaf").joinpath("hermes_provider")
    required = ("__init__.py", "plugin.yaml", "README.md")
    with tempfile.TemporaryDirectory(prefix=".memleaf-provider-", dir=plugins) as temporary:
        staging = Path(temporary)
        for name in required:
            item = package.joinpath(name)
            if not item.is_file():
                raise RuntimeError(f"packaged Hermes provider resource is missing: {name}")
            staging.joinpath(name).write_bytes(item.read_bytes())

        if not target.exists():
            os.replace(staging, target)
            return target
        if not target.is_dir():
            raise RuntimeError(f"refusing to overwrite non-directory Hermes provider path: {target}")
        manifest = target / "plugin.yaml"
        if manifest.exists():
            try:
                text = manifest.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise RuntimeError("existing Hermes provider manifest cannot be read") from error
            if "name: memleaf" not in text:
                raise RuntimeError(f"existing Hermes plugin is not memleaf: {target}")
        for name in required:
            destination = target / name
            if destination.is_symlink():
                raise RuntimeError(f"refusing to overwrite symlinked Hermes provider file: {destination}")
            temporary_file = target / f".{name}.{os.getpid()}.tmp"
            temporary_file.write_bytes(staging.joinpath(name).read_bytes())
            os.replace(temporary_file, destination)
    return target


def _provider_manifest_version(provider_path: Path) -> str | None:
    try:
        text = (provider_path / "plugin.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _PROVIDER_VERSION_RE.search(text)
    return match.group(1) if match else None


def _write_provider_config(path: Path, command: Path, vault: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing to write symlinked Hermes config: {path}")
    value: dict[str, Any] = {}
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f"Hermes memleaf config is not a regular file: {path}")
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise RuntimeError(f"invalid Hermes memleaf config: {path}") from error
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Hermes memleaf config must be an object: {path}")
        value.update(parsed)
    value.update(
        {
            "vault": str(vault.expanduser().resolve()),
            "command": str(command),
            "timeout": 5,
            "process_timeout": 300,
            "auto_process": True,
        }
    )
    atomic_write_json(path, value, mode=0o600)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _run(
    command: list[str],
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    options: dict[str, Any] = {
        "env": os.environ.copy(),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "strict",
        "check": False,
    }
    if timeout is not None:
        options["timeout"] = timeout
    return subprocess.run(command, **options)


def _verify_provider(hermes: str) -> bool:
    result = _run([hermes, "memory", "status"])
    if result.returncode != 0:
        return False
    text = (result.stdout + "\n" + result.stderr).casefold()
    return (
        "memleaf" in text
        and "provider" in text
        and ("available" in text or "active" in text)
    )


def _not_checked_model(reason: str) -> dict[str, Any]:
    return {
        "status": "not_checked",
        "reason": reason,
        "selected": None,
        "candidates": [],
        "diagnostics": [],
    }


def _runtime_action() -> str:
    return (
        "Choose the Hermes MCP runtime explicitly: rerun with --mcp-runtime current "
        "to migrate the MCP entry to this memleaf installation, or --mcp-runtime "
        "existing to retain the configured executable after its version is verified."
    )


def _probe_memleaf_runtime_version(command: str) -> tuple[str | None, str | None]:
    if not is_absolute_memleaf_command(command, platform=os.name):
        return None, "the configured MCP command is not an absolute memleaf-mcp path"
    executable = Path(command).expanduser()
    if executable.is_symlink():
        try:
            executable = executable.resolve(strict=True)
        except OSError:
            return None, "the configured MCP executable symlink cannot be resolved"
    if not executable.is_file():
        return None, "the configured MCP executable does not exist"
    try:
        result = _run([str(executable), "--version"], timeout=10)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None, "the configured MCP runtime version could not be queried"
    if result.returncode != 0:
        return None, "the configured MCP runtime rejected --version"
    lines = [
        line.strip()
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        return None, "the configured MCP runtime returned an invalid version response"
    return lines[0], None


def _choose_hermes_mcp_command(
    inspection: HermesMcpInspection,
    current_command: Path,
    *,
    policy: str,
    core_version: str,
) -> tuple[str | None, dict[str, Any]]:
    if policy not in _MCP_RUNTIME_POLICIES:
        raise ValueError(f"unsupported Hermes MCP runtime policy: {policy}")

    details = inspection.to_dict()
    details.update({"policy": policy, "current_command": str(current_command)})
    if inspection.status in {"unknown", "conflict"}:
        details["selection_status"] = "failure"
        return None, details

    if policy == "auto":
        if inspection.status == "runtime_conflict":
            details["selection_status"] = "requires_explicit_choice"
            return None, details
        selected = str(current_command)
    elif policy == "current":
        selected = str(current_command)
    else:
        if inspection.status in {"absent", "legacy"}:
            details.update(
                {
                    "selection_status": "failure",
                    "selection_reason": "there is no absolute existing memleaf runtime to retain",
                }
            )
            return None, details
        selected = inspection.configured_command
        if not selected:
            details.update(
                {
                    "selection_status": "failure",
                    "selection_reason": "the existing runtime path is unavailable",
                }
            )
            return None, details
        version, error = _probe_memleaf_runtime_version(selected)
        details["existing_version"] = version
        if error is not None:
            details.update({"selection_status": "failure", "selection_reason": error})
            return None, details
        if version != core_version:
            details.update(
                {
                    "selection_status": "version_mismatch",
                    "selection_reason": (
                        f"existing MCP runtime version {version} does not match core {core_version}"
                    ),
                }
            )
            return None, details

    details.update({"selection_status": "selected", "selected_command": selected})
    return selected, details


def _configure_hermes_mcp_entry(
    adapter: HermesAdapter,
    detection: Any,
    vault: Path,
    command: str,
    *,
    allow_runtime_migration: bool,
) -> ConfigureResult:
    """Persist through ``hermes config set`` and verify the file from disk."""

    if not detection.detected or detection.confidence != "high" or not detection.executable:
        return result_from_detection(
            detection,
            status="diagnostic",
            reason="Hermes was not reliably detected",
        )

    config_value = getattr(detection, "config_path", None)
    config_path = Path(config_value) if config_value else adapter.config_path
    platform = getattr(adapter, "platform", os.name)
    inspection = inspect_hermes_mcp(config_path, vault, command, platform=platform)
    if inspection.status == "correct":
        return result_from_detection(
            detection,
            status="already_configured",
            reason="existing memleaf MCP entry is correct",
            command=[detection.executable, "mcp", "list"],
        )
    allowed = inspection.status in {"absent", "legacy"}
    if inspection.status == "runtime_conflict" and allow_runtime_migration:
        allowed = True
    if not allowed:
        return result_from_detection(
            detection,
            status="diagnostic",
            reason=inspection.reason,
            command=[detection.executable, "mcp", "list"],
        )

    try:
        backup = make_backup(config_path)
    except Exception:
        return result_from_detection(
            detection,
            status="failure",
            reason="could not create a Hermes config backup; unchanged",
        )

    resolved_vault = str(vault.expanduser().resolve())
    # Disable first. An interrupted update cannot leave an active command with
    # incomplete arguments, even before the outer transaction can roll back.
    commands = (
        [
            detection.executable,
            "config",
            "set",
            "mcp_servers.memleaf.enabled",
            "false",
        ],
        [
            detection.executable,
            "config",
            "set",
            "mcp_servers.memleaf.command",
            command,
        ],
        [
            detection.executable,
            "config",
            "set",
            "mcp_servers.memleaf.args",
            json.dumps(["--vault", resolved_vault], ensure_ascii=False, separators=(",", ":")),
        ],
        [
            detection.executable,
            "config",
            "set",
            "mcp_servers.memleaf.enabled",
            "true",
        ],
    )
    for host_command in commands:
        try:
            result = run_argv(adapter.runner, host_command, env=adapter.env)
        except Exception:
            return result_from_detection(
                detection,
                status="failure",
                reason="Hermes config writer failed; backup retained",
                backup_path=backup,
                command=host_command,
            )
        if result.returncode != 0:
            return result_from_detection(
                detection,
                status="failure",
                reason="Hermes config writer returned a non-zero status; backup retained",
                backup_path=backup,
                command=host_command,
            )

    confirmed = inspect_hermes_mcp(config_path, vault, command, platform=platform)
    if confirmed.status != "correct":
        return result_from_detection(
            detection,
            status="failure",
            reason="Hermes config writer returned success but the MCP entry was not confirmed",
            backup_path=backup,
            command=commands[-1],
        )
    return result_from_detection(
        detection,
        status="configured",
        reason="MCP entry persisted through Hermes config and verified from disk",
        changed=True,
        backup_path=backup,
        command=commands[-1],
    )


def _mcp_recovery_commands(hermes: str, command: str, vault: Path) -> list[list[str]]:
    resolved_vault = str(vault.expanduser().resolve())
    return [
        [hermes, "config", "set", "mcp_servers.memleaf.enabled", "false"],
        [hermes, "config", "set", "mcp_servers.memleaf.command", command],
        [
            hermes,
            "config",
            "set",
            "mcp_servers.memleaf.args",
            json.dumps(["--vault", resolved_vault], ensure_ascii=False, separators=(",", ":")),
        ],
        [hermes, "config", "set", "mcp_servers.memleaf.enabled", "true"],
        [hermes, "config", "set", "mcp_servers.memleaf.lazy", "true"],
        [
            hermes,
            "config",
            "set",
            "mcp_servers.memleaf.idle_timeout_seconds",
            "60",
        ],
        [hermes, "mcp", "list"],
        [hermes, "mcp", "test", "memleaf"],
    ]


def _snapshot_path(path: Path, staging_root: Path, label: str) -> _PathSnapshot:
    if path.is_symlink():
        return _PathSnapshot(
            path=path,
            kind="symlink",
            link_target=os.readlink(path),
            link_is_directory=path.is_dir(),
        )
    if not path.exists():
        return _PathSnapshot(path=path, kind="missing")
    stored = staging_root / label
    if path.is_file():
        shutil.copy2(path, stored)
        return _PathSnapshot(
            path=path,
            kind="file",
            stored=stored,
            mode=path.stat().st_mode & 0o7777,
        )
    if path.is_dir():
        shutil.copytree(path, stored, symlinks=True)
        return _PathSnapshot(path=path, kind="directory", stored=stored)
    raise OSError(f"unsupported path type: {path}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        raise OSError(f"unsupported path type: {path}")


def _restore_snapshot(snapshot: _PathSnapshot) -> None:
    _remove_path(snapshot.path)
    if snapshot.kind == "missing":
        return
    snapshot.path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.kind == "symlink":
        if snapshot.link_target is None:
            raise OSError("missing symlink target in snapshot")
        snapshot.path.symlink_to(
            snapshot.link_target,
            target_is_directory=snapshot.link_is_directory,
        )
        return
    if snapshot.stored is None:
        raise OSError("missing snapshot payload")
    if snapshot.kind == "file":
        atomic_replace_bytes(
            snapshot.path,
            snapshot.stored.read_bytes(),
            mode=snapshot.mode or 0o600,
        )
        return
    if snapshot.kind == "directory":
        shutil.copytree(snapshot.stored, snapshot.path, symlinks=True)
        return
    raise OSError(f"unknown snapshot kind: {snapshot.kind}")


def _rollback_snapshots(snapshots: list[_PathSnapshot]) -> str:
    try:
        for snapshot in reversed(snapshots):
            _restore_snapshot(snapshot)
    except Exception:
        return "failed"
    return "completed"


def _failure_result(
    *,
    stage: str,
    reason: str,
    core_version: str,
    vault: Path | str | None,
    vault_source: str | None = None,
    model: dict[str, Any] | None = None,
    provider_version: str | None = None,
    provider: Path | str | None = None,
    provider_updated: bool = False,
    mcp: dict[str, Any] | None = None,
    mcp_runtime: dict[str, Any] | None = None,
    user_action: str | None = None,
    recovery_commands: list[list[str]] | None = None,
    rollback_status: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "failure",
        "stage": stage,
        "reason": reason,
        "core_version": core_version,
        "provider_version": provider_version,
        "provider_updated": bool(provider_updated),
        "vault": str(vault) if vault is not None else None,
        "model": model or _not_checked_model(f"installation stopped at {stage}"),
    }
    if vault_source is not None:
        result["vault_source"] = vault_source
    if provider is not None:
        result["provider"] = str(provider)
    if mcp is not None:
        result["mcp"] = mcp
    if mcp_runtime is not None:
        result["mcp_runtime"] = mcp_runtime
    if user_action is not None:
        result.update({"user_action_required": True, "user_action": user_action})
    if recovery_commands:
        result["recovery_commands"] = recovery_commands
    if rollback_status is not None:
        result["rollback_status"] = rollback_status
    return result


def install_hermes(
    *,
    vault_path: Path | None = None,
    mcp_runtime: str = "auto",
) -> dict[str, Any]:
    """Install memleaf for Hermes with runtime preflight and host rollback.

    ``auto`` refuses to choose between two absolute memleaf runtimes.
    ``current`` migrates Hermes to the runtime executing this installer.
    ``existing`` retains the configured runtime after an exact version check.
    """

    core_version = __version__
    if mcp_runtime not in _MCP_RUNTIME_POLICIES:
        raise ValueError(f"unsupported Hermes MCP runtime policy: {mcp_runtime}")

    home = _home_from_environment()
    hermes_home = _hermes_home(home)
    try:
        selected_vault, vault_source = _select_vault_path(
            home=home,
            hermes_home=hermes_home,
            vault_path=vault_path,
        )
    except RuntimeError:
        return _failure_result(
            stage="vault_selection",
            reason="existing Hermes memleaf Vault configuration is invalid",
            core_version=core_version,
            vault=None,
        )

    adapter = HermesAdapter(home=home, env=os.environ, hermes_home=hermes_home)
    detection = adapter.detect()
    if not detection.detected or detection.confidence != "high" or not detection.executable:
        return _failure_result(
            stage="hermes_detection",
            reason="Hermes executable was not found",
            core_version=core_version,
            vault=selected_vault,
            vault_source=vault_source,
        )

    try:
        current_command = _memleaf_mcp_command()
    except RuntimeError:
        return _failure_result(
            stage="runtime_detection",
            reason="memleaf-mcp console entry point was not found after package installation",
            core_version=core_version,
            vault=selected_vault,
            vault_source=vault_source,
        )

    config_value = getattr(detection, "config_path", None)
    config_path = Path(config_value) if config_value else adapter.config_path
    platform = getattr(adapter, "platform", os.name)
    preflight = inspect_hermes_mcp(
        config_path,
        selected_vault,
        current_command,
        platform=platform,
    )
    selected_command, runtime_details = _choose_hermes_mcp_command(
        preflight,
        current_command,
        policy=mcp_runtime,
        core_version=core_version,
    )
    if selected_command is None:
        reason = runtime_details.get("selection_reason") or preflight.reason
        action = (
            _runtime_action()
            if preflight.status == "runtime_conflict"
            else (
                "Fix or remove the conflicting mcp_servers.memleaf entry in the "
                "reported Hermes config, then rerun the installer."
            )
        )
        return _failure_result(
            stage="mcp_preflight",
            reason=str(reason),
            core_version=core_version,
            vault=selected_vault,
            vault_source=vault_source,
            mcp_runtime=runtime_details,
            user_action=action,
        )

    vault = Vault.initialize(selected_vault)
    model = _prepare_model_route(
        vault.root,
        home=home,
        dry_run=False,
        non_interactive=not sys.stdin.isatty(),
        skip_discovery=False,
    )
    if model.get("status") == "failure":
        return _failure_result(
            stage="model_route",
            reason="model route is not configured",
            core_version=core_version,
            vault=vault.root,
            vault_source=vault_source,
            model=model,
            mcp_runtime=runtime_details,
        )

    provider_target = hermes_home / "plugins" / "memleaf"
    provider_config = hermes_home / "memleaf.json"
    provider_path = provider_target
    provider_version: str | None = None
    configured: ConfigureResult | None = None
    native_registration: dict[str, Any] | None = None

    try:
        transaction = tempfile.TemporaryDirectory(prefix=".memleaf-hermes-transaction-")
    except Exception:
        return _failure_result(
            stage="host_snapshot",
            reason="Hermes configuration snapshot storage could not be created; unchanged",
            core_version=core_version,
            vault=vault.root,
            vault_source=vault_source,
            model=model,
            provider=provider_target,
            mcp_runtime=runtime_details,
        )

    with transaction as temporary:
        staging_root = Path(temporary)
        try:
            snapshots = [
                _snapshot_path(config_path, staging_root, "config.yaml"),
                _snapshot_path(provider_config, staging_root, "memleaf.json"),
                _snapshot_path(provider_target, staging_root, "provider"),
            ]
        except Exception:
            return _failure_result(
                stage="host_snapshot",
                reason="Hermes configuration could not be snapshotted safely; unchanged",
                core_version=core_version,
                vault=vault.root,
                vault_source=vault_source,
                model=model,
                provider=provider_target,
                mcp_runtime=runtime_details,
            )

        failure: _HermesInstallFailure | None = None
        try:
            configured = _configure_hermes_mcp_entry(
                adapter,
                detection,
                vault.root,
                selected_command,
                allow_runtime_migration=(
                    mcp_runtime == "current" and preflight.status == "runtime_conflict"
                ),
            )
            if configured.status not in {"configured", "already_configured"}:
                raise _HermesInstallFailure(
                    stage="mcp_persist",
                    reason=f"Hermes MCP entry could not be configured: {configured.reason}",
                    mcp=configured.to_dict(),
                    user_action=(
                        "Use the commands below to write the MCP entry, then require both "
                        "`hermes mcp list` and `hermes mcp test memleaf` to succeed."
                    ),
                    recovery_commands=_mcp_recovery_commands(
                        detection.executable,
                        selected_command,
                        vault.root,
                    ),
                )
            if not adapter.configure_mcp_lifecycle(detection, idle_timeout_seconds=60):
                raise _HermesInstallFailure(
                    stage="mcp_lifecycle",
                    reason="Hermes MCP lifecycle could not be configured",
                    mcp=configured.to_dict(),
                    recovery_commands=_mcp_recovery_commands(
                        detection.executable,
                        selected_command,
                        vault.root,
                    ),
                )
            if not adapter.test_mcp(detection, expected_tools=MCP_EXPECTED_TOOL_COUNT):
                raise _HermesInstallFailure(
                    stage="mcp_test",
                    reason=f"Hermes MCP test did not confirm {MCP_EXPECTED_TOOL_COUNT} tools",
                    mcp=configured.to_dict(),
                    user_action="Inspect the MCP process error, then rerun the installer.",
                    recovery_commands=[
                        [detection.executable, "mcp", "list"],
                        [detection.executable, "mcp", "test", "memleaf"],
                    ],
                    mark_mcp_failed=True,
                )

            try:
                provider_path = _copy_provider(hermes_home)
            except Exception as error:
                raise _HermesInstallFailure(
                    "provider_copy",
                    "Hermes MemoryProvider files could not be installed safely",
                ) from error
            provider_version = _provider_manifest_version(provider_path)
            if provider_version != core_version:
                raise _HermesInstallFailure(
                    "provider_version",
                    (
                        "Hermes provider version mismatch after copy: "
                        f"core={core_version}, provider={provider_version or 'unknown'}"
                    ),
                )
            try:
                _write_provider_config(provider_config, Path(selected_command), vault.root)
            except Exception as error:
                raise _HermesInstallFailure(
                    "provider_config",
                    "Hermes memleaf provider configuration could not be written safely",
                ) from error

            activated = _run(
                [detection.executable, "config", "set", "memory.provider", "memleaf"]
            )
            if activated.returncode != 0 or not _verify_provider(detection.executable):
                raise _HermesInstallFailure(
                    "provider_activation",
                    "Hermes MemoryProvider could not be activated",
                )
            try:
                native_registration = ensure_hermes_native_sources(vault, hermes_home)
            except Exception as error:
                raise _HermesInstallFailure(
                    "native_sources",
                    "Hermes native memory sources could not be registered safely",
                ) from error
        except _HermesInstallFailure as error:
            failure = error
        except Exception:
            failure = _HermesInstallFailure(
                "host_transaction",
                "an unexpected Hermes installation error occurred",
                mcp=configured.to_dict() if configured is not None else None,
            )

        if failure is not None:
            rollback = _rollback_snapshots(snapshots)
            if failure.mark_mcp_failed:
                update_agents_index(
                    vault.agents_index_path,
                    {"hermes": {"mcp_status": "failed", "mcp_availability": "unavailable"}},
                )
            return _failure_result(
                stage=failure.stage,
                reason=failure.reason,
                core_version=core_version,
                provider_version=provider_version,
                provider_updated=rollback == "failed",
                vault=vault.root,
                vault_source=vault_source,
                model=model,
                provider=provider_path,
                mcp=failure.mcp,
                mcp_runtime=runtime_details,
                user_action=failure.user_action,
                recovery_commands=failure.recovery_commands,
                rollback_status=rollback,
            )

    update_agents_index(
        vault.agents_index_path,
        {
            "hermes": {
                "agent": "hermes",
                "detected": True,
                "confidence": "high",
                "reason": "PyPI-installed memleaf provider is active and MCP test passed",
                "executable": str(Path(detection.executable).resolve()),
                "config_path": str(provider_config.resolve()),
                "status": "configured",
                "provider_status": "active",
                "mcp_status": "active",
                "mcp_availability": "available",
                "user_action_required": False,
            }
        },
    )
    return {
        "status": "configured",
        "reason": "memleaf is fully configured for Hermes",
        "core_version": core_version,
        "provider_version": provider_version,
        "provider_updated": True,
        "vault": str(vault.root),
        "vault_source": vault_source,
        "provider": str(provider_path),
        "mcp_command": selected_command,
        "mcp_runtime": runtime_details,
        "model": model,
        "native_sources": native_registration,
    }


def install_codex(*, vault_path: Path | None = None) -> dict[str, Any]:
    """Explicitly configure the Codex host without changing Codex model settings."""

    home = _home_from_environment()
    adapter = CodexAdapter(home=home, env=os.environ)
    detection = adapter.detect()
    if not detection.detected or detection.confidence != "high" or not detection.executable:
        return {"status": "failure", "reason": "Codex executable was not found", "vault": None}

    try:
        selected_vault, vault_source = _select_codex_vault_path(
            home=home,
            hermes_home=_hermes_home(home),
            adapter=adapter,
            detection=detection,
            vault_path=vault_path,
        )
    except RuntimeError as error:
        conflict = str(error).startswith("vault_conflict:")
        return {
            "status": "diagnostic" if conflict else "failure",
            "reason": "vault_conflict" if conflict else "existing host configuration is invalid",
            "vault": None,
        }
    preflight = adapter.configure(detection, selected_vault, dry_run=True)
    if preflight.status == "diagnostic":
        return {
            "status": "diagnostic",
            "reason": preflight.reason,
            "vault": str(selected_vault),
            "vault_source": vault_source,
            "codex": preflight.to_dict(),
        }

    vault = Vault.initialize(selected_vault)
    model = _prepare_model_route(
        vault.root,
        home=home,
        dry_run=False,
        non_interactive=True,
        skip_discovery=True,
    )
    configured = adapter.configure(detection, vault.root, attempt=True)
    update_agents_index(vault.agents_index_path, {"codex": configured.to_dict()})
    if configured.status not in {"configured", "already_configured"}:
        return {
            "status": configured.status,
            "reason": configured.reason,
            "vault": str(vault.root),
            "vault_source": vault_source,
            "model": model,
            "codex": configured.to_dict(),
        }
    model_ready = model.get("status") in {"configured", "already_configured"}
    actions: list[str] = []
    if configured.user_action_required and configured.user_action:
        actions.append(configured.user_action)
    if not model_ready:
        actions.append(
            "Configure an independent memleaf Model Route for this Vault before relying on "
            "automatic memory extraction. Codex model/provider settings are intentionally "
            "not used or modified."
        )
    return {
        "status": configured.status,
        "reason": configured.reason,
        "vault": str(vault.root),
        "vault_source": vault_source,
        "model": model,
        "processing_status": "ready" if model_ready else "model_route_required",
        "codex": configured.to_dict(),
        "user_action_required": bool(actions),
        "user_action": " ".join(actions) if actions else None,
    }


__all__ = ["install_codex", "install_hermes"]
