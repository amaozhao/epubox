from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from engine.services import state
from engine.services.atomic import AtomicStore, StoreLocked


def _try_lock(root: str, queue: multiprocessing.Queue[bool]) -> None:
    try:
        with AtomicStore(root).lock(blocking=False):
            queue.put(True)
    except StoreLocked:
        queue.put(False)


def _compact(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    root = tmp_path / "book"
    state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run-1")
    return root, source


def test_compact_store_keeps_only_source_and_state_and_round_trips_exact_bytes(tmp_path: Path) -> None:
    root, source = _compact(tmp_path)
    payload = b'{"z": 1, "a": "\xe4\xb8\xad"}\n'
    path = root / "documents" / "d1.json"

    AtomicStore(root, directories=("documents", "units")).atomic_write_bytes(path, payload)

    assert state.compact(root)
    assert state.snapshot(root) == source
    assert state.artifact(path) == root / "state.json"
    assert state.read(path) == payload
    state._cache.clear()
    assert state.read(path) == payload
    assert state.text(path) == payload.decode()
    assert {item.name for item in root.iterdir()} == {"source", "state.json"}


def test_compact_filesystem_helpers_expose_logical_records(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    first = root / "units" / "u1.json"
    second = root / "units" / "u2.json"
    state.write(first, b"one")
    state.write(second, b"two")

    assert state.exists(first)
    assert state.is_file(first)
    assert state.is_dir(root / "units")
    assert list(state.glob(root / "units", "*.json")) == [first, second]
    assert list(state.glob(root, "**/*.json")) == [first, second]
    with pytest.raises(FileNotFoundError):
        state.read(root / "units" / "missing.json")
    state.unlink(first)
    assert not state.exists(first)
    state.unlink(first, missing_ok=True)


def test_record_stat_is_stable_when_an_unrelated_record_changes(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    first = root / "units" / "u1.json"
    second = root / "units" / "u2.json"
    state.write(first, b"one")
    before = state.stat(first)

    state.write(second, b"two")

    assert state.stat(first) == before
    assert before.st_size == 3
    state.write(first, b"changed")
    assert state.stat(first).st_mtime_ns > before.st_mtime_ns


def test_external_state_change_refreshes_the_cache(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    path = root / "units" / "u1.json"
    state.write(path, b"before")
    assert state.read(path) == b"before"
    physical = root / "state.json"
    value = json.loads(physical.read_text())
    value["records"]["units/u1.json"]["data"] = "after!"
    physical.write_text(json.dumps(value), encoding="utf-8")

    assert state.read(path) == b"after!"


def test_nested_store_instances_share_one_reentrant_lock(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    outer = AtomicStore(root)
    inner = AtomicStore(root)

    with outer.lock(blocking=False), inner.lock(blocking=False):
        state.write(root / "report.json", b"{}")

    assert state.read(root / "report.json") == b"{}"
    assert not (root / ".store.lock").exists()


def test_compact_lock_excludes_another_process(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[bool] = context.Queue()

    with state.batch(root):
        process = context.Process(target=_try_lock, args=(str(root), queue))
        process.start()
        process.join(timeout=5)

    assert process.exitcode == 0
    assert queue.get(timeout=1) is False


def test_import_records_preserves_bytes_without_deleting_legacy(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    legacy = tmp_path / "legacy"
    (legacy / "requests").mkdir(parents=True)
    payload = b'{ "request": true }\n'
    old = legacy / "requests" / "r1.json"
    old.write_bytes(payload)
    (legacy / "source.epub").write_bytes(b"duplicate")

    state.import_records(root, legacy)

    assert state.read(root / "requests" / "r1.json") == payload
    assert old.read_bytes() == payload
    assert not state.exists(root / "source.epub")


def test_initialize_migrates_legacy_records_in_the_header_commit(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    legacy = tmp_path / "legacy"
    (legacy / "units").mkdir(parents=True)
    payload = b'{ "unit": 1 }\n'
    (legacy / "units" / "u1.json").write_bytes(payload)
    root = tmp_path / "book"

    state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run-1", legacy)

    assert state.read(root / "units" / "u1.json") == payload
    assert (legacy / "units" / "u1.json").read_bytes() == payload
    assert {item.name for item in root.iterdir()} == {"source", "state.json"}


def test_initialize_recovers_an_existing_empty_header_from_legacy(tmp_path: Path) -> None:
    root, source = _compact(tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "report.json").write_bytes(b"report")

    state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run-1", legacy)

    assert state.read(root / "report.json") == b"report"


def test_failed_migration_leaves_no_header_and_preserves_legacy(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    record = legacy / "report.json"
    record.write_bytes(b"report")
    root = tmp_path / "book"
    monkeypatch.setattr(state, "_commit", lambda *_args: (_ for _ in ()).throw(OSError("stop")))

    with pytest.raises(OSError, match="stop"):
        state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run-1", legacy)

    assert not (root / "state.json").exists()
    assert record.read_bytes() == b"report"


def test_initialize_rejects_unrelated_preexisting_book_directory(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    root = tmp_path / "book"
    root.mkdir()
    notes = root / "notes.txt"
    notes.write_bytes(b"user notes")
    with pytest.raises(state.StateError, match="unrelated files"):
        state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run")
    assert notes.read_bytes() == b"user notes"
    assert {path.name for path in root.iterdir()} == {"notes.txt"}


def test_migration_rejects_internal_symlink_without_commit_or_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    record = legacy / "report.json"
    record.write_bytes(b"report")
    alias = legacy / "link.json"
    alias.symlink_to(record)
    root = tmp_path / "book"
    with pytest.raises(state.StateError, match="unsupported symbolic link"):
        state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run", legacy)
    assert alias.is_symlink() and record.read_bytes() == b"report"
    assert not (root / "state.json").exists()


def test_migration_rejects_legacy_parent_sibling_without_adopting_root(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    root = tmp_path / "book"
    legacy = root / "hash" / "run"
    legacy.mkdir(parents=True)
    record = legacy / "report.json"
    record.write_bytes(b"report")
    sibling = legacy.parent / "notes.txt"
    sibling.write_bytes(b"user notes")
    with pytest.raises(state.StateError, match="legacy parent contains unrelated"):
        state.initialize(root, source, hashlib.sha256(b"epub").hexdigest(), "run", legacy)
    assert sibling.read_bytes() == b"user notes" and record.read_bytes() == b"report"
    assert not (root / "state.json").exists() and not (root / "source").exists()


def test_batch_commits_many_writes_once_and_exposes_pending_records(tmp_path: Path, monkeypatch) -> None:
    root, _ = _compact(tmp_path)
    commits = 0
    original = state._commit

    def counted(*args):
        nonlocal commits
        commits += 1
        return original(*args)

    monkeypatch.setattr(state, "_commit", counted)
    with state.batch(root):
        state.write(root / "results" / "one.json", b"one")
        state.write(root / "results" / "two.json", b"two")
        assert state.read(root / "results" / "one.json") == b"one"
        assert state.stat(root / "results" / "two.json").st_size == 3
        assert list(state.glob(root / "results", "*.json")) == [
            root / "results" / "one.json",
            root / "results" / "two.json",
        ]

    assert commits == 1
    assert state.read(root / "results" / "two.json") == b"two"


def test_batch_exception_restores_committed_records_cache_and_stamps(tmp_path: Path, monkeypatch) -> None:
    root, _ = _compact(tmp_path)
    path = root / "results" / "one.json"
    state.write(path, b"before")
    before = state.stat(path)
    commits = 0
    original = state._commit

    def counted(*args):
        nonlocal commits
        commits += 1
        return original(*args)

    monkeypatch.setattr(state, "_commit", counted)
    with pytest.raises(RuntimeError, match="abort"), state.batch(root):
        state.write(path, b"pending")
        state.write(root / "results" / "two.json", b"two")
        assert state.read(path) == b"pending"
        raise RuntimeError("abort")

    assert commits == 0
    assert state.read(path) == b"before"
    assert state.stat(path) == before
    assert not state.exists(root / "results" / "two.json")


def test_reset_records_is_one_compact_commit_and_preserves_header(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    state.write(root / "report.json", b"report")
    state.write(root / "units" / "u1.json", b"unit")
    before = state.header(root)
    assert list(state.glob(root, "**/*")) == [root / "report.json", root / "units" / "u1.json"]

    state.reset_records(root)

    assert state.header(root) == before
    assert list(state.glob(root, "**/*")) == []
    assert {item.name for item in root.iterdir()} == {"source", "state.json"}


def test_header_rejects_tampered_schema(tmp_path: Path) -> None:
    root, source = _compact(tmp_path)
    assert state.header(root)["source"]["path"] == str(source)
    physical = root / "state.json"
    value = json.loads(physical.read_text())
    value["unexpected"] = True
    physical.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(state.StateError, match="root schema"):
        state.header(root)


def test_compact_record_can_exceed_32_megabytes(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    payload = b"x" * (32 * 1024 * 1024 + 1)
    path = root / "responses" / "large.json"

    state.write(path, payload)

    assert state.read(path) == payload


def test_logical_symlink_cannot_redirect_a_compact_write(tmp_path: Path) -> None:
    root, _ = _compact(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "units").symlink_to(outside, target_is_directory=True)

    state.write(root / "units" / "u1.json", b"safe")

    assert state.read(root / "units" / "u1.json") == b"safe"
    assert not (outside / "u1.json").exists()


def test_legacy_paths_keep_native_filesystem_behavior(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    path = root / "records" / "one.txt"
    with state.batch(root):
        state.write(path, b"one")

    assert not state.compact(root)
    assert state.read(path) == b"one"
    assert list(state.glob(root / "records", "*.txt")) == [path]
    assert state.snapshot(root) == root.resolve() / "source.epub"
    assert state.artifact(path) == path
