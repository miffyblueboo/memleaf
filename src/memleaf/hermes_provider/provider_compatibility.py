"""Bounded Provider resource identity shared by Core and the standalone copy.

This is compatibility detection, not authentication or proof of live bytecode.
Copies must still be installed while hosts are stopped, then restarted.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from collections.abc import Mapping

BUILD_META = "io.memleaf/provider-build"
BUILD_SCHEMA = 1
PROVIDER_FILES = ("__init__.py", "_shared.py", "_mcp_client.py", "_provider.py",
                  "evidence_budget.py", "provider_compatibility.py", "plugin.yaml")
MAX_FILE_BYTES = 512 * 1024
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
READ_ONLY_TOOLS = frozenset({"stats", "process_status", "scope_catalog", "search",
                             "list_todos", "read", "context"})
COMPATIBILITY_CODES = frozenset({"provider_build_unavailable", "provider_restart_required",
                                "core_build_unverified", "provider_core_mismatch"})


def _identity(info):
    # lstat and fstat ctime have different meanings on some Windows versions.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def provider_build(directory: Path | str) -> dict:
    """Read only fixed behavior resources, with no Core/Hermes imports or Vault IO."""
    root = Path(directory)
    try:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("invalid_provider_directory")
        entries = []
        total = 0
        for name in PROVIDER_FILES:
            path = root / name
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FILE_BYTES:
                raise ValueError("invalid_provider_file")
            total += before.st_size
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("provider_bundle_too_large")
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                    raise ValueError("provider_file_changed")
                raw = stream.read(opened.st_size + 1)
                finished = os.fstat(stream.fileno())
            after = path.lstat()
            if (not stat.S_ISREG(after.st_mode) or len(raw) != opened.st_size
                    or _identity(before) != _identity(after)
                    or before.st_ctime_ns != after.st_ctime_ns
                    or _identity(opened) != _identity(finished)
                    or opened.st_ctime_ns != finished.st_ctime_ns):
                raise ValueError("provider_file_changed")
            entries.append((name, len(raw), hashlib.sha256(raw).hexdigest()))
        digest = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
        return {"schema": BUILD_SCHEMA, "digest": digest}
    except (OSError, ValueError, RuntimeError):
        # A missing/damaged copy must not prevent local reading or MCP startup.
        return {"schema": BUILD_SCHEMA, "status": "unavailable"}


def valid_build(value) -> bool:
    return (isinstance(value, Mapping) and set(value) == {"schema", "digest"}
            and type(value.get("schema")) is int and value["schema"] == BUILD_SCHEMA
            and isinstance(value.get("digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["digest"]) is not None)


def compatibility(loaded, current, expected) -> str:
    """Compare import-time copy, present copy and the peer's expected resources."""
    if not valid_build(loaded) or not valid_build(current):
        return "provider_build_unavailable"
    if loaded != current:
        return "provider_restart_required"
    if not valid_build(expected):
        return "core_build_unverified"
    if loaded != expected:
        return "provider_core_mismatch"
    return "compatible"
