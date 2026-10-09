from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from engine.epub.preparation import PreparationConfig
from engine.execution.atomic import _pending_batches, _retry_partitions, run_atomic
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION, extract_resource
from engine.item.inline import events_to_projection, parse_projection
from engine.item.members import MemberIndex, materialize_members, pack_members
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash
from engine.schemas.internal import Event
from engine.services.journal import BodyJournal
from engine.services.preflight import preflight_atomic_resources
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession, limits_from_config
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer
from tests.engine.item.members import glossary, source

MODEL = "gpt-3.5-turbo"
CONFIG = {
    "model": MODEL,
    "max_source_tokens": 5000,
    "max_input_tokens": 50000,
    "max_output_tokens": 10000,
    "context_tokens": 60000,
    "output_budget_version": 7,
    "source_hard_limit": 1500,
}


def _case(*, large_second: bool = False):
    second = "word " * 900 if large_second else "Other"
    tail = "tail " * 900 if large_second else "Other tail"
    raws = {
        "OPS/one.xhtml": source(
            '<p>First<img alt="One" title="Two"/></p><p>Middle</p>'
            '<p>Last<img alt="Three" title="Four"/></p><p>Fourth</p>'
        ).replace(
            b"<head/>",
            b'<head><title>First title</title><meta name="description" content="First summary"/></head>',
        ),
        "OPS/two.xhtml": source(f'<p>{second}<img alt="Five" title="Six"/></p><p>{tail}</p>').replace(
            b"<head/>",
            b'<head><title>Second title</title><meta name="description" content="Second summary"/></head>',
        ),
    }
    inventories = tuple(extract_resource(raw, path, "book") for path, raw in raws.items())
    capacity = limits_from_config(CONFIG, context_unlimited=True)
    report = preflight_atomic_resources(inventories, raws, capacity, MODEL)
    members = materialize_members(inventories, report)
    index = MemberIndex(inventories, report, members)
    frozen = glossary()
    initial = tuple(
        pack_members("translate", (member,), frozen, index, capacity, sparse=True).batches[0] for member in members
    )
    records = {
        member.item_id: ItemRecord(
            item_id=member.item_id,
            segment_id=member.item_id,
            terms_hash="terms",
            context_hash="context",
        )
        for member in members
    }
    session = SimpleNamespace(
        index=index,
        prepared=SimpleNamespace(
            glossary=frozen,
            plan=SimpleNamespace(derived_sources={}, translation_config=CONFIG),
        ),
    )

    def saved(item_ids=None):
        return records if item_ids is None else {item_id: records[item_id] for item_id in item_ids}

    return SimpleNamespace(session=session, records=saved, _requests={}), initial, records


def test_resume_reuses_unchanged_batches_inside_a_partly_completed_html(monkeypatch):
    import engine.execution.atomic as atomic_module

    journal, initial, records = _case(large_second=True)
    initial = tuple(atomic_module._unlimited_batches(journal, initial))
    document = journal.session.index.document_order[1]
    body = [batch for batch in initial if batch.items[0].document_id == document and batch.items[0].channel == "body"]
    assert len(body) == 2
    completed = body[0].items[0]
    records[completed.item_id] = records[completed.item_id].model_copy(update={"status": ItemStatus.REVIEWED})
    untouched = body[1]
    original = atomic_module.pack_members

    def tracked(stage, members, *args, **kwargs):
        assert not set(untouched.manifest.item_ids).intersection(member.item_id for member in members)
        return original(stage, members, *args, **kwargs)

    monkeypatch.setattr(atomic_module, "pack_members", tracked)
    scheduled = _pending_batches(journal, initial, output_unlimited=True)
    assert any(batch is untouched for batch in scheduled)
    assert all(completed.item_id not in batch.manifest.item_ids for batch in scheduled)


def test_regrouping_sorts_sparse_retries_around_an_untouched_batch():
    journal, initial, records = _case()
    document = journal.session.index.document_order[0]
    body = [item for item in journal.session.index.members if item.document_id == document and item.channel == "body"]
    first = pack_members(
        "translate",
        body[:2],
        journal.session.prepared.glossary,
        journal.session.index,
        limits_from_config(CONFIG, context_unlimited=True),
        sparse=True,
    ).batches[0]
    replaced = {item.item_id for item in body[:2]}
    initial = tuple(batch for batch in initial if not replaced.intersection(batch.manifest.item_ids)) + (first,)
    records[body[1].item_id] = records[body[1].item_id].model_copy(update={"status": ItemStatus.REVIEWED})
    records[body[3].item_id] = records[body[3].item_id].model_copy(update={"checks": {"translation_epoch": 1}})
    planned = _pending_batches(journal, initial, output_unlimited=True)
    batches = [
        batch for batch in planned if batch.items[0].document_id == document and batch.items[0].channel == "body"
    ]
    assert len(batches) == 1
    assert batches[0].manifest.item_ids == tuple(body[index].item_id for index in (0, 2, 3))


def test_first_dispatch_merges_sparse_text_lanes_per_html():
    journal, initial, _ = _case()
    scheduled = _pending_batches(journal, initial)

    body = [batch for batch in scheduled if batch.items[0].channel == "body"]
    assert len(body) == 2
    assert len({batch.items[0].document_id for batch in body}) == 2
    assert all(batch.manifest.sparse and batch.context == () for batch in body)
    for channel in ("attribute", "metadata"):
        batches = [batch for batch in scheduled if batch.items[0].channel == channel]
        assert len(batches) == 2
        assert len({batch.items[0].document_id for batch in batches}) == 2
        assert all(batch.manifest.sparse and batch.context == () for batch in batches)
        assert all(len({item.document_id for item in batch.items}) == 1 for batch in batches)


def test_resume_merges_pending_attributes_across_accepted_gap_but_keeps_review_separate():
    journal, initial, records = _case()
    document = initial[0].items[0].document_id
    attributes = [
        item
        for batch in initial
        for item in batch.items
        if item.document_id == document and item.channel == "attribute"
    ]
    first, accepted, third, review = attributes
    records[accepted.item_id] = records[accepted.item_id].model_copy(update={"status": ItemStatus.REVIEWED})
    records[review.item_id] = records[review.item_id].model_copy(
        update={
            "status": ItemStatus.LOCAL_VALID,
            "target_projection": review.source_projection,
            "target_hash": canonical_hash(review.source_projection),
        }
    )

    scheduled = _pending_batches(journal, initial)
    batches = [
        batch
        for batch in scheduled
        if batch.items[0].document_id == document and batch.items[0].channel == "attribute"
    ]
    assert [tuple(item.item_id for item in batch.items) for batch in batches] == [
        (first.item_id, third.item_id),
        (review.item_id,),
    ]
    assert accepted.item_id not in {item.item_id for batch in batches for item in batch.items}


def test_terminal_attribute_request_does_not_block_lane_merge():
    journal, initial, _ = _case()
    attribute = next(batch for batch in initial if batch.items[0].channel == "attribute")
    document = attribute.items[0].document_id
    journal._requests[attribute.manifest.request_id] = attribute.manifest

    scheduled = _pending_batches(journal, initial)

    batches = [
        batch
        for batch in scheduled
        if batch.items[0].document_id == document and batch.items[0].channel == "attribute"
    ]
    assert len(batches) == 1 and len(batches[0].items) == 4


def test_ambiguous_attribute_request_keeps_its_saved_lane_ids():
    journal, initial, _ = _case()
    attribute = next(batch for batch in initial if batch.items[0].channel == "attribute")
    document = attribute.items[0].document_id
    journal._requests[attribute.manifest.request_id] = attribute.manifest
    journal._ambiguous = lambda _request: True
    journal._translation_batch = lambda _request: attribute

    scheduled = _pending_batches(journal, initial)

    expected = {
        batch.manifest.request_id
        for batch in initial
        if batch.items[0].document_id == document and batch.items[0].channel == "attribute"
    }
    assert {
        batch.manifest.request_id
        for batch in scheduled
        if batch.items[0].document_id == document and batch.items[0].channel == "attribute"
    } == expected


def test_ambiguous_sparse_body_request_with_a_new_id_keeps_its_owned_members():
    journal, initial, _ = _case()
    document = journal.session.index.document_order[0]
    originals = [
        batch for batch in initial if batch.items[0].document_id == document and batch.items[0].channel == "body"
    ]
    owner = pack_members(
        "translate",
        tuple(item for batch in originals[:2] for item in batch.items),
        journal.session.prepared.glossary,
        journal.session.index,
        limits_from_config(CONFIG, context_unlimited=True),
        sparse=True,
    ).batches[0]
    assert owner.manifest.request_id not in {batch.manifest.request_id for batch in originals}
    journal._requests[owner.manifest.request_id] = owner.manifest
    journal._ambiguous = lambda request: request.request_id == owner.manifest.request_id
    journal._translation_batch = lambda _request: owner

    scheduled = _pending_batches(journal, initial)

    body = [
        batch for batch in scheduled if batch.items[0].document_id == document and batch.items[0].channel == "body"
    ]
    assert body[0] is owner
    assert len(body) == 2 and tuple(body[1].items) == tuple(item for batch in originals[2:] for item in batch.items)


def test_partial_resume_reuses_an_untouched_html_body_without_repacking(monkeypatch):
    import engine.execution.atomic as atomic_module

    journal, initial, records = _case(large_second=True)
    documents = journal.session.index.document_order
    first, second = documents
    first_body = [
        item for item in journal.session.index.members if item.document_id == first and item.channel == "body"
    ]
    first_batch = pack_members(
        "translate",
        first_body,
        journal.session.prepared.glossary,
        journal.session.index,
        limits_from_config(CONFIG, context_unlimited=True),
        sparse=True,
    ).batches[0]
    initial = tuple(
        batch for batch in initial if not (batch.items[0].document_id == first and batch.items[0].channel == "body")
    ) + (first_batch,)
    records[first_body[0].item_id] = records[first_body[0].item_id].model_copy(update={"status": ItemStatus.REVIEWED})
    second_body = [
        batch for batch in initial if batch.items[0].document_id == second and batch.items[0].channel == "body"
    ]
    real_pack = atomic_module.pack_members

    def tracked(stage, members, *args, **kwargs):
        assert not any(member.document_id == second and member.channel == "body" for member in members)
        return real_pack(stage, members, *args, **kwargs)

    monkeypatch.setattr(atomic_module, "pack_members", tracked)
    scheduled = _pending_batches(journal, initial)

    assert all(any(batch is original for batch in scheduled) for original in second_body)
    assert not any(batch is first_batch for batch in scheduled)
    remaining = [
        batch for batch in scheduled if batch.items[0].document_id == first and batch.items[0].channel == "body"
    ]
    assert len(remaining) == 1
    assert tuple(item.item_id for item in remaining[0].items) == tuple(item.item_id for item in first_body[1:])


def test_retry_partition_replans_only_its_html_body(monkeypatch):
    import engine.execution.atomic as atomic_module

    journal, initial, _ = _case(large_second=True)
    first, second = journal.session.index.document_order
    first_body = [
        item for item in journal.session.index.members if item.document_id == first and item.channel == "body"
    ]
    first_batch = pack_members(
        "translate",
        first_body,
        journal.session.prepared.glossary,
        journal.session.index,
        limits_from_config(CONFIG, context_unlimited=True),
        sparse=True,
    ).batches[0]
    initial = tuple(
        batch for batch in initial if not (batch.items[0].document_id == first and batch.items[0].channel == "body")
    ) + (first_batch,)
    second_body = [
        batch for batch in initial if batch.items[0].document_id == second and batch.items[0].channel == "body"
    ]
    real_pack = atomic_module.pack_members

    def tracked(stage, members, *args, **kwargs):
        assert not any(member.document_id == second and member.channel == "body" for member in members)
        return real_pack(stage, members, *args, **kwargs)

    monkeypatch.setattr(atomic_module, "pack_members", tracked)
    scheduled = _pending_batches(journal, initial, partitions=({first_body[0].item_id},))

    assert all(any(batch is original for batch in scheduled) for original in second_body)
    replanned = [
        batch for batch in scheduled if batch.items[0].document_id == first and batch.items[0].channel == "body"
    ]
    assert {tuple(item.item_id for item in batch.items) for batch in replanned} == {
        tuple(item.item_id for item in first_body[1:]),
        (first_body[0].item_id,),
    }
    assert all(
        batch.manifest.sparse and batch.manifest.request_id != first_batch.manifest.request_id for batch in replanned
    )


def test_structural_retries_merge_across_accepted_gap_and_keep_review_separate():
    journal, initial, records = _case()
    document = journal.session.index.document_order[0]
    body = [item for item in journal.session.index.members if item.document_id == document and item.channel == "body"]
    first, accepted, third, review = body
    feedback = "text moved across a protected range or boundary"
    for member in (first, third):
        records[member.item_id] = records[member.item_id].model_copy(
            update={"checks": {"translation_epoch": 1, "retry_feedback": feedback}}
        )
    records[accepted.item_id] = records[accepted.item_id].model_copy(update={"status": ItemStatus.REVIEWED})
    records[review.item_id] = records[review.item_id].model_copy(
        update={
            "status": ItemStatus.LOCAL_VALID,
            "target_projection": review.source_projection,
            "target_hash": canonical_hash(review.source_projection),
            "checks": {"review_epoch": 1},
        }
    )

    selected = {first.item_id, third.item_id, review.item_id}
    scheduled = _pending_batches(journal, initial, selected)

    assert {tuple(item.item_id for item in batch.items) for batch in scheduled} == {
        (first.item_id, third.item_id),
        (review.item_id,),
    }
    translation = next(batch for batch in scheduled if len(batch.items) == 2)
    assert translation.manifest.feedback_by_item == {first.item_id: feedback, third.item_id: feedback}
    assert all(batch.manifest.sparse for batch in scheduled)


def test_saved_review_targets_split_when_the_combined_actual_input_overflows():
    journal, initial, records = _case()
    document = journal.session.index.document_order[0]
    body = [item for item in journal.session.index.members if item.document_id == document and item.channel == "body"][
        :2
    ]
    for member in body:
        target = events_to_projection(
            Event(kind="text", value="甲乙丙丁戊己庚辛壬癸" * 1000)
            if event.kind == "text" and event.value.strip()
            else event
            for event in parse_projection(member.source_projection)
        )
        records[member.item_id] = records[member.item_id].model_copy(
            update={
                "status": ItemStatus.LOCAL_VALID,
                "target_projection": target,
                "target_hash": canonical_hash(target),
                "checks": {"review_epoch": 1},
            }
        )

    scheduled = _pending_batches(journal, initial, {member.item_id for member in body})

    assert [tuple(item.item_id for item in batch.items) for batch in scheduled] == [
        (body[0].item_id,),
        (body[1].item_id,),
    ]


def test_truncated_request_keeps_binary_retry_partitions():
    journal, _, records = _case()
    document = journal.session.index.document_order[0]
    body = tuple(
        item for item in journal.session.index.members if item.document_id == document and item.channel == "body"
    )
    batch = pack_members(
        "translate",
        body,
        journal.session.prepared.glossary,
        journal.session.index,
        limits_from_config(CONFIG, context_unlimited=True),
        sparse=True,
    ).batches[0]
    journal._requests[batch.manifest.request_id] = batch.manifest
    feedback = f"translate:{batch.manifest.request_id}: translation response was truncated"
    for member in body:
        records[member.item_id] = records[member.item_id].model_copy(
            update={"checks": {"translation_epoch": 1, "retry_feedback": feedback}}
        )

    partitions = _retry_partitions(journal, {member.item_id for member in body})
    scheduled = _pending_batches(
        journal,
        (batch,),
        {member.item_id for member in body},
        partitions=partitions,
    )

    assert partitions == (
        tuple(member.item_id for member in body[:2]),
        tuple(member.item_id for member in body[2:]),
    )
    assert {tuple(item.item_id for item in value.items) for value in scheduled} == set(partitions)
    assert all(value.manifest.sparse for value in scheduled)


def test_regrouped_attributes_persist_frames_and_resume_without_requests(tmp_path: Path):
    source_epub = make_epub(
        tmp_path / "source.epub",
        {
            "chapter.xhtml": (
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter title</title>'
                '<meta name="description" content="Chapter summary"/></head><body>'
                '<p>First<img alt="One" title="Two"/></p><p>Middle</p>'
                '<p>Last<img alt="Three" title="Four"/></p></body></html>'
            )
        },
    )
    prepared = asyncio.run(
        prepare_translation(
            source_epub,
            tmp_path / "work",
            PreparationConfig(
                run_id="scheduling",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config=CONFIG,
            ),
            StubChecker(),
        )
    )
    assert prepared.prepared is not None
    session = ReadySession(RunStore(prepared.work_dir))
    attribute_order = tuple(
        item.item_id
        for item in session.index.members
        if item.channel == "attribute"
        and session.index.documents[item.document_id].resource.path.endswith("chapter.xhtml")
    )
    attributes = set(attribute_order)
    initial = tuple(session._prepared_batches.values())
    assert len(attributes) == 4
    assert (
        sum(
            batch.items[0].channel == "attribute" and bool(attributes & set(batch.manifest.item_ids))
            for batch in initial
        )
        > 1
    )
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def transport(stage, payload):
        ids = tuple(item["item_id"] for item in payload["items"])
        if attributes & set(ids):
            calls.append((stage, ids))
        return answer(stage, payload)

    result = asyncio.run(run_atomic(session.store.root, transport=transport))
    assert result.status == "translated", result.reason
    assert calls == [("translate", attribute_order), ("review", attribute_order)]
    records = BodyJournal(session.store).records(attribute_order)
    for record in records.values():
        frame = record.checks["translation_frame"]
        assert isinstance(frame, dict) and frame["member_ids"] == list(attribute_order)
    before = len(calls)
    assert asyncio.run(run_atomic(session.store.root, transport=transport)).status == "translated"
    assert len(calls) == before
