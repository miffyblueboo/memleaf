"""Host-generated proposals, durable work, and recovery without model calls.

The Vault lock is the authorization revocation and mutation linearization point.
Read operations never recover pending writes. Frozen payloads are written by the
same MemoryWriter used by the existing incremental executor.
"""
from __future__ import annotations

from copy import deepcopy
import json
import time
import uuid
from typing import Any

from .host_v2_common import PROTOCOL, V2Error, canonical, digest, failure, revision, success
from .locking import atomic_write_json, atomic_unlink
from .models import Memory, MemoryVersionError, utc_now
from .memory_writer import MemoryWriter
from .query_scan import scan_memories, ensure_scan_current
from .retrieval import RetrievalError
from .state_layout import require_control, control_required
from .validation import parse_strict_json

CONTROL = "host_v2_work.json"
SETTLED = {"saved", "unchanged", "not_recorded", "cancelled"}
USAGE = {"memleaf_model_calls": 0, "host_model_calls": None, "host_input_tokens": None,
         "host_output_tokens": None, "host_cached_tokens": None, "usage_complete": False,
         "provenance": "unavailable"}


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


class HostMemory:
    def __init__(self, service: Any, token: str):
        from .host_v2_auth import HostAuthorization
        self.service, self.vault, self.token = service, service.vault, token
        self.auth = HostAuthorization(self.vault)
        self.path = self.vault._inside("_state", CONTROL)

    def _load(self) -> dict[str, Any]:
        # Establishing a host ledger never replaces a missing required ledger.
        try:
            required = control_required(self.path)
        except ValueError as exc:
            raise V2Error("STATE_CORRUPT", "Host work control invariant is invalid") from exc
        if not self.path.exists():
            if required:
                raise V2Error("STATE_CORRUPT", "Required host work ledger is missing")
            return {"works": {}, "sources": {}, "requests": {}, "cursors": {}, "plans": {}}
        try:
            if self.path.is_symlink():
                raise ValueError()
            value = json.loads(self.path.read_text(encoding="utf-8"))
            payload = value["payload"]
            if value["version"] != 1 or value["checksum"] != digest(payload):
                raise ValueError()
            if any(not isinstance(payload[k], dict) for k in ("works", "sources", "requests", "cursors", "plans")):
                raise ValueError()
            # Reject impossible saved receipts left by older interrupted
            # freezing, including cached replay of a false completion.
            for work in payload["works"].values():
                for entry in work["items"].values():
                    receipt = entry["receipt"]
                    if receipt["status"] == "saved":
                        ids = receipt["memory_ids"]
                        committed = receipt["committed_revisions"]
                        if (not ids or len(ids) != len(set(ids))
                                or [row["memory_id"] for row in committed] != ids
                                or receipt["applied"] is not True or receipt["settled"] is not True):
                            raise ValueError()
            return payload
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise V2Error("STATE_CORRUPT", "Host work ledger cannot be verified") from exc

    def _save(self, state: dict[str, Any]) -> None:
        if len(state["works"]) > 10000 or len(state["requests"]) > 50000:
            raise V2Error("CAPACITY_EXCEEDED", "Retained work capacity reached; no identity was discarded")
        # The invariant is durable BEFORE any frozen business write.
        require_control(self.vault, CONTROL)
        atomic_write_json(self.path, {"version": 1, "checksum": digest(state), "payload": state})

    def _context(self) -> dict[str, Any]:
        return self.auth.authenticate(self.token)

    def _work(self, state: dict[str, Any], context: dict[str, Any], work_id: str,
              permission: str = "work.read") -> dict[str, Any]:
        work = state["works"].get(work_id)
        if work is None:
            raise V2Error("RESOURCE_UNAVAILABLE")
        self.auth.check(context, permission, work=work)
        current_ids = set()
        for target in work.get("snapshot", {}).get("targets", {}).values():
            self.auth.check(context, "memory.read", scopes=target["memory"]["scopes"])
            if target.get("native"):
                from .incremental_native import guard_current
                if not guard_current(self.service, work["principal_id"], work["snapshot"]["native_guard"]):
                    raise V2Error("REVISION_CONFLICT", retryable=True, next_action="refresh")
            else:
                current_ids.add(target["memory"]["memory_id"])
        for item in work.get("items", {}).values():
            current_ids.update(item.get("receipt", {}).get("memory_ids", []))
            for op in item.get("operations", []):
                current_ids.add(op["memory_id"])
                scopes = op.get("scopes") or (Memory.from_markdown(op["after"]).scopes if op.get("after") else [])
                if scopes:
                    self.auth.check(context, "memory.read", scopes=scopes)
        self._authorize_current_heads(context, current_ids, work)
        return work

    def _authorize_current_heads(self, context: dict[str, Any], memory_ids: set[str],
                                 work: dict[str, Any], *, require_present: bool = False) -> None:
        """Authorize present heads; only a complete scan may prove absence.

        Frozen scopes still protect cached content. Current scopes additionally
        protect its identity after a head moves to another authorization scope.
        Receipts for proven absent heads do not require history read authority.
        """
        if not memory_ids:
            return
        scan = scan_memories(self.vault, include_history=False)
        try:
            for memory_id in memory_ids:
                scan.require_identity(memory_id)
            if any(issue.identity is None for issue in scan.issues):
                raise RetrievalError("memory_unreadable", "Current identities cannot be verified")
            heads = {r.memory.memory_id.casefold(): r.memory for r in scan.records}
            for memory_id in memory_ids:
                memory = heads.get(memory_id.casefold())
                if memory is None:
                    if require_present:
                        raise V2Error("RESOURCE_UNAVAILABLE")
                    continue
                self.auth.check(context, "memory.read", scopes=memory.scopes, work=work)
            ensure_scan_current(self.vault, scan)
        except RetrievalError as exc:
            raise V2Error("STATE_CORRUPT", "Current memory identities cannot be verified") from exc

    def _budget(self, state: dict[str, Any], work: dict[str, Any]) -> dict[str, int]:
        used = state["works"][work["budget_owner_work_id"]]["budget_used"]
        return {"limit": 3, "used": used, "remaining": 3 - used}

    def _summary(self, state: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
        counts = {"saved": 0, "unchanged": 0, "not_recorded": 0, "unresolved": 0, "cancelled": 0}
        for item in work["items"].values():
            counts[item["receipt"]["status"] if item["receipt"]["status"] in counts else "unresolved"] += 1
        return {"work_id": work["work_id"], "related_work_id": work.get("related_work_id"),
                "budget_owner_work_id": work["budget_owner_work_id"], "work_revision": work["work_revision"],
                "latest_submission_revision": work["latest_submission_revision"], "status": work["status"],
                "terminal": work["terminal"], "stop_reason": work["stop_reason"], "authorization_state": work.get("authorization_state", "active"),
                "budget": self._budget(state, work), "outcomes": counts,
                "recovery_required": any(i["receipt"]["status"] in {"prepared", "recovery_required"} for i in work["items"].values()),
                "blocked_groups": self._groups(state, work), "index_status": work.get("index_status", "current")}

    def _groups(self, state: dict[str, Any], work: dict[str, Any] | None = None) -> list[str]:
        works = [work] if work else state["works"].values()
        return [i["receipt"]["group_id"] for w in works for i in w["items"].values()
                if not i.get("transferred_to_work_id") and i["receipt"]["status"] in {"prepared", "recovery_required"} and i["receipt"]["group_id"]]

    def _blocked_ids(self, state: dict[str, Any]) -> set[str]:
        return {c["memory_id"] for w in state["works"].values() for i in w["items"].values()
                if not i.get("transferred_to_work_id") and i["receipt"]["status"] in {"prepared", "recovery_required"}
                for c in i.get("operations", [])}

    def _page(self, state: dict[str, Any], context: dict[str, Any], binding: Any,
              rows: list[Any], *, cursor: str | None = None, limit: int = 20) -> tuple[list[Any], dict[str, Any]]:
        key = digest([context["principal_id"], context["grant_id"], context["grant_epoch"], binding])
        offset = 0
        if cursor:
            entry = state["cursors"].get(cursor)
            if entry is None or entry["expires"] < time.time():
                raise V2Error("CURSOR_EXPIRED")
            if entry["binding"] != key:
                raise V2Error("CURSOR_CONTEXT_MISMATCH")
            offset = entry["offset"]
        selected = rows[offset:offset + min(limit, 20)]
        next_cursor = None
        if offset + len(selected) < len(rows):
            if len(state["cursors"]) > 10000:
                state["cursors"] = {k: v for k, v in state["cursors"].items() if v["expires"] >= time.time()}
            if len(state["cursors"]) > 10000:
                raise V2Error("CAPACITY_EXCEEDED")
            next_cursor = _id("page_")
            state["cursors"][next_cursor] = {"binding": key, "offset": offset + len(selected), "expires": time.time() + 900}
        return selected, {"has_more": next_cursor is not None, "next_cursor": next_cursor}

    def _receipt(self, state: dict[str, Any], context: dict[str, Any], work: dict[str, Any],
                 *, replayed: bool = False, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        rows = [deepcopy(i["receipt"]) for i in work["items"].values()]
        for r in rows:
            origin = work["items"][r["item_id"]]["submission"]
            if origin != work["latest_submission_revision"] and r["status"] in SETTLED:
                r["inherited_from_submission"] = origin
        rows, page = self._page(state, context, ["receipt", work["work_id"], work["work_revision"]], rows, cursor=cursor, limit=limit)
        return {"kind": "receipt", "work": self._summary(state, work),
                "submission_revision": work["latest_submission_revision"], "replayed": replayed,
                "coverage_complete": work["status"] == "completed", "items": rows, "page": page,
                "usage": deepcopy(USAGE), "next_action": "none" if work["terminal"] else
                "resume" if self._summary(state, work)["recovery_required"] else "refresh"}

    def _request_key(self, context: dict[str, Any], tool: str, request_id: str) -> str:
        return digest([context["authorization_domain"], tool, request_id])

    def _replay(self, state: dict[str, Any], context: dict[str, Any], tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        old = state["requests"].get(self._request_key(context, tool, args["request_id"]))
        if old is None:
            return None
        if old["digest"] != digest(args):
            raise V2Error("IDEMPOTENCY_CONFLICT")
        if old.get("work_id"):
            self._work(state, context, old["work_id"])
        if old.get("response") is None:
            work = self._work(state, context, old["work_id"])
            raise V2Error("RECOVERY_REQUIRED", retryable=True, next_action="resume")
        result = deepcopy(old["response"])
        self._authorize_response(state, context, result)
        if result.get("data", {}).get("kind") in {"prepared", "receipt", "deletion_receipt"}:
            result["data"]["replayed"] = True
        if not result["ok"]:
            result["error"]["budget_charged"] = False
            if "receipt" in result:
                result["receipt"]["replayed"] = True
        return result

    def _cache(self, state: dict[str, Any], context: dict[str, Any], tool: str, args: dict[str, Any],
               response: dict[str, Any], work_id: str | None = None) -> None:
        state["requests"][self._request_key(context, tool, args["request_id"])] = {
            "digest": digest(args), "response": deepcopy(response), "work_id": work_id}

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        from .host_v2_schema import validate_request, SchemaValidationError
        work = None
        charged = False
        with self.vault.lock():
            try:
                context = self._context()
                if len(canonical(args).encode("utf-8")) > 131072:
                    raise V2Error("CAPACITY_EXCEEDED")
                validate_request(tool, args, routable_only=tool == "submit_memory")
                state = self._load()
                permission = {"prepare_memory": "source.capture", "submit_memory": "memory.write", "memory_work": "work.read",
                              "resume_memory": "work.resume", "cancel_memory": "work.cancel", "forget_memory": "memory.delete"}.get(tool)
                self.auth.check(context, permission)
                if args.get("work_id"):
                    work = self._work(state, context, args["work_id"], permission or "work.read")
                # Current authorization must precede every replay lookup.
                if "request_id" in args:
                    replay = self._replay(state, context, tool, args)
                    if replay is not None:
                        return replay
                    # Never perform an effect that cannot retain its request
                    # identity. Existing requests above remain replayable at
                    # the limit; no old receipt is evicted to admit a new one.
                    if len(state["requests"]) >= 50000:
                        raise V2Error("CAPACITY_EXCEEDED", "Retained request capacity reached; no identity was discarded")
                if tool == "memory_capabilities":
                    response = success(self._capabilities(context))
                elif tool == "prepare_memory":
                    response, work = self._prepare(state, context, args, work)
                elif tool == "submit_memory":
                    self._cas(work, args)
                    if work["terminal"]:
                        raise V2Error("WORK_TERMINAL")
                    if self._summary(state, work)["recovery_required"]:
                        raise V2Error("RECOVERY_REQUIRED", retryable=True, next_action="resume")
                    if args["snapshot_id"] != work["snapshot"]["snapshot_id"]:
                        raise V2Error("SNAPSHOT_STALE", retryable=True, next_action="refresh")
                    if args["submission_revision"] != work["latest_submission_revision"] + 1:
                        raise V2Error("SUBMISSION_REVISION_CONFLICT")
                    root = state["works"][work["budget_owner_work_id"]]
                    if root["budget_used"] >= 3:
                        raise V2Error("BUDGET_EXHAUSTED")
                    root["budget_used"] += 1
                    charged = True
                    work["latest_submission_revision"] = args["submission_revision"]
                    work["work_revision"] += 1
                    work["status"], work["stop_reason"] = "submitted", None
                    state["requests"][self._request_key(context, tool, args["request_id"])] = {
                        "digest": digest(args), "response": None, "work_id": work["work_id"]}
                    self._save(state)
                    validate_request(tool, args)
                    response = self._submit(state, context, work, args)
                elif tool == "memory_work":
                    response = success(self._read_work(state, context, args, work))
                elif tool == "resume_memory":
                    self._cas(work, args)
                    self._execute(state, context, work)
                    work["work_revision"] += 1
                    response = self._result(state, context, work)
                elif tool == "cancel_memory":
                    self._cas(work, args)
                    self._cancel(state, work)
                    work["work_revision"] += 1
                    response = success(self._receipt(state, context, work))
                elif tool == "forget_memory":
                    response = success(self._forget(state, context, args))
                else:
                    raise V2Error("ACTION_NOT_SUPPORTED")
                if "request_id" in args:
                    self._cache(state, context, tool, args, response, work["work_id"] if work else None)
                if tool != "memory_capabilities":
                    self._save(state)
                self._authorize_response(state, context, response)
                if len(canonical(response).encode("utf-8")) > 32768:
                    raise V2Error("CAPACITY_EXCEEDED", "Response cannot be represented within the protocol bound")
                return response
            except Exception as exc:
                # Do not echo submitted text, credentials or filesystem paths.
                if isinstance(exc, V2Error):
                    error = exc
                elif isinstance(exc, SchemaValidationError):
                    error = V2Error("INVALID_SCHEMA", str(exc))
                elif isinstance(exc, (OSError, MemoryVersionError)):
                    error = V2Error("IO_INTERRUPTED" if isinstance(exc, OSError) else "REVISION_CONFLICT", retryable=True, next_action="resume")
                else:
                    error = V2Error("INVALID_SCHEMA")
                error.budget_charged = charged
                extra = {}
                if charged and work is not None:
                    if error.code == "IO_INTERRUPTED":
                        work["stop_reason"] = "io"
                    else:
                        work["status"], work["stop_reason"] = "partial" if any(i["receipt"]["status"] in SETTLED for i in work["items"].values()) else "pending", "validation"
                        if self._budget(state, work)["remaining"] == 0:
                            work.update(terminal=True, status="partial" if work["status"] == "partial" else "failed", stop_reason="budget_exhausted")
                    extra = {"work": self._summary(state, work), "receipt": self._receipt(state, context, work)}
                    response = failure(error, **extra)
                    try:
                        self._authorize_response(state, context, response)
                    except Exception as authorization_exc:
                        # A charged failure may still refer to a head that was
                        # written or changed before the response boundary. Do
                        # not let error wrapping bypass the same scope checks
                        # used for success and cached response replay.
                        denied = authorization_exc if isinstance(authorization_exc, V2Error) else V2Error("STATE_CORRUPT")
                        denied.budget_charged = charged
                        response = failure(denied)
                    self._cache(state, context, tool, args, response, work["work_id"])
                    self._save(state)
                    return response
                return failure(error)

    @staticmethod
    def _cas(work: dict[str, Any], args: dict[str, Any]) -> None:
        if args["expected_work_revision"] != work["work_revision"]:
            raise V2Error("WORK_REVISION_CONFLICT", retryable=True, next_action="refresh")

    def _capabilities(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "capabilities", "contract_version": PROTOCOL, "executor": "host", "profile": "full",
                "supported_actions": ["CREATE", "UPDATE", "MERGE", "COMPACT", "NO_CHANGE", "NO_MEMORY", "DEFERRED"],
                "supported_update_kinds": ["patch", "retract_memory", "restore"], "supported_body_ops": ["append", "replace", "retract"],
                "supported_todo_transitions": ["complete", "cancel", "reopen"], "body_compaction": True,
                "semantic_summary": False, "sampling_required": False, "independent_model_fallback": False,
                "credential_discovery": False, "automatic_capture": "unsupported", "automatic_processing": "unsupported",
                "access": {"principal_id": context["principal_id"], "grant_epoch": context["grant_epoch"],
                           "permissions": context["permissions"], "read_scopes": context["read_scopes"], "write_scopes": context["write_scopes"],
                           "admissible_source_trust": context["admissible_source_trust"],
                           "allow_automatic_recording": context.get("allow_automatic_recording", False),
                           "source_assurance": "trusted_caller_assertion" if context["accept_caller_asserted"] else "owner_confirmed"},
                "limits": {"request_bytes": 131072, "response_bytes": 32768, "source_messages_per_work": 16, "items_per_submission": 64,
                           "candidate_targets_per_snapshot": 12, "rows_per_page": 20, "content_characters_per_page": 2000,
                           "fragment_characters": 1000, "patches_per_item": 16, "body_characters_after_write": 8000,
                           "submission_attempts_per_work": 3, "cursor_ttl_seconds": 900},
                "acceptance": {"deterministic": "not_run", "real_agents": "not_run", "evidence_id": None}}

    def _source(self, state: dict[str, Any], context: dict[str, Any], role: str, text: str,
                *, trust: str = "caller_asserted", origin: dict[str, Any] | None = None) -> dict[str, Any]:
        if trust == "caller_asserted" and not context["accept_caller_asserted"]:
            raise V2Error("SOURCE_CONFIRMATION_REQUIRED")
        if trust not in context["admissible_source_trust"]:
            raise V2Error("SOURCE_NOT_ADMISSIBLE")
        from .redaction import redact_text
        # Same visible role/text within one authorization domain shares a budget root.
        text = redact_text(text).replace("\r\n", "\n").replace("\r", "\n")
        sid = "src_" + digest([context["authorization_domain"], role, text, origin if trust != "caller_asserted" else None])
        if sid not in state["sources"]:
            state["sources"][sid] = {"source_id": sid, "text": text, "role": role, "trust": trust,
                                     "source_time": None, "timezone": None, "revision": "sha256:" + digest([role, text]),
                                     "authorization_domain": context["authorization_domain"], "origin": origin}
        return state["sources"][sid]

    def _prepare(self, state: dict[str, Any], context: dict[str, Any], args: dict[str, Any],
                 work: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        if args["mode"] == "refresh":
            self._cas(work, args)
            if work["terminal"]:
                raise V2Error("WORK_TERMINAL")
            if self._summary(state, work)["recovery_required"]:
                raise V2Error("RECOVERY_REQUIRED", retryable=True, next_action="resume")
            work["work_revision"] += 1
            work["read_context"] = args.get("read_context", work["read_context"])
        else:
            selection, purpose = args["source"], args["purpose"]
            sources = []
            if selection["kind"] == "inline":
                sources = [self._source(state, context, m["role"], m["text"]) for m in selection["messages"]]
            elif selection["kind"] in {"bound", "maintenance"}:
                for sid in selection.get("source_ids", selection.get("decision_source_ids", [])):
                    source = state["sources"].get(sid)
                    if not source or source["authorization_domain"] != context["authorization_domain"]:
                        raise V2Error("RESOURCE_UNAVAILABLE")
                    if source["trust"] not in context["admissible_source_trust"]:
                        raise V2Error("SOURCE_CONFIRMATION_REQUIRED")
                    sources.append(source)
                if selection["kind"] == "maintenance" and purpose != "maintenance":
                    raise V2Error("INVALID_SCHEMA")
            elif selection["kind"] == "authorized_recovery":
                return self._authorized_recovery(state, context, args)
            if purpose == "automatic":
                if not context.get("allow_automatic_recording", False):
                    raise V2Error("FORBIDDEN")
                roles = [s["role"] for s in sources]
                if not roles or roles[0] != "user" or roles[-1] != "assistant":
                    raise V2Error("SOURCE_NOT_ADMISSIBLE")
            if purpose == "explicit" and not any(s["role"] == "user" for s in sources):
                raise V2Error("SOURCE_NOT_ADMISSIBLE")
            owned = {s.get("owner_work_id") for s in sources if s.get("owner_work_id")}
            selected_ids = {s["source_id"] for s in sources}
            prior = [self._work(state, context, owner) for owner in owned]
            # Exact observations return their existing receipt, including a
            # child whose complete comparison context includes parent sources.
            for old in reversed(list(state["works"].values())):
                if selected_ids and old["authorization_domain"] == context["authorization_domain"] and selected_ids == set(old["source_ids"] + old.get("context_source_ids", [])):
                    self._work(state, context, old["work_id"])
                    return success({"kind": "prepared", "work": self._summary(state, old), "snapshot": self._public_snapshot(old), "replayed": True}), old
            new_sources = [s for s in sources if not s.get("owner_work_id")]
            context_sources = [s for s in sources if s.get("owner_work_id")]
            if context_sources and (not new_sources or any(old["status"] != "completed" for old in prior)):
                raise V2Error("SOURCE_ALREADY_OWNED", "Resolve existing sources in their original work before extending context")
            if len(owned) > 1:
                raise V2Error("WORK_SET_SPLIT")
            wid = _id("work_")
            work = {"work_id": wid, "budget_owner_work_id": wid, "budget_used": 0, "related_work_id": next(iter(owned), None),
                    **{k: context[k] for k in ("principal_id", "authorization_domain", "grant_id", "grant_epoch")},
                    "purpose": purpose, "source_ids": [s["source_id"] for s in new_sources],
                    "context_source_ids": [s["source_id"] for s in context_sources], "source_order": [s["source_id"] for s in sources], "work_revision": 1,
                    "latest_submission_revision": 0, "status": "pending", "terminal": False, "stop_reason": "needs_host",
                    "items": {}, "index_status": "current", "read_context": args.get("read_context", {}),
                    "maintenance_ids": selection.get("memory_ids", []), "snapshot": {}, "removed_ranges": {},
                    "scope_revision": "sha256:" + digest(self.vault.config())}
            state["works"][wid] = work
            for source in new_sources:
                source["owner_work_id"] = wid
        work["snapshot"] = self._snapshot(state, context, work)
        if args["mode"] == "new" and work["purpose"] == "maintenance" and not work["source_ids"]:
            # A new request/scope query over the same protected maintenance
            # basis cannot replenish the semantic processing budget.
            key = digest(sorted((t["memory"]["memory_id"], t["revision"]) for t in work["snapshot"]["targets"].values()))
            for existing in list(state["works"].values()):
                if existing is not work and existing["authorization_domain"] == work["authorization_domain"] and existing.get("maintenance_basis_key") == key:
                    self._work(state, context, existing["work_id"])
                    del state["works"][work["work_id"]]
                    return success({"kind": "prepared", "work": self._summary(state, existing), "snapshot": self._public_snapshot(existing), "replayed": True}), existing
            work["maintenance_basis_key"] = key
        return success({"kind": "prepared", "work": self._summary(state, work), "snapshot": self._public_snapshot(work), "replayed": False}), work

    def _snapshot(self, state: dict[str, Any], context: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
        self.auth.check(context, "source.read")
        sources = {f"e{i}": deepcopy(state["sources"][sid]) for i, sid in enumerate(work.get("source_order", work["source_ids"]), 1)}
        for source in sources.values():
            self._check_source(context, source)
            if source["source_id"] in work.get("context_source_ids", []):
                source["covered_by_work_id"] = source["owner_work_id"]
        targets = {}
        read_context = work["read_context"]
        priorities = list(dict.fromkeys(work["maintenance_ids"] + read_context.get("priority_memory_ids", [])))
        if len(priorities) > 12:
            raise V2Error("CAPACITY_EXCEEDED")
        scan = scan_memories(self.vault, include_history=False)
        if any(issue.identity is None for issue in scan.issues):
            raise V2Error("STATE_CORRUPT")
        blocked = self._blocked_ids(state)
        allowed = set(context["read_scopes"])
        requested = set(read_context.get("scopes", context["read_scopes"]))
        if not requested.issubset(allowed):
            raise V2Error("FORBIDDEN")
        queries = read_context.get("query", [])
        from .incremental_native import read_comparison
        from types import SimpleNamespace
        native = read_comparison(self.service, context["principal_id"])
        native_records = [SimpleNamespace(memory=m, native=True) for m in native.memories.values()]
        eligible = [r for r in scan.records if set(r.memory.scopes).issubset(allowed) and requested.intersection(r.memory.scopes)
                    and r.memory.memory_id not in blocked]
        eligible += [r for r in native_records if set(r.memory.scopes).issubset(allowed) and requested.intersection(r.memory.scopes)]
        for mid in priorities:
            scan.require_identity(mid)
            if not any(r.memory.memory_id == mid for r in eligible):
                raise V2Error("RESOURCE_UNAVAILABLE")
        ranked = sorted(eligible, key=lambda r: (-sum(q.casefold() in (r.memory.title + " " + r.memory.body).casefold() for q in queries), r.memory.memory_id))
        selected = [next(r for r in eligible if r.memory.memory_id == mid) for mid in priorities]
        selected += [r for r in ranked if r not in selected and queries and any(q.casefold() in (r.memory.title + " " + r.memory.body).casefold() for q in queries)]
        for i, record in enumerate(selected[:12], 1):
            self.auth.check(context, "memory.read", scopes=record.memory.scopes)
            memory = record.memory
            ref = f"m{i}"
            targets[ref] = {"memory": memory.to_dict(), "revision": revision(memory), "native": bool(getattr(record, "native", False)),
                            "writable": not getattr(record, "native", False) and set(memory.scopes).issubset(set(context["write_scopes"])) and not memory.extra.get("merged_into"),
                            "read_fields": False, "read_fragments": [],
                            "selection_basis": "maintenance_selection" if memory.memory_id in work["maintenance_ids"] else "prior_read" if memory.memory_id in priorities else "search",
                            "fragments": [{"fragment_id": f"{ref}_f{n // 1000 + 1}", "text": memory.body[n:n + 1000], "start": n, "end": min(n + 1000, len(memory.body))} for n in range(0, len(memory.body), 1000)]}
        ensure_scan_current(self.vault, scan)
        return {"snapshot_id": _id("snap_"), "sources": sources, "targets": targets, "purpose": work["purpose"],
                "write_scopes": context["write_scopes"], "removed_ranges": work["removed_ranges"], "native_guard": native.guard}

    def _public_snapshot(self, work: dict[str, Any]) -> dict[str, Any]:
        snapshot = work["snapshot"]
        return {"snapshot_id": snapshot["snapshot_id"], "policy_revision": work["scope_revision"],
                "sources": [{"ref": ref, **{k: s[k] for k in ("source_id", "revision", "role", "trust", "source_time", "timezone")}, "characters": len(s["text"]), **({"covered_by_work_id": s["covered_by_work_id"]} if s.get("covered_by_work_id") else {})} for ref, s in snapshot["sources"].items()],
                "targets": [{"ref": ref, "memory_id": t["memory"]["memory_id"], "revision": t["revision"], "title": t["memory"]["title"] if len(t["memory"]["title"]) <= 120 else t["memory"]["title"][:119] + "…",
                             "kind": "native" if t["native"] else "merged_alias" if t["memory"].get("merged_into") else "vault",
                             "writable": bool(t["writable"]), "selection_basis": t["selection_basis"], "body_characters": len(t["memory"]["body"]),
                             "requires_full_read": work["purpose"] == "maintenance" or len(t["memory"]["title"]) > 120} for ref, t in snapshot["targets"].items()],
                "allowed_actions": ["CREATE", "UPDATE", "MERGE", "COMPACT", "NO_CHANGE", "NO_MEMORY", "DEFERRED"],
                "allowed_update_kinds": ["patch", "retract_memory", "restore"], "allowed_body_ops": ["append", "replace", "retract"],
                "allowed_todo_transitions": ["complete", "cancel", "reopen"],
                "focus_item_ids": [k for k, i in work["items"].items() if i["receipt"]["status"] not in SETTLED],
                "source_projection": {"automatic": "complete_turn", "explicit": "explicit_subset", "maintenance": "maintenance_basis", "authorized_recovery": "recovery_only"}[work["purpose"]],
                "context_complete": True, "next_action": "read_more"}

    def _submit(self, state: dict[str, Any], context: dict[str, Any], work: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        from .host_v2_proposal import compile_item
        snapshot = work["snapshot"]
        if work["scope_revision"] != "sha256:" + digest(self.vault.config()):
            raise V2Error("SNAPSHOT_STALE", retryable=True, next_action="refresh")
        ids = [i["item_id"] for i in args["items"]]
        if len(set(ids)) != len(ids):
            raise V2Error("ITEM_ID_REUSE")
        focus = [k for k, i in work["items"].items() if i["receipt"]["status"] not in SETTLED]
        if focus and set(ids) != set(focus):
            raise V2Error("ITEM_SET_CHANGED")
        coverage = {r["source"]: r["item_ids"] for r in args["coverage"]}
        basis = {r["target"]: r["item_ids"] for r in args["basis_coverage"]}
        expected_sources = {s for s, source in snapshot["sources"].items() if not source.get("covered_by_work_id")
                            and (not focus or any(s in i.get("source_refs", []) for k, i in work["items"].items() if k in focus))}
        if set(coverage) != expected_sources or any(not set(v).issubset(ids) for v in coverage.values()):
            raise V2Error("INVALID_COVERAGE")
        if work["purpose"] == "maintenance" and (set(basis) != set(snapshot["targets"]) or any(not set(v).issubset(ids) for v in basis.values())):
            raise V2Error("INVALID_COVERAGE")
        proposals = []
        for item in args["items"]:
            iid = item["item_id"]
            old = work["items"].get(iid)
            if old and old["receipt"]["status"] in SETTLED:
                raise V2Error("ITEM_ALREADY_SETTLED")
            receipt = {"item_id": iid, "action": item["action"], "operation_id": None, "group_id": None,
                       "status": "invalid", "memory_ids": [], "committed_revisions": [], "applied": False,
                       "settled": False, "inherited_from_submission": None, "error": None}
            entry = {"receipt": receipt, "proposal": deepcopy(item), "submission": args["submission_revision"], "operations": [],
                     "source_refs": [s for s, items in coverage.items() if iid in items]}
            work["items"][iid] = entry
            try:
                plan = compile_item(item, snapshot, approval_check=lambda ref, data: self.auth.validate_approval(
                    context, ref, "body_compaction", "sha256:" + digest(data), work_id=work["work_id"]) is not None)
                entry["source_refs"] = plan.get("source_refs", entry["source_refs"])
                if {s for s, members in coverage.items() if iid in members} != {ref for ref in entry["source_refs"] if not snapshot["sources"][ref].get("covered_by_work_id")}:
                    raise V2Error("INVALID_COVERAGE")
                if any(mid in {m for i in work["items"].values() if i["receipt"]["status"] in SETTLED for m in i["receipt"]["memory_ids"]}
                       for mid in [c["memory_id"] for c in plan["changes"]]):
                    raise V2Error("ITEM_ALREADY_SETTLED")
                for change in plan["changes"]:
                    for mapping in (change.get("before"), change["after"]):
                        if mapping:
                            self.auth.check(context, "memory.write", scopes=mapping["scopes"], work=work)
                            self.auth.check(context, "memory.read", scopes=mapping["scopes"], work=work)
                    if change.get("before"):
                        current, _ = self.service._revision_target_unlocked(change["memory_id"])
                        if not current or revision(current.memory) != change["expected_revision"]:
                            raise V2Error("REVISION_CONFLICT", retryable=True, next_action="refresh")
                    elif change["memory_id"] in self._blocked_ids(state):
                        raise V2Error("RECOVERY_REQUIRED")
                if item["action"] in {"MERGE", "COMPACT"}:
                    self.auth.check(context, "memory.maintain", work=work)
                if item.get("update_kind") == "retract_memory" or any(p.get("op") == "retract" for p in item.get("body_patch", [])):
                    self.auth.check(context, "memory.retract", work=work)
                frozen_entry = deepcopy(entry)
                self._freeze(frozen_entry, plan, snapshot)
                if item["action"] == "COMPACT":
                    approval_plan = plan.get("approval_plan")
                    if approval_plan is None:
                        approval_plan = {"target": item["target"], "revision": snapshot["targets"][item["target"]]["revision"],
                                         "new_body": item["new_body"], "mapping": item["mapping"]}
                    self.auth.consume_approval(context, item["approval_ref"], "body_compaction", "sha256:" + digest(approval_plan), work_id=work["work_id"], locked=True)
                entry.update(frozen_entry)
                entry["removed_ranges"] = plan.get("removed_ranges", {})
                proposals.append(entry)
            except ValueError as exc:
                if not isinstance(exc, V2Error):
                    exc = V2Error("INVALID_SCHEMA", "Memory cannot be serialized into a valid frozen group")
                exc.budget_charged = True
                receipt["error"] = exc.public()
        # Reject implicit dependencies across independently submitted Items.
        targets = [c["memory_id"] for p in proposals for c in p["operations"]]
        if len(targets) != len(set(targets)):
            for p in proposals:
                p["operations"] = []
                p["receipt"].update(status="invalid", operation_id=None, group_id=None,
                                     error=V2Error("ITEM_SET_CHANGED", budget_charged=True).public())
        self._save(state)
        self._execute(state, context, work)
        return self._result(state, context, work)

    def _freeze(self, entry: dict[str, Any], plan: dict[str, Any], snapshot: dict[str, Any]) -> None:
        receipt = entry["receipt"]
        if receipt["action"] == "DEFERRED":
            receipt["status"] = "deferred"
            return
        # Publish prepared only after every group member can be serialized and
        # validated. A later member failure must leave no executable prefix.
        operation_id = _id("op_")
        operations = []
        now = utc_now()
        for change in plan["changes"]:
            before = Memory.from_mapping(change["before"]) if change.get("before") else None
            after = Memory.from_mapping(change["after"])
            after.extra["incremental_operation_id"] = operation_id
            after.updated = now
            if before:
                after.created = before.created
            for ref in plan.get("source_refs", []):
                source = snapshot["sources"][ref]
                evidence = {"source": "host_v2", "session_id": source["source_id"], "event_key": source["source_id"],
                            "message_revision": source["revision"], "trust": source["trust"], "role": source["role"]}
                if evidence not in after.sources:
                    after.sources.append(evidence)
            operations.append({"action": "UPDATE" if before else "CREATE", "memory_id": after.memory_id,
                                         "operation_id": operation_id, "prepared_at": now,
                                         "before": before.to_markdown() if before else None, "after": after.to_markdown(),
                                         "expected_revision": revision(before).removeprefix("sha256:") if before else None,
                                         "replacement_revision": revision(after).removeprefix("sha256:"), "applied": False})
        frozen_receipt = {**receipt, "operation_id": operation_id,
                          "group_id": _id("group_") if operations else None,
                          "status": "prepared", "memory_ids": [c["memory_id"] for c in operations]}
        if not plan["changes"]:
            if receipt["action"] not in {"NO_CHANGE", "NO_MEMORY"}:
                raise V2Error("STATE_CORRUPT", "A write requires a complete frozen operation group")
            frozen_receipt["memory_ids"] = [snapshot["targets"][r]["memory"]["memory_id"] for r in plan.get("target_refs", [])]
            frozen_receipt.update(status="not_recorded" if receipt["action"] == "NO_MEMORY" else "unchanged", settled=True)
        else:
            self._validate_frozen({**entry, "receipt": frozen_receipt, "operations": operations}, snapshot)
        entry.update(operations=operations, frozen_digest=self._frozen_digest(operations))
        receipt.update(frozen_receipt)

    @staticmethod
    def _frozen_digest(operations: list[dict[str, Any]]) -> str:
        # Application bookkeeping changes during recovery; frozen payloads do not.
        fields = ("action", "memory_id", "operation_id", "prepared_at", "before", "after",
                  "expected_revision", "replacement_revision")
        return digest([{key: operation[key] for key in fields} for operation in operations])

    def _validate_frozen(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """Prove complete membership and payloads before any write or settlement.

        Old complete groups have no digest but retain their full receipt IDs and
        proposal participants. Old incomplete groups fail closed without repair
        from an inferred/recompiled plan.
        """
        try:
            receipt, operations = entry["receipt"], entry["operations"]
            ids = [op["memory_id"] for op in operations]
            if not ids or len(ids) != len(set(ids)) or ids != receipt["memory_ids"]:
                raise ValueError()
            proposal = entry["proposal"]
            action = receipt["action"]
            if proposal["action"] != action or action not in {"CREATE", "UPDATE", "MERGE", "COMPACT"}:
                raise ValueError()
            if action == "CREATE":
                if len(operations) != 1 or operations[0]["action"] != "CREATE":
                    raise ValueError()
            else:
                refs = [proposal["target"], *proposal.get("duplicates", [])]
                expected = [snapshot["targets"][ref]["memory"]["memory_id"] for ref in refs]
                if ids != expected or any(op["action"] != "UPDATE" for op in operations):
                    raise ValueError()
            if "frozen_digest" in entry and entry["frozen_digest"] != self._frozen_digest(operations):
                raise ValueError()
            for op in operations:
                after = Memory.from_markdown(op["after"])
                before = Memory.from_markdown(op["before"]) if op["before"] is not None else None
                if (op["operation_id"] != receipt["operation_id"]
                        or after.memory_id != op["memory_id"]
                        or after.extra.get("incremental_operation_id") != op["operation_id"]
                        or revision(after) != "sha256:" + op["replacement_revision"]
                        or (op["action"] == "CREATE" and (before is not None or op["expected_revision"] is not None))
                        or (op["action"] == "UPDATE" and (before is None or before.memory_id != after.memory_id
                            or revision(before) != "sha256:" + op["expected_revision"]))):
                    raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise V2Error("RECOVERY_REQUIRED", "Frozen operation group is incomplete or invalid",
                          next_action="contact_owner") from exc

    def _verify_source(self, state: dict[str, Any], work: dict[str, Any]) -> None:
        for source in work["snapshot"]["sources"].values():
            current = state["sources"].get(source["source_id"])
            if not current or current["revision"] != source["revision"]:
                raise V2Error("SOURCE_CHANGED")
        if "native_guard" in work["snapshot"]:
            from .incremental_native import guard_current
            if not guard_current(self.service, work["principal_id"], work["snapshot"]["native_guard"]):
                raise V2Error("SNAPSHOT_STALE", retryable=True, next_action="refresh")

    def _applied(self, operation: dict[str, Any]) -> bool | None:
        if operation.get("dependency_deleted"):
            return None
        try:
            record, _ = self.service._revision_target_unlocked(operation["memory_id"])
            if record is None:
                return False if operation["action"] == "CREATE" else None
            if revision(record.memory).removeprefix("sha256:") == operation["replacement_revision"] and record.memory.extra.get("incremental_operation_id") == operation["operation_id"]:
                if operation["before"]:
                    before = Memory.from_markdown(operation["before"])
                    history = self.vault.memory_path(MemoryWriter._history_id(before), "history")
                    if not history.is_file() or history.is_symlink():
                        return None
                    archived = Memory.from_markdown(history.read_text(encoding="utf-8"))
                    after = Memory.from_markdown(operation["after"])
                    expected = MemoryWriter.history_projection(before, superseded_by=after.memory_id,
                        archived_at=operation["prepared_at"],
                        invalidated_reason="retracted" if after.validity == "retracted" else None)
                    if not MemoryWriter._same_content(archived, expected, ignore_archived_at=True):
                        return None
                return True
            if operation["before"] and revision(record.memory).removeprefix("sha256:") == operation["expected_revision"]:
                return False
            return None
        except (OSError, ValueError):
            return None

    def _execute(self, state: dict[str, Any], context: dict[str, Any], work: dict[str, Any]) -> None:
        self._verify_source(state, work)
        writer = MemoryWriter(self.service)
        for entry in work["items"].values():
            r = entry["receipt"]
            if entry.get("transferred_to_work_id") or r["status"] not in {"prepared", "recovery_required"}:
                continue
            try:
                if entry.get("deleted_dependency_ids"):
                    raise V2Error("RECOVERY_REQUIRED", "A deleted dependency requires an exact new owner disposition", next_action="contact_owner")
                self._validate_frozen(entry, work["snapshot"])
                self.auth.check(context, "memory.write", work=work)
                # Guard every member before beginning a multi-file dependency group.
                for op in entry["operations"]:
                    actual = self._applied(op)
                    if actual is None:
                        raise V2Error("REVISION_CONFLICT", retryable=True, next_action="contact_owner")
                    if actual is False and op.get("applied") is True:
                        raise V2Error("RECOVERY_REQUIRED", "Previously applied content is no longer present", next_action="contact_owner")
                    self.auth.check(context, "memory.write", scopes=Memory.from_markdown(op["after"]).scopes, work=work)
                for op in entry["operations"]:
                    self.auth.check(context, "memory.write", work=work)
                    if self._applied(op) is not True:
                        writer.write_frozen_unlocked(op)
                    if self._applied(op) is not True:
                        raise V2Error("IO_INTERRUPTED", "Frozen write cannot be confirmed on disk", retryable=True, next_action="resume")
                    op["applied"] = True
                    self._save(state)
                if not all(self._applied(op) is True for op in entry["operations"]):
                    raise V2Error("RECOVERY_REQUIRED", "Complete group persistence cannot be confirmed", next_action="contact_owner")
                r.update(status="saved", applied=True, settled=True, error=None,
                         committed_revisions=[{"memory_id": o["memory_id"], "revision": "sha256:" + o["replacement_revision"]} for o in entry["operations"]])
                for key, ranges in entry.get("removed_ranges", {}).items():
                    work["removed_ranges"].setdefault(key, []).extend(ranges)
                self._save(state)
            except (OSError, MemoryVersionError, V2Error, ValueError) as exc:
                applied = [self._applied(o) for o in entry["operations"]]
                error = exc if isinstance(exc, V2Error) else V2Error("REVISION_CONFLICT" if isinstance(exc, (MemoryVersionError, ValueError)) else "IO_INTERRUPTED", retryable=True, next_action="resume")
                r.update(status="recovery_required", applied=True if applied and all(x is True for x in applied) else None if any(x is None for x in applied) else any(applied), settled=False, error=error.public())
                work["stop_reason"] = "io" if error.code == "IO_INTERRUPTED" else "conflict"
                self._save(state)
        if any(i["receipt"]["status"] == "saved" for i in work["items"].values()):
            try:
                self.service._rebuild_index_unlocked()
                work["index_status"] = "current"
            except OSError:
                work["index_status"] = "dirty"
        self._derive_status(state, work)

    def _derive_status(self, state: dict[str, Any], work: dict[str, Any]) -> None:
        states = [i["receipt"]["status"] for i in work["items"].values()]
        if states and all(s in {"saved", "unchanged", "not_recorded"} for s in states):
            work.update(status="completed", terminal=True, stop_reason=None)
        elif any(s in {"prepared", "recovery_required"} for s in states):
            work["status"] = "partial" if any(s in SETTLED for s in states) else "submitted"
        elif self._budget(state, work)["remaining"] == 0:
            work.update(status="partial" if any(s in SETTLED for s in states) else "failed", terminal=True, stop_reason="budget_exhausted")
        else:
            work.update(status="partial" if any(s in SETTLED for s in states) else "pending", stop_reason="needs_evidence" if "deferred" in states else "validation")

    def _result(self, state: dict[str, Any], context: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
        receipt = self._receipt(state, context, work)
        errors = [i["receipt"]["error"] for i in work["items"].values() if i["receipt"]["error"]]
        if errors:
            return {"protocol_version": PROTOCOL, "ok": False, "error": errors[0], "work": receipt["work"], "receipt": receipt}
        return success(receipt)

    def _cancel(self, state: dict[str, Any], work: dict[str, Any]) -> None:
        has_applied = False
        uncertain = False
        for item in work["items"].values():
            r = item["receipt"]
            if r["status"] in SETTLED:
                has_applied |= r["status"] == "saved"
                continue
            actual = [self._applied(op) for op in item["operations"]]
            if any(x is None for x in actual) or any(x is True for x in actual):
                uncertain = True
                has_applied |= any(x is True for x in actual)
                r.update(status="recovery_required", applied=True if actual and all(x is True for x in actual) else None if any(x is None for x in actual) else True, settled=False)
                continue
            r.update(status="cancelled", applied=False, settled=True, error=None)
            item["operations"] = []
            item.pop("proposal", None)
        work.update(status="partial" if has_applied else "submitted" if uncertain else "cancelled",
                    terminal=not uncertain, stop_reason="integrity_blocked" if uncertain else "user_cancelled")

    def _read_work(self, state: dict[str, Any], context: dict[str, Any], args: dict[str, Any], work: dict[str, Any] | None) -> dict[str, Any]:
        view = args["view"]
        if view == "list":
            rows = []
            for w in state["works"].values():
                try:
                    self._work(state, context, w["work_id"])
                    if not args.get("statuses") or w["status"] in args["statuses"]:
                        rows.append(self._summary(state, w))
                except V2Error:
                    continue
            rows, page = self._page(state, context, ["list", args.get("statuses"), digest(rows)], rows, cursor=args.get("cursor"), limit=args.get("limit", 20))
            return {"kind": "work_list", "works": rows, "page": page}
        if view == "summary":
            return {"kind": "work_summary", "work": self._summary(state, work)}
        if view in {"receipt", "operations"}:
            value = self._receipt(state, context, work, cursor=args.get("cursor"), limit=args.get("limit", 20))
            if view == "operations":
                return {"kind": "operations_page", "work": value["work"], "items": value["items"], "page": value["page"]}
            return value
        snapshot = work["snapshot"]
        if view in {"sources", "targets"}:
            if args["snapshot_id"] != snapshot["snapshot_id"]:
                raise V2Error("SNAPSHOT_STALE")
            key = "source_refs" if view == "sources" else "target_refs"
            refs = args[key]
            rows, headers = [], []
            objects = snapshot[view]
            for ref in refs:
                if ref not in objects:
                    raise V2Error("INVALID_EVIDENCE")
                obj = objects[ref]
                if view == "sources":
                    self.auth.check(context, "source.read", work=work)
                    self._check_source(context, obj)
                    text = obj["text"]
                    fragments = [{"fragment_id": f"{ref}_f{n // 1000 + 1}", "text": text[n:n + 1000], "start": n} for n in range(0, len(text), 1000)]
                else:
                    self.auth.check(context, "memory.read", scopes=obj["memory"]["scopes"], work=work)
                    if obj["memory"]["memory_id"] in self._blocked_ids(state):
                        raise V2Error("RECOVERY_REQUIRED")
                    if obj["native"]:
                        from .incremental_native import guard_current
                        if not guard_current(self.service, work["principal_id"], snapshot["native_guard"]):
                            raise V2Error("REVISION_CONFLICT", retryable=True, next_action="refresh")
                    else:
                        current, _ = self.service._revision_target_unlocked(obj["memory"]["memory_id"])
                        if not current or revision(current.memory) != obj["revision"]:
                            raise V2Error("REVISION_CONFLICT", retryable=True, next_action="refresh")
                    allowed = {"type", "title", "scopes", "validity", "tags", "aliases", "keywords", "status", "actionable", "assignee", "waiting_on", "due_date", "due_text", "completed_at"}
                    fields = {k: v for k, v in obj["memory"].items() if k in allowed and (v is not None or k in {"assignee", "waiting_on", "due_date", "due_text", "completed_at"})}
                    fields["actionable"] = Memory.from_mapping(obj["memory"]).actionable
                    headers.append({"target_ref": ref, "memory_id": obj["memory"]["memory_id"], "revision": obj["revision"], "fields": fields,
                                    "writable": bool(obj["writable"]), "basis_status": "trusted_native_input" if obj["native"] else "verified_revision", "legacy_extra_present": bool(set(obj["memory"]) - allowed - {"body", "memory_id", "created", "updated", "sources", "hit_count", "last_hit_at"})})
                    obj["read_fields"] = True
                    fragments = obj["fragments"]
                rows.extend({"ref": ref, "fragment_id": f["fragment_id"], "offset": f["start"], "text": f["text"],
                             "revision": obj["revision"], "editable": view == "targets" and bool(obj["writable"]), "complete": True} for f in fragments)
            selected, page = self._page(state, context, [view, work["work_id"], snapshot["snapshot_id"], refs, work["work_revision"]], rows, cursor=args.get("cursor"), limit=2)
            if view == "targets":
                for f in selected:
                    if f["fragment_id"] not in objects[f["ref"]]["read_fragments"]:
                        objects[f["ref"]]["read_fragments"].append(f["fragment_id"])
            return {"kind": view + "_page", "work_id": work["work_id"], "snapshot_id": snapshot["snapshot_id"],
                    **({"headers": headers} if view == "targets" else {}), "fragments": selected, "page": page}
        if view == "proposal":
            rows = []
            for iid in args["item_ids"]:
                item = work["items"].get(iid)
                if not item or "proposal" not in item:
                    raise V2Error("RESOURCE_UNAVAILABLE")
                for s in item.get("source_refs", []):
                    self.auth.check(context, "source.read", work=work)
                for t in work["snapshot"]["targets"].values():
                    self.auth.check(context, "memory.read", scopes=t["memory"]["scopes"], work=work)
                text = canonical(item["proposal"])
                rows.extend({"item_id": iid, "json_offset": n, "json_text": text[n:n + 1000], "payload_revision": "sha256:" + digest(item["proposal"]), "complete": n + 1000 >= len(text)} for n in range(0, len(text), 1000))
            selected, page = self._page(state, context, [view, work["work_id"], work["work_revision"], args["item_ids"]], rows, cursor=args.get("cursor"), limit=2)
            return {"kind": "proposal_page", "work_id": work["work_id"], "submission_revision": work["latest_submission_revision"], "fragments": selected, "page": page}
        raise V2Error("INVALID_SCHEMA")

    def _authorized_recovery(self, state: dict[str, Any], context: dict[str, Any], args: dict[str, Any]):
        from .host_v2_owner import prepare_authorized_recovery
        return prepare_authorized_recovery(self, state, context, args)

    def _check_source(self, context: dict[str, Any], source: dict[str, Any]) -> None:
        self.auth.check(context, "source.read")
        if source["authorization_domain"] != context["authorization_domain"]:
            raise V2Error("RESOURCE_UNAVAILABLE")
        if source["trust"] not in context["admissible_source_trust"]:
            raise V2Error("SOURCE_CONFIRMATION_REQUIRED")

    def _authorize_response(self, state: dict[str, Any], context: dict[str, Any], response: dict[str, Any]) -> None:
        """Recheck actual object scopes, including cached metadata and receipts."""
        data = response.get("data", response.get("receipt", {}))
        if data.get("kind") == "prepared":
            work = self._work(state, context, data["work"]["work_id"])
            for source in work["snapshot"]["sources"].values():
                self._check_source(context, source)
            for target in work["snapshot"]["targets"].values():
                self.auth.check(context, "memory.read", scopes=target["memory"]["scopes"], work=work)
            self._authorize_current_heads(context, {t["memory"]["memory_id"] for t in
                work["snapshot"]["targets"].values() if not t.get("native")}, work, require_present=True)
        elif data.get("kind") in {"receipt", "operations_page"}:
            work = self._work(state, context, data["work"]["work_id"])
            ids = {mid for row in data["items"] for mid in row["memory_ids"]}
            self._authorize_current_heads(context, ids, work)
            for item in work["items"].values():
                for op in item.get("operations", []):
                    if op["memory_id"] in ids:
                        self.auth.check(context, "memory.read", scopes=op.get("scopes") or Memory.from_markdown(op["after"]).scopes, work=work)
            for target in work["snapshot"]["targets"].values():
                if target["memory"]["memory_id"] in ids:
                    self.auth.check(context, "memory.read", scopes=target["memory"]["scopes"], work=work)
        elif data.get("kind") in {"deletion_plan", "deletion_receipt"}:
            plan = state["plans"].get(data["deletion_plan_id"])
            if not plan:
                raise V2Error("RESOURCE_UNAVAILABLE")
            for scopes in plan.get("scopes", []):
                self.auth.check(context, "memory.delete", scopes=scopes)

    def _forget(self, state: dict[str, Any], context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if args["mode"] == "preview":
            targets, paths, scopes = [], [], []
            scan = scan_memories(self.vault, include_history=True)
            for mid in args["memory_ids"]:
                scan.require_identity(mid)
                record = next((r for r in scan.records if r.area == "knowledge" and r.memory.memory_id == mid), None)
                if not record:
                    raise V2Error("RESOURCE_UNAVAILABLE")
                self.auth.check(context, "memory.delete", scopes=record.memory.scopes)
                scopes.append(record.memory.scopes)
                history = [r for r in scan.records if r.area == "history" and (r.memory.extra.get("active_memory_id") == mid or r.memory.memory_id == mid)]
                if history:
                    self.auth.check(context, "memory.read_history")
                for archived in history:
                    self.auth.check(context, "memory.delete", scopes=archived.memory.scopes)
                    scopes.append(archived.memory.scopes)
                groups = [i["receipt"]["group_id"] for w in state["works"].values() for i in w["items"].values() if mid in i["receipt"]["memory_ids"] and i["receipt"]["status"] not in SETTLED and i["receipt"]["group_id"]]
                targets.append({"memory_id": mid, "revision": revision(record.memory), "history_artifacts": len(history), "pending_groups": groups})
                paths.extend({"path": str(r.path.relative_to(self.vault.root)), "digest": digest(r.path.read_text(encoding="utf-8"))} for r in [record, *history])
            ensure_scan_current(self.vault, scan)
            groups = sorted({g for t in targets for g in t["pending_groups"]})
            plan = {"kind": "deletion_plan", "deletion_plan_id": _id("delete_"), "targets": targets, "needs_owner_approval": True,
                    "scope": "current_history_and_referencing_pending_payloads", "remaining_blocked_groups": groups}
            plan["plan_revision"] = "sha256:" + digest([targets, paths, groups])
            state["plans"][plan["deletion_plan_id"]] = {"public": plan, "paths": paths, "scopes": scopes, "context": {k: context[k] for k in ("principal_id", "grant_id", "grant_epoch")}}
            return plan
        plan = state["plans"].get(args["deletion_plan_id"])
        if not plan or any(plan["context"][k] != context[k] for k in plan["context"]) or plan["public"]["plan_revision"] != args["expected_plan_revision"]:
            raise V2Error("DELETE_SCOPE_CHANGED")
        if "result" in plan:
            return {**plan["result"], "replayed": True}
        ids = {t["memory_id"] for t in plan["public"]["targets"]}
        for scopes in plan["scopes"]:
            self.auth.check(context, "memory.delete", scopes=scopes)
        if not plan.get("approved"):
            # Re-enumerate every dependency/history artifact, including ones
            # created after preview whose head bytes are still unchanged.
            probe = self._forget(state, context, {"mode": "preview", "memory_ids": sorted(ids)})
            state["plans"].pop(probe["deletion_plan_id"])
            if probe["plan_revision"] != plan["public"]["plan_revision"]:
                raise V2Error("DELETE_SCOPE_CHANGED")
        for p in plan["paths"]:
            path = self.vault.root / p["path"]
            if plan.get("approved") and p["path"] in plan.get("deleted_paths", []):
                if path.exists():
                    raise V2Error("DELETE_SCOPE_CHANGED")
                continue
            if path.is_symlink() or (not path.is_file() and not plan.get("approved")) or (path.is_file() and digest(path.read_text(encoding="utf-8")) != p["digest"]):
                raise V2Error("DELETE_SCOPE_CHANGED")
        if not plan.get("approved"):
            approval = self.auth._load()["approvals"].get(args["approval_ref"])
            if isinstance(approval, dict) and approval.get("used") is True:
                # Approval and work controls are separate durable files. After
                # a failed first ledger save, only this exact consumed deletion
                # transaction can rebuild its approved checkpoint.
                self.auth.check(context)
                if (approval.get("consumption_id") != args["deletion_plan_id"]
                        or approval.get("kind") != "permanent_delete"
                        or approval.get("issued_by") != "vault_owner_control_plane"
                        or approval.get("target_work_id") is not None
                        or approval.get("plan_digest") != args["expected_plan_revision"]
                        or any(approval.get(key) != context[key] for key in
                               ("principal_id", "authorization_domain", "grant_id", "grant_epoch"))):
                    raise V2Error("DELETE_APPROVAL_REQUIRED", next_action="request_owner_approval")
            else:
                self.auth.consume_approval(context, args["approval_ref"], "permanent_delete", args["expected_plan_revision"], locked=True,
                                           consumption_id=args["deletion_plan_id"])
            plan["approved"] = True
            plan["deleted_paths"] = []
        # Persist cancellations BEFORE deleting source-of-truth files.
        for w in state["works"].values():
            for i in w["items"].values():
                target_refs = set(i.get("proposal", {}).get("duplicates", [])) | {i.get("proposal", {}).get("target")}
                referenced = {t["memory"]["memory_id"] for r, t in w["snapshot"]["targets"].items() if r in target_refs}
                if ids.intersection(set(i["receipt"]["memory_ids"]) | referenced):
                    i.pop("proposal", None)
                    if i["receipt"]["status"] not in SETTLED and i.get("operations") and len(i["operations"]) > 1:
                        i["deleted_dependency_ids"] = sorted(ids.intersection(i["receipt"]["memory_ids"]))
                    i["operations"] = [o for o in i["operations"] if o["memory_id"] not in ids]
                    if i.get("deleted_dependency_ids"):
                        for op in i["operations"]:
                            op["scopes"] = Memory.from_markdown(op["after"]).scopes
                            op.update(before=None, after="", dependency_deleted=True)
                    if i["receipt"]["status"] not in SETTLED and not i["operations"]:
                        i["receipt"].update(status="cancelled", applied=False, settled=True, error=None)
            self._derive_status(state, w)
            w["snapshot"]["targets"] = {r: t for r, t in w["snapshot"]["targets"].items() if t["memory"]["memory_id"] not in ids}
        self._save(state)
        for p in plan["paths"]:
            atomic_unlink(self.vault.root / p["path"])
            if p["path"] not in plan["deleted_paths"]:
                plan["deleted_paths"].append(p["path"])
            self._save(state)
        self.service._rebuild_index_unlocked()
        result = {"kind": "deletion_receipt", "deletion_plan_id": args["deletion_plan_id"], "status": "completed",
                  "deleted_memory_ids": sorted(ids), "remaining_memory_ids": [], "blocked_groups": [g for g in self._groups(state) if g in plan["public"]["remaining_blocked_groups"]], "replayed": False, "error": None}
        plan["result"] = result
        return result


def pending_host_memory_ids(service: Any) -> set[str]:
    """Shared read/mutation fence. Does not authenticate or recover any work."""
    host = HostMemory(service, "")
    return host._blocked_ids(host._load())
