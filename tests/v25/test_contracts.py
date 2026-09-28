from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from engine.schemas.v25 import (
    DOCUMENT_FORMAT,
    FREEZE_FORMAT,
    REQUEST_FORMAT,
    Attempt,
    BookPlan,
    CandidatePool,
    CutPlan,
    DocumentPlan,
    ExtractionItem,
    FreezeIntent,
    FrozenTerm,
    GlossaryPayload,
    ItemRecord,
    NodeRecord,
    PreparationPlan,
    RequestManifest,
    ResourceRecord,
    Segment,
    SlotRange,
    SourceRef,
    SourceSlot,
    SourceTextView,
    TermCandidate,
    TermEvidence,
    TermExtractionPlan,
    TermExtractionRecord,
    TermPreparation,
    TermScope,
    Unit,
    UnitRecord,
    UnsupportedFormatError,
    UserTerm,
    candidate_pool_record_hash,
    canonical_hash,
    canonical_json_bytes,
    compute_input_hash,
    cut_plan_hash,
    glossary_rules_hash,
    parse_contract,
    require_protocol,
    segment_hash,
    source_view_hash_payload,
    strict_json_loads,
    term_plan_hash,
    unit_record_hash,
    validate_cut_plan_coverage,
)


def make_document() -> DocumentPlan:
    ref = SourceRef(slot_id="s1", start=0, end=21)
    view_data = {
        "unit_id": "u1",
        "document_id": "d1",
        "text": "The process uses RAM.",
        "source_refs": (ref,),
        "view_kind": "primary",
    }
    view = SourceTextView(view_id="v1", view_hash=canonical_hash(source_view_hash_payload(**view_data)), **view_data)
    return DocumentPlan(
        document_id="d1",
        source_hash="source-sha",
        resource=ResourceRecord(path="OPS/chapter.xhtml", media_type="application/xhtml+xml", source_sha256="doc-sha"),
        adapter_version="adapter-1",
        extractor_version="extractor-1",
        source_markup="<html><body><p>The process uses RAM.</p></body></html>",
        nodes={"n1": NodeRecord(node_key="n1", element_path=(0, 0), qname="p")},
        source_slots={
            "s1": SourceSlot(
                slot_id="s1",
                node_key="n1",
                field="text",
                source_value="The process uses RAM.",
                ranges=(SlotRange(start=0, end=21, owner_kind="unit", owner_unit_id="u1"),),
            )
        },
        source_views={"v1": view},
        units=(
            Unit(
                unit_id="u1",
                document_id="d1",
                kind="paragraph",
                source_projection="The process uses RAM.",
                node_key="n1",
                slot_ids=("s1",),
                source_view_ids=("v1",),
            ),
        ),
    )


def make_cut_plan() -> CutPlan:
    segment_data = {
        "segment_id": "s1",
        "item_id": "i1",
        "source_start": 0,
        "source_end": 4,
        "source_projection": "text",
        "selected_term_ids": (),
        "term_applicability": {},
        "terms_hash": canonical_hash({"terms": []}),
        "context_hash": canonical_hash({"context": []}),
        "virtual_boundaries": (),
    }
    segment = Segment(**segment_data, segment_hash=segment_hash(segment_data))
    plan_data = {"plan_epoch": 0, "segments": (segment,)}
    return CutPlan(**plan_data, plan_hash=cut_plan_hash(plan_data))


def make_term_plan(*, auto_extract: bool = True, item_limit: int = 4) -> TermExtractionPlan:
    items: tuple[ExtractionItem, ...] = ()
    group_limit = 0
    if auto_extract:
        items = (
            ExtractionItem(
                item_id="te1",
                document_id="d1",
                view_ids=("v1",),
                primary_ranges=({"view_id": "v1"},),
                extraction_input_hash="extract-input",
                http_limit=item_limit,
            ),
        )
        group_limit = 1
    data = {
        "source_hash": "source-sha",
        "preparation_hash": "prep-hash",
        "auto_extract": auto_extract,
        "extraction_http_limit": sum(item.http_limit for item in items) + 3 * group_limit,
        "resolution_group_limit": group_limit,
        "items": items,
    }
    return TermExtractionPlan(**data, plan_hash=term_plan_hash(data))


def test_document_plan_is_source_only_and_rejects_old_format() -> None:
    document = make_document()
    assert document.format == DOCUMENT_FORMAT
    assert "logical_hash" not in Unit.model_fields
    assert "terms" not in Unit.model_fields

    old = document.model_dump(mode="json") | {"format": "epubox-document-2"}
    with pytest.raises(UnsupportedFormatError, match="unsupported format"):
        parse_contract(old, DocumentPlan, DOCUMENT_FORMAT)


def test_document_rejects_unknown_view_and_bad_hash() -> None:
    document = make_document()
    bad_unit = document.units[0].model_copy(update={"source_view_ids": ("missing",)})
    with pytest.raises(ValidationError, match="unknown source view"):
        DocumentPlan.model_validate(document.model_dump(mode="python") | {"units": (bad_unit,)})

    view = document.source_views["v1"]
    with pytest.raises(ValidationError, match="view_hash"):
        SourceTextView.model_validate(view.model_dump(mode="python") | {"text": "changed"})

    unknown_owner = document.source_slots["s1"].model_copy(
        update={"ranges": (SlotRange(start=0, end=21, owner_kind="unit", owner_unit_id="missing"),)}
    )
    with pytest.raises(ValidationError, match="unknown Unit"):
        DocumentPlan.model_validate(document.model_dump(mode="python") | {"source_slots": {"s1": unknown_owner}})

    no_refs_data = {
        "unit_id": "u1",
        "document_id": "d1",
        "text": "The process uses RAM.",
        "source_refs": (),
        "view_kind": "primary",
    }
    with pytest.raises(ValidationError, match="requires source_refs"):
        SourceTextView(
            view_id="v-empty-ref",
            view_hash=canonical_hash(source_view_hash_payload(**no_refs_data)),
            **no_refs_data,
        )

    unregistered = document.units[0].model_copy(update={"source_view_ids": ()})
    with pytest.raises(ValidationError, match="registered exactly once"):
        DocumentPlan.model_validate(document.model_dump(mode="python") | {"units": (unregistered,)})

    split_slot = document.source_slots["s1"].model_copy(
        update={
            "ranges": (
                SlotRange(start=0, end=10, owner_kind="unit", owner_unit_id="u1"),
                SlotRange(start=10, end=21, owner_kind="protected"),
            )
        }
    )
    with pytest.raises(ValidationError, match="not wholly owned"):
        DocumentPlan.model_validate(document.model_dump(mode="python") | {"source_slots": {"s1": split_slot}})

    duplicate_slot = document.source_slots["s1"].model_copy(update={"slot_id": "s2"})
    with pytest.raises(ValidationError, match="duplicate physical source slot"):
        DocumentPlan.model_validate(
            document.model_dump(mode="python")
            | {"source_slots": {"s1": document.source_slots["s1"], "s2": duplicate_slot}}
        )

    for refs in (
        (SourceRef(slot_id="s1", start=0, end=21),) * 2,
        (SourceRef(slot_id="s1", start=10, end=21), SourceRef(slot_id="s1", start=0, end=10)),
    ):
        view_data = {
            "unit_id": view.unit_id,
            "document_id": view.document_id,
            "text": view.text,
            "source_refs": refs,
            "view_kind": view.view_kind,
        }
        reordered = SourceTextView(
            view_id="v1", view_hash=canonical_hash(source_view_hash_payload(**view_data)), **view_data
        )
        with pytest.raises(ValidationError, match="without overlap"):
            DocumentPlan.model_validate(document.model_dump(mode="python") | {"source_views": {"v1": reordered}})


def test_preparation_rejects_invalid_or_unknown_term_scope() -> None:
    base = {
        "source_hash": "source-sha",
        "source_path": "source.epub",
        "source_epub_version": "3.0",
        "run_id": "run-1",
        "document_hashes": {"d1": "document-hash"},
        "reading_order": ("d1",),
        "unit_documents": {"u1": "d1"},
    }
    with pytest.raises(ValidationError, match="documents scope must contain exactly"):
        TermScope(kind="documents")
    with pytest.raises(ValidationError, match="must contain exactly"):
        TermScope.model_validate({"kind": "book", "document_ids": []})
    with pytest.raises(ValidationError, match="must contain exactly"):
        TermScope.model_validate({"kind": "documents", "document_ids": ["d1"], "unit_ids": []})
    assert TermScope(kind="book").model_dump(mode="json") == {"kind": "book"}

    unknown = UserTerm(term_id="t1", source="RAM", target="内存", scope=TermScope(kind="units", unit_ids=("u2",)))
    with pytest.raises(ValidationError, match="unknown Unit"):
        PreparationPlan.model_validate(
            base | {"user_terms": (unknown,), "user_terms_hash": canonical_hash((unknown,))}
        )

    valid = unknown.model_copy(update={"scope": TermScope(kind="units", unit_ids=("u1",))})
    preparation = PreparationPlan.model_validate(
        base | {"user_terms": (valid,), "user_terms_hash": canonical_hash((valid,))}
    )
    assert preparation.state == "parsed_ready"


def test_glossary_rules_hash_uses_normalized_prompt_fields_only() -> None:
    first = FrozenTerm(
        term_id="t1",
        source="RAM",
        target="内存",
        aliases=("random access memory", "RAM", "RAM"),
        scope=TermScope(kind="book"),
        mode="required",
        origin="user",
        evidence=(),
        frequency=2,
    )
    second = first.model_copy(update={"frequency": 99})
    assert first.aliases == ("RAM", "random access memory")
    assert glossary_rules_hash((first,)) == glossary_rules_hash((second,))
    assert glossary_rules_hash((first,)) != glossary_rules_hash((first.model_copy(update={"note": "hardware"}),))

    payload = GlossaryPayload(
        source_hash="source-sha",
        freeze_id="freeze-1",
        extraction_config_hash="extract-config",
        user_terms_hash="user-hash",
        extraction_status="closed",
        terms=(first,),
    )
    freeze = FreezeIntent(
        source_hash="source-sha",
        freeze_id="freeze-1",
        preparation_hash="prep-hash",
        term_plan_hash="plan-hash",
        candidate_pool_hash="pool-hash",
        user_terms_hash="user-hash",
        rules_hash=glossary_rules_hash(payload.terms),
        snapshot_payload=payload,
    )
    assert freeze.format == FREEZE_FORMAT

    with pytest.raises(ValidationError, match="explicit reason"):
        GlossaryPayload(
            source_hash="source-sha",
            freeze_id="freeze-empty",
            extraction_config_hash="extract-config",
            user_terms_hash="user-hash",
            extraction_status="closed",
        )


def test_segment_cut_plan_item_and_unit_hashes_reject_tampering() -> None:
    plan = make_cut_plan()
    validate_cut_plan_coverage(plan, 4)
    with pytest.raises(ValueError, match="complete Unit"):
        validate_cut_plan_coverage(plan, 5)

    segment = plan.segments[0]
    with pytest.raises(ValidationError, match="segment_hash"):
        Segment.model_validate(segment.model_dump(mode="python") | {"source_projection": "changed"})
    with pytest.raises(ValidationError, match="plan_hash"):
        CutPlan.model_validate(plan.model_dump(mode="python") | {"plan_epoch": 1})

    gap_data = {
        "segment_id": "s2",
        "item_id": "i2",
        "source_start": 5,
        "source_end": 6,
        "source_projection": "x",
        "selected_term_ids": (),
        "term_applicability": {},
        "terms_hash": canonical_hash({"terms": []}),
        "context_hash": canonical_hash({"context": []}),
        "virtual_boundaries": (),
    }
    gap = Segment(**gap_data, segment_hash=segment_hash(gap_data))
    broken_data = {"plan_epoch": 0, "segments": (segment, gap)}
    with pytest.raises(ValidationError, match="ordered and continuous"):
        CutPlan(**broken_data, plan_hash=cut_plan_hash(broken_data))

    item = ItemRecord(
        item_id="i1",
        segment_id="s1",
        terms_hash=segment.terms_hash,
        context_hash=segment.context_hash,
        target_projection="译文",
        target_hash=canonical_hash("译文"),
    )
    with pytest.raises(ValidationError, match="target_hash"):
        ItemRecord.model_validate(item.model_dump(mode="python") | {"target_projection": "篡改"})

    logical_hash = canonical_hash({"unit": "u1"})
    record = UnitRecord(
        unit_id="u1",
        document_id="d1",
        source_hash="source-sha",
        logical_hash=logical_hash,
        input_hash=compute_input_hash(logical_hash, plan.plan_hash),
        cut_plan=plan,
        items={"i1": item},
    )
    stored = UnitRecord.model_validate(record.model_dump(mode="python") | {"record_hash": unit_record_hash(record)})
    assert stored.record_hash == unit_record_hash(stored)
    with pytest.raises(ValidationError, match="input_hash"):
        UnitRecord.model_validate(stored.model_dump(mode="python") | {"input_hash": "wrong"})
    with pytest.raises(ValidationError, match="one-to-one"):
        UnitRecord.model_validate(stored.model_dump(mode="python") | {"items": {}})


def test_extraction_plan_uses_configured_item_limits_and_exact_global_budget() -> None:
    plan = make_term_plan(item_limit=4)
    assert plan.items[0].http_limit == 4
    assert plan.extraction_http_limit == 7
    changed = plan.model_dump(mode="python") | {"extraction_http_limit": 8}
    changed["plan_hash"] = term_plan_hash(changed)
    with pytest.raises(ValidationError, match="fixed preparation budget"):
        TermExtractionPlan.model_validate(changed)

    pool = CandidatePool(
        source_hash="source-sha",
        preparation_hash="prep-hash",
        term_plan_hash=plan.plan_hash,
        extraction_status="open",
    )
    stored = CandidatePool.model_validate(
        pool.model_dump(mode="python") | {"record_hash": candidate_pool_record_hash(pool)}
    )
    with pytest.raises(ValidationError, match="record_hash"):
        CandidatePool.model_validate(stored.model_dump(mode="python") | {"record_version": 1})


def test_freeze_requires_terminal_consistent_term_preparation() -> None:
    plan = make_term_plan(item_limit=4)
    record = TermExtractionRecord(
        item_id="te1",
        document_id="d1",
        view_ids=("v1",),
        extraction_input_hash="extract-input",
        status="succeeded",
    )
    pool = CandidatePool(
        source_hash="source-sha",
        preparation_hash="prep-hash",
        term_plan_hash=plan.plan_hash,
        extraction_status="closed",
    )
    payload = GlossaryPayload(
        source_hash="source-sha",
        freeze_id="freeze-empty",
        extraction_config_hash="extract-config",
        user_terms_hash="user-hash",
        extraction_status="closed",
        warnings=("No valid candidates were found.",),
    )
    freeze = FreezeIntent(
        source_hash="source-sha",
        freeze_id="freeze-empty",
        preparation_hash="prep-hash",
        term_plan_hash=plan.plan_hash,
        candidate_pool_hash=canonical_hash(pool),
        user_terms_hash="user-hash",
        rules_hash=glossary_rules_hash(payload.terms),
        snapshot_payload=payload,
    )
    preparation = TermPreparation(plan=plan, records={"te1": record}, candidates=pool, freeze=freeze)
    assert preparation.freeze == freeze

    with pytest.raises(ValidationError, match="open candidate pools"):
        TermPreparation.model_validate(
            preparation.model_dump(mode="python")
            | {"candidates": pool.model_copy(update={"extraction_status": "open"})}
        )
    with pytest.raises(ValidationError, match="paused"):
        TermPreparation.model_validate(preparation.model_dump(mode="python") | {"paused": True})
    pending = record.model_copy(update={"status": "in_flight"})
    with pytest.raises(ValidationError, match="terminal record"):
        TermPreparation.model_validate(preparation.model_dump(mode="python") | {"records": {"te1": pending}})
    bad_freeze = freeze.model_copy(update={"candidate_pool_hash": "wrong"})
    with pytest.raises(ValidationError, match="candidate pool hash"):
        TermPreparation.model_validate(preparation.model_dump(mode="python") | {"freeze": bad_freeze})

    disabled_plan = make_term_plan(auto_extract=False)
    disabled_pool = CandidatePool(
        source_hash="source-sha",
        preparation_hash="prep-hash",
        term_plan_hash=disabled_plan.plan_hash,
        extraction_status="disabled",
        consumed_response_ids=("unexpected",),
    )
    with pytest.raises(ValidationError, match="empty disabled"):
        TermPreparation(plan=disabled_plan, candidates=disabled_pool)

    not_required_pool = disabled_pool.model_copy(
        update={"extraction_status": "not_required", "consumed_response_ids": ()}
    )
    with pytest.raises(ValidationError, match="enabled plan"):
        TermPreparation(plan=disabled_plan, candidates=not_required_pool)


def test_frozen_model_term_must_come_from_adopted_pool_candidate() -> None:
    plan = make_term_plan(item_limit=4)
    evidence = TermEvidence(
        view_id="v1",
        source_quote="RAM",
        unit_id="u1",
        document_id="d1",
        source_refs=(SourceRef(slot_id="slot1", start=0, end=3),),
        evidence_check="source_matched",
    )
    candidate = TermCandidate(
        candidate_id="c1",
        extraction_item_id="te1",
        source="RAM",
        target="内存",
        category="abbreviation",
        evidence=(evidence,),
        status="adopted_preferred",
    )
    record = TermExtractionRecord(
        item_id="te1",
        document_id="d1",
        view_ids=("v1",),
        extraction_input_hash="extract-input",
        status="succeeded",
        candidates=(candidate,),
    )
    pool = CandidatePool(
        source_hash="source-sha",
        preparation_hash="prep-hash",
        term_plan_hash=plan.plan_hash,
        extraction_status="closed",
        candidates=(candidate,),
    )
    term = FrozenTerm(
        term_id="t1",
        source="RAM",
        target="内存",
        scope=TermScope(kind="documents", document_ids=("d1",)),
        origin="model_extraction",
        candidate_ids=("c1",),
        evidence=(evidence,),
    )
    payload = GlossaryPayload(
        source_hash="source-sha",
        freeze_id="freeze-1",
        extraction_config_hash="extract-config",
        user_terms_hash="user-hash",
        extraction_status="closed",
        terms=(term,),
    )
    freeze = FreezeIntent(
        source_hash="source-sha",
        freeze_id="freeze-1",
        preparation_hash="prep-hash",
        term_plan_hash=plan.plan_hash,
        candidate_pool_hash=canonical_hash(pool),
        user_terms_hash="user-hash",
        rules_hash=glossary_rules_hash(payload.terms),
        snapshot_payload=payload,
    )
    TermPreparation(plan=plan, records={"te1": record}, candidates=pool, freeze=freeze)

    ghost = term.model_copy(update={"candidate_ids": ("ghost",)})
    ghost_payload = payload.model_copy(update={"terms": (ghost,)})
    ghost_freeze = freeze.model_copy(
        update={"snapshot_payload": ghost_payload, "rules_hash": glossary_rules_hash(ghost_payload.terms)}
    )
    with pytest.raises(ValidationError, match="ghost or non-adopted"):
        TermPreparation(plan=plan, records={"te1": record}, candidates=pool, freeze=ghost_freeze)

    with pytest.raises(ValidationError, match="cannot expand to book scope"):
        FrozenTerm.model_validate(term.model_dump(mode="python") | {"scope": TermScope(kind="book")})
    with pytest.raises(ValidationError, match="separately verified alias"):
        FrozenTerm.model_validate(term.model_dump(mode="python") | {"aliases": ("made-up",)})

    enlarged = term.model_copy(update={"scope": TermScope(kind="documents", document_ids=("d2",))})
    enlarged_payload = payload.model_copy(update={"terms": (enlarged,)})
    enlarged_freeze = freeze.model_copy(
        update={"snapshot_payload": enlarged_payload, "rules_hash": glossary_rules_hash(enlarged_payload.terms)}
    )
    with pytest.raises(ValidationError, match="scope exceeds adopted source evidence"):
        TermPreparation(plan=plan, records={"te1": record}, candidates=pool, freeze=enlarged_freeze)

    tampered = candidate.model_copy(update={"note": "altered after extraction"})
    changed_pool = pool.model_copy(update={"candidates": (tampered,)})
    changed_freeze = freeze.model_copy(update={"candidate_pool_hash": canonical_hash(changed_pool)})
    with pytest.raises(ValidationError, match="changed an extraction response payload"):
        TermPreparation(plan=plan, records={"te1": record}, candidates=changed_pool, freeze=changed_freeze)


def test_attempt_requires_nonempty_unique_affected_items_and_nonnegative_reservation() -> None:
    for affected in ((), ("i1", "i1")):
        with pytest.raises(ValidationError, match="non-empty and unique"):
            Attempt(attempt_id="a1", affected_items=affected, created_at="now")
    for reservation in ({"http": -1}, {"http": True}):
        with pytest.raises(ValidationError, match="non-negative integers"):
            Attempt.model_validate(
                {"attempt_id": "a1", "affected_items": ("i1",), "created_at": "now", "reservation": reservation}
            )


def test_request_owner_discriminator_keeps_term_requests_outside_unit_revision() -> None:
    extraction = RequestManifest(
        request_id="r1",
        stage="terms",
        owner_kind="extraction_item",
        owner_id="te1",
        item_ids=("te1",),
        input_hashes={"te1": "extract-input"},
        wire_hash="wire",
    )
    assert extraction.format == REQUEST_FORMAT
    assert extraction.revisions == {}

    with pytest.raises(ValidationError, match="cannot claim Unit revision"):
        RequestManifest.model_validate(extraction.model_dump(mode="python") | {"revisions": {"u1": 0}})

    translation = RequestManifest(
        request_id="r2",
        stage="translate",
        owner_kind="translation_item",
        owner_id="i1",
        item_ids=("i1", "i2"),
        input_hashes={"i1": "input-1", "i2": "input-2"},
        wire_hash="wire",
        record_versions={"u1": 3, "u2": 4},
        item_unit_ids={"i1": ("u1",), "i2": ("u2",)},
        unit_document_ids={"u1": "d1", "u2": "d2"},
        plan_epochs={"u1": 0, "u2": 2},
        revisions={"u1": 1, "u2": 5},
        glossary_file_sha256="glossary-sha",
        freeze_id="freeze-1",
        term_ids_by_item={"i1": ("t2", "t1", "t2"), "i2": ()},
        terms_hashes={"i1": "terms-1", "i2": "terms-empty"},
        context_hashes={"i1": "context-1", "i2": "context-2"},
    )
    assert translation.term_ids_by_item["i1"] == ("t1", "t2")
    assert translation.revisions == {"u1": 1, "u2": 5}


def test_coherence_request_binds_multi_unit_version_vector_and_targets() -> None:
    manifest = RequestManifest(
        request_id="r-coherence",
        stage="coherence",
        owner_kind="translation_item",
        owner_id="window-1",
        item_ids=("window-1",),
        input_hashes={"window-1": "window-input"},
        wire_hash="wire",
        record_versions={"u1": 8, "u2": 4},
        item_unit_ids={"window-1": ("u1", "u2")},
        unit_document_ids={"u1": "d1", "u2": "d1"},
        plan_epochs={"u1": 0, "u2": 1},
        revisions={"u1": 3, "u2": 2},
        target_hashes={"window-1": "target-vector-hash"},
        glossary_file_sha256="glossary-sha",
        freeze_id="freeze-1",
        term_ids_by_item={"window-1": ("t1",)},
        terms_hashes={"window-1": "terms-hash"},
        context_hashes={"window-1": "context-hash"},
    )
    assert manifest.item_unit_ids["window-1"] == ("u1", "u2")

    with pytest.raises(ValidationError, match="version maps"):
        RequestManifest.model_validate(manifest.model_dump(mode="python") | {"revisions": {"u1": 3}})

    with pytest.raises(ValidationError, match="exactly one Unit"):
        RequestManifest.model_validate(manifest.model_dump(mode="python") | {"stage": "review"})
    with pytest.raises(ValidationError, match="Unit participants"):
        RequestManifest.model_validate(
            manifest.model_dump(mode="python") | {"item_unit_ids": {"window-1": ("u1", "u1", "u2")}}
        )
    with pytest.raises(ValidationError, match="cannot be empty"):
        RequestManifest.model_validate(manifest.model_dump(mode="python") | {"terms_hashes": {"window-1": ""}})


def test_resolution_request_owner_must_name_the_group() -> None:
    with pytest.raises(ValidationError, match="resolution owner_id"):
        RequestManifest(
            request_id="r-resolution",
            stage="resolution",
            owner_kind="resolution_group",
            owner_id="group-missing",
            item_ids=("group-1",),
            input_hashes={"group-1": "group-input"},
            wire_hash="wire",
        )


def test_term_modes_and_candidate_dispositions_match_the_frozen_contract() -> None:
    keep = UserTerm(
        term_id="t-keep",
        source="OpenAI",
        target="OpenAI",
        scope=TermScope(kind="book"),
        mode="keep_source",
    )
    assert keep.mode == "keep_source"
    with pytest.raises(ValidationError, match="target to equal source"):
        UserTerm(
            term_id="t-bad-keep",
            source="OpenAI",
            target="开放人工智能",
            scope=TermScope(kind="book"),
            mode="keep_source",
        )
    candidate = TermCandidate(
        candidate_id="c1",
        extraction_item_id="te1",
        source="RAM",
        target="内存",
        category="abbreviation",
        evidence=(TermEvidence(view_id="v1", source_quote="RAM"),),
        status="shadowed_by_user",
    )
    assert candidate.status == "shadowed_by_user"
    with pytest.raises(ValidationError):
        TermCandidate.model_validate(candidate.model_dump(mode="python") | {"status": "accepted"})
    with pytest.raises(ValidationError, match="source-matched evidence"):
        TermCandidate.model_validate(candidate.model_dump(mode="python") | {"status": "adopted_preferred"})

    evidence = TermEvidence(
        view_id="v1",
        source_quote="RAM",
        unit_id="u1",
        document_id="d1",
        source_refs=(SourceRef(slot_id="s1", start=17, end=20),),
        evidence_check="source_matched",
    )
    model_term = FrozenTerm(
        term_id="t-model",
        source="RAM",
        target="内存",
        scope=TermScope(kind="documents", document_ids=("d1",)),
        origin="model_extraction",
        candidate_ids=("c1",),
        evidence=(evidence,),
    )
    assert model_term.mode == "preferred"
    with pytest.raises(ValidationError, match="must be preferred"):
        FrozenTerm.model_validate(model_term.model_dump(mode="python") | {"mode": "required"})
    with pytest.raises(ValidationError, match="retain candidate IDs"):
        FrozenTerm.model_validate(model_term.model_dump(mode="python") | {"candidate_ids": ()})


def test_extraction_record_rejects_candidate_from_unknown_view() -> None:
    candidate = TermCandidate(
        candidate_id="c1",
        extraction_item_id="te1",
        source="RAM",
        target="内存",
        category="abbreviation",
        evidence=(TermEvidence(view_id="other", source_quote="RAM"),),
    )
    with pytest.raises(ValidationError, match="unknown extraction view"):
        TermExtractionRecord(
            item_id="te1",
            document_id="d1",
            view_ids=("v1",),
            extraction_input_hash="input-hash",
            candidates=(candidate,),
        )


@pytest.mark.parametrize(
    ("model", "expected", "old"),
    [
        (DocumentPlan, DOCUMENT_FORMAT, "epubox-document-1"),
        (DocumentPlan, DOCUMENT_FORMAT, "epubox-document-2"),
        (BookPlan, "epubox-book-3", "epubox-book-1"),
        (BookPlan, "epubox-book-3", "epubox-book-2"),
        (UnitRecord, "epubox-unit-3", "epubox-unit-1"),
        (UnitRecord, "epubox-unit-3", "epubox-unit-2"),
        (RequestManifest, REQUEST_FORMAT, "epubox-request-1"),
    ],
)
def test_contract_loader_rejects_old_formats(model: type, expected: str, old: str) -> None:
    with pytest.raises(UnsupportedFormatError, match=old):
        parse_contract({"format": old}, model, expected)


def test_review_1_and_hostile_json_are_rejected() -> None:
    with pytest.raises(UnsupportedFormatError, match="epubox-review-1"):
        require_protocol({"protocol": "epubox-review-1"}, "epubox-review-2")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        strict_json_loads('{"format":"epubox-book-3","format":"epubox-book-2"}')
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"value": math.inf})


def test_canonical_hash_preserves_sequence_order_but_not_object_key_order() -> None:
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    assert canonical_hash({"events": ["first", "second"]}) != canonical_hash({"events": ["second", "first"]})
