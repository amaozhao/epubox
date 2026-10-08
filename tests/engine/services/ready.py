from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import engine.services.ready as ready_module
from engine.item.members import pack_members
from engine.schemas.contracts import canonical_json_bytes
from engine.services.atomic import IdentityMismatch
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession, limits_for, read_ready
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.services.preparing import atomic_config


def prepared(tmp_path: Path, **config):
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Translate this text.</p>"})
    result = asyncio.run(prepare_translation(source, tmp_path / "work", atomic_config(**config), StubChecker()))
    assert result.status == "ready" and result.prepared is not None
    return RunStore(result.work_dir), result.prepared


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


def test_session_recovery_uses_saved_batches_without_repacking(tmp_path: Path, monkeypatch) -> None:
    import engine.item.members as members_module

    store, original = prepared(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recovery must not rebuild the frozen request plan")

    monkeypatch.setattr(members_module, "pack_members", forbidden)
    session = ReadySession(store)
    assert session.prepared == original
    assert set(session._prepared_batches) == set(original.plan.batch_hashes)


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
        limits=limits_for(session.prepared.preparation),
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


def test_batch_verification_reuses_frozen_glossary_hash_and_measured_budget(tmp_path: Path, monkeypatch) -> None:
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
    assert measurements == 1


def test_batch_verification_rejects_a_changed_saved_budget(tmp_path: Path) -> None:
    store, _ = prepared(tmp_path)
    session = ReadySession(store)
    batch = next(iter(session._prepared_batches.values()))
    forged = batch.model_copy(
        update={"budget": batch.budget.model_copy(update={"source_tokens": batch.budget.source_tokens + 1})}
    )
    with pytest.raises(IdentityMismatch, match="frozen capacity"):
        session.verify_batch(forged)


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
