import asyncio
from types import SimpleNamespace

import pytest

from engine.epub.preparation import PreparationConfig
from engine.orchestrator import run_translation
from engine.schemas.contracts import ItemStatus
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.store import RunStore
from tests.engine.agents.workflow import prepare_case
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer


@pytest.mark.parametrize("stage", ("translate", "review"))
def test_unknown_timeout_skips_only_affected_items_and_translates_later_batches(tmp_path, stage):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p><p>Third.</p>", ("First.", "Second.", "Third."))
    calls = []
    timed_out = []

    async def transport(kind, payload):
        calls.append((kind, payload["request_id"]))
        if kind == stage and not timed_out:
            timed_out.extend(item["item_id"] for item in payload["items"])
            raise TimeoutError("Request timed out.")
        return answer(kind, payload)

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
        )
    )
    expected_status = "translated" if stage == "review" else "needs_attention"
    assert result.status == expected_status
    assert timed_out and result.accepted_units > 0
    assert len({request_id for _, request_id in calls}) >= 3
    records = BodyJournal(case.session.store).records()
    expected_item_status = ItemStatus.REVIEWED if stage == "review" else ItemStatus.NEEDS_ATTENTION
    assert all(records[item].status == expected_item_status for item in timed_out)
    assert any(record.status == ItemStatus.REVIEWED for record in records.values())
    accepted = {key: record for key, record in records.items() if record.status == ItemStatus.REVIEWED}
    before = len(calls)
    again = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
        )
    )
    assert again.status == expected_status and len(calls) == before
    restored = BodyJournal(case.session.store).records()
    assert {key: restored[key] for key in accepted} == accepted


@pytest.mark.parametrize("stage", ("translate", "review"))
def test_three_unknown_body_requests_do_not_pause_later_batches(tmp_path, stage):
    source = make_epub(tmp_path / "source.epub", {f"chapter{index}.xhtml": "<p>Hello.</p>" for index in range(5)})
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="failures",
                auto_extract=False,
                translation_config={
                    "model": "gpt-3.5-turbo",
                    "max_source_tokens": 5000,
                    "max_input_tokens": 50_000,
                    "max_output_tokens": 10_000,
                    "context_tokens": 60_000,
                    "concurrency": 1,
                },
            ),
            StubChecker(),
        )
    )
    assert prepared.status == "ready"
    failed_requests = set()
    failed_items = set()

    async def transport(kind, payload):
        if kind == stage and len(failed_requests) < 3:
            failed_requests.add(payload["request_id"])
            failed_items.update(item["item_id"] for item in payload["items"])
            raise TimeoutError("Request timed out.")
        return answer(kind, payload)

    result = asyncio.run(run_translation(prepared.work_dir, transport=transport))
    assert result.status == ("translated" if stage == "review" else "needs_attention") and len(failed_requests) == 3
    assert result.accepted_units > 0
    records = BodyJournal(RunStore(prepared.work_dir)).records()
    expected_item_status = ItemStatus.REVIEWED if stage == "review" else ItemStatus.NEEDS_ATTENTION
    assert all(records[item].status == expected_item_status for item in failed_items)
    assert any(record.status == ItemStatus.REVIEWED for record in records.values())
