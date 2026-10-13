from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import weakref
from pathlib import Path
from typing import Any, cast

import pytest

import engine.services.ready as ready_module
from engine.item.members import pack_members
from engine.schemas.contracts import Attempt, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession, limits_for, limits_from_config, read_ready
from engine.services.store import RunStore
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.services.test_preparing import atomic_config


def prepared(tmp_path: Path, **config):
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    result = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(**config), StubChecker()))
    assert result.status == "ready" and result.prepared is not None
    return RunStore(result.work_dir), result.prepared


def compact_prepared(tmp_path: Path):
    legacy, original = prepared(tmp_path)
    root = tmp_path / "compact"
    source = legacy.root / "source.epub"
    state.initialize(root, source, original.preparation.source_hash, original.preparation.run_id, legacy.root)
    return RunStore(root), original


@pytest.mark.parametrize("directory", ("documents", "inventories", "members", "batches"))
def test_ready_rejects_missing_immutable_dependency(tmp_path: Path, directory: str) -> None:
    store, _ = prepared(tmp_path)
    next((store.root / directory).glob("*.json")).unlink()
    with pytest.raises((IdentityMismatch, OSError, RuntimeError)):
        read_ready(store)


def test_ready_rejects_self_consistent_parent_membership_forgery(tmp_path: Path) -> None:
    store, original = prepared(tmp_path)
    data = original.model_dump(mode="python")
    parents = list(data["plan"]["unit_members"])
    assert len(parents) > 1
    first, second = parents[:2]
    data["plan"]["unit_members"][first], data["plan"]["unit_members"][second] = (
        data["plan"]["unit_members"][second],
        data["plan"]["unit_members"][first],
    )
    (store.root / "plans" / "book.json").write_bytes(canonical_json_bytes(data["plan"]))
    (store.root / "prepared.json").write_bytes(canonical_json_bytes(data))
    with pytest.raises(IdentityMismatch, match="parent membership"):
        read_ready(store)


def test_session_caches_full_verification_but_not_changed_dependency(tmp_path: Path, monkeypatch) -> None:
    store, original = prepared(tmp_path)
    session = ReadySession(store)
    calls = 0
    original_read = ready_module._read_ready

    def counted(*args):
        nonlocal calls
        calls += 1
        return original_read(*args)

    monkeypatch.setattr(ready_module, "_read_ready", counted)
    assert session.verify() == original
    assert session.verify() == original
    assert calls == 0
    path = next((store.root / "members").glob("*.json"))
    data = json.loads(path.read_text())
    data["source_projection"] += "Changed."
    path.write_text(json.dumps(data))
    with pytest.raises(IdentityMismatch, match="member"):
        session.verify()
    assert calls == 1


def test_compact_session_ignores_runtime_writes_without_rescanning_dependencies(tmp_path: Path, monkeypatch) -> None:
    store, original = compact_prepared(tmp_path)
    session = ReadySession(store)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime writes must not rescan immutable ready dependencies")

    monkeypatch.setattr(session, "_fingerprint", forbidden)
    monkeypatch.setattr(ready_module, "_read_ready", forbidden)
    state.write(store.root / "report.json", b"{}")

    assert session.verify() == original


def test_compact_verification_survives_runtime_only_reload_during_read(tmp_path: Path, monkeypatch) -> None:
    store, original = compact_prepared(tmp_path)
    state.write(store.root / "report.json", b"{}")
    read = ready_module._read_ready

    def with_external_progress(*args, **kwargs):
        result = read(*args, **kwargs)
        path = store.root / "state.json"
        value = json.loads(path.read_text())
        value["records"]["report.json"]["data"] = '{"progress":1}'
        path.write_text(json.dumps(value))
        return result

    monkeypatch.setattr(ready_module, "_read_ready", with_external_progress)
    session = ReadySession(store)
    assert session.verify() == original


def test_compact_session_rejects_changed_logical_dependency(tmp_path: Path) -> None:
    store, _ = compact_prepared(tmp_path)
    session = ReadySession(store)
    path = next(path for path in state.glob(store.root / "members", "*.json") if b"Translate" in state.read(path))
    data = json.loads(state.text(path))
    data["source_projection"] += "Changed."
    state.write(path, json.dumps(data).encode())

    with pytest.raises(IdentityMismatch, match="member"):
        session.verify()


def test_compact_session_rejects_pending_dependency_change(tmp_path: Path) -> None:
    store, _ = compact_prepared(tmp_path)
    session = ReadySession(store)
    path = next(path for path in state.glob(store.root / "members", "*.json") if b"Translate" in state.read(path))
    data = json.loads(state.text(path))
    data["source_projection"] += "Changed."

    with state.batch(store.root):
        state.write(path, json.dumps(data).encode())
        with pytest.raises(IdentityMismatch, match="member"):
            session.verify()


def test_compact_session_revalidates_consistent_external_tampering(tmp_path: Path, monkeypatch) -> None:
    store, _ = compact_prepared(tmp_path)
    session = ReadySession(store)
    path = next(path for path in state.glob(store.root / "members", "*.json") if b"Translate" in state.read(path))
    key = path.relative_to(store.root).as_posix()
    physical = state.artifact(path)
    shard = json.loads(physical.read_text())
    record = shard["records"][key]
    original_stamp = record["stamp"]
    original_size = len(record["data"].encode())
    record["data"] = record["data"].replace("Translate", "Changedxx")
    assert "Changedxx" in record["data"]
    assert record["stamp"] == original_stamp
    assert len(record["data"].encode()) == original_size
    raw = canonical_json_bytes(shard)
    physical.write_bytes(raw)
    saved = json.loads((store.root / "state.json").read_text())
    metadata = saved["storage"]["shards"][physical.stem]
    metadata.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    (store.root / "state.json").write_bytes(canonical_json_bytes(saved))
    calls = 0
    original = ready_module._read_ready

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ready_module, "_read_ready", counted)
    with pytest.raises(IdentityMismatch, match="member"):
        session.verify()
    assert calls == 1


def test_session_recovery_uses_saved_batches_without_repacking(tmp_path: Path, monkeypatch) -> None:
    import engine.item.members as members_module

    store, original = prepared(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recovery must not rebuild the frozen request plan")

    monkeypatch.setattr(members_module, "pack_members", forbidden)
    session = ReadySession(store)
    assert session.prepared == original
    assert set(session._prepared_batches) == set(original.plan.batch_hashes)


def test_restored_batches_share_verified_source_members(tmp_path: Path) -> None:
    store, original = prepared(tmp_path)
    session = ReadySession(store)
    for request_id, batch in session._prepared_batches.items():
        assert ready_module.canonical_hash(batch) == original.plan.batch_hashes[request_id]
        for member in batch.items:
            assert member is session.index.items_by_id[member.item_id]


def test_base_session_owns_only_the_frozen_unit_member_and_routes_its_result(tmp_path: Path, monkeypatch) -> None:
    store, _ = prepared(tmp_path)
    session = ReadySession(store)
    member = next(iter(session.index.members_by_id.values()))

    assert session.owns(member)
    assert session.result_path(member.item_id) == store.root / "results" / f"{member.item_id}.json"
    assert not session.owns(member.model_copy(update={"unit_id": "foreign-unit"}))
    assert not session.owns(member.model_copy(update={"document_id": "foreign-document"}))
    with pytest.raises(IdentityMismatch, match="unknown ready member"):
        session.result_path("foreign-item")
    batch = next(iter(session._prepared_batches.values()))
    monkeypatch.setattr(session, "owns", lambda _member: False)
    with pytest.raises(IdentityMismatch, match="unprepared member"):
        session.verify_batch(batch)


def test_session_refuses_dependencies_changed_during_initial_verification(tmp_path: Path, monkeypatch) -> None:
    store, _ = prepared(tmp_path)
    original = ready_module._read_ready

    def changing(*args):
        ready = original(*args)
        path = store.root / "prepared.json"
        path.write_bytes(path.read_bytes() + b" ")
        return ready

    monkeypatch.setattr(ready_module, "_read_ready", changing)
    with pytest.raises(IdentityMismatch, match="during workflow verification"):
        ReadySession(store)


def test_frozen_safety_and_ratio_are_shared_across_preflight_and_ready(tmp_path: Path) -> None:
    store, value = prepared(tmp_path, safety_margin=512, target_ratio=2.0)
    assert limits_for(value.preparation).safety_tokens == 512
    assert limits_for(value.preparation).target_ratio == 2.0
    assert read_ready(store) == value


def test_session_refuses_derived_navigation_model_request(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": '<h1 id="intro">Introduction</h1><p>Text.</p>'})
    # Build a matching nav resource, using the factory's conventional root/metadata.
    import zipfile

    with zipfile.ZipFile(source) as archive:
        resources = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
    resources["OEBPS/nav.xhtml"] = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>'
        b'<nav><a href="chapter.xhtml#intro">Introduction</a></nav></body></html>'
    )
    with zipfile.ZipFile(source, "w") as archive:
        for name, raw in resources.items():
            archive.writestr(name, raw)
    result = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))
    assert result.prepared is not None and result.prepared.plan.derived_sources
    session = ReadySession(RunStore(result.work_dir))
    derived = tuple(
        member
        for member in session.index.items_by_id.values()
        if member.unit_id in result.prepared.plan.derived_sources
    )
    plan = pack_members(
        "translate",
        derived,
        result.prepared.glossary,
        session.index,
        limits_for(result.prepared.preparation),
        tokenizer_model="fake",
    )
    with pytest.raises(IdentityMismatch, match="derived navigation"):
        session.verify_batch(plan.batches[0])


def test_session_rebuilds_dynamic_payload_from_frozen_terminology(tmp_path: Path) -> None:
    from engine.item.budget import measure_budget
    from engine.schemas.bridge import batch_item_hash
    from engine.schemas.contracts import canonical_hash
    from engine.schemas.members import MemberBatch

    store, _ = prepared(tmp_path)
    session = ReadySession(store)
    path = next((store.root / "batches").glob("*.json"))
    batch = MemberBatch.model_validate_json(path.read_bytes())
    data = batch.model_dump(mode="python")
    wire = data["payload"]["items"][0]
    wire["terms"] = [
        {
            "term_id": "forged",
            "source": "Text",
            "target": "错误",
            "aliases": [],
            "mode": "required",
            "match_policy": "exact",
            "note": "",
            "role": "target",
        }
    ]
    item = batch.items[0]
    context_hash = canonical_hash(data["payload"]["context"])
    budget = measure_budget(
        stage="translate",
        payload=data["payload"],
        limits=limits_from_config(
            session.prepared.plan.translation_config,
            output_unlimited=batch.manifest.output_unlimited,
            context_unlimited=batch.manifest.context_unlimited,
        ),
        tokenizer_model="fake",
    )
    data["budget"] = budget.model_dump(mode="python")
    data["manifest"]["wire_hash"] = budget.wire_hash
    assert batch.manifest.freeze_id is not None
    data["manifest"]["input_hashes"][item.item_id] = batch_item_hash(
        item, batch.manifest.freeze_id, wire, context_hash
    )
    data["manifest"]["term_ids_by_item"][item.item_id] = ("forged",)
    data["manifest"]["terms_hashes"][item.item_id] = canonical_hash(wire["terms"])
    forged = MemberBatch.model_validate(data)
    with pytest.raises(IdentityMismatch, match="frozen source, terms or context"):
        session.verify_batch(forged)


def test_initial_batch_verification_reuses_the_plan_bound_budget(tmp_path: Path, monkeypatch) -> None:
    import engine.item.members as members_module

    store, _ = prepared(tmp_path)
    session = ReadySession(store)
    batch = next(iter(session._prepared_batches.values()))
    original_hash = ready_module.canonical_hash
    original_measure = members_module.measure_budget
    measurements = 0

    def guarded_hash(value):
        if value is session.prepared.glossary:
            raise AssertionError("verified glossary must not be rehashed for every batch")
        return original_hash(value)

    def counted_measure(*args, **kwargs):
        nonlocal measurements
        measurements += 1
        return original_measure(*args, **kwargs)

    monkeypatch.setattr(ready_module, "canonical_hash", guarded_hash)
    monkeypatch.setattr(members_module, "measure_budget", counted_measure)
    session.verify_batch(batch, initial=True)
    assert measurements == 0


def test_batch_verification_rejects_a_changed_saved_budget(tmp_path: Path) -> None:
    store, _ = prepared(tmp_path)
    session = ReadySession(store)
    batch = next(iter(session._prepared_batches.values()))
    forged = batch.model_copy(
        update={"budget": batch.budget.model_copy(update={"source_tokens": batch.budget.source_tokens + 1})}
    )
    with pytest.raises(IdentityMismatch, match="frozen capacity"):
        session.verify_batch(forged)


def test_sent_batch_verification_still_remeasures_a_changed_budget(tmp_path: Path) -> None:
    store, _ = prepared(tmp_path)
    session = ReadySession(store)
    batch = next(iter(session._prepared_batches.values()))
    changed_input = batch.budget.input_tokens + 1
    changed_reserve = changed_input + (changed_input + 1) // 2 + batch.budget.identity.wrapper_tokens
    forged = batch.model_copy(
        update={
            "budget": batch.budget.model_copy(
                update={
                    "input_tokens": changed_input,
                    "input_reserve": changed_reserve,
                    "context_tokens": (
                        changed_reserve + batch.budget.output_tokens + batch.budget.identity.safety_tokens
                    ),
                }
            )
        }
    )
    sent = batch.manifest.model_copy(
        update={
            "attempts": (
                Attempt(
                    attempt_id="attempt-1",
                    affected_items=batch.manifest.item_ids,
                    reservation={"input_budget_algorithm_version": 2},
                    state="succeeded",
                    created_at="2026-10-10T00:00:00+00:00",
                ),
            )
        }
    )
    with pytest.raises(IdentityMismatch, match="frozen capacity"):
        session.verify_batch(forged, sent_request=sent)


def test_resume_rejects_a_changed_output_policy(tmp_path: Path) -> None:
    from engine.services.preparation import resume_preparation

    store, _ = prepared(tmp_path)
    with pytest.raises(IdentityMismatch, match="output policy"):
        asyncio.run(resume_preparation(store.root, StubChecker(), output_policy_hash="changed-policy"))


def test_public_preparation_accepts_config_above_the_effective_input_cap(tmp_path: Path) -> None:
    from engine.schemas.members import MemberBatch

    store, value = prepared(tmp_path, max_input_tokens=60000, context_tokens=65536)
    assert read_ready(store) == value
    for path in (store.root / "batches").glob("*.json"):
        batch = MemberBatch.model_validate_json(path.read_bytes())
        assert batch.budget.identity.input_limit == 50000


def test_ready_rejects_an_extra_source_inventory(tmp_path: Path) -> None:
    store, _ = prepared(tmp_path)
    source = next((store.root / "inventories").glob("*.json"))
    (store.root / "inventories" / "foreign.json").write_bytes(source.read_bytes())
    with pytest.raises(IdentityMismatch, match="inventories inventory"):
        read_ready(store)


def test_session_rejects_context_removal_when_the_larger_context_fits(tmp_path: Path) -> None:
    from engine.item.budget import measure_budget
    from engine.item.members import build_member_payload
    from engine.schemas.bridge import batch_item_hash
    from engine.schemas.contracts import ItemRecord, canonical_hash
    from engine.schemas.members import MemberBatch

    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>First paragraph.</p><p>Second paragraph.</p>"})
    result = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))
    session = ReadySession(RunStore(result.work_dir))
    body = tuple(item for item in session.index.items_by_id.values() if item.channel == "body")
    item = next(member for member in body if member.source_projection == "Second paragraph.")
    record = ItemRecord(
        item_id=item.item_id,
        segment_id=item.item_id,
        terms_hash=canonical_hash([]),
        context_hash=canonical_hash([]),
        target_projection="当前译文。",
        target_hash=canonical_hash("当前译文。"),
    )
    packed = pack_members(
        "review",
        (item,),
        session.prepared.glossary,
        session.index,
        limits_for(session.prepared.preparation),
        targets={item.item_id: record},
        revisions={item.unit_id: 0},
        tokenizer_model="fake",
    )
    batch = packed.batches[0]
    assert batch.context
    payload = build_member_payload(
        "review",
        (item,),
        session.prepared.glossary,
        session.index,
        request_id=batch.manifest.request_id,
        targets={item.item_id: record},
        revisions={item.unit_id: 0},
        context_count=0,
    )
    budget = measure_budget(
        stage="review", payload=payload, limits=limits_for(session.prepared.preparation), tokenizer_model="fake"
    )
    data = batch.model_dump(mode="python")
    data.update(payload=payload, context=(), budget=budget.model_dump(mode="python"))
    data["manifest"]["wire_hash"] = budget.wire_hash
    data["manifest"]["context_hashes"][item.item_id] = canonical_hash([])
    data["manifest"]["input_hashes"][item.item_id] = batch_item_hash(
        item, session.prepared.glossary.freeze_id, payload["items"][0], canonical_hash([])
    )
    stripped = MemberBatch.model_validate(data)
    with pytest.raises(IdentityMismatch, match="frozen source, terms or context"):
        session.verify_batch(stripped)


def test_recovery_uses_bounded_lazy_source_and_batch_views(tmp_path: Path, monkeypatch) -> None:
    from engine.services.working import RecordSequence, RecordView

    store, _ = prepared(tmp_path)
    # Force oversized-record bypass to prove callers do not depend on retained objects.
    monkeypatch.setattr(ready_module, "_SOURCE_CACHE_BYTES", 0)
    monkeypatch.setattr(ready_module, "_BATCH_CACHE_BYTES", 0)
    session = ReadySession(store)
    assert isinstance(session.index.inventories, RecordSequence)
    assert isinstance(session._prepared_batches, RecordView)
    request_id = next(iter(session._prepared_batches))
    first = session._prepared_batches[request_id]
    second = session._prepared_batches[request_id]
    assert first == second and first is not second
    assert session._prepared_batches.cached_bytes == 0
    for member in first.items:
        assert member is session.index.items_by_id[member.item_id]
    assert session.index.inventories[0] == session.index.inventories[0]
    assert session.index.inventories[0] is not session.index.inventories[0]
    session.verify_batch(first, initial=True)


def test_uncached_lazy_source_rejects_changed_inventory(tmp_path: Path, monkeypatch) -> None:
    store, _ = prepared(tmp_path)
    monkeypatch.setattr(ready_module, "_SOURCE_CACHE_BYTES", 0)
    session = ReadySession(store)
    document_id = session.index.document_order[0]
    path = store.root / "inventories" / f"{document_id}.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(IdentityMismatch, match="inventory changed"):
        _ = session.index.documents[document_id]


def test_ready_loader_does_not_revalidate_a_second_inventory_model_tree(tmp_path: Path, monkeypatch) -> None:
    import engine.item.members as members_module

    store, _ = prepared(tmp_path)
    original = members_module.MemberIndex.__init__
    options = []

    def checked(self, *args, **kwargs):
        options.append(kwargs.get("copy_inventories"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(members_module.MemberIndex, "__init__", checked)
    ReadySession(store)
    assert options == []


def test_many_document_recovery_keeps_source_models_in_a_bounded_window(tmp_path: Path, monkeypatch) -> None:
    source = make_epub(
        tmp_path / "source.epub",
        {f"chapter-{index}.xhtml": f"<p>Document {index} text.</p>" for index in range(12)},
    )
    result = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(), StubChecker()))
    assert result.status == "ready"
    monkeypatch.setattr(ready_module, "_SOURCE_CACHE_BYTES", 0)
    monkeypatch.setattr(ready_module, "_MEMBER_CACHE_BYTES", 0)
    monkeypatch.setattr(ready_module, "_TEXT_CACHE_BYTES", 0)
    verify_preflight = ready_module.verify_preflight_window
    live_counts: list[int] = []

    def windowed(store, limits, model, visitor):
        references = []

        def checked(report, document_id, raw, inventory):
            gc.collect()
            live_counts.append(sum(reference() is not None for reference in references))
            visitor(report, document_id, raw, inventory)
            references.append(weakref.ref(inventory))

        return verify_preflight(store, limits, model, checked)

    monkeypatch.setattr(ready_module, "verify_preflight_window", windowed)
    session = ReadySession(RunStore(result.work_dir))
    inventories = cast(Any, session.index.inventories_by_id)
    members = cast(Any, session.index.members_by_id)
    texts = cast(Any, session.index._texts)
    assert inventories.cached_bytes == members.cached_bytes == texts.cached_bytes == 0
    parsed_inventories = 0
    parse = ready_module.parse_contract

    def counted_parse(raw, model, format, *args, **kwargs):
        nonlocal parsed_inventories
        if model is ready_module.AtomicDocument:
            parsed_inventories += 1
        return parse(raw, model, format, *args, **kwargs)

    monkeypatch.setattr(ready_module, "parse_contract", counted_parse)

    document_ids = session.index.document_order
    for document_id in document_ids:
        member = next(member for member in session.index.members if member.document_id == document_id)
        assert session.index.text(member)
        assert inventories.cached_bytes == 0
    assert 0 < parsed_inventories <= len(document_ids) * 2
    assert live_counts and max(live_counts) == 0
    assert len(session.index.documents) == len(document_ids) >= 12


def test_initial_batch_lookup_never_scans_other_lazy_batches(tmp_path, monkeypatch):
    store, _ = prepared(tmp_path)
    monkeypatch.setattr(ready_module, "_BATCH_CACHE_BYTES", 0)
    session = ReadySession(store)
    item_id = next(iter(session._initial_batch_ids))
    batches = session._prepared_batches

    class DirectOnly(dict):
        def __getitem__(self, key):
            return batches[key]

        def values(self):
            raise AssertionError("per-member proof must not scan the book's batch payloads")

    session._prepared_batches = DirectOnly()
    batch = session.initial_batch(item_id)
    assert batch is not None and item_id in batch.manifest.item_ids
    assert session.initial_batch("not-owned") is None
