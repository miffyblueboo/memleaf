"""One-line PyPI installer for the Hermes integration."""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path
import shutil
import site
import stat
import subprocess
import sys
import sysconfig
import tempfile
from typing import Any

from .adapters.base import update_agents_index
from .adapters.hermes import HermesAdapter, hermes_home_for_platform
from .cli import _home_from_environment, _prepare_model_route
from .locking import atomic_write_json
from .vault import Vault


_EXPECTED_TOOLS = 11


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
    """Read the Vault used by an existing Hermes memleaf installation.

    Existing configuration is authoritative during upgrades. If it exists but
    is unsafe or malformed, fail instead of silently switching the user to a
    new default Vault.
    """

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


def _memleaf_mcp_command() -> Path:
    name = "memleaf-mcp.exe" if os.name == "nt" else "memleaf-mcp"
    user_scripts = Path(site.USER_BASE) / ("Scripts" if os.name == "nt" else "bin")
    candidates = [
        Path(sysconfig.get_path("scripts")) / name,
        user_scripts / name,
    ]
    found = shutil.which("memleaf-mcp")
    if found:
        candidates.insert(0, Path(found))
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
            old_manifest = old_target / "plugin.yaml"
            old_text = old_manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"refusing unverified symlinked Hermes provider path: {target}") from error
        if "name: memleaf" not in old_text:
            raise RuntimeError(f"refusing non-memleaf Hermes provider symlink: {target}")
        target.unlink()
    plugins.mkdir(parents=True, exist_ok=True)

    package = resources.files("memleaf").joinpath("hermes_provider")
    required = ("__init__.py", "plugin.yaml", "README.md")
    with tempfile.TemporaryDirectory(prefix=".memleaf-provider-", dir=plugins) as temp_name:
        staging = Path(temp_name)
        for name in required:
            item = package.joinpath(name)
            if not item.is_file():
                raise RuntimeError(f"packaged Hermes provider resource is missing: {name}")
            staging.joinpath(name).write_bytes(item.read_bytes())

        if target.exists():
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
                temporary = target / f".{name}.{os.getpid()}.tmp"
                temporary.write_bytes(staging.joinpath(name).read_bytes())
                os.replace(temporary, destination)
        else:
            os.replace(staging, target)
    return target


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


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )


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


def install_hermes(*, vault_path: Path | None = None) -> dict[str, Any]:
    """Install and configure memleaf for Hermes from the PyPI package."""

    home = _home_from_environment()
    hermes_home = _hermes_home(home)
    selected_vault, vault_source = _select_vault_path(
        home=home,
        hermes_home=hermes_home,
        vault_path=vault_path,
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
        return {
            "status": "failure",
            "reason": "model route is not configured",
            "vault": str(vault.root),
            "model": model,
        }

    adapter = HermesAdapter(home=home, env=os.environ, hermes_home=hermes_home)
    detection = adapter.detect()
    if not detection.detected or detection.confidence != "high" or not detection.executable:
        return {
            "status": "failure",
            "reason": "Hermes executable was not found",
            "vault": str(vault.root),
            "model": model,
        }

    command = _memleaf_mcp_command()
    provider_path = _copy_provider(hermes_home)
    _write_provider_config(hermes_home / "memleaf.json", command, vault.root)

    activated = _run([detection.executable, "config", "set", "memory.provider", "memleaf"])
    if activated.returncode != 0 or not _verify_provider(detection.executable):
        return {
            "status": "failure",
            "reason": "Hermes MemoryProvider could not be activated",
            "vault": str(vault.root),
            "provider": str(provider_path),
            "model": model,
        }

    adapter = HermesAdapter(
        home=home,
        env=os.environ,
        hermes_home=hermes_home,
        memleaf_command=command,
    )
    detection = adapter.detect()
    configured = adapter.configure(detection, vault.root, attempt=True)
    if configured.status not in {"configured", "already_configured"}:
        return {
            "status": "failure",
            "reason": "Hermes MCP entry could not be configured",
            "vault": str(vault.root),
            "provider": str(provider_path),
            "model": model,
            "mcp": configured.to_dict(),
        }
    if not adapter.configure_mcp_lifecycle(detection, idle_timeout_seconds=60):
        return {
            "status": "failure",
            "reason": "Hermes MCP lifecycle could not be configured",
            "vault": str(vault.root),
            "provider": str(provider_path),
            "model": model,
        }
    if not adapter.test_mcp(detection, expected_tools=_EXPECTED_TOOLS):
        update_agents_index(
            vault.agents_index_path,
            {"hermes": {"mcp_status": "failed", "mcp_availability": "unavailable"}},
        )
        return {
            "status": "failure",
            "reason": "Hermes MCP test did not confirm 11 tools",
            "vault": str(vault.root),
            "provider": str(provider_path),
            "model": model,
        }

    update_agents_index(
        vault.agents_index_path,
        {
            "hermes": {
                "agent": "hermes",
                "detected": True,
                "confidence": "high",
                "reason": "PyPI-installed memleaf provider is active and MCP test passed",
                "executable": str(Path(detection.executable).resolve()),
                "config_path": str((hermes_home / "memleaf.json").resolve()),
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
        "vault": str(vault.root),
        "vault_source": vault_source,
        "provider": str(provider_path),
        "mcp_command": str(command),
        "model": model,
    }


__all__ = ["install_hermes"]
