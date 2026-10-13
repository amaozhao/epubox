from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from engine.orchestrator import run_translation
from engine.services import state
from engine.services.failures import failure_count
from engine.services.journal import BodyJournal
from engine.services.partition import plan_path
from tests.engine.agents.test_workflow import prepare_case
from tests.engine.execution.test_atomic import answer
from tests.engine.services.test_partition import _adaptive_case


@pytest.mark.parametrize("failed_stage", ["translate", "review"])
def test_short_list_exhaustion_changes_strategy_and_resume_reuses_completed_results(tmp_path, failed_stage):
    session, parent = _adaptive_case(
        tmp_path, "<ol><li>First step.</li><li>Second step.</li><li>Last step.</li></ol>", "ol"
    )
    calls = []

    async def transport(stage, payload):
        item_ids = tuple(item["item_id"] for item in payload["items"])
        calls.append((stage, item_ids))
        if stage == failed_stage and parent.item_id in item_ids:
            return {"raw": "not JSON", "usage": {"input_tokens": 2, "output_tokens": 1}}
        return answer(stage, payload)

    def run():
        return asyncio.run(
            run_translation(
                session.store.root,
                model=SimpleNamespace(id=session.prepared.plan.translation_config["model"]),
                transport=transport,
                reopen_attention=True,
            )
        )

    result = run()
    assert result.status == "translated", result.reason
    assert sum(stage == failed_stage and parent.item_id in ids for stage, ids in calls) == 10
    child_calls = [(stage, ids) for stage, ids in calls if any(i.startswith(f"pc-{parent.item_id}-") for i in ids)]
    assert child_calls
    assert all(len(ids) == 1 for _stage, ids in child_calls)
    if failed_stage == "review":
        assert all(stage == "review" for stage, _ in child_calls)
    assert BodyJournal(session.store).parts(parent.item_id).plan.strategy == "list_items"
    before = len(calls)
    assert run().status == "translated" and len(calls) == before


def test_tenth_usable_content_failure_switches_to_durable_sentence_parts(tmp_path) -> None:
    case = prepare_case(
        tmp_path,
        "<p>First sentence. Second sentence! Third sentence?</p>",
        ("First sentence. Second sentence! Third sentence?",),
    )
    parent = case.batch.items[0]
    calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    async def malformed(stage, payload):
        item_ids = tuple(item["item_id"] for item in payload["items"])
        calls.append((stage, item_ids, tuple(item["source"] for item in payload["items"])))
        if stage == "translate" and parent.item_id in item_ids:
            return {"raw": "not JSON", "usage": {"input_tokens": 2, "output_tokens": 1}}
        return answer(stage, payload)

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=malformed,
        )
    )

    parent_calls = [ids for stage, ids, _sources in calls if stage == "translate" and parent.item_id in ids]
    child_calls = [
        (ids, sources)
        for stage, ids, sources in calls
        if stage == "translate" and any(item.startswith("pc-") for item in ids)
    ]
    assert len(parent_calls) == 10
    assert child_calls
    assert max(len(source) for _ids, sources in child_calls for source in sources) < len(parent.source_projection)
    assert result.status == "translated", result.reason
    assert state.exists(plan_path(case.session.store.root, parent.item_id))
    plan = BodyJournal(case.session.store).parts(parent.item_id).plan
    assert plan.strategy == "sentences" and plan.trigger_count == 10


def test_network_failures_do_not_consume_the_content_failure_threshold(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>First. Second.</p>", ("First. Second.",))
    parent = case.batch.items[0]

    async def unavailable(_stage, _payload):
        raise TimeoutError("provider unavailable")

    asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=unavailable,
        )
    )

    record = BodyJournal(case.session.store).records((parent.item_id,))[parent.item_id]
    assert failure_count(BodyJournal(case.session.store), record, "translate") == 0
    assert not state.exists(plan_path(case.session.store.root, parent.item_id))


def test_tenth_failure_without_a_safe_split_stops_without_an_eleventh_request(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>One indivisible sentence</p>", ("One indivisible sentence",))
    parent = case.batch.items[0]
    parent_calls = 0

    async def malformed(stage, payload):
        nonlocal parent_calls
        if stage == "translate" and parent.item_id in {item["item_id"] for item in payload["items"]}:
            parent_calls += 1
            return {"raw": "not JSON", "usage": {"input_tokens": 2, "output_tokens": 1}}
        return answer(stage, payload)

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=malformed,
        )
    )

    assert result.status == "needs_attention"
    assert parent_calls == 10
    assert not state.exists(plan_path(case.session.store.root, parent.item_id))

    again = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=malformed,
            reopen_attention=True,
        )
    )
    assert again.status == "needs_attention"
    assert parent_calls == 10


def test_identical_translation_result_stops_after_three_content_failures(tmp_path) -> None:
    source = "This response remains entirely untranslated English prose."
    case = prepare_case(tmp_path, f"<p>{source}</p>", (source,))
    parent = case.batch.items[0]
    parent_calls = 0

    async def unchanged(stage, payload):
        nonlocal parent_calls
        response = answer(stage, payload)
        if stage == "translate" and parent.item_id in {item["item_id"] for item in payload["items"]}:
            parent_calls += 1
            body = json.loads(response["raw"])
            next(item for item in body["items"] if item["item_id"] == parent.item_id)["target"] = source
            response["raw"] = json.dumps(body)
        return response

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=unchanged,
        )
    )

    assert result.status == "needs_attention"
    assert parent_calls == 3
    again = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=unchanged,
            reopen_attention=True,
        )
    )
    assert again.status == "needs_attention"
    assert parent_calls == 3


def test_later_network_failure_does_not_recount_an_older_content_response(tmp_path) -> None:
    case = prepare_case(tmp_path, "<p>One indivisible sentence</p>", ("One indivisible sentence",))
    parent = case.batch.items[0]

    async def malformed(_stage, _payload):
        return {"raw": "not JSON", "usage": {"input_tokens": 2, "output_tokens": 1}}

    asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=malformed,
        )
    )
    before_journal = BodyJournal(case.session.store)
    before_record = before_journal.records((parent.item_id,))[parent.item_id]
    before = failure_count(before_journal, before_record, "translate")
    assert before > 0

    async def unavailable(_stage, _payload):
        raise TimeoutError("provider unavailable")

    asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=unavailable,
            reopen_attention=True,
        )
    )
    after_journal = BodyJournal(case.session.store)
    after_record = after_journal.records((parent.item_id,))[parent.item_id]
    assert failure_count(after_journal, after_record, "translate") == before


def test_tenth_review_failure_splits_cjk_target_without_retranslation(tmp_path) -> None:
    case = prepare_case(
        tmp_path,
        "<p>First sentence. Second sentence! Third sentence?</p>",
        ("First sentence. Second sentence! Third sentence?",),
    )
    parent = case.batch.items[0]
    calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    async def transport(stage, payload):
        item_ids = tuple(item["item_id"] for item in payload["items"])
        sources = tuple(item["source"] for item in payload["items"])
        calls.append((stage, item_ids, sources))
        response = answer(stage, payload)
        body = json.loads(response["raw"])
        if stage == "translate" and parent.item_id in item_ids:
            next(item for item in body["items"] if item["item_id"] == parent.item_id)["target"] = (
                "第一句。第二句！第三句？"
            )
        if stage == "review" and parent.item_id in item_ids:
            decision = next(item for item in body["items"] if item["item_id"] == parent.item_id)
            decision.update(
                decision="needs_attention",
                issues=[{"code": "accuracy", "severity": "major", "message": "split review"}],
            )
        response["raw"] = json.dumps(body, ensure_ascii=False)
        return response

    result = asyncio.run(
        run_translation(
            case.session.store.root,
            model=SimpleNamespace(id=case.prepared.plan.translation_config["model"]),
            transport=transport,
        )
    )

    parent_reviews = [ids for stage, ids, _sources in calls if stage == "review" and parent.item_id in ids]
    child_translations = [
        ids for stage, ids, _sources in calls if stage == "translate" and any(i.startswith("pc-") for i in ids)
    ]
    child_reviews = [
        sources
        for stage, ids, sources in calls
        if stage == "review" and any(item_id.startswith("pc-") for item_id in ids)
    ]
    assert result.status == "translated", result.reason
    assert len(parent_reviews) == 10
    assert not child_translations and child_reviews
    assert max(len(source) for sources in child_reviews for source in sources) < len(parent.source_projection)
