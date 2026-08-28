"""Small, local-only helpers for durable and serialized file writes."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover - the fallback is for non-POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _fsync_directory(directory: Path) -> None:
    """Make a directory entry update durable where the host supports it."""

    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write a file through a same-directory temp file and ``os.replace``."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: Optional[int] = None
    temporary: Optional[Path] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(Path(path), text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n"
    atomic_write_text(Path(path), payload, mode=mode)


def atomic_unlink(path: Path) -> None:
    """Remove one known file and durably flush its directory entry."""

    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


class VaultLock:
    """An advisory lock shared by all writes to one vault."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream = None
        self._fallback_lock: Optional[threading.RLock] = None

    def __enter__(self) -> "VaultLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("unsafe vault lock path")
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
        else:  # pragma: no cover - exercised only on non-POSIX hosts.
            key = str(self.path.resolve())
            with _thread_locks_guard:
                self._fallback_lock = _thread_locks.setdefault(key, threading.RLock())
            self._fallback_lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._stream is None:
            return
        if fcntl is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        elif self._fallback_lock is not None:  # pragma: no cover
            self._fallback_lock.release()
        self._stream.close()
        self._stream = None


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)
