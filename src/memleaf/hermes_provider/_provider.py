"""Hermes MemoryProvider lifecycle, retrieval and capture orchestration."""
from __future__ import annotations

try:
    from ._mcp_client import *
except (ImportError, ValueError):
    import importlib.util as _importlib_util
    import sys as _sys
    from pathlib import Path as _Path

    _name = "_memleaf_hermes_mcp_client"
    _module = _sys.modules.get(_name)
    if _module is None:
        _spec = _importlib_util.spec_from_file_location(_name, _Path(__file__).with_name("_mcp_client.py"))
        if _spec is None or _spec.loader is None:
            raise ImportError("Hermes provider module _mcp_client is unavailable")
        _module = _importlib_util.module_from_spec(_spec)
        _sys.modules[_name] = _module
        _spec.loader.exec_module(_module)
    globals().update({name: getattr(_module, name) for name in getattr(_module, "__all__", ())})

def _capture_policy_status(config: Mapping[str, Any], vault: Path) -> dict[str, Any]:
    """Read the effective policy through the public Core MCP stats result.

    Hermes installs this module as a standalone provider, so it must not import
    Core or duplicate its YAML migration rules. If the read-only stats call is
    unavailable, return an explicit unknown status instead of guessing from the
    Vault file.
    """

    unknown = {
        "tool_evidence_mode": "unknown",
        "include_attachments": "unknown",
        "body_retention": "unknown",
        "source": "mcp_unavailable",
    }
    command = _resolve_command(config)
    if command is None or not vault.is_dir():
        return unknown
    client: Optional[_MCPClient] = None
    try:
        client = _MCPClient(
            command,
            str(vault),
            config["timeout"],
            config["process_timeout"],
        )
        result = client.call_tool("stats", {})
    except Exception:
        return unknown
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    capture = result.get("capture") if isinstance(result, Mapping) else None
    if not isinstance(capture, Mapping):
        return unknown
    return dict(capture)


class MemleafMemoryProvider(MemoryProvider):
    """Use the local memleaf vault as Hermes' single external provider."""

    def __init__(self) -> None:
        self._hermes_home = ""
        self._session_id = ""
        self._client: Optional[_MCPClient] = None
        self._write_enabled = True
        self._auto_process = True
        self._sync_lock = threading.RLock()
        self._pending_turn_numbers: "OrderedDict[str, Deque[Tuple[str, int]]]" = OrderedDict()
        self._pending_turn_count = 0
        self._turn_ids_by_pair: "OrderedDict[Tuple[str, str], str]" = OrderedDict()
        self._last_recall: Optional[RecallStatus] = None
        self._last_call_error: Optional[dict[str, str]] = None
        # The provider runs capture/process in a background worker. Keep the
        # last automatic-sync outcome so the next user turn can distinguish a
        # pending failed process from a successful extraction. This is control
        # state only; it never becomes memory content.
        self._last_auto_process_failure: Optional[dict[str, str]] = None
        self._last_auto_process_deferred: Optional[dict[str, int]] = None
        self._last_auto_process_external_evidence: Optional[dict[str, Any]] = None
        # Hermes exposes no final-answer blocking hook.  Keep this adapter's
        # retrieval state only when it was initialized from an explicit local
        # provider config; tests and disabled/manual instances must not request
        # managed MCP tokens merely because ``on_turn_start`` is called.
        self._gate_enabled = False
        # Hermes cannot enforce a final-answer gate and must not import Core
        # from its independent plugin environment.  The token returned by
        # scope_catalog is therefore retained only as bounded provider state
        # and echoed back in the visible MCP instructions for this turn.
        self._retrieval_ids_by_turn: "OrderedDict[Tuple[str, int], str]" = OrderedDict()
        self._gate_turn_ids: "OrderedDict[Tuple[str, int], str]" = OrderedDict()
        self._active_turn_numbers: "OrderedDict[str, int]" = OrderedDict()
        self._active_retrieval_ids: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._observed_tool_call_keys: "OrderedDict[str, None]" = OrderedDict()
        self._version_warning_emitted = False
        # Hermes keeps one provider instance alive while compression rotates
        # the physical session id.  Keep only a bounded alias chain so a
        # callback already queued for the parent can still resolve to the
        # live continuation.
        self._session_aliases: "OrderedDict[str, str]" = OrderedDict()
        # A compression child may be visible before the control RPC can be
        # persisted.  Keep that child captureable, but never process it until
        # its parent link is confirmed.
        self._pending_lineage: Deque[dict[str, Any]] = deque()
        # A physical session can be captured while lineage is pending.  Keep
        # those sessions separate from aliases so their inbox is processed
        # under the original session id after the chain is restored.
        self._deferred_process_sessions: "OrderedDict[str, None]" = OrderedDict()
        # Automatic process requests are detached from the MCP client. Keep a
        # bounded session -> job mapping so a later turn can poll the prior
        # result and request a rerun on the same job while it is active.
        self._process_jobs_by_session: "OrderedDict[str, str]" = OrderedDict()
        self._last_retrieval_observation = "unknown"
        self._last_retrieval_audit = "SEARCH_UNKNOWN"

    @property
    def name(self) -> str:
        return "memleaf"

    def _config(self) -> dict[str, Any]:
        return _load_config(self._hermes_home or _default_hermes_home())

    def _gate_id_for_turn(self, session_id: str, turn_number: Any) -> Optional[str]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        if isinstance(turn_number, bool) or not isinstance(turn_number, int) or turn_number <= 0:
            return None
        return self._retrieval_ids_by_turn.get((session_id, turn_number))

    def _current_gate_id(self, session_id: str) -> Optional[str]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        return self._active_retrieval_ids.get(session_id)

    def _current_turn_number(self, session_id: str) -> Optional[int]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        return self._active_turn_numbers.get(session_id)

    def _gate_turn_id(self, session_id: str, turn_number: Any) -> Optional[str]:
        if not self._gate_enabled:
            return None
        session_id = self._canonical_session_id(session_id)
        if isinstance(turn_number, bool) or not isinstance(turn_number, int) or turn_number <= 0:
            return None
        return self._gate_turn_ids.get((session_id, turn_number))

    def _canonical_session_id(self, session_id: Any) -> str:
        """Resolve a compression continuation to its current session id."""

        candidate = _safe_component(str(session_id or ""), "hermes-session")
        with self._sync_lock:
            seen: set[str] = set()
            while candidate not in seen:
                seen.add(candidate)
                successor = self._session_aliases.get(candidate)
                if not isinstance(successor, str) or not successor:
                    break
                candidate = successor
            return candidate

    def _migrate_session_state(self, old_session_id: str, new_session_id: str) -> None:
        """Move in-flight turn state across a non-reset Hermes rotation."""

        if not old_session_id or not new_session_id or old_session_id == new_session_id:
            return
        self._session_aliases[old_session_id] = new_session_id
        self._session_aliases.move_to_end(old_session_id)
        while len(self._session_aliases) > _MAX_SESSION_ALIASES:
            self._session_aliases.popitem(last=False)

        for fingerprint, queue in list(self._pending_turn_numbers.items()):
            migrated: deque[Tuple[str, int]] = deque()
            seen: set[Tuple[str, int]] = set()
            for queued_session, queued_number in queue:
                target_session = new_session_id if queued_session == old_session_id else queued_session
                item = (target_session, queued_number)
                if item not in seen:
                    migrated.append(item)
                    seen.add(item)
            if migrated:
                self._pending_turn_numbers[fingerprint] = migrated
            else:
                del self._pending_turn_numbers[fingerprint]
        self._pending_turn_count = sum(len(queue) for queue in self._pending_turn_numbers.values())

        for pair_key in list(self._turn_ids_by_pair):
            if pair_key[0] != old_session_id:
                continue
            target_key = (new_session_id, pair_key[1])
            value = self._turn_ids_by_pair.pop(pair_key)
            self._turn_ids_by_pair.setdefault(target_key, value)
            self._turn_ids_by_pair.move_to_end(target_key)

        for state_map in (self._retrieval_ids_by_turn, self._gate_turn_ids):
            for key in list(state_map):
                if key[0] != old_session_id:
                    continue
                target_key = (new_session_id, key[1])
                value = state_map.pop(key)
                state_map.setdefault(target_key, value)
                state_map.move_to_end(target_key)

        turn_number = self._active_turn_numbers.pop(old_session_id, None)
        if turn_number is not None:
            self._active_turn_numbers.setdefault(new_session_id, turn_number)
        retrieval_id = self._active_retrieval_ids.pop(old_session_id, None)
        if retrieval_id is not None:
            if self._active_retrieval_ids.get(new_session_id) is None:
                self._active_retrieval_ids[new_session_id] = retrieval_id
        if new_session_id in self._active_turn_numbers:
            self._active_turn_numbers.move_to_end(new_session_id)
        if new_session_id in self._active_retrieval_ids:
            self._active_retrieval_ids.move_to_end(new_session_id)

        for attribute in (
            "_last_auto_process_failure",
            "_last_auto_process_deferred",
            "_last_auto_process_external_evidence",
        ):
            value = getattr(self, attribute)
            if isinstance(value, Mapping) and value.get("session_id") == old_session_id:
                updated = dict(value)
                updated["session_id"] = new_session_id
                setattr(self, attribute, updated)

        job_id = self._process_jobs_by_session.pop(old_session_id, None)
        if isinstance(job_id, str) and job_id:
            self._process_jobs_by_session[new_session_id] = job_id
        if new_session_id in self._process_jobs_by_session:
            self._process_jobs_by_session.move_to_end(new_session_id)
        while len(self._process_jobs_by_session) > _MAX_SESSION_ALIASES:
            self._process_jobs_by_session.popitem(last=False)

    def _drop_session_aliases(self, *session_ids: str) -> None:
        targets = {value for value in session_ids if value}
        changed = True
        while changed:
            changed = False
            for key, value in self._session_aliases.items():
                if key in targets or value in targets:
                    before = len(targets)
                    targets.update((key, value))
                    changed = len(targets) != before
        for key, value in list(self._session_aliases.items()):
            if key in targets or value in targets:
                del self._session_aliases[key]

    @staticmethod
    def _lineage_result_valid(result: Any, arguments: Mapping[str, Any]) -> bool:
        if not isinstance(result, Mapping):
            return False
        if arguments.get("reset") is True:
            return (
                result.get("session_id") == arguments.get("session_id")
                and isinstance(result.get("cleared"), bool)
            )
        return (
            result.get("linked") is True
            and result.get("session_id") == arguments.get("session_id")
            and result.get("parent_session_id") == arguments.get("parent_session_id")
        )

    def _remember_pending_lineage(self, arguments: Mapping[str, Any], attempts: int) -> bool:
        pending = dict(arguments)
        pending["attempts"] = attempts
        with self._sync_lock:
            if len(self._pending_lineage) >= _MAX_SESSION_ALIASES:
                logger.warning(
                    "memleaf provider session lineage queue is full; automatic process remains deferred",
                )
                return False
            self._pending_lineage.append(pending)
            return True

    def _defer_process_session(self, session_id: str) -> None:
        if not session_id:
            return
        with self._sync_lock:
            if session_id in self._deferred_process_sessions:
                return
            if len(self._deferred_process_sessions) >= _MAX_DEFERRED_PROCESS_SESSIONS:
                logger.warning(
                    "memleaf provider deferred process queue is full; session remains retryable in inbox",
                )
                return
            self._deferred_process_sessions[session_id] = None

    def _record_auto_process_failure(self, session_id: str) -> None:
        with self._sync_lock:
            error = self._last_call_error or {}
            self._last_auto_process_failure = {
                "session_id": session_id,
                "error_code": str(error.get("error_code") or "model_failed"),
                "error_stage": str(error.get("error_stage") or "process"),
            }
            self._last_auto_process_deferred = None
            self._last_auto_process_external_evidence = None

    def _record_auto_process_failure_fields(
        self, session_id: str, *, error_code: str = "model_failed", error_stage: str = "process"
    ) -> None:
        with self._sync_lock:
            self._last_auto_process_failure = {
                "session_id": session_id,
                "error_code": str(error_code or "model_failed")[:120],
                "error_stage": str(error_stage or "process")[:120],
            }
            self._last_auto_process_deferred = None
            self._last_auto_process_external_evidence = None

    @staticmethod
    def _safe_external_evidence_status(value: Any) -> Optional[dict[str, Any]]:
        """Project Core's bounded capture status into provider control state."""

        if not isinstance(value, Mapping):
            return None
        detail = value.get("external_evidence")
        detail = dict(detail) if isinstance(detail, Mapping) else {}
        status = detail.get("status") or value.get("external_evidence_status")
        if not isinstance(status, str) or status not in {
            "available",
            "partial",
            "metadata_only",
            "disabled",
            "unavailable",
            "not_provided",
        }:
            return None
        projected: dict[str, Any] = {"status": status}
        for field in (
            "external_record_count",
            "retained_body_count",
            "retained_body_bytes",
            "metadata_only_record_count",
            "incomplete_record_count",
            "unusable_record_count",
        ):
            count = detail.get(field)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                projected[field] = count
        policy = detail.get("capture_policy")
        if isinstance(policy, Mapping):
            mode = policy.get("tool_evidence_mode")
            if isinstance(mode, str) and mode in {"bounded", "metadata", "off"}:
                projected["tool_evidence_mode"] = mode
        return projected

    def _record_auto_process_external_evidence(self, session_id: str, value: Any) -> None:
        projected = self._safe_external_evidence_status(value)
        if projected is not None:
            projected["session_id"] = session_id
        with self._sync_lock:
            self._last_auto_process_external_evidence = projected

    def _remember_process_job(self, session_id: str, job_id: str) -> None:
        with self._sync_lock:
            self._process_jobs_by_session[session_id] = job_id
            self._process_jobs_by_session.move_to_end(session_id)
            while len(self._process_jobs_by_session) > _MAX_SESSION_ALIASES:
                self._process_jobs_by_session.popitem(last=False)

    def _consume_process_job(self, session_id: str, job: Mapping[str, Any], *, turn_id: str = "") -> Any:
        status = job.get("status")
        with self._sync_lock:
            self._process_jobs_by_session.pop(session_id, None)
        if status == "failed":
            error = job.get("error")
            if isinstance(error, Mapping):
                code = str(error.get("code") or error.get("type") or "model_failed")
                stage = str(error.get("stage") or "process")
            else:
                code, stage = "model_failed", "process"
            self._defer_process_session(session_id)
            self._record_auto_process_failure_fields(session_id, error_code=code, error_stage=stage)
            return _CALL_FAILED
        result = job.get("result")
        result = dict(result) if isinstance(result, Mapping) else {}
        result["completed"] = True
        with self._sync_lock:
            if isinstance(self._last_auto_process_failure, Mapping) and self._last_auto_process_failure.get("session_id") == session_id:
                self._last_auto_process_failure = None
        if status == "deferred":
            self._defer_process_session(session_id)
        else:
            with self._sync_lock:
                self._deferred_process_sessions.pop(session_id, None)
        deferred = self._process_deferred_counts(result)
        unresolved = result.get("unresolved_evidence_count", 0)
        unresolved = unresolved if type(unresolved) is int and unresolved >= 0 else 0
        if status == "deferred" or unresolved > 0 or (deferred is not None and any(deferred)):
            with self._sync_lock:
                self._last_auto_process_deferred = {
                    "session_id": session_id,
                    "deferred_candidates": deferred[0] if deferred else 0,
                    "deferred_inbox_turns": deferred[1] if deferred else 0,
                    "unresolved_evidence_count": unresolved,
                }
        else:
            with self._sync_lock:
                prior_deferred_session = (
                    self._last_auto_process_deferred.get("session_id")
                    if isinstance(self._last_auto_process_deferred, Mapping)
                    else None
                )
                if prior_deferred_session == session_id:
                    self._last_auto_process_deferred = None
        self._record_auto_process_external_evidence(session_id, result)
        return result

    def _enqueue_process_job(self, session_id: str, *, turn_id: str = "") -> Any:
        processed = self._call(
            "process",
            {"source": "hermes", "session_id": session_id, "background": True},
            stage="process",
            session_id=session_id,
            turn_id=turn_id,
        )
        if processed is _CALL_FAILED:
            self._defer_process_session(session_id)
            self._record_auto_process_failure(session_id)
            logger.warning(
                "memleaf provider auto-process failed for hermes/%s; queue retained",
                session_id,
            )
            return _CALL_FAILED
        if isinstance(processed, Mapping):
            job_id = processed.get("job_id")
            if isinstance(job_id, str) and job_id:
                self._remember_process_job(session_id, job_id)
                self._defer_process_session(session_id)
                return _PROCESS_PENDING
            if processed.get("completed") is False:
                self._defer_process_session(session_id)
                return _PROCESS_PENDING
        with self._sync_lock:
            self._deferred_process_sessions.pop(session_id, None)
        self._record_auto_process_external_evidence(session_id, processed)
        return processed

    def _process_session(self, session_id: str, *, turn_id: str = "", request_rerun: bool = False) -> Any:
        """Poll a detached job, then enqueue at most one non-blocking retry."""

        with self._sync_lock:
            job_id = self._process_jobs_by_session.get(session_id)
        if isinstance(job_id, str) and job_id:
            observed = self._call(
                "process_status",
                {"job_id": job_id},
                stage="process_status",
                session_id=session_id,
                turn_id=turn_id,
            )
            if observed is _CALL_FAILED or not isinstance(observed, Mapping):
                if request_rerun:
                    # A status read may fail after the host has already
                    # captured a new turn. Re-submit the idempotent enqueue
                    # so the core records the rerun request on the same job.
                    return self._enqueue_process_job(session_id, turn_id=turn_id)
                self._defer_process_session(session_id)
                return _PROCESS_PENDING
            status = observed.get("status")
            if status in {"succeeded", "deferred", "failed"}:
                consumed = self._consume_process_job(session_id, observed, turn_id=turn_id)
                if request_rerun:
                    queued = self._enqueue_process_job(session_id, turn_id=turn_id)
                    if queued is _CALL_FAILED:
                        return queued
                    if queued is _PROCESS_PENDING:
                        return queued
                return consumed
            # A current visible turn should request a rerun on the same job;
            # an older deferred session only polls and leaves it alone.
            if request_rerun:
                return self._enqueue_process_job(session_id, turn_id=turn_id)
            if observed.get("recovery_pending") is True:
                return self._enqueue_process_job(session_id, turn_id=turn_id)
            self._defer_process_session(session_id)
            return _PROCESS_PENDING
        return self._enqueue_process_job(session_id, turn_id=turn_id)

    def _process_deferred_sessions(self, current_session: str, turn_id: str) -> None:
        """Process deferred physical sessions before the current continuation."""

        with self._sync_lock:
            queued_sessions = [
                session_id
                for session_id in self._deferred_process_sessions
                if session_id != current_session
            ]
        for physical_session in queued_sessions:
            self._process_session(physical_session)

        processed = self._process_session(current_session, turn_id=turn_id, request_rerun=True)
        if processed is _CALL_FAILED or processed is _PROCESS_PENDING:
            return
        with self._sync_lock:
            deferred = self._process_deferred_counts(processed)
            unresolved = processed.get("unresolved_evidence_count", 0) if isinstance(processed, Mapping) else 0
            unresolved = unresolved if type(unresolved) is int and unresolved >= 0 else 0
            if isinstance(self._last_auto_process_failure, Mapping) and self._last_auto_process_failure.get("session_id") == current_session:
                self._last_auto_process_failure = None
            deferred_session = self._last_auto_process_deferred.get("session_id") if isinstance(self._last_auto_process_deferred, Mapping) else None
            if deferred_session is not None and deferred_session != current_session:
                pass
            elif not unresolved and (deferred is None or (deferred[0] <= 0 and deferred[1] <= 0)):
                self._last_auto_process_deferred = None
            else:
                self._last_auto_process_deferred = {
                    "session_id": current_session,
                    "deferred_candidates": deferred[0] if deferred else 0,
                    "deferred_inbox_turns": deferred[1] if deferred else 0,
                    "unresolved_evidence_count": unresolved,
                }
        if deferred is not None and (deferred[0] > 0 or deferred[1] > 0):
            logger.info(
                "memleaf provider auto-process deferred scope work for hermes/%s: candidates=%d inbox_turns=%d",
                current_session,
                deferred[0],
                deferred[1],
            )

    def _retry_pending_lineage(self, session_id: str) -> bool:
        while True:
            with self._sync_lock:
                pending_queue = self._pending_lineage
                if not pending_queue:
                    return True
                # The tail is always the current compression continuation.
                # A non-continuous session switch clears the queue before this
                # point.  If it does not match, fail closed rather than letting
                # any unresolved link reach automatic process.
                if pending_queue[-1].get("session_id") != session_id:
                    logger.warning(
                        "memleaf provider session lineage belongs to another hermes session; automatic process deferred",
                    )
                    return False
                pending = dict(pending_queue[0])
                attempts = pending.get("attempts", 0)
                if isinstance(attempts, bool) or not isinstance(attempts, int):
                    attempts = 0
                if attempts > _MAX_LINEAGE_RETRIES:
                    logger.warning(
                        "memleaf provider session lineage retries exhausted for hermes/%s; automatic process deferred",
                        pending.get("session_id", session_id),
                    )
                    return False
                arguments = {
                    key: value
                    for key, value in pending.items()
                    if key in {"source", "session_id", "parent_session_id", "reset"}
                }
                next_attempt = attempts + 1

            result = self._call(
                "session_lineage",
                arguments,
                stage="session_lineage_retry",
                session_id=str(arguments.get("session_id") or session_id),
            )
            succeeded = result is not _CALL_FAILED and self._lineage_result_valid(result, arguments)
            with self._sync_lock:
                current = self._pending_lineage
                if not current or not all(
                    current[0].get(key) == value for key, value in arguments.items()
                ) or current[0].get("attempts") != attempts:
                    return False
                if succeeded:
                    current.popleft()
                    if not current:
                        return True
                    continue
                current[0]["attempts"] = next_attempt
            logger.warning(
                "memleaf provider session lineage pending for hermes/%s; automatic process deferred",
                arguments.get("session_id", session_id),
            )
            return False

    def is_available(self) -> bool:
        config = _load_config(_default_hermes_home())
        return _resolve_command(config) is not None and _resolve_vault(config).is_dir()

    def unavailable_reason(self) -> str:
        config = _load_config(_default_hermes_home())
        if _resolve_command(config) is None:
            return "Install memleaf and expose the memleaf-mcp executable."
        if not _resolve_vault(config).is_dir():
            return f"memleaf vault does not exist: {_resolve_vault(config)}"
        return ""

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "vault",
                "description": "memleaf vault directory",
                "default": _DEFAULT_VAULT,
            },
            {
                "key": "auto_process",
                "description": "Automatically process each complete visible Hermes turn",
                "default": True,
                "choices": [True, False],
            },
            {
                "key": "timeout",
                "description": "Short MCP timeout for capture, stats, and context requests",
                "default": _DEFAULT_TIMEOUT,
            },
            {
                "key": "process_timeout",
                "description": "MCP timeout for the model-backed process request",
                "default": _DEFAULT_PROCESS_TIMEOUT,
            },
        ]

    def save_config(self, values: Mapping[str, Any], hermes_home: str) -> None:
        path = _config_path(hermes_home)
        existing: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            pass
        vault = str(values.get("vault") or existing.get("vault") or _DEFAULT_VAULT).strip()
        auto_process = _as_bool(values.get("auto_process", existing.get("auto_process")), True)
        timeout = _bounded_timeout(
            values.get("timeout", existing.get("timeout", _DEFAULT_TIMEOUT)),
            _DEFAULT_TIMEOUT,
            _MAX_TIMEOUT,
        )
        process_timeout = _bounded_timeout(
            values.get("process_timeout", existing.get("process_timeout", _DEFAULT_PROCESS_TIMEOUT)),
            _DEFAULT_PROCESS_TIMEOUT,
            _MAX_PROCESS_TIMEOUT,
        )
        _write_json(
            path,
            {
                **existing,
                "vault": vault or _DEFAULT_VAULT,
                "auto_process": auto_process,
                "timeout": timeout,
                "process_timeout": process_timeout,
            },
        )

    def get_status_config(self, provider_config: Mapping[str, Any]) -> dict[str, Any]:
        config = _load_config(self._hermes_home or _default_hermes_home())
        vault = _resolve_vault(config)
        return {
            "vault": str(vault),
            "mcp_command": _resolve_command(config) or str(config.get("command", _DEFAULT_COMMAND)),
            "auto_process": config["auto_process"],
            "timeout": config["timeout"],
            "process_timeout": config["process_timeout"],
            "capture": _capture_policy_status(config, vault),
        }

    @staticmethod
    def _log_stage(
        stage: str,
        *,
        started_at: float,
        status: str,
        session_id: str = "",
        turn_id: str = "",
        error_type: str = "none",
        error_code: str = "",
        error_stage: str = "",
        validation_reason: str = "",
        validation_detail: str = "",
        attempt_count: Optional[int] = None,
    ) -> None:
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count not in (1, 2, 3, 4):
            attempt_count = None
        logger.info(
            "memleaf stage=%s duration_ms=%d status=%s error_type=%s error_code=%s error_stage=%s validation_reason=%s validation_detail=%s attempt_count=%s source=hermes session=%s turn=%s",
            stage,
            max(0, int((time.monotonic() - started_at) * 1000)),
            status,
            error_type,
            error_code or "none",
            error_stage or "none",
            validation_reason or "none",
            validation_detail or "none",
            attempt_count if attempt_count is not None else "none",
            _safe_component(session_id, "none") if session_id else "none",
            _safe_component(turn_id, "none") if turn_id else "none",
        )

    def _check_version_sync(self) -> None:
        """Warn when the copied provider and MCP core came from different releases."""

        client = self._client
        if client is None:
            return
        # Test doubles and older host integrations may not expose the MCP
        # initialize metadata.  The real client always does, so only suppress
        # this check for a double that has no version attribute at all.
        client_type = _MCPClient
        is_real_client = isinstance(client_type, type) and isinstance(client, client_type)
        client_fields = getattr(client, "__dict__", {})
        if not is_real_client and not (
            isinstance(client_fields, Mapping) and "server_version" in client_fields
        ):
            return
        provider_version = _provider_manifest_version()
        core_version = _version_value(getattr(client, "server_version", None))
        if provider_version is None or core_version is None:
            if not self._version_warning_emitted:
                logger.warning(
                    "memleaf provider/core version check unavailable "
                    "(provider=%s core=%s); do not assume they are synchronized. "
                    "Run: %s",
                    provider_version or "unknown",
                    core_version or "unknown",
                    _UPDATE_COMMAND,
                )
                self._version_warning_emitted = True
            return
        if provider_version == core_version:
            return
        if not self._version_warning_emitted:
            logger.warning(
                "memleaf provider/core version mismatch (provider=%s core=%s). "
                "Run: %s",
                provider_version,
                core_version,
                _UPDATE_COMMAND,
            )
            self._version_warning_emitted = True

    def _queue_turn_number(self, turn_number: Any, message: Any) -> None:
        if isinstance(turn_number, bool) or not isinstance(turn_number, int) or turn_number <= 0:
            return
        visible_user = _visible_message_text(message)
        if not visible_user.strip():
            return
        fingerprint = _visible_fingerprint(visible_user)
        session_id = self._canonical_session_id(self._session_id)
        with self._sync_lock:
            queue = self._pending_turn_numbers.get(fingerprint)
            if queue is None:
                queue = deque()
                self._pending_turn_numbers[fingerprint] = queue
            queue.append((session_id, turn_number))
            self._pending_turn_numbers.move_to_end(fingerprint)
            self._pending_turn_count += 1
            while self._pending_turn_count > _MAX_PENDING_TURN_NUMBERS:
                _, removed = self._pending_turn_numbers.popitem(last=False)
                self._pending_turn_count -= len(removed)

    def _take_turn_number(self, session_id: str, user_content: str) -> Optional[int]:
        session_id = self._canonical_session_id(session_id)
        fingerprint = _visible_fingerprint(user_content)
        with self._sync_lock:
            queue = self._pending_turn_numbers.get(fingerprint)
            if not queue:
                return None
            selected_index = None
            selected_number = None
            for index, (queued_session, queued_number) in enumerate(queue):
                if queued_session == session_id:
                    selected_index = index
                    selected_number = queued_number
                    break
            if selected_index is None:
                return None
            values = list(queue)
            del values[selected_index]
            self._pending_turn_count -= 1
            if values:
                self._pending_turn_numbers[fingerprint] = deque(values)
                self._pending_turn_numbers.move_to_end(fingerprint)
            else:
                del self._pending_turn_numbers[fingerprint]
            return selected_number

    def _discard_turn_number(self, session_id: str, user_content: str, turn_number: int) -> None:
        session_id = self._canonical_session_id(session_id)
        fingerprint = _visible_fingerprint(user_content)
        with self._sync_lock:
            queue = self._pending_turn_numbers.get(fingerprint)
            if not queue:
                return
            values = list(queue)
            for index, (queued_session, queued_number) in enumerate(values):
                if queued_session == session_id and queued_number == turn_number:
                    del values[index]
                    self._pending_turn_count -= 1
                    break
            else:
                return
            if values:
                self._pending_turn_numbers[fingerprint] = deque(values)
                self._pending_turn_numbers.move_to_end(fingerprint)
            else:
                del self._pending_turn_numbers[fingerprint]

    def _clear_session_turn_state(self, session_id: str) -> None:
        if not session_id:
            return
        for fingerprint in list(self._pending_turn_numbers):
            queue = self._pending_turn_numbers[fingerprint]
            values = [item for item in queue if item[0] != session_id]
            self._pending_turn_count -= len(queue) - len(values)
            if values:
                self._pending_turn_numbers[fingerprint] = deque(values)
            else:
                del self._pending_turn_numbers[fingerprint]
        for pair_key in list(self._turn_ids_by_pair):
            if pair_key[0] == session_id:
                del self._turn_ids_by_pair[pair_key]

    def _resolve_turn_id(
        self,
        session_id: str,
        turn_number: Optional[int],
        user_content: str,
        assistant_content: str,
    ) -> str:
        pair_digest = sha256(f"{user_content}\x00{assistant_content}".encode("utf-8")).hexdigest()[:16]
        pair_key = (session_id, pair_digest)
        existing = self._turn_ids_by_pair.get(pair_key)
        if existing is not None and turn_number is None:
            self._turn_ids_by_pair.move_to_end(pair_key)
            return existing
        resolved = _turn_id(turn_number, user_content, assistant_content)
        if existing is not None and existing == resolved:
            self._turn_ids_by_pair.move_to_end(pair_key)
            return existing
        self._turn_ids_by_pair[pair_key] = resolved
        self._turn_ids_by_pair.move_to_end(pair_key)
        while len(self._turn_ids_by_pair) > _MAX_PENDING_TURN_NUMBERS:
            self._turn_ids_by_pair.popitem(last=False)
        return resolved

    def on_turn_start(self, turn_number: Any, message: Any = None, **kwargs: Any) -> None:
        """Remember only a bounded user-text fingerprint for later ``sync_turn``."""

        del kwargs
        if not self._write_enabled:
            return
        if self._gate_enabled:
            if isinstance(turn_number, int) and not isinstance(turn_number, bool) and turn_number > 0:
                # The token is created by the MCP server during this turn's
                # scope_catalog call.  Clearing the active value here prevents
                # a skipped prefetch from reusing the previous turn's token.
                self._active_turn_numbers[self._session_id] = turn_number
                self._active_turn_numbers.move_to_end(self._session_id)
                self._active_retrieval_ids[self._session_id] = None
                self._active_retrieval_ids.move_to_end(self._session_id)
                visible_user = _visible_message_text(message)
                self._gate_turn_ids[(self._session_id, turn_number)] = (
                    f"turn-{turn_number:06d}-{_visible_fingerprint(visible_user)}"
                )
                self._gate_turn_ids.move_to_end((self._session_id, turn_number))
                while len(self._active_turn_numbers) > _MAX_PENDING_TURN_NUMBERS:
                    self._active_turn_numbers.popitem(last=False)
                while len(self._active_retrieval_ids) > _MAX_PENDING_TURN_NUMBERS:
                    self._active_retrieval_ids.popitem(last=False)
                while len(self._gate_turn_ids) > _MAX_PENDING_TURN_NUMBERS:
                    self._gate_turn_ids.popitem(last=False)
                self._last_retrieval_observation = "not_observed"
        self._queue_turn_number(turn_number, message)

    def on_session_switch(
        self,
        new_session_id: str,
        reset: Any = False,
        rewound: Any = False,
        **kwargs: Any,
    ) -> None:
        """Update session identity and discard state only for reset/rewind."""

        reason = kwargs.get("reason")
        parent_session_id = kwargs.get("parent_session_id")
        old_session_id = self._session_id
        next_session_id = _safe_component(str(new_session_id or ""), "hermes-session")
        parent_session = (
            _safe_component(str(parent_session_id), "hermes-session")
            if parent_session_id
            else ""
        )
        lineage_args: Optional[dict[str, Any]] = None
        with self._sync_lock:
            continuous = (
                not _as_bool(reset, False)
                and not _as_bool(rewound, False)
                and next_session_id != old_session_id
                and (
                    parent_session == old_session_id
                    or (reason == "compression" and not parent_session)
                )
            )
            if continuous:
                self._migrate_session_state(parent_session or old_session_id, next_session_id)
            self._session_id = next_session_id
            if not continuous:
                self._pending_lineage.clear()
                self._deferred_process_sessions.clear()
                self._observed_tool_call_keys.clear()
            if _as_bool(reset, False) or _as_bool(rewound, False):
                self._clear_session_turn_state(old_session_id or next_session_id)
                for key in list(self._retrieval_ids_by_turn):
                    if key[0] == (old_session_id or next_session_id):
                        del self._retrieval_ids_by_turn[key]
                for key in list(self._gate_turn_ids):
                    if key[0] == (old_session_id or next_session_id):
                        del self._gate_turn_ids[key]
                self._active_turn_numbers.pop(old_session_id or next_session_id, None)
                self._active_retrieval_ids.pop(old_session_id or next_session_id, None)
                self._drop_session_aliases(old_session_id, next_session_id)
            if self._gate_enabled:
                if not continuous:
                    self._active_turn_numbers.pop(next_session_id, None)
                    self._active_retrieval_ids.pop(next_session_id, None)
                    for key in list(self._gate_turn_ids):
                        if key[0] == next_session_id:
                            del self._gate_turn_ids[key]
            if self._gate_enabled and continuous and (parent_session or old_session_id):
                lineage_args = {
                    "source": "hermes",
                    "session_id": next_session_id,
                    "parent_session_id": parent_session or old_session_id,
                }
            elif self._gate_enabled and (_as_bool(reset, False) or _as_bool(rewound, False)):
                lineage_args = {
                    "source": "hermes",
                    "session_id": next_session_id,
                    "reset": True,
                }
        if lineage_args is not None:
            with self._sync_lock:
                has_pending = bool(self._pending_lineage)
            if has_pending:
                # Preserve the ordered parent link.  A grandchild must not
                # overwrite a failed child link or bypass it on the next sync.
                queued = self._remember_pending_lineage(lineage_args, 0)
                logger.warning(
                    "memleaf provider session lineage %s for hermes/%s; parent link is pending",
                    "queued" if queued else "not queued because the queue is full",
                    next_session_id,
                )
            else:
                linked = self._call(
                    "session_lineage",
                    lineage_args,
                    stage="session_lineage",
                    session_id=next_session_id,
                )
                if linked is not _CALL_FAILED and self._lineage_result_valid(linked, lineage_args):
                    return
                self._remember_pending_lineage(lineage_args, 1)
                logger.warning(
                    "memleaf provider session lineage update failed for hermes/%s; automatic process deferred",
                    next_session_id,
                )

    def initialize(self, session_id: str, **kwargs) -> None:
        started_at = time.monotonic()
        self._hermes_home = str(kwargs.get("hermes_home") or _default_hermes_home())
        self._session_id = _safe_component(session_id, "hermes-session")
        with self._sync_lock:
            self._last_call_error = None
            self._last_auto_process_failure = None
            self._last_auto_process_deferred = None
            self._last_auto_process_external_evidence = None
        self._write_enabled = _memory_session_enabled(
            kwargs.get("platform", ""), kwargs.get("agent_context", "")
        )
        self._last_recall = None
        self._gate_enabled = False
        self._retrieval_ids_by_turn.clear()
        self._gate_turn_ids.clear()
        self._active_turn_numbers.clear()
        self._active_retrieval_ids.clear()
        self._observed_tool_call_keys.clear()
        self._session_aliases.clear()
        self._pending_lineage.clear()
        self._deferred_process_sessions.clear()
        self._last_retrieval_observation = "unknown"
        self._last_retrieval_audit = "SEARCH_UNKNOWN"
        self._version_warning_emitted = False
        if self._client is not None:
            self._client.close()
            self._client = None
        if not self._write_enabled:
            self._log_stage(
                "initialize",
                started_at=started_at,
                status="disabled",
                session_id=self._session_id,
                error_type="ExcludedSession",
            )
            return
        config = self._config()
        self._auto_process = bool(config["auto_process"])
        command = _resolve_command(config)
        if command is None:
            self._log_stage(
                "initialize",
                started_at=started_at,
                status="unavailable",
                session_id=self._session_id,
                error_type="ClientUnavailable",
            )
            return
        self._client = _MCPClient(
            command,
            str(_resolve_vault(config)),
            float(config["timeout"]),
            float(config["process_timeout"]),
        )
        # A real provider config is the explicit opt-in boundary for the
        # short-lived gate ledger.  Do not create gate state for hand-built
        # test providers or an unconfigured default path.
        if _config_path(self._hermes_home).is_file():
            self._gate_enabled = True
        self._log_stage(
            "initialize",
            started_at=started_at,
            status="ready",
            session_id=self._session_id,
        )
        self._call("stats", {}, stage="stats", session_id=self._session_id)
        self._check_version_sync()

    def system_prompt_block(self) -> str:
        if not self._write_enabled:
            return ""
        return (
            "# Memleaf Memory\n"
            "Memleaf is the active local-first memory provider. Each visible user "
            "turn must call the configured memleaf MCP search tool at least once "
            "before answering, including ordinary greetings; a no-match result is "
            "valid, but an MCP error must remain an error. The Scope Map supplied "
            "for the turn tells you where to search. Search returns only a light "
            "directory; read the best project/identifier match with memleaf MCP "
            "read(memory_id, retrieval_id) when its body is needed, carrying the "
            "current turn's retrieval_id exactly as supplied; a missing or mismatched "
            "token is a read failure, never a reason to fall back to a file tool. "
            "Read more only if needed; for ordinary relevance queries, do not read all entries to filter unrelated items. "
            "When the user asks for current "
            "todos, all unfinished work, urgent work, or work due in a time range, call memleaf MCP "
            "list_todos instead of relevance search; omit scope for a global query, follow every "
            "next_cursor until has_more=false, and read every matching todo body with the same retrieval_id. "
            "Never exclude a todo because another Hermes session or another Agent created it. Hermes has a soft "
            "observer only: do not claim a search happened unless the visible tool "
            "messages show it. Visible Hermes "
            "turns are durably captured into the local memleaf inbox. Automatic "
            "capture → process runs after your final answer and is authoritative: "
            "a capture only proves that the inbox received the turn. Until a "
            "successful explicit remember or update tool result is visible, never "
            "say that the turn was saved to permanent memory, persisted, or "
            "记好了/已落库. For automatic handling, say only that you will process "
            "it through the workflow or that it is recorded in the current "
            "conversation. If process fails, report that automatic processing "
            "failed and leave the inbox retryable. Do not use "
            "terminal, search_files, read_file, Python, or direct filesystem "
            "operations to search or read the memleaf Vault; those tools remain "
            "available for ordinary project/wiki files. Do not use direct vault "
            "file writes to simulate success, and do not infer automatic success merely because "
            "active or history files exist. Automatic recall is a directory of "
            "scope identifiers, hierarchy, and aliases only; it never contains "
            "memory IDs, titles, or bodies. Use deliberate remember/forget tools "
            "only when the user explicitly asks for that operation. Automatic "
            "capture and processing use only visible user and assistant text; "
            "tool calls/results, email or attachment bodies, and other hidden "
            "payloads are not automatic memory input."
        )

    def _auto_process_failure_notice(self, session_id: str) -> str:
        """Return a safe next-turn notice for an unfinished auto process.

        Only bounded status fields are retained. In particular, neither model
        output nor MCP error text is copied into the prompt. The notice is
        deliberately separate from recalled memories so a process failure can
        never be mistaken for a durable memory.
        """
        with self._sync_lock:
            failure = self._last_auto_process_failure
            if not isinstance(failure, Mapping) or failure.get("session_id") != session_id:
                return ""
            code = str(failure.get("error_code") or "model_failed")
            stage = str(failure.get("error_stage") or "process")
        return (
            "<memleaf-process-status>\n"
            f"Automatic memleaf processing for the previous visible turn failed "
            f"at {stage} ({code}). The captured turn remains pending and automatic "
            "memory extraction has not succeeded. Report the failure if relevant; "
            "do not write or rewrite the vault through terminal, read_file, Python, "
            "or filesystem operations, and do not claim success from active/history "
            "files alone.\n"
            "</memleaf-process-status>"
        )

    def _auto_process_deferred_notice(self, session_id: str) -> str:
        """Return a safe notice when process left scope work for later."""

        with self._sync_lock:
            deferred = self._last_auto_process_deferred
            if not isinstance(deferred, Mapping) or deferred.get("session_id") != session_id:
                return ""
            candidates = int(deferred.get("deferred_candidates", 0) or 0)
            turns = int(deferred.get("deferred_inbox_turns", 0) or 0)
            unresolved = int(deferred.get("unresolved_evidence_count", 0) or 0)
        if candidates <= 0 and turns <= 0 and unresolved <= 0:
            return ""
        return (
            "<memleaf-process-status>\n"
            f"Automatic memleaf processing completed with {candidates} deferred "
            f"candidate(s) across {turns} pending inbox turn(s) awaiting scope "
            "clarification. "
            f"There are {unresolved} unresolved evidence unit(s), including incomplete tool observations. "
            "Memory extraction is not fully complete; do not claim "
            "that every captured turn was processed.\n"
            "</memleaf-process-status>"
        )

    def _auto_process_external_evidence_notice(self, session_id: str) -> str:
        """Explain successful processing when no external body was retained."""

        with self._sync_lock:
            status = self._last_auto_process_external_evidence
            if not isinstance(status, Mapping) or status.get("session_id") != session_id:
                return ""
            status = dict(status)
        kind = status.get("status")
        if kind not in {"metadata_only", "disabled", "unavailable", "partial"}:
            return ""
        external_records = status.get("external_record_count", 0)
        retained_bodies = status.get("retained_body_count", 0)
        metadata_only = status.get("metadata_only_record_count", 0)
        incomplete = status.get("incomplete_record_count", 0)
        unusable = status.get("unusable_record_count", 0)
        mode = status.get("tool_evidence_mode", "unknown")
        if kind == "disabled":
            reason = f"the capture policy is disabled (mode={mode})"
        elif kind == "metadata_only":
            reason = "external records were retained as metadata without source bodies"
        elif kind == "partial":
            reason = (
                "some external records had no retained or complete source body "
                f"(metadata-only={metadata_only}, incomplete={incomplete}, unusable={unusable})"
            )
        else:
            reason = "external records were present without usable complete source bodies"
        return (
            "<memleaf-process-status>\n"
            "Automatic memleaf processing completed successfully, but "
            f"{reason}. Current batch: external records={external_records}, "
            f"retained external bodies={retained_bodies}. This status does not "
            "confirm that external content was extracted into memory; it is an "
            "informational capture status, not a processing failure.\n"
            "</memleaf-process-status>"
        )

    @staticmethod
    def _process_deferred_counts(value: Any) -> tuple[int, int] | None:
        if not isinstance(value, Mapping):
            return None
        counts: list[int] = []
        for key in ("deferred_candidates", "deferred_inbox_turns"):
            count = value.get(key, 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                return None
            counts.append(count)
        return counts[0], counts[1]

    def _call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        stage: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> Any:
        started_at = time.monotonic()
        resolved_stage = stage or name
        with self._sync_lock:
            self._last_call_error = None
        if self._client is None:
            self._log_stage(
                resolved_stage,
                started_at=started_at,
                status="unavailable",
                session_id=session_id,
                turn_id=turn_id,
                error_type="ClientUnavailable",
            )
            return _CALL_FAILED
        try:
            result = self._client.call_tool(name, arguments)
        except Exception as error:
            with self._sync_lock:
                self._last_call_error = {
                    "error_code": str(getattr(error, "code", "") or "mcp_failed"),
                    "error_stage": str(getattr(error, "stage", "") or resolved_stage),
                }
            self._log_stage(
                resolved_stage,
                started_at=started_at,
                status="error",
                session_id=session_id,
                turn_id=turn_id,
                error_type=_error_type(error),
                error_code=getattr(error, "code", "") if isinstance(error, _MCPToolError) else "",
                error_stage=getattr(error, "stage", "") if isinstance(error, _MCPToolError) else "",
                validation_reason=getattr(error, "validation_reason", "") if isinstance(error, _MCPToolError) else "",
                validation_detail=getattr(error, "validation_detail", "") if isinstance(error, _MCPToolError) else "",
                attempt_count=getattr(error, "attempt_count", None) if isinstance(error, _MCPToolError) else None,
            )
            return _CALL_FAILED
        error_fields = _mcp_error_fields(result)
        if error_fields is not None:
            error_code, error_stage, validation_reason, attempt_count, validation_detail = error_fields
            with self._sync_lock:
                self._last_call_error = {
                    "error_code": error_code,
                    "error_stage": error_stage or resolved_stage,
                }
            self._log_stage(
                resolved_stage,
                started_at=started_at,
                status="error",
                session_id=session_id,
                turn_id=turn_id,
                error_type="MCPToolError",
                error_code=error_code,
                error_stage=error_stage or "",
                validation_reason=validation_reason or "",
                validation_detail=validation_detail or "",
                attempt_count=attempt_count,
            )
            return _CALL_FAILED
        self._log_stage(
            resolved_stage,
            started_at=started_at,
            status="ok",
            session_id=session_id,
            turn_id=turn_id,
        )
        return result

    def _capture_visible(
        self,
        *,
        session_id: str,
        turn_id: str,
        role: str,
        content: str,
    ) -> bool:
        result = self._call(
            "capture",
            {
                "source": "hermes",
                "session_id": session_id,
                "turn_id": turn_id,
                "role": role,
                "content": content,
                "record": True,
                "visible": True,
            },
            stage=f"capture_{role}",
            session_id=session_id,
            turn_id=turn_id,
        )
        if isinstance(result, Mapping) and (result.get("stored") is True or result.get("duplicate") is True or result.get("suppressed") is True):
            return True
        logger.warning(
            "memleaf stage=capture_%s status=invalid_result source=hermes session=%s turn=%s",
            role,
            session_id,
            turn_id,
        )
        return False

    @staticmethod
    def _observe_search_messages(
        messages: Optional[List[Dict[str, Any]]],
        retrieval_id: Optional[str],
        *,
        session_id: str = "",
        turn_id: str = "",
        vault_root: Optional[Path] = None,
        seen_call_keys: Any = None,
        audit_state: Optional[dict[str, Any]] = None,
    ) -> str:
        """Observe explicit host MCP calls in public messages.

        The provider's own stdio calls (stats/capture/process/scope_catalog)
        are not evidence that Hermes' main agent performed a search or read.
        Hermes has no public pre-final hook, so this is deliberately
        diagnostic and fail-open when the message contract does not expose a
        tool result.  Read and file-tool diagnostics never write Core state.
        """

        calls = _visible_tool_calls(messages)
        results = _visible_tool_results(messages)
        statuses: list[str] = []
        search_results_used: set[int] = set()
        search_ordinal = 0
        for call in calls:
            if call.get("name") not in {"mcp__memleaf__search", "mcp__memleaf__list_todos"}:
                continue
            search_ordinal += 1
            arguments = call.get("arguments")
            if not isinstance(arguments, Mapping) or arguments.get("retrieval_id") != retrieval_id:
                continue
            payload = _tool_result_for_call(call, calls, results, search_results_used)
            if payload is not _CALL_FAILED:
                status = _hermes_search_status(payload)
                observation_key = _tool_observation_key(call, search_ordinal)
                if not _record_tool_observation(seen_call_keys, observation_key):
                    continue
                # Hermes has no public write-back hook for this soft observer.
                # A valid current-turn result is enough to mark the local
                # provider diagnostic; no Core ledger or body is touched here.
                statuses.append(status)

        read_results_used: set[int] = set()
        read_sequence = 0
        controlled_reads = 0
        read_ordinal = 0
        for call in calls:
            if call.get("name") != "mcp__memleaf__read":
                continue
            read_ordinal += 1
            arguments = call.get("arguments")
            retrieval_present = isinstance(arguments, Mapping) and "retrieval_id" in arguments
            retrieval_match = bool(
                retrieval_present
                and isinstance(retrieval_id, str)
                and arguments.get("retrieval_id") == retrieval_id
            )
            payload = _tool_result_for_call(call, calls, results, read_results_used)
            result_status = _hermes_read_status(payload)
            if result_status == "ok" and not retrieval_match:
                # A compatibility read may return body text despite missing or
                # mismatched gate binding.  Do not make that look like a
                # controlled success in the diagnostic stream.
                result_status = "uncontrolled_success"
            observation_key = _tool_observation_key(call, read_ordinal)
            if not _record_tool_observation(seen_call_keys, observation_key):
                continue
            read_sequence += 1
            if result_status == "ok" and retrieval_match:
                controlled_reads += 1
            logger.info(
                "memleaf retrieval-read source=hermes session=%s turn=%s read_seq=%d retrieval_present=%s retrieval_match=%s result=%s",
                _safe_component(session_id, "none") if session_id else "none",
                _safe_component(turn_id, "none") if turn_id else "none",
                read_sequence,
                retrieval_present,
                retrieval_match,
                result_status,
            )

        file_sequence = 0
        for call in calls:
            if not _file_tool_name(call.get("name")):
                continue
            path = _path_from_tool_arguments(call.get("arguments"))
            bypass = _path_is_within(vault_root, path)
            if bypass is None:
                continue
            file_sequence += 1
            logger.info(
                "memleaf file-tool source=hermes session=%s turn=%s file_seq=%d bypass=%s",
                _safe_component(session_id, "none") if session_id else "none",
                _safe_component(turn_id, "none") if turn_id else "none",
                file_sequence,
                "detected" if bypass else "not_detected",
            )
        if not statuses:
            search_status = "unknown"
            audit_status = "SEARCH_UNKNOWN"
        elif "found" in statuses:
            search_status = "found"
            # Hermes exposes no public pre-final hook and no reliable signal
            # that proves whether an answer used historical memory when no
            # controlled read occurred. Do not fabricate FOUND_NOT_USED or
            # FOUND_REQUIRED_READ_MISSING; record the uncertainty explicitly.
            audit_status = "FOUND_READ" if controlled_reads else "FOUND_NO_READ_UNDETERMINED"
        elif "no_match" in statuses:
            search_status = "no_match"
            audit_status = "NO_MATCH"
        else:
            search_status = "error"
            audit_status = "ERROR"
        if isinstance(audit_state, dict):
            audit_state.clear()
            audit_state.update(
                {
                    "status": audit_status,
                    "search_status": search_status,
                    "controlled_reads": controlled_reads,
                }
            )
        logger.info(
            "memleaf retrieval-audit source=hermes session=%s turn=%s status=%s search=%s controlled_reads=%d",
            _safe_component(session_id, "none") if session_id else "none",
            _safe_component(turn_id, "none") if turn_id else "none",
            audit_status,
            search_status,
            controlled_reads,
        )
        return search_status

    @staticmethod
    def _catalog_retrieval_id(value: Any) -> Optional[str]:
        """Extract only the MCP-issued opaque token from scope_catalog."""

        if not isinstance(value, Mapping):
            return None
        token = value.get("retrieval_id")
        if (
            not isinstance(token, str)
            or not token.startswith("rtv-")
            or len(token) > 80
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in token)
        ):
            return None
        return token

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = None
        if not self._write_enabled or not query:
            return ""
        safe_session = self._canonical_session_id(session_id or self._session_id)
        failure_notice = self._auto_process_failure_notice(safe_session)
        deferred_notice = self._auto_process_deferred_notice(safe_session)
        external_evidence_notice = self._auto_process_external_evidence_notice(safe_session)
        turn_number = self._current_turn_number(safe_session)
        scope_args: dict[str, Any] = {"limit": _MAX_SCOPE_ITEMS}
        if self._gate_enabled and isinstance(turn_number, int) and turn_number > 0:
            gate_turn_id = self._gate_turn_id(safe_session, turn_number)
            if gate_turn_id is None:
                # A missing on_turn_start is not enough evidence to create a
                # managed Hermes turn.  Keep this soft path unbound rather
                # than risking reuse of another visible conversation turn.
                gate_turn_id = ""
            if gate_turn_id:
                scope_args.update(
                    {
                        "source": "hermes",
                        "session_id": safe_session,
                        "turn_id": gate_turn_id,
                    }
                )
        catalog = self._call(
            "scope_catalog",
            scope_args,
            stage="scope_catalog",
            session_id=safe_session,
        )
        if catalog is _CALL_FAILED:
            notices = [
                notice
                for notice in (failure_notice, deferred_notice, external_evidence_notice)
                if notice
            ]
            notices.append(_SCOPE_MAP_INVALID_NOTICE)
            return "\n\n".join(notices)
        if not _scope_catalog_is_valid(catalog):
            notices = [
                notice
                for notice in (failure_notice, deferred_notice, external_evidence_notice)
                if notice
            ]
            notices.append(_SCOPE_MAP_INVALID_NOTICE)
            return "\n\n".join(notices)
        retrieval_id = self._catalog_retrieval_id(catalog)
        if retrieval_id is not None and isinstance(turn_number, int) and turn_number > 0:
            key = (safe_session, turn_number)
            self._retrieval_ids_by_turn[key] = retrieval_id
            self._retrieval_ids_by_turn.move_to_end(key)
            while len(self._retrieval_ids_by_turn) > _MAX_PENDING_TURN_NUMBERS:
                self._retrieval_ids_by_turn.popitem(last=False)
            self._active_retrieval_ids[safe_session] = retrieval_id
            self._active_retrieval_ids.move_to_end(safe_session)
        context, _ = _scope_context(
            catalog,
            retrieval_id=retrieval_id,
            scope_hint=_unique_query_scope(query, catalog),
        )
        if not context:
            notices = [
                notice
                for notice in (failure_notice, deferred_notice, external_evidence_notice)
                if notice
            ]
            return "\n\n".join(notices)
        # This provider injects a map, not recalled memory entries.  Do not
        # report it as N memories in Hermes' indicator.
        self._last_recall = None
        notices = [
            notice
            for notice in (failure_notice, deferred_notice, external_evidence_notice)
            if notice
        ]
        return "\n\n".join([*notices, context]) if notices else context

    def recall_status(self) -> Optional[RecallStatus]:
        if not self._write_enabled:
            return None
        return self._last_recall

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        turn_number: Optional[int] = None,
    ) -> None:
        # ``messages`` may contain system prompts, tool calls/results, and
        # attachment parts.  Hermes already supplies the visible user and
        # assistant strings separately; derive only a bounded search status
        # from an explicit public memleaf tool result below.  Never capture
        # the raw message list as business conversation content.
        if not self._write_enabled or self._client is None:
            return
        visible_events = [
            ("user", user_content),
            ("assistant", assistant_content),
        ]
        if any(not isinstance(content, str) or not content.strip() for _, content in visible_events):
            return

        effective_session = self._canonical_session_id(session_id or self._session_id)
        # Hermes serializes provider sync work, but this lock also protects
        # direct/plugin-level concurrent calls and makes process one-shot per
        # captured turn within this provider instance.
        with self._sync_lock:
            try:
                resolved_turn_number = turn_number
                if resolved_turn_number is None:
                    resolved_turn_number = self._take_turn_number(effective_session, user_content)
                elif isinstance(resolved_turn_number, int) and not isinstance(resolved_turn_number, bool):
                    self._discard_turn_number(effective_session, user_content, resolved_turn_number)
                turn_id = self._resolve_turn_id(
                    effective_session,
                    resolved_turn_number,
                    user_content,
                    assistant_content,
                )
                retrieval_id = self._gate_id_for_turn(effective_session, resolved_turn_number)
                if retrieval_id is None and resolved_turn_number is None:
                    retrieval_id = self._current_gate_id(effective_session)
                if self._gate_enabled:
                    audit_state: dict[str, Any] = {}
                    observation = self._observe_search_messages(
                        messages,
                        retrieval_id,
                        session_id=effective_session,
                        turn_id=turn_id,
                        vault_root=_resolve_vault(self._config()),
                        seen_call_keys=self._observed_tool_call_keys,
                        audit_state=audit_state,
                    )
                    while len(self._observed_tool_call_keys) > _MAX_OBSERVED_TOOL_CALL_KEYS:
                        self._observed_tool_call_keys.popitem(last=False)
                    self._last_retrieval_observation = observation
                    self._last_retrieval_audit = str(audit_state.get("status") or "SEARCH_UNKNOWN")
                lineage_ready = self._retry_pending_lineage(effective_session)
                for role, content in visible_events:
                    if not self._capture_visible(
                        session_id=effective_session,
                        turn_id=turn_id,
                        role=role,
                        content=content,
                    ):
                        return
                if not self._auto_process:
                    return
                if not lineage_ready:
                    self._defer_process_session(effective_session)
                    logger.warning(
                        "memleaf provider automatic process deferred for hermes/%s; session lineage is pending",
                        effective_session,
                    )
                    return
                self._process_deferred_sessions(effective_session, turn_id)
            except Exception as error:
                # A provider failure must not fail the user's Hermes turn.
                # Core process owns the transaction and leaves inbox/state
                # retryable when model, parsing, or persistence fails.
                logger.warning(
                    "memleaf provider sync failed stage=sync_turn source=hermes session=%s error_type=%s; inbox retained",
                    effective_session,
                    _error_type(error),
                )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # memleaf-mcp remains configured separately for deliberate search,
        # remember, and forget operations. The native provider owns automatic
        # recall/capture only, avoiding duplicate tool names in Hermes.
        return []

    def shutdown(self) -> None:
        with self._sync_lock:
            self._pending_lineage.clear()
            self._deferred_process_sessions.clear()
            self._process_jobs_by_session.clear()
        if self._client is not None:
            self._client.close()
        self._client = None


def register(ctx) -> None:
    ctx.register_memory_provider(MemleafMemoryProvider())

__all__ = [name for name in globals() if not name.startswith('__')]
