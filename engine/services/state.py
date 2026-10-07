"""Single-file persistence for compact book sessions."""

from __future__ import annotations

import hashlib
import json
import os
import stat as statmod
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = "epubox-state-1"
_STATE = "state.json"
_SOURCE = "source"
_cache: dict[Path, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
_cache_lock = threading.RLock()
_local = threading.local()


class StateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordStat:
    st_mode: int
    st_ino: int
    st_dev: int
    st_nlink: int
    st_uid: int
    st_gid: int
    st_size: int
    st_atime: float
    st_mtime: float
    st_ctime: float
    st_atime_ns: int
    st_mtime_ns: int
    st_ctime_ns: int


@dataclass
class _Batch:
    state: dict[str, Any]
    dirty: bool = False
    depth: int = 1
    aborted: bool = False


def initialize(
    root: Path | str,
    source: Path,
    source_hash: str,
    run_id: str,
    legacy_workdir: Path | None = None,
    *,
    _legacy_locked: bool = False,
) -> None:
    root = Path(root).resolve()
    source = source.resolve(strict=True)
    before = source.stat()
    if not _sha256(source) == source_hash:
        raise StateError("source hash does not match the original EPUB")
    metadata = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    ):
        raise StateError("source EPUB changed while its identity was recorded")
    from engine.services.atomic import AtomicStore

    with ExitStack() as locks:
        if legacy_workdir is None:
            imported = {}
        else:
            legacy = legacy_workdir.resolve(strict=True)
            if not _legacy_locked:
                locks.enter_context(AtomicStore(legacy).lock(blocking=False))
            imported = _legacy_records(legacy)
            imported["migration.json"] = json.dumps(
                {
                    "format": "epubox-migration-1",
                    "legacy_workdir": str(legacy),
                    "source_hash": source_hash,
                    "run_id": run_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        stamp = time.time_ns()
        value: dict[str, Any] = {
            "format": FORMAT,
            "source": {
                "path": str(source),
                "hash": source_hash,
                "st_dev": metadata.st_dev,
                "st_ino": metadata.st_ino,
            },
            "run_id": run_id,
            "records": {
                key: {"data": data, "stamp": stamp + offset} for offset, (key, data) in enumerate(imported.items())
            },
        }
        _available_root(root, legacy_workdir)
        root.mkdir(parents=True, exist_ok=True)
        (root / _SOURCE).mkdir(exist_ok=True)
        path = root / _STATE
        locks.enter_context(AtomicStore(root, compact=True).lock())
        if path.exists():
            current = _load(root)
            if _header(current) != _header(value):
                raise StateError("state.json belongs to a different source or run")
            if imported:
                current_data = {key: record["data"] for key, record in current["records"].items()}
                if not current_data:
                    _commit(root, value)
                elif current_data != imported:
                    raise StateError("existing compact records differ from the legacy checkpoint")
            return
        _commit(root, value)


@contextmanager
def batch(root: Path | str) -> Iterator[None]:
    root = Path(root).resolve()
    if not compact(root):
        yield
        return
    active = _active(root)
    if active is not None:
        active.depth += 1
        try:
            yield
        except BaseException:
            active.aborted = True
            raise
        finally:
            active.depth -= 1
        return
    from engine.services.atomic import AtomicStore

    with AtomicStore(root).lock():
        committed = _load(root)
        pending = dict(committed)
        pending["records"] = dict(committed["records"])
        current = _Batch(pending)
        _batches()[root] = current
        try:
            yield
            if current.aborted:
                raise StateError("nested compact batch aborted")
            if current.dirty:
                _commit(root, current.state)
        finally:
            del _batches()[root]


def compact(root: Path | str) -> bool:
    root = Path(root).resolve()
    path = root / _STATE
    if path.is_symlink():
        raise StateError("state.json must be a regular file")
    if not path.exists():
        return False
    if not path.is_file():
        raise StateError("state.json must be a regular file")
    _load(root)
    return True


def read(path: Path | str) -> bytes:
    path = Path(path)
    located = _locate(path)
    if located is None:
        return path.read_bytes()
    root, key = located
    record = _record(_view(root), key)
    return record["data"].encode("utf-8")


def text(path: Path | str) -> str:
    path = Path(path)
    located = _locate(path)
    if located is None:
        return path.read_text(encoding="utf-8")
    root, key = located
    return _record(_view(root), key)["data"]


def write(path: Path | str, data: bytes) -> str:
    path = Path(path)
    located = _locate(path)
    if located is None:
        return _native_write(path, data)
    root, key = located
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StateError(f"compact record is not UTF-8: {key}") from error
    active = _active(root)
    if active is not None:
        active.dirty = _write_record(active.state, key, value) or active.dirty
        return hashlib.sha256(data).hexdigest()
    from engine.services.atomic import AtomicStore

    with AtomicStore(root).lock():
        state = dict(_load(root))
        state["records"] = dict(state["records"])
        if not _write_record(state, key, value):
            return hashlib.sha256(data).hexdigest()
        _commit(root, state)
    return hashlib.sha256(data).hexdigest()


def exists(path: Path | str) -> bool:
    path = Path(path)
    located = _locate(path)
    if located is None:
        return path.exists()
    root, key = located
    records = _view(root)["records"]
    return key in records or any(name.startswith(f"{key}/") for name in records)


def is_file(path: Path | str) -> bool:
    path = Path(path)
    located = _locate(path)
    if located is None:
        return path.is_file()
    root, key = located
    return key in _view(root)["records"]


def is_dir(path: Path | str) -> bool:
    path = Path(path)
    located = _locate(path)
    if located is None:
        return path.is_dir()
    root, key = located
    return any(name.startswith(f"{key}/") for name in _view(root)["records"])


def glob(directory: Path | str, pattern: str) -> Iterator[Path]:
    directory = Path(directory)
    if compact(directory):
        root, prefix = directory.resolve(), Path()
    else:
        located = _locate(directory)
        if located is not None:
            root, key = located
            prefix = Path(key)
        else:
            root = None
            prefix = Path()
    if root is None:
        yield from directory.glob(pattern)
        return
    for key in sorted(_view(root)["records"]):
        candidate = Path(key)
        try:
            relative = candidate.relative_to(prefix)
        except ValueError:
            continue
        if relative.full_match(pattern):
            yield root / candidate


def stat(path: Path | str) -> os.stat_result | RecordStat:
    path = Path(path)
    located = _locate(path)
    if located is None:
        return path.stat()
    root, key = located
    record = _record(_view(root), key)
    stamp = record["stamp"]
    size = len(record["data"].encode("utf-8"))
    identifier = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    device = int.from_bytes(hashlib.sha256(str(root).encode()).digest()[:8], "big")
    seconds = stamp / 1_000_000_000
    return RecordStat(
        statmod.S_IFREG | 0o600,
        identifier,
        device,
        1,
        os.getuid(),
        os.getgid(),
        size,
        seconds,
        seconds,
        seconds,
        stamp,
        stamp,
        stamp,
    )


def unlink(path: Path | str, *, missing_ok: bool = False) -> None:
    path = Path(path)
    located = _locate(path)
    if located is None:
        path.unlink(missing_ok=missing_ok)
        return
    root, key = located
    active = _active(root)
    if active is not None:
        if key not in active.state["records"]:
            if missing_ok:
                return
            raise FileNotFoundError(path)
        del active.state["records"][key]
        active.dirty = True
        return
    from engine.services.atomic import AtomicStore

    with AtomicStore(root).lock():
        state = dict(_load(root))
        state["records"] = dict(state["records"])
        if key not in state["records"]:
            if missing_ok:
                return
            raise FileNotFoundError(path)
        del state["records"][key]
        _commit(root, state)


def snapshot(root: Path | str) -> Path:
    root = Path(root).resolve()
    if not compact(root):
        return root / "source.epub"
    return Path(_load(root)["source"]["path"])


def artifact(path: Path | str) -> Path:
    path = Path(path)
    located = _locate(path)
    return located[0] / _STATE if located is not None else path


def header(root: Path | str) -> Mapping[str, Any]:
    root = Path(root).resolve()
    if not compact(root):
        raise StateError(f"compact state does not exist: {root}")
    return _header(_load(root))


def import_records(root: Path | str, legacy_workdir: Path | str) -> None:
    root = Path(root).resolve()
    if not compact(root):
        raise StateError("compact state must be initialized before import")
    imported = _legacy_records(Path(legacy_workdir))
    from engine.services.atomic import AtomicStore

    with AtomicStore(root).lock():
        state = dict(_load(root))
        state["records"] = dict(state["records"])
        records = state["records"]
        stamp = time.time_ns()
        for offset, (key, value) in enumerate(imported.items()):
            previous = records.get(key)
            if isinstance(previous, dict) and previous.get("data") == value:
                continue
            prior_stamp = previous.get("stamp", 0) if isinstance(previous, dict) else 0
            records[key] = {"data": value, "stamp": max(stamp + offset, prior_stamp + 1)}
        _commit(root, state)


def reset_records(root: Path | str) -> None:
    root = Path(root).resolve()
    if not compact(root):
        raise StateError("compact state must be initialized before reset")
    from engine.services.atomic import AtomicStore

    with AtomicStore(root).lock():
        state = dict(_load(root))
        state["records"] = {}
        _commit(root, state)


def _locate(path: Path) -> tuple[Path, str] | None:
    absolute = path.absolute()
    for parent in absolute.parents:
        marker = parent / _STATE
        if not marker.exists() and not marker.is_symlink():
            continue
        compact(parent)
        try:
            relative = absolute.relative_to(parent)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] in {_SOURCE, _STATE}:
            return None
        _load(parent)
        return parent, relative.as_posix()
    return None


def _batches() -> dict[Path, _Batch]:
    value = getattr(_local, "batches", None)
    if value is None:
        value = {}
        _local.batches = value
    return value


def _active(root: Path) -> _Batch | None:
    return _batches().get(root)


def _view(root: Path) -> dict[str, Any]:
    active = _active(root)
    return active.state if active is not None else _load(root)


def _write_record(state: dict[str, Any], key: str, value: str) -> bool:
    records = state["records"]
    previous = records.get(key)
    if isinstance(previous, dict) and previous.get("data") == value:
        return False
    prior_stamp = previous.get("stamp", 0) if isinstance(previous, dict) else 0
    records[key] = {"data": value, "stamp": max(time.time_ns(), prior_stamp + 1)}
    return True


def _available_root(root: Path, legacy_workdir: Path | None) -> None:
    if not root.exists() or (root / _STATE).exists():
        return
    allowed: set[str] = set()
    source = root / _SOURCE
    if source.is_dir() and not source.is_symlink() and not any(source.iterdir()):
        allowed.add(_SOURCE)
    if legacy_workdir is not None:
        legacy = legacy_workdir.resolve(strict=True)
        if legacy.is_relative_to(root) and legacy != root:
            child = legacy
            while child.parent != root:
                parent = child.parent
                siblings = [path.name for path in parent.iterdir() if path.name not in {child.name, ".store.lock"}]
                if siblings:
                    raise StateError(f"legacy parent contains unrelated files: {', '.join(sorted(siblings))}")
                child = parent
            allowed.update({legacy.relative_to(root).parts[0], "active.json", ".store.lock"})
    unrelated = [path.name for path in root.iterdir() if path.name not in allowed]
    if unrelated:
        raise StateError(f"book directory contains unrelated files: {', '.join(sorted(unrelated))}")


def _legacy_records(legacy_workdir: Path) -> dict[str, str]:
    legacy = legacy_workdir.resolve(strict=True)
    imported: dict[str, str] = {}
    for path in sorted(legacy.rglob("*")):
        if path.is_symlink():
            raise StateError(f"legacy checkpoint contains an unsupported symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(legacy).as_posix()
        if relative in {"source.epub", ".store.lock", _STATE} or relative.startswith(f"{_SOURCE}/"):
            continue
        try:
            imported[relative] = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise StateError(f"legacy record is not UTF-8: {relative}") from error
    return imported


def _load(root: Path) -> dict[str, Any]:
    path = root / _STATE
    metadata = path.stat()
    signature = (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
    with _cache_lock:
        cached = _cache.get(root)
        if cached is not None and cached[0] == signature:
            return cached[1]
        try:
            value = json.loads(path.read_bytes(), object_pairs_hook=_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StateError(f"invalid state.json: {error}") from error
        _validate(value)
        _cache[root] = (signature, value)
        return value


def _validate(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"format", "source", "run_id", "records"}:
        raise StateError("state.json has an invalid root schema")
    if value["format"] != FORMAT or not isinstance(value["run_id"], str) or not value["run_id"]:
        raise StateError("state.json has an invalid format or run id")
    source = value["source"]
    if not isinstance(source, dict) or set(source) != {"path", "hash", "st_dev", "st_ino"}:
        raise StateError("state.json has an invalid source identity")
    if not isinstance(source["path"], str) or not Path(source["path"]).is_absolute():
        raise StateError("state source path must be absolute")
    if not isinstance(source["hash"], str) or len(source["hash"]) != 64:
        raise StateError("state source hash must be SHA-256")
    if any(character not in "0123456789abcdef" for character in source["hash"]):
        raise StateError("state source hash must be lowercase hexadecimal")
    if any(type(source[name]) is not int or source[name] < 0 for name in ("st_dev", "st_ino")):
        raise StateError("state source inode identity is invalid")
    records = value["records"]
    if not isinstance(records, dict):
        raise StateError("state records must be an object")
    for key, record in records.items():
        if not _valid_key(key) or not isinstance(record, dict) or set(record) != {"data", "stamp"}:
            raise StateError(f"invalid compact record: {key!r}")
        if not isinstance(record["data"], str) or type(record["stamp"]) is not int or record["stamp"] < 0:
            raise StateError(f"invalid compact record payload: {key!r}")


def _valid_key(key: Any) -> bool:
    if not isinstance(key, str) or not key or "\\" in key:
        return False
    path = Path(key)
    return (
        not path.is_absolute()
        and path.as_posix() == key
        and path.parts[0] not in {_SOURCE, _STATE}
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise StateError(f"duplicate state.json key: {key!r}")
        value[key] = item
    return value


def _record(state: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        return state["records"][key]
    except KeyError as error:
        raise FileNotFoundError(key) from error


def _header(state: dict[str, Any]) -> dict[str, Any]:
    return {"format": state["format"], "source": dict(state["source"]), "run_id": state["run_id"]}


def _commit(root: Path, value: dict[str, Any]) -> None:
    _validate(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path = root / _STATE
    descriptor, name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    metadata = path.stat()
    signature = (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
    with _cache_lock:
        _cache[root] = (signature, value)


def _native_write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "FORMAT",
    "RecordStat",
    "StateError",
    "artifact",
    "batch",
    "compact",
    "exists",
    "glob",
    "header",
    "import_records",
    "initialize",
    "is_dir",
    "is_file",
    "read",
    "reset_records",
    "snapshot",
    "stat",
    "text",
    "unlink",
    "write",
]
