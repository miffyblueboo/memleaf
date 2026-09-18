"""Small forward-recovery journal for explicit, revision-checked retraction.

Only an explicit retry resumes this journal. Entering a mutation boundary must
not replay it: forget needs to be able to cancel the frozen plaintext first.
The existing Vault lock, history writer and atomic file helpers are reused.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .locking import atomic_unlink, atomic_write_json, atomic_write_text, read_json
from .memory_writer import MemoryWriter
from .query_scan import ensure_scan_current
from .models import Memory, MemoryVersionError, utc_now
from .turn_plan import MAX_PLAN_BYTES, revision_digest
from .validation import parse_strict_json
from .vault import safe_component


def journal_identities(vault: Any, directory: str) -> list[str]:
    """Bounded, non-mutating inventory of the two explicit mutation journals.

    Presence is not permission to replay. Unknown entries are never discarded.
    """
    if directory not in {"retractions", "explicit_updates"}:
        raise ValueError("invalid mutation journal directory")
    root = vault.state_path / directory
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("unsafe mutation journal directory")
    if not root.exists():
        return []
    identities = []
    total = 0
    for path in root.iterdir():
        if len(identities) >= 128:
            raise ValueError("mutation journal inventory exceeds limit")
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise ValueError("invalid mutation journal entry")
        safe_component(path.stem, "memory id")
        total += path.stat().st_size
        if path.stat().st_size > MAX_PLAN_BYTES * 2 or total > 16 * 1024 * 1024:
            raise ValueError("mutation journal inventory exceeds byte limit")
        identities.append(path.stem)
    if len({i.casefold() for i in identities}) != len(identities):
        raise ValueError("duplicate mutation journal identity")
    return sorted(identities)


class RetractionCommitError(OSError):
    """An interrupted commit; applied describes the head, not index readiness."""

    def __init__(self, *, applied: bool | None):
        super().__init__("retraction incomplete; retry the same request to recover")
        self.applied = applied
        self.recovery_required = True


class RetractionManager:
    def __init__(self, service: Any):
        self.service = service

    def _path(self, memory_id: str) -> Path:
        safe_component(memory_id, "memory id")
        root = self.service.vault.state_path / "retractions"
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ValueError("unsafe retraction state path")
        path = root / f"{memory_id}.json"
        if path.is_symlink():
            raise ValueError("unsafe retraction journal")
        return path

    @staticmethod
    def _operation_id(memory_id: str, revision: str, reason: str | None) -> str:
        raw = json.dumps(["retract", memory_id, revision, reason], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        return "retract-" + hashlib.sha256(raw).hexdigest()

    def _load(self, memory_id: str) -> dict[str, Any] | None:
        path = self._path(memory_id)
        if not path.exists():
            return None
        if path.stat().st_size > MAX_PLAN_BYTES * 2:
            raise ValueError("retraction journal exceeds storage budget")
        stored = read_json(path)  # Existing malformed state is never reset.
        if (not isinstance(stored, Mapping) or type(stored.get("schema_version")) is not int
                or stored["schema_version"] != 1):
            raise ValueError("invalid retraction journal version")
        payload = stored.get("payload")
        if (not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_PLAN_BYTES
                or hashlib.sha256(payload.encode("utf-8")).hexdigest() != stored.get("checksum")):
            raise ValueError("invalid retraction journal checksum")
        plan = parse_strict_json(payload)
        required = {"memory_id", "operation_id", "expected_revision", "replacement_revision",
                    "before", "after", "archived_at", "reason"}
        if (not isinstance(plan, dict) or set(plan) != required
                or plan["memory_id"] != memory_id
                or any(not isinstance(plan[k], str) or not plan[k] for k in required - {"reason"})
                or (plan["reason"] is not None and not isinstance(plan["reason"], str))):
            raise ValueError("invalid retraction journal shape")
        before, after = Memory.from_markdown(plan["before"]), Memory.from_markdown(plan["after"])
        if (before.memory_id != memory_id or after.memory_id != memory_id
                or before.validity != "valid" or after.validity != "retracted"
                or revision_digest(before) != plan["expected_revision"]
                or revision_digest(after) != plan["replacement_revision"]
                or plan["operation_id"] != self._operation_id(memory_id, plan["expected_revision"], plan["reason"])
                or after.extra.get("retraction_operation_id") != plan["operation_id"]):
            raise ValueError("retraction journal does not match its frozen state")
        return plan

    def _prepare(self, current: Memory, expected: str, reason: str | None) -> dict[str, Any]:
        identities = journal_identities(self.service.vault, "retractions")
        if current.memory_id not in identities and len(identities) >= 128:
            raise ValueError("retraction journal capacity exhausted")
        now = utc_now()
        operation_id = self._operation_id(current.memory_id, expected, reason)
        value = current.to_dict()
        # A previously withdrawn record can be explicitly restored by a trusted
        # editor. Its old receipt/reason must not become the new operation's.
        value.pop("retraction_result_digest", None)
        value.pop("retraction_reason", None)
        value.update(body="", validity="retracted", updated=now,
                     retracted_at=now, retraction_operation_id=operation_id)
        if reason is not None:
            value["retraction_reason"] = reason
        from copy import deepcopy
        bases = deepcopy(value.get("field_basis", {}))
        if not isinstance(bases, dict) or any(not isinstance(item, dict) for item in bases.values()):
            raise ValueError("invalid_target_basis")
        bases["validity"] = {"source": "explicit_retraction", "source_time": now}
        value["field_basis"] = bases
        after = Memory.from_mapping(value)
        # The receipt validates replay after the journal is settled. It is
        # computed before adding itself; later authored edits invalidate it.
        after.extra["retraction_result_digest"] = revision_digest(after)
        # Serialize both versions before history or knowledge is changed.
        plan = {"memory_id": current.memory_id, "operation_id": operation_id,
                "expected_revision": expected, "replacement_revision": revision_digest(after),
                "before": current.to_markdown(), "after": after.to_markdown(),
                "archived_at": now, "reason": reason}
        payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_PLAN_BYTES:
            raise ValueError("retraction plan exceeds storage budget")
        atomic_write_json(self._path(current.memory_id), {
            "schema_version": 1, "payload": payload,
            "checksum": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        })
        return plan

    def retract_unlocked(self, memory_id: str, expected_revision: str, reason: str | None) -> Memory:
        """Caller holds the Vault mutation lock; no model is involved."""
        record, snapshot = self.service._revision_target_unlocked(memory_id)
        ensure_scan_current(self.service.vault, snapshot)
        if record is None:
            # Only proven absence permits cancellation. Preserve corrupt state
            # for inspection rather than deleting it as a missing-target retry.
            self._load(memory_id)
            ensure_scan_current(self.service.vault, snapshot)
            atomic_unlink(self._path(memory_id))
            raise ValueError("memory does not exist")
        current = record.memory
        # Case aliases resolve to the on-disk identity, not another journal or
        # operation receipt. Renaming a file does not change that identity.
        memory_id = current.memory_id
        current_revision = revision_digest(current)
        plan = self._load(memory_id)
        if plan is not None and current_revision not in {
            plan["expected_revision"], plan["replacement_revision"]
        }:
            # A later authorized edit wins. Cancel this stale plan; never apply
            # its old snapshot, even if the original caller retries afterwards.
            ensure_scan_current(self.service.vault, snapshot)
            atomic_unlink(self._path(memory_id))
            self.service._rebuild_index_unlocked()
            plan = None
        if plan is not None:
            acceptable = {plan["expected_revision"]}
            if current_revision == plan["replacement_revision"]:
                acceptable.add(current_revision)
            if expected_revision not in acceptable or (reason is not None and reason != plan["reason"]):
                raise MemoryVersionError("memory revision mismatch")
        else:
            receipt_state = current.to_dict()
            receipt_digest = receipt_state.pop("retraction_result_digest", None)
            same_applied_request = (
                receipt_digest == revision_digest(receipt_state)
                and current.validity == "retracted"
                and current.extra.get("retraction_operation_id") ==
                self._operation_id(memory_id, expected_revision, reason)
            )
            if current_revision != expected_revision and not same_applied_request:
                raise MemoryVersionError("memory revision mismatch")
            if current.validity == "retracted":
                # Also repairs pre-journal retractions when retried with their
                # current revision. No new history or business write is needed.
                ensure_scan_current(self.service.vault, snapshot)
                self.service._rebuild_index_unlocked()
                return current
            plan = self._prepare(current, expected_revision, reason)
        try:
            before = Memory.from_markdown(plan["before"])
            after = Memory.from_markdown(plan["after"])
            # Preparing the journal and writing history take time. An external
            # edit need not use our lock, so recheck the bounded current scan at
            # both boundaries. A stale prepared plan is not permission to write.
            ensure_scan_current(self.service.vault, snapshot)
            MemoryWriter(self.service)._write_history(
                before, superseded_by=memory_id, archived_at=plan["archived_at"],
                invalidated_reason="retracted",
            )
            ensure_scan_current(self.service.vault, snapshot)
            if current_revision == plan["expected_revision"]:
                if record.path.is_symlink():
                    raise ValueError("unsafe memory path")
                # Reads since the original preparation do not invalidate CAS.
                # Keep their accounting without changing the frozen assertion,
                # history identity or protected replacement revision.
                after.hit_count = current.hit_count
                after.last_hit_at = current.last_hit_at
                atomic_write_text(record.path, after.to_markdown())
            self.service._rebuild_index_unlocked()
            atomic_unlink(self._path(memory_id))
            return after if current_revision == plan["expected_revision"] else current
        except OSError as error:
            applied = None
            try:
                head = Memory.from_markdown(record.path.read_text(encoding="utf-8"), record.path)
                head_revision = revision_digest(head)
                if head_revision == plan["replacement_revision"]:
                    applied = True
                elif head_revision == plan["expected_revision"]:
                    applied = False
                # Another authored revision may have followed the write. Its
                # copied operation marker alone cannot prove the current result.
            except (OSError, ValueError, UnicodeError):
                pass
            raise RetractionCommitError(applied=applied) from error

    def cancel_unlocked(self, records: Iterable[Any]) -> None:
        """Forget must remove pending copies before deleting their source files."""
        ids: set[str] = set()
        for record in records:
            memory = record.memory
            ids.add(memory.memory_id)
            for key in ("active_memory_id", "original_memory_id"):
                value = memory.extra.get(key)
                if isinstance(value, str) and value:
                    ids.add(value)
        folded = {identity.casefold() for identity in ids}
        for memory_id in journal_identities(self.service.vault, "retractions"):
            if memory_id.casefold() in folded:
                atomic_unlink(self._path(memory_id))
