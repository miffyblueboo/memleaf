"""Bounded, redacted tool observations; metadata is not proof of content."""
from __future__ import annotations
import hashlib
import re
from typing import Any, Mapping
from .redaction import redact_text

TOOL_EVIDENCE_FIELDS = frozenset({"message_id", "subject", "sender", "domain",
    "tool_name", "call_id", "record_id", "title", "kind", "result_status",
    "content", "result_digest", "execution_status", "completeness", "schema_version", "omitted_count", "source_type", "retention"})
MAX_ITEMS = 8
MAX_TEXT = 320
MAX_CONTENT = 2000
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)


def normalize_tool_evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("tool evidence must be a list")
    result = []
    seen = set()
    for raw in value[:MAX_ITEMS]:
        if not isinstance(raw, Mapping) or set(raw) - TOOL_EVIDENCE_FIELDS:
            raise ValueError("invalid tool evidence record")
        item = {}
        for key, field in raw.items():
            if field is None:
                continue
            if not isinstance(field, str) or not field.strip() or "\x00" in field:
                raise ValueError("invalid tool evidence field")
            if key != "content" and any(ch in field for ch in "\r\n"):
                raise ValueError("invalid tool evidence field")
            text = redact_text(field.strip())
            limit = MAX_CONTENT if key == "content" else MAX_TEXT
            item[key] = text[:limit]
            if key == "content" and len(text) > limit:
                item["result_status"] = "truncated"
            if key == "domain":
                item[key] = text.casefold().lstrip("@")
                if not DOMAIN_RE.fullmatch(item[key]):
                    raise ValueError("invalid tool evidence domain")
        if "content" in item:
            if len(redact_text(str(raw.get("content", "")).strip())) > MAX_CONTENT:
                item["result_status"] = "truncated"
            # Recompute after redaction. A caller-supplied digest cannot assert
            # that a different body was observed.
            item["result_digest"] = hashlib.sha256(item["content"].encode()).hexdigest()
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
        if "source_type" in item and item["source_type"] not in {"document", "tool_result", "unknown"}:
            raise ValueError("invalid tool evidence source type")
        if "retention" in item and item["retention"] not in {"metadata", "bounded"}:
            raise ValueError("invalid tool evidence retention")
        if "omitted_count" in item and (not item["omitted_count"].isascii()
            or not item["omitted_count"].isdigit() or len(item["omitted_count"]) > 12):
            raise ValueError("invalid omitted observation count")
        fingerprint = tuple(sorted(item.items()))
        if item and fingerprint not in seen:
            seen.add(fingerprint)
            result.append(item)
    if len(value) > MAX_ITEMS:
        omitted = sum(int(row.get("omitted_count", "1")) if isinstance(row, Mapping) and str(row.get("omitted_count", "1")).isdigit() else 1 for row in value[MAX_ITEMS-1:])
        result = result[:MAX_ITEMS - 1]
        result.append({"omitted_count": str(omitted), "completeness": "partial","tool_name": "evidence.inventory", "call_id": "overflow",
                       "kind": "unknown", "result_status": "truncated",
                       "content": "Additional tool observations exceeded the capture budget."})
    return result


def read_tool_evidence(value: Any) -> tuple[dict[str, str], ...]:
    """Legacy/malformed inbox records remain readable but never gain trust."""
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for raw in value[:MAX_ITEMS]:
        try:
            result.extend(normalize_tool_evidence([raw]))
        except ValueError:
            result.append({"tool_name": "evidence.inventory", "call_id": "invalid", "kind": "unknown", "result_status": "unknown", "completeness": "missing", "content": "Invalid tool evidence could not be retained."})
    if len(value) > MAX_ITEMS:
        result.append({"tool_name": "evidence.inventory", "call_id": "overflow",
            "kind": "unknown", "result_status": "truncated", "completeness": "partial",
            "omitted_count": str(len(value) - MAX_ITEMS),
            "content": "Additional stored observations exceeded the evidence budget."})
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
    """Retain bounded complete records and explicit incompleteness tombstones.

    Collection splitting is structural, never keyed to email or another
    business domain. Shared collection context remains with each child.
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

    def record(value: Any, ident: str | None = None) -> dict[str, str] | None:
        try:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        if not text or "\x00" in text:
            return None
        item = {"tool_name": tool_name, "call_id": call_id, "kind": kind,
                "execution_status": "error" if error else "success", "completeness": "complete",
                "schema_version": "2", "result_status": "error" if error else "success", "content": text}
        if isinstance(value, Mapping):
            for field in ("record_id", "title", "message_id", "subject", "sender", "domain"):
                raw = value.get(field)
                if isinstance(raw, str) and raw.strip() and not any(c in raw for c in "\x00\r\n"):
                    item[field] = raw
        if ident is not None:
            item["record_id"] = ident
        try:
            return normalize_tool_evidence([item])[0]
        except (ValueError, IndexError):
            return None

    if payload is None:
        return []
    # Prefer the native structured result; do not duplicate serialized content.
    if not error and isinstance(payload, Mapping) and isinstance(payload.get("structuredContent"), Mapping):
        payload = payload["structuredContent"]
    original = record(payload)
    if original is None:
        return []
    if original.get("completeness") == "complete" or error:
        return [original]
    collection, context = None, {}
    if isinstance(payload, list):
        collection = payload
    elif isinstance(payload, Mapping):
        keys = [key for key in ("items", "records", "results") if isinstance(payload.get(key), list)]
        if len(keys) == 1:
            collection = payload[keys[0]]
            context = {key: value for key, value in payload.items() if key != keys[0]}
    if not collection:
        return [original]
    output = []
    for index, value in enumerate(collection[:MAX_ITEMS]):
        item = record({"context": context, "record": value}, f"result-record-{index}")
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
    if len(collection) > MAX_ITEMS:
        output.append({"tool_name": tool_name, "call_id": call_id, "record_id": "overflow",
            "kind": "unknown", "result_status": "truncated", "completeness": "partial",
            "execution_status": "success", "omitted_count": str(len(collection) - MAX_ITEMS),
            "content": "Additional structured observations exceeded the capture budget."})
    return normalize_tool_evidence(output)
