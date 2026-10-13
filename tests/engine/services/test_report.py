import json
from pathlib import Path

from engine.schemas.contracts import Attempt, RequestManifest, TermExtractionRecord, Usage
from engine.services.report import write_report
from tests.engine.services.test_store import _prepare, _write_term_plan


def test_paused_preparation_report_does_not_invent_book_or_cost(tmp_path: Path) -> None:
    store, _ = _prepare(tmp_path)

    report_path = write_report(store, status="paused", phase="terms", reason="run limit")
    report = json.loads(report_path.read_text())

    assert report["status"] == "paused"
    assert report["source_units"] == 1
    assert report["required_units"] is None
    assert report["accepted_units"] == 0
    assert report["http"]["actual_attempts"] == 0
    assert report["output_path"] is None
    assert report["reader_check"] == "not_run"


def test_replaceable_report_with_invalid_display_json_is_regenerated(tmp_path: Path) -> None:
    store, _ = _prepare(tmp_path)
    (store.root / "report.json").write_text("{broken")
    report = json.loads(write_report(store, status="paused", phase="terms").read_text())
    assert report["status"] == "paused"
    assert "source_validation" not in report


def test_report_exposes_model_input_bounds_and_json_paths(tmp_path: Path) -> None:
    store, preparation = _prepare(tmp_path)
    item = _write_term_plan(store, preparation).items[0]
    store.save_extraction(
        TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
        )
    )
    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="terms",
            owner_kind="extraction_item",
            owner_id=item.item_id,
            item_ids=(item.item_id,),
            input_hashes={item.item_id: item.extraction_input_hash},
            wire_hash="wire",
        )
    )
    store.reserve_attempt(
        "r1",
        Attempt(
            attempt_id="a1",
            affected_items=(item.item_id,),
            reservation={
                "estimated_input_tokens": 1400,
                "rendered_input_bytes": 1200,
                "input_budget_algorithm_version": 1,
            },
            created_at="now",
        ),
    )
    store.finish_attempt("r1", "a1", state="succeeded", usage=Usage(input_tokens=1300, output_tokens=50))

    report = json.loads(write_report(store, status="paused", phase="terms").read_text())

    assert report["http"]["max_known_input_tokens"] == 1300
    assert report["http"]["max_preflight_input_bound"] == 1400
    assert report["http"]["max_rendered_utf8_bytes"] == 1200
    assert report["http"]["input_budget_algorithm_version"] == 1
    assert report["http"]["preflight_limit_kind"] == "conservative_local_bound_not_provider_exact"
    assert report["http"]["input_limit_violations"] == 0
    assert report["http"]["by_stage"]["terms"]["max_items_per_request"] == 1
    assert report["json_paths"]["units"] == str(store.root / "units")


def test_report_counts_journaled_usage_before_attempt_finish(tmp_path: Path) -> None:
    store, preparation = _prepare(tmp_path)
    item = _write_term_plan(store, preparation).items[0]
    store.save_extraction(
        TermExtractionRecord(
            item_id=item.item_id,
            document_id=item.document_id,
            view_ids=item.view_ids,
            extraction_input_hash=item.extraction_input_hash,
        )
    )
    store.write_request(
        RequestManifest(
            request_id="r1",
            stage="terms",
            owner_kind="extraction_item",
            owner_id=item.item_id,
            item_ids=(item.item_id,),
            input_hashes={item.item_id: item.extraction_input_hash},
            wire_hash="wire",
        )
    )
    store.reserve_attempt("r1", Attempt(attempt_id="a1", affected_items=(item.item_id,), created_at="now"))
    store.finish_attempt("r1", "a1", state="sent")
    store.save_model_response(
        "terms",
        "r1",
        "a1",
        {
            "raw": "{}",
            "finish_reason": "stop",
            "usage": {"input_tokens": 50_001, "output_tokens": 1, "total_tokens": 50_002},
            "metadata": {},
        },
    )

    report = json.loads(write_report(store, status="paused", phase="terms").read_text())

    assert report["http"]["max_known_input_tokens"] == 50_001
    assert report["http"]["input_limit_violations"] == 1
    assert report["http"]["journal_recovered_usage_attempts"] == 1


def test_atomic_report_reads_lightweight_progress_without_workflow_recovery(tmp_path: Path, monkeypatch) -> None:
    from engine.services.journal import BodyJournal
    from engine.services.ready import ReadySession
    from tests.engine.services.test_ready import prepared

    store, ready = prepared(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a display report must not rebuild verified workflow state")

    monkeypatch.setattr(BodyJournal, "__init__", forbidden)
    monkeypatch.setattr(ReadySession, "__init__", forbidden)
    report = json.loads(write_report(store, status="paused", phase="translation").read_text())

    assert not (store.root / "bookplan.json").exists()
    assert report["required_units"] == ready.plan.required_unit_count
    assert report["accepted_units"] == 0
    assert report["pending_items"] == len(ready.plan.member_hashes)
    assert report["body"]["required_items"] == len(ready.plan.member_hashes)
    assert report["body"]["http_attempts"] == 0
    assert report["json_paths"]["results"] == str(store.root / "results")


def test_atomic_report_rejects_result_ownership_change_without_workflow_recovery(
    tmp_path: Path,
) -> None:
    import pytest

    from engine.services.atomic import IdentityMismatch
    from tests.engine.services.test_ready import prepared

    store, _ = prepared(tmp_path)
    path = next((store.root / "results").glob("*.json"))
    value = json.loads(path.read_text())
    value["item_id"] = "another-item"
    path.write_text(json.dumps(value))

    with pytest.raises(IdentityMismatch, match="ownership changed"):
        write_report(store, status="paused", phase="translation")


def test_compact_proof_fingerprint_tracks_nested_proof_json_only(tmp_path: Path) -> None:
    from engine.services import state
    from engine.services.context import proof_fingerprint
    from tests.engine.services.test_ready import compact_prepared

    store, _ = compact_prepared(tmp_path)
    before = proof_fingerprint(store.root)
    state.write(store.root / "responses" / "review" / "request" / "attempt.json", b"{}")
    changed = proof_fingerprint(store.root)
    assert changed != before

    state.write(store.root / "report.json", b'{"status":"paused"}')
    assert proof_fingerprint(store.root) == changed


def test_compact_progress_cache_is_invalidated_by_results_not_report_writes(tmp_path, monkeypatch):
    from engine.schemas.contracts import ItemStatus, canonical_json_bytes
    from engine.services import report as report_module
    from engine.services import state
    from tests.engine.services.test_ready import compact_prepared

    store, prepared = compact_prepared(tmp_path)
    original = report_module._read_atomic_progress
    reads = []

    def read(store):
        reads.append(store.root)
        return original(store)

    monkeypatch.setattr(report_module, "_read_atomic_progress", read)
    first, _ = report_module._atomic_progress(store)
    first["accepted_units"] = 999  # Caller mutation cannot poison subsequent reports.
    state.write(store.root / "report.json", b"{}")
    second, _ = report_module._atomic_progress(store)
    assert second["accepted_units"] == 0 and len(reads) == 1
    item_id = next(iter(prepared.plan.member_hashes))
    path = store.root / "results" / f"{item_id}.json"
    record = json.loads(state.read(path))
    record["status"] = ItemStatus.NEEDS_ATTENTION
    state.write(path, canonical_json_bytes(record))
    third, _ = report_module._atomic_progress(store)
    assert third["needs_attention_units"] == 1 and len(reads) == 2
