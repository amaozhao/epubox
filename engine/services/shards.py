"""Five-file storage for compact translation sessions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from engine.services.stream import IndexedRecords, StaleRecordIndex, StreamJSONError, indexed_object, sha256_file

FORMAT = "epubox-shards-1"
SIDE_FORMAT = "epubox-shard-1"
TRANSACTION_FORMAT = "epubox-shard-transaction-1"
NAMES = ("origin", "mapping", "plan", "terms", "state")
FILES = tuple(f"{name}.json" for name in NAMES)
MAX_BYTES = 400_000_000
STREAM_THRESHOLD = 1_000_000


class ShardError(RuntimeError):
    pass


class _NeedsRebalance(RuntimeError):
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
    if not all((root / name).is_file() and not (root / name).is_symlink() for name in FILES):
        state = _read_json(state_path, object_hook)
        if state.get("storage") is None:
            return state, {}, (_signature(state_path),)
    try:
        state, state_records = indexed_object(state_path, object_hook)
    except StaleRecordIndex:
        raise
    except StreamJSONError as error:
        # Keep compatibility with small externally reformatted checkpoints,
        # without turning corruption of a production-sized shard set into an
        # unbounded eager parse.
        if str(error).startswith("non-canonical record") and _small_fallback(root):
            return _load_eager(root, object_hook)
        raise ShardError(str(error)) from error
    storage = state.get("storage") if isinstance(state, dict) else None
    if storage is None:
        # Legacy single-file checkpoints are uncommon and retain their exact
        # eager-loading behavior until they are migrated to the five shards.
        return _read_json(state_path, object_hook), {}, (_signature(state_path),)
    _validate_storage(storage)
    if "transaction" in storage:
        _recover(root, state, object_hook)
        try:
            state, state_records = indexed_object(state_path, object_hook)
        except StaleRecordIndex:
            raise
        except StreamJSONError as error:
            raise ShardError(str(error)) from error
        storage = state.get("storage")
        _validate_storage(storage)
    elif state_path.stat().st_size >= MAX_BYTES:
        raise ShardError(f"state.json exceeds {MAX_BYTES} bytes")
    if not isinstance(storage, dict):
        raise ShardError("state.json has an invalid storage schema")
    owners = storage["owners"]
    refs = state_records.refs()
    snapshots = dict(state_records.snapshots)
    locations = {key: "state" for key in refs}
    for name in NAMES[:-1]:
        meta = storage["shards"][name]
        path = root / meta["file"]
        if path.stat().st_size != meta["size"]:
            raise ShardError(f"{path.name} does not match state.json")
        try:
            value, shard_records = indexed_object(path, object_hook)
        except StaleRecordIndex:
            raise
        except StreamJSONError as error:
            raise ShardError(str(error)) from error
        if not isinstance(value, dict) or set(value) != {"format", "name", "records"}:
            raise ShardError(f"{path.name} has an invalid root schema")
        if value["format"] != SIDE_FORMAT or value["name"] != name or not isinstance(value["records"], Mapping):
            raise ShardError(f"{path.name} has an invalid shard identity")
        overlap = locations.keys() & shard_records.keys()
        if overlap:
            raise ShardError(f"duplicate logical record across shards: {min(overlap)}")
        refs.update(shard_records.refs())
        locations.update({key: name for key in shard_records})
        if shard_records.snapshots[path].digest != meta["sha256"]:
            raise ShardError(f"{path.name} does not match state.json")
        snapshots.update(shard_records.snapshots)
    signatures = {path.name: snapshot.check_path() for path, snapshot in snapshots.items()}
    if set(owners) != set(refs):
        raise ShardError("state.json owners do not match stored records")
    for key, name in owners.items():
        if name not in NAMES:
            raise ShardError(f"invalid shard owner for {key!r}")
        if locations.get(key) != name:
            raise ShardError(f"record is stored in the wrong shard: {key!r}")
    merged = dict(state)
    merged.pop("storage", None)
    merged["records"] = IndexedRecords(refs, object_hook, snapshots=snapshots)
    return merged, dict(owners), tuple(signatures[name] for name in FILES)


def _load_eager(
    root: Path, object_hook
) -> tuple[dict[str, Any], dict[str, str], tuple[tuple[str, int, int, int, int], ...]]:
    """Compatibility path for legacy or externally reformatted checkpoints."""
    state_path = root / "state.json"
    state = _read_json(state_path, object_hook)
    storage = state.get("storage") if isinstance(state, dict) else None
    if storage is None:
        return state, {}, (_signature(state_path),)
    _validate_storage(storage)
    records = dict(state.get("records", {}))
    _validate_eager_records(records, state_path.name)
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
        _validate_eager_records(value["records"], path.name)
        overlap = locations.keys() & value["records"].keys()
        if overlap:
            raise ShardError(f"duplicate logical record across shards: {min(overlap)}")
        records.update(value["records"])
        locations.update({key: name for key in value["records"]})
        signatures[path.name] = _signature(path)
    owners = storage["owners"]
    if set(owners) != set(records):
        raise ShardError("state.json owners do not match stored records")
    for key, name in owners.items():
        if name not in NAMES or locations.get(key) != name:
            raise ShardError(f"record is stored in the wrong shard: {key!r}")
    merged = dict(state)
    merged.pop("storage", None)
    merged["records"] = records
    return merged, dict(owners), tuple(signatures[name] for name in FILES)


def _small_fallback(root: Path) -> bool:
    """Allow eager compatibility only when the complete checkpoint is small."""
    total = 0
    for filename in FILES:
        path = root / filename
        if path.is_symlink() or not path.is_file():
            return False
        total += path.stat().st_size
        if total >= STREAM_THRESHOLD:
            return False
    return True


def _validate_eager_records(records: Any, name: str) -> None:
    if not isinstance(records, dict):
        raise ShardError(f"{name} records must contain an object")
    for key, record in records.items():
        try:
            key.encode("utf-8")
        except (AttributeError, UnicodeEncodeError) as error:
            raise ShardError(f"invalid record key in {name}") from error
        if (
            not isinstance(record, dict)
            or set(record) != {"data", "stamp"}
            or not isinstance(record["data"], str)
            or type(record["stamp"]) is not int
            or record["stamp"] < 0
        ):
            raise ShardError(f"invalid record in {name}: {key!r}")
        try:
            record["data"].encode("utf-8")
        except UnicodeEncodeError as error:
            raise ShardError(f"invalid record payload in {name}: {key!r}") from error


def commit(
    root: Path,
    value: dict[str, Any],
    previous: dict[str, str] | None = None,
    before: dict[str, Any] | None = None,
    *,
    changed: set[str] | None = None,
) -> dict[str, str]:
    changed_keys = changed
    previous = previous or {}
    records = value["records"]
    owners = {key: previous.get(key, owner(key)) for key in records}
    sharded = all((root / name).is_file() and not (root / name).is_symlink() for name in FILES)
    if sharded:
        try:
            stored_state, _ = indexed_object(root / "state.json", _unique)
        except StreamJSONError as error:
            raise ShardError(str(error)) from error
        storage = stored_state.get("storage")
    else:
        storage = None
    if (
        storage is not None
        and before is not None
        and changed_keys is not None
        and any((root / name).stat().st_size >= STREAM_THRESHOLD for name in FILES)
    ):
        try:
            return _stream_commit(root, value, owners, previous, storage, changed_keys)
        except _NeedsRebalance:
            # Rebalancing is rare and changes ownership across every shard. Keep
            # the established whole-record algorithm as a compatibility fallback;
            # normal updates remain bounded and streaming.
            pass
    grouped = _group(records, owners)
    if storage is None or before is None:
        encoded = _encode_all(value, grouped, owners)
        _rebalance(value, grouped, owners, encoded)
        changed_names: set[str] = set(NAMES)
    else:
        if changed_keys is not None:
            changed_names = {
                name for key in changed_keys for name in {previous.get(key, owner(key)), owners.get(key, owner(key))}
            }
        else:
            changed_names = {
                name
                for key in set(before["records"]) | set(records)
                for name in {previous.get(key, owner(key)), owners.get(key, owner(key))}
                if before["records"].get(key) != records.get(key)
            }
        sidecars = {
            name: _encode({"format": SIDE_FORMAT, "name": name, "records": grouped[name]}, limit=False)
            for name in changed_names - {"state"}
        }
        if any(len(data) >= MAX_BYTES for data in sidecars.values()):
            encoded = _encode_all(value, grouped, owners)
            _rebalance(value, grouped, owners, encoded)
            changed_names = set(NAMES)
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
                changed_names = set(NAMES)
            else:
                changed_names.add("state")
    if not changed_names:
        return owners
    if changed_names == {"state"}:
        _replace(root / "state.json", encoded["state"])
        return owners
    _transaction(root, value, encoded, changed_names)
    return owners


def _stream_commit(
    root: Path,
    value: dict[str, Any],
    owners: dict[str, str],
    previous: dict[str, str],
    storage: dict[str, Any],
    changed_keys: set[str],
) -> dict[str, str]:
    """Rewrite large affected shards without constructing shard-sized bytes/objects."""
    records = value["records"]
    changed_names = {
        name for key in changed_keys for name in {previous.get(key, owner(key)), owners.get(key, owner(key))}
    }
    if not changed_names:
        return owners
    snapshots = getattr(getattr(records, "base", records), "snapshots", {})
    before = {
        name: {
            "size": snapshots[root / f"{name}.json"].signature[1],
            "sha256": snapshots[root / f"{name}.json"].digest,
        }
        if root / f"{name}.json" in snapshots
        else _metadata(root / f"{name}.json")
        for name in NAMES
    }
    staged: dict[str, Path] = {}
    staged_meta: dict[str, dict[str, Any]] = {}
    try:
        metadata = dict(storage["shards"])
        for name in sorted(changed_names - {"state"}):
            path = _stream_stage(root, name, lambda handle, shard=name: _write_sidecar(handle, shard, records, owners))
            staged[name] = path
            staged_meta[name] = _staged_metadata(path)
            if staged_meta[name]["size"] >= MAX_BYTES:
                raise _NeedsRebalance
            metadata[name] = {
                "file": f"{name}.json",
                "size": staged_meta[name]["size"],
                "sha256": staged_meta[name]["sha256"],
            }
        state_path = _stream_stage(
            root,
            "state",
            lambda handle: _write_state(handle, value, records, owners, metadata),
        )
        staged["state"] = state_path
        staged_meta["state"] = _staged_metadata(state_path)
        if staged_meta["state"]["size"] >= MAX_BYTES:
            raise _NeedsRebalance
        changed_names.add("state")
        for snapshot in snapshots.values():
            snapshot.check_path()
        if changed_names == {"state"}:
            prior_state = before["state"]
            if prior_state is None or not _matches(root / "state.json", prior_state):
                raise ShardError("stable shard changed before transaction: state")
            os.replace(state_path, root / "state.json")
            staged.pop("state")
            _sync(root)
            return owners
        _transaction_staged(root, value, staged, staged_meta, changed_names, metadata, before, snapshots)
        staged.clear()
        return owners
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)


def _transaction_staged(
    root: Path,
    value: dict[str, Any],
    staged: dict[str, Path],
    staged_meta: dict[str, dict[str, Any]],
    changed: set[str],
    metadata: dict[str, Any],
    before: dict[str, Any],
    snapshots: Mapping[Path, Any],
) -> None:
    transaction = {
        "format": TRANSACTION_FORMAT,
        "files": {
            name: {
                "temporary": staged[name].name,
                "size": staged_meta[name]["size"],
                "sha256": staged_meta[name]["sha256"],
                "before": before[name],
            }
            for name in changed
        },
    }
    journal = {key: value[key] for key in ("format", "source", "run_id")}
    journal["records"] = {}
    journal["storage"] = {
        "format": FORMAT,
        "owners": {},
        "shards": metadata,
        "transaction": transaction,
    }
    try:
        for snapshot in snapshots.values():
            snapshot.check_path()
        for name, meta in transaction["files"].items():
            before_meta = meta["before"]
            final_path = root / f"{name}.json"
            if before_meta is None:
                if final_path.exists() or final_path.is_symlink():
                    raise ShardError(f"unexpected stable shard before transaction: {name}")
            elif not _matches(final_path, before_meta):
                raise ShardError(f"stable shard changed before transaction: {name}")
        _replace(root / "state.json", _encode(journal))
    except BaseException:
        if not _journal_matches(root / "state.json", transaction):
            for path in staged.values():
                path.unlink(missing_ok=True)
        else:
            staged.clear()
        raise
    # The journal now owns the staged files. They must survive an interrupted
    # install so the next reader can replay the transaction.
    staged.clear()
    _finish(root, transaction)


def _stream_stage(root: Path, name: str, writer) -> Path:
    descriptor, filename = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=root)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_sidecar(handle, name: str, records: Mapping[str, Any], owners: dict[str, str]) -> None:
    handle.write(b'{"format":"epubox-shard-1","name":')
    handle.write(_encode(name))
    handle.write(b',"records":')
    _write_records(handle, records, (key for key in records if owners[key] == name))
    handle.write(b"}")


def _write_state(
    handle,
    value: dict[str, Any],
    records: Mapping[str, Any],
    owners: dict[str, str],
    metadata: dict[str, Any],
) -> None:
    handle.write(b'{"format":')
    handle.write(_encode(value["format"]))
    handle.write(b',"records":')
    _write_records(handle, records, (key for key in records if owners[key] == "state"))
    handle.write(b',"run_id":')
    handle.write(_encode(value["run_id"]))
    handle.write(b',"source":')
    handle.write(_encode(value["source"]))
    handle.write(b',"storage":{"format":')
    handle.write(_encode(FORMAT))
    handle.write(b',"owners":')
    _write_mapping(handle, owners)
    handle.write(b',"shards":')
    handle.write(_encode(metadata))
    handle.write(b"}}")


def _write_mapping(handle, values: Mapping[str, Any]) -> None:
    handle.write(b"{")
    for index, key in enumerate(sorted(values)):
        if index:
            handle.write(b",")
        handle.write(_encode(key))
        handle.write(b":")
        handle.write(_encode(values[key], limit=False))
    handle.write(b"}")


def _write_records(handle, records: Mapping[str, Any], keys) -> None:
    handle.write(b"{")
    raw_reader = getattr(records, "raw", None)
    for index, key in enumerate(sorted(keys)):
        if index:
            handle.write(b",")
        handle.write(_encode(key))
        handle.write(b":")
        ref = raw_reader(key) if raw_reader is not None else None
        if ref is None:
            handle.write(_encode(records[key], limit=False))
        else:
            ref.copy_to(handle)
    handle.write(b"}")


def _staged_metadata(path: Path) -> dict[str, Any]:
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


def signatures(root: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    names = FILES if all((root / name).is_file() for name in FILES) else ("state.json",)
    return tuple(_signature(root / name) for name in names)


def reload_after_commit(
    root: Path,
    object_hook,
    previous_records: Mapping[str, Any],
    previous_owners: dict[str, str],
    owners: dict[str, str],
    changed_keys: set[str],
    previous_signatures: tuple[tuple[str, int, int, int, int], ...],
) -> tuple[dict[str, Any], tuple[tuple[str, int, int, int, int], ...]]:
    """Refresh only files replaced by our verified commit."""
    if not isinstance(previous_records, IndexedRecords):
        value, _owners, signature = load(root, object_hook)
        return value, signature
    state, state_records = indexed_object(root / "state.json", object_hook)
    snapshots = dict(state_records.snapshots)
    storage = state.get("storage")
    _validate_storage(storage)
    if not isinstance(storage, dict) or "transaction" in storage:
        raise ShardError("committed state still contains a transaction")
    changed_names = {"state"}
    for key in changed_keys:
        changed_names.update({previous_owners.get(key, owner(key)), owners.get(key, owner(key))})
    for key, name in owners.items():
        if previous_owners.get(key, owner(key)) != name:
            changed_names.update({previous_owners.get(key, owner(key)), name})
    prior_refs = previous_records.refs()
    refs = state_records.refs()
    prior_by_name = {item[0]: item for item in previous_signatures}
    for name in NAMES[:-1]:
        if name not in changed_names:
            filename = f"{name}.json"
            snapshot = previous_records.snapshots[root / filename]
            current = snapshot.check_path()
            if prior_by_name[filename][:4] != current[:4]:
                raise ShardError(f"{filename} changed during commit")
            snapshots[root / filename] = snapshot
            refs.update({key: ref for key, ref in prior_refs.items() if owners.get(key) == name})
            continue
        meta = storage["shards"][name]
        path = root / meta["file"]
        if path.stat().st_size != meta["size"]:
            raise ShardError(f"{path.name} does not match state.json")
        value, records = indexed_object(path, object_hook)
        if value.get("format") != SIDE_FORMAT or value.get("name") != name:
            raise ShardError(f"{path.name} has an invalid shard identity")
        if records.snapshots[path].digest != meta["sha256"]:
            raise ShardError(f"{path.name} does not match state.json")
        snapshots.update(records.snapshots)
        refs.update(records.refs())
    if set(refs) != set(owners):
        raise ShardError("state.json owners do not match stored records")
    verified = {path.name: snapshot.check_path() for path, snapshot in snapshots.items()}
    merged = dict(state)
    merged.pop("storage", None)
    merged["records"] = IndexedRecords(refs, object_hook, snapshots=snapshots)
    return merged, tuple(verified[name] for name in FILES)


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
        if not _journal_matches(root / "state.json", transaction):
            for path in staged.values():
                path.unlink(missing_ok=True)
        else:
            staged.clear()
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
    return sha256_file(path) == meta["sha256"]


def _metadata(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise ShardError(f"{path.name} must be a regular file")
    if not path.exists():
        return None
    if not path.is_file():
        raise ShardError(f"{path.name} must be a regular file")
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


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
    "reload_after_commit",
    "signatures",
]
