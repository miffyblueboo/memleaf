"""Owner-only accounting and explicit reauthorization of frozen host work.

These Python/CLI entry points must never be registered as model MCP tools.
Accounting writes control records and derived indexes, never memory/history.
Recovery creates a new authorized child and retains the original operation IDs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from typing import Any

from .host_v2_common import V2Error, canonical, digest, revision, success
from .models import Memory
from .query_scan import ensure_scan_current, scan_memories

_FINAL = {"saved", "unchanged", "not_recorded"}


def _work(state: dict[str, Any], work_id: str) -> dict[str, Any]:
    work = state["works"].get(work_id)
    if work is None:
        raise V2Error("RESOURCE_UNAVAILABLE", "Owner work is unavailable")
    return work


def _inactive(host: Any, work: dict[str, Any]) -> str:
    """Require an actually invalidated frozen authority, not a caller claim."""
    try:
        host.auth.check({key: work[key] for key in
                         ("principal_id", "authorization_domain", "grant_id", "grant_epoch")})
    except V2Error as error:
        if error.code == "AUTH_EXPIRED":
            return "expired"
        if error.code == "AUTH_REVOKED":
            return "revoked"
        raise
    raise V2Error("FORBIDDEN", "Active work uses ordinary authorized recovery")


def account_work(host: Any, work_id: str) -> dict[str, Any]:
    """Owner accounts a revoked/expired work without replaying its writes."""
    with host.vault.lock():
        state = host._load()
        work = _work(state, work_id)
        authority = _inactive(host, work)
        if work.get("recovery_child_id"):
            return host._summary(state, work)
        unresolved = False
        has_applied = False
        for entry in work["items"].values():
            receipt = entry["receipt"]
            if receipt["status"] in _FINAL:
                has_applied |= receipt["status"] == "saved"
                continue
            operations = entry.get("operations", [])
            actual = [host._applied(operation) for operation in operations]
            complete = False
            if operations and not entry.get("deleted_dependency_ids"):
                try:
                    host._validate_frozen(entry, work["snapshot"])
                    complete = True
                except V2Error:
                    pass
            for operation, applied in zip(operations, actual):
                # Keep a remembered application true even if a later external
                # deletion makes the current CREATE head absent. Do not revive.
                if applied is not False or not operation.get("applied"):
                    operation["applied"] = applied
            if complete and all(applied is True for applied in actual):
                receipt.update(status="saved", applied=True, settled=True, error=None,
                    committed_revisions=[{"memory_id": operation["memory_id"],
                        "revision": "sha256:" + operation["replacement_revision"]}
                        for operation in operations])
                has_applied = True
            elif any(applied is None for applied in actual) or any(applied is True for applied in actual):
                unresolved = True
                has_applied |= any(applied is True for applied in actual)
                receipt.update(status="recovery_required", settled=False,
                    applied=None if any(applied is None for applied in actual) else True,
                    error=V2Error("AUTH_RENEWAL_REQUIRED",
                        "Owner authorization is required to resolve retained frozen work",
                        next_action="request_owner_approval").public())
            elif any(operation.get("applied") is True for operation in operations):
                # A previously applied head that is now absent is not cleanly
                # cancellable, and must not be recreated through recovery.
                unresolved = True
                receipt.update(status="recovery_required", applied=None, settled=False,
                    error=V2Error("RECOVERY_REQUIRED", next_action="contact_owner").public())
            else:
                receipt.update(status="cancelled", applied=False, settled=True, error=None)
                # Retain exact frozen operations for a future explicit owner
                # recovery approval. Cancellation itself grants no replay.
        states = [entry["receipt"]["status"] for entry in work["items"].values()]
        if unresolved:
            work.update(status="partial" if has_applied else "submitted", terminal=False)
        elif states and all(status in _FINAL for status in states):
            work.update(status="completed", terminal=True)
        else:
            work.update(status="partial" if has_applied else "cancelled", terminal=True)
        work["authorization_state"] = authority
        work["stop_reason"] = "authorization_expired" if authority == "expired" else "authorization_revoked"
        work["work_revision"] += 1
        host._save(state)
        if has_applied:
            try:
                host.service._rebuild_index_unlocked()
                work["index_status"] = "current"
            except OSError:
                work["index_status"] = "dirty"
            host._save(state)
        result = host._summary(state, work)
        result["authorization_state"] = authority
        return result


def _material(host: Any, state: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    physical = []
    for item_id, entry in sorted(work["items"].items()):
        if entry["receipt"]["status"] in _FINAL or entry.get("transferred_to_work_id"):
            continue
        for operation in entry.get("operations", []):
            record, _ = host.service._revision_target_unlocked(operation["memory_id"])
            physical.append({"item_id": item_id, "operation_id": operation["operation_id"],
                "memory_id": operation["memory_id"], "applied": host._applied(operation),
                "current_revision": revision(record.memory) if record else None,
                "frozen_digest": "sha256:" + digest(operation)})
    sources = [{"source_id": source_id,
                "current": deepcopy(state["sources"].get(source_id))}
               for source_id in work["source_ids"]]
    return {"work": deepcopy(work), "physical": physical, "sources": sources}


def _plan(host: Any, state: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    _inactive(host, work)
    if work.get("recovery_child_id"):
        raise V2Error("WORK_TERMINAL", "Frozen operations already belong to a recovery child")
    material = _material(host, state, work)
    if not material["physical"]:
        raise V2Error("WORK_TERMINAL", "No frozen business operation remains to recover")
    return {"kind": "authorized_recovery_plan", "target_work_id": work["work_id"],
            "plan_digest": "sha256:" + digest(material),
            "budget_owner_work_id": work["budget_owner_work_id"],
            "operations": material["physical"], "semantic_budget_added": 0}


def recovery_plan(host: Any, work_id: str) -> dict[str, Any]:
    """Owner observes exact retained payloads and physical heads for approval."""
    with host.vault.lock():
        state = host._load()
        return _plan(host, state, _work(state, work_id))


def _check_recovery_resources(host: Any, state: dict[str, Any], context: dict[str, Any],
                              work: dict[str, Any]) -> None:
    host.auth.check(context, "work.read", work=work)
    host.auth.check(context, "work.resume")
    host.auth.check(context, "memory.write")
    host.auth.check(context, "source.read")
    if (work["principal_id"] != context["principal_id"]
            and not context["allow_other_work_resume"]):
        raise V2Error("RESOURCE_UNAVAILABLE", "Requested resource is unavailable")
    host._verify_source(state, work)
    for source in work["snapshot"]["sources"].values():
        current_source = state["sources"].get(source["source_id"], {})
        if any(current_source.get(key) != source.get(key) for key in
               ("text", "role", "trust", "revision", "authorization_domain",
                "source_time", "timezone", "origin")):
            raise V2Error("SOURCE_CHANGED")
        if source.get("authorization_domain") != context["authorization_domain"]:
            raise V2Error("RESOURCE_UNAVAILABLE")
        if source.get("trust") not in context["admissible_source_trust"]:
            raise V2Error("SOURCE_CONFIRMATION_REQUIRED", next_action="request_owner_approval")
    for target in work["snapshot"]["targets"].values():
        host.auth.check(context, "memory.read", scopes=target["memory"]["scopes"])
    for entry in work["items"].values():
        if entry["receipt"]["status"] in _FINAL:
            continue
        if entry.get("operations"):
            host._validate_frozen(entry, work["snapshot"])
        if entry["receipt"]["action"] in {"MERGE", "COMPACT"}:
            host.auth.check(context, "memory.maintain")
        proposal = entry.get("proposal", {})
        if proposal.get("update_kind") == "retract_memory" or any(
                patch.get("op") == "retract" for patch in proposal.get("body_patch", [])):
            host.auth.check(context, "memory.retract")
        for operation in entry.get("operations", []):
            actual = host._applied(operation)
            if actual is None:
                raise V2Error("REVISION_CONFLICT", next_action="contact_owner")
            if actual is False and operation.get("applied") is True:
                raise V2Error("TARGET_UNAVAILABLE", "Applied content is no longer available")
            for field in ("before", "after"):
                if operation.get(field):
                    memory = Memory.from_markdown(operation[field])
                    host.auth.check(context, "memory.read", scopes=memory.scopes)
                    host.auth.check(context, "memory.write", scopes=memory.scopes)


def prepare_authorized_recovery(host: Any, state: dict[str, Any], context: dict[str, Any],
                                args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Caller already holds the Vault lock. Authorize a deterministic child.

Approval consumption precedes the child ledger write. If that write fails, only
the same consumption ID and exact plan can complete the interrupted binding.
No unapproved pending child is ever visible to the business executor.
"""
    approval_ref = args["source"]["approval_ref"]
    auth_state = host.auth._load()
    approval = auth_state["approvals"].get(approval_ref)
    if not isinstance(approval, dict) or not approval.get("target_work_id"):
        raise V2Error("AUTH_RENEWAL_REQUIRED", next_action="request_owner_approval")
    if approval.get("kind") == "new_processing_budget":
        return prepare_authorized_processing(host, state, context, args)
    parent = _work(state, approval["target_work_id"])
    child_id = "work_recovery_" + digest(approval_ref)[:32]
    previous = state["works"].get(child_id)
    if previous is not None:
        if parent.get("recovery_child_id") != child_id or previous.get("recovery_approval_ref") != approval_ref:
            raise V2Error("STATE_CORRUPT")
        host.auth.check(context, "work.read", work=previous)
        if any(previous[key] != context[key] for key in ("grant_id", "grant_epoch", "principal_id")):
            raise V2Error("AUTH_RENEWAL_REQUIRED")
        return success({"kind": "prepared", "work": host._summary(state, previous),
                        "snapshot": host._public_snapshot(previous), "replayed": True}), previous
    plan = _plan(host, state, parent)
    _check_recovery_resources(host, state, context, parent)
    if approval.get("used") is True:
        # Exact retry after durable approval consumption but before host save.
        host.auth.check(context)
        if (approval.get("consumption_id") != child_id
                or approval.get("kind") != "resume_after_revocation"
                or approval.get("issued_by") != "vault_owner_control_plane"
                or approval.get("plan_digest") != plan["plan_digest"]
                or any(approval.get(key) != context[key] for key in
                       ("principal_id", "authorization_domain", "grant_id", "grant_epoch"))):
            raise V2Error("AUTH_RENEWAL_REQUIRED", next_action="request_owner_approval")
    else:
        host.auth.consume_approval(context, approval_ref, "resume_after_revocation",
            plan["plan_digest"], parent["work_id"], locked=True, consumption_id=child_id)
    child = deepcopy(parent)
    child.update(work_id=child_id, related_work_id=parent["work_id"], recovery_parent_id=parent["work_id"],
        recovery_approval_ref=approval_ref, recovery_plan_digest=plan["plan_digest"],
        work_revision=1, status="submitted", terminal=False, stop_reason="io",
        authorization_state="active", purpose="authorized_recovery", budget_used=0,
        **{key: context[key] for key in ("principal_id", "authorization_domain", "grant_id", "grant_epoch")})
    child.pop("recovery_child_id", None)
    child["snapshot"]["snapshot_id"] = "snap_recovery_" + digest(approval_ref)[:32]
    child["snapshot"]["purpose"] = "authorized_recovery"
    child["snapshot"]["write_scopes"] = deepcopy(context["write_scopes"])
    child["items"] = {}
    for item_id, entry in parent["items"].items():
        if entry["receipt"]["status"] in _FINAL or not entry.get("operations"):
            continue
        clone = deepcopy(entry)
        clone.pop("transferred_to_work_id", None)
        clone["receipt"].update(status="recovery_required", settled=False, error=None)
        child["items"][item_id] = clone
        entry["transferred_to_work_id"] = child_id
    parent["recovery_child_id"] = child_id
    parent["work_revision"] += 1
    parent.update(terminal=True, status="partial" if any(
        entry["receipt"]["applied"] is True for entry in parent["items"].values()) else "cancelled")
    state["works"][child_id] = child
    host._save(state)
    return success({"kind": "prepared", "work": host._summary(state, child),
                    "snapshot": host._public_snapshot(child), "replayed": False}), child


def _processing_material(host: Any, state: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    if any(entry["receipt"]["status"] in {"prepared", "recovery_required"}
           or (entry.get("operations") and entry["receipt"]["status"] not in _FINAL | {"cancelled"})
           for entry in work["items"].values()):
        raise V2Error("RECOVERY_REQUIRED", "Frozen operations must be resolved before a semantic budget is added",
                      next_action="resume")
    if work.get("processing_child_id") or work.get("recovery_child_id"):
        raise V2Error("WORK_TERMINAL", "This work already has an authorized child")
    focus = [item_id for item_id, entry in work["items"].items()
             if entry["receipt"]["status"] not in _FINAL | {"cancelled"}]
    if work["items"] and not focus:
        raise V2Error("WORK_TERMINAL", "No unresolved semantic item remains")
    host._verify_source(state, work)
    source_ids = work.get("source_order", work["source_ids"])
    sources = []
    for source_id in source_ids:
        source = state["sources"].get(source_id)
        if source is None:
            raise V2Error("SOURCE_CHANGED")
        sources.append(deepcopy(source))
    scan = scan_memories(host.vault, include_history=False)
    if scan.issues:
        raise V2Error("STATE_CORRUPT", "Current comparison resources are incomplete")
    current_targets = sorted([{"memory_id": record.memory.memory_id,
        "revision": revision(record.memory), "scopes": list(record.memory.scopes),
        "path": str(record.path.relative_to(host.vault.root))} for record in scan.records],
        key=lambda target: (target["memory_id"], target["path"]))
    ensure_scan_current(host.vault, scan)
    from .incremental_native import read_comparison
    native = read_comparison(host.service, work["principal_id"])
    return {"work": deepcopy(work), "sources": sources, "current_targets": current_targets,
            "native_guard": native.guard,
            "native_targets": sorted([{"memory_id": memory.memory_id, "revision": revision(memory),
                "scopes": list(memory.scopes)} for memory in native.memories.values()],
                key=lambda target: target["memory_id"]),
            "scope_policy": host.vault.config(), "focus_item_ids": focus}


def _processing_plan(host: Any, state: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    material = _processing_material(host, state, work)
    return {"kind": "authorized_processing_plan", "target_work_id": work["work_id"],
            "plan_digest": "sha256:" + digest(material),
            "original_budget_owner_work_id": work["budget_owner_work_id"],
            "focus_item_ids": material["focus_item_ids"], "semantic_budget_added": 3}


def processing_plan(host: Any, work_id: str) -> dict[str, Any]:
    """Owner approves new semantic attempts independently of I/O recovery."""
    with host.vault.lock():
        state = host._load()
        return _processing_plan(host, state, _work(state, work_id))


def prepare_authorized_processing(host: Any, state: dict[str, Any], context: dict[str, Any],
                                   args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Caller holds the Vault lock. Bind a separately approved semantic budget."""
    approval_ref = args["source"]["approval_ref"]
    approval = host.auth._load()["approvals"].get(approval_ref)
    if not isinstance(approval, dict) or approval.get("kind") != "new_processing_budget":
        raise V2Error("AUTH_RENEWAL_REQUIRED", next_action="request_owner_approval")
    parent = _work(state, approval.get("target_work_id"))
    child_id = "work_processing_" + digest(approval_ref)[:32]
    previous = state["works"].get(child_id)
    if previous is not None:
        if (parent.get("processing_child_id") != child_id
                or previous.get("processing_approval_ref") != approval_ref):
            raise V2Error("STATE_CORRUPT")
        host.auth.check(context, "work.read", work=previous)
        if any(previous[key] != context[key] for key in ("grant_id", "grant_epoch", "principal_id")):
            raise V2Error("AUTH_RENEWAL_REQUIRED")
        return success({"kind": "prepared", "work": host._summary(state, previous),
                        "snapshot": host._public_snapshot(previous), "replayed": True}), previous
    plan = _processing_plan(host, state, parent)
    host.auth.check(context, "work.read", work=parent)
    host.auth.check(context, "source.capture")
    host.auth.check(context, "source.read")
    host.auth.check(context, "memory.write")
    if (parent["principal_id"] != context["principal_id"]
            and not context["allow_other_work_resume"]):
        raise V2Error("RESOURCE_UNAVAILABLE")
    for source_id in parent.get("source_order", parent["source_ids"]):
        source = state["sources"][source_id]
        host._check_source(context, source)
    child = deepcopy(parent)
    child.update(work_id=child_id, related_work_id=parent["work_id"],
        processing_parent_id=parent["work_id"], processing_approval_ref=approval_ref,
        processing_plan_digest=plan["plan_digest"], budget_owner_work_id=child_id, budget_used=0,
        work_revision=1, status="pending", terminal=False, stop_reason="needs_host",
        authorization_state="active", scope_revision="sha256:" + digest(host.vault.config()),
        **{key: context[key] for key in ("principal_id", "authorization_domain", "grant_id", "grant_epoch")})
    for key in ("processing_child_id", "recovery_child_id", "recovery_parent_id",
                "recovery_approval_ref", "recovery_plan_digest"):
        child.pop(key, None)
    if child["purpose"] == "authorized_recovery":
        raise V2Error("RECOVERY_REQUIRED", "An I/O recovery child cannot mint semantic work", next_action="contact_owner")
    for entry in child["items"].values():
        entry.pop("transferred_to_work_id", None)
        if entry["receipt"]["status"] in _FINAL | {"cancelled"}:
            entry["receipt"]["inherited_from_submission"] = entry["submission"]
    if any(entry["receipt"]["status"] in _FINAL | {"cancelled"}
           for entry in child["items"].values()):
        child["status"] = "partial"
    child["snapshot"] = host._snapshot(state, context, child)
    if approval.get("used") is True:
        host.auth.check(context)
        if (approval.get("consumption_id") != child_id
                or approval.get("issued_by") != "vault_owner_control_plane"
                or approval.get("plan_digest") != plan["plan_digest"]
                or any(approval.get(key) != context[key] for key in
                       ("principal_id", "authorization_domain", "grant_id", "grant_epoch"))):
            raise V2Error("AUTH_RENEWAL_REQUIRED", next_action="request_owner_approval")
    else:
        host.auth.consume_approval(context, approval_ref, "new_processing_budget",
            plan["plan_digest"], parent["work_id"], locked=True, consumption_id=child_id)
    parent["processing_child_id"] = child_id
    parent["work_revision"] += 1
    state["works"][child_id] = child
    host._save(state)
    return success({"kind": "prepared", "work": host._summary(state, child),
                    "snapshot": host._public_snapshot(child), "replayed": False}), child


def _confirmed_messages(messages: Any) -> list[dict[str, Any]]:
    """Validate the whole batch before creating any owner authorization."""
    if not isinstance(messages, list) or not 1 <= len(messages) <= 16:
        raise V2Error("INVALID_SCHEMA", "Owner confirmation requires 1 to 16 visible messages")
    from .redaction import redact_text
    result = []
    for message in messages:
        if (not isinstance(message, dict) or set(message) - {"role", "text", "source_time", "timezone"}
                or not isinstance(message.get("role"), str) or message["role"] not in {"user", "assistant"}
                or not isinstance(message.get("text"), str) or not message["text"].strip()
                or len(message["text"]) > 16000 or "\x00" in message["text"]):
            raise V2Error("INVALID_SCHEMA", "Owner confirmation accepts only visible user/assistant text")
        source_time, zone = message.get("source_time"), message.get("timezone")
        if source_time is not None:
            try:
                if (not isinstance(source_time, str) or len(source_time) > 64 or not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", source_time)):
                    raise ValueError()
                parsed = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
                if "T" not in source_time or parsed.tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise V2Error("INVALID_SCHEMA", "Confirmed source time must include its actual offset") from None
        # Preserve the owner's reported timezone rather than deriving one from
        # this process, the offset, or the date of the confirmation transaction.
        if zone is not None and (not isinstance(zone, str) or not zone.strip()
                or len(zone) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in zone)):
            raise V2Error("INVALID_SCHEMA", "Invalid confirmed source timezone")
        result.append({"role": message["role"],
            "text": redact_text(message["text"]).replace("\r\n", "\n").replace("\r", "\n"),
            "source_time": source_time, "timezone": zone})
    if len(canonical(result).encode("utf-8")) > 131072:
        raise V2Error("INPUT_TOO_LARGE", "Owner confirmation batch exceeds the source bound")
    return result


def register_confirmed_sources(host: Any, grant_id: str,
                               messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Owner confirms exact visible messages for an active, strict MCP grant.

This is an owner entry point, not a model tool. Repeating it is a NEW explicit
owner confirmation, which may distinguish genuinely separate identical events.
On a storage failure after approval consumption, callers must inspect the given
approval identity before making another confirmation; no automatic retry invents
an event or silently upgrades a caller assertion.
"""
    confirmed = _confirmed_messages(messages)
    # Owner approval creation owns its own lock, so do not nest it inside the
    # business file lock. Recheck current epoch after acquiring that lock below.
    with host.vault.lock():
        grant = host.auth._active(host.auth._load(), grant_id)
        context = {key: deepcopy(value) for key, value in grant.items()
                   if key != "credential_digest"}
        context["accept_caller_asserted"] = "caller_asserted" in grant["admissible_source_trust"]
        host.auth.check(context, "source.capture")
        if "user_confirmed" not in context["admissible_source_trust"]:
            raise V2Error("SOURCE_NOT_ADMISSIBLE", "This grant does not accept owner-confirmed sources")
    plan_digest = "sha256:" + digest({"authorization_domain": context["authorization_domain"],
        "messages": confirmed})
    approval = host.auth.approve(context, "source_confirmation", plan_digest)
    transaction_id = "source_registration_" + digest(approval["approval_id"])[:32]
    with host.vault.lock():
        context = host.auth.check(context, "source.capture")
        state = host._load()
        records = []
        for sequence, message in enumerate(confirmed, 1):
            source = host._source(state, context, message["role"], message["text"],
                trust="user_confirmed", origin={"approval_id": approval["approval_id"], "message_sequence": sequence})
            source["source_time"] = message["source_time"]
            source["timezone"] = message["timezone"]
            source["revision"] = "sha256:" + digest({"role": source["role"], "text": source["text"],
                "source_time": source["source_time"], "timezone": source["timezone"]})
            records.append(source)
        host.auth.consume_approval(context, approval["approval_id"], "source_confirmation",
            plan_digest, locked=True, consumption_id=transaction_id)
        try:
            host._save(state)
        except OSError as error:
            raise V2Error("IO_INTERRUPTED",
                "Owner confirmation was consumed; inspect approval " + approval["approval_id"]
                + " before confirming again", next_action="contact_owner") from error
    return {"source_ids": [record["source_id"] for record in records], "trust": "user_confirmed",
            "approval_ref": approval["approval_id"]}
