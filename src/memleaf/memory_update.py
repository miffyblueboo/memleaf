"""Explicit current-record editing with revisions and forward recovery.

Trusted Python callers supply the exact patch and both old/new write scopes.
There is no semantic inference, source impersonation, automatic upsert, or new
MCP surface. Shared history/head, scan, lock and atomic JSON helpers do the IO.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .incremental_scopes import registry_view
from .locking import atomic_unlink, atomic_write_json, read_json
from .memory_retraction import journal_identities
from .memory_writer import MemoryWriter
from .models import Memory, MemoryVersionError, utc_now
from .query_scan import ensure_scan_current
from .scope_state import normalize_scopes
from .turn_plan import MAX_PLAN_BYTES, revision_digest
from .validation import parse_strict_json
from .vault import safe_component

_FIELDS = frozenset({"title", "body", "tags", "aliases", "keywords", "scopes", "status", "actionable",
                     "completed_at", "due_date", "due_text", "assignee", "waiting_on", "validity", "type"})
_TODO = frozenset({"status", "completed_at", "due_date", "due_text", "assignee", "waiting_on"})
_GROUP = {"title": "content", "body": "content", "type": "content", "scopes": "scope",
          "actionable": "status",
          "status": "status", "completed_at": "status", "validity": "validity",
          "due_date": "deadline", "due_text": "deadline", "assignee": "responsibility", "waiting_on": "responsibility"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _legacy_request(patch: Mapping[str, Any], authorized_scopes: Any, *, reopen: bool,
             restore: bool, allow_type_change: bool) -> dict:
    if any(type(v) is not bool for v in (reopen, restore, allow_type_change)):
        raise ValueError("update flags must be booleans")
    if not isinstance(patch, Mapping) or not patch or set(patch) - _FIELDS:
        raise ValueError("invalid explicit update fields")
    values = deepcopy(dict(patch))
    if len(_json(values).encode("utf-8")) > 64 * 1024:
        raise ValueError("explicit update exceeds byte budget")
    if "type" in values and not allow_type_change:
        raise ValueError("explicit type correction required")
    for key, value in values.items():
        if key in {"scopes", "tags", "aliases", "keywords"}:
            if not isinstance(value, list) or len(value) > 64 or any(
                    not isinstance(x, str) or not x.strip() or len(x) > 256 or "\x00" in x for x in value):
                raise ValueError("invalid explicit update list")
            if key == "scopes":
                values[key] = normalize_scopes(value)
        elif key == "status":
            if value not in (None, "active", "completed", "cancelled"):
                raise ValueError("invalid explicit task status")
        elif key == "actionable":
            if type(value) is not bool:
                raise ValueError("invalid explicit action marker")
        elif key == "validity":
            if value not in ("valid", "retracted"):
                raise ValueError("invalid explicit validity")
        elif key == "type":
            if value not in ("fact", "todo", "preference", "project", "event", "identity", "other"):
                raise ValueError("invalid explicit type")
        elif value is None:
            if key not in _TODO:
                raise ValueError("field cannot be cleared")
        elif not isinstance(value, str) or "\x00" in value or len(value) > (16384 if key == "body" else 512):
            raise ValueError("invalid explicit update text")
        elif key != "body" and not value.strip():
            raise ValueError("empty explicit update text")
    if "completed_at" in values and values["completed_at"] is not None:
        from .incremental_dates import parse_source_time
        parse_source_time(values["completed_at"])
    if not isinstance(authorized_scopes, list):
        raise ValueError("authorized_scopes must be an explicit list")
    scopes = sorted(normalize_scopes(authorized_scopes))
    if len(scopes) > 256:
        raise ValueError("explicit scope budget exceeded")
    return {"patch": values, "authorized_scopes": scopes, "reopen": reopen,
            "restore": restore, "allow_type_change": allow_type_change}


def _legacy_patched(current: Memory, request: Mapping[str, Any], now: str) -> Memory:
    patch = request["patch"]
    value = deepcopy(current.to_dict())
    value.update(deepcopy(patch))
    if value.get("actionable") and value.get("status") is None:
        value["status"] = "active"
    if set(current.scopes) - set(request["authorized_scopes"]) or set(value["scopes"]) - set(request["authorized_scopes"]):
        raise ValueError("blocked_scope")
    if current.status in {"completed", "cancelled"} and value.get("status") == "active" and not request["reopen"]:
        raise ValueError("explicit_reopen_required")
    if current.validity == "retracted" and value["validity"] == "valid" and not (
            request["restore"] and isinstance(patch.get("body"), str) and patch["body"].strip()):
        raise ValueError("explicit_restore_required")
    if value["validity"] == "retracted":
        if patch.get("body"):
            raise ValueError("retracted_body_must_be_empty")
        value["body"] = ""
    if value["type"] == "todo" and value.get("status") not in {"active", "completed", "cancelled"}:
        raise ValueError("missing_status")
    if "status" in patch and patch["status"] != current.status and "completed_at" not in patch:
        value.pop("completed_at", None)
    if value.get("completed_at") is not None and value.get("status") != "completed":
        raise ValueError("completion_time_requires_completed_status")
    if value["validity"] == "valid" and not value["body"].strip():
        raise ValueError("valid_assertion_body_cannot_be_empty")
    return Memory.from_mapping(value)


def _request(patch: Mapping[str, Any], authorized_scopes: Any, *, reopen: bool = False,
             restore: bool = False, allow_type_change: bool = False,
             source_time: str | None = None) -> dict:
    """One explicit-update contract; v1 readers are compatibility-only."""
    from .incremental_dates import parse_source_time
    parse_source_time(source_time)
    if not isinstance(patch, Mapping) or not patch:
        raise ValueError("invalid explicit update fields")
    fields = deepcopy(dict(patch))
    deadline = fields.pop("deadline", None)
    if "deadline" in patch:
        if set(fields) & {"due_date", "due_text"}:
            raise ValueError("ambiguous_deadline_patch")
        if not isinstance(deadline, dict) or not (
                (set(deadline) == {"clear"} and deadline["clear"] is True) or
                (set(deadline) == {"text"} and isinstance(deadline["text"], str)
                 and deadline["text"].strip() and len(deadline["text"]) <= 512
                 and "\x00" not in deadline["text"])):
            raise ValueError("invalid_update_deadline")
    # A deadline-only patch still passes the same scalar/list validator.
    normalized = _legacy_request(fields or {"body": "validation-only"}, authorized_scopes,
                                reopen=reopen, restore=restore, allow_type_change=allow_type_change)
    if not fields:
        normalized["patch"] = {}
    if "deadline" in patch:
        normalized["patch"]["deadline"] = deepcopy(deadline)
    if reopen and patch.get("status") != "active":
        raise ValueError("invalid_reopen")
    normalized["source_time"] = source_time
    return normalized


def _patched(current: Memory, request: Mapping[str, Any], now: str) -> Memory:
    from .incremental_dates import selected_calendar
    fields = deepcopy(request["patch"])
    if current.validity == "retracted" and not (
            request["restore"] and fields.get("validity") == "valid" and fields.get("body")):
        # A repeated explicit retraction can be an unchanged audit operation.
        if fields != {"validity": "retracted"}:
            raise ValueError("explicit_restore_required")
    deadline = fields.pop("deadline", None)
    derived = {}
    if deadline is not None:
        if deadline.get("clear"):
            fields.update(due_date=None, due_text=None)
            derived["due_status"] = "cleared"
        else:
            selected = selected_calendar(deadline["text"], {
                "text": deadline["text"], "source_time": request.get("source_time")})
            fields.update(due_date=selected["date"], due_text=selected["text"])
            derived["due_status"] = selected["status"]
    changed_request = dict(request, patch=fields)
    after = _legacy_patched(current, changed_request, now)
    if current.type == "todo" and after.type != "todo" and "actionable" not in fields:
        after.actionable = True
    after.extra.update(derived)
    # Compare the same normalized body that a later Markdown read will see.
    return Memory.from_markdown(after.to_markdown())


def pending_explicit_mutations(vault: Any) -> tuple[dict[str, int], dict[str, str], set[str]]:
    """Bounded read-only inventory, including both former candidate formats."""
    from types import SimpleNamespace
    from .memory_retraction import RetractionManager
    service = SimpleNamespace(vault=vault)
    counts = {"updates": 0, "retractions": 0}
    stamps, targets, update_ids = {}, set(), set()
    total = 0
    for directory, label in (("retractions", "retractions"),
                             ("explicit_updates", "updates"), ("memory_updates", "updates")):
        root = vault.state_path / directory
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("invalid_explicit_mutation_directory")
        if not root.exists():
            continue
        paths = []
        for path in root.iterdir():
            paths.append(path)
            if len(paths) > 128:
                raise ValueError("explicit_mutation_inventory_limit")
        seen = set()
        for path in sorted(paths):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ValueError("invalid_explicit_mutation_file")
            safe_component(path.stem, "memory id")
            identity = path.stem.casefold()
            if identity in seen or (label == "updates" and identity in update_ids):
                raise ValueError("duplicate_explicit_mutation_identity")
            seen.add(identity)
            if label == "updates":
                update_ids.add(identity)
            with path.open("rb") as stream:
                raw = stream.read(MAX_PLAN_BYTES * 2 + 1)
            total += len(raw)
            if len(raw) > MAX_PLAN_BYTES * 2 or total > 32 * 1024 * 1024:
                raise ValueError("explicit_mutation_inventory_limit")
            manager = RetractionManager(service) if label == "retractions" else ExplicitUpdateManager(service)
            plan = manager._load(path.stem)
            with path.open("rb") as stream:
                after = stream.read(MAX_PLAN_BYTES * 2 + 1)
            if plan is None or after != raw:
                raise ValueError("explicit_mutation_inventory_changed")
            targets.add(plan["memory_id"].casefold())
            counts[label] += 1
            stamps[f"{directory}/{path.name}"] = hashlib.sha256(raw).hexdigest()
        if set(paths) != set(root.iterdir()):
            raise ValueError("explicit_mutation_inventory_changed")
    return counts, stamps, targets


def explicit_mutation_inventory(vault: Any) -> dict:
    counts, stamps, _ = pending_explicit_mutations(vault)
    return {"retractions": counts["retractions"], "explicit_updates": counts["updates"],
            "generation": hashlib.sha256(_json(stamps).encode()).hexdigest()}


class MemoryUpdateCommitError(OSError):
    def __init__(self, *, applied: bool | None, memory_id: str, operation_id: str):
        super().__init__("explicit update incomplete; retry the same request")
        self.applied = applied
        self.recovery_required = True
        self.result = {"execution_status": "recovery_required", "action": "UPDATE",
                       "memory_id": memory_id, "operation_id": operation_id,
                       "applied": applied, "index_status": "unknown", "model_calls": 0}


ExplicitUpdateError = MemoryUpdateCommitError


class ExplicitUpdateManager:
    def __init__(self, service: Any):
        self.service = service

    def _path(self, identity: str) -> Path:
        safe_component(identity, "memory id")
        paths = []
        for directory in ("explicit_updates", "memory_updates"):
            root = self.service.vault.state_path / directory
            path = root / (identity + ".json")
            if root.is_symlink() or (root.exists() and not root.is_dir()) or path.is_symlink():
                raise ValueError("unsafe explicit update journal")
            paths.append(path)
        if all(p.exists() for p in paths):
            raise ValueError("duplicate_explicit_mutation_identity")
        return paths[1] if paths[1].exists() else paths[0]

    @staticmethod
    def _operation(identity: str, expected: str, request: Mapping[str, Any]) -> str:
        return "edit-" + hashlib.sha256(_json([identity, expected, request]).encode()).hexdigest()

    def _load(self, identity: str) -> dict | None:
        path = self._path(identity)
        if not path.exists():
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_PLAN_BYTES * 2 + 1)
        if len(raw) > MAX_PLAN_BYTES * 2:
            raise ValueError("explicit journal exceeds budget")
        stored = parse_strict_json(raw.decode("utf-8"))
        # The narrower candidate used a different v1 wrapper and receipt name.
        # Its original pure validator is retained; all writes use the same writer.
        if isinstance(stored, dict) and "schema_version" in stored:
            from .legacy_update_journal import LegacyMemoryUpdateReader
            plan = LegacyMemoryUpdateReader(self.service)._load(identity)
            if plan is None:
                raise ValueError("legacy update inventory changed")
            return {**plan, "action": "UPDATE", "legacy_kind": "b1",
                    "receipt_key": "explicit_update_operation_id"}
        if (not isinstance(stored, dict) or set(stored) != {"version", "payload", "checksum"}
                or type(stored["version"]) is not int or stored["version"] not in {1, 2}):
            raise ValueError("invalid explicit journal version")
        payload = stored["payload"]
        if (not isinstance(payload, str) or len(payload.encode()) > MAX_PLAN_BYTES
                or hashlib.sha256(payload.encode()).hexdigest() != stored["checksum"]):
            raise ValueError("invalid explicit journal checksum")
        plan = parse_strict_json(payload)
        keys = {"memory_id", "operation_id", "action", "expected_revision", "replacement_revision",
                "prepared_at", "before", "after", "request", "scope_guard", "basis_reset"}
        if (not isinstance(plan, dict) or set(plan) != keys or plan["memory_id"] != identity
                or plan["action"] != "UPDATE" or type(plan["basis_reset"]) is not bool
                or any(not isinstance(plan[k], str) or not plan[k] for k in keys - {"request", "scope_guard", "basis_reset"})):
            raise ValueError("invalid explicit journal shape")
        req = plan["request"]
        expected_keys = {"patch", "authorized_scopes", "reopen", "restore", "allow_type_change"}
        if stored["version"] == 2:
            expected_keys.add("source_time")
        if not isinstance(req, dict) or set(req) != expected_keys:
            raise ValueError("invalid explicit request")
        canonical = _legacy_request(**req) if stored["version"] == 1 else _request(**req)
        if req != canonical:
            raise ValueError("noncanonical explicit request")
        from .incremental_dates import parse_source_time
        from .incremental_scopes import validate_guard
        if parse_source_time(plan["prepared_at"]) is None:
            raise ValueError("missing explicit observation time")
        validate_guard(plan["scope_guard"])
        before, after = Memory.from_markdown(plan["before"]), Memory.from_markdown(plan["after"])
        if (before.memory_id != identity or after.memory_id != identity
                or revision_digest(before) != plan["expected_revision"]
                or revision_digest(after) != plan["replacement_revision"]
                or plan["operation_id"] != self._operation(identity, plan["expected_revision"], req)
                or after.extra.get("explicit_operation_id") != plan["operation_id"]):
            raise ValueError("explicit journal state mismatch")
        if stored["version"] == 1:
            expected_after = self._decorate_v1(_legacy_patched(before, req, plan["prepared_at"]), req,
                plan["operation_id"], plan["prepared_at"], basis_reset=plan["basis_reset"])
        else:
            expected_after = self._decorate(_patched(before, req, plan["prepared_at"]), before, req,
                plan["operation_id"], plan["prepared_at"], basis_reset=plan["basis_reset"])
        if expected_after.to_dict() != after.to_dict():
            raise ValueError("explicit journal patch does not match its frozen result")
        return {**plan, "legacy_kind": "a1" if stored["version"] == 1 else None,
                "receipt_key": "explicit_operation_id"}

    @staticmethod
    def _decorate_v1(after: Memory, request: Mapping[str, Any], opid: str, now: str, *, basis_reset: bool = False) -> Memory:
        after.updated = now
        for key in ("explicit_result_digest", "retraction_operation_id", "retraction_result_digest", "retraction_reason", "retracted_at"):
            after.extra.pop(key, None)
        # This is the observation time of the explicit edit command, not a
        # guessed event completion/effective time or fabricated chat message.
        if basis_reset:
            after.extra["field_basis"] = {}
            after.extra["external_edit_observed_at"] = now
        bases = deepcopy(after.extra.get("field_basis", {}))
        if not isinstance(bases, dict) or any(not isinstance(v, dict) for v in bases.values()):
            raise ValueError("invalid_target_basis")
        for key in request["patch"]:
            if key in _GROUP:
                bases[_GROUP[key]] = {"source": "explicit_edit", "source_time": now}
        after.extra["field_basis"] = bases
        if set(request["patch"]) & {"due_date", "due_text"}:
            after.extra["due_anchor"] = {"source": "explicit_edit", "source_time": now}
            after.extra["due_status"] = "resolved" if after.due_date else "unresolved" if after.extra.get("due_text") else "cleared"
        after.extra["explicit_operation_id"] = opid
        after.extra["explicit_result_digest"] = revision_digest(after)
        return after

    @staticmethod
    def _decorate(after: Memory, before: Memory, request: Mapping[str, Any],
                  opid: str, now: str, *, basis_reset: bool = False) -> Memory:
        from .incremental_protocol import basis_status
        for key in ("explicit_result_digest", "explicit_update_operation_id", "explicit_update_result_digest",
                    "retraction_operation_id", "retraction_result_digest", "retraction_reason", "retracted_at"):
            after.extra.pop(key, None)
        after.updated = now
        bases = deepcopy(after.extra.get("field_basis", {}))
        if not isinstance(bases, dict) or any(not isinstance(v, dict) for v in bases.values()):
            raise ValueError("invalid_target_basis")
        if basis_reset:
            bases = {}
            after.extra["external_edit_observed_at"] = now
        observation = {"source": "explicit_edit", "event_key": opid, "observed_at": now}
        if request.get("source_time") is not None:
            observation["source_time"] = request["source_time"]
        old, new = before.to_dict(), after.to_dict()
        changed = {k for k in request["patch"] if k == "deadline" or old.get(k) != new.get(k)}
        groups = dict(_GROUP, deadline="deadline")
        for key in changed:
            if key in groups:
                bases[groups[key]] = deepcopy(observation)
        after.extra["field_basis"] = bases
        if changed & {"due_date", "due_text", "deadline"}:
            after.extra["due_anchor"] = deepcopy(observation)
            if "deadline" not in request["patch"]:
                after.extra["due_status"] = "resolved" if after.due_date else "unresolved" if after.extra.get("due_text") else "cleared"
        if "scopes" in changed:
            after.scope_source = "explicit"
        after.extra["explicit_operation_id"] = opid
        after.extra["explicit_result_digest"] = revision_digest(after)
        return after

    @staticmethod
    def _result(identity: str, revision: str, action: str, replayed: bool,
                operation: str | None = None) -> dict:
        return {"execution_status": "completed", "memory_id": identity, "revision": revision,
                "action": action, "replayed": replayed, "already_applied": replayed,
                "operation_id": operation, "index_status": "current" if action == "UPDATE" else "not_refreshed",
                "model_calls": 0}

    def _settled_head(self, identity: str, expected: str) -> Memory:
        head, scan = self.service._revision_target_unlocked(identity)
        ensure_scan_current(self.service.vault, scan)
        if head is None or revision_digest(head.memory) != expected:
            raise MemoryVersionError("memory changed during update settlement")
        return head.memory

    def _applied(self, identity: str, before: str, after: str) -> bool | None:
        try:
            head, scan = self.service._revision_target_unlocked(identity)
            ensure_scan_current(self.service.vault, scan)
            actual = revision_digest(head.memory) if head else None
            if actual == after:
                return True
            if actual == before:
                return False
        except (OSError, ValueError, RuntimeError):
            pass
        return None

    @staticmethod
    def _same_request(plan: dict, request: dict) -> bool:
        old = plan["request"]
        if plan.get("legacy_kind") == "b1":
            return (old == {k: request[k] for k in ("patch", "reopen", "source_time")}
                    and not request["allow_type_change"])
        if plan.get("legacy_kind") == "a1":
            return request["source_time"] is None and old == {k:v for k,v in request.items() if k != "source_time"}
        return old == request

    def update_unlocked(self, identity: str, expected: str, request: dict) -> dict:
        record, snapshot = self.service._revision_target_unlocked(identity)
        ensure_scan_current(self.service.vault, snapshot)
        if record is None:
            self._load(identity)
            raise ValueError("memory does not exist")
        current, identity = record.memory, record.memory.memory_id
        revision = revision_digest(current)
        opid = self._operation(identity, expected, request)
        plan = self._load(identity)
        if plan is not None and revision not in {plan["expected_revision"], plan["replacement_revision"]}:
            if expected != revision:
                raise MemoryVersionError("explicit update target changed")
            # Do not erase stale evidence before validating a replacement request.
            plan = None
        replayed = plan is not None and revision == plan["replacement_revision"]
        if plan is not None:
            if expected != plan["expected_revision"] or not self._same_request(plan, request):
                raise MemoryVersionError("another explicit update is pending")
            opid = plan["operation_id"]
        else:
            variants = [("explicit", opid)]
            if request["source_time"] is None:
                variants.append(("explicit", self._operation(identity, expected,
                    {k:v for k,v in request.items() if k != "source_time"})))
            old_b_request = {k:request[k] for k in ("patch", "reopen", "source_time")}
            variants.append(("explicit_update", "update-" + hashlib.sha256(_json([identity,expected,old_b_request]).encode()).hexdigest()))
            for prefix, operation in variants:
                state = current.to_dict()
                receipt = state.pop(prefix + "_result_digest", None)
                if current.extra.get(prefix + "_operation_id") == operation and receipt == revision_digest(state):
                    try:
                        self.service._rebuild_index_unlocked()
                    except OSError as error:
                        raise MemoryUpdateCommitError(applied=self._applied(identity, expected, revision),
                            memory_id=identity, operation_id=operation) from error
                    self._settled_head(identity, revision)
                    return self._result(identity, revision, "UPDATE", True, operation)
            if revision != expected:
                raise MemoryVersionError("explicit update revision mismatch")
            now = utc_now()
            after = _patched(current, request, now)
            registry, guard, _ = registry_view(self.service.vault.config())
            if set(after.scopes) - set(current.scopes) - set(registry) - {"global", "unscoped"}:
                raise ValueError("update_requires_registered_scope")
            if revision_digest(after) == revision:
                ensure_scan_current(self.service.vault, snapshot)
                return self._result(identity, revision, "NO_CHANGE", False)
            counts, stamps, _ = pending_explicit_mutations(self.service.vault)
            if counts["updates"] >= 128 and not self._path(identity).exists():
                raise ValueError("explicit_update_capacity_exhausted")
            from .incremental_protocol import basis_status, applied_revision_index
            from .process_common import _read_processed
            proofs = applied_revision_index(_read_processed(self.service.vault.processed_state_path))
            reset = basis_status(current, proofs) == "external_change_detected"
            after = self._decorate(after, current, request, opid, now, basis_reset=reset)
            plan = {"memory_id": identity, "operation_id": opid, "action": "UPDATE",
                    "expected_revision": expected, "replacement_revision": revision_digest(after),
                    "prepared_at": now, "before": current.to_markdown(), "after": after.to_markdown(),
                    "request": request, "scope_guard": guard, "basis_reset": reset}
            payload = _json(plan)
            if len(payload.encode()) > MAX_PLAN_BYTES:
                raise ValueError("explicit plan exceeds storage budget")
            total = sum((self.service.vault.state_path / name).stat().st_size for name in stamps)
            if total + len(payload.encode()) * 2 > 32 * 1024 * 1024:
                raise ValueError("explicit_update_capacity_exhausted")
            atomic_write_json(self._path(identity), {"version": 2, "payload": payload,
                "checksum": hashlib.sha256(payload.encode()).hexdigest()})
        try:
            ensure_scan_current(self.service.vault, snapshot)
            receipt_key = plan.get("receipt_key", "explicit_operation_id")
            MemoryWriter(self.service).write_frozen_unlocked(plan, receipt_key=receipt_key)
            self.service._rebuild_index_unlocked()
            self._settled_head(identity, plan["replacement_revision"])
            atomic_unlink(self._path(identity))
            return self._result(identity, plan["replacement_revision"], "UPDATE", replayed, opid)
        except OSError as error:
            raise MemoryUpdateCommitError(applied=self._applied(identity, plan["expected_revision"],
                plan["replacement_revision"]), memory_id=identity, operation_id=opid) from error

    def cancel_unlocked(self, records: Any) -> None:
        identities = {r.memory.memory_id.casefold() for r in records}
        identities.update(v.casefold() for r in records for k in ("active_memory_id", "original_memory_id")
                          if isinstance(v := r.memory.extra.get(k), str))
        # Validate the full inventory before removing any frozen plaintext.
        _, stamps, _ = pending_explicit_mutations(self.service.vault)
        for relative in stamps:
            if relative.startswith(("explicit_updates/", "memory_updates/")) and Path(relative).stem.casefold() in identities:
                atomic_unlink(self.service.vault.state_path / relative)


# Compatibility names refer to one implementation, never a second writer.
MemoryUpdateManager = ExplicitUpdateManager
