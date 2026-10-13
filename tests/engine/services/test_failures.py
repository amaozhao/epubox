from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from engine.schemas.contracts import canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch, safe_id
from engine.services.failures import (
    CONTENT_FAILURE_BUDGET,
    IDENTICAL_CONTENT_FAILURE_LIMIT,
    FailureGeneration,
    FailureLedger,
    automatically_reopenable_units,
    deterministic_retry_available,
    failure_budget_available,
    failure_request,
    isolate_retry,
    record_content_failure,
    retryable_units,
    tree_failure_count,
)
from engine.services.journal import BodyJournal
from tests.engine.agents.test_workflow import prepare_case


def test_review_decode_failure_does_not_fingerprint_the_unchanged_input_draft(tmp_path):
    from engine.services.failures import _generation, _response_target, failure_count

    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store)
    member = case.batch.items[0]
    record = journal.records((member.item_id,))[member.item_id].model_copy(
        update={
            "stage": "review",
            "status": "needs_attention",
            "target_projection": "第一。",
            "target_hash": canonical_hash("第一。"),
            "failure": {
                "stage": "review",
                "code": "review_failed",
                "message": "review:tx-example: target decoding failed: target slots require nonempty strings: invalid=['6']",
            },
        }
    )
    raw = json.dumps(
        {
            "items": [
                {
                    "item_id": member.item_id,
                    "decision": "replace",
                    "target": None,
                    "decode_error": "target slots require nonempty strings",
                }
            ]
        }
    )
    assert _response_target(record, "review", raw) is None
    source_hash = canonical_hash(member)
    ledger = FailureLedger(
        item_id=member.item_id,
        entries=(
            FailureGeneration(
                item_id=member.item_id,
                stage="review",
                generation=_generation(record, "review", source_hash),
                source_hash=source_hash,
                target_hash=record.target_hash,
                events=tuple(canonical_hash(i) for i in range(3)),
                fingerprints=(canonical_hash("old-draft"),) * 3,
            ),
        ),
    )
    state.write(case.session.store.root / "failures" / f"{member.item_id}.json", canonical_json_bytes(ledger))
    assert failure_count(journal, record, "review") == 3
    assert deterministic_retry_available(journal, record, "review")
    assert automatically_reopenable_units(
        journal, {member.unit_id: (member.item_id,)}, {member.item_id: record}, {member.unit_id}
    ) == {member.unit_id}


@pytest.mark.parametrize("stage", ["translate", "review"])
@pytest.mark.parametrize("count", [256, 262])
@pytest.mark.parametrize("legacy_limit", [False, True])
def test_only_obsolete_body_item_limit_can_reopen_after_retry_exhaustion(stage, count, legacy_limit, monkeypatch):
    from engine.schemas.contracts import ItemRecord, ItemStatus
    from engine.services import failures

    message = (
        ("items exceeds 256" if stage == "translate" else "items must be an array of at most 256 entries")
        if legacy_limit
        else "response was truncated"
    )
    request_id = "tx-large-index"
    record = ItemRecord(
        item_id="i0",
        segment_id="i0",
        terms_hash="terms",
        context_hash="context",
        stage=stage,
        status=ItemStatus.NEEDS_ATTENTION,
        request_id=request_id,
        failure={"stage": stage, "code": f"{stage}_failed", "message": f"{stage}:{request_id}: {message}"},
    )
    request = SimpleNamespace(stage=stage, item_ids=tuple(f"i{i}" for i in range(count)))
    journal = SimpleNamespace(_requests={request_id: request})
    monkeypatch.setattr(failures, "failure_count", lambda *_args: 10)
    monkeypatch.setattr(failures, "failure_budget_available", lambda *_args: False)
    monkeypatch.setattr(failures, "deterministic_retry_available", lambda *_args: False)

    reopened = automatically_reopenable_units(journal, {"u0": ("i0",)}, {"i0": record}, {"u0"})
    assert reopened == ({"u0"} if legacy_limit and count > 256 else set())


def test_parent_tree_content_failure_budget_is_durable_and_bounded(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First. Second.</p>", ("First. Second.",))
    parent = case.batch.items[0]
    ledger = FailureLedger(
        item_id=parent.item_id,
        entries=(
            FailureGeneration(
                item_id=parent.item_id,
                stage="translate",
                generation=canonical_hash("generation"),
                source_hash=canonical_hash(parent),
                events=tuple(canonical_hash(index) for index in range(CONTENT_FAILURE_BUDGET)),
            ),
        ),
    )
    state.write(
        case.session.store.root / "failures" / f"{safe_id(parent.item_id)}.json",
        canonical_json_bytes(ledger),
    )

    restarted = BodyJournal(case.session.store)
    assert tree_failure_count(restarted, parent.item_id) == CONTENT_FAILURE_BUDGET
    assert not failure_budget_available(restarted, parent.item_id)


def test_legacy_failure_entries_inherit_the_ledger_owner(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    parent = case.batch.items[0]
    legacy = {
        "format": "epubox-content-failures-1",
        "item_id": parent.item_id,
        "entries": [
            {
                "stage": "translate",
                "generation": canonical_hash("legacy-generation"),
                "source_hash": canonical_hash(parent),
                "target_hash": None,
                "events": [canonical_hash("legacy-event")],
            }
        ],
    }
    state.write(
        case.session.store.root / "failures" / f"{safe_id(parent.item_id)}.json",
        canonical_json_bytes(legacy),
    )

    restarted = BodyJournal(case.session.store)
    assert tree_failure_count(restarted, parent.item_id) == 1
    from engine.services.failures import _read

    loaded = _read(restarted, parent.item_id).entries[0]
    assert loaded.merged_events == loaded.singleton_events == ()


def test_review_failure_uses_the_persisted_review_request_identity(tmp_path, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store)
    item_id = case.batch.items[0].item_id
    current_id = "tx-a-current"
    old_id = "tx-z-old"
    record = journal.records((item_id,))[item_id].model_copy(
        update={
            "stage": "review",
            "status": "needs_attention",
            "target_projection": "第一。",
            "target_hash": canonical_hash("第一。"),
            "checks": {
                "accepted": False,
                "error": "accuracy concern",
                "review_request_id": current_id,
            },
            "failure": {
                "stage": "review",
                "code": "review_failed",
                "message": "accuracy concern",
            },
        }
    )
    old_attempt = SimpleNamespace(attempt_id="old-attempt")
    current_attempt = SimpleNamespace(attempt_id="current-attempt")
    old_request = SimpleNamespace(stage="review", request_id=old_id, item_ids=(item_id,), attempts=(old_attempt,))
    current_request = SimpleNamespace(
        stage="review", request_id=current_id, item_ids=(item_id,), attempts=(current_attempt,)
    )
    monkeypatch.setattr(journal, "_requests", {old_id: old_request, current_id: current_request})
    monkeypatch.setattr(
        journal.store,
        "read_model_response",
        lambda _stage, request_id, _attempt_id: SimpleNamespace(raw='{"items": []}') if request_id == old_id else None,
    )

    assert failure_request(journal, record) is current_request
    assert record_content_failure(journal, record) == 0
    assert tree_failure_count(journal, item_id) == 0


def test_ambiguous_legacy_review_request_is_not_guessed(tmp_path, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store)
    item_id = case.batch.items[0].item_id
    record = journal.records((item_id,))[item_id].model_copy(
        update={
            "stage": "review",
            "status": "needs_attention",
            "target_projection": "第一。",
            "target_hash": canonical_hash("第一。"),
            "checks": {"accepted": False, "error": "legacy failure"},
            "failure": {"stage": "review", "code": "review_failed", "message": "legacy failure"},
        }
    )
    monkeypatch.setattr(
        journal,
        "_requests",
        {
            request_id: SimpleNamespace(
                stage="review",
                request_id=request_id,
                item_ids=(item_id,),
                attempts=(SimpleNamespace(attempt_id=f"attempt-{request_id}"),),
            )
            for request_id in ("tx-z-old", "tx-a-current")
        },
    )

    assert failure_request(journal, record) is None


def test_legacy_history_triggers_isolation_without_spending_singleton_budget(tmp_path, monkeypatch) -> None:
    from engine.services.failures import _generation, _mode_events, _mode_fingerprints, _read

    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store)
    member = case.batch.items[0]
    item_id = member.item_id
    request_id = "tx-singleton"
    record = journal.records((item_id,))[item_id].model_copy(
        update={
            "stage": "translate",
            "status": "needs_attention",
            "request_id": request_id,
            "checks": {"accepted": False, "error": "same failure"},
            "failure": {"stage": "translate", "code": "translate_failed", "message": "same failure"},
        }
    )
    events = tuple(canonical_hash(f"legacy-event-{index}") for index in range(3))
    fingerprints = (canonical_hash("legacy-result"),) * 3
    entry = FailureGeneration(
        item_id=item_id,
        stage="translate",
        generation=_generation(record, "translate", canonical_hash(member)),
        source_hash=canonical_hash(member),
        events=events,
        fingerprints=fingerprints,
    )
    state.write(
        case.session.store.root / "failures" / f"{safe_id(item_id)}.json",
        canonical_json_bytes(FailureLedger(item_id=item_id, entries=(entry,))),
    )
    request = SimpleNamespace(
        stage="translate",
        request_id=request_id,
        item_ids=(item_id,),
        attempts=(SimpleNamespace(attempt_id="network-failure"),),
    )
    monkeypatch.setattr(journal, "_requests", {request_id: request})
    monkeypatch.setattr(journal.store, "read_model_response", lambda *_args: None)

    assert _mode_events(entry, request) == (events, ())
    assert _mode_fingerprints(entry, request, singleton=True) == ()
    assert isolate_retry(journal, record)
    assert deterministic_retry_available(journal, record, "translate")
    assert record_content_failure(journal, record) == 3
    assert _read(journal, item_id).entries[0] == entry

    singleton_event = canonical_hash("singleton-event")
    singleton_fingerprint = canonical_hash("singleton-result")
    mixed = entry.model_copy(
        update={
            "events": (*events, singleton_event),
            "fingerprints": (*fingerprints, singleton_fingerprint),
            "singleton_events": (singleton_event,),
            "singleton_fingerprints": (singleton_fingerprint,),
        }
    )
    assert _mode_events(mixed, request) == (events, (singleton_event,))
    assert _mode_fingerprints(mixed, request, singleton=True) == (singleton_fingerprint,)


def test_failure_ledger_rejects_an_explicit_foreign_entry_owner(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    parent = case.batch.items[0]
    ledger = FailureLedger(
        item_id=parent.item_id,
        entries=(
            FailureGeneration(
                item_id="foreign-item",
                stage="translate",
                generation=canonical_hash("generation"),
                source_hash=canonical_hash(parent),
                events=(canonical_hash("event"),),
            ),
        ),
    )
    state.write(
        case.session.store.root / "failures" / f"{safe_id(parent.item_id)}.json",
        canonical_json_bytes(ledger),
    )

    with pytest.raises(IdentityMismatch, match="ownership changed"):
        tree_failure_count(BodyJournal(case.session.store), parent.item_id)


def test_identical_target_and_error_stop_after_bounded_correction_attempts(tmp_path, monkeypatch) -> None:
    case = prepare_case(
        tmp_path, "<p>const index = pc.index('document-search');</p>", ("const index = pc.index('document-search');",)
    )
    journal = BodyJournal(case.session.store)
    item_id = case.batch.items[0].item_id
    initial = journal.records((item_id,))[item_id]
    record = initial.model_copy(
        update={
            "stage": "translate",
            "status": "needs_attention",
            "request_id": "tx-deterministic",
            "checks": {"accepted": False, "error": "untranslated English remains"},
            "failure": {
                "stage": "translate",
                "code": "translate_failed",
                "message": "untranslated English remains",
            },
        }
    )
    responses = {}
    monkeypatch.setattr(
        journal.store,
        "read_model_response",
        lambda _stage, _request_id, attempt_id: responses.get(attempt_id),
    )

    attempts = []
    for index in range(IDENTICAL_CONTENT_FAILURE_LIMIT):
        attempt_id = f"attempt-{index}"
        attempts.append(SimpleNamespace(attempt_id=attempt_id))
        responses[attempt_id] = SimpleNamespace(
            raw=json.dumps(
                {
                    "protocol": "epubox-text-1",
                    "request_id": "tx-deterministic",
                    "items": [{"item_id": item_id, "target": "const index = pc.index('document-search');"}],
                }
            )
        )
        monkeypatch.setattr(
            journal,
            "_requests",
            {
                "tx-deterministic": SimpleNamespace(
                    stage="translate",
                    request_id="tx-deterministic",
                    item_ids=(item_id,),
                    attempts=tuple(attempts),
                )
            },
        )
        record_content_failure(journal, record)

    unit_id = case.batch.items[0].unit_id
    unit_members = {unit_id: (item_id,)}
    records = {item_id: record}
    assert not deterministic_retry_available(journal, record, "translate")
    assert retryable_units(journal, unit_members, records, {unit_id}) == set()
    assert automatically_reopenable_units(journal, unit_members, records, {unit_id}) == set()


def test_a_changed_target_retains_the_content_correction_budget(tmp_path, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>Index</p>", ("Index",))
    journal = BodyJournal(case.session.store)
    item_id = case.batch.items[0].item_id
    initial = journal.records((item_id,))[item_id]
    record = initial.model_copy(
        update={
            "stage": "translate",
            "status": "needs_attention",
            "request_id": "tx-changing",
            "checks": {"accepted": False, "error": "untranslated English remains"},
            "failure": {
                "stage": "translate",
                "code": "translate_failed",
                "message": "untranslated English remains",
            },
        }
    )
    responses = {}
    monkeypatch.setattr(
        journal.store,
        "read_model_response",
        lambda _stage, _request_id, attempt_id: responses.get(attempt_id),
    )
    attempts = []
    for index, target in enumerate(("Index", "索引", "Index")):
        attempt_id = f"attempt-{index}"
        attempts.append(SimpleNamespace(attempt_id=attempt_id))
        responses[attempt_id] = SimpleNamespace(
            raw=json.dumps(
                {
                    "protocol": "epubox-text-1",
                    "request_id": "tx-changing",
                    "items": [{"item_id": item_id, "target": target}],
                }
            )
        )
        monkeypatch.setattr(
            journal,
            "_requests",
            {
                "tx-changing": SimpleNamespace(
                    stage="translate",
                    request_id="tx-changing",
                    item_ids=(item_id,),
                    attempts=tuple(attempts),
                )
            },
        )
        record_content_failure(journal, record)

    assert deterministic_retry_available(journal, record, "translate")


def test_review_request_ids_do_not_hide_an_identical_failure(tmp_path, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store)
    item_id = case.batch.items[0].item_id
    initial = journal.records((item_id,))[item_id]
    responses = {}
    monkeypatch.setattr(
        journal.store,
        "read_model_response",
        lambda _stage, request_id, attempt_id: responses.get((request_id, attempt_id)),
    )

    record = initial
    for index in range(IDENTICAL_CONTENT_FAILURE_LIMIT):
        request_id = f"tx-review-{index}"
        attempt_id = f"attempt-{index}"
        record = initial.model_copy(
            update={
                "stage": "review",
                "status": "needs_attention",
                "target_projection": "第一。",
                "target_hash": canonical_hash("第一。"),
                "checks": {"accepted": False, "error": "same protocol defect"},
                "failure": {
                    "stage": "review",
                    "code": "review_failed",
                    "message": f"review:{request_id}: same protocol defect",
                },
            }
        )
        responses[(request_id, attempt_id)] = SimpleNamespace(
            raw=json.dumps(
                {
                    "protocol": "epubox-review-2",
                    "request_id": request_id,
                    "items": [{"item_id": item_id, "decision": "no_change"}],
                }
            )
        )
        monkeypatch.setattr(
            journal,
            "_requests",
            {
                request_id: SimpleNamespace(
                    stage="review",
                    request_id=request_id,
                    item_ids=(item_id,),
                    attempts=(SimpleNamespace(attempt_id=attempt_id),),
                )
            },
        )
        record_content_failure(journal, record)

    assert not deterministic_retry_available(journal, record, "review")


def test_model_review_needs_attention_does_not_become_a_deterministic_failure(tmp_path, monkeypatch) -> None:
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    journal = BodyJournal(case.session.store)
    item_id = case.batch.items[0].item_id
    initial = journal.records((item_id,))[item_id]
    responses = {}
    monkeypatch.setattr(
        journal.store,
        "read_model_response",
        lambda _stage, request_id, attempt_id: responses.get((request_id, attempt_id)),
    )

    record = initial
    for index in range(IDENTICAL_CONTENT_FAILURE_LIMIT):
        request_id = f"tx-semantic-{index}"
        attempt_id = f"attempt-{index}"
        record = initial.model_copy(
            update={
                "stage": "review",
                "status": "needs_attention",
                "target_projection": "第一。",
                "target_hash": canonical_hash("第一。"),
                "checks": {"accepted": False, "error": "accuracy concern"},
                "failure": {
                    "stage": "review",
                    "code": "review_failed",
                    "message": "[{'code': 'accuracy', 'severity': 'major', 'message': 'accuracy concern'}]",
                },
            }
        )
        responses[(request_id, attempt_id)] = SimpleNamespace(
            raw=json.dumps(
                {
                    "protocol": "epubox-review-2",
                    "request_id": request_id,
                    "items": [{"item_id": item_id, "decision": "needs_attention"}],
                }
            )
        )
        monkeypatch.setattr(
            journal,
            "_requests",
            {
                request_id: SimpleNamespace(
                    stage="review",
                    request_id=request_id,
                    item_ids=(item_id,),
                    attempts=(SimpleNamespace(attempt_id=attempt_id),),
                )
            },
        )
        record_content_failure(journal, record)

    assert deterministic_retry_available(journal, record, "review")
