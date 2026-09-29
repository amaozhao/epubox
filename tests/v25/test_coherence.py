from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from engine.item.extractor import extract_document
from engine.schemas.contracts import UnitRecord, canonical_hash
from engine.services.coherence import (
    add_http_budget,
    load_budget_overrides,
    pending_windows,
    prepare_document_check,
    retry_document_check,
    save_window_result,
)
from engine.services.store import RunStore


def accepted_records(document):
    return {
        unit.unit_id: UnitRecord(
            unit_id=unit.unit_id,
            document_id=document.document_id,
            source_hash=document.source_hash,
            revision=0,
            candidate=unit.source_projection,
            accepted_revision=0,
            accepted_target_hash=canonical_hash(unit.source_projection),
        )
        for unit in document.units
    }


def test_windows_freeze_budget_block_dependencies_and_keep_later_results(tmp_path) -> None:
    markup = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head><body>'
        "<p>First.</p><p>Second.</p><p>Third.</p></body></html>"
    )
    document = extract_document(markup, "chapter.xhtml", "source-sha")
    store = RunStore(tmp_path)
    records = accepted_records(document)
    missing_id = next(unit.unit_id for unit in document.units if "Second" in unit.source_projection)
    records[missing_id] = records[missing_id].model_copy(
        update={"accepted_revision": None, "accepted_target_hash": None}
    )

    blocked = prepare_document_check(store, document, records)
    assert blocked["status"] == "blocked_dependency"
    assert missing_id in blocked["dependency_ids"]
    assert blocked["http_limit"] == 6 * len(blocked["windows"])
    assert pending_windows(blocked) == ()

    records[missing_id] = accepted_records(document)[missing_id]
    pending = prepare_document_check(store, document, records)
    windows = pending_windows(pending)
    assert windows
    initial_limit = pending["http_limit"]
    for index, window in enumerate(windows):
        issues = [{"code": "continuity", "severity": "major", "message": "broken link"}] if index == 0 else []
        pending = save_window_result(store, pending, str(window["item_id"]), issues)

    assert pending["status"] == "needs_attention"
    assert len(pending["checks"]) == len(windows)
    assert pending["http_limit"] == initial_limit
    retried = retry_document_check(store, document.document_id)
    assert retried["status"] == "pending"
    assert len(retried["checks"]) == len(windows) - 1

    limits = add_http_budget(
        store,
        authorization_id="budget-1",
        add_run_http=3,
        add_unit_http={missing_id: 2},
        add_check_http={document.document_id: 1},
    )
    assert limits["add_run_http"] == 3
    assert load_budget_overrides(store)["add_unit_http"] == {missing_id: 2}
    limits = add_http_budget(
        store,
        authorization_id="budget-2",
        add_run_http=1,
        add_unit_http={missing_id: 1},
    )
    assert limits["add_run_http"] == 4
    assert limits["add_unit_http"] == {missing_id: 3}


def test_budget_authorization_is_idempotent_atomic_and_payload_bound(tmp_path) -> None:
    def add_once() -> dict:
        return add_http_budget(RunStore(tmp_path), authorization_id="approval-1", add_run_http=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: add_once(), range(2)))

    assert {value["add_run_http"] for value in results} == {3}
    assert load_budget_overrides(RunStore(tmp_path))["add_run_http"] == 3
    with pytest.raises(ValueError, match="different HTTP budget action"):
        add_http_budget(RunStore(tmp_path), authorization_id="approval-1", add_run_http=4)
    with pytest.raises(ValueError, match="different HTTP budget action"):
        add_http_budget(
            RunStore(tmp_path),
            authorization_id="approval-1",
            action_context_hash="different-repair-file",
            add_run_http=3,
        )
