"""Small data objects shared by the local core."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .frontmatter import FrontmatterError, dump_frontmatter, parse_frontmatter


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"memory {field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"memory {field_name} must contain strings")
    return list(value)


def _sources(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("memory sources must be a list")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("memory sources must contain mappings")
        result.append(dict(item))
    return result


class MemoryVersionError(ValueError):
    """The caller's page version no longer matches source memory content."""

    code = "memory_version_changed"


@dataclass
class Memory:
    memory_id: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    type: str = "other"
    scopes: list[str] = field(default_factory=lambda: ["global"])
    aliases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    scope_source: Optional[str] = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    created: str = field(default_factory=utc_now)
    updated: str = field(default_factory=utc_now)
    hit_count: int = 0
    last_hit_at: Optional[str] = None
    status: Optional[str] = None
    completed_at: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not self.memory_id:
            raise ValueError("memory_id is required")
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("memory title is required")
        if not isinstance(self.body, str):
            raise ValueError("memory body must be text")
        self.tags = _string_list(self.tags, "tags")
        self.scopes = _string_list(self.scopes, "scopes") or ["global"]
        self.aliases = _string_list(self.aliases, "aliases")
        self.keywords = _string_list(self.keywords, "keywords")
        self.sources = _sources(self.sources)
        if not isinstance(self.type, str) or not self.type:
            raise ValueError("memory type must be a string")
        if not isinstance(self.hit_count, int) or isinstance(self.hit_count, bool) or self.hit_count < 0:
            raise ValueError("memory hit_count must be a non-negative integer")
        if self.status is not None and not isinstance(self.status, str):
            raise ValueError("memory status must be a string")
        if self.completed_at is not None and not isinstance(self.completed_at, str):
            raise ValueError("memory completed_at must be a string")

    @classmethod
    def new(
        cls,
        *,
        title: str,
        body: str,
        memory_id: Optional[str] = None,
        tags: Any = None,
        type: str = "other",
        scopes: Any = None,
        aliases: Any = None,
        keywords: Any = None,
        **metadata: Any,
    ) -> "Memory":
        identifier = memory_id or f"mem-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        now = utc_now()
        return cls(
            memory_id=identifier,
            title=title,
            body=body,
            tags=[] if tags is None else tags,
            type=type,
            scopes=["global"] if scopes is None else scopes,
            aliases=[] if aliases is None else aliases,
            keywords=[] if keywords is None else keywords,
            created=metadata.pop("created", now),
            updated=metadata.pop("updated", now),
            scope_source=metadata.pop("scope_source", None),
            sources=metadata.pop("sources", []),
            hit_count=metadata.pop("hit_count", 0),
            last_hit_at=metadata.pop("last_hit_at", None),
            status=metadata.pop("status", None),
            completed_at=metadata.pop("completed_at", None),
            extra=metadata,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Memory":
        if not isinstance(value, Mapping):
            raise ValueError("memory must be a mapping")
        known = {
            "memory_id",
            "title",
            "body",
            "tags",
            "type",
            "scopes",
            "aliases",
            "keywords",
            "scope_source",
            "sources",
            "created",
            "updated",
            "hit_count",
            "last_hit_at",
            "status",
            "completed_at",
        }
        missing = [key for key in ("memory_id", "title", "body") if key not in value]
        if missing:
            raise ValueError("memory is missing required fields")
        return cls(
            memory_id=value["memory_id"],
            title=value["title"],
            body=value["body"],
            tags=value.get("tags", []),
            type=value.get("type", "other"),
            scopes=value.get("scopes", ["global"]),
            aliases=value.get("aliases", []),
            keywords=value.get("keywords", []),
            scope_source=value.get("scope_source"),
            sources=value.get("sources", []),
            created=value.get("created", utc_now()),
            updated=value.get("updated", utc_now()),
            hit_count=value.get("hit_count", 0),
            last_hit_at=value.get("last_hit_at"),
            status=value.get("status"),
            completed_at=value.get("completed_at"),
            extra={key: item for key, item in value.items() if key not in known},
        )

    @classmethod
    def from_markdown(cls, text: str, path: Optional[Path] = None) -> "Memory":
        try:
            metadata, body = parse_frontmatter(text)
        except FrontmatterError as error:
            raise ValueError("invalid memory Markdown frontmatter") from error
        value = dict(metadata)
        value["body"] = body
        if "memory_id" not in value and path is not None:
            value["memory_id"] = path.stem
        if "title" not in value:
            value["title"] = value.get("memory_id", path.stem if path else "memory")
        return cls.from_mapping(value)

    def frontmatter(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        metadata["title"] = self.title
        metadata["memory_id"] = self.memory_id
        metadata["tags"] = list(self.tags)
        metadata["type"] = self.type
        metadata["scopes"] = list(self.scopes)
        if self.aliases:
            metadata["aliases"] = list(self.aliases)
        if self.keywords:
            metadata["keywords"] = list(self.keywords)
        if self.scope_source is not None:
            metadata["scope_source"] = self.scope_source
        metadata["sources"] = [dict(item) for item in self.sources]
        metadata["created"] = self.created
        metadata["updated"] = self.updated
        metadata["hit_count"] = self.hit_count
        if self.last_hit_at is not None:
            metadata["last_hit_at"] = self.last_hit_at
        if self.status is not None:
            metadata["status"] = self.status
        if self.completed_at is not None:
            metadata["completed_at"] = self.completed_at
        for key, value in self.extra.items():
            metadata.setdefault(key, value)
        return metadata

    def to_markdown(self) -> str:
        return dump_frontmatter(self.frontmatter(), self.body)

    def to_dict(self) -> dict[str, Any]:
        value = dict(self.frontmatter())
        value["body"] = self.body
        return value

    def __getitem__(self, key: str) -> Any:
        if key == "body":
            return self.body
        if hasattr(self, key):
            return getattr(self, key)
        return self.extra[key]


@dataclass(frozen=True)
class DirectoryEntry:
    """The small, body-free shape used for automatic memory recall."""

    memory_id: str
    title: str
    scopes: list[str] = field(default_factory=lambda: ["global"])

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not self.memory_id:
            raise ValueError("directory memory_id is required")
        if not isinstance(self.title, str) or not self.title:
            raise ValueError("directory title is required")
        scopes = _string_list(self.scopes, "scopes") or ["global"]
        object.__setattr__(self, "scopes", scopes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "title": self.title,
            "scopes": list(self.scopes),
        }

    def __getitem__(self, key: str) -> Any:
        if key == "memory_id":
            return self.memory_id
        if key == "title":
            return self.title
        if key == "scopes":
            return self.scopes
        raise KeyError(key)


@dataclass(frozen=True)
class CaptureResult:
    event_id: str
    stored: bool
    duplicate: bool
    path: Optional[Path] = None
    content: str = ""

    def __bool__(self) -> bool:
        return self.stored


@dataclass
class ForgetAboutResult:
    status: str
    deleted: list[str] = field(default_factory=list)
    candidates: list[Memory] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def deleted_memory_ids(self) -> list[str]:
        return list(self.deleted)
