"""Read-only migration checks and explicit, locally verified backup creation.

No pipeline switch, process termination, model call, source repair or restore is
performed. A clear local report cannot certify that external writers stopped or
that the real model and host installations passed acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
from typing import Any

from . import __version__
from .inspection import _checked_snapshot, _snapshot, _fingerprint, existing_root
from .locking import _fsync_directory
from .models import utc_now
from .validation import parse_strict_json

VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_EXCLUDED = ["advisory_locks", "logs", "unmanaged_root_files", "external_native_sources",
             "host_installations", "external_credentials"]
_EXTERNAL_CHECKS = ["stop_all_supported_writers", "verify_core_and_provider_installations",
                    "real_model_multiturn_acceptance", "native_host_acceptance",
                    "verify_restore_against_latest_forget_state"]


class MigrationError(ValueError):
    """An explicit operational failure, containing only safe public codes."""
    def __init__(self, code: str, *, result: dict | None = None):
        super().__init__(code)
        self.code = code
        self.result = result or {"execution_status": "blocked", "code": code,
                                 "switch_authorized": False, "model_calls": 0}


def _runtime() -> dict:
    # A version number alone does not distinguish unreleased builds. Fingerprint
    # the installed implementation and packaged Provider, not a guessed Git SHA.
    root = Path(__file__).parent
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and (path.suffix == ".py" or path.name in {"plugin.yaml", "README.md"}):
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"package_version": __version__, "python": platform.python_version(),
            "platform": platform.system(), "implementation_fingerprint": _fingerprint(
                {key: bytes.fromhex(value) for key, value in files.items()}),
            "installed_host_verification": "not_performed"}


def _native_source_readiness(service: Any, config: Any) -> tuple[int, list[str]]:
    """Inspect configured native-source path readiness without reading content."""

    if not isinstance(config, dict):
        return 0, ["native_source_configuration_invalid"]
    try:
        from .native_index import validate_native_sources
        sources = validate_native_sources(config.get("native_sources", {}), base_dir=service.vault.root)
    except (OSError, ValueError, RuntimeError):
        return 0, ["native_source_configuration_invalid"]

    unavailable = 0
    for source in sources.values():
        if not source["enabled"]:
            continue
        path = Path(source["resolved_path"])
        try:
            if path.is_symlink() or not path.is_file():
                unavailable += 1
                continue
            with path.open("rb"):
                pass
        except OSError:
            unavailable += 1
    return unavailable, (["native_sources_unavailable"] if unavailable else [])


def _checks(service: Any, snapshot: dict[str, bytes]) -> dict:
    from . import extraction_work_state as budgets
    from . import incremental_journal as commits
    from . import incremental_run_state as runs
    from .compaction import Compactor
    from .process_common import _read_processed
    from .process_jobs import _read_state, _ACTIVE, _TERMINAL
    from .process_journal import ProcessJournal
    from .query_progress import observe_progress
    from .query_scan import scan_memories

    blockers: set[str] = set()
    warnings: set[str] = set()
    counts: dict[str, int] = {}
    config = None
    binding = {"status": "unknown"}
    try:
        binding = service.vault.identity_status()
        if binding["status"] != "bound":
            blockers.add("legacy_vault_requires_explicit_binding")
    except (OSError, ValueError, RuntimeError):
        blockers.add("invalid_vault_binding")
    try:
        config = service.vault.config()
    except (OSError, ValueError, UnicodeError, TypeError):
        blockers.add("invalid_configuration")
    if config is not None:
        native_unavailable, native_blockers = _native_source_readiness(service, config)
        counts["unavailable_native_sources"] = native_unavailable
        blockers.update(native_blockers)
    try:
        if "_state/processed.json" not in snapshot:
            blockers.add("processed_state_missing")
        # A pre-layout Vault must keep its old controls, not infer fresh empty
        # authority. Backup includes _index; layout migration is a separate step.
        if any(name in snapshot for name in ("_index/processed.json", "_index/compaction.json")):
            blockers.add("legacy_state_layout_requires_review")
        processed = _read_processed(service.vault.processed_state_path)
        for key in ("pending_turn_plans", "pending_operations"):
            if not isinstance(processed.get(key, {}), dict):
                raise ValueError("invalid_legacy_controls")
        from .state_layout import _read_layout
        _read_layout(service.vault.state_layout_path)
        loaded_runs = [runs.load_run(processed, key) for key in processed.get(runs.KEY, {})]
        loaded_commits = [commits.load_work(processed, key) for key in processed.get(commits.KEY, {})]
        resolved = commits.resolved_parent_ids(processed)
        counts["active_runs"] = sum(r["status"] not in runs.TERMINAL for r in loaded_runs)
        counts["unresolved_runs"] = sum(r["status"] in {"blocked", "failed", "completed_with_unresolved"}
                                         for r in loaded_runs)
        counts["pending_commits"] = sum(commits.public_result(w)["execution_status"] == "recovery_required"
                                           for w in loaded_commits if w["work_id"] not in resolved)
        counts["legacy_plans"] = len(processed.get("pending_turn_plans", {}))
        counts["legacy_operations"] = len(processed.get("pending_operations", {}))
        if runs.owner_live(processed) or any(ProcessJournal._processing_marker_live(s.get("processing"), utc_now())
                for s in processed.get("sessions", {}).values()):
            blockers.add("processing_owner_live")
        for key in ("active_runs", "pending_commits", "legacy_plans", "legacy_operations"):
            if counts[key]:
                blockers.add(key)
        if counts["unresolved_runs"]:
            blockers.add("unresolved_runs_require_review")
        from .state_layout import control_required
        if ("_state/extraction_request_budget.json" not in snapshot and
                (control_required(budgets._budget_path(service.vault)) or any(r["reserved_requests"] for r in loaded_runs))):
            blockers.add("request_budget_evidence_missing")
        budget = budgets._read_budget_state_unlocked(service.vault)
        for r in loaded_runs:
            row = budget["works"].get(r["budget_id"], {}).get("turns", {}).get(r["turn_budget_id"])
            if r["reserved_requests"] and (not row or row["requests"] < r["reserved_requests"]):
                blockers.add("request_budget_evidence_missing")
        if Compactor(service)._read_journal_unlocked() is not None:
            blockers.add("compaction_recovery_pending")
        from .memory_update import explicit_mutation_inventory
        explicit = explicit_mutation_inventory(service.vault)
        for name in ("retractions", "explicit_updates"):
            counts["pending_" + name] = explicit[name]
            if explicit[name]:
                blockers.add("pending_" + name)
                # Preserve both already documented machine-readable aliases.
                blockers.add("explicit_" + ("updates" if name == "explicit_updates" else "retractions") + "_recovery_pending")
    except (OSError, ValueError, UnicodeError, TypeError, KeyError, RuntimeError, RecursionError):
        blockers.add("invalid_runtime_controls")
    try:
        if "_state/process_jobs.json" in snapshot:
            parse_strict_json(snapshot["_state/process_jobs.json"].decode("utf-8"))
        queue = _read_state(service.vault)
        counts["queued_or_running_jobs"] = sum(j.get("status") in _ACTIVE | {"pending"} for j in queue["jobs"].values())
        if counts["queued_or_running_jobs"] or queue["active_job_id"] is not None:
            blockers.add("queue_not_quiescent")
        if any(j.get("status") not in _ACTIVE | _TERMINAL | {"pending"}
               for j in queue["jobs"].values()):
            blockers.add("unknown_job_state")
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
        blockers.add("invalid_job_state")
    try:
        scan = scan_memories(service.vault, include_history=True)
        scanned = scan.report()
        if scanned["issue_count"]:
            blockers.add("memory_scan_incomplete")
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
        scanned = {"status": "unknown"}
        blockers.add("memory_scan_unavailable")
    progress = observe_progress(service.vault)
    if progress["status"] == "unknown":
        blockers.add("pipeline_status_unknown")
    elif progress["status"] != "current":
        blockers.add("unsettled_sources_require_review")
    warnings.update(_EXTERNAL_CHECKS)
    # Even a disabled stage does not prove that an older installed binary will
    # never run. Report facts; never automatically flip either route.
    configured = {k: config["process"].get(k, "legacy") for k in
                  ("automatic_pipeline", "remember_pipeline")} if config else {}
    return {"local_status": "blocked" if blockers else "clear",
            "blockers": sorted(blockers),
            "backup_blockers": sorted(blockers & {"processing_owner_live", "queue_not_quiescent"}),
            "required_external_checks": sorted(warnings),
            "vault_binding": binding, "counts": counts, "scan_status": scanned, "pipeline_status": progress,
            "configured_pipelines": configured}


def migration_preflight(service: Any) -> dict:
    """Inspect existing files only. Does not create a lock or initialize a Vault."""
    root = existing_root(service.vault.root)
    before = _checked_snapshot(root)
    report = _checks(service, before)
    runtime = _runtime()
    if before != _snapshot(root):
        raise MigrationError("migration_snapshot_changed")
    return {"execution_status": "inspected", "read_only": True, "switch_authorized": False,
            "snapshot_revision": _fingerprint(before), "file_count": len(before),
            "total_bytes": sum(map(len, before.values())), "runtime": runtime,
            "backup_scope": {"areas": ["knowledge", "history", "inbox", "_state", "_index"],
                             "root_files": ["config.yaml"], "excluded": list(_EXCLUDED)},
            "model_calls": 0, **report}


def _exclusive_write(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _manifest(snapshot: dict[str, bytes], root: Path, report: dict) -> dict:
    return {"version": VERSION, "kind": "memleaf-migration-backup", "created_at": utc_now(),
            "source_vault": str(root), "snapshot_revision": _fingerprint(snapshot),
            "runtime": report["runtime"], "local_migration_status": report["local_status"],
            "migration_blockers": report["blockers"], "files": {name: {"size": len(raw),
              "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in sorted(snapshot.items())},
            "excluded": list(_EXCLUDED), "switch_authorized": False,
            "contains_sensitive_data": True, "restore_supported": False}


def verify_migration_backup(path: Path | str) -> dict:
    """Verify an existing backup without extracting, repairing or initializing it."""
    raw_path = Path(path).expanduser().absolute()
    try:
        base = raw_path.parent.resolve(strict=True) / raw_path.name
        if base.is_symlink() or not base.is_dir() or base.resolve() != base:
            raise MigrationError("unsafe_backup_path")
        manifest_path = base / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise MigrationError("backup_manifest_missing")
        with manifest_path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise MigrationError("backup_manifest_too_large")
        meta = parse_strict_json(raw.decode("utf-8"))
        if (not isinstance(meta, dict) or type(meta.get("version")) is not int or meta["version"] != VERSION
                or meta.get("kind") != "memleaf-migration-backup" or not isinstance(meta.get("files"), dict)
                or not isinstance(meta.get("runtime"), dict) or meta.get("switch_authorized") is not False):
            raise MigrationError("invalid_backup_manifest")
        if set(p.name for p in base.iterdir()) != {"manifest.json", "vault"}:
            raise MigrationError("unexpected_backup_files")
        vault = base / "vault"
        if vault.is_symlink() or not vault.is_dir():
            raise MigrationError("unsafe_backup_path")
        snapshot = _checked_snapshot(vault)
        # Reject even files the managed-area scanner intentionally ignores:
        # a backup must contain exactly its declared files, never an injected
        # path to restore later. No archive entry is ever interpreted as a path.
        observed = set()
        visited = 0
        def unreadable(error):
            raise MigrationError("backup_enumeration_failed") from error
        for parent, dirs, files in os.walk(vault, followlinks=False, onerror=unreadable):
            visited += len(dirs) + len(files)
            from .inspection import MAX_SNAPSHOT_FILES
            if visited > MAX_SNAPSHOT_FILES:
                raise MigrationError("backup_file_limit")
            for name in dirs + files:
                if (Path(parent) / name).is_symlink():
                    raise MigrationError("unsafe_backup_path")
            observed.update((Path(parent) / name).relative_to(vault).as_posix() for name in files)
        if observed != set(snapshot) or set(meta["files"]) != set(snapshot):
            raise MigrationError("backup_file_set_mismatch")
        for name, data in snapshot.items():
            expected = meta["files"][name]
            if (not isinstance(expected, dict) or set(expected) != {"size", "sha256"}
                    or type(expected["size"]) is not int or expected["size"] != len(data)
                    or expected["sha256"] != hashlib.sha256(data).hexdigest()):
                raise MigrationError("backup_content_mismatch")
        if meta.get("snapshot_revision") != _fingerprint(snapshot):
            raise MigrationError("backup_revision_mismatch")
        with manifest_path.open("rb") as stream:
            final_manifest = stream.read(MAX_MANIFEST_BYTES + 1)
        if raw != final_manifest or snapshot != _snapshot(vault):
            raise MigrationError("backup_changed_during_verification")
        return {"execution_status": "verified", "read_only": True, "switch_authorized": False,
                "restore_authorized": False, "snapshot_revision": meta["snapshot_revision"],
                "file_count": len(snapshot), "total_bytes": sum(map(len, snapshot.values())),
                "contains_sensitive_data": True, "model_calls": 0}
    except MigrationError:
        raise
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError) as error:
        raise MigrationError("backup_verification_failed") from error


def backup_for_migration(service: Any, destination: Path | str, *, expected_snapshot: str,
                         writers_stopped: bool = False) -> dict:
    """Create a new private directory; never replace an existing backup or Vault.

    writers_stopped is an operator attestation, not process-discovery proof.
    A manifest is published last. An interrupted destination is left incomplete
    for inspection; no source or destination is rolled back by guessing.
    """
    if writers_stopped is not True:
        raise MigrationError("writers_stopped_confirmation_required")
    if not isinstance(expected_snapshot, str) or re.fullmatch(r"[0-9a-f]{64}", expected_snapshot) is None:
        raise MigrationError("migration_snapshot_required")
    root = existing_root(service.vault.root)
    raw_destination = Path(destination).expanduser().absolute()
    try:
        dest = raw_destination.parent.resolve(strict=True) / raw_destination.name
    except OSError as error:
        raise MigrationError("unsafe_backup_destination") from error
    if not dest.parent.is_dir():
        raise MigrationError("unsafe_backup_destination")
    if dest == root or root in dest.parents or dest in root.parents:
        raise MigrationError("backup_destination_overlaps_vault")
    if dest.exists() or dest.is_symlink():
        raise MigrationError("backup_destination_exists")
    # Do not create a missing source state directory merely to take a lock.
    if not (root / "_state").is_dir() or (root / "_state").is_symlink():
        raise MigrationError("state_layout_requires_review")
    result = {"execution_status": "interrupted", "backup_status": "incomplete",
              "source_data_modified": False, "switch_authorized": False, "model_calls": 0}
    with service.vault.lock():
        report = migration_preflight(service)
        if expected_snapshot != report["snapshot_revision"]:
            raise MigrationError("migration_snapshot_changed")
        if report["backup_blockers"]:
            raise MigrationError("migration_preflight_blocked", result={**result, "blockers": report["backup_blockers"],
                                  "backup_status": "not_created", "code": "migration_preflight_blocked"})
        snapshot = _checked_snapshot(root)
        if _fingerprint(snapshot) != expected_snapshot:
            raise MigrationError("migration_snapshot_changed")
        # Exclusive mkdir prevents accidentally replacing an existing backup.
        # All subsequent operations are restricted to this new private directory.
        try:
            dest.mkdir(mode=0o700, exist_ok=False)
        except OSError as error:
            raise MigrationError("backup_destination_create_failed") from error
        try:
            target = dest / "vault"
            target.mkdir(mode=0o700)
            for name, raw in sorted(snapshot.items()):
                path = target / name
                for parent in reversed(path.parent.relative_to(target).parents):
                    (target / parent).mkdir(mode=0o700, exist_ok=True)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _exclusive_write(path, raw)
            meta = _manifest(snapshot, root, report)
            if snapshot != _snapshot(root):
                raise MigrationError("migration_snapshot_changed")
            _exclusive_write(dest / "manifest.json", (json.dumps(meta, ensure_ascii=False, sort_keys=True,
                indent=2, allow_nan=False) + "\n").encode("utf-8"))
            for directory, _, _ in os.walk(dest, topdown=False):
                _fsync_directory(Path(directory))
            _fsync_directory(dest.parent)
            verified = verify_migration_backup(dest)
            if snapshot != _snapshot(root):
                raise MigrationError("migration_snapshot_changed")
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            code = error.code if isinstance(error, MigrationError) else "backup_write_or_verification_failed"
            result.update(code=code, backup_status="requires_verification" if (dest / "manifest.json").exists() else "incomplete")
            raise MigrationError(code, result=result) from error
    return {**verified, "execution_status": "completed", "read_only": False,
            "backup_status": "verified", "source_data_modified": False,
            "writers_stopped": "operator_attested_not_independently_verified",
            "local_migration_status": report["local_status"], "migration_blockers": report["blockers"]}
