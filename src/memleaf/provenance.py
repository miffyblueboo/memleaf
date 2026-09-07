"""Bounded, redacted tool observations; metadata is not proof of content."""
from __future__ import annotations
import hashlib
import re
from typing import Any, Mapping
from .evidence_budget import (
    DEFAULT_MAX_RECORD_BYTES,
    DEFAULT_MAX_RECORDS,
    apply_evidence_budget,
    is_overflow_marker,
)
from .redaction import redact_text

TOOL_EVIDENCE_FIELDS = frozenset({"message_id", "subject", "sender", "domain",
    "tool_name", "call_id", "record_id", "title", "kind", "result_status",
    "content", "result_digest", "execution_status", "completeness", "schema_version",
    "omitted_count", "omitted_bytes", "source_type", "retention"})
MAX_ITEMS = DEFAULT_MAX_RECORDS
MAX_TEXT = 320
# Kept as an exported compatibility name; bodies are now bounded in UTF-8
# bytes by the shared budget rather than by Python character count.
MAX_CONTENT = DEFAULT_MAX_RECORD_BYTES
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)


def normalize_tool_evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("tool evidence must be a list")
    result: list[dict[str, str]] = []
    seen = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) - TOOL_EVIDENCE_FIELDS:
            raise ValueError("invalid tool evidence record")
        item: dict[str, str] = {}
        for key, field in raw.items():
            if field is None:
                continue
            if not isinstance(field, str) or not field.strip() or "\x00" in field:
                raise ValueError("invalid tool evidence field")
            if key != "content" and any(ch in field for ch in "\r\n"):
                raise ValueError("invalid tool evidence field")
            text = redact_text(field.strip())
            # Metadata keeps its historic character bound.  Body text is
            # handled once below by the shared UTF-8 byte budget.
            item[key] = text if key == "content" else text[:MAX_TEXT]
            if key == "domain":
                item[key] = text.casefold().lstrip("@")
                if not DOMAIN_RE.fullmatch(item[key]):
                    raise ValueError("invalid tool evidence domain")
        if "content" in item:
            # Recompute after redaction. A caller-supplied digest cannot assert
            # that a different body was observed.
            item["result_digest"] = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
        if "kind" in item and item["kind"] not in {"external_observation", "retrieved_memory", "unknown"}:
            raise ValueError("invalid tool evidence kind")
        if "result_status" in item and item["result_status"] not in {"success", "error", "truncated", "unknown"}:
            raise ValueError("invalid tool result status")
        if "execution_status" in item and item["execution_status"] not in {"success", "error", "unknown"}:
            raise ValueError("invalid execution status")
        if "completeness" in item and item["completeness"] not in {"complete", "partial", "missing"}:
            raise ValueError("invalid evidence completeness")
        if item.get("result_status") == "truncated":
            item["completeness"] = "partial"
        if "source_type" in item and item["source_type"] not in {"document", "attachment", "tool_result", "unknown"}:
            raise ValueError("invalid tool evidence source type")
        if "retention" in item and item["retention"] not in {"metadata", "bounded"}:
            raise ValueError("invalid tool evidence retention")
        for counter, message, width in (
            ("omitted_count", "invalid omitted observation count", 12),
            ("omitted_bytes", "invalid omitted observation bytes", 20),
        ):
            if counter in item and (not item[counter].isascii()
                or not item[counter].isdigit() or len(item[counter]) > width):
                raise ValueError(message)
        fingerprint = tuple(sorted(item.items()))
        if item and fingerprint not in seen:
            seen.add(fingerprint)
            result.append(item)
    bounded = apply_evidence_budget(result)
    for item in bounded:
        if "content" in item:
            # Budget truncation changes the retained body, so recompute the
            # digest after the budget boundary, including loss markers.
            item["result_digest"] = hashlib.sha256(item["content"].encode("utf-8")).hexdigest()
    return bounded


def read_tool_evidence(value: Any) -> tuple[dict[str, str], ...]:
    """Legacy/malformed inbox records remain readable but never gain trust."""
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[dict[str, str]] = []
    for raw in value:
        try:
            result.extend(normalize_tool_evidence([raw]))
        except ValueError:
            result.append({"tool_name": "evidence.inventory", "call_id": "invalid", "kind": "unknown", "result_status": "unknown", "completeness": "missing", "content": "Invalid tool evidence could not be retained."})
    return tuple(normalize_tool_evidence(result))


def observation_record(tool_name: str, call_id: str, payload: Any) -> dict[str, str] | None:
    """Build one observation from a host-matched call/result, never assistant text.

    A bounded complete result is evidence; an over-budget result is retained as
    an explicitly incomplete observation and cannot authorize a write.
    """
    import json
    if not isinstance(tool_name, str) or not tool_name or not isinstance(call_id, str) or not call_id:
        return None
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except ValueError:
            pass
        else:
            payload = decoded
    if isinstance(payload, Mapping) and (payload.get("isError") is True or payload.get("error")):
        return None
    memory_tool = bool(re.search(r"(?:^|[_.:/-])memleaf(?:$|[_.:/-])", tool_name, re.I))
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
    if not text.strip():
        return None
    record = {"tool_name": tool_name, "call_id": call_id,
              "kind": "retrieved_memory" if memory_tool else "external_observation",
              "result_status": "success", "content": text}
    if isinstance(payload, Mapping):
        for field in ("record_id", "title", "message_id", "subject", "sender", "domain"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                record[field] = value
    try:
        return normalize_tool_evidence([record])[0]
    except (ValueError, IndexError):
        return None


def refers_to_vault(arguments: Any, root: Any) -> bool:
    """Recognize direct Vault-file reads without inspecting/executing commands."""
    from pathlib import Path
    from urllib.parse import urlparse, unquote
    import ntpath

    def inside(value: str) -> bool:
        if value.startswith("file://"):
            value = unquote(urlparse(value).path)
        try:
            # Windows paths are compared using Windows semantics even when a
            # fixture or exported transcript is inspected on another platform.
            if re.match(r"^[A-Za-z]:[/\\]", str(root)):
                base = ntpath.normcase(ntpath.normpath(str(root)))
                target = ntpath.normcase(ntpath.normpath(value))
                return ntpath.isabs(target) and ntpath.commonpath([base, target]) == base
            base = Path(root).expanduser().resolve()
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                return False
            return candidate.resolve().is_relative_to(base)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def visit(value: Any, depth: int = 0) -> bool:
        if depth > 4:
            return False
        if isinstance(value, Mapping):
            for key, item in list(value.items())[:32]:
                if key in {"path", "file", "file_path", "filepath", "filename", "directory", "uri"}:
                    if isinstance(item, str) and inside(item):
                        return True
                if isinstance(item, (Mapping, list, tuple)) and visit(item, depth + 1):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(visit(item, depth + 1) for item in value[:32])
        return False
    return visit(arguments)


def observation_records(tool_name: str, call_id: str, payload: Any, *,
                        source_kind: str | None = None) -> list[dict[str, str]]:
    """Retain complete structural records and explicit incompleteness markers.

    Collection splitting is structural, never keyed to email or another
    business domain. Shared collection context remains with each child. The
    resulting records cross the shared budget exactly once at the end.
    """
    import json
    if not isinstance(tool_name, str) or not tool_name or not isinstance(call_id, str) or not call_id:
        return []
    if source_kind is not None and source_kind not in {"retrieved_memory", "external_observation", "unknown"}:
        raise ValueError("invalid observation source kind")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    error = isinstance(payload, Mapping) and payload.get("isError") is True
    kind = source_kind or ("retrieved_memory" if re.search(r"(?:^|[_.:/-])memleaf(?:$|[_.:/-])", tool_name, re.I)
                           else "external_observation")

    def raw_record(value: Any, ident: str | None = None) -> dict[str, str] | None:
        try:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        if not text or "\x00" in text:
            return None
        item: dict[str, str] = {"tool_name": tool_name, "call_id": call_id, "kind": kind,
            "execution_status": "error" if error else "success", "completeness": "complete",
            "schema_version": "2", "result_status": "error" if error else "success", "content": text}
        if isinstance(value, Mapping):
            for field in ("record_id", "title", "message_id", "subject", "sender", "domain"):
                raw = value.get(field)
                if isinstance(raw, str) and raw.strip() and not any(c in raw for c in "\x00\r\n"):
                    item[field] = raw
        if ident is not None:
            item["record_id"] = ident
        return item

    if payload is None:
        return []
    # Prefer the native structured result; do not duplicate serialized content.
    if not error and isinstance(payload, Mapping) and isinstance(payload.get("structuredContent"), Mapping):
        payload = payload["structuredContent"]
    original = raw_record(payload)
    if original is None:
        return []

    def finalize(value: list[Mapping[str, Any]]) -> list[dict[str, str]]:
        bounded = normalize_tool_evidence(value)
        # A per-call observation marker must retain the host call identity.
        # The shared budget uses a global overflow identity when no call
        # context is available; remapping here prevents two different pending
        # calls from collapsing in HostRuntime's call/record dedupe map.
        for item in bounded:
            if is_overflow_marker(item) and item.get("call_id") in {"overflow", "retention-overflow"}:
                item["call_id"] = call_id[:MAX_TEXT]
        return bounded

    collection, context = None, {}
    if isinstance(payload, list):
        collection = payload
    elif isinstance(payload, Mapping):
        keys = [key for key in ("items", "records", "results") if isinstance(payload.get(key), list)]
        if len(keys) == 1:
            collection = payload[keys[0]]
            context = {key: value for key, value in payload.items() if key != keys[0]}
    if error or not collection:
        return finalize([original])
    output = []
    for index, value in enumerate(collection):
        item = raw_record({"context": context, "record": value}, f"result-record-{index}")
        if item is None:
            item = {"tool_name": tool_name, "call_id": call_id, "record_id": f"result-record-{index}",
                    "kind": "unknown", "result_status": "unknown", "execution_status": "success",
                    "completeness": "missing", "content": "Tool record could not be retained safely."}
        for field in ("message_id", "subject", "sender", "domain", "title"):
            raw = value.get(field) if isinstance(value, Mapping) else None
            if not isinstance(raw, str) or not raw.strip():
                raw = original.get(field)
            if isinstance(raw, str) and raw.strip() and not any(c in raw for c in "\x00\r\n"):
                item[field] = raw
        output.append(item)
    return finalize(output)
