"""Bounded, redacted tool observations; metadata is not proof of content."""
from __future__ import annotations
import hashlib
import re
from typing import Any, Mapping
from .redaction import redact_text

TOOL_EVIDENCE_FIELDS = frozenset({"message_id", "subject", "sender", "domain",
    "tool_name", "call_id", "record_id", "title", "kind", "result_status",
    "content", "result_digest"})
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
        fingerprint = tuple(sorted(item.items()))
        if item and fingerprint not in seen:
            seen.add(fingerprint)
            result.append(item)
    if len(value) > MAX_ITEMS:
        result = result[:MAX_ITEMS - 1]
        result.append({"tool_name": "evidence.inventory", "call_id": "overflow",
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
            continue
    return tuple(result)


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
