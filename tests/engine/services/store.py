from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

import engine.services.atomic as base_store_module
import engine.services.store as store_module
from engine.item import structure
from engine.item.structure import EXTRACTOR_VERSION
from engine.item.views import SOURCE_VIEW_RULE_VERSION, SourceViewError
from engine.schemas.contracts import (
    Attempt,
    CandidatePool,
    FreezeIntent,
    GlossaryPayload,
    GlossarySnapshot,
    JsonValue,
    PreparationPlan,
    RequestManifest,
    TermExtractionPlan,
    TermExtractionRecord,
    TermScope,
    UnsupportedFormatError,
    UserTerm,
    canonical_hash,
    canonical_json_bytes,
    glossary_rules_hash,
    source_view_hash_payload,
    term_plan_hash,
)
from engine.services import state
from engine.services.atomic import CorruptRecord, IdentityMismatch, StaleWrite, StoreLocked
from engine.services.store import RunStore
from engine.services.terms.planning import plan_term_extraction
from tests.engine.epub.factory import make_epub
from tests.engine.schemas.contracts import make_document

_SOURCE_MARKUP = "<html><body><p>The process uses RAM.</p></body></html>"
_EXTRACTION_CONFIG: dict[str, JsonValue] = {
    "auto_extract": True,
    "strategy": "epubox-term-planner-1",
    "prompt_version": "epubox-v25-3",
    "model": "agnes",
    "target_language": "zh-Hans",
}


def _try_lock(root: str, queue: multiprocessing.Queue[bool]) -> None:
    try:
        with RunStore(root).lock(blocking=False):
            queue.put(True)
    except StoreLocked:
        queue.put(False)


def _prepare(
    tmp_path: Path,
    terms: tuple[UserTerm, ...] = (),
    *,
    extraction_config: dict[str, JsonValue] | None = None,
) -> tuple[RunStore, PreparationPlan]:
    source_hash = _write_source_snapshot(tmp_path)
    store = RunStore(tmp_path)
    document = _trusted_document(source_hash)
    document_hash = store.write_document(document)
    user_terms_hash = canonical_hash(terms)
    store.write_user_terms(terms)
    preparation = PreparationPlan(
        source_hash=source_hash,
        source_path="source.epub",
        source_epub_version="3.0",
        run_id="run-1",
        document_hashes={"d1": document_hash},
        reading_order=("d1",),
        unit_documents={"u1": "d1"},
        user_terms=terms,
        user_terms_hash=user_terms_hash,
        extraction_config=extraction_config or _EXTRACTION_CONFIG,
    )
    store.write_preparation(preparation)
    return store, preparation


def _trusted_document(source_hash: str):
    document = make_document().model_copy(update={"source_hash": source_hash, "extractor_version": EXTRACTOR_VERSION})
    document = document.model_copy(
        update={
            "resource": document.resource.model_copy(
                update={
                    "path": "OEBPS/chapter.xhtml",
                    "source_sha256": hashlib.sha256(_SOURCE_MARKUP.encode()).hexdigest(),
                }
            )
        }
    )
    original = document.source_views["v1"]
    view_id = "sv-" + canonical_hash({"rule_version": SOURCE_VIEW_RULE_VERSION, "view_hash": original.view_hash})
    view = original.model_copy(update={"view_id": view_id})
    unit = document.units[0].model_copy(update={"source_view_ids": (view_id,)})
    return document.model_copy(update={"source_views": {view_id: view}, "units": (unit,)})


def _write_source_snapshot(tmp_path: Path) -> str:
    source = make_epub(tmp_path / "input.epub", {"chapter.xhtml": _SOURCE_MARKUP})
    raw = source.read_bytes()
    (tmp_path / "source.epub").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _write_term_plan(store: RunStore, preparation: PreparationPlan) -> TermExtractionPlan:
    plan = _expected_term_plan(store, preparation)
    store.write_term_plan(plan)
    return plan


def _expected_term_plan(store: RunStore, preparation: PreparationPlan) -> TermExtractionPlan:
    ordered_ids = (
        *preparation.reading_order,
        *(document_id for document_id in preparation.document_hashes if document_id not in preparation.reading_order),
    )
    documents = tuple(store.read_document(document_id) for document_id in ordered_ids)
    return plan_term_extraction(
        documents,
        preparation.user_terms,
        source_hash=preparation.source_hash,
        preparation_hash=hashlib.sha256((store.root / "preparation.json").read_bytes()).hexdigest(),
        reading_edges=tuple(zip(preparation.reading_order, preparation.reading_order[1:], strict=False)),
        extraction_identity=preparation.extraction_config,
    ).plan


def test_p1_commits_documents_and_empty_user_copy_without_bookplan(tmp_path: Path) -> None:
    store, preparation = _prepare(tmp_path)

    assert store.read_preparation() == preparation
    assert store.read_document("d1").source_hash == preparation.source_hash
    assert store.read_user_terms().terms == ()
    assert not (tmp_path / "bookplan.json").exists()

    raw = json.loads((tmp_path / "documents" / "d1.json").read_text())
    raw["format"] = "epubox-document-2"
    (tmp_path / "documents" / "d1.json").write_text(json.dumps(raw))
    with pytest.raises(UnsupportedFormatError, match="unsupported format"):
        store.read_document("d1")


def test_obsolete_extractor_checkpoint_is_rejected_before_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _prepare(tmp_path)
    monkeypatch.setattr(structure, "EXTRACTOR_VERSION", "epubox-extractor-next")

    with pytest.raises(IdentityMismatch, match="obsolete extractor version"):
        store._trusted_preparation_documents()
    assert not list((store.root / "requests").glob("*.json"))


def test_store_replays_source_views_before_accepting_a_document(tmp_path: Path) -> None:
    document = _trusted_document("source-sha")
    original = next(iter(document.source_views.values()))
    payload = {
        "unit_id": original.unit_id,
        "document_id": original.document_id,
        "text": "forged text",
        "source_refs": original.source_refs,
        "view_kind": original.view_kind,
    }
    forged = original.model_copy(
        update={"text": payload["text"], "view_hash": canonical_hash(source_view_hash_payload(**payload))}
    )
    bad = document.model_copy(update={"source_views": {original.view_id: forged}})

    with pytest.raises(SourceViewError, match="do not match frozen"):
        RunStore(tmp_path).write_document(bad)


@pytest.mark.parametrize("record_id", ("../escape", "nested/name", ".", ".."))
def test_atomic_store_rejects_unsafe_record_ids(tmp_path: Path, record_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe record id"):
        RunStore(tmp_path)._path("documents", record_id)


def test_preparation_is_last_commit_and_rejects_partial_or_changed_inventory(tmp_path: Path) -> None:
    source_hash = _write_source_snapshot(tmp_path)
    store = RunStore(tmp_path)
    document = _trusted_document(source_hash)
    document_hash = store.write_document(document)
    store.write_user_terms(())
    preparation = PreparationPlan(
        source_hash=source_hash,
        source_path="source.epub",
        source_epub_version="3.0",
        run_id="run-1",
        document_hashes={"d1": document_hash},
        unit_documents={"u1": "d1"},
        user_terms_hash=canonical_hash(()),
    )

    with pytest.raises(CorruptRecord, match="document hash mismatch"):
        store.write_preparation(preparation.model_copy(update={"document_hashes": {"d1": "wrong"}}))
    assert not (tmp_path / "preparation.json").exists()

    store.write_preparation(preparation)
    with pytest.raises(StaleWrite, match="immutable preparation.json"):
        store.write_preparation(preparation.model_copy(update={"run_id": "changed"}))


def test_extraction_and_candidate_pool_use_record_version_cas(tmp_path: Path) -> None:
    store, preparation = _prepare(tmp_path)
    plan = _write_term_plan(store, preparation)
    item = plan.items[0]
    initial = TermExtractionRecord(
        item_id=item.item_id,
        document_id=item.document_id,
        view_ids=item.view_ids,
        extraction_input_hash=item.extraction_input_hash,
    )
    store.save_extraction(initial)
    succeeded = initial.model_copy(update={"record_version": 1, "status": "succeeded"})
    assert store.save_extraction(succeeded, expected_record_version=0) == succeeded
    with pytest.raises(StaleWrite, match="expected 0, found 1"):
        store.save_extraction(succeeded.model_copy(update={"record_version": 2}), expected_record_version=0)

    pool = CandidatePool(
        source_hash=plan.source_hash,
        preparation_hash=plan.preparation_hash,
        term_plan_hash=plan.plan_hash,
    )
    stored = store.save_candidate_pool(pool)
    assert stored.record_hash is not None
    closed = stored.model_copy(update={"record_version": 1, "extraction_status": "closed"})
    assert store.save_candidate_pool(closed, expected_record_version=0).record_version == 1
    with pytest.raises(StaleWrite, match="expected 0, found 1"):
        store.save_candidate_pool(closed.model_copy(update={"record_version": 2}), expected_record_version=0)


def test_term_plan_read_cache_tracks_only_the_plan_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, preparation = _prepare(tmp_path)
    plan = _write_term_plan(store, preparation)
    store = RunStore(tmp_path)
    calls = 0
    parse = store_module.parse_contract

    def count_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return parse(*args, **kwargs)

    monkeypatch.setattr(store_module, "parse_contract", count_parse)
    assert store.read_term_plan() == plan
    assert store.read_term_plan() == plan
    assert calls == 1

    state.write(tmp_path / "unrelated.json", b"{}")
    assert store.read_term_plan() == plan
    assert calls == 1

    invalid = plan.model_dump(mode="json") | {"plan_hash": "invalid"}
    state.write(tmp_path / "glossary" / "plan.json", canonical_json_bytes(invalid))
    with pytest.raises(CorruptRecord, match="term plan hash"):
        store.read_term_plan()
    assert calls == 2


def test_term_plan_validation_cache_tracks_its_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, preparation = _prepare(tmp_path)
    plan = _write_term_plan(store, preparation)
    from engine.services.terms import storage

    writes = 0
    write_plan = storage.write_plan

    def count_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return write_plan(*args, **kwargs)

    monkeypatch.setattr(storage, "write_plan", count_write)
    store.write_term_plan(plan)
    state.write(tmp_path / "unrelated.json", b"{}")
    store.write_term_plan(plan)
    assert writes == 0

    forged = plan.model_copy(update={"items": ()})
    with pytest.raises(IdentityMismatch, match="deterministic"):
        store.write_term_plan(forged)
    nested = plan.items[0].model_copy(update={"context_refs": ("ghost",)})
    with pytest.raises(IdentityMismatch, match="deterministic"):
        store.write_term_plan(plan.model_copy(update={"items": (nested, *plan.items[1:])}))
    assert writes == 2

    with pytest.raises(IdentityMismatch, match="verified inventories differ"):
        store.write_term_plan(plan, verified_inventories=())
    assert writes == 3

    changed = preparation.model_copy(update={"run_id": "changed"})
    state.write(tmp_path / "preparation.json", canonical_json_bytes(changed))
    with pytest.raises(IdentityMismatch, match="committed preparation"):
        store.write_term_plan(plan)
    assert writes == 4


def test_term_plan_and_requests_cannot_reference_uncommitted_inputs(tmp_path: Path) -> None:
    store, preparation = _prepare(tmp_path)
    template = _expected_term_plan(store, preparation)
    template_item = template.items[0]
    bad_item = template_item.model_copy(
        update={"document_id": "ghost", "view_ids": ("ghost",), "primary_ranges": ({"view_id": "ghost"},)}
    )
    base = template.model_dump(mode="python") | {
        "source_hash": preparation.source_hash,
        "preparation_hash": hashlib.sha256((tmp_path / "preparation.json").read_bytes()).hexdigest(),
    }
    data = base | {"items": (bad_item,)}
    data["plan_hash"] = term_plan_hash(data)
    with pytest.raises(IdentityMismatch, match="deterministic P1 coverage"):
        store.write_term_plan(TermExtractionPlan.model_validate(data))

    view_id = next(iter(store.read_document("d1").source_views))
    valid_item = template_item.model_copy(update={"view_ids": (view_id,), "primary_ranges": ({"view_id": view_id},)})
    invalid_items = (
        valid_item.model_copy(update={"context_refs": ("ghost",)}),
        valid_item.model_copy(update={"context_ranges": ({"view_id": view_id, "start": 0, "end": 1},)}),
        valid_item.model_copy(update={"user_term_ids": ("ghost",)}),
        valid_item.model_copy(update={"primary_ranges": ({"view_id": "ghost"},)}),
    )
    for invalid_item in invalid_items:
        invalid = base | {"items": (invalid_item,)}
        invalid["plan_hash"] = term_plan_hash(invalid)
        with pytest.raises(IdentityMismatch, match="deterministic P1 coverage"):
            store.write_term_plan(TermExtractionPlan.model_validate(invalid))

    empty = {
        "source_hash": preparation.source_hash,
        "preparation_hash": base["preparation_hash"],
        "auto_extract": True,
        "extraction_http_limit": 0,
        "resolution_group_limit": 0,
        "items": (),
    }
    empty["plan_hash"] = term_plan_hash(empty)
    with pytest.raises(IdentityMismatch, match="deterministic P1 coverage"):
        store.write_term_plan(TermExtractionPlan.model_validate(empty))

    plan = _write_term_plan(store, preparation)
    item = plan.items[0]
    store.save_extraction(
        TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
        )
    )
    ghost_batch = RequestManifest(
        request_id="ghost-batch",
        stage="terms",
        owner_kind="extraction_item",
        owner_id=item.item_id,
        item_ids=(item.item_id, "ghost"),
        input_hashes={item.item_id: item.extraction_input_hash, "ghost": "forged"},
        wire_hash="wire",
    )
    with pytest.raises(IdentityMismatch, match="unknown plan item"):
        store.write_request(ghost_batch)

    pool = store.save_candidate_pool(
        CandidatePool(
            source_hash=plan.source_hash,
            preparation_hash=plan.preparation_hash,
            term_plan_hash=plan.plan_hash,
            conflict_groups=({"group_id": "group-1", "group_input_hash": "group-input"},),
        )
    )
    assert pool.conflict_groups
    wrong_resolution = RequestManifest(
        request_id="bad-resolution",
        stage="resolution",
        owner_kind="resolution_group",
        owner_id="group-1",
        item_ids=("group-1",),
        input_hashes={"group-1": "forged"},
        wire_hash="wire",
    )
    with pytest.raises(IdentityMismatch, match="does not match conflict group"):
        store.write_request(wrong_resolution)
    valid_resolution = wrong_resolution.model_copy(
        update={"request_id": "good-resolution", "input_hashes": {"group-1": "group-input"}}
    )
    assert store.write_request(valid_resolution) == valid_resolution


def test_term_plan_revalidates_the_shared_source_snapshot(tmp_path: Path) -> None:
    store, preparation = _prepare(tmp_path)
    plan = _expected_term_plan(store, preparation)
    (tmp_path / "source.epub").write_bytes((tmp_path / "source.epub").read_bytes() + b"tampered")

    with pytest.raises(IdentityMismatch, match="source snapshot identity changed"):
        store.write_term_plan(plan)


def test_term_plan_hash_binds_complete_frozen_extraction_configuration(tmp_path: Path) -> None:
    extraction_config = {
        "auto_extract": True,
        "model": "term-model",
        "prompt_version": "epubox-terms-1",
        "strategy": "epubox-term-planner-1",
        "target_language": "zh-Hans",
    }
    store, preparation = _prepare(tmp_path, extraction_config=extraction_config)
    valid = _expected_term_plan(store, preparation)
    documents = tuple(store.read_document(document_id) for document_id in preparation.reading_order)
    forged = plan_term_extraction(
        documents,
        preparation.user_terms,
        source_hash=preparation.source_hash,
        preparation_hash=hashlib.sha256((tmp_path / "preparation.json").read_bytes()).hexdigest(),
        extraction_identity=extraction_config | {"model": "other-model"},
    ).plan

    assert forged.items[0].extraction_input_hash != valid.items[0].extraction_input_hash
    with pytest.raises(IdentityMismatch, match="deterministic P1 coverage"):
        store.write_term_plan(forged)
    assert store.write_term_plan(valid)


def test_atomic_update_preserves_old_record_and_lock_excludes_other_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, preparation = _prepare(tmp_path)
    plan = _write_term_plan(store, preparation)
    item = plan.items[0]
    initial = TermExtractionRecord(
        item_id=item.item_id,
        document_id=item.document_id,
        view_ids=item.view_ids,
        extraction_input_hash=item.extraction_input_hash,
    )
    store.save_extraction(initial)

    def fail_replace(source: object, target: object) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(base_store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interruption"):
        store.save_extraction(
            initial.model_copy(update={"record_version": 1, "status": "succeeded"}),
            expected_record_version=0,
        )
    assert store.read_extraction(item.item_id) == initial

    monkeypatch.undo()
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[bool] = context.Queue()
    with store.lock(blocking=False):
        process = context.Process(target=_try_lock, args=(str(tmp_path), queue))
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert queue.get(timeout=1) is False


def test_paid_term_request_remains_readable_without_bookplan_and_freeze_replays_snapshot(
    tmp_path: Path,
) -> None:
    store, preparation = _prepare(tmp_path)
    plan = _write_term_plan(store, preparation)
    item = plan.items[0]
    record = TermExtractionRecord(
        item_id=item.item_id,
        document_id=item.document_id,
        view_ids=item.view_ids,
        extraction_input_hash=item.extraction_input_hash,
        status="succeeded",
    )
    store.save_extraction(record)
    pool = store.save_candidate_pool(
        CandidatePool(
            source_hash=plan.source_hash,
            preparation_hash=plan.preparation_hash,
            term_plan_hash=plan.plan_hash,
            extraction_status="closed",
        )
    )
    request = RequestManifest(
        request_id="request-1",
        stage="terms",
        owner_kind="extraction_item",
        owner_id=item.item_id,
        item_ids=(item.item_id,),
        input_hashes={item.item_id: item.extraction_input_hash},
        wire_hash="wire-hash",
    )
    store.write_request(request)
    sent = store.reserve_attempt(
        request.request_id,
        Attempt(
            attempt_id="attempt-1",
            affected_items=(item.item_id,),
            reservation={"http": 1},
            created_at="2026-09-29T00:00:00Z",
        ),
    )
    assert sent.attempts[0].state == "reserved"
    assert store.read_request(request.request_id) == sent
    assert not (tmp_path / "bookplan.json").exists()

    payload = GlossaryPayload(
        source_hash=preparation.source_hash,
        freeze_id="freeze-1",
        extraction_config_hash=canonical_hash(preparation.extraction_config),
        user_terms_hash=preparation.user_terms_hash,
        extraction_status="closed",
        warnings=("No valid candidates were found.",),
    )
    freeze = FreezeIntent(
        freeze_id=payload.freeze_id,
        source_hash=payload.source_hash,
        preparation_hash=plan.preparation_hash,
        term_plan_hash=plan.plan_hash,
        candidate_pool_hash=canonical_hash(pool),
        user_terms_hash=payload.user_terms_hash,
        rules_hash=glossary_rules_hash(payload.terms),
        snapshot_payload=payload,
    )
    forged_payload = payload.model_copy(update={"extraction_config_hash": "forged"})
    forged_freeze = freeze.model_copy(update={"snapshot_payload": forged_payload})
    with pytest.raises(IdentityMismatch, match="configuration differs"):
        store.write_freeze(forged_freeze)
    store.write_freeze(freeze)
    glossary = GlossarySnapshot.model_validate(payload.model_dump(mode="python"))
    store.write_glossary(glossary)
    assert store.read_freeze() == freeze
    assert store.read_glossary() == glossary
    with pytest.raises(StaleWrite, match="sealed after freeze"):
        store.save_extraction(record.model_copy(update={"record_version": 1}), expected_record_version=0)
    with pytest.raises(StaleWrite, match="sealed after freeze"):
        store.save_candidate_pool(pool.model_copy(update={"record_version": 1}), expected_record_version=0)


def test_freeze_requires_exact_user_rules_from_p1(tmp_path: Path) -> None:
    user_term = UserTerm(
        term_id="user-1",
        source="RAM",
        target="内存",
        scope=TermScope(kind="book"),
    )
    store, preparation = _prepare(tmp_path, (user_term,))
    plan = _write_term_plan(store, preparation)
    item = plan.items[0]
    store.save_extraction(
        TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
            status="succeeded",
        )
    )
    pool = store.save_candidate_pool(
        CandidatePool(
            source_hash=plan.source_hash,
            preparation_hash=plan.preparation_hash,
            term_plan_hash=plan.plan_hash,
            extraction_status="closed",
        )
    )
    payload = GlossaryPayload(
        source_hash=preparation.source_hash,
        freeze_id="freeze-user",
        extraction_config_hash=canonical_hash(preparation.extraction_config),
        user_terms_hash=preparation.user_terms_hash,
        extraction_status="closed",
        warnings=("Missing committed user rule.",),
    )
    freeze = FreezeIntent(
        freeze_id=payload.freeze_id,
        source_hash=payload.source_hash,
        preparation_hash=plan.preparation_hash,
        term_plan_hash=plan.plan_hash,
        candidate_pool_hash=canonical_hash(pool),
        user_terms_hash=payload.user_terms_hash,
        rules_hash=glossary_rules_hash(payload.terms),
        snapshot_payload=payload,
    )
    with pytest.raises(IdentityMismatch, match="user rules differ"):
        store.write_freeze(freeze)
