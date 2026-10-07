"""Schema-neutral crash-safe filesystem primitives."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_locks_guard = threading.Lock()


@dataclass
class _RootLock:
    mutex: threading.RLock = field(default_factory=threading.RLock)
    handle: BinaryIO | None = None
    depth: int = 0


_locks: dict[Path, _RootLock] = {}


class StoreError(RuntimeError):
    pass


class StoreLocked(StoreError):
    pass


class CorruptRecord(StoreError):
    pass


class StaleWrite(StoreError):
    pass


class IdentityMismatch(StoreError):
    pass


def safe_id(value: str) -> str:
    if value in {".", ".."} or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"unsafe record id: {value!r}")
    return value


class AtomicStore:
    def __init__(
        self,
        root: Path | str,
        *,
        directories: tuple[str, ...] = (),
        compact: bool | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        from engine.services import state

        self._compact = state.compact(self.root) if compact is None else compact
        if not self._compact:
            for directory in directories:
                (self.root / directory).mkdir(parents=True, exist_ok=True)
            self._legacy_handle: BinaryIO | None = None
            self._legacy_depth = 0
            return
        key = self.root.resolve()
        with _locks_guard:
            self._shared_lock = _locks.setdefault(key, _RootLock())

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        if not self._compact:
            with self._legacy_lock(blocking=blocking):
                yield
            return
        shared = self._shared_lock
        if not shared.mutex.acquire(blocking=blocking):
            raise StoreLocked(f"run store is locked: {self.root}")
        if shared.depth:
            shared.depth += 1
            try:
                yield
            finally:
                shared.depth -= 1
                shared.mutex.release()
            return
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except OSError as error:
            handle.close()
            shared.mutex.release()
            if isinstance(error, BlockingIOError):
                raise StoreLocked(f"run store is locked: {self.root}") from error
            raise
        shared.handle = handle
        shared.depth = 1
        try:
            yield
        finally:
            shared.depth = 0
            shared.handle = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            shared.mutex.release()

    @contextmanager
    def _legacy_lock(self, *, blocking: bool) -> Iterator[None]:
        if self._legacy_depth:
            self._legacy_depth += 1
            try:
                yield
            finally:
                self._legacy_depth -= 1
            return
        handle = (self.root / ".store.lock").open("a+b")
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except OSError as error:
            handle.close()
            if isinstance(error, BlockingIOError):
                raise StoreLocked(f"run store is locked: {self.root}") from error
            raise
        self._legacy_handle = handle
        self._legacy_depth = 1
        try:
            yield
        finally:
            self._legacy_depth = 0
            self._legacy_handle = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _lock_path(self) -> Path:
        if not self._compact:
            return self.root / ".store.lock"
        identity = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()
        return Path(tempfile.gettempdir()) / "epubox-locks" / f"{identity}.lock"

    def path(self, directory: str, record_id: str) -> Path:
        return self.root / directory / f"{safe_id(record_id)}.json"

    @staticmethod
    def sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def atomic_write_bytes(cls, path: Path, data: bytes) -> str:
        from engine.services import state

        if state.artifact(path) != path:
            return state.write(path, data)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            cls.sync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return hashlib.sha256(data).hexdigest()


__all__ = [
    "AtomicStore",
    "CorruptRecord",
    "IdentityMismatch",
    "StaleWrite",
    "StoreError",
    "StoreLocked",
    "safe_id",
]
