from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest
from pydantic import ValidationError

import engine.services.store as store_module
from engine.item.planner import PlannerConfig, plan_unit
from engine.schemas.v23 import (
    Attempt,
    BookPlan,
    CutPlan,
    DocumentPlan,
    ItemRecord,
    ItemStatus,
    NodeRecord,
    RegistryEntry,
    ResourceRecord,
    RunResult,
    SlotRange,
    SourceSlot,
    Unit,
    UnitRecord,
    canonical_hash,
    canonical_json_bytes,
    compute_input_hash,
    strict_json_loads,
    unit_record_hash,
)
from engine.services.store import CorruptRecord, IdentityMismatch, StaleWrite, Store, StoreLocked


def _resource() -> ResourceRecord:
    return ResourceRecord(path="OEBPS/ch1.xhtml", media_type="application/xhtml+xml", source_sha256="a" * 64)


def _unit(unit_id: str = "u1") -> Unit:
    return Unit(
        unit_id=unit_id,
        document_id="d1",
        kind="paragraph",
        source_projection="Use ⟦=x1⟧ now.",
        node_key="n1",
        slot_ids=("s1",),
        registry={
            "x1": RegistryEntry(
                ref_id="x1",
                kind="x",
                source_node_key="n2",
                parent_ref="n1",
                movement="same_parent",
                reorder_allowed=True,
                source_text="finally",
                hints={"display": "inline code"},
            )
        },
        context={"section": "Cleanup"},
        terms=({"source": "release", "target": "释放"},),
        checks=("coverage", "markers"),
        region={"assembly_node_key": "n1"},
        logical_hash="b" * 64,
    )


def _document() -> DocumentPlan:
    unit = _unit()
    return DocumentPlan(
        document_id="d1",
        source_hash="c" * 64,
        resource=_resource(),
        adapter_version="xml-1",
        extractor_version="extract-1",
        source_markup="<html><body><p>Use <code>finally</code> now.</p></body></html>",
        nodes={
            "n1": NodeRecord(node_key="n1", element_path=(1, 0), qname="p"),
            "n2": NodeRecord(node_key="n2", element_path=(1, 0, 0), qname="code"),
        },
        source_slots={
            "s1": SourceSlot(
                slot_id="s1",
                node_key="n1",
                field="text",
                source_value="Use ",
                ranges=(SlotRange(start=0, end=4, owner_kind="unit", owner_unit_id="u1"),),
                owner_kind="unit",
                owner_unit_id="u1",
            )
        },
        units=(unit,),
    )


def _cut_plan(epoch: int = 0, unit: Unit | None = None) -> CutPlan:
    return plan_unit(unit or _unit(), PlannerConfig(context_tokens=4096), epoch=epoch)


def _try_lock(root: str, queue: multiprocessing.Queue[bool]) -> None:
    try:
        with Store(root).lock(blocking=False):
            queue.put(True)
    except StoreLocked:
        queue.put(False)


def test_schema_round_trip_is_plain_json_and_versions_are_independent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    document = _document()
    document_hash = store.write_document(document)
    assert store.read_document("d1", expected_hash=document_hash) == document

    record = store.initialize_unit(_unit(), _cut_plan(), source_hash=document.source_hash)
    assert record.plan_epoch == 0
    assert record.revision == 0
    assert record.record_version == 0
    assert record.input_hash == compute_input_hash(record.logical_hash, record.cut_plan.plan_hash)  # type: ignore[union-attr]
    assert record.counters.unit_http_limit == 24
    assert set(record.items) == {record.cut_plan.segments[0].item_id}  # type: ignore[union-attr]

    decoded = json.loads((tmp_path / "units" / "u1.json").read_text())
    assert decoded["format"] == "epubox-unit-1"
    assert "record_hash" in decoded
    with pytest.raises(ValidationError):
        DocumentPlan.model_validate({**document.model_dump(), "surprise": True})


def test_canonical_json_rejects_ambiguous_or_hostile_input() -> None:
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    with pytest.raises(ValueError, match="duplicate"):
        strict_json_loads('{"a": 1, "a": 2}')
    with pytest.raises(ValueError, match="non-finite"):
        strict_json_loads('{"a": NaN}')
    nested: object = 0
    for _ in range(66):
        nested = [nested]
    with pytest.raises(ValueError, match="nesting"):
        canonical_json_bytes(nested)
    with pytest.raises(ValueError, match="exceeds"):
        strict_json_loads(b"{}", max_bytes=1)


@pytest.mark.parametrize(
    ("ranges", "message"),
    [
        ((SlotRange(start=2, end=3, owner_kind="out_of_scope"),), "continuous"),
        (
            (
                SlotRange(start=0, end=4, owner_kind="out_of_scope"),
                SlotRange(start=3, end=6, owner_kind="out_of_scope"),
            ),
            "continuous",
        ),
        ((SlotRange(start=0, end=7, owner_kind="out_of_scope"),), "exceeds"),
    ],
)
def test_source_slot_ranges_reject_gaps_overlap_and_out_of_bounds(ranges: tuple[SlotRange, ...], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SourceSlot(
            slot_id="bad",
            node_key="n1",
            field="text",
            source_value="abcdef",
            ranges=ranges,
        )


def test_document_references_require_bidirectional_slot_ownership() -> None:
    document = _document()
    raw = document.model_dump(mode="json")
    raw["units"][0]["slot_ids"] = []
    with pytest.raises(ValidationError, match="bidirectional"):
        DocumentPlan.model_validate(raw)

    raw = document.model_dump(mode="json")
    raw["units"][0]["registry"]["x1"]["source_node_key"] = "missing"
    with pytest.raises(ValidationError, match="unknown source node"):
        DocumentPlan.model_validate(raw)


def test_atomic_replace_preserves_old_file_when_commit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path)
    store.write_report({"state": "old"})

    def fail_replace(source: object, target: object) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interruption"):
        store.write_report({"state": "new"})

    assert strict_json_loads((tmp_path / "report.json").read_bytes()) == {"state": "old"}
    assert not list(tmp_path.glob(".report.json.*.tmp"))


def test_lock_is_reentrant_but_excludes_another_process(tmp_path: Path) -> None:
    store = Store(tmp_path)
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[bool] = context.Queue()
    with store.lock(blocking=False):
        store.write_report({"nested": True})
        process = context.Process(target=_try_lock, args=(str(tmp_path), queue))
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert queue.get(timeout=1) is False

    with Store(tmp_path).lock(blocking=False):
        pass


def test_same_generation_segments_merge_without_lost_update_and_late_epoch_is_rejected(tmp_path: Path) -> None:
    store = Store(tmp_path)
    unit = _unit().model_copy(update={"source_projection": "word " * 160, "registry": {}, "logical_hash": "d" * 64})
    plan = plan_unit(
        unit,
        PlannerConfig(
            context_tokens=1500,
            max_output_tokens=512,
            review_output_tokens=128,
            safety_margin=16,
            translation_overhead=1,
            review_overhead=1,
        ),
    )
    assert len(plan.segments) > 1
    initial = store.initialize_unit(unit, plan, source_hash="c" * 64)
    first_segment, second_segment = plan.segments[:2]
    first = ItemRecord(
        item_id=first_segment.item_id,
        segment_id=first_segment.segment_id,
        status=ItemStatus.LOCAL_VALID,
        target_projection="使用",
        target_hash=canonical_hash("使用"),
        request_id="r1",
        attempt_id="a1",
        attempts={"translation": 1},
    )
    second = ItemRecord(
        item_id=second_segment.item_id,
        segment_id=second_segment.segment_id,
        status=ItemStatus.LOCAL_VALID,
        target_projection="⟦=x1⟧。",
        target_hash=canonical_hash("⟦=x1⟧。"),
        request_id="r2",
        attempt_id="a2",
        attempts={"translation": 1},
    )

    after_first = store.merge_item("u1", first, plan_epoch=0, revision=0, input_hash=initial.input_hash or "")
    after_second = store.merge_item("u1", second, plan_epoch=0, revision=0, input_hash=initial.input_hash or "")
    assert after_second.record_version == after_first.record_version + 1
    assert (after_second.plan_epoch, after_second.revision) == (0, 0)
    assert after_second.items[first.item_id] == first
    assert after_second.items[second.item_id] == second
    with pytest.raises(StaleWrite, match="late"):
        store.merge_item("u1", second, plan_epoch=1, revision=0, input_hash=initial.input_hash or "")

    with pytest.raises(StaleWrite, match="counter cannot decrease"):
        store.save_unit(
            after_second.model_copy(
                update={"counters": after_second.counters.model_copy(update={"unit_http_limit": 1})}
            )
        )

    stale_attempt_count = first.model_copy(update={"attempts": {"translation": 0}})
    with pytest.raises(StaleWrite, match="item attempt counter cannot decrease"):
        store.merge_item(
            "u1",
            stale_attempt_count,
            plan_epoch=0,
            revision=0,
            input_hash=initial.input_hash or "",
        )


def test_record_hash_detects_damage_and_scan_isolates_only_bad_unit(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.initialize_unit(_unit("u1"), _cut_plan(), source_hash="c" * 64)
    damaged = tmp_path / "units" / "u1.json"
    data = json.loads(damaged.read_text())
    data["revision"] = 9
    damaged.write_text(json.dumps(data))

    valid, invalid = store.scan_units(("u1", "missing"))
    assert valid == {}
    assert set(invalid) == {"u1", "missing"}
    with pytest.raises(CorruptRecord, match="hash mismatch"):
        store.load_unit("u1")


def test_reserved_attempt_survives_restart_and_is_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    from engine.schemas.v23 import RequestManifest, Usage

    manifest = RequestManifest(
        request_id="r1",
        stage="translation",
        unit_ids=("u1",),
        item_ids=("i1",),
        plan_epochs={"u1": 0},
        revisions={"u1": 0},
        input_hashes={"u1": "4" * 64},
        wire_hash="5" * 64,
    )
    store.write_request(manifest)
    attempt = Attempt(
        attempt_id="a1",
        affected_items=("i1",),
        reservation={"estimated_tokens": 100},
        created_at="2026-09-28T00:00:00Z",
    )
    reserved = store.reserve_attempt("r1", attempt)
    assert store.reserve_attempt("r1", attempt) == reserved
    assert Store(tmp_path).read_request("r1").attempts == (attempt,)

    finished = Store(tmp_path).finish_attempt(
        "r1",
        "a1",
        state="unknown",
        usage=Usage(input_tokens=100),
        sent_at="2026-09-28T00:00:01Z",
        metadata={"finish_reason": "stop", "response_id": "response-1"},
    )
    assert finished.attempts[0].state == "unknown"
    assert finished.attempts[0].reservation == {"estimated_tokens": 100}
    assert finished.attempts[0].metadata == {"finish_reason": "stop", "response_id": "response-1"}


def test_ready_bookplan_fixes_document_hashes_and_unit_inventory(tmp_path: Path) -> None:
    store = Store(tmp_path)
    document_hash = store.write_document(_document())
    store.initialize_unit(_unit(), _cut_plan(), source_hash="c" * 64)
    plan = BookPlan(
        source_hash="c" * 64,
        source_path="/input/book.epub",
        source_epub_version="3.0",
        run_id="run1",
        preparation_state="ready",
        resources={"OEBPS/ch1.xhtml": _resource()},
        reading_order=("d1",),
        document_hashes={"d1": document_hash},
        unit_ids=("u1",),
        unit_documents={"u1": "d1"},
        required_unit_count=1,
        output_policy_hash="6" * 64,
    )
    store.write_bookplan(plan)
    assert store.read_bookplan(ready=True) == plan

    (tmp_path / "documents" / "d1.json").write_text("{}")
    with pytest.raises(CorruptRecord):
        store.write_bookplan(plan)


def test_building_document_replacement_only_changes_derived_preparation_fields(tmp_path: Path) -> None:
    from engine.schemas.v23 import RequestManifest

    store = Store(tmp_path)
    store.write_bookplan(
        BookPlan(
            source_hash="c" * 64,
            source_path="/input/book.epub",
            source_epub_version="3.0",
            run_id="run1",
            required_unit_count=0,
            output_policy_hash="6" * 64,
        )
    )
    document = _document()
    store.write_document(document)
    replacement = document.model_copy(update={"derived_bindings": ({"kind": "derived_navigation"},)})
    assert store.replace_building_document(replacement) == canonical_hash(replacement)
    assert store.read_document("d1") == replacement

    changed_source = replacement.model_copy(update={"source_markup": "<html/>"})
    with pytest.raises(IdentityMismatch, match="frozen source plan"):
        store.replace_building_document(changed_source)

    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="translation",
            unit_ids=(),
            item_ids=(),
            plan_epochs={},
            revisions={},
            input_hashes={},
            wire_hash="5" * 64,
        )
    )
    with pytest.raises(StaleWrite, match="request creation"):
        store.replace_building_document(replacement)


def test_ready_bookplan_rejects_record_with_wrong_source_identity(tmp_path: Path) -> None:
    store = Store(tmp_path)
    document_hash = store.write_document(_document())
    cut_plan = _cut_plan()
    store.initialize_unit(
        UnitRecord(
            unit_id="u1",
            document_id="d1",
            source_hash="c" * 64,
            logical_hash="wrong",
            input_hash=compute_input_hash("wrong", cut_plan.plan_hash),
            plan_epoch=0,
            cut_plan=cut_plan,
            items={
                segment.item_id: ItemRecord(item_id=segment.item_id, segment_id=segment.segment_id)
                for segment in cut_plan.segments
            },
        )
    )
    plan = BookPlan(
        source_hash="c" * 64,
        source_path="/input/book.epub",
        source_epub_version="3.0",
        run_id="run1",
        preparation_state="ready",
        resources={"OEBPS/ch1.xhtml": _resource()},
        reading_order=("d1",),
        document_hashes={"d1": document_hash},
        unit_ids=("u1",),
        unit_documents={"u1": "d1"},
        required_unit_count=1,
        output_policy_hash="6" * 64,
    )
    with pytest.raises(IdentityMismatch, match="source identity mismatch"):
        store.write_bookplan(plan)


def test_ready_run_cannot_recreate_a_missing_unit_with_fresh_counters(tmp_path: Path) -> None:
    store = Store(tmp_path)
    document_hash = store.write_document(_document())
    store.initialize_unit(_unit(), _cut_plan(), source_hash="c" * 64)
    store.write_bookplan(
        BookPlan(
            source_hash="c" * 64,
            source_path="/input/book.epub",
            source_epub_version="3.0",
            run_id="run1",
            preparation_state="ready",
            resources={"OEBPS/ch1.xhtml": _resource()},
            reading_order=("d1",),
            document_hashes={"d1": document_hash},
            unit_ids=("u1",),
            unit_documents={"u1": "d1"},
            required_unit_count=1,
            output_policy_hash="6" * 64,
        )
    )
    (tmp_path / "units" / "u1.json").unlink()
    with pytest.raises(StaleWrite, match="after BookPlan is ready"):
        store.initialize_unit(_unit(), _cut_plan(), source_hash="c" * 64)


def test_explicit_document_repair_requires_exact_registered_bytes_and_preserves_unit_state(tmp_path: Path) -> None:
    store = Store(tmp_path)
    document = _document()
    document_hash = store.write_document(document)
    store.initialize_unit(_unit(), _cut_plan(), source_hash="c" * 64)
    store.write_bookplan(
        BookPlan(
            source_hash="c" * 64,
            source_path="/input/book.epub",
            source_epub_version="3.0",
            run_id="run1",
            preparation_state="ready",
            resources={"OEBPS/ch1.xhtml": _resource()},
            reading_order=("d1",),
            document_hashes={"d1": document_hash},
            unit_ids=("u1",),
            unit_documents={"u1": "d1"},
            required_unit_count=1,
            output_policy_hash="6" * 64,
        )
    )
    document_path = tmp_path / "documents" / "d1.json"
    unit_path = tmp_path / "units" / "u1.json"
    unit_before = unit_path.read_bytes()
    bookplan_before = (tmp_path / "bookplan.json").read_bytes()

    document_path.write_text(json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2))
    with pytest.raises(CorruptRecord, match="document hash mismatch"):
        store.read_document("d1", expected_hash=document_hash)
    assert store.restore_document_exact(document) == document_hash
    assert unit_path.read_bytes() == unit_before
    assert (tmp_path / "bookplan.json").read_bytes() == bookplan_before

    wrong = document.model_copy(update={"preparation_issues": ({"code": "different"},)})
    with pytest.raises(IdentityMismatch, match="rebuilt DocumentPlan hash mismatch"):
        store.restore_document_exact(wrong)

    document_path.unlink()
    assert store.restore_document_exact(document) == document_hash
    assert store.read_document("d1", expected_hash=document_hash) == document


def test_resume_isolates_record_whose_self_hash_is_valid_but_source_identity_is_wrong(tmp_path: Path) -> None:
    store = Store(tmp_path)
    document_hash = store.write_document(_document())
    store.initialize_unit(_unit(), _cut_plan(), source_hash="c" * 64)
    store.write_bookplan(
        BookPlan(
            source_hash="c" * 64,
            source_path="/input/book.epub",
            source_epub_version="3.0",
            run_id="run1",
            preparation_state="ready",
            resources={"OEBPS/ch1.xhtml": _resource()},
            reading_order=("d1",),
            document_hashes={"d1": document_hash},
            unit_ids=("u1",),
            unit_documents={"u1": "d1"},
            required_unit_count=1,
            output_policy_hash="6" * 64,
        )
    )
    record = store.load_unit("u1")
    tampered = record.model_copy(update={"logical_hash": "wrong", "input_hash": "wrong", "record_hash": None})
    tampered = tampered.model_copy(update={"record_hash": unit_record_hash(tampered)})
    (tmp_path / "units" / "u1.json").write_bytes(canonical_json_bytes(tampered))

    valid, invalid = store.scan_units(("u1",))
    assert valid == {}
    assert "source identity mismatch" in invalid["u1"]


def test_non_completed_run_cannot_claim_an_output() -> None:
    with pytest.raises(ValidationError):
        RunResult(
            outcome="failed",
            run_id="run1",
            work_dir="/work/run1",
            output_path="/output/book.epub",
            output_hash="7" * 64,
        )

    completed = RunResult(
        outcome="completed",
        run_id="run1",
        work_dir="/work/run1",
        output_path="/output/book.epub",
        output_hash="7" * 64,
    )
    serialized = strict_json_loads(canonical_json_bytes(completed))
    assert isinstance(serialized, dict)
    assert serialized["status"] == "completed"
    assert serialized["output_sha256"] == "7" * 64
    assert "outcome" not in serialized
