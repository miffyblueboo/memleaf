"""Read-only native comparison snapshots for the incremental planner.

Reuse NativeIndexer's source and fragment identities without refreshing its
index or shadow map. Guard all eligible files, not only retrieved fragments:
a changed native file can alter whether an incoming CREATE is a duplicate.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
import stat
from typing import Any

from .models import Memory
from .native_index import (MAX_NATIVE_BYTES, _markdown_segments, _text_segments, _native_id,
                           validate_native_sources)

MAX_SOURCES = 64
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS = 4096


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read_file(path: Path) -> bytes:
    """Read one stable regular file with a bounded allocation; never write it."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("native_source_unsafe")
        if before.st_size > MAX_NATIVE_BYTES:
            raise ValueError("native_source_too_large")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("native_source_unsafe")
            # Compare identity, size and mtime across APIs, but not ctime:
            # CPython 3.12 on Windows can expose creation time via lstat and
            # metadata-change time via fstat. Each ctime is still checked
            # against a second observation through the same API below.
            if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
                raise ValueError("native_source_changed_during_read")
            data = stream.read(opened.st_size + 1)
            finished = os.fstat(stream.fileno())
        after = path.lstat()
    except FileNotFoundError:
        raise ValueError("native_source_missing") from None
    except OSError:
        raise ValueError("native_source_unreadable") from None
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(data) > MAX_NATIVE_BYTES:
        raise ValueError("native_source_too_large")
    if (signature(after) != signature(before) or signature(finished) != signature(opened)
            or len(data) != before.st_size
            or not stat.S_ISREG(after.st_mode) or not stat.S_ISREG(finished.st_mode)):
        raise ValueError("native_source_changed_during_read")
    return data


@dataclass
class NativeComparison:
    guard: dict[str, Any]
    memories: dict[str, Memory]
    bindings: dict[str, dict[str, Any]]



def read_comparison(service: Any, agent: str, *, fragments: bool = True) -> NativeComparison:
    """Own-agent sources may be private; foreign sources require share=true.

    Configuration paths are trusted local configuration, not model output. The
    frozen guard contains hashes, never paths or native plaintext. An unreadable
    eligible source blocks this opt-in planner, not ordinary local retrieval.
    """
    try:
        configured = validate_native_sources(service.vault.config().get("native_sources", {}),
                                              base_dir=service.vault.root)
    except ValueError:
        raise ValueError("invalid_native_configuration") from None
    if len(configured) > MAX_SOURCES:
        raise ValueError("native_context_too_large")
    sources, memories, bindings = {}, {}, {}
    total = 0
    for source_id, config in sorted(configured.items()):
        if not config["enabled"] or (config["agent"] != agent and not config["share"]):
            continue
        # Use the configured lexical path to retain symlink checking; resolving
        # only for the config digest must not turn a link into a regular file.
        path = Path(config["path"]).expanduser()
        if not path.is_absolute():
            path = service.vault.root / path
        data = _read_file(path)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("native_context_too_large")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise ValueError("native_source_invalid_utf8") from None
        config_hash = _digest({k: config[k] for k in ("agent", "resolved_path", "format", "share", "enabled")})
        file_hash = hashlib.sha256(data).hexdigest()
        sources[source_id] = {"config_hash": config_hash, "file_hash": file_hash}
        if not fragments:
            continue
        segments = (_markdown_segments if config["format"] == "markdown" else _text_segments)(source_id, text)
        if len(memories) + len(segments) > MAX_SEGMENTS:
            raise ValueError("native_context_too_large")
        lines = text.splitlines()
        for segment in segments:
            identity = segment["native_id"]
            body = "\n".join(lines[segment["start_line"] - 1:segment["end_line"]])
            if identity in memories:
                raise ValueError("duplicate_native_identity")
            memories[identity] = Memory(memory_id=identity, title=segment["heading"] or source_id,
                                         body=body, type="fact", scopes=["global"],
                                         keywords=segment["keywords"])
            bindings[identity] = {"native_id": identity, "source_id": source_id, "agent": config["agent"],
                                  "locator": segment["locator"], "content_hash": segment["content_hash"],
                                  "config_hash": config_hash, "file_hash": file_hash}
    return NativeComparison({"version": 1, "agent": agent, "sources": sources}, memories, bindings)


def validate_guard(guard: Any) -> None:
    if (not isinstance(guard, dict) or set(guard) != {"version", "agent", "sources"}
            or type(guard["version"]) is not int or guard["version"] != 1
            or not isinstance(guard["agent"], str) or not guard["agent"]
            or not isinstance(guard["sources"], dict) or len(guard["sources"]) > MAX_SOURCES):
        raise ValueError("invalid_native_guard")
    for source_id, entry in guard["sources"].items():
        if (not isinstance(source_id, str) or not source_id or not isinstance(entry, dict)
                or set(entry) != {"config_hash", "file_hash"}
                or any(not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None for v in entry.values())):
            raise ValueError("invalid_native_guard")


def validate_binding(binding: Any, guard: Any, memory_id: str) -> None:
    validate_guard(guard)
    required = {"native_id", "source_id", "agent", "locator", "content_hash", "config_hash", "file_hash"}
    if (not isinstance(binding, dict) or set(binding) != required
            or any(not isinstance(v, str) or not v for v in binding.values())
            or binding["native_id"] != memory_id or re.fullmatch(r"native-[0-9a-f]{32}", memory_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", binding["content_hash"]) is None):
        raise ValueError("invalid_native_binding")
    if memory_id != _native_id(binding["source_id"], binding["locator"], binding["content_hash"]):
        raise ValueError("invalid_native_binding")
    if (guard["sources"].get(binding["source_id"]) !=
            {"config_hash": binding["config_hash"], "file_hash": binding["file_hash"]}):
        raise ValueError("invalid_native_binding")


def guard_current(service: Any, agent: str, guard: Any) -> bool:
    """Recheck sharing and full file hashes; a missing old guard proves nothing."""
    current = read_comparison(service, agent, fragments=False).guard
    if guard is None:
        return not current["sources"]
    validate_guard(guard)
    return current == guard
