from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from engine.item.inline import events_to_projection, parse_projection
from engine.orchestrator import run_translation
from engine.schemas.internal import Event
from engine.services.journal import BodyJournal
from engine.services.session import reopen
from main import _progress_printer
from tests.engine.agents.workflow import prepare_case, review_item


def answer(kind, payload):
    if kind == "translate":
        items = [
            {
                "item_id": item["item_id"],
                "target": events_to_projection(
                    Event(kind="text", value="译文。") if event.kind == "text" and event.value.strip() else event
                    for event in parse_projection(item["source"])
                ),
            }
            for item in payload["items"]
        ]
        protocol = "epubox-text-1"
    else:
        items = [review_item(item, decision="no_change") for item in payload["items"]]
        protocol = "epubox-review-2"
    return {
        "raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items}),
        "usage": {"input_tokens": 7, "output_tokens": 5},
    }


def test_shared_execution_entry_persists_every_batch_and_resume_sends_nothing(tmp_path, capsys) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    calls = []
    events = []

    async def transport(kind, payload):
        calls.append((kind, payload["request_id"]))
        return answer(kind, payload)

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
            progress=events.append,
        )
    )
    assert result.status == "translated"
    assert result.accepted_units == case.prepared.plan.required_unit_count
    assert result.http_attempts == len(calls)
    assert any(event.get("request_id") for event in events)
    assert any(event.get("reserved_output_tokens") for event in events)
    printer = _progress_printer()
    for event in events:
        printer(event)
    summaries = [line for line in capsys.readouterr().out.splitlines() if "批次=" in line]
    assert len(summaries) == len(case.prepared.plan.batch_hashes)
    assert all(line.startswith("workflow:") and "结果=通过" in line for line in summaries)
    total = len(case.session.index.members)
    assert f"初译={total}，校对={total}" in summaries[-1]
    before = len(calls)
    resumed = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
        )
    )
    assert resumed.status == "translated" and len(calls) == before
    assert resumed.http_attempts == result.http_attempts
    assert len(BodyJournal(case.session.store).parent_targets()) == case.prepared.plan.required_unit_count


def test_shared_runtime_refills_configured_concurrency_without_double_ownership(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    active = 0
    maximum = 0

    async def transport(kind, payload):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return answer(kind, payload)

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
        )
    )
    assert result.status == "translated"
    assert 1 < maximum <= 2


def test_legacy_output_budget_does_not_block_new_unlimited_dispatch(tmp_path, monkeypatch):
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",), output_budget_version=2)
    original = BodyJournal.runtime
    calls = []

    async def transport(kind, payload):
        calls.append(kind)
        return answer(kind, payload)

    monkeypatch.setattr(BodyJournal, "runtime", lambda self, *args, **kwargs: original(self, transport=transport))
    result = asyncio.run(run_translation(case.session.store.root))
    assert result.status == "translated"
    assert "translate" in calls and "review" in calls
    assert result.http_attempts == len(calls)


@pytest.mark.parametrize("finish", ("length", "max_tokens"))
def test_truncated_translation_is_retried_without_accepting_incomplete_json(tmp_path, finish):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    wanted = set(case.batch.manifest.item_ids)
    calls = []
    events = []

    async def transport(kind, payload):
        if wanted & {item["item_id"] for item in payload["items"]}:
            calls.append((kind, payload["request_id"]))
            if len(calls) == 1:
                return {
                    "raw": '{"protocol":"epubox-text-1","items":[{"target":{"1"' + " \n" * 4000,
                    "finish_reason": finish,
                    "usage": {"input_tokens": 100, "output_tokens": 10000},
                }
        return answer(kind, payload)

    result = asyncio.run(run_translation(case.session.store.root, transport=transport, progress=events.append))
    assert result.status == "translated", result.reason
    assert calls[0][0] == "translate"
    assert [kind for kind, _ in calls].count("translate") == 3
    assert [kind for kind, _ in calls].count("review") == 2
    assert len({request_id for _, request_id in calls}) == len(calls)
    failures = [
        event
        for event in events
        if event.get("phase") == "workflow" and event.get("batch_status") == "needs_attention"
    ]
    assert len(failures) == 1 and str(failures[0]["reason"]).count("translation response was truncated") == 1
    journal = BodyJournal(case.session.store)
    assert len(journal.parent_targets()) == case.prepared.plan.required_unit_count
    before = len(calls)
    assert asyncio.run(run_translation(case.session.store.root, transport=transport)).status == "translated"
    assert len(calls) == before


def test_repeated_truncation_stays_an_error_and_preserves_restart_retry(tmp_path):
    from engine.schemas.contracts import ItemStatus

    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    wanted = set(case.batch.manifest.item_ids)
    calls = []

    async def broken(kind, payload):
        if kind == "translate" and wanted & {item["item_id"] for item in payload["items"]}:
            calls.append(payload["request_id"])
            return {"raw": '{"items":[' + " \n" * 4000, "finish_reason": "length"}
        return answer(kind, payload)

    result = asyncio.run(run_translation(case.session.store.root, transport=broken))
    assert result.status == "needs_attention"
    assert len(calls) == 5 and len(set(calls)) == 5
    assert result.reason and result.reason.count("translation response was truncated") == 2
    journal = BodyJournal(case.session.store)
    assert all(
        record.status == ItemStatus.NEEDS_ATTENTION and record.target_projection is None
        for record in journal.records(tuple(wanted)).values()
    )
    assert result.accepted_units == case.prepared.plan.required_unit_count - len(wanted)

    resumed_calls = []

    async def healthy(kind, payload):
        if kind == "translate" and wanted & {item["item_id"] for item in payload["items"]}:
            resumed_calls.append(tuple(item["item_id"] for item in payload["items"]))
        return answer(kind, payload)

    assert set(reopen(case.session.store.root)) == wanted
    resumed = asyncio.run(run_translation(case.session.store.root, transport=healthy))
    assert resumed.status == "translated"
    assert sorted(map(len, resumed_calls)) == [1, 1]


@pytest.mark.parametrize("stage", ("translate", "review"))
def test_truncated_large_batch_is_bisected_until_complete(tmp_path, stage):
    values = tuple(f"Part {number}." for number in range(8))
    case = prepare_case(
        tmp_path,
        "".join(f"<p>{value}</p>" for value in values),
        values,
        output_budget_version=5,
    )
    wanted = set(case.batch.manifest.item_ids)
    calls = []

    async def transport(kind, payload):
        if kind == stage and wanted & {item["item_id"] for item in payload["items"]}:
            calls.append(tuple(item["item_id"] for item in payload["items"]))
            if len(payload["items"]) > 2:
                return {"raw": "{", "finish_reason": "length"}
        return answer(kind, payload)

    result = asyncio.run(run_translation(case.session.store.root, transport=transport))

    assert result.status == "translated", result.reason
    assert len(calls[0]) == 8
    assert sorted(map(len, calls[1:3])) == [4, 4]
    assert len(calls) == 7 and all(len(group) == 2 for group in calls[3:])
    assert len({item for group in calls[3:] for item in group}) == 8


@pytest.mark.parametrize("failure", ("protocol", "fluency", "needs_attention"))
def test_review_errors_retry_review_without_retranslating_valid_drafts(tmp_path, failure):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    wanted = set(case.batch.manifest.item_ids)
    calls = []
    events = []

    async def transport(kind, payload):
        response = answer(kind, payload)
        if wanted & {item["item_id"] for item in payload["items"]}:
            calls.append(kind)
            if calls == ["translate", "review"]:
                if failure == "protocol":
                    response["raw"] = '{"items":[]}'
                else:
                    root = json.loads(response["raw"])
                    if failure == "fluency":
                        root["items"][0]["checks"]["fluency"] = "not_applicable"
                    else:
                        root["items"][0]["decision"] = "needs_attention"
                        root["items"][0]["checks"]["accuracy"] = "fail"
                        root["items"][0]["issues"] = [
                            {"code": "accuracy", "severity": "major", "message": "Incorrect meaning"}
                        ]
                    response["raw"] = json.dumps(root)
        return response

    result = asyncio.run(run_translation(case.session.store.root, transport=transport, progress=events.append))
    assert result.status == "translated", result.reason
    assert calls == ["translate", "review", "review"]
    if failure == "protocol":
        bad = [
            event
            for event in events
            if event.get("phase") == "workflow" and event.get("batch_status") == "needs_attention"
        ]
        assert len(bad) == 1 and str(bad[0]["reason"]).count("response root must contain exactly") == 1
