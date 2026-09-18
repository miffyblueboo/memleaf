"""Read-only, bounded Markdown snapshots for public retrieval.

This is not a second index or a repairer. Invalid files and ambiguous identities
are isolated, never picked as winners. Only independently valid scope metadata
can narrow a diagnostic; directory names and prose cannot supply its scope.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter
from .models import Memory
from .retrieval import inherited_scopes, memory_scope_rank, RetrievalError
from .scope_state import normalize_scopes
from .vault import safe_component

MAX_FILES = 20000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass
class ScanRecord:
    memory: Memory
    path: Path
    area: str
    score: int = 0
    scope_rank: int = 0
    account_ready: bool = True


@dataclass(frozen=True)
class ScanIssue:
    code: str
    locator: str
    scopes: tuple[str, ...] | None = None
    identity: str | None = None


@dataclass(frozen=True)
class _ValidatedFile:
    """Request-local proof for exact bytes, never a persisted or stat-only cache."""
    raw_digest: str
    token: str
    scopes: tuple[str, ...]
    identity: str


@dataclass
class QueryScan:
    records: list[ScanRecord]
    issues: list[ScanIssue]
    ambiguous: set[str]
    generation: str
    areas: tuple[str, ...]
    # Immutable parse facts, not the mutable Memory/ScanRecord shown to callers.
    # Retained only until this observation has been checked before returning.
    _validated: dict[tuple[str, str], _ValidatedFile] = field(default_factory=dict, repr=False, compare=False)
    # Exact Forget must enumerate all valid copies of an authorized ID, not
    # select a winner from the public query quarantine. Ordinary consumers use
    # records only; invalid files remain failures, never deletion candidates.
    _all_records: list[ScanRecord] = field(default_factory=list, repr=False, compare=False)

    def area(self, name: str) -> list[ScanRecord]:
        return [r for r in self.records if r.area == name]

    def require_identity(self, identity: str) -> None:
        if identity.casefold() in self.ambiguous:
            raise RetrievalError("memory_id_conflict", "multiple records claim this identity")
        if any(i.identity and i.identity.casefold() == identity.casefold() for i in self.issues):
            raise RetrievalError("memory_unreadable", "requested memory could not be validated")

    def report(self, scope=None, config=None) -> dict[str, Any]:
        allowed = inherited_scopes(scope, config or {}) if scope is not None else None
        relevant = []
        for issue in self.issues:
            if allowed is None or issue.scopes is None:
                relevant.append(issue)
            else:
                probe = Memory("diagnostic", "diagnostic", "", scopes=list(issue.scopes))
                if memory_scope_rank(probe, allowed) >= 0:
                    relevant.append(issue)
        codes = Counter(i.code for i in relevant)
        return {"status": "partial" if relevant else "complete",
                "issue_count": len(relevant), "codes": dict(sorted(codes.items())),
                "areas": list(self.areas)}


def markdown_paths(root: Path, area: str) -> tuple[list[Path], list[ScanIssue]]:
    """Enumerate without following links, retaining enumeration failures."""
    base = root / area
    issues: list[ScanIssue] = []
    def issue(path, code):
        issues.append(ScanIssue(code, hashlib.sha256(str(path).encode()).hexdigest()))
    if base.is_symlink():
        issue(base, "unsafe_path")
        return [], issues
    if not base.exists():
        return [], issues
    if not base.is_dir():
        issue(base, "unreadable_path")
        return [], issues
    paths = []
    for directory, dirs, files in os.walk(base, followlinks=False,
            onerror=lambda error: issue(error.filename or base, "unreadable_path")):
        for name in list(dirs):
            if len(paths) + len(issues) >= MAX_FILES:
                issue(base, "scan_limit")
                return sorted(paths), issues
            child = Path(directory) / name
            if child.is_symlink():
                dirs.remove(name)
                issue(child, "unsafe_path")
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            path = Path(directory) / name
            if len(paths) >= MAX_FILES:
                issue(base, "scan_limit")
                return sorted(paths), issues
            paths.append(path)
    return sorted(paths), issues


def scan_memories(vault, include_history: bool = False) -> QueryScan:
    return _scan_memories(vault, include_history)


def _scan_memories(vault, include_history: bool, *,
                   reuse: dict[tuple[str, str], _ValidatedFile] | None = None) -> QueryScan:
    """Recheck every path and byte; reuse only a matching validated parse.

    A verification scan needs fingerprints/claims, not a second set of Memory
    objects. Changed bytes and every malformed record use the normal parser.
    """
    areas = ("knowledge", "history") if include_history else ("knowledge",)
    records: list[ScanRecord] = []
    issues: list[ScanIssue] = []
    stamps = []
    validated: dict[tuple[str, str], _ValidatedFile] = {}
    total = 0
    claims: dict[str, list[tuple[str, tuple[str, ...] | None]]] = defaultdict(list)
    for area in areas:
        paths, failures = markdown_paths(vault.root, area)
        issues.extend(failures)
        for path in paths:
            locator = hashlib.sha256(str(path.relative_to(vault.root)).encode()).hexdigest()
            scopes = None
            identity = None
            raw_digest = None
            try:
                if path.resolve() != path.absolute():
                    raise ValueError("unsafe_path")
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or path.is_symlink():
                    raise ValueError("unsafe_path")
                if before.st_size > MAX_FILE_BYTES or total + before.st_size > MAX_TOTAL_BYTES:
                    raise ValueError("scan_limit")
                with path.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise ValueError("scan_changed")
                    raw = stream.read(MAX_FILE_BYTES + 1)
                total += len(raw)
                if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError("scan_limit")
                raw_digest = hashlib.sha256(raw).hexdigest()
                after = path.lstat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError("scan_changed")
                cached = reuse.get((area, locator)) if reuse is not None else None
                if cached is not None and cached.raw_digest == raw_digest:
                    # This shortcut occurs after all normal path, type, size,
                    # open-file identity and before/after change checks.
                    claims[cached.identity.casefold()].append((locator, cached.scopes))
                    stamps.append((area, locator, cached.token))
                    continue
                meta, body = parse_frontmatter(raw.decode("utf-8"))
                candidate = meta.get("memory_id", path.stem)
                if isinstance(candidate, str):
                    try:
                        safe_component(candidate, "memory id")
                        identity = candidate
                    except ValueError:
                        pass
                # Unknown/invalid scope never becomes a guessed project.
                try:
                    scopes = tuple(normalize_scopes(meta.get("scopes", ["global"])))
                except (ValueError, TypeError):
                    scopes = None
                data = dict(meta)
                data.setdefault("memory_id", path.stem)
                data.setdefault("title", data["memory_id"])
                data["body"] = body
                ready = all(isinstance(meta.get(k), str) and meta[k] for k in ("created", "updated"))
                # Unknown legacy times stay unknown in this transient view; no
                # processing-time defaults enter ordering, cursors or hit writes.
                data.setdefault("created", "")
                data.setdefault("updated", "")
                memory = Memory.from_mapping(data)
                safe_component(memory.memory_id, "memory id")
                if scopes is None or any(not isinstance(getattr(memory, k), str) for k in ("created", "updated")):
                    raise ValueError("invalid_memory")
                stable = memory.to_dict()
                stable.pop("hit_count", None)
                stable.pop("last_hit_at", None)
                token = digest(stable)
                if reuse is None:
                    records.append(ScanRecord(memory, path, area, account_ready=ready))
                    validated[(area, locator)] = _ValidatedFile(raw_digest, token, scopes, identity)
                stamps.append((area, locator, token))
            except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as error:
                code = str(error) if str(error) in {"unsafe_path", "scan_limit", "scan_changed"} else "invalid_memory"
                issues.append(ScanIssue(code, locator, scopes, identity))
                stamps.append((area, locator, raw_digest, code))
            if identity:
                claims[identity.casefold()].append((locator, scopes))
    ambiguous = {key for key, values in claims.items() if len(values) > 1}
    for identity in sorted(ambiguous):
        for locator, scopes in claims[identity]:
            issues.append(ScanIssue("duplicate_id", locator, scopes, identity))
    all_records = records
    records = [r for r in records if r.memory.memory_id.casefold() not in ambiguous]
    generation = digest({"files": stamps, "issues": [(i.code, i.locator, i.scopes) for i in issues]})
    return QueryScan(records, issues, ambiguous, generation, areas, validated, all_records)


def ensure_scan_current(vault, snapshot: QueryScan) -> None:
    """One bounded recheck, never an unbounded retry or repair."""
    latest = _scan_memories(vault, "history" in snapshot.areas, reuse=snapshot._validated)
    if latest.generation != snapshot.generation:
        raise RetrievalError("scan_changed", "memory files changed during retrieval; repeat query")
