"""Read-only inspection of Hermes' persisted memleaf MCP runtime.

The installer uses this module before mutating Hermes so an existing MCP entry
that points at a different virtual environment cannot be silently combined with
a newly installed MemoryProvider. The parser deliberately reuses the adapter's
small JSON/YAML reader and never executes a command discovered in user config.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import ntpath
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters.hermes import _parse_json_or_yaml_mcp


_MEMLEAF_COMMAND_NAMES = frozenset({"memleaf-mcp", "memleaf-mcp.exe"})
_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


@dataclass(frozen=True)
class HermesMcpInspection:
    """Safe, serializable description of one Hermes memleaf MCP entry."""

    status: str
    reason: str
    config_path: str
    expected_command: str
    configured_command: str | None = None
    configured_vault: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "config_path": self.config_path,
            "expected_command": self.expected_command,
            "configured_command": self.configured_command,
            "configured_vault": self.configured_vault,
        }


def _platform_name(platform: str | None) -> str:
    return os.name if platform is None else platform


def _path_text(value: Path | str) -> str:
    return os.path.expandvars(os.path.expanduser(str(value).strip()))


def _host_path_key(value: Path | str, *, platform: str | None = None) -> str:
    """Normalize a host path without pretending a foreign path is local."""

    text = _path_text(value)
    if _platform_name(platform) == "nt":
        return ntpath.normcase(ntpath.normpath(text.replace("/", "\\")))
    return os.path.normcase(str(Path(text).expanduser().resolve(strict=False)))


def host_paths_equivalent(
    left: Path | str,
    right: Path | str,
    *,
    platform: str | None = None,
) -> bool:
    """Return whether two paths identify the same location on the host."""

    effective_platform = _platform_name(platform)
    if effective_platform == os.name:
        try:
            left_path = Path(left).expanduser()
            right_path = Path(right).expanduser()
            if left_path.exists() and right_path.exists():
                return os.path.samefile(left_path, right_path)
        except (OSError, TypeError, ValueError):
            pass
    try:
        return _host_path_key(left, platform=effective_platform) == _host_path_key(
            right, platform=effective_platform
        )
    except (OSError, TypeError, ValueError):
        return False


def _command_basename(command: str, *, platform: str | None = None) -> str:
    text = command.strip()
    if _platform_name(platform) == "nt":
        return ntpath.basename(text.replace("/", "\\")).casefold()
    return Path(text).name.casefold()


def is_absolute_memleaf_command(
    command: str,
    *,
    platform: str | None = None,
) -> bool:
    """Return whether ``command`` is an absolute memleaf-mcp executable path."""

    if not isinstance(command, str) or not command.strip():
        return False
    text = _path_text(command)
    if _command_basename(text, platform=platform) not in _MEMLEAF_COMMAND_NAMES:
        return False
    if _platform_name(platform) == "nt":
        return ntpath.isabs(text.replace("/", "\\"))
    return Path(text).is_absolute()


def _is_bare_memleaf_command(command: str, *, platform: str | None = None) -> bool:
    text = command.strip()
    return (
        _command_basename(text, platform=platform) in _MEMLEAF_COMMAND_NAMES
        and not is_absolute_memleaf_command(text, platform=platform)
        and "/" not in text
        and "\\" not in text
    )


def _boolish_enabled(value: Any) -> bool | None:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    return None


def _vault_from_args(args: Any) -> str | None:
    if not isinstance(args, Sequence) or isinstance(args, (str, bytes, bytearray)):
        return None
    values = [str(item) for item in args]
    if len(values) == 2 and values[0] == "--vault" and values[1].strip():
        return values[1].strip()
    if len(values) == 1 and values[0].startswith("--vault="):
        value = values[0].split("=", 1)[1].strip()
        return value or None
    return None


def _result(
    status: str,
    reason: str,
    *,
    config_path: Path,
    expected_command: Path | str,
    configured_command: str | None = None,
    configured_vault: str | None = None,
) -> HermesMcpInspection:
    return HermesMcpInspection(
        status=status,
        reason=reason,
        config_path=str(config_path),
        expected_command=str(expected_command),
        configured_command=configured_command,
        configured_vault=configured_vault,
    )


def inspect_hermes_mcp(
    config_path: Path | str,
    vault: Path | str,
    expected_command: Path | str,
    *,
    platform: str | None = None,
) -> HermesMcpInspection:
    """Inspect ``mcp_servers.memleaf`` without executing configured commands.

    Status values are:

    ``absent``
        No memleaf entry exists.
    ``correct``
        The entry uses the requested Vault and the same executable.
    ``legacy``
        The entry uses the requested Vault and a bare ``memleaf-mcp`` command;
        it is safe for the installer to canonicalize to an absolute path.
    ``runtime_conflict``
        The Vault matches, but an absolute memleaf executable from a different
        environment is configured. The installer must require an explicit
        runtime policy before changing either side.
    ``conflict``
        The entry is disabled, points at another Vault, has invalid arguments,
        or names a non-memleaf command.
    ``unknown``
        The configuration cannot be read safely.
    """

    path = Path(config_path)
    if path.is_symlink():
        return _result(
            "unknown",
            "Hermes config is a symlink; refusing automatic access",
            config_path=path,
            expected_command=expected_command,
        )
    if path.exists() and not path.is_file():
        return _result(
            "unknown",
            "Hermes config is not a regular file",
            config_path=path,
            expected_command=expected_command,
        )
    if not path.exists():
        return _result(
            "absent",
            "no persisted memleaf MCP entry was found",
            config_path=path,
            expected_command=expected_command,
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = ""
        parsed = None
    else:
        parsed = _parse_json_or_yaml_mcp(text)
        # The adapter's compact parser historically returns None for a valid
        # JSON object that simply has no mcp_servers key. Treat that shape as
        # an absent entry, while retaining fail-closed behavior for malformed
        # or type-invalid configuration.
        if parsed is None:
            try:
                root = json.loads(text)
            except (TypeError, ValueError):
                root = None
            if isinstance(root, Mapping) and root.get("mcp_servers") in (None, {}):
                parsed = {}
    if parsed is None:
        return _result(
            "unknown",
            "Hermes config could not be parsed safely",
            config_path=path,
            expected_command=expected_command,
        )

    entry = parsed.get("memleaf")
    if entry is None:
        return _result(
            "absent",
            "no persisted memleaf MCP entry was found",
            config_path=path,
            expected_command=expected_command,
        )
    if not isinstance(entry, Mapping):
        return _result(
            "conflict",
            "mcp_servers.memleaf is not an object",
            config_path=path,
            expected_command=expected_command,
        )

    enabled = _boolish_enabled(entry.get("enabled"))
    command_value = entry.get("command")
    command = command_value.strip() if isinstance(command_value, str) else None
    configured_vault = _vault_from_args(entry.get("args"))

    if enabled is not True:
        reason = (
            "the existing memleaf MCP entry is disabled"
            if enabled is False
            else "the existing memleaf MCP enabled value is invalid"
        )
        return _result(
            "conflict",
            reason,
            config_path=path,
            expected_command=expected_command,
            configured_command=command,
            configured_vault=configured_vault,
        )
    if not command:
        return _result(
            "conflict",
            "the existing memleaf MCP command is missing or invalid",
            config_path=path,
            expected_command=expected_command,
            configured_vault=configured_vault,
        )
    if configured_vault is None:
        return _result(
            "conflict",
            "the existing memleaf MCP args must contain exactly one --vault path",
            config_path=path,
            expected_command=expected_command,
            configured_command=command,
        )
    if not host_paths_equivalent(configured_vault, vault, platform=platform):
        return _result(
            "conflict",
            "the existing memleaf MCP entry points at a different Vault",
            config_path=path,
            expected_command=expected_command,
            configured_command=command,
            configured_vault=configured_vault,
        )
    if host_paths_equivalent(command, expected_command, platform=platform):
        return _result(
            "correct",
            "the existing memleaf MCP entry uses this runtime and Vault",
            config_path=path,
            expected_command=expected_command,
            configured_command=command,
            configured_vault=configured_vault,
        )
    if _is_bare_memleaf_command(command, platform=platform):
        return _result(
            "legacy",
            "the existing entry uses a PATH-dependent memleaf-mcp command",
            config_path=path,
            expected_command=expected_command,
            configured_command=command,
            configured_vault=configured_vault,
        )
    if is_absolute_memleaf_command(command, platform=platform):
        return _result(
            "runtime_conflict",
            "the existing MCP entry uses the same Vault but a different memleaf runtime",
            config_path=path,
            expected_command=expected_command,
            configured_command=command,
            configured_vault=configured_vault,
        )
    return _result(
        "conflict",
        "the existing MCP command is not a recognized memleaf-mcp executable",
        config_path=path,
        expected_command=expected_command,
        configured_command=command,
        configured_vault=configured_vault,
    )


__all__ = [
    "HermesMcpInspection",
    "host_paths_equivalent",
    "inspect_hermes_mcp",
    "is_absolute_memleaf_command",
]
