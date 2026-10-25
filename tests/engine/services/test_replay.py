from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import engine.agents.workflow as workflow_module
from engine.agents.workflow import _translate_step, run_workflow
from engine.execution.atomic import _pending_batches
from engine.execution.recovery import retry_partitions
from engine.schemas.contracts import ItemStatus, canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import StaleWrite
from engine.services.failures import failure_count, isolate_retry
from engine.services.journal import BodyJournal
from tests.engine.agents.test_workflow import ReadyCase, prepare_case
from tests.engine.services.test_journal import _answer, _compact_case, _tree

_GROUNDED_SOURCE = "A Google search for “attention is not all you need” yields 583,000 results."
_GROUNDED_TARGET = "谷歌搜索“attention is not all you need”可得到 583,000 个结果。"


def _legacy_residual_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: str = _GROUNDED_TARGET,
    siblings: bool = False,
    malformed: bool = False,
) -> tuple[ReadyCase, BodyJournal, str, list[str]]:
    sources = (_GROUNDED_SOURCE, "Second sentence.") if siblings else (_GROUNDED_SOURCE,)
    markup = "".join(f"<p>{source}</p>" for source in sources)
    case = prepare_case(tmp_path, markup, sources)
    journal = BodyJournal(case.session.store, case.session)
    failed_id = case.batch.items[0].item_id
    calls: list[str] = []
    current_error = workflow_module._target_error

    def legacy_error(member, translated, wire):
        if member.item_id == failed_id:
            return "untranslated English remains: attention is not all you need"
        return current_error(member, translated, wire)

    async def transport(kind, payload):
        calls.append(kind)
        if kind == "review":
            return _answer(kind, payload)
        items = [
            {
                "item_id": item["item_id"],
                "target": target if item["item_id"] == failed_id else "第二句。",
            }
            for item in payload["items"]
        ]
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-text-invalid" if malformed else "epubox-text-1",
                    "request_id": payload["request_id"],
                    "items": items,
                }
            ),
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    with monkeypatch.context() as legacy:
        legacy.setattr(workflow_module, "_target_error", legacy_error)
        result = asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=transport),
                session=journal.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    assert result.status == "needs_attention"
    return case, journal, failed_id, calls


def test_recover_results_revalidates_legacy_residual_failure_without_retranslation_or_fake_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, journal, item_id, calls = _legacy_residual_failure(tmp_path, monkeypatch, siblings=True)
    failed = journal.records((item_id,))[item_id]
    reviewed = next(
        record for other_id, record in journal.records(case.batch.manifest.item_ids).items() if other_id != item_id
    )
    ledger_before = _tree(case.session.store.root / "failures")
    attempts_before = journal.progress_snapshot()["body_http_attempts"]
    assert failed.status == ItemStatus.NEEDS_ATTENTION
    assert failed.stage == "translate" and failed.target_projection is None
    assert failed.request_id == case.batch.manifest.request_id
    assert reviewed.status == ItemStatus.REVIEWED
    recovered_checks = {
        key: value for key, value in failed.checks.items() if key in {"translation_frame", "translation_epoch"}
    }
    ordinary_save = failed.model_copy(
        update={
            "stage": "proofread",
            "status": ItemStatus.LOCAL_VALID,
            "target_projection": _GROUNDED_TARGET,
            "target_hash": canonical_hash(_GROUNDED_TARGET),
            "checks": recovered_checks,
            "failure": None,
            "next_action": "review",
        }
    )
    with pytest.raises(StaleWrite, match="terminal"):
        journal.save_many((ordinary_save,))

    resumed = BodyJournal(case.session.store)
    resumed.recover_results()

    recovered = resumed.records((item_id,))[item_id]
    assert recovered.status == ItemStatus.LOCAL_VALID
    assert recovered.stage == "proofread"
    assert recovered.target_projection == _GROUNDED_TARGET
    assert recovered.next_action == "review" and recovered.failure is None
    assert resumed.records((reviewed.item_id,))[reviewed.item_id] == reviewed
    assert _tree(case.session.store.root / "failures") == ledger_before
    assert resumed.progress_snapshot()["body_http_attempts"] == attempts_before
    assert all(item_id not in request.item_ids for request in resumed._requests.values() if request.stage == "review")

    async def review_only(kind, payload):
        calls.append(kind)
        assert kind == "review"
        return _answer(kind, payload)

    completed = asyncio.run(
        run_workflow(
            resumed.session.prepared,
            case.batch,
            resumed.session.index,
            resumed.runtime(transport=review_only),
            session=resumed.session,
            save=resumed.save,
            records=resumed.records(case.batch.manifest.item_ids),
        )
    )
    assert completed.status == "completed"
    assert calls.count("translate") == 1
    assert calls[-1] == "review"
    assert resumed.records((item_id,))[item_id].status == ItemStatus.REVIEWED
    assert _tree(case.session.store.root / "failures") == ledger_before


@pytest.mark.parametrize(
    "counterproof",
    ("still-invalid", "protocol-invalid", "unrelated-failure", "stale-epoch", "old-request"),
)
def test_recover_results_keeps_unproven_legacy_translation_failure_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counterproof: str
) -> None:
    case, journal, item_id, _calls = _legacy_residual_failure(
        tmp_path,
        monkeypatch,
        target=(
            "This replacement remains entirely untranslated and contains a complete English sentence."
            if counterproof == "still-invalid"
            else _GROUNDED_TARGET
        ),
        malformed=counterproof == "protocol-invalid",
    )
    prior = journal.records((item_id,))[item_id]
    if counterproof == "stale-epoch":
        request = journal._requests[prior.request_id or ""]
        epoch = request.record_versions[case.batch.items[0].unit_id]
        prior = prior.model_copy(update={"checks": dict(prior.checks) | {"translation_epoch": epoch + 1}})
    elif counterproof == "old-request":
        prior = prior.model_copy(update={"request_id": "tx-obsolete-request"})
    elif counterproof == "unrelated-failure":
        message = "required term is missing: tokenizer"
        prior = prior.model_copy(
            update={
                "checks": dict(prior.checks) | {"error": message},
                "failure": {"stage": "translate", "code": "translate_failed", "message": message},
            }
        )
    if counterproof in {"stale-epoch", "old-request", "unrelated-failure"}:
        journal.store._base.atomic_write_bytes(
            journal.store.root / "results" / f"{item_id}.json", canonical_json_bytes(prior)
        )
        journal._records[item_id] = prior
    result_path = journal.store.root / "results" / f"{item_id}.json"
    before = result_path.read_bytes()
    ledger_before = _tree(case.session.store.root / "failures")
    attempts_before = journal.progress_snapshot()["body_http_attempts"]

    journal.recover_results()

    assert result_path.read_bytes() == before
    assert journal.records((item_id,))[item_id] == prior
    assert _tree(case.session.store.root / "failures") == ledger_before
    assert journal.progress_snapshot()["body_http_attempts"] == attempts_before


def test_recovery_rolls_back_then_commits_all_response_siblings_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    first_calls: list[str] = []

    def crash_before_results(_records) -> None:
        raise RuntimeError("crash before result commit")

    monkeypatch.setattr(journal, "save_many", crash_before_results)

    async def first(kind, payload):
        first_calls.append(kind)
        return _answer(kind, payload)

    with pytest.raises(RuntimeError, match="before result commit"):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=first),
                session=case.session,
                save=journal.save,
                records=journal.records(case.batch.manifest.item_ids),
            )
        )
    assert first_calls == ["translate"]

    resumed = BodyJournal(case.session.store)
    before_disk = (case.session.store.root / "state.json").read_bytes()
    before_records = resumed.records()
    before_progress = resumed.progress_snapshot()
    original_commit = state._commit

    def fail_commit(_root, _value) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(state, "_commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        resumed.recover_results()
    assert (case.session.store.root / "state.json").read_bytes() == before_disk
    assert resumed.records() == before_records
    assert resumed.progress_snapshot() == before_progress

    previous = {
        key: value["data"] for key, value in json.loads(before_disk)["records"].items() if key.startswith("results/")
    }
    commits: list[int] = []

    def count_commit(root, value) -> None:
        current = {key: record["data"] for key, record in value["records"].items() if key.startswith("results/")}
        commits.append(sum(previous.get(key) != data for key, data in current.items()))
        original_commit(root, value)

    monkeypatch.setattr(state, "_commit", count_commit)
    recovered = resumed.recover_results()
    assert commits == [len(case.batch.items)]
    assert all(recovered[item.item_id].status == ItemStatus.LOCAL_VALID for item in case.batch.items)

    replay_calls: list[str] = []

    async def review_only(kind, payload):
        replay_calls.append(kind)
        assert kind == "review"
        return _answer(kind, payload)

    monkeypatch.setattr(resumed, "save_many", crash_before_results)
    with pytest.raises(RuntimeError, match="before result commit"):
        asyncio.run(
            run_workflow(
                resumed.session.prepared,
                case.batch,
                resumed.session.index,
                resumed.runtime(transport=review_only),
                session=resumed.session,
                save=resumed.save,
                records=resumed.records(case.batch.manifest.item_ids),
            )
        )
    assert replay_calls == ["review"]

    commits.clear()
    reviewed = BodyJournal(case.session.store)
    recovered = reviewed.recover_results()
    assert commits == [len(case.batch.items)]
    assert all(recovered[item.item_id].status == ItemStatus.REVIEWED for item in case.batch.items)

    async def forbidden(*_args):
        raise AssertionError("durable responses must not trigger another transport call")

    result = asyncio.run(
        run_workflow(
            reviewed.session.prepared,
            case.batch,
            reviewed.session.index,
            reviewed.runtime(transport=forbidden),
            session=reviewed.session,
            save=reviewed.save,
            records=reviewed.records(case.batch.manifest.item_ids),
        )
    )
    assert result.status == "completed"


@pytest.mark.parametrize("failed_stage", ["translate", "review"])
def test_recovered_content_failure_is_counted_once_and_keeps_its_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str
) -> None:
    case = _compact_case(tmp_path)
    journal = BodyJournal(case.session.store, case.session)
    item_ids = case.batch.manifest.item_ids
    if failed_stage == "review":

        async def valid(kind, payload):
            return _answer(kind, payload)

        asyncio.run(
            _translate_step(
                case.batch,
                journal.runtime(transport=valid),
                session=case.session,
                save=journal.save,
                records=journal.records(item_ids),
            )
        )
        for record in journal.records(item_ids).values():
            journal.save(record.model_copy(update={"checks": dict(record.checks) | {"review_request_id": "tx-old"}}))

    async def failing(kind, payload):
        response = _answer(kind, payload)
        body = json.loads(response["raw"])
        if kind == "translate":
            for item in body["items"]:
                item["target"] = ""
        else:
            for item in body["items"]:
                item["decision"] = "needs_attention"
        response["raw"] = json.dumps(body)
        return response

    def crash_before_results(_records) -> None:
        raise RuntimeError("crash before failed result commit")

    monkeypatch.setattr(journal, "save_many", crash_before_results)
    with pytest.raises(RuntimeError, match="before failed result commit"):
        asyncio.run(
            run_workflow(
                case.prepared,
                case.batch,
                case.index,
                journal.runtime(transport=failing),
                session=case.session,
                save=journal.save,
                records=journal.records(item_ids),
            )
        )

    resumed = BodyJournal(case.session.store)
    recovered = resumed.recover_results()
    requests = [request for request in resumed._requests.values() if request.stage == failed_stage]
    assert len(requests) == 1
    current_request = requests[0]
    for item_id in item_ids:
        record = recovered[item_id]
        assert record.status == ItemStatus.NEEDS_ATTENTION
        assert failure_count(resumed, record, failed_stage) == 1
        if failed_stage == "review":
            assert record.checks["review_request_id"] == current_request.request_id
            assert record.checks["review_request_id"] != "tx-old"
        else:
            assert record.request_id == current_request.request_id

    resumed.recover_results()
    for item_id in item_ids:
        record = resumed.records((item_id,))[item_id]
        assert failure_count(resumed, record, failed_stage) == 1


def test_third_recovered_merged_failure_switches_to_singleton_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _compact_case(tmp_path)
    item_ids = case.batch.manifest.item_ids
    unit_ids = tuple(dict.fromkeys(item.unit_id for item in case.batch.items))
    journal = BodyJournal(case.session.store)
    batch = case.batch

    async def failing(kind, payload):
        assert kind == "translate"
        response = _answer(kind, payload)
        body = json.loads(response["raw"])
        for item in body["items"]:
            item["target"] = ""
        response["raw"] = json.dumps(body)
        return response

    def crash_before_failed_results(_records) -> None:
        raise RuntimeError("crash before failed result commit")

    for expected in range(1, 4):
        with monkeypatch.context() as scoped:
            scoped.setattr(journal, "save_many", crash_before_failed_results)
            with pytest.raises(RuntimeError, match="before failed result commit"):
                asyncio.run(
                    run_workflow(
                        journal.session.prepared,
                        batch,
                        journal.session.index,
                        journal.runtime(transport=failing),
                        session=journal.session,
                        save=journal.save,
                        records=journal.records(item_ids),
                    )
                )
        journal = BodyJournal(case.session.store)
        journal.recover_results()
        records = journal.records(item_ids)
        counts = {item_id: failure_count(journal, record, "translate") for item_id, record in records.items()}
        assert set(counts.values()) == {expected}, counts
        if expected < 3:
            assert set(journal.retry_units(unit_ids)) == set(item_ids)
            batches = _pending_batches(
                journal,
                (case.batch,),
                selected=set(item_ids),
                partitions=(item_ids,),
                output_unlimited=True,
            )
            batch = next(value for value in batches if value.manifest.item_ids == item_ids)

    records = journal.records(item_ids)
    assert all(isolate_retry(journal, record) for record in records.values())
    assert retry_partitions(journal, item_ids, output_unlimited=True) == tuple((item_id,) for item_id in item_ids)
