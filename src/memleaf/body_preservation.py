"""Account for omitted old prose without guessing which facts remain valid.

Text fragments are comparison aids, not a parser for facts. The existing
semantic review judges equivalence or an authorized change; Core checks that
every omission has a real, source-bound disposition for the final candidate.
"""
from __future__ import annotations

import re


def _compact(text):
    return re.sub(r"\s+", "", text)


def omissions(row, original):
    if not isinstance(row, dict):
        return []
    action = row.get("action")
    action = action.strip().upper() if isinstance(action, str) else ""
    if action not in {"UPDATE", "MERGE"}:
        return []
    patch = row.get("patch")
    if not isinstance(patch, dict):
        return []
    # Retraction erases the stored body even when the model omits patch.body.
    body = "" if patch.get("validity") == "retracted" else patch.get("body")
    if not isinstance(body, str):
        return []
    refs = [row.get("target")]
    if action == "MERGE" and isinstance(row.get("duplicates"), list):
        refs.extend(row["duplicates"])
    refs = [ref.strip() for ref in refs if isinstance(ref, str)]
    new = _compact(body)
    missing = []
    seen = set()
    for memory in original.get("memories", []):
        if memory.get("ref") not in refs or memory.get("validity", "valid") != "valid":
            continue
        old = memory.get("body", "")
        if not isinstance(old, str):
            continue
        # Language-neutral punctuation/line boundaries. No names, keywords or
        # expected facts influence the comparison. Short fragments count too.
        for text in re.split(r"[。！？!?；;\n，,]+|(?<=\.)\s+", old):
            text = text.strip()
            key = (memory["ref"], text)
            if text and _compact(text) not in new and key not in seen:
                missing.append({"target": key[0], "old": text})
                seen.add(key)
    return missing


def covered(row, original, coverage=None):
    """Missing/malformed proofs defer this proposal, never erase the head."""
    missing = omissions(row, original)
    if not missing:
        return True
    if not isinstance(coverage, list):
        return False
    expected = {(part["target"], part["old"]) for part in missing}
    # A reviewer may restore a fragment and still include its proof. Check
    # against the real old fragments, not an exact output-count convention.
    all_old = omissions({**row, "patch": {"body": ""}}, original)
    allowed = {(part["target"], part["old"]) for part in all_old}
    verified = set()
    body = "" if row["patch"].get("validity") == "retracted" else row["patch"].get("body", "")
    evidence = {e["ref"]: e for e in original.get("evidence", [])}
    for proof in coverage:
        if not isinstance(proof, dict):
            return False
        key = (proof.get("target"), proof.get("old"))
        if not all(isinstance(value, str) for value in key) or key not in allowed or key in verified:
            return False
        if set(proof) == {"target", "old", "body"}:
            quote = proof["body"]
            if not isinstance(quote, str) or not quote.strip() or quote not in body:
                return False
        elif set(proof) == {"target", "old", "source"}:
            selection = proof["source"]
            if not isinstance(selection, dict) or set(selection) not in ({"ref", "text"}, {"ref", "text", "kind"}):
                return False
            if not isinstance(selection["ref"], str):
                return False
            event = evidence.get(selection.get("ref"))
            quote = selection.get("text")
            reported = selection.get("kind") == "reported_result"
            # Only the existing semantic reviewer may classify an exact cited
            # assistant result as a superseding observation. This does not
            # authorize retraction, nor turn a suggestion into a user request.
            role_allowed = event is not None and (
                event.get("role") == "user" and "kind" not in selection
                or event.get("role") == "assistant" and reported
                and row["patch"].get("validity") != "retracted"
                and any(e.get("role") == "user" and e.get("use") == "new"
                        and e.get("ref") in row.get("evidence", []) for e in evidence.values()))
            if (event is None or event.get("use") != "new" or not role_allowed
                    or event["ref"] not in row.get("evidence", [])
                    or not isinstance(quote, str) or not quote.strip() or quote not in event.get("text", "")):
                return False
        else:
            return False
        verified.add(key)
    return expected <= verified


def defer_unreviewed(response, snapshot):
    """A manual partial repair has no model reviewer; preserve unsafe heads."""
    from .incremental_semantics import envelope
    from .validation import parse_strict_json
    from .incremental_protocol import compile_incremental
    import json
    draft = envelope(parse_strict_json(response))
    original = snapshot.model_input()
    for i, row in enumerate(draft["items"]):
        if (omissions(row, original)
                and not compile_incremental(json.dumps({"items": [row]}), snapshot)["issues"]):
            draft["items"][i] = {"action": "DEFERRED", "evidence": row.get("evidence", []),
                                 "reason": "missing_context",
                                 "need": "The body replacement has unverified omissions from the previous memory."}
    return json.dumps(draft, ensure_ascii=False, separators=(",", ":"))
