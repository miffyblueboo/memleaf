"""Read-only Vault audit and isolated processing previews.

No filesystem lock or index is created in the inspected Vault. An optimistic
content snapshot detects concurrent changes; a preview is never an authorization
to replay the displayed plan against a different live revision. Model calls may
occur during preview, but audit is entirely local and never calls a model.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import Memory
from .turn_plan import dedup_digest

MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_SNAPSHOT_FILES = 100_000
_AREAS = frozenset({"knowledge", "history", "inbox", "_index"})


class InspectionError(ValueError):
    """An inspection cannot establish a safe, stable input snapshot."""


def existing_root(path: Path | str | None = None) -> Path:
    root = Path(path or os.environ.get("MEMLEAF_VAULT") or Path.home() / ".memleaf").expanduser().resolve()
    if not root.is_dir() or not (root / "config.yaml").is_file():
        raise InspectionError("existing Vault with config.yaml is required")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    total = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        current = Path(parent)
        relative = current.relative_to(root)
        if current == root:
            directories[:] = [name for name in directories if name in _AREAS]
            files = [name for name in files if name == "config.yaml"]
        for name in directories + files:
            if (current / name).is_symlink():
                raise InspectionError("inspection refuses symlinked Vault children")
        for name in sorted(files):
            if name.endswith(".lock"):
                continue
            path = current / name
            if not path.is_file():
                raise InspectionError("inspection requires regular files")
            if path.stat().st_size + total > MAX_SNAPSHOT_BYTES or len(result) >= MAX_SNAPSHOT_FILES:
                raise InspectionError("Vault exceeds bounded inspection budget")
            data = path.read_bytes()
            total += len(data)
            if total > MAX_SNAPSHOT_BYTES:
                raise InspectionError("Vault changed beyond inspection budget")
            result[(relative / name).as_posix()] = data
    return result


def _fingerprint(snapshot: dict[str, bytes]) -> str:
    hashes = [(name, hashlib.sha256(value).hexdigest()) for name, value in sorted(snapshot.items())]
    return hashlib.sha256(json.dumps(hashes, separators=(",", ":")).encode()).hexdigest()


def _checked_snapshot(root: Path) -> dict[str, bytes]:
    first = _snapshot(root)
    if first != _snapshot(root):
        raise InspectionError("Vault changed while reading; retry inspection")
    return first


def audit_vault(path: Path | str | None = None) -> dict[str, Any]:
    """Report verifiable inconsistencies only; never infer producing versions."""
    root = existing_root(path)
    snapshot = _checked_snapshot(root)
    issues: list[dict[str, Any]] = []
    groups: dict[str, list[str]] = {}
    memories = 0
    for name, raw in sorted(snapshot.items()):
        if not name.startswith("knowledge/") or not name.endswith(".md"):
            continue
        try:
            memory = Memory.from_markdown(raw.decode("utf-8"), root / name)
            memories += 1
            groups.setdefault(dedup_digest(memory.to_dict()), []).append(memory.memory_id)
        except (ValueError, UnicodeError, TypeError):
            issues.append({"kind": "invalid_memory", "path": name})
    for ids in groups.values():
        if len(ids) > 1:
            issues.append({"kind": "identical_active_payloads", "memory_ids": sorted(ids)})
    try:
        processed = json.loads(snapshot.get("_index/processed.json", b"{}"))
        if not isinstance(processed, dict):
            raise ValueError("invalid ledger")
        plans = processed.get("pending_turn_plans", {})
        operations = processed.get("pending_operations", {})
        if not isinstance(plans, dict) or not isinstance(operations, dict):
            raise ValueError("invalid journal")
        for key, record in plans.items():
            valid = (isinstance(record, dict) and isinstance(record.get("payload"), str)
                     and hashlib.sha256(record["payload"].encode()).hexdigest() == record.get("checksum"))
            issues.append({"kind": "pending_turn_plan" if valid else "corrupt_turn_plan", "plan_id": key})
        if operations:
            issues.append({"kind": "pending_operations", "count": len(operations)})
        legacy = 0
        for state in processed.get("sessions", {}).values():
            for entry in state.get("processed_turns", []):
                if "evidence_dispositions" not in entry:
                    legacy += 1
                if entry.get("deferred_evidence") or entry.get("deferred_candidates"):
                    issues.append({"kind": "unresolved_turn", "turn_key": entry.get("turn_key"),
                        "evidence_count": len(entry.get("deferred_evidence", [])),
                        "candidate_count": len(entry.get("deferred_candidates", []))})
    except (ValueError, UnicodeError, TypeError, AttributeError):
        issues.append({"kind": "invalid_processed_ledger"})
        legacy = None
    if snapshot != _snapshot(root):
        raise InspectionError("Vault changed while auditing; retry inspection")
    return {"status": "ok", "mode": "audit", "vault": str(root), "read_only": True,
            "snapshot_digest": _fingerprint(snapshot), "memory_count": memories,
            "issue_count": len(issues), "issues": issues, "legacy_unaccounted_turns": legacy,
            "semantic_adjudication": "not_performed", "producing_version": "not_inferred"}


def preview_process(path: Path | str | None = None, *, model: Any = None, router: Any = None,
                    source: str | None = None, session_id: str | None = None, scope: Any = None,
                    clock: Any = None) -> dict[str, Any]:
    """Run the same processor on an isolated copy, never the source Vault.

    TemporaryDirectory is private to the current user. Existing configured
    native-memory sources may be READ by the normal processor; they are not
    rewritten. The result contains knowledge/history changes, never configuration
    credentials or a raw copy of tool output. The temporary copy is removed.
    """
    from .service import Memleaf
    root = existing_root(path)
    before = _checked_snapshot(root)
    with tempfile.TemporaryDirectory(prefix="memleaf-preview-") as temporary:
        target = Path(temporary) / "vault"
        for name, data in before.items():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        core = Memleaf(target, model=model, router=router, clock=clock)
        result = core.process(source=source, session_id=session_id, model=model, router=router, scope=scope)
        after = _snapshot(target)
        changes = []
        for name in sorted(set(before) | set(after)):
            if not name.startswith(("knowledge/", "history/")) or before.get(name) == after.get(name):
                continue
            changes.append({"path": name, "action": "DELETE" if name not in after else
                           "CREATE" if name not in before else "UPDATE",
                            "content": after[name].decode("utf-8") if name in after else None})
        ledger = json.loads(after.get("_index/processed.json", b"{}"))
        dispositions = []
        for key, state in ledger.get("sessions", {}).items():
            if source is not None and not key.startswith(source + "/"):
                continue
            if session_id is not None and key.partition("/")[2] != session_id:
                continue
            for entry in state.get("processed_turns", []):
                dispositions.append({"session": key, "turn_key": entry.get("turn_key"),
                    "candidates": entry.get("candidate_dispositions", []),
                    "evidence": entry.get("evidence_dispositions", [])})
    if before != _snapshot(root):
        raise InspectionError("source Vault changed during preview; discard this preview and retry")
    return {"status": "ok", "mode": "dry_run", "vault": str(root), "source_unchanged": True,
            "snapshot_digest": _fingerprint(before), "model_calls_possible": True,
            "result": result, "changes": changes, "dispositions": dispositions,
            "apply_supported": False}
