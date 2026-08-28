"""Local, deterministic active-memory compaction for stage B3a."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .llm import CallableBackend, ModelError, ModelRouter, ModelUnavailable
from .locking import atomic_unlink, atomic_write_json, atomic_write_text, read_json
from .models import Memory, utc_now
from .prompts import COMPACT_SYSTEM, compact_prompt
from .validation import ModelOutputError, parse_compact_output
from .vault import safe_component


class CompactionError(RuntimeError):
    """A compaction request could not be safely committed."""


_JOURNAL_VERSION = 1
_JOURNAL_PHASES = frozenset(("staged", "histories", "replacements", "sources_removed", "committed"))


def _content_mapping(value: Memory | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Memory):
        return {
            "title": value.title,
            "body": value.body,
            "tags": list(value.tags),
            "type": value.type,
            "scopes": list(value.scopes),
            "scope_source": value.scope_source,
            "aliases": list(value.aliases),
            "keywords": list(value.keywords),
            "status": value.status,
            "completed_at": value.completed_at,
        }
    if not isinstance(value, Mapping):
        raise ValueError("memory content must be a memory or mapping")
    return {
        "title": value.get("title", ""),
        "body": value.get("body", ""),
        "tags": list(value.get("tags", [])),
        "type": value.get("type", "other"),
        "scopes": list(value.get("scopes", [])),
        "scope_source": value.get("scope_source"),
        "aliases": list(value.get("aliases", [])),
        "keywords": list(value.get("keywords", [])),
        "status": value.get("status"),
        "completed_at": value.get("completed_at"),
    }


def estimate_memory_tokens(value: Memory | Mapping[str, Any]) -> int:
    """Return a stable local estimate, not a model tokenizer count.

    The estimate serializes active content fields and rounds UTF-8 bytes up at
    four bytes per estimate unit.  It is intentionally simple and is reused by
    both ``stats()`` and compaction decisions.
    """

    payload = json.dumps(
        _content_mapping(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, math.ceil(len(payload) / 4))


def estimate_active_tokens(memories: Iterable[Memory]) -> int:
    return sum(estimate_memory_tokens(memory) for memory in memories)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock_now(clock: Any = None) -> str:
    try:
        value = clock.now() if hasattr(clock, "now") and callable(clock.now) else clock() if callable(clock) else clock
    except Exception as error:
        raise CompactionError("compaction clock failed") from error
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value
    raise CompactionError("compaction clock returned an invalid timestamp")


def _error_kind(error: BaseException) -> str:
    if isinstance(error, ModelUnavailable):
        return "unavailable"
    if isinstance(error, ModelOutputError):
        return "invalid_output"
    if isinstance(error, ModelError):
        return "model_failed"
    return "failed"


@dataclass(frozen=True)
class _Candidate:
    memory: Memory
    path: Path
    raw: str
    raw_hash: str
    tokens: int


@dataclass(frozen=True)
class _Replacement:
    memory: Memory
    raw: str
    source_ids: tuple[str, ...]
    source_tokens: int
    replacement_tokens: int


class Compactor:
    """Snapshot, validate, and safely commit active-memory replacements."""

    def __init__(self, service: Any):
        self.service = service

    def _config(self) -> tuple[int, float]:
        try:
            config = self.service.vault.config()
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise CompactionError("cannot read compaction configuration") from error
        process = config.get("process") if isinstance(config, Mapping) else None
        threshold = process.get("memory_compact_threshold_tokens") if isinstance(process, Mapping) else None
        ratio = process.get("memory_compact_candidate_ratio") if isinstance(process, Mapping) else None
        if type(threshold) is not int or threshold <= 0:
            raise CompactionError("invalid memory compaction threshold")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise CompactionError("invalid memory compaction ratio")
        if not math.isfinite(float(ratio)) or not 0 < float(ratio) <= 1:
            raise CompactionError("invalid memory compaction ratio")
        return threshold, float(ratio)

    def _resolve_backend(self, model: Any = None, router: Any = None) -> Any:
        backend = router if router is not None else model
        if backend is None:
            backend = getattr(self.service, "router", None)
        if backend is None:
            backend = ModelRouter.from_config(self.service.vault.config())
            self.service.router = backend
        if callable(backend) and not hasattr(backend, "complete"):
            backend = CallableBackend(backend)
        if not hasattr(backend, "complete"):
            raise ModelUnavailable("no model backend is configured")
        return backend

    @staticmethod
    def _candidate_sort_key(candidate: _Candidate) -> tuple[int, datetime, int, int, str]:
        hit = _parse_time(candidate.memory.last_hit_at)
        # A missing or malformed hit time is treated as never hit and is
        # therefore older than every valid hit time.
        explicit_rank = 1 if candidate.memory.extra.get("explicit_remember") is True else 0
        return (
            0 if hit is None else 1,
            hit or datetime.min.replace(tzinfo=timezone.utc),
            candidate.memory.hit_count,
            explicit_rank,
            candidate.memory.memory_id,
        )

    @staticmethod
    def _valid_hash(value: Any) -> bool:
        return isinstance(value, str) and len(value) == 64 and all(
            character in "0123456789abcdef" for character in value.casefold()
        )

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_dir():
            atomic_unlink(path)
            return
        for child in list(path.iterdir()):
            Compactor._remove_tree(child)
        try:
            path.rmdir()
        except FileNotFoundError:
            pass

    def _clear_orphan_staging_unlocked(self) -> None:
        root = self.service.vault.compaction_staging_root
        if not root.exists():
            return
        if root.is_symlink():
            raise CompactionError("unsafe compaction staging root")
        if not root.is_dir():
            raise CompactionError("invalid compaction staging root")
        for child in list(root.iterdir()):
            self._remove_tree(child)

    def _staging_dir_unlocked(self, transaction_id: str, *, create: bool) -> Path:
        safe_component(transaction_id, "compaction transaction id")
        root = self.service.vault.compaction_staging_root
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise CompactionError("unsafe compaction staging root")
        if create:
            root.mkdir(parents=True, exist_ok=True)
            try:
                root.chmod(0o700)
            except OSError:
                pass
        path = self.service.vault.compaction_staging_dir(transaction_id)
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise CompactionError("unsafe compaction staging directory")
        if create:
            path.mkdir(parents=False, exist_ok=False)
            try:
                path.chmod(0o700)
            except OSError:
                pass
        return path

    def _read_journal_unlocked(self) -> dict[str, Any] | None:
        path = self.service.vault.compaction_journal_path
        if path.is_symlink():
            raise CompactionError("unsafe compaction journal path")
        if not path.exists():
            return None
        try:
            value = read_json(path)
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise CompactionError("invalid compaction journal") from error
        if not isinstance(value, Mapping) or value.get("version") != _JOURNAL_VERSION:
            raise CompactionError("invalid compaction journal")
        transaction_id = value.get("transaction_id")
        phase = value.get("phase")
        try:
            safe_component(transaction_id, "compaction transaction id")
        except (TypeError, ValueError) as error:
            raise CompactionError("invalid compaction journal") from error
        if phase not in _JOURNAL_PHASES:
            raise CompactionError("invalid compaction journal phase")
        result: dict[str, Any] = {
            "version": _JOURNAL_VERSION,
            "transaction_id": transaction_id,
            "phase": phase,
        }
        for field in ("sources", "replacements", "histories"):
            items = value.get(field)
            if not isinstance(items, list):
                raise CompactionError("invalid compaction journal entries")
            normalized: list[dict[str, str]] = []
            seen: set[str] = set()
            for item in items:
                if not isinstance(item, Mapping):
                    raise CompactionError("invalid compaction journal entry")
                memory_id = item.get("memory_id")
                digest = item.get("sha256")
                try:
                    safe_component(memory_id, "compaction memory id")
                except (TypeError, ValueError) as error:
                    raise CompactionError("invalid compaction journal memory id") from error
                if not self._valid_hash(digest) or memory_id in seen:
                    raise CompactionError("invalid compaction journal hash")
                seen.add(memory_id)
                normalized_item = {"memory_id": memory_id, "sha256": digest.casefold()}
                if field == "sources":
                    staging_file = item.get("staging_file")
                    try:
                        safe_component(staging_file, "compaction staging file")
                    except (TypeError, ValueError) as error:
                        raise CompactionError("invalid compaction staging file") from error
                    normalized_item["staging_file"] = staging_file
                normalized.append(normalized_item)
            result[field] = normalized
        return result

    def _write_journal_unlocked(self, journal: Mapping[str, Any]) -> None:
        path = self.service.vault.compaction_journal_path
        if path.is_symlink():
            raise CompactionError("unsafe compaction journal path")
        atomic_write_json(path, dict(journal))

    def _clear_transaction_unlocked(self, journal: Mapping[str, Any]) -> None:
        path = self.service.vault.compaction_journal_path
        if path.is_symlink():
            raise CompactionError("unsafe compaction journal path")
        if path.exists():
            atomic_unlink(path)
        staging = self._staging_dir_unlocked(str(journal["transaction_id"]), create=False)
        self._remove_tree(staging)

    def _read_expected_file_unlocked(self, path: Path, expected_hash: str, label: str) -> str | None:
        if path.is_symlink():
            raise CompactionError(f"unsafe {label} path")
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise CompactionError(f"cannot read {label}") from error
        if _sha256(raw) != expected_hash:
            raise CompactionError(f"{label} changed during recovery")
        return raw

    def _rollback_pending_unlocked(self, journal: Mapping[str, Any]) -> None:
        transaction_id = str(journal["transaction_id"])
        staging = self._staging_dir_unlocked(transaction_id, create=False)
        if staging.is_symlink() or not staging.exists() or not staging.is_dir():
            raise CompactionError("missing compaction staging")
        for entry in journal["sources"]:
            source_path = self.service.vault.memory_path(entry["memory_id"], "knowledge")
            if source_path.is_symlink():
                raise CompactionError("unsafe active memory path during recovery")
            if source_path.exists():
                try:
                    current = source_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise CompactionError("cannot read active memory during recovery") from error
                # A user edit is preserved; the transaction additions below
                # are still removed so the vault returns to a stable old view.
                if _sha256(current) != entry["sha256"]:
                    continue
            else:
                staged_path = staging / entry["staging_file"]
                if staged_path.is_symlink() or not staged_path.exists():
                    raise CompactionError("missing staged active memory")
                try:
                    original = staged_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise CompactionError("cannot read staged active memory") from error
                if _sha256(original) != entry["sha256"]:
                    raise CompactionError("staged active memory hash mismatch")
                atomic_write_text(source_path, original)
        for entry in journal["replacements"]:
            path = self.service.vault.memory_path(entry["memory_id"], "knowledge")
            if self._read_expected_file_unlocked(path, entry["sha256"], "compaction replacement") is not None:
                atomic_unlink(path)
        for entry in journal["histories"]:
            path = self.service.vault.memory_path(entry["memory_id"], "history")
            if self._read_expected_file_unlocked(path, entry["sha256"], "compaction history") is not None:
                atomic_unlink(path)
        self.service._rebuild_index_unlocked()
        self._clear_transaction_unlocked(journal)

    def _recover_pending_unlocked(self) -> None:
        """Rollback only the transaction named by the short-lived journal."""

        journal = self._read_journal_unlocked()
        if journal is None:
            self._clear_orphan_staging_unlocked()
            return
        if journal["phase"] == "committed":
            self._clear_transaction_unlocked(journal)
            return
        self._rollback_pending_unlocked(journal)

    def _snapshot(self, threshold: int, ratio: float) -> tuple[list[_Candidate], list[_Candidate], int]:
        with self.service.vault.lock():
            self._recover_pending_unlocked()
            candidates: list[_Candidate] = []
            for record in self.service._read_memories_unlocked("knowledge"):
                try:
                    raw = record.path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise CompactionError("cannot read active memory") from error
                candidates.append(
                    _Candidate(
                        memory=record.memory,
                        path=record.path,
                        raw=raw,
                        raw_hash=_sha256(raw),
                        tokens=estimate_memory_tokens(record.memory),
                    )
                )
            active_tokens = sum(item.tokens for item in candidates)
            if active_tokens < threshold or not candidates:
                return [], candidates, active_tokens
            ordered = sorted(candidates, key=self._candidate_sort_key)
            count = max(1, math.ceil(len(ordered) * ratio))
            return ordered[:count], candidates, active_tokens

    @staticmethod
    def _prompt_memory(candidate: _Candidate) -> dict[str, Any]:
        value = _content_mapping(candidate.memory)
        value["memory_id"] = candidate.memory.memory_id
        return value

    @staticmethod
    def _replacement_id(summary: Mapping[str, Any], source_ids: Iterable[str]) -> str:
        material = {
            "source_memory_ids": sorted({item.casefold() for item in source_ids}),
            "replacement": {
                key: summary.get(key)
                for key in (
                    "title",
                    "body",
                    "tags",
                    "type",
                    "scopes",
                    "scope_source",
                    "aliases",
                    "keywords",
                    "status",
                    "completed_at",
                )
            },
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"mem-compact-{digest[:24]}"

    @staticmethod
    def _history_id(source: Memory, replacement_id: str, source_hash: str) -> str:
        material = {
            "original_memory_id": source.memory_id,
            "compacted_into": replacement_id,
            "source_hash": source_hash,
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"hist-{digest[:24]}"

    @staticmethod
    def _merge_sources(memories: Iterable[Memory]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for memory in memories:
            for source in memory.sources:
                if not isinstance(source, Mapping):
                    continue
                value = dict(source)
                key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if key in seen:
                    continue
                seen.add(key)
                result.append(value)
        return result

    @staticmethod
    def _first_created(memories: Iterable[Memory], now: str) -> str:
        values = [memory.created for memory in memories if isinstance(memory.created, str) and memory.created]
        return min(values) if values else now

    @staticmethod
    def _last_hit(memories: Iterable[Memory]) -> str | None:
        values = [
            (_parse_time(memory.last_hit_at), memory.last_hit_at)
            for memory in memories
            if isinstance(memory.last_hit_at, str) and _parse_time(memory.last_hit_at) is not None
        ]
        if not values:
            return None
        return max(values, key=lambda item: item[0])[1]

    def _build_replacement(
        self,
        summary: Mapping[str, Any],
        source_candidates: list[_Candidate],
        *,
        now: str,
    ) -> _Replacement:
        source_memories = [candidate.memory for candidate in source_candidates]
        source_ids = tuple(memory.memory_id for memory in source_memories)
        replacement_id = self._replacement_id(summary, source_ids)
        status = summary.get("status")
        if summary["type"] == "todo" and status is None:
            status = "active"
        extra = {
            "compaction_source_ids": list(source_ids),
            "compacted_at": now,
        }
        if any(memory.extra.get("explicit_remember") is True for memory in source_memories):
            extra["explicit_remember"] = True
        memory = Memory(
            memory_id=replacement_id,
            title=summary["title"],
            body=summary["body"],
            tags=list(summary["tags"]),
            type=summary["type"],
            scopes=list(summary["scopes"]),
            scope_source=summary["scope_source"],
            aliases=list(summary["aliases"]),
            keywords=list(summary["keywords"]),
            sources=self._merge_sources(source_memories),
            created=self._first_created(source_memories, now),
            updated=now,
            hit_count=sum(memory.hit_count for memory in source_memories),
            last_hit_at=self._last_hit(source_memories),
            status=status,
            completed_at=summary.get("completed_at"),
            extra=extra,
        )
        raw = memory.to_markdown()
        return _Replacement(
            memory=memory,
            raw=raw,
            source_ids=source_ids,
            source_tokens=sum(candidate.tokens for candidate in source_candidates),
            replacement_tokens=estimate_memory_tokens(summary),
        )

    @staticmethod
    def _memory_content_equal(left: Memory, right: Memory) -> bool:
        ignored_extra = {"compacted_at", "archived_at"}
        left_extra = {key: value for key, value in left.extra.items() if key not in ignored_extra}
        right_extra = {key: value for key, value in right.extra.items() if key not in ignored_extra}
        return (
            left.memory_id == right.memory_id
            and left.title == right.title
            and left.body == right.body
            and left.tags == right.tags
            and left.type == right.type
            and left.scopes == right.scopes
            and left.scope_source == right.scope_source
            and left.aliases == right.aliases
            and left.keywords == right.keywords
            and left.sources == right.sources
            and left.status == right.status
            and left.completed_at == right.completed_at
            and left_extra == right_extra
        )

    def _preflight(
        self,
        selected: list[_Candidate],
        all_active: list[_Candidate],
        replacements: list[_Replacement],
    ) -> None:
        selected_by_id = {candidate.memory.memory_id: candidate for candidate in selected}
        active_ids = {candidate.memory.memory_id for candidate in all_active}
        replacement_ids: set[str] = set()
        consumed: set[str] = set()
        for replacement in replacements:
            if replacement.memory.memory_id in replacement_ids:
                raise CompactionError("duplicate compaction replacement id")
            replacement_ids.add(replacement.memory.memory_id)
            if replacement.memory.memory_id in active_ids:
                raise CompactionError("compaction replacement id collides with active memory")
            for source_id in replacement.source_ids:
                if source_id in consumed:
                    raise CompactionError("compaction source memory is consumed twice")
                if source_id not in selected_by_id:
                    raise CompactionError("compaction source is outside the snapshot")
                source = selected_by_id[source_id]
                history_id = self._history_id(source.memory, replacement.memory.memory_id, source.raw_hash)
                history_path = self.service.vault.memory_path(history_id, "history")
                if history_path.is_symlink() or history_path.exists():
                    raise CompactionError("compaction history exists without a pending transaction")
                consumed.add(source_id)
        if not replacements:
            return
        selected_ids = {candidate.memory.memory_id for candidate in selected}
        if not consumed.issubset(selected_ids):
            raise CompactionError("compaction source is outside the candidate set")
        for replacement in replacements:
            path = self.service.vault.memory_path(replacement.memory.memory_id, "knowledge")
            if path.is_symlink() or path.exists():
                raise CompactionError("compaction replacement exists without a pending transaction")

    def _build_plan(
        self,
        output: Mapping[str, Any],
        selected: list[_Candidate],
        *,
        now: str,
    ) -> list[_Replacement]:
        by_id = {candidate.memory.memory_id.casefold(): candidate for candidate in selected}
        replacements: list[_Replacement] = []
        for summary in output["memories"]:
            source_ids = tuple(summary["source_memory_ids"])
            try:
                source_candidates = [by_id[source_id.casefold()] for source_id in source_ids]
            except KeyError as error:
                raise CompactionError("compaction source is outside the snapshot") from error
            replacement = self._build_replacement(summary, source_candidates, now=now)
            if replacement.replacement_tokens >= replacement.source_tokens:
                raise CompactionError("compaction replacement is not smaller than its sources")
            replacements.append(replacement)
        return replacements

    def _history_payload(
        self,
        source: _Candidate,
        replacement: _Replacement,
        *,
        now: str,
    ) -> tuple[str, Memory, str]:
        history_id = self._history_id(source.memory, replacement.memory.memory_id, source.raw_hash)
        extra = dict(source.memory.extra)
        extra.update(
            {
                "reason": "compaction",
                "original_memory_id": source.memory.memory_id,
                "compacted_into": replacement.memory.memory_id,
                "active_memory_id": replacement.memory.memory_id,
                "archived_at": now,
                "compaction_source_hash": source.raw_hash,
                "compacted_into_hash": _sha256(replacement.raw),
            }
        )
        historical = Memory(
            memory_id=history_id,
            title=source.memory.title,
            body=source.memory.body,
            tags=list(source.memory.tags),
            type=source.memory.type,
            scopes=list(source.memory.scopes),
            scope_source=source.memory.scope_source,
            aliases=list(source.memory.aliases),
            keywords=list(source.memory.keywords),
            sources=[dict(item) for item in source.memory.sources],
            created=source.memory.created,
            updated=source.memory.updated,
            hit_count=source.memory.hit_count,
            last_hit_at=source.memory.last_hit_at,
            status=source.memory.status,
            completed_at=source.memory.completed_at,
            extra=extra,
        )
        return history_id, historical, historical.to_markdown()

    def _write_history(self, source: _Candidate, replacement: _Replacement, *, now: str) -> str:
        history_id, historical, raw = self._history_payload(source, replacement, now=now)
        path = self.service.vault.memory_path(history_id, "history")
        if path.is_symlink():
            raise CompactionError("unsafe compaction history path")
        if path.exists():
            try:
                current = Memory.from_markdown(path.read_text(encoding="utf-8"), path)
            except (OSError, UnicodeError, ValueError) as error:
                raise CompactionError("existing compaction history is invalid") from error
            if not self._memory_content_equal(current, historical):
                raise CompactionError("compaction history id collision")
        else:
            atomic_write_text(path, raw)
        return history_id

    def _commit(
        self,
        selected: list[_Candidate],
        all_active: list[_Candidate],
        replacements: list[_Replacement],
        *,
        now: str,
    ) -> tuple[list[str], list[str]]:
        replacement_ids = [replacement.memory.memory_id for replacement in replacements]
        selected_by_id = {candidate.memory.memory_id: candidate for candidate in selected}
        with self.service.vault.lock():
            self._recover_pending_unlocked()
            current_active = self.service._read_memories_unlocked("knowledge")
            current_by_id = {record.memory.memory_id: record for record in current_active}
            for candidate in selected:
                record = current_by_id.get(candidate.memory.memory_id)
                if record is None:
                    raise CompactionError("active memory changed during compaction")
                try:
                    raw = record.path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise CompactionError("cannot read active memory during commit") from error
                if _sha256(raw) != candidate.raw_hash:
                    raise CompactionError("active memory changed during compaction")
            self._preflight(selected, all_active, replacements)

            history_payloads: list[tuple[_Candidate, _Replacement, str, str]] = []
            consumed_candidates: list[_Candidate] = []
            seen_sources: set[str] = set()
            for replacement in replacements:
                for source_id in replacement.source_ids:
                    source = selected_by_id[source_id]
                    history_id, _historical, history_raw = self._history_payload(source, replacement, now=now)
                    history_payloads.append((source, replacement, history_id, history_raw))
                    if source_id not in seen_sources:
                        seen_sources.add(source_id)
                        consumed_candidates.append(source)

            transaction_id = uuid.uuid4().hex
            staging = self._staging_dir_unlocked(transaction_id, create=True)
            source_entries: list[dict[str, str]] = []
            for index, source in enumerate(consumed_candidates):
                source_entries.append(
                    {
                        "memory_id": source.memory.memory_id,
                        "sha256": source.raw_hash,
                        "staging_file": f"{index:04d}-{_sha256(source.memory.memory_id)[:16]}.md",
                    }
                )
            journal: dict[str, Any] = {
                "version": _JOURNAL_VERSION,
                "transaction_id": transaction_id,
                "phase": "staged",
                "sources": source_entries,
                "replacements": [
                    {"memory_id": replacement.memory.memory_id, "sha256": _sha256(replacement.raw)}
                    for replacement in replacements
                ],
                "histories": [
                    {"memory_id": history_id, "sha256": _sha256(history_raw)}
                    for _source, _replacement, history_id, history_raw in history_payloads
                ],
            }
            try:
                for source, entry in zip(consumed_candidates, source_entries):
                    atomic_write_text(staging / entry["staging_file"], source.raw)
                self._write_journal_unlocked(journal)
                for source, replacement, _history_id_value, history_raw in history_payloads:
                    history_id = self._history_id(source.memory, replacement.memory.memory_id, source.raw_hash)
                    path = self.service.vault.memory_path(history_id, "history")
                    if path.exists() or path.is_symlink():
                        raise CompactionError("compaction history exists without a pending transaction")
                    atomic_write_text(path, history_raw)
                journal = dict(journal, phase="histories")
                self._write_journal_unlocked(journal)
                for replacement in replacements:
                    path = self.service.vault.memory_path(replacement.memory.memory_id, "knowledge")
                    if path.exists() or path.is_symlink():
                        raise CompactionError("compaction replacement exists without a pending transaction")
                    atomic_write_text(path, replacement.raw)
                journal = dict(journal, phase="replacements")
                self._write_journal_unlocked(journal)
                for source in consumed_candidates:
                    if source.path.is_symlink():
                        raise CompactionError("unsafe active memory path")
                    try:
                        current = source.path.read_text(encoding="utf-8")
                    except (OSError, UnicodeError) as error:
                        raise CompactionError("cannot read active memory before removal") from error
                    if _sha256(current) != source.raw_hash:
                        raise CompactionError("active memory changed during compaction")
                    atomic_unlink(source.path)
                journal = dict(journal, phase="sources_removed")
                self._write_journal_unlocked(journal)
                self.service._rebuild_index_unlocked()
                journal = dict(journal, phase="committed")
                self._write_journal_unlocked(journal)
                self._clear_transaction_unlocked(journal)
            except Exception:
                try:
                    self._recover_pending_unlocked()
                except Exception as recover_error:
                    raise CompactionError("compaction rollback failed") from recover_error
                raise
        return [item[2] for item in history_payloads], replacement_ids

    def _base_result(
        self,
        selected: list[_Candidate],
        all_active: list[_Candidate],
        active_tokens: int,
        threshold: int,
        ratio: float,
    ) -> dict[str, Any]:
        return {
            "status": "not_due",
            "active_tokens_before": active_tokens,
            "active_tokens_after": active_tokens,
            "threshold": threshold,
            "ratio": ratio,
            "candidates": [candidate.memory.memory_id for candidate in selected],
            "compacted": 0,
            "replacements": [],
            "history_written": [],
        }

    def _run(self, *, model: Any = None, router: Any = None, explicit: bool) -> dict[str, Any]:
        threshold, ratio = self._config()
        selected, all_active, active_tokens = self._snapshot(threshold, ratio)
        result = self._base_result(selected, all_active, active_tokens, threshold, ratio)
        if not selected:
            return result
        try:
            backend = self._resolve_backend(model=model, router=router)
            prompt = compact_prompt([self._prompt_memory(candidate) for candidate in selected])
            try:
                raw = backend.complete(
                    prompt,
                    system=COMPACT_SYSTEM,
                    purpose="compact",
                    temperature=0.0,
                )
            except ModelUnavailable:
                raise
            except ModelError:
                raise ModelError("compaction model failed")
            except Exception as error:
                raise ModelError("compaction model failed") from error
            if not isinstance(raw, str):
                raise ModelError("compaction model returned non-text output")
            output = parse_compact_output(raw, [candidate.memory.memory_id for candidate in selected])
            if not output["memories"]:
                result["status"] = "noop"
                return result
            now = _clock_now(getattr(self.service, "clock", None))
            replacements = self._build_plan(output, selected, now=now)
            self._preflight(selected, all_active, replacements)
            history_ids, replacement_ids = self._commit(
                selected,
                all_active,
                replacements,
                now=now,
            )
            with self.service.vault.lock():
                active_after = [record.memory for record in self.service._read_memories_unlocked("knowledge")]
            result.update(
                {
                    "status": "compacted",
                    "active_tokens_after": estimate_active_tokens(active_after),
                    "compacted": sum(len(replacement.source_ids) for replacement in replacements),
                    "replacements": replacement_ids,
                    "history_written": history_ids,
                }
            )
            return result
        except Exception as error:
            if explicit:
                if isinstance(error, CompactionError):
                    raise
                if isinstance(error, ModelUnavailable):
                    raise CompactionError("compaction model is unavailable") from error
                if isinstance(error, ModelOutputError):
                    raise CompactionError("invalid compaction model output") from error
                if isinstance(error, ModelError):
                    raise CompactionError("compaction model failed") from error
                raise CompactionError("compaction failed") from error
            result["status"] = _error_kind(error)
            result["error"] = {
                "unavailable": "model unavailable",
                "invalid_output": "invalid compaction model output",
                "model_failed": "compaction model failed",
            }.get(result["status"], "compaction failed")
            return result

    def compact(self, *, model: Any = None, router: Any = None) -> dict[str, Any]:
        return self._run(model=model, router=router, explicit=True)

    def auto(self, *, model: Any = None, router: Any = None) -> dict[str, Any]:
        try:
            return self._run(model=model, router=router, explicit=False)
        except Exception as error:
            status = _error_kind(error)
            return {
                "status": status,
                "error": {
                    "unavailable": "model unavailable",
                    "invalid_output": "invalid compaction model output",
                    "model_failed": "compaction model failed",
                }.get(status, "compaction failed"),
            }


__all__ = [
    "CompactionError",
    "Compactor",
    "estimate_active_tokens",
    "estimate_memory_tokens",
]
