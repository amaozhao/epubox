from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from engine.item.unit_planner import build_context_index, plan_unit
from engine.schemas.contracts import (
    Attempt,
    BookPlan,
    CutPlan,
    ItemStatus,
    RequestManifest,
    Segment,
    TermExtractionRecord,
    Unit,
    UnitRecord,
    canonical_hash,
    compute_input_hash,
    cut_plan_hash,
    segment_hash,
)
from engine.services.atomic import IdentityMismatch, StaleWrite
from engine.services.store import RunStore
from engine.services.terms.freeze import freeze_terminology
from tests.engine.services.test_store import _prepare, _write_term_plan


def _frozen_store(tmp_path: Path) -> tuple[RunStore, Unit]:
    store, preparation = _prepare(tmp_path)
    plan = _write_term_plan(store, preparation)
    records = {
        item.item_id: store.save_extraction(
            TermExtractionRecord(
                item_id=item.item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
                status="succeeded",
            )
        )
        for item in plan.items
    }
    documents = tuple(store.read_document(document_id) for document_id in preparation.document_hashes)
    frozen = freeze_terminology(
        plan,
        records,
        preparation.user_terms,
        preparation.unit_documents,
        documents,
        extraction_config_hash=canonical_hash(preparation.extraction_config),
    )
    store.save_candidate_pool(frozen.candidate_pool)
    store.write_freeze(frozen.freeze_intent)
    store.write_glossary(frozen.glossary)
    return store, next(unit for document in documents for unit in document.units)


def _unit_record(
    store: RunStore, unit: Unit, *, end_delta: int = 0, selected_terms: tuple[str, ...] = ()
) -> UnitRecord:
    preparation = store.read_preparation()
    document = store.read_document(unit.document_id)
    documents = {document_id: store.read_document(document_id) for document_id in preparation.document_hashes}
    context_chars = preparation.translation_config.get("context_chars", 400)
    assert isinstance(context_chars, int)
    reading_edges = tuple(zip(preparation.reading_order, preparation.reading_order[1:], strict=False))
    context_index = build_context_index(documents, reading_edges, context_chars)
    initialized = plan_unit(
        unit,
        document,
        store.read_glossary(),
        preparation.translation_config,
        documents=documents,
        reading_edges=reading_edges,
        context_chars=context_chars,
        context_index=context_index,
    )
    cut_plan = initialized.cut_plan
    items = initialized.items
    if end_delta or selected_terms:
        original = cut_plan.segments[0]
        segment_data = original.model_dump(mode="python")
        segment_data["source_end"] = original.source_end + end_delta
        if selected_terms:
            segment_data["selected_term_ids"] = selected_terms
            segment_data["term_applicability"] = {term_id: "target" for term_id in selected_terms}
            segment_data["terms_hash"] = canonical_hash({"terms": selected_terms})
        segment_data["segment_hash"] = segment_hash(segment_data)
        segment = Segment.model_validate(segment_data)
        plan_data = {"plan_epoch": 0, "segments": (segment, *cut_plan.segments[1:])}
        cut_plan = CutPlan(**plan_data, plan_hash=cut_plan_hash(plan_data))
        item = items[segment.item_id].model_copy(
            update={
                "selected_term_ids": segment.selected_term_ids,
                "term_applicability": segment.term_applicability,
                "terms_hash": segment.terms_hash,
            }
        )
        items = items | {item.item_id: item}
    return UnitRecord(
        unit_id=unit.unit_id,
        document_id=unit.document_id,
        source_hash=preparation.source_hash,
        logical_hash=initialized.logical_hash,
        input_hash=compute_input_hash(initialized.logical_hash, cut_plan.plan_hash),
        cut_plan=cut_plan,
        items=items,
    )


def _bookplan(store: RunStore, record: UnitRecord) -> BookPlan:
    preparation = store.read_preparation()
    freeze = store.read_freeze()
    return BookPlan(
        source_hash=preparation.source_hash,
        run_id=preparation.run_id,
        preparation_hash=hashlib.sha256((store.root / "preparation.json").read_bytes()).hexdigest(),
        glossary_file_sha256=hashlib.sha256((store.root / "glossary.json").read_bytes()).hexdigest(),
        freeze_file_sha256=hashlib.sha256((store.root / "glossary" / "freeze.json").read_bytes()).hexdigest(),
        freeze_id=freeze.freeze_id,
        document_hashes=preparation.document_hashes,
        unit_ids=(record.unit_id,),
        unit_documents=preparation.unit_documents,
        required_unit_count=1,
        initial_unit_plans={record.unit_id: record.cut_plan.plan_hash if record.cut_plan else None},
        translation_config=preparation.translation_config,
        output_policy_hash="preserve-source-resources-1",
    )


def test_ready_bookplan_is_the_only_p4_commit_after_complete_unit_inventory(tmp_path: Path) -> None:
    store, unit = _frozen_store(tmp_path)
    assert not (store.root / "bookplan.json").exists()
    record = store.save_unit(_unit_record(store, unit))
    plan = _bookplan(store, record)

    plan_hash = store.write_bookplan(plan)
    assert plan_hash == hashlib.sha256((store.root / "bookplan.json").read_bytes()).hexdigest()
    assert store.read_bookplan() == plan


def test_unit_cut_plan_must_reconstruct_and_cover_the_atomized_source(tmp_path: Path) -> None:
    store, unit = _frozen_store(tmp_path)
    with pytest.raises(ValueError, match="complete Unit"):
        store.save_unit(_unit_record(store, unit, end_delta=-1))
    with pytest.raises(IdentityMismatch, match="unknown frozen term"):
        store.save_unit(_unit_record(store, unit, selected_terms=("ghost-term",)))


def test_unit_record_updates_use_cas_after_ready(tmp_path: Path) -> None:
    store, unit = _frozen_store(tmp_path)
    initial = store.save_unit(_unit_record(store, unit))
    store.write_bookplan(_bookplan(store, initial))
    item_id = next(iter(initial.items))
    target = "译文"
    item = initial.items[item_id].model_copy(
        update={
            "status": ItemStatus.CANDIDATE,
            "target_projection": target,
            "target_hash": canonical_hash(target),
        }
    )
    updated = initial.model_copy(update={"record_version": 1, "items": {item_id: item}})

    saved = store.save_unit(updated, expected_record_version=0)
    assert saved.record_version == 1 and store.read_unit(unit.unit_id) == saved
    with pytest.raises(StaleWrite, match="expected 0, found 1"):
        store.save_unit(saved.model_copy(update={"record_version": 2}), expected_record_version=0)


def test_ready_rejects_missing_unit_and_unresolved_term_attempt(tmp_path: Path) -> None:
    store, unit = _frozen_store(tmp_path)
    record = _unit_record(store, unit)
    with pytest.raises(IdentityMismatch, match="exactly one UnitRecord"):
        store.write_bookplan(_bookplan(store, record))

    stored = store.save_unit(record)
    request_path = store._path("requests", "unresolved-term")
    request = RequestManifest(
        request_id="unresolved-term",
        stage="terms",
        owner_kind="extraction_item",
        owner_id=store.read_term_plan().items[0].item_id,
        item_ids=(store.read_term_plan().items[0].item_id,),
        input_hashes={store.read_term_plan().items[0].item_id: store.read_term_plan().items[0].extraction_input_hash},
        wire_hash="wire",
        attempts=(
            Attempt(
                attempt_id="unknown-attempt",
                affected_items=(store.read_term_plan().items[0].item_id,),
                state="unknown",
                created_at="2026-09-29T00:00:00Z",
            ),
        ),
    )
    store._atomic_write(request_path, request)
    with pytest.raises(IdentityMismatch, match="term request is not terminal"):
        store.write_bookplan(_bookplan(store, stored))


def test_reserved_but_never_sent_term_attempt_occupies_budget_without_blocking_ready(tmp_path: Path) -> None:
    store, unit = _frozen_store(tmp_path)
    stored = store.save_unit(_unit_record(store, unit))
    term_item = store.read_term_plan().items[0]
    request = RequestManifest(
        request_id="reserved-only",
        stage="terms",
        owner_kind="extraction_item",
        owner_id=term_item.item_id,
        item_ids=(term_item.item_id,),
        input_hashes={term_item.item_id: term_item.extraction_input_hash},
        wire_hash="wire",
        attempts=(
            Attempt(
                attempt_id="reserved-attempt",
                affected_items=(term_item.item_id,),
                reservation={"http": 1},
                created_at="2026-09-29T00:00:00Z",
            ),
        ),
    )
    store._atomic_write(store._path("requests", request.request_id), request)

    store.write_bookplan(_bookplan(store, stored))
    persisted = store.read_request(request.request_id).attempts[0]
    assert persisted.state == "reserved" and persisted.sent_at is None and persisted.usage is None


def test_unit_cas_reuses_one_verified_shared_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, unit = _frozen_store(tmp_path)
    counts = {"preparation": 0, "freeze": 0}
    load_preparation = store._trusted_preparation_documents
    load_freeze = store._trusted_frozen_glossary

    def counted_preparation():
        counts["preparation"] += 1
        return load_preparation()

    def counted_freeze(preparation=None):
        counts["freeze"] += 1
        return load_freeze(preparation)

    monkeypatch.setattr(store, "_trusted_preparation_documents", counted_preparation)
    monkeypatch.setattr(store, "_trusted_frozen_glossary", counted_freeze)
    current = store.save_unit(_unit_record(store, unit))
    for version in range(1, 21):
        current = store.save_unit(
            current.model_copy(update={"record_version": version}),
            expected_record_version=version - 1,
        )

    assert current.record_version == 20
    assert counts == {"preparation": 1, "freeze": 1}
