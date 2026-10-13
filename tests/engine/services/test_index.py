import asyncio
import json
import os

import pytest

from engine.services import shards, state, stream
from tests.engine.services.test_fingerprint import compact


def replace_state(root):
    physical = root / "state.json"
    value = json.loads(physical.read_bytes())
    value["records"]["report.json"]["data"] = "after!"
    temporary = root / "replacement.json"
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    os.replace(temporary, physical)


def test_reload_cannot_cache_old_offsets_with_new_file_signature(tmp_path, monkeypatch):
    root = compact(tmp_path)
    state.write(root / "report.json", b"before")
    original = shards.indexed_object
    replaced = False

    def interleaved(path, object_hook):
        nonlocal replaced
        result = original(path, object_hook)
        if path.name == "state.json" and not replaced:
            replaced = True
            replace_state(root)
        return result

    monkeypatch.setattr(shards, "indexed_object", interleaved)
    state._refresh(root)

    assert state.read(root / "report.json") == b"after!"
    assert state.read(root / "report.json") == b"after!"


@pytest.mark.parametrize("reader,expected", [(state.read, b"after!"), (state.text, "after!")])
def test_read_recovers_if_replacement_happens_after_cache_lookup(tmp_path, monkeypatch, reader, expected):
    root = compact(tmp_path)
    logical = root / "report.json"
    state.write(logical, b"before")
    original = stream.RecordRef.read
    replaced = False

    def interleaved(ref, object_hook):
        nonlocal replaced
        if ref.path.name == "state.json" and not replaced:
            replaced = True
            replace_state(root)
        return original(ref, object_hook)

    monkeypatch.setattr(stream.RecordRef, "read", interleaved)
    assert reader(logical) == expected


def test_active_batch_does_not_merge_a_replaced_base_snapshot(tmp_path, monkeypatch):
    root = compact(tmp_path)
    logical = root / "report.json"
    state.write(logical, b"before")
    with pytest.raises(stream.StreamJSONError, match="changed after"), state.batch(root):
        state.write(root / "pending.json", b"uncommitted")
        replace_state(root)
        state.read(logical)
    assert state.read(logical) == b"after!"
    assert not state.exists(root / "pending.json")


def test_index_rejects_replacement_during_parsing(tmp_path, monkeypatch):
    root = compact(tmp_path)
    state.write(root / "report.json", b"before")
    original = stream._indexed_root

    def interleaved(*args):
        result = original(*args)
        replace_state(root)
        return result

    monkeypatch.setattr(stream, "_indexed_root", interleaved)
    with pytest.raises(stream.StaleRecordIndex, match="while.*index"):
        stream.indexed_object(root / "state.json", state._object)


def test_repeated_replacement_stops_after_bounded_attempts(tmp_path, monkeypatch):
    root = compact(tmp_path)
    state.write(root / "report.json", b"before")
    original = shards.indexed_object
    calls = 0

    def interleaved(path, object_hook):
        nonlocal calls
        result = original(path, object_hook)
        if path.name == "state.json":
            calls += 1
            replace_state(root)
        return result

    monkeypatch.setattr(shards, "indexed_object", interleaved)
    with pytest.raises(stream.StaleRecordIndex):
        state._refresh(root)
    assert calls == 3
    assert root not in state._cache


def test_commit_refresh_failure_drops_old_cache_and_next_read_recovers(tmp_path, monkeypatch):
    root = compact(tmp_path)
    logical = root / "report.json"
    state.write(logical, b"before")

    def fail_refresh(*args, **kwargs):
        raise stream.StaleRecordIndex("injected index refresh failure")

    monkeypatch.setattr(shards, "reload_after_commit", fail_refresh)
    with pytest.raises(stream.StaleRecordIndex):
        state.write(logical, b"after!")
    assert root not in state._cache
    assert state.read(logical) == b"after!"


def test_reload_rejects_corruption_instead_of_hiding_it(tmp_path, monkeypatch):
    root = compact(tmp_path)
    logical = root / "documents" / "d.json"
    state.write(logical, b"before")
    original = stream.RecordRef.read
    replaced = False

    def interleaved(ref, object_hook):
        nonlocal replaced
        if ref.path.name == "origin.json" and not replaced:
            replaced = True
            ref.path.write_bytes(ref.path.read_bytes().replace(b"before", b"after!"))
        return original(ref, object_hook)

    monkeypatch.setattr(stream.RecordRef, "read", interleaved)
    with pytest.raises(state.StateError, match="does not match state.json"):
        state.read(logical)


@pytest.mark.parametrize("during_index", [False, True])
def test_metadata_only_change_during_commit_preserves_records(tmp_path, monkeypatch, during_index):
    root = compact(tmp_path)
    logical = root / "report.json"
    state.write(logical, b"before")
    before = (root / "state.json").read_bytes()
    original = stream._indexed_root

    def metadata_update(*args):
        result = original(*args)
        if args[1].name == "state.json":
            physical = args[1]
            physical.chmod(physical.stat().st_mode ^ 0o100)
        return result

    if during_index:
        monkeypatch.setattr(stream, "_indexed_root", metadata_update)
    with state.batch(root):
        if not during_index:
            physical = root / "state.json"
            physical.chmod(physical.stat().st_mode ^ 0o100)
        assert (root / "state.json").read_bytes() == before
        state.write(logical, b"after!")
        state.write(root / "results" / "unit.json", b"accepted")
    assert state.read(logical) == b"after!"
    assert state.read(root / "results" / "unit.json") == b"accepted"


def test_same_size_content_edit_with_restored_mtime_is_still_rejected(tmp_path):
    root = compact(tmp_path)
    logical = root / "report.json"
    state.write(logical, b"before")
    records = state._load(root)["records"]
    ref = records.raw("report.json")
    physical = root / "state.json"
    original = physical.stat()
    physical.write_bytes(physical.read_bytes().replace(b"before", b"after!"))
    os.utime(physical, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(stream.StaleRecordIndex):
        ref.read(state._object)


def test_full_workflow_survives_metadata_updates_while_saving_and_resuming(tmp_path, monkeypatch):
    from engine.orchestrator import run_translation
    from tests.engine.execution.test_atomic import answer
    from tests.engine.services.test_journal import _compact_case

    case = _compact_case(tmp_path)
    original = stream._indexed_root
    changes = 0
    calls = []

    def metadata_update(*args):
        nonlocal changes
        result = original(*args)
        if args[1].name == "state.json":
            changes += 1
            args[1].chmod(args[1].stat().st_mode ^ 0o100)
        return result

    async def transport(kind, payload):
        calls.append(kind)
        return answer(kind, payload)

    monkeypatch.setattr(shards, "STREAM_THRESHOLD", 1)
    monkeypatch.setattr(stream, "_indexed_root", metadata_update)
    first = asyncio.run(run_translation(case.session.store.root, transport=transport))
    assert first.status == "translated", first.reason
    assert "translate" in calls and "review" in calls
    before = len(calls)
    state._refresh(case.session.store.root)
    second = asyncio.run(run_translation(case.session.store.root, transport=transport))
    assert second.status == "translated", second.reason
    assert second.accepted_units == first.accepted_units
    assert second.http_attempts == first.http_attempts
    assert len(calls) == before
    assert changes > 10


@pytest.mark.parametrize("directory,physical_name", [("documents", "origin"), ("results", "state")])
def test_stream_commit_rejects_changes_after_copying_source(tmp_path, monkeypatch, directory, physical_name):
    root = compact(tmp_path)
    state.write(root / directory / "a.json", b"old-a")
    state.write(root / directory / "b.json", b"old-b")
    monkeypatch.setattr(shards, "STREAM_THRESHOLD", 1)
    original = shards._stream_stage

    def interleaved(*args, **kwargs):
        staged = original(*args, **kwargs)
        if args[1] == "state":
            physical = root / f"{physical_name}.json"
            physical.write_bytes(physical.read_bytes().replace(b"old-b", b"NEW-b"))
        return staged

    monkeypatch.setattr(shards, "_stream_stage", interleaved)
    with pytest.raises((state.StateError, stream.StaleRecordIndex)):
        state.write(root / directory / "a.json", b"new-a")
    data = json.loads((root / f"{physical_name}.json").read_bytes())["records"]
    assert data[f"{directory}/a.json"]["data"] == "old-a"
    assert data[f"{directory}/b.json"]["data"] == "NEW-b"
    assert not list(root.glob(".*.tmp"))
