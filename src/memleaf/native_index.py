"""Read-only, bounded indexes for configured native-memory files."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .locking import atomic_write_json, read_json


class NativeIndexError(RuntimeError):
    """The native index itself could not be read or persisted safely."""


class NativeConfigError(ValueError):
    """A native source configuration is outside the local contract."""


NATIVE_INDEX_VERSION = 1
MAX_NATIVE_BYTES = 5 * 1024 * 1024
MAX_SEGMENT_TERMS = 64
MAX_TERM_LENGTH = 48
MAX_TEXT_SEGMENT_LINES = 80
_SOURCE_ID = re.compile(r"^[^\s/\\.][^\s/\\\x00\r\n]*$")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII_TERM = re.compile(r"[a-z0-9]+")
_ALLOWED_SOURCE_FIELDS = frozenset(("agent", "path", "share", "enabled", "format"))


def empty_native_index() -> dict[str, Any]:
    return {"version": NATIVE_INDEX_VERSION, "sources": {}}


def _single_line_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise NativeConfigError(f"invalid native source {field}")
    return value


def _resolved_path(value: str | Path, *, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise NativeConfigError("invalid native source path") from error


def validate_native_sources(value: Any, *, base_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Validate config and return normalized source metadata for local use."""

    if not isinstance(value, Mapping):
        raise NativeConfigError("native_sources must be a mapping")
    root = Path(base_dir or Path.cwd()).expanduser().resolve(strict=False)
    result: dict[str, dict[str, Any]] = {}
    resolved_paths: dict[Path, str] = {}
    for raw_source_id, raw_source in value.items():
        source_id = _single_line_string(raw_source_id, "source_id")
        if not _SOURCE_ID.fullmatch(source_id) or source_id in (".", ".."):
            raise NativeConfigError("invalid native source_id")
        if not isinstance(raw_source, Mapping):
            raise NativeConfigError("native source entry must be a mapping")
        unknown = set(raw_source) - _ALLOWED_SOURCE_FIELDS
        if unknown:
            raise NativeConfigError("native source contains unknown fields")
        if "agent" not in raw_source or "path" not in raw_source or "share" not in raw_source:
            raise NativeConfigError("native source requires agent, path, and share")
        agent = _single_line_string(raw_source["agent"], "agent")
        raw_path = _single_line_string(raw_source["path"], "path")
        if type(raw_source["share"]) is not bool:
            raise NativeConfigError("native source share must be boolean")
        enabled = raw_source.get("enabled", True)
        if type(enabled) is not bool:
            raise NativeConfigError("native source enabled must be boolean")
        file_format = raw_source.get("format", "markdown")
        if file_format not in ("markdown", "text"):
            raise NativeConfigError("native source format must be markdown or text")
        resolved = _resolved_path(raw_path, base_dir=root)
        previous = resolved_paths.get(resolved)
        if previous is not None:
            raise NativeConfigError("native source paths must be unique")
        resolved_paths[resolved] = source_id
        # Existing directories are a configuration error.  Missing paths are
        # retained so refresh can report a useful, non-fatal missing status.
        raw_path_object = Path(raw_path).expanduser()
        if not raw_path_object.is_absolute():
            raw_path_object = root / raw_path_object
        if raw_path_object.exists() and raw_path_object.is_dir():
            raise NativeConfigError("native source path must be a file")
        result[source_id] = {
            "source_id": source_id,
            "agent": agent,
            "path": raw_path,
            "resolved_path": str(resolved),
            "share": raw_source["share"],
            "enabled": enabled,
            "format": file_format,
        }
    return result


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _native_id(source_id: str, locator: str, content_hash: str) -> str:
    material = json.dumps(
        {"content_hash": content_hash, "locator": locator, "source_id": source_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"native-{hashlib.sha256(material).hexdigest()[:32]}"


def _bounded_terms(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.casefold().strip()
        if not value or len(value) > MAX_TERM_LENGTH or value in seen:
            return
        seen.add(value)
        result.append(value)

    for word in _ASCII_WORD.findall(text.casefold()):
        add(word)
        if len(result) >= MAX_SEGMENT_TERMS:
            return result
    for match in _CJK_RUN.finditer(text):
        chars = list(match.group(0))
        for char in chars:
            add(char)
            if len(result) >= MAX_SEGMENT_TERMS:
                return result
        for index in range(len(chars) - 1):
            add("".join(chars[index : index + 2]))
            if len(result) >= MAX_SEGMENT_TERMS:
                return result
    return result


def _query_terms(query: Any) -> tuple[str, list[str]]:
    if isinstance(query, str):
        raw = query
    elif isinstance(query, Iterable):
        raw = " ".join(str(item) for item in query)
    else:
        return "", []
    normalized = " ".join(raw.casefold().strip().split())
    if not normalized:
        return "", []
    terms: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.casefold().strip()
        if value and value not in seen:
            seen.add(value)
            terms.append(value)

    for word in _ASCII_TERM.findall(normalized):
        add(word)
    for match in _CJK_RUN.finditer(normalized):
        chars = list(match.group(0))
        for char in chars:
            add(char)
        for index in range(len(chars) - 1):
            add("".join(chars[index : index + 2]))
    return normalized, terms


def _query_term_matches(term: str, normalized_query: str) -> bool:
    if _ASCII_TERM.fullmatch(term):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", normalized_query))
    return term in normalized_query


def _segment(source_id: str, locator: str, heading: str, lines: list[str], start: int, end: int) -> dict[str, Any]:
    content = "\n".join(lines[start - 1 : end])
    content_hash = _hash_bytes(content.encode("utf-8"))
    terms = _bounded_terms(content)
    return {
        "native_id": _native_id(source_id, locator, content_hash),
        "heading": heading,
        "locator": locator,
        "start_line": start,
        "end_line": end,
        "content_hash": content_hash,
        "normalized_terms": terms,
        "keywords": list(terms),
    }


def _markdown_segments(source_id: str, text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    if not lines:
        return []
    headings: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        match = _MARKDOWN_HEADING.match(line)
        if match:
            headings.append((number, match.group(2).strip()))
    segments: list[dict[str, Any]] = []
    if not headings:
        if any(line.strip() for line in lines):
            segments.append(_segment(source_id, "document", "", lines, 1, len(lines)))
        return segments
    if headings[0][0] > 1 and any(line.strip() for line in lines[: headings[0][0] - 1]):
        segments.append(_segment(source_id, "preamble", "", lines, 1, headings[0][0] - 1))
    for position, (start, heading) in enumerate(headings, start=1):
        end = headings[position][0] - 1 if position < len(headings) else len(lines)
        segments.append(_segment(source_id, f"heading:{position}", heading, lines, start, end))
    return segments


def _text_segments(source_id: str, text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    if not lines or not any(line.strip() for line in lines):
        return []
    segments: list[dict[str, Any]] = []
    for offset in range(0, len(lines), MAX_TEXT_SEGMENT_LINES):
        start = offset + 1
        end = min(offset + MAX_TEXT_SEGMENT_LINES, len(lines))
        segments.append(_segment(source_id, f"chunk:{offset // MAX_TEXT_SEGMENT_LINES + 1}", "", lines, start, end))
    return segments


def _stat_source(path: Path) -> tuple[str, int | None, int | None]:
    if path.is_symlink():
        return "unsafe", None, None
    if not path.exists():
        return "missing", None, None
    if path.is_dir():
        return "directory", None, None
    try:
        stat = path.stat()
    except OSError:
        return "unreadable", None, None
    if stat.st_size > MAX_NATIVE_BYTES:
        return "too_large", stat.st_mtime_ns, stat.st_size
    return "ready", stat.st_mtime_ns, stat.st_size


def _source_entry(
    source: Mapping[str, Any],
    *,
    availability: str,
    mtime_ns: int | None,
    size: int | None,
    file_hash: str | None = None,
    segments: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    error_category = None if availability == "available" else availability
    return {
        "source_id": source["source_id"],
        "agent": source["agent"],
        "path": source["resolved_path"],
        "mtime_ns": mtime_ns,
        "size": size,
        "file_hash": file_hash,
        "format": source["format"],
        "share": source["share"],
        "enabled": source["enabled"],
        "availability": availability,
        "error_category": error_category,
        "segments": [dict(item) for item in segments],
    }


class NativeIndexer:
    """Build and read one vault's native index; callers hold the vault lock."""

    def __init__(self, vault: Any):
        self.vault = vault

    def _load_index_unlocked(self) -> dict[str, Any]:
        path = self.vault.native_sources_index_path
        if path.is_symlink():
            raise NativeIndexError("unsafe native index path")
        try:
            value = read_json(path)
        except (OSError, UnicodeError, TypeError, ValueError):
            return empty_native_index()
        if not isinstance(value, Mapping) or value.get("version") != NATIVE_INDEX_VERSION:
            return empty_native_index()
        sources = value.get("sources")
        if not isinstance(sources, Mapping):
            return empty_native_index()
        return {"version": NATIVE_INDEX_VERSION, "sources": deepcopy(dict(sources))}

    @staticmethod
    def _config_changed(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        return any(
            previous.get(previous_key) != current.get(current_key)
            for previous_key, current_key in (
                ("agent", "agent"),
                ("path", "resolved_path"),
                ("format", "format"),
                ("share", "share"),
                ("enabled", "enabled"),
            )
        )

    def _read_source(
        self,
        source: Mapping[str, Any],
        *,
        previous_segments: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        raw_path = Path(source["path"]).expanduser()
        if not raw_path.is_absolute():
            raw_path = self.vault.root / raw_path
        availability, mtime_ns, size = _stat_source(raw_path)
        if not source["enabled"]:
            return _source_entry(
                source,
                availability="disabled",
                mtime_ns=mtime_ns,
                size=size,
            )
        if availability != "ready":
            return _source_entry(
                source,
                availability=availability,
                mtime_ns=mtime_ns,
                size=size,
            )
        try:
            data = raw_path.read_bytes()
        except OSError:
            return _source_entry(source, availability="unreadable", mtime_ns=mtime_ns, size=size)
        if len(data) > MAX_NATIVE_BYTES:
            return _source_entry(source, availability="too_large", mtime_ns=mtime_ns, size=len(data))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _source_entry(source, availability="decode_error", mtime_ns=mtime_ns, size=size)
        digest = _hash_bytes(data)
        segments = (
            _markdown_segments(source["source_id"], text)
            if source["format"] == "markdown"
            else _text_segments(source["source_id"], text)
        )
        old_by_id = {
            item.get("native_id"): item
            for item in previous_segments
            if isinstance(item, Mapping) and isinstance(item.get("native_id"), str)
        }
        for segment in segments:
            old = old_by_id.get(segment["native_id"])
            shadowed_by = old.get("shadowed_by") if isinstance(old, Mapping) else None
            if isinstance(shadowed_by, str) and shadowed_by:
                segment["shadowed_by"] = shadowed_by
        return _source_entry(
            source,
            availability="available",
            mtime_ns=mtime_ns,
            size=size,
            file_hash=digest,
            segments=segments,
        )

    def _refresh_unlocked(self, *, full: bool = False, force_sources: set[str] | None = None) -> dict[str, Any]:
        try:
            config = self.vault.config()
            configured = validate_native_sources(
                config.get("native_sources", {}),
                base_dir=self.vault.root,
            )
        except NativeConfigError as error:
            raise NativeIndexError("invalid native_sources configuration") from error
        previous = self._load_index_unlocked()
        previous_sources = previous.get("sources", {})
        if not isinstance(previous_sources, Mapping):
            previous_sources = {}
        sources: dict[str, dict[str, Any]] = {}
        changed: list[str] = []
        forced = force_sources or set()
        for source_id, source in configured.items():
            old = previous_sources.get(source_id)
            if not isinstance(old, Mapping):
                old = None
            raw_path = Path(source["path"]).expanduser()
            if not raw_path.is_absolute():
                raw_path = self.vault.root / raw_path
            availability, mtime_ns, size = _stat_source(raw_path)
            can_reuse = (
                not full
                and source_id not in forced
                and old is not None
                and not self._config_changed(old, source)
                and old.get("mtime_ns") == mtime_ns
                and old.get("size") == size
            )
            if can_reuse:
                sources[source_id] = deepcopy(dict(old))
                continue
            changed.append(source_id)
            if availability == "ready" and source["enabled"]:
                previous_segments = []
                if isinstance(old, Mapping) and old.get("path") == source["resolved_path"]:
                    previous_segments = old.get("segments", [])
                sources[source_id] = self._read_source(
                    source,
                    previous_segments=previous_segments if isinstance(previous_segments, list) else [],
                )
            elif not source["enabled"]:
                sources[source_id] = _source_entry(
                    source,
                    availability="disabled",
                    mtime_ns=mtime_ns,
                    size=size,
                )
            else:
                sources[source_id] = _source_entry(
                    source,
                    availability=availability,
                    mtime_ns=mtime_ns,
                    size=size,
                )
        result = {"version": NATIVE_INDEX_VERSION, "sources": sources}
        if changed or set(previous_sources) != set(sources) or full:
            try:
                atomic_write_json(self.vault.native_sources_index_path, result)
            except OSError as error:
                raise NativeIndexError("native index write failed") from error
        return self.summary(result, changed_sources=changed)

    @staticmethod
    def summary(index: Mapping[str, Any], *, changed_sources: Iterable[str] = ()) -> dict[str, Any]:
        sources = index.get("sources", {}) if isinstance(index, Mapping) else {}
        if not isinstance(sources, Mapping):
            sources = {}
        segments = 0
        unavailable = 0
        for value in sources.values():
            if not isinstance(value, Mapping):
                unavailable += 1
                continue
            items = value.get("segments", [])
            if isinstance(items, list):
                segments += len(items)
            if value.get("availability") != "available":
                unavailable += 1
        changed = list(changed_sources)
        return {
            "native_sources": len(sources),
            "native_segments": segments,
            "native_unavailable": unavailable,
            "changed_sources": changed,
        }

    def refresh_unlocked(self, *, full: bool = False) -> dict[str, Any]:
        return self._refresh_unlocked(full=full)

    def refresh(self, *, full: bool = False) -> dict[str, Any]:
        with self.vault.lock():
            return self.refresh_unlocked(full=full)

    def _read_segment_current_unlocked(self, source_id: str, native_id: str) -> str | None:
        if not isinstance(source_id, str) or not isinstance(native_id, str):
            return None
        index = self._load_index_unlocked()
        sources = index.get("sources", {})
        entry = sources.get(source_id) if isinstance(sources, Mapping) else None
        if not isinstance(entry, Mapping) or entry.get("availability") != "available":
            return None
        try:
            config = validate_native_sources(
                self.vault.config().get("native_sources", {}),
                base_dir=self.vault.root,
            )
        except NativeConfigError as error:
            raise NativeIndexError("invalid native_sources configuration") from error
        source = config.get(source_id)
        if source is None or source["resolved_path"] != entry.get("path"):
            return None
        raw_path = Path(source["path"]).expanduser()
        if not raw_path.is_absolute():
            raw_path = self.vault.root / raw_path
        availability, mtime_ns, size = _stat_source(raw_path)
        if availability != "ready" or mtime_ns != entry.get("mtime_ns") or size != entry.get("size"):
            return None
        try:
            data = raw_path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if _hash_bytes(data) != entry.get("file_hash"):
            return None
        segments = entry.get("segments", [])
        if not isinstance(segments, list):
            return None
        target = next(
            (item for item in segments if isinstance(item, Mapping) and item.get("native_id") == native_id),
            None,
        )
        if target is None:
            return None
        locator = target.get("locator")
        content_hash = target.get("content_hash")
        if not isinstance(locator, str) or not isinstance(content_hash, str):
            return None
        if _native_id(source_id, locator, content_hash) != native_id:
            return None
        start = target.get("start_line")
        end = target.get("end_line")
        lines = text.splitlines()
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 1
            or end < start
            or end > len(lines)
        ):
            return None
        content = "\n".join(lines[start - 1 : end])
        if _hash_bytes(content.encode("utf-8")) != content_hash:
            return None
        return content

    def _read_segment_unlocked(self, source_id: str, native_id: str) -> str | None:
        self._refresh_unlocked()
        return self._read_segment_current_unlocked(source_id, native_id)

    def read_shared_segment_by_id_unlocked(self, native_id: str) -> dict[str, Any] | None:
        """Resolve one shared native id after current-file validation.

        Native files remain source-of-truth and are never written here.  The
        configured share flag is checked again at read time so an id copied
        from an older directory cannot bypass current isolation settings.
        """

        if not isinstance(native_id, str) or not native_id:
            return None
        self._refresh_unlocked()
        index = self._load_index_unlocked()
        sources = index.get("sources", {})
        if not isinstance(sources, Mapping):
            return None
        source_ids = sorted(source_id for source_id in sources if isinstance(source_id, str))
        for source_id in source_ids:
            entry = sources.get(source_id)
            if not isinstance(entry, Mapping):
                continue
            if not self._source_allowed(entry, None, for_context=False):
                continue
            segments = entry.get("segments", [])
            if not isinstance(segments, list):
                continue
            target = next(
                (
                    item
                    for item in segments
                    if isinstance(item, Mapping) and item.get("native_id") == native_id
                ),
                None,
            )
            if target is None:
                continue
            body = self._read_segment_current_unlocked(source_id, native_id)
            if body is None:
                continue
            title = target.get("heading") or source_id
            if not isinstance(title, str) or not title:
                title = source_id
            return {
                "memory_id": native_id,
                "title": title,
                "scopes": ["global"],
                "body": body,
            }
        return None

    @staticmethod
    def _source_allowed(entry: Mapping[str, Any], target_agent: str | None, *, for_context: bool) -> bool:
        if entry.get("enabled") is not True or entry.get("availability") != "available":
            return False
        share = entry.get("share") is True
        if target_agent is None:
            return share
        is_own = entry.get("agent") == target_agent
        if for_context and is_own:
            return False
        return is_own or share

    def search_unlocked(
        self,
        query: Any,
        *,
        target_agent: str | None = None,
        for_context: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded native segment bodies without exposing index internals."""

        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise ValueError("native search limit must be a non-negative integer")
        normalized_query, query_values = _query_terms(query)
        # Refresh is intentionally first, including for an empty query.  The
        # mtime/size fast path does not read or hash unchanged files.
        self._refresh_unlocked()
        if not normalized_query or not query_values:
            return []
        if limit == 0:
            return []
        index = self._load_index_unlocked()
        sources = index.get("sources", {})
        if not isinstance(sources, Mapping):
            return []
        candidates: list[tuple[int, str, str, Mapping[str, Any], Mapping[str, Any]]] = []
        for source_id, raw_entry in sources.items():
            if not isinstance(source_id, str) or not isinstance(raw_entry, Mapping):
                continue
            if not self._source_allowed(raw_entry, target_agent, for_context=for_context):
                continue
            segments = raw_entry.get("segments", [])
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if not isinstance(segment, Mapping) or segment.get("shadowed_by"):
                    continue
                terms = segment.get("normalized_terms", segment.get("keywords", []))
                if not isinstance(terms, list):
                    continue
                normalized_terms = dict.fromkeys(
                    item.casefold() for item in terms if isinstance(item, str)
                )
                native_id = segment.get("native_id")
                locator = segment.get("locator")
                score = sum(
                    1
                    for term in normalized_terms
                    if _query_term_matches(term, normalized_query)
                )
                if isinstance(native_id, str) and normalized_query == native_id.casefold():
                    score = max(score, 100000)
                if score and isinstance(native_id, str) and isinstance(locator, str):
                    candidates.append((score, source_id, locator, raw_entry, segment))
        candidates.sort(
            key=lambda item: (-item[0], item[1], item[2], str(item[4].get("native_id", "")))
        )
        results: list[dict[str, Any]] = []
        for _, source_id, locator, entry, segment in candidates:
            native_id = segment["native_id"]
            body = self._read_segment_unlocked(source_id, native_id)
            if body is None:
                continue
            results.append(
                {
                    "memory_id": native_id,
                    "native_id": native_id,
                    "native": True,
                    "source": source_id,
                    "native_source_id": source_id,
                    "agent": entry.get("agent", ""),
                    "native_agent": entry.get("agent", ""),
                    "locator": locator,
                    "title": segment.get("heading") or source_id,
                    "body": body,
                    "scopes": ["global"],
                    "share": entry.get("share") is True,
                }
            )
            if limit is not None and len(results) >= limit:
                break
        return results

    def search(
        self,
        query: Any,
        *,
        target_agent: str | None = None,
        for_context: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.vault.lock():
            return self.search_unlocked(
                query,
                target_agent=target_agent,
                for_context=for_context,
                limit=limit,
            )

    def apply_shadow_unlocked(self, references: Iterable[Mapping[str, Any]], memory_id: str) -> None:
        """Atomically mark current indexed segments as shadowed by a memory."""

        if not isinstance(memory_id, str) or not memory_id or "/" in memory_id or "\\" in memory_id:
            raise NativeIndexError("invalid shadow memory id")
        refs = [dict(item) for item in references if isinstance(item, Mapping)]
        if not refs:
            return
        native_ids = [item.get("native_id") for item in refs]
        if any(not isinstance(value, str) or not value for value in native_ids):
            raise NativeIndexError("invalid shadow native id")
        if len({value.casefold() for value in native_ids}) != len(native_ids):
            raise NativeIndexError("duplicate shadow native id")
        for item in refs:
            source_id = item.get("source_id", item.get("native_source_id"))
            native_id = item.get("native_id")
            if not isinstance(source_id, str) or not isinstance(native_id, str):
                raise NativeIndexError("invalid shadow reference")
            if self._read_segment_unlocked(source_id, native_id) is None:
                raise NativeIndexError("native segment changed before shadow commit")
        index = self._load_index_unlocked()
        sources = index.get("sources", {})
        changed = False
        for item in refs:
            source_id = item.get("source_id", item.get("native_source_id"))
            native_id = item.get("native_id")
            entry = sources.get(source_id) if isinstance(sources, Mapping) else None
            segments = entry.get("segments", []) if isinstance(entry, Mapping) else []
            target = next(
                (
                    segment
                    for segment in segments
                    if isinstance(segment, dict) and segment.get("native_id") == native_id
                ),
                None,
            )
            if target is None:
                raise NativeIndexError("native segment disappeared before shadow commit")
            current = target.get("shadowed_by")
            if current and current != memory_id:
                raise NativeIndexError("native segment already shadowed")
            if current != memory_id:
                target["shadowed_by"] = memory_id
                changed = True
        if changed:
            atomic_write_json(self.vault.native_sources_index_path, index)

    def apply_shadow(self, references: Iterable[Mapping[str, Any]], memory_id: str) -> None:
        with self.vault.lock():
            self.apply_shadow_unlocked(references, memory_id)

    def read_segment(self, source_id: str, native_id: str) -> str | None:
        with self.vault.lock():
            return self._read_segment_unlocked(source_id, native_id)

    read_locator = read_segment


def refresh_native_sources(vault: Any, *, full: bool = False) -> dict[str, Any]:
    return NativeIndexer(vault).refresh(full=full)


def read_native_segment(vault: Any, source_id: str, native_id: str) -> str | None:
    return NativeIndexer(vault).read_segment(source_id, native_id)


read_native_locator = read_native_segment


__all__ = [
    "MAX_NATIVE_BYTES",
    "MAX_SEGMENT_TERMS",
    "NativeConfigError",
    "NativeIndexError",
    "NativeIndexer",
    "empty_native_index",
    "read_native_locator",
    "read_native_segment",
    "refresh_native_sources",
    "validate_native_sources",
]
