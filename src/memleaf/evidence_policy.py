"""One capture-retention policy for direct, Hermes and hook-based ingress.

This module does not infer business meaning. Adapters label document payloads
from structural file arguments; no tool-name or domain keyword grants consent.
The policy applies before pending-cache and inbox writes. Already committed
knowledge and already captured inbox events are not retroactively deleted.
"""
from __future__ import annotations

from typing import Any, Mapping
from .provenance import normalize_tool_evidence


MODES = frozenset({"bounded", "metadata", "off"})


def capture_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the current capture settings; legacy names are migrated in config."""
    settings = value.get("capture", {})
    if not isinstance(settings, Mapping):
        raise ValueError("invalid memleaf capture settings")
    settings = dict(settings)
    for field in ("include_attachments", "redact_secrets", "visible_messages_only"):
        if field in settings and type(settings[field]) is not bool:
            raise ValueError("invalid memleaf capture." + field)
    mode = settings.get("tool_evidence_mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("invalid memleaf capture.tool_evidence_mode")
    settings.setdefault("include_attachments", False)
    return settings


def document_arguments(value: Any, depth: int = 0) -> bool:
    """Recognize structural file/attachment handles, not business semantics."""
    if depth > 4:
        return False
    if isinstance(value, Mapping):
        for key, item in list(value.items())[:32]:
            if key in {"path", "file", "file_path", "filepath", "filename", "attachment_id", "file_id"}:
                if isinstance(item, str) and item.strip():
                    return True
            if key == "uri" and isinstance(item, str) and item.startswith("file://"):
                return True
            if isinstance(item, (Mapping, list, tuple)) and document_arguments(item, depth + 1):
                return True
    elif isinstance(value, (list, tuple)):
        return any(document_arguments(item, depth + 1) for item in value[:32])
    return False


def retain_tool_evidence(value: Any, config: Mapping[str, Any]) -> list[dict[str, str]]:
    """Normalize/redact, then apply the same permission to every storage path."""
    policy = capture_settings(config)
    if policy["tool_evidence_mode"] == "off":
        return []
    output = []
    for record in normalize_tool_evidence(value):
        record = dict(record)
        excluded = (
            policy["tool_evidence_mode"] == "metadata"
            or record.get("retention") == "metadata"
            or (record.get("source_type") == "document" and not policy["include_attachments"])
        )
        if excluded:
            record.pop("content", None)
            record["retention"] = "metadata"
            record["completeness"] = "missing"
        output.append(record)
    return output
