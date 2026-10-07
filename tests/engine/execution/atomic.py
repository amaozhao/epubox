from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from engine.item.inline import events_to_projection, parse_projection
from engine.orchestrator import run_translation
from engine.schemas.internal import Event
from engine.services.journal import BodyJournal
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


def test_real_provider_pauses_legacy_output_budget_without_dispatch(tmp_path, monkeypatch):
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    monkeypatch.setattr(BodyJournal, "runtime", lambda *args, **kwargs: object())
    result = asyncio.run(run_translation(case.session.store.root))
    assert result.status == "paused"
    assert "output budget v2" in (result.reason or "")
    assert result.http_attempts == 0
