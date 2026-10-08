"""Five-file storage for compact translation sessions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

FORMAT = "epubox-shards-1"
SIDE_FORMAT = "epubox-shard-1"
TRANSACTION_FORMAT = "epubox-shard-transaction-1"
NAMES = ("origin", "mapping", "plan", "terms", "state")
FILES = tuple(f"{name}.json" for name in NAMES)
MAX_BYTES = 400_000_000


class ShardError(RuntimeError):
    pass


def owner(key: str) -> str:
    if key in {"source.json", "preparation.json", "documents"} or key.startswith("documents/"):
        return "origin"
    if key in {"inventories", "members", "pieces"} or key.startswith(("inventories/", "members/", "pieces/")):
        return "mapping"
    if key in {"prepared.json", "bookplan.json", "batches", "checks", "plans", "units"} or key.startswith(
        ("batches/", "checks/", "plans/", "units/")
    ):
        return "plan"
    if key in {"glossary", "glossary.json"} or key.startswith("glossary/"):
        return "terms"
    return "state"


def physical(root: Path, key: str, owners: dict[str, str] | None = None) -> Path:
    name = owners.get(key, owner(key)) if owners is not None else owner(key)
    return root / f"{name}.json"


def load(root: Path, object_hook) -> tuple[dict[str, Any], dict[str, str], tuple[tuple[str, int, int, int, int], ...]]:
    state_path = root / "state.json"
    state_raw = state_path.read_bytes()
    state = _loads(state_raw, state_path.name, object_hook)
    storage = state.get("storage") if isinstance(state, dict) else None
    if storage is None:
        return state, {}, (_signature(state_path),)
    _validate_storage(storage)
    if "transaction" in storage:
        _recover(root, state, object_hook)
        state = _read_json(state_path, object_hook)
        storage = state.get("storage")
        _validate_storage(storage)
    elif len(state_raw) >= MAX_BYTES:
        raise ShardError(f"state.json exceeds {MAX_BYTES} bytes")
    if not isinstance(storage, dict):
        raise ShardError("state.json has an invalid storage schema")
    owners = storage["owners"]
    records = dict(state.get("records", {}))
    signatures = {"state.json": _signature(state_path)}
    locations = {key: "state" for key in records}
    for name in NAMES[:-1]:
        meta = storage["shards"][name]
        path = root / meta["file"]
        raw = _read(path)
        if len(raw) != meta["size"] or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise ShardError(f"{path.name} does not match state.json")
        value = _loads(raw, path.name, object_hook)
        if not isinstance(value, dict) or set(value) != {"format", "name", "records"}:
            raise ShardError(f"{path.name} has an invalid root schema")
        if value["format"] != SIDE_FORMAT or value["name"] != name or not isinstance(value["records"], dict):
            raise ShardError(f"{path.name} has an invalid shard identity")
        overlap = locations.keys() & value["records"].keys()
        if overlap:
            raise ShardError(f"duplicate logical record across shards: {min(overlap)}")
        records.update(value["records"])
        locations.update({key: name for key in value["records"]})
        signatures[path.name] = _signature(path)
    if set(owners) != set(records):
        raise ShardError("state.json owners do not match stored records")
    for key, name in owners.items():
        if name not in NAMES:
            raise ShardError(f"invalid shard owner for {key!r}")
        if locations.get(key) != name:
            raise ShardError(f"record is stored in the wrong shard: {key!r}")
    merged = dict(state)
    merged.pop("storage", None)
    merged["records"] = records
    return merged, dict(owners), tuple(signatures[name] for name in FILES)


def commit(
    root: Path,
    value: dict[str, Any],
    previous: dict[str, str] | None = None,
    before: dict[str, Any] | None = None,
) -> dict[str, str]:
    previous = previous or {}
    records = value["records"]
    owners = {key: previous.get(key, owner(key)) for key in records}
    grouped = _group(records, owners)
    sharded = all((root / name).is_file() and not (root / name).is_symlink() for name in FILES)
    storage = _read_json(root / "state.json", _unique).get("storage") if sharded else None
    if storage is None or before is None:
        encoded = _encode_all(value, grouped, owners)
        _rebalance(value, grouped, owners, encoded)
        changed: set[str] = set(NAMES)
    else:
        changed = {
            name
            for key in set(before["records"]) | set(records)
            for name in {previous.get(key, owner(key)), owners.get(key, owner(key))}
            if before["records"].get(key) != records.get(key)
        }
        sidecars = {
            name: _encode({"format": SIDE_FORMAT, "name": name, "records": grouped[name]}, limit=False)
            for name in changed - {"state"}
        }
        if any(len(data) >= MAX_BYTES for data in sidecars.values()):
            encoded = _encode_all(value, grouped, owners)
            _rebalance(value, grouped, owners, encoded)
            changed = set(NAMES)
        else:
            metadata = dict(storage["shards"])
            metadata.update(
                {
                    name: {
                        "file": f"{name}.json",
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for name, data in sidecars.items()
                }
            )
            encoded = dict(sidecars)
            encoded["state"] = _encode_state_with_metadata(value, grouped["state"], owners, metadata)
            if len(encoded["state"]) >= MAX_BYTES:
                encoded = _encode_all(value, grouped, owners)
                _rebalance(value, grouped, owners, encoded)
                changed = set(NAMES)
            else:
                changed.add("state")
    if not changed:
        return owners
    if changed == {"state"}:
        _replace(root / "state.json", encoded["state"])
        return owners
    _transaction(root, value, encoded, changed)
    return owners


def signatures(root: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    names = FILES if all((root / name).is_file() for name in FILES) else ("state.json",)
    return tuple(_signature(root / name) for name in names)


def initialize(root: Path, value: dict[str, Any]) -> None:
    grouped = _group(value["records"], {key: owner(key) for key in value["records"]})
    owners = {key: owner(key) for key in value["records"]}
    encoded = _encode_all(value, grouped, owners)
    _rebalance(value, grouped, owners, encoded)
    _transaction(root, value, encoded, set(NAMES))


def cleanup(root: Path) -> None:
    if (root / "state.json").exists() or (root / "state.json").is_symlink():
        return
    for name in NAMES:
        for path in root.glob(f".{name}.*.tmp"):
            if path.is_symlink() or not path.is_file() or path.parent != root:
                raise ShardError(f"unsafe orphan shard temporary: {path.name}")
            path.unlink()


def _transaction(root: Path, value: dict[str, Any], encoded: dict[str, bytes], changed: set[str]) -> None:
    staged: dict[str, Path] = {}
    try:
        for name in changed:
            staged[name] = _stage(root, name, encoded[name])
    except BaseException:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise
    transaction = {
        "format": TRANSACTION_FORMAT,
        "files": {
            name: {
                "temporary": path.name,
                "size": len(encoded[name]),
                "sha256": hashlib.sha256(encoded[name]).hexdigest(),
                "before": _metadata(root / f"{name}.json"),
            }
            for name, path in staged.items()
        },
    }
    final = _loads(encoded["state"], "state.json", _unique)
    journal = {key: value[key] for key in ("format", "source", "run_id")}
    journal["records"] = {}
    journal["storage"] = {
        "format": FORMAT,
        "owners": {},
        "shards": final["storage"]["shards"],
        "transaction": transaction,
    }
    try:
        for name, meta in transaction["files"].items():
            before = meta["before"]
            final_path = root / f"{name}.json"
            if before is None:
                if final_path.exists() or final_path.is_symlink():
                    raise ShardError(f"unexpected stable shard before transaction: {name}")
            elif not _matches(final_path, before):
                raise ShardError(f"stable shard changed before transaction: {name}")
        _replace(root / "state.json", _encode(journal))
    except BaseException:
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise
    _finish(root, transaction)


def _recover(root: Path, state: dict[str, Any], object_hook) -> None:
    from engine.services.atomic import AtomicStore, StoreLocked

    try:
        with AtomicStore(root, compact=True).lock(blocking=False):
            current = _read_json(root / "state.json", object_hook)
            storage = current.get("storage", {})
            transaction = storage.get("transaction")
            if transaction is not None:
                _validate_pending(root, current, transaction, object_hook)
                _finish(root, transaction)
    except StoreLocked as error:
        raise ShardError("state transaction is pending while the run is locked") from error


def _finish(root: Path, transaction: dict[str, Any]) -> None:
    _validate_transaction(transaction)
    files = transaction["files"]
    for name in NAMES[:-1]:
        if name not in files:
            continue
        _install(root, name, files[name])
    if "state" not in files:
        raise ShardError("cross-shard transaction has no final state")
    _install(root, "state", files["state"], transaction=transaction)
    _sync(root)


def _validate_pending(root: Path, journal: dict[str, Any], transaction: dict[str, Any], object_hook) -> None:
    _validate_transaction(transaction)
    if (root / "state.json").stat().st_size >= MAX_BYTES:
        raise ShardError(f"state.json exceeds {MAX_BYTES} bytes")
    meta = transaction["files"]["state"]
    temporary = root / meta["temporary"]
    if not _matches(temporary, meta):
        raise ShardError("cannot recover incomplete shard transaction: state")
    final = _loads(_read(temporary), "staged state", object_hook)
    if any(journal.get(key) != final.get(key) for key in ("format", "source", "run_id")):
        raise ShardError("state transaction identity does not match its staged state")
    storage = final.get("storage")
    _validate_storage(storage)
    if isinstance(storage, dict) and "transaction" in storage:
        raise ShardError("staged state must not contain a transaction")


def _install(root: Path, name: str, meta: dict[str, Any], *, transaction: dict[str, Any] | None = None) -> None:
    final = root / f"{name}.json"
    if _matches(final, meta):
        return
    before = meta["before"]
    if transaction is not None:
        if not _journal_matches(final, transaction):
            raise ShardError("state transaction journal changed before recovery")
    elif before is None:
        if final.exists() or final.is_symlink():
            raise ShardError(f"unexpected stable shard during recovery: {name}")
    elif not _matches(final, before):
        raise ShardError(f"stable shard changed during recovery: {name}")
    temporary = root / meta["temporary"]
    if not _matches(temporary, meta):
        raise ShardError(f"cannot recover incomplete shard transaction: {name}")
    os.replace(temporary, final)


def _encode_all(value: dict[str, Any], grouped: dict[str, dict[str, Any]], owners: dict[str, str]) -> dict[str, bytes]:
    encoded = {
        name: _encode({"format": SIDE_FORMAT, "name": name, "records": grouped[name]}, limit=False)
        for name in NAMES[:-1]
    }
    encoded["state"] = _encode_state(value, grouped["state"], owners, encoded)
    return encoded


def _encode_state(
    value: dict[str, Any], records: dict[str, Any], owners: dict[str, str], sidecars: dict[str, bytes]
) -> bytes:
    metadata = {
        name: {
            "file": f"{name}.json",
            "size": len(sidecars[name]),
            "sha256": hashlib.sha256(sidecars[name]).hexdigest(),
        }
        for name in NAMES[:-1]
    }
    return _encode_state_with_metadata(value, records, owners, metadata)


def _encode_state_with_metadata(
    value: dict[str, Any], records: dict[str, Any], owners: dict[str, str], metadata: dict[str, Any]
) -> bytes:
    state = {key: item for key, item in value.items() if key != "records"}
    state["records"] = records
    state["storage"] = {
        "format": FORMAT,
        "owners": owners,
        "shards": metadata,
    }
    return _encode(state, limit=False)


def _rebalance(
    value: dict[str, Any],
    grouped: dict[str, dict[str, Any]],
    owners: dict[str, str],
    encoded: dict[str, bytes],
) -> None:
    while True:
        oversized = [name for name in NAMES if len(encoded[name]) >= MAX_BYTES]
        if not oversized:
            return
        source = max(oversized, key=lambda name: len(encoded[name]))
        candidates = sorted(
            grouped[source], key=lambda key: len(_encode(grouped[source][key], limit=False)), reverse=True
        )
        moved = False
        for key in candidates:
            for target in sorted((name for name in NAMES if name != source), key=lambda name: len(encoded[name])):
                record = grouped[source].pop(key)
                grouped[target][key] = record
                owners[key] = target
                trial = _encode_all(value, grouped, owners)
                if len(trial[target]) < MAX_BYTES and len(trial[source]) < len(encoded[source]):
                    encoded.clear()
                    encoded.update(trial)
                    moved = True
                    break
                del grouped[target][key]
                grouped[source][key] = record
                owners[key] = source
            if moved:
                break
        if not moved:
            raise ShardError(f"records cannot fit below {MAX_BYTES} bytes")


def _group(records: dict[str, Any], owners: dict[str, str]) -> dict[str, dict[str, Any]]:
    grouped = {name: {} for name in NAMES}
    for key, record in records.items():
        name = owners[key]
        if name not in grouped:
            raise ShardError(f"invalid shard owner for {key!r}")
        grouped[name][key] = record
    return grouped


def _stage(root: Path, name: str, data: bytes) -> Path:
    descriptor, filename = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=root)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _replace(path: Path, data: bytes) -> None:
    temporary = _stage(path.parent, path.stem, data)
    try:
        os.replace(temporary, path)
        _sync(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ShardError(f"{path.name} must be a regular file")
    raw = path.read_bytes()
    if len(raw) >= MAX_BYTES:
        raise ShardError(f"{path.name} exceeds {MAX_BYTES} bytes")
    return raw


def _read_json(path: Path, object_hook) -> dict[str, Any]:
    return _loads(path.read_bytes(), path.name, object_hook)


def _loads(raw: bytes, name: str, object_hook) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=object_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShardError(f"invalid {name}: {error}") from error
    if not isinstance(value, dict):
        raise ShardError(f"{name} must contain an object")
    return value


def _encode(value: Any, *, limit: bool = True) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if limit and len(encoded) >= MAX_BYTES:
        raise ShardError(f"JSON exceeds shard limit of {MAX_BYTES} bytes")
    return encoded


def _validate_storage(value: Any) -> None:
    if not isinstance(value, dict) or set(value) not in (
        {"format", "owners", "shards"},
        {"format", "owners", "shards", "transaction"},
    ):
        raise ShardError("state.json has an invalid storage schema")
    if value["format"] != FORMAT or not isinstance(value["owners"], dict):
        raise ShardError("state.json has an invalid storage format")
    shards = value["shards"]
    if not isinstance(shards, dict) or set(shards) != set(NAMES[:-1]):
        raise ShardError("state.json has an invalid shard index")
    for name, meta in shards.items():
        if not isinstance(meta, dict) or set(meta) != {"file", "size", "sha256"}:
            raise ShardError(f"invalid shard metadata: {name}")
        if meta["file"] != f"{name}.json" or type(meta["size"]) is not int or meta["size"] < 0:
            raise ShardError(f"invalid shard metadata: {name}")
        if not _hash(meta["sha256"]):
            raise ShardError(f"invalid shard metadata: {name}")
    if "transaction" in value:
        _validate_transaction(value["transaction"])


def _validate_transaction(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"format", "files"} or value["format"] != TRANSACTION_FORMAT:
        raise ShardError("state.json has an invalid shard transaction")
    files = value["files"]
    if not isinstance(files, dict) or "state" not in files or not set(files).issubset(NAMES):
        raise ShardError("state.json has an invalid shard transaction")
    for name, meta in files.items():
        if not isinstance(meta, dict) or set(meta) != {"temporary", "size", "sha256", "before"}:
            raise ShardError(f"invalid transaction metadata: {name}")
        temporary = meta["temporary"]
        if (
            not isinstance(temporary, str)
            or Path(temporary).name != temporary
            or not temporary.startswith(f".{name}.")
            or not temporary.endswith(".tmp")
        ):
            raise ShardError(f"invalid transaction path: {name}")
        if type(meta["size"]) is not int or meta["size"] < 0 or meta["size"] >= MAX_BYTES or not _hash(meta["sha256"]):
            raise ShardError(f"invalid transaction metadata: {name}")
        before = meta["before"]
        if before is not None and (
            not isinstance(before, dict)
            or set(before) != {"size", "sha256"}
            or type(before["size"]) is not int
            or before["size"] < 0
            or not _hash(before["sha256"])
        ):
            raise ShardError(f"invalid prior shard metadata: {name}")


def _matches(path: Path, meta: dict[str, Any]) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != meta["size"]:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == meta["sha256"]


def _metadata(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise ShardError(f"{path.name} must be a regular file")
    if not path.exists():
        return None
    if not path.is_file():
        raise ShardError(f"{path.name} must be a regular file")
    raw = path.read_bytes()
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _journal_matches(path: Path, transaction: dict[str, Any]) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        value = _read_json(path, _unique)
    except (OSError, ShardError):
        return False
    storage = value.get("storage")
    return isinstance(storage, dict) and storage.get("transaction") == transaction


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _signature(path: Path) -> tuple[str, int, int, int, int]:
    if path.is_symlink() or not path.is_file():
        raise ShardError(f"{path.name} must be a regular file")
    stat = path.stat()
    return (path.name, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _sync(root: Path) -> None:
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ShardError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


__all__ = [
    "FILES",
    "FORMAT",
    "MAX_BYTES",
    "NAMES",
    "ShardError",
    "cleanup",
    "commit",
    "initialize",
    "load",
    "owner",
    "physical",
    "signatures",
]
