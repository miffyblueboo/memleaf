"""Compare automatic turns with verified explicit-retention receipts.

Only the capture ledger and committed operations establish this relation. Tool
reply text, model declarations and similar titles do not establish coverage.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from .capture import _payload_digest
from .explicit_text_source import validate_origin
from .index import extract_event_metadata
from .recording_policy import recording_allowed


def _verified_continuation(processed: Mapping[str, Any], *, source: str, session_id: str,
                           origin_session_id: str, turn_key: str) -> bool:
    """Allow only one captured host turn continued down a trusted lineage.

    Legacy turn labels may repeat after a session reset. A lineage relation alone
    cannot prove their identity; require the host's opaque, stable turn contract
    in actual capture receipts, in addition to the matching origin digest.
    """
    from .index import turn_key as identity_key
    from .process_common import _MAX_SESSION_LINEAGE_DEPTH
    from .vault import safe_component

    receipts = [event for event in processed.get("events", {}).values()
                if isinstance(event, dict) and event.get("source") == source
                and event.get("session_id") == session_id and event.get("turn_key") == turn_key]
    if (not any(event.get("role") == "user" for event in receipts)
            or not any(event.get("role") == "assistant" and event.get("final") is True for event in receipts)
            or any(not isinstance(event.get("turn_id"), str)
                   or not re.fullmatch(r"host-[0-9a-f]{24}", event["turn_id"])
                   or identity_key(event["turn_id"]) != turn_key for event in receipts)):
        return False
    sessions = processed.get("sessions", {})
    if not isinstance(sessions, dict):
        raise ValueError("invalid_session_lineage")
    current, seen = session_id, set()
    # Check the whole path even after reaching the origin: cycles must never
    # authorize a relation just because their first edge happens to match.
    for _ in range(_MAX_SESSION_LINEAGE_DEPTH):
        if current in seen or not recording_allowed(processed, source, current, turn_key):
            return False
        seen.add(current)
        state = sessions.get(f"{source}/{current}", {})
        if not isinstance(state, dict):
            raise ValueError("invalid_session_lineage")
        parent = state.get("lineage_parent_session_id")
        if parent is None:
            return origin_session_id in seen
        current = safe_component(parent, "lineage parent session id")
    return False


def matching_writes(service: Any, processed: Mapping[str, Any], work: Mapping[str, Any], *,
                    source: str, session_id: str, turn_key: str,
                    content_cache: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only independently verified operations from this originating turn.

    The source text is optional after inbox cleanup. Its absence never authorizes
    borrowing the tool result or the current turn text as the submitted input.
    """
    if work.get("request_kind") != "explicit_remember" or work["source"] != source:
        return []
    if not recording_allowed(processed, source, session_id, turn_key):
        return []
    eligible = {}
    events = processed.get("events", {})
    if not isinstance(events, dict):
        raise ValueError("invalid_capture_receipts")
    for evidence in work["evidence"]:
        origin = validate_origin(evidence.get("explicit_input"))
        if origin is None or origin["turn_key"] != turn_key:
            continue
        if origin["session_id"] != session_id and not _verified_continuation(processed,
                source=source, session_id=session_id, origin_session_id=origin["session_id"], turn_key=turn_key):
            continue
        if not recording_allowed(processed, source, origin["session_id"], turn_key):
            continue
        receipt = events.get(evidence["event_key"])
        if (not isinstance(receipt, dict) or receipt.get("explicit_input") != origin
                or receipt.get("source") != source or receipt.get("session_id") != work["session_id"]
                or receipt.get("turn_key") != work["turn_key"] or receipt.get("role") != "user"
                or not isinstance(receipt.get("payload_digest"), str)):
            continue
        if work["session_id"] not in content_cache:
            path = service.vault._inside("inbox", source, f"{work['session_id']}.md")
            if path.is_symlink():
                raise ValueError("unsafe_explicit_input_path")
            content_cache[work["session_id"]] = ({e["event_key"]: e for e in
                extract_event_metadata(path.read_text(encoding="utf-8"))} if path.exists() else {})
        raw = content_cache[work["session_id"]].get(evidence["event_key"])
        item = {"event_key": evidence["event_key"], "payload_digest": receipt["payload_digest"],
                "origin": origin}
        if raw is not None:
            if _payload_digest(raw, raw["content"]) != receipt["payload_digest"]:
                raise ValueError("explicit_input_changed")
            item["text"] = raw["content"].rstrip("\n")
            item["source_time"] = raw.get("source_time")
        eligible[evidence["ref"]] = item
    result = []
    for operation in work["operations"]:
        if (operation["action"] not in {"CREATE", "UPDATE", "NO_CHANGE"}
                or operation["state"] not in {"applied", "settled"} or operation.get("native")):
            continue
        revision = operation.get("replacement_revision", operation.get("expected_revision"))
        sources = [eligible[ref] for ref in operation["evidence"] if ref in eligible]
        if not sources or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
            continue
        result.append({"memory_id": operation["memory_id"], "action": operation["action"],
                       "work_id": work["work_id"], "operation_id": operation["operation_id"],
                       "committed_revision": revision, "submitted_sources": sources})
    return result


def validate_links(value: Any, targets: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate frozen internal bindings; no model-supplied receipt claims."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("invalid_explicit_write_links")
    seen = set()
    for link in value:
        if (not isinstance(link, dict) or set(link) != {"target", "action", "work_id", "operation_id",
                "committed_revision", "submitted_sources"} or not isinstance(link["target"], str)
                or link["target"] not in targets
                or "native" in targets[link["target"]] or not isinstance(link["action"], str)
                or link["action"] not in {"CREATE", "UPDATE", "NO_CHANGE"}
                or not isinstance(link["work_id"], str) or not re.fullmatch(r"inc-[0-9a-f]{64}", link["work_id"])
                or not isinstance(link["operation_id"], str) or not link["operation_id"]
                or not isinstance(link["committed_revision"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", link["committed_revision"])
                or not isinstance(link["submitted_sources"], list) or not 1 <= len(link["submitted_sources"]) <= 64):
            raise ValueError("invalid_explicit_write_links")
        identity = (link["work_id"], link["operation_id"])
        if identity in seen:
            raise ValueError("invalid_explicit_write_links")
        seen.add(identity)
        for item in link["submitted_sources"]:
            if (not isinstance(item, dict) or not {"event_key", "payload_digest", "origin"} <= set(item)
                    or set(item) - {"event_key", "payload_digest", "origin", "text", "source_time"}
                    or any(not isinstance(item[k], str) or not re.fullmatch(r"[0-9a-f]{64}", item[k])
                           for k in ("event_key", "payload_digest"))):
                raise ValueError("invalid_explicit_write_links")
            if validate_origin(item["origin"]) is None:
                raise ValueError("invalid_explicit_write_links")
            if "text" in item:
                if not isinstance(item["text"], str) or not item["text"].strip() or "\0" in item["text"]:
                    raise ValueError("invalid_explicit_write_links")
                from .incremental_dates import parse_source_time
                parse_source_time(item.get("source_time"))
    return deepcopy(value)


def project_links(links: list[dict[str, Any]], targets: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose comparison context, never receipts, identities or new evidence."""
    return [{"target": link["target"], "action": link["action"],
             "target_unchanged_since_write": targets[link["target"]]["revision"] == link["committed_revision"],
             "coverage": "explicit_submitted_content_only",
             "submitted_sources": [{"content_available": "text" in item,
                 **({"text": item["text"], "source_time": item.get("source_time")} if "text" in item else {})}
                 for item in link["submitted_sources"]]} for link in links]


def freeze_bindings(links: list[dict[str, Any]], targets: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep comparison dependencies in a commit receipt without source bodies."""
    return [{**{k: deepcopy(v) for k, v in link.items() if k not in {"target", "submitted_sources"}},
             "memory_id": targets[link["target"]]["memory"]["memory_id"],
             "submitted_sources": [{k: deepcopy(v) for k, v in item.items() if k not in {"text", "source_time"}}
                                   for item in link["submitted_sources"]]} for link in links]


def validate_bindings(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError("invalid_explicit_write_bindings")
    from .vault import safe_component
    links, targets = [], {}
    for binding in value:
        if not isinstance(binding, dict) or "memory_id" not in binding or "target" in binding:
            raise ValueError("invalid_explicit_write_bindings")
        mid = safe_component(binding["memory_id"], "memory id")
        targets[mid] = {}
        links.append({**{k: v for k, v in binding.items() if k != "memory_id"}, "target": mid})
    validate_links(links, targets)
    if any(set(item) != {"event_key", "payload_digest", "origin"}
           for binding in value for item in binding["submitted_sources"]):
        raise ValueError("invalid_explicit_write_bindings")


def bindings_current(service: Any, processed: Mapping[str, Any], work: Mapping[str, Any]) -> bool:
    """Recheck comparison dependencies when a prepared commit resumes."""
    from .incremental_journal import load_work
    cache = {}
    for binding in work.get("explicit_write_bindings", []):
        origin_work = load_work(processed, binding["work_id"])
        if origin_work is None:
            return False
        actual = matching_writes(service, processed, origin_work, source=work["source"],
            session_id=work["session_id"], turn_key=work["turn_key"], content_cache=cache)
        actual = [{**item, "submitted_sources": [
            {k: v for k, v in source.items() if k not in {"text", "source_time"}}
            for source in item["submitted_sources"]]} for item in actual]
        if binding not in actual:
            return False
    return True
