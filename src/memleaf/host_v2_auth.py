"""Local owner authorization for the model-free MCP host profile.

Control-plane methods belong to the owner CLI, never the model's tool set.
Business callers hold ``vault.lock()`` across check and the authorized write;
``check`` deliberately does not acquire another (non-reentrant) file lock.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .host_v2_common import V2Error
from .locking import atomic_write_json
from .scope_state import ScopeError, validate_scope_key
from .state_layout import StateLayoutError, control_required, require_control


PERMISSIONS = frozenset({
    "memory.search", "memory.read", "memory.read_history", "source.capture",
    "source.read", "work.read", "memory.write", "memory.retract",
    "memory.maintain", "memory.delete", "work.cancel", "work.resume",
})
APPROVAL_KINDS = frozenset({
    "source_confirmation", "permanent_delete", "body_compaction",
    "resume_after_revocation", "new_processing_budget",
})
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WRITE_PERMISSIONS = frozenset({
    "memory.write", "memory.retract", "memory.maintain", "memory.delete",
})
_APPROVAL_ERRORS = {
    "source_confirmation": "SOURCE_CONFIRMATION_REQUIRED",
    "permanent_delete": "DELETE_APPROVAL_REQUIRED",
    "body_compaction": "MAINTENANCE_AUTH_REQUIRED",
    "resume_after_revocation": "AUTH_RENEWAL_REQUIRED",
    "new_processing_budget": "AUTH_RENEWAL_REQUIRED",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include time zone")
    return parsed.astimezone(timezone.utc)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise V2Error("INVALID_SCHEMA", "Invalid authorization identifier")
    return value


def _scopes(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > 64:
        raise V2Error("INVALID_SCHEMA", "Invalid authorization scopes")
    try:
        result = [validate_scope_key(value) for value in values]
    except ScopeError as error:
        raise V2Error("INVALID_SCHEMA", "Invalid authorization scopes") from error
    if len(set(result)) != len(result):
        raise V2Error("INVALID_SCHEMA", "Duplicate authorization scope")
    return result


def _permissions(values: Any) -> list[str]:
    if (not isinstance(values, (list, tuple, set, frozenset))
            or len(values) > len(PERMISSIONS)
            or any(not isinstance(value, str) or value not in PERMISSIONS for value in values)
            or len(set(values)) != len(values)):
        raise V2Error("INVALID_SCHEMA", "Invalid authorization permissions")
    return sorted(values)


def _context(grant: Mapping[str, Any]) -> dict[str, Any]:
    value = {key: copy.deepcopy(item) for key, item in grant.items()
             if key != "credential_digest"}
    value["accept_caller_asserted"] = "caller_asserted" in grant["admissible_source_trust"]
    return value


class HostAuthorization:
    """Durable grants and exact, one-use owner approvals for one Vault."""

    def __init__(self, vault: Any):
        self.vault = vault
        self.path = vault._inside("_state", "host_v2_authorization.json")

    def _load(self) -> dict[str, Any]:
        try:
            required = control_required(self.path)
        except StateLayoutError as error:
            raise V2Error("STATE_CORRUPT", "Authorization control invariant is invalid") from error
        if self.path.is_symlink():
            raise V2Error("STATE_CORRUPT", "Authorization state is unavailable")
        if not self.path.exists():
            if required:
                raise V2Error("STATE_CORRUPT", "Authorization state is missing")
            return {"version": 1, "grants": {}, "approvals": {}, "barriers": {}}
        try:
            with self.path.open(encoding="utf-8") as stream:
                state = json.load(stream)
            if (not isinstance(state, dict) or state.get("version") != 1
                    or any(not isinstance(state.get(key), dict)
                           for key in ("grants", "approvals", "barriers"))):
                raise ValueError("invalid state")
            for key, grant in state["grants"].items():
                if (not isinstance(grant, dict) or key != grant.get("grant_id")
                        or grant.get("state") not in {"active", "revoked", "expired"}
                        or type(grant.get("grant_epoch")) is not int
                        or not 1 <= grant["grant_epoch"] <= 2147483647
                        or not isinstance(grant.get("credential_digest"), str)
                        or not _DIGEST.fullmatch(grant["credential_digest"])):
                    raise ValueError("invalid grant")
                for field in ("principal_id", "authorization_domain", "grant_id"):
                    _identifier(grant.get(field))
                _permissions(grant.get("permissions"))
                _scopes(grant.get("read_scopes"))
                _scopes(grant.get("write_scopes"))
                trust = grant.get("admissible_source_trust")
                if (not isinstance(trust, list) or not trust
                        or len(trust) != len(set(trust))
                        or any(item not in {"host_bound", "user_confirmed", "caller_asserted"}
                               for item in trust)
                        or type(grant.get("allow_automatic_recording")) is not bool
                        or type(grant.get("allow_other_work_resume")) is not bool
                        or grant.get("issued_by") != "vault_owner_control_plane"):
                    raise ValueError("invalid grant policy")
                if grant.get("expires_at") is not None:
                    _date(grant["expires_at"])
            return state
        except (OSError, ValueError, TypeError, KeyError, V2Error) as error:
            raise V2Error("STATE_CORRUPT", "Authorization state is invalid") from error

    def _save(self, state: dict[str, Any]) -> None:
        try:
            require_control(self.vault, self.path.name)
        except StateLayoutError as error:
            raise V2Error("STATE_CORRUPT", "Authorization control invariant is invalid") from error
        atomic_write_json(self.path, state, mode=0o600)

    def _active(self, state: Mapping[str, Any], grant_id: Any,
                expected_epoch: Any = None) -> dict[str, Any]:
        grant = state["grants"].get(grant_id) if isinstance(grant_id, str) else None
        if grant is None:
            raise V2Error("AUTH_REQUIRED", "Valid local authorization is required",
                          next_action="authenticate")
        if grant["state"] == "revoked" or (expected_epoch is not None
                and grant["grant_epoch"] != expected_epoch):
            raise V2Error("AUTH_REVOKED", "Authorization has been revoked",
                          next_action="new_authorization")
        if grant["state"] == "expired" or (grant["expires_at"] is not None
                and _date(grant["expires_at"]) <= _now()):
            raise V2Error("AUTH_EXPIRED", "Authorization has expired",
                          next_action="new_authorization")
        return grant

    def grant(self, principal_id: str, permissions: Any, read_scopes: Any,
              write_scopes: Any, *, accept_caller_asserted: bool = False,
              authorization_domain: str | None = None,
              allow_other_work_resume: bool = False,
              allow_automatic_recording: bool = True,
              expires_at: str | None = None,
              admissible_source_trust: Any = None) -> dict[str, Any]:
        """Owner-only setup. Return the random credential once; store its hash."""
        principal_id = _identifier(principal_id)
        domain = _identifier(authorization_domain or "domain-" + uuid.uuid4().hex)
        permissions = _permissions(permissions)
        read_scopes, write_scopes = _scopes(read_scopes), _scopes(write_scopes)
        if any(type(value) is not bool for value in (accept_caller_asserted,
                allow_other_work_resume, allow_automatic_recording)):
            raise V2Error("INVALID_SCHEMA", "Invalid authorization policy")
        if (admissible_source_trust is not None
                and not isinstance(admissible_source_trust, (list, tuple))):
            raise V2Error("INVALID_SCHEMA", "Invalid source trust policy")
        trust = ["host_bound", "user_confirmed"] if admissible_source_trust is None else list(admissible_source_trust)
        if accept_caller_asserted and "caller_asserted" not in trust:
            trust.append("caller_asserted")
        if (not trust or any(not isinstance(value, str) or value not in {"host_bound", "user_confirmed", "caller_asserted"}
                             for value in trust) or len(trust) != len(set(trust))
                or ("caller_asserted" in trust) != accept_caller_asserted):
            raise V2Error("INVALID_SCHEMA", "Invalid source trust policy")
        if expires_at is not None:
            try:
                expiry = _date(expires_at)
            except ValueError as error:
                raise V2Error("INVALID_SCHEMA", "Invalid authorization expiry") from error
            if expiry <= _now():
                raise V2Error("INVALID_SCHEMA", "Authorization expiry must be in the future")
            expires_at = _timestamp(expiry)
        token = secrets.token_urlsafe(32)
        grant = {
            "principal_id": principal_id, "authorization_domain": domain,
            "grant_id": "grant-" + uuid.uuid4().hex, "grant_epoch": 1,
            "state": "active", "permissions": permissions,
            "read_scopes": read_scopes, "write_scopes": write_scopes,
            "admissible_source_trust": trust,
            "allow_automatic_recording": allow_automatic_recording,
            "allow_other_work_resume": allow_other_work_resume,
            "expires_at": expires_at, "issued_by": "vault_owner_control_plane",
            "credential_digest": "sha256:" + hashlib.sha256(token.encode()).hexdigest(),
        }
        with self.vault.lock():
            state = self._load()
            state["grants"][grant["grant_id"]] = grant
            self._save(state)
        return {"token": token, "grant": _context(grant)}

    def authenticate(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 256:
            raise V2Error("AUTH_REQUIRED", "Valid local authorization is required",
                          next_action="authenticate")
        digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        state = self._load()
        for grant in state["grants"].values():
            if hmac.compare_digest(digest, grant["credential_digest"]):
                return _context(self._active(state, grant["grant_id"]))
        raise V2Error("AUTH_REQUIRED", "Valid local authorization is required",
                      next_action="authenticate")

    def check(self, context: Mapping[str, Any], permission: str | None = None,
              scopes: Any = (), work: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Reread current authority, including before replay and each actual write."""
        if (not isinstance(context, Mapping) or type(context.get("grant_epoch")) is not int):
            raise V2Error("AUTH_REQUIRED", "Valid local authorization is required")
        state = self._load()
        grant = self._active(state, context.get("grant_id"), context["grant_epoch"])
        if any(context.get(key) != grant[key] for key in ("principal_id", "authorization_domain")):
            raise V2Error("AUTH_REQUIRED", "Valid local authorization is required")
        if permission is not None and permission not in grant["permissions"]:
            raise V2Error("FORBIDDEN", "Operation is not authorized")
        if scopes:
            requested = _scopes(list(scopes))
            allowed = grant["write_scopes"] if permission in _WRITE_PERMISSIONS else grant["read_scopes"]
            if not set(requested) <= set(allowed):
                code = "SCOPE_DENIED" if permission in _WRITE_PERMISSIONS else "RESOURCE_UNAVAILABLE"
                raise V2Error(code, "Requested resource is unavailable")
        if work is not None:
            if work.get("authorization_domain") != grant["authorization_domain"]:
                raise V2Error("RESOURCE_UNAVAILABLE", "Requested resource is unavailable")
            if permission in {"work.resume", "work.cancel"}:
                if (work.get("principal_id") != grant["principal_id"]
                        and not grant["allow_other_work_resume"]):
                    raise V2Error("RESOURCE_UNAVAILABLE", "Requested resource is unavailable")
            # A refreshed connection must not turn a changed/replaced grant into
            # renewed authority for a frozen work. The rule applies equally to
            # submission, maintenance and refresh, not only the resume tool.
            if permission in _WRITE_PERMISSIONS | {"work.resume", "source.capture"}:
                if type(work.get("grant_epoch")) is not int:
                    raise V2Error("RESOURCE_UNAVAILABLE", "Requested resource is unavailable")
                original = self._active(state, work.get("grant_id"), work.get("grant_epoch"))
                if any(work.get(key) != original[key] for key in ("principal_id", "authorization_domain")):
                    raise V2Error("RESOURCE_UNAVAILABLE", "Requested resource is unavailable")
        return _context(grant)

    def list_grants(self) -> list[dict[str, Any]]:
        """Owner inspection, with no credentials or credential hashes."""
        return [_context(grant) for grant in self._load()["grants"].values()]

    def revoke(self, grant_id: str) -> dict[str, Any]:
        with self.vault.lock():
            state = self._load()
            grant = state["grants"].get(grant_id)
            if grant is None:
                raise V2Error("RESOURCE_UNAVAILABLE", "Requested authorization is unavailable")
            if grant["state"] != "revoked":
                if grant["grant_epoch"] >= 2147483647:
                    raise V2Error("CAPACITY_EXCEEDED", "Authorization epoch capacity is exhausted")
                grant["grant_epoch"] += 1
                grant["state"] = "revoked"
                state["barriers"][grant_id] = {"grant_epoch": grant["grant_epoch"],
                    "effective_at": _timestamp(_now()), "reason": "revoked"}
                self._save(state)
            return _context(grant)

    def alter_grant(self, grant_id: str, *, permissions: Any = None,
                    read_scopes: Any = None, write_scopes: Any = None) -> dict[str, Any]:
        """Owner policy changes invalidate all contexts, work and old approvals."""
        with self.vault.lock():
            state = self._load()
            grant = self._active(state, grant_id)
            changes = {}
            if permissions is not None:
                changes["permissions"] = _permissions(permissions)
            if read_scopes is not None:
                changes["read_scopes"] = _scopes(read_scopes)
            if write_scopes is not None:
                changes["write_scopes"] = _scopes(write_scopes)
            if any(grant[key] != value for key, value in changes.items()):
                if grant["grant_epoch"] >= 2147483647:
                    raise V2Error("CAPACITY_EXCEEDED", "Authorization epoch capacity is exhausted")
                grant.update(changes)
                grant["grant_epoch"] += 1
                state["barriers"][grant_id] = {"grant_epoch": grant["grant_epoch"],
                    "effective_at": _timestamp(_now()), "reason": "policy_changed"}
                self._save(state)
            return _context(grant)

    def approve(self, context_or_grant_id: Mapping[str, Any] | str, kind: str,
                plan_digest: str, work_id: str | None = None, *,
                expires_in: int = 900) -> dict[str, Any]:
        """Owner creates an approval bound to the exact plan and current grant."""
        if (kind not in APPROVAL_KINDS or not isinstance(plan_digest, str)
                or not _DIGEST.fullmatch(plan_digest) or type(expires_in) is not int
                or not 1 <= expires_in <= 86400):
            raise V2Error("INVALID_SCHEMA", "Invalid approval plan")
        if work_id is not None:
            _identifier(work_id)
        with self.vault.lock():
            state = self._load()
            if isinstance(context_or_grant_id, Mapping):
                context = self.check(context_or_grant_id)
                grant = state["grants"][context["grant_id"]]
            else:
                grant = self._active(state, context_or_grant_id)
            approval = {
                "approval_id": "approval-" + uuid.uuid4().hex,
                "kind": kind, "principal_id": grant["principal_id"],
                "authorization_domain": grant["authorization_domain"],
                "grant_id": grant["grant_id"], "grant_epoch": grant["grant_epoch"],
                "target_work_id": work_id, "plan_digest": plan_digest,
                "used": False, "expires_at": _timestamp(_now() + timedelta(seconds=expires_in)),
                "issued_by": "vault_owner_control_plane",
            }
            state["approvals"][approval["approval_id"]] = approval
            self._save(state)
            return copy.deepcopy(approval)

    def _approval(self, state: dict[str, Any], context: Mapping[str, Any],
                  approval_ref: str, kind: str, plan_digest: str,
                  work_id: str | None) -> dict[str, Any]:
        current = self.check(context)
        record = state["approvals"].get(approval_ref) if isinstance(approval_ref, str) else None
        valid = isinstance(record, dict)
        if valid:
            try:
                valid = (record.get("used") is False and record.get("kind") == kind
                    and record.get("issued_by") == "vault_owner_control_plane"
                    and record.get("plan_digest") == plan_digest
                    and record.get("target_work_id") == work_id
                    and all(record.get(key) == current[key] for key in
                            ("principal_id", "authorization_domain", "grant_id", "grant_epoch"))
                    and _date(record.get("expires_at")) > _now())
            except (TypeError, ValueError):
                valid = False
        if not valid:
            raise V2Error(_APPROVAL_ERRORS.get(kind, "FORBIDDEN"),
                          "A current owner approval for this exact plan is required",
                          next_action="request_owner_approval")
        return record

    def validate_approval(self, context: Mapping[str, Any], approval_ref: str,
                          kind: str, plan_digest: str,
                          work_id: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._approval(self._load(), context, approval_ref,
                                            kind, plan_digest, work_id))

    def consume_approval(self, context: Mapping[str, Any], approval_ref: str,
                         kind: str, plan_digest: str, work_id: str | None = None,
                         *, locked: bool = False,
                         consumption_id: str | None = None) -> dict[str, Any]:
        """Consume under the caller's write lock, or acquire it for owner use."""
        if not locked:
            with self.vault.lock():
                return self.consume_approval(context, approval_ref, kind,
                                             plan_digest, work_id, locked=True,
                                             consumption_id=consumption_id)
        if consumption_id is not None:
            _identifier(consumption_id)
        state = self._load()
        record = self._approval(state, context, approval_ref, kind, plan_digest, work_id)
        record["used"] = True
        if consumption_id is not None:
            record["consumption_id"] = consumption_id
            record["consumed_at"] = _timestamp(_now())
        self._save(state)
        return copy.deepcopy(record)
