"""Schema-neutral crash-safe filesystem primitives."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


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
    def __init__(self, root: Path | str, *, directories: tuple[str, ...] = ()):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self._lock_handle: BinaryIO | None = None
        self._lock_depth = 0

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        lock_path = self.root / ".store.lock"
        handle = lock_path.open("a+b")
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as error:
            handle.close()
            raise StoreLocked(f"run store is locked: {self.root}") from error
        self._lock_handle = handle
        self._lock_depth = 1
        try:
            yield
        finally:
            self._lock_depth = 0
            self._lock_handle = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

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
