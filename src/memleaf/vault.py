"""Filesystem layout and path safety for a memleaf vault."""

from __future__ import annotations

import os
from pathlib import Path

from .config import default_config, load_config, save_config
from .locking import VaultLock, atomic_write_json, atomic_write_text


_VAULT_README = """# memleaf vault

This directory is managed by memleaf. Markdown in `knowledge/` is the source
of truth. Files under `_index/` are disposable derived indexes and can be
rebuilt. Files under `_state/` are runtime correctness/recovery state and must
not be deleted as part of index maintenance.
"""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"unsafe {label}")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError(f"unsafe {label}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"unsafe {label}")
    return value


class Vault:
    """A local memleaf vault with predictable subdirectories."""

    def __init__(self, path: Path | str | None = None, *, create: bool = True):
        raw_path = Path(path).expanduser() if path is not None else Path.home() / ".memleaf"
        self.root = _resolved(raw_path)
        if create:
            self.ensure()

    @classmethod
    def initialize(cls, path: Path | str | None = None) -> "Vault":
        return cls(path, create=True)

    @property
    def config_path(self) -> Path:
        return self._inside("config.yaml")

    @property
    def inbox_path(self) -> Path:
        return self._inside("inbox")

    @property
    def knowledge_path(self) -> Path:
        return self._inside("knowledge")

    @property
    def history_path(self) -> Path:
        return self._inside("history")

    @property
    def index_path(self) -> Path:
        return self._inside("_index")

    @property
    def state_path(self) -> Path:
        return self._inside("_state")

    @property
    def logs_path(self) -> Path:
        return self._inside("logs")

    @property
    def tags_index_path(self) -> Path:
        return self._inside("_index", "tags.json")

    @property
    def native_sources_index_path(self) -> Path:
        return self._inside("_index", "native_sources.json")

    @property
    def native_index_path(self) -> Path:
        """Compatibility alias for the native sources derived index."""
        return self.native_sources_index_path

    @property
    def processed_state_path(self) -> Path:
        return self._inside("_state", "processed.json")

    @property
    def agents_state_path(self) -> Path:
        return self._inside("_state", "agents.json")

    @property
    def host_ingest_path(self) -> Path:
        return self._inside("_state", "host_ingest.json")

    @property
    def retrieval_gate_state_path(self) -> Path:
        return self._inside("_state", "retrieval_gate.json")

    @property
    def retrieval_gate_lock_path(self) -> Path:
        return self._inside("_state", "retrieval_gate.lock")

    @property
    def lock_path(self) -> Path:
        return self._inside("_state", "vault.lock")

    @property
    def state_layout_path(self) -> Path:
        return self._inside("_state", "layout.json")

    @property
    def compaction_journal_path(self) -> Path:
        return self._inside("_state", "compaction.json")

    @property
    def compaction_staging_root(self) -> Path:
        return self._inside("_state", ".compaction-staging")

    def compaction_staging_dir(self, transaction_id: str) -> Path:
        _safe_component(transaction_id, "compaction transaction id")
        return self._inside("_state", ".compaction-staging", transaction_id)

    def _inside(self, *parts: str) -> Path:
        candidate = self.root.joinpath(*parts)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ValueError("path escapes memleaf vault")
        return candidate

    def ensure(self) -> None:
        # Only a genuinely new/empty directory gets an automatic local owner.
        # Opening an existing Vault must not guess ownership or split its state.
        fresh = not self.root.exists() or not any(self.root.iterdir())
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            self.root = self.root.resolve()
        for directory in (
            self.inbox_path,
            self.knowledge_path,
            self.history_path,
            self.index_path,
            self.state_path,
        ):
            self._ensure_directory(directory)

        with self.lock():
            from .state_layout import migrate_state_layout
            migrate_state_layout(self)
            if fresh and self.identity_status()["status"] != "bound":
                import uuid
                self._bind_identity_unlocked("local-" + uuid.uuid4().hex)

            empty_tags = {
                "version": 1,
                "tags": {},
                "aliases": {},
                "keywords": {},
                "wikilinks": {},
                "history": {
                    "tags": {},
                    "aliases": {},
                    "keywords": {},
                    "wikilinks": {},
                },
            }
            empty_processed = {"version": 1, "event_keys": [], "events": {}, "sessions": {}}
            empty_agents = {"version": 1, "agents": {}}
            from .native_index import empty_native_index

            for path, value in (
                (self.tags_index_path, empty_tags),
                (self.native_sources_index_path, empty_native_index()),
                (self.processed_state_path, empty_processed),
                (self.agents_state_path, empty_agents),
            ):
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("unsafe vault managed path")
                else:
                    if path == self.processed_state_path:
                        from .state_layout import control_required
                        if control_required(path):
                            # Preserve the missing-authority condition. Public
                            # read-only APIs can still inspect Markdown; mutation
                            # readers reject instead of manufacturing permission.
                            continue
                    atomic_write_json(path, value)
            from .state_layout import require_control
            if self.processed_state_path.is_file():
                require_control(self, "processed.json")
            if (self.state_path / "extraction_request_budget.json").is_file():
                require_control(self, "extraction_request_budget.json")
            if not self.config_path.exists():
                save_config(self.config_path, default_config(self.root))
            if not self._inside("README.md").exists():
                atomic_write_text(self._inside("README.md"), _VAULT_README)

    def _ensure_directory(self, directory: Path) -> None:
        if directory.exists() and directory.is_symlink():
            raise ValueError("unsafe vault directory")
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    def config(self) -> dict:
        return load_config(self.config_path, vault=self.root)

    def identity_status(self) -> dict:
        """Local binding metadata, not a multi-user authentication mechanism."""
        from .state_layout import _read_layout
        layout = _read_layout(self.state_layout_path)
        binding = layout.get("binding") if layout else None
        return ({"status": "bound", **binding} if binding is not None
                else {"status": "legacy_unbound", "requires_explicit_binding": True})

    def _bind_identity_unlocked(self, principal_id: str) -> dict:
        from .state_layout import _read_layout, StateLayoutError
        import uuid
        safe_component(principal_id, "principal id")
        if len(principal_id) > 160 or principal_id.strip() != principal_id:
            raise ValueError("invalid principal id")
        layout = _read_layout(self.state_layout_path)
        if layout is None:
            raise StateLayoutError("binding requires an established layout")
        previous = layout.get("binding")
        if previous is not None:
            if previous["principal_id"] != principal_id:
                raise ValueError("Vault already bound; rebinding and cross-user merging are unsupported")
            return {"status": "bound", **previous}
        layout["binding"] = {"version": 1, "vault_id": "vault-" + uuid.uuid4().hex,
                             "principal_id": principal_id}
        atomic_write_json(self.state_layout_path, layout, mode=0o600)
        return self.identity_status()

    def bind_identity(self, principal_id: str) -> dict:
        """Explicit first binding only; keeps all existing work/receipt keys."""
        with self.lock():
            if self.identity_status()["status"] != "bound":
                from .service import Memleaf
                report = Memleaf(self).migration_preflight()
                if set(report["blockers"]) - {"legacy_vault_requires_explicit_binding"}:
                    raise ValueError("resolve pending migration checks before binding an existing Vault")
            return self._bind_identity_unlocked(principal_id)

    def lock(self) -> VaultLock:
        return VaultLock(self.lock_path)

    def memory_path(self, memory_id: str, area: str = "knowledge") -> Path:
        _safe_component(memory_id, "memory id")
        if area == "knowledge":
            base = self.knowledge_path
        elif area == "history":
            base = self.history_path
        else:
            raise ValueError("invalid memory area")
        return self._inside(str(base.relative_to(self.root)), f"{memory_id}.md")

    def history_version_path(self, memory_id: str, version: str) -> Path:
        _safe_component(memory_id, "memory id")
        _safe_component(version, "history version")
        if version.endswith(".md"):
            version = version[:-3]
            _safe_component(version, "history version")
        return self._inside("history", f"{memory_id}--{version}.md")

    def session_path(self, source: str, session_id: str) -> Path:
        _safe_component(source, "source")
        _safe_component(session_id, "session id")
        source_directory = self._inside("inbox", source)
        if source_directory.exists() and source_directory.is_symlink():
            raise ValueError("unsafe inbox source directory")
        source_directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(source_directory, 0o700)
        except OSError:
            pass
        return self._inside("inbox", source, f"{session_id}.md")

    def list_markdown(self, area: str) -> list[Path]:
        if area == "knowledge":
            base = self.knowledge_path
        elif area == "history":
            base = self.history_path
        elif area == "inbox":
            base = self.inbox_path
        else:
            raise ValueError("invalid markdown area")
        if not base.exists():
            return []
        paths: list[Path] = []
        for path in sorted(base.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(self.root)
            except ValueError:
                continue
            paths.append(path)
        return paths


def safe_component(value: str, label: str = "path component") -> str:
    """Public validation helper used by capture and tests."""
    return _safe_component(value, label)
