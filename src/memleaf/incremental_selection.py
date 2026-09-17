"""Selected-source authorization binding, not a second runner or semantic gate.

A trusted local caller supplies the actual user retention request and a stable
intent ID. Event keys identify immutable captured messages, never model eN refs.
The terminal binding retains only the request hash; no authorization text is
kept after the existing run releases its request/response plaintext.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .incremental_journal import digest


def validate_selection(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"intent_id", "source_refs", "request_hash"}:
        raise ValueError("invalid_retention_selection")
    intent, refs, fingerprint = value["intent_id"], value["source_refs"], value["request_hash"]
    if (not isinstance(intent, str) or not intent.strip() or len(intent) > 800
            or any(c in intent for c in "\0\r\n")):
        raise ValueError("invalid_intent_id")
    if (not isinstance(refs, list) or not 1 <= len(refs) <= 64
            or any(not isinstance(ref, str) or re.fullmatch(r"[0-9a-f]{64}", ref) is None for ref in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError("invalid_selected_source_refs")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("invalid_retention_request_hash")
    return {"intent_id": intent, "source_refs": sorted(refs), "request_hash": fingerprint}


def bind_selection(intent_id: str, source_refs: Any, retention_request: str) -> tuple[dict[str, Any], str]:
    from .redaction import redact_text
    text = validate_request(redact_text(validate_request(retention_request)))
    selection = validate_selection({"intent_id": intent_id, "source_refs": source_refs,
                                    "request_hash": digest(text)})
    return selection, text


def validate_request(text: Any, selection: dict[str, Any] | None = None) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 8192 or "\0" in text:
        raise ValueError("invalid_retention_request")
    text = text.strip()
    if selection is not None and digest(text) != selection["request_hash"]:
        raise ValueError("retention_request_changed")
    return text


def explicit_run_id(source: str, session_id: str, selection: dict[str, Any]) -> str:
    # Reusing an intent with different source revisions is a binding conflict,
    # not a new automatic work. The budget itself also binds the selected inputs.
    return "inc-run-" + digest(["explicit_remember", source, session_id, selection["intent_id"]])


def select_evidence(evidence: list[dict[str, Any]], selection: dict[str, Any]) -> list[dict[str, Any]]:
    refs = set(selection["source_refs"])
    available = {e["event_key"] for e in evidence if e["use"] == "new"}
    if not refs <= available:
        raise ValueError("selected_source_unavailable")
    result = deepcopy(evidence)
    for event in result:
        if event["use"] == "new" and event["event_key"] not in refs:
            event["use"] = "context"
    return result
