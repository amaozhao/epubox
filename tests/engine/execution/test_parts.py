from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from engine.agents.workflow import _manifest
from engine.execution.atomic import run_atomic
from engine.execution.parts import WithoutRecords, ensure_part_plans, owns_request
from engine.item.budget import request_source_tokens
from engine.schemas.contracts import ItemStatus, canonical_hash
from engine.schemas.parts import PartPlan
from engine.services import state
from engine.services.coherence import add_http_budget
from engine.services.journal import BodyJournal
from engine.services.partition import plan_path
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession
from engine.services.store import RunStore
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.execution.test_atomic import answer
from tests.engine.services.test_partition import _case
from tests.engine.services.test_preparing import atomic_config


def test_part_plans_persist_the_original_parent_record_and_replay(tmp_path) -> None:
    session, parent = _case(tmp_path)
    journal = BodyJournal(session.store, session=session)
    original = journal.records((parent.item_id,))[parent.item_id]

    first = ensure_part_plans(journal)
    second = ensure_part_plans(journal)

    assert len(first) == 1 and second == first
    assert first[0].seed == original
    assert PartPlan.model_validate_json(state.read(plan_path(session.store.root, parent.item_id))) == first[0]


def test_part_parent_can_be_hidden_without_changing_the_root_journal(tmp_path) -> None:
    session, parent = _case(tmp_path)
    journal = BodyJournal(session.store, session=session)

    visible = WithoutRecords(journal, (parent.item_id,)).records()

    assert parent.item_id not in visible
    assert parent.item_id in journal.records()


def test_request_ownership_excludes_child_requests_from_root_retry(tmp_path) -> None:
    session, parent = _case(tmp_path)
    journal = BodyJournal(session.store, session=session)
    plan = ensure_part_plans(journal)[0]

    assert owns_request(journal, SimpleNamespace(item_ids=(parent.item_id,)))
    assert not owns_request(journal, SimpleNamespace(item_ids=(plan.children[0].item_id,)))


def test_atomic_executes_persisted_list_parts_once_under_the_source_limit(tmp_path) -> None:
    session, parent = _case(tmp_path)
    plan = ensure_part_plans(BodyJournal(session.store, session=session))[0]
    calls: list[tuple[str, dict]] = []

    async def transport(stage, payload):
        calls.append((stage, payload))
        return answer(stage, payload)

    first = asyncio.run(run_atomic(session.store.root, transport=transport, ready_session=session))

    assert first.status == "translated", first.reason
    child_ids = {child.item_id for child in plan.children}
    part_calls = [
        payload for _, payload in calls if child_ids.intersection(item["item_id"] for item in payload["items"])
    ]
    assert part_calls
    assert all(parent.item_id not in {item["item_id"] for item in payload["items"]} for _, payload in calls)
    assert all(
        request_source_tokens(payload, str(session.prepared.plan.translation_config["model"])) <= 1500
        for payload in part_calls
    )
    assert (
        BodyJournal(session.store, session=session).records((parent.item_id,))[parent.item_id].status
        == ItemStatus.REVIEWED
    )

    before = len(calls)
    resumed = asyncio.run(run_atomic(session.store.root, transport=transport, ready_session=session))
    assert resumed.status == "translated"
    assert len(calls) == before


def test_atomic_leaves_an_oversized_indivisible_paragraph_for_attention(tmp_path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": f"<p>{'word ' * 1800}</p>"})
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(max_source_tokens=3000, max_input_tokens=50_000, context_tokens=60_000),
            StubChecker(),
        )
    )
    assert prepared.status == "ready"
    session = ReadySession(RunStore(prepared.work_dir))
    parent = next(
        member
        for member in session.index.members
        if member.atomic_tag == "p" and session.index.source_tokens[member.item_id] > 1500
    )
    calls: list[tuple[str, dict]] = []

    async def transport(stage, payload):
        calls.append((stage, payload))
        return answer(stage, payload)

    result = asyncio.run(run_atomic(session.store.root, transport=transport, ready_session=session))

    assert result.status == "needs_attention"
    record = BodyJournal(session.store, session=session).records((parent.item_id,))[parent.item_id]
    assert record.status == ItemStatus.NEEDS_ATTENTION
    assert not state.exists(plan_path(session.store.root, parent.item_id))
    assert all(parent.item_id not in {item["item_id"] for item in payload["items"]} for _, payload in calls)


def test_atomic_reuses_a_proven_parent_draft_for_review_only_parts(tmp_path, monkeypatch) -> None:
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session=session)
    parent_batch = next(
        batch for batch in session._prepared_batches.values() if parent.item_id in batch.manifest.item_ids
    )

    async def seed_parent() -> None:
        async def translate(_stage, payload):
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-text-1",
                        "request_id": payload["request_id"],
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
                            }
                            for item in payload["items"]
                        ],
                    }
                )
            }

        await root.runtime(transport=translate).invoke("translate", parent_batch.payload, _manifest(parent_batch))
        root.recover_results()

    with monkeypatch.context() as legacy:
        legacy.setattr("engine.agents.wire.enforce_source_limit", lambda *_args, **_kwargs: None)
        asyncio.run(seed_parent())
    assert root.records((parent.item_id,))[parent.item_id].status == ItemStatus.LOCAL_VALID
    calls: list[str] = []

    async def review_only(stage, payload):
        if any(item["item_id"].startswith("pc-") for item in payload["items"]):
            calls.append(stage)
            assert stage == "review"
        return answer(stage, payload)

    result = asyncio.run(run_atomic(session.store.root, transport=review_only, ready_session=session))

    assert result.status == "translated", result.reason
    assert calls and set(calls) == {"review"}


def test_two_part_documents_share_the_run_budget_and_keep_completed_children_on_resume(tmp_path) -> None:
    items = "".join(f"<li>Entry {number}: {'word ' * 110}</li>" for number in range(14))
    source = make_epub(
        tmp_path / "source.epub",
        {"one.xhtml": f"<ul>{items}</ul>", "two.xhtml": f"<ul>{items}</ul>"},
    )
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(
                max_source_tokens=3000,
                max_input_tokens=50_000,
                context_tokens=60_000,
                concurrency=2,
                run_http_limit=8,
            ),
            StubChecker(),
        )
    )
    assert prepared.status == "ready"
    session = ReadySession(RunStore(prepared.work_dir))
    calls: list[tuple[str, dict]] = []

    async def transport(stage, payload):
        calls.append((stage, payload))
        await asyncio.sleep(0.001)
        return answer(stage, payload)

    paused = asyncio.run(run_atomic(session.store.root, transport=transport, ready_session=session))

    assert paused.status == "paused"
    root = BodyJournal(session.store, session=session)
    plans = ensure_part_plans(root)
    assert len(plans) == 2
    completed = {
        record.item_id: canonical_hash(record)
        for plan in plans
        for record in root.parts(plan.parent_id).records().values()
        if record.status == ItemStatus.REVIEWED
    }
    assert completed

    add_http_budget(session.store, authorization_id="finish-parts", add_run_http=100)
    resumed = asyncio.run(run_atomic(session.store.root, transport=transport, ready_session=session))

    assert resumed.status == "translated", resumed.reason
    restarted = BodyJournal(session.store, session=session)
    assert {
        item_id: canonical_hash(restarted.parts(plan.parent_id).records((item_id,))[item_id])
        for plan in plans
        for item_id in completed
        if item_id in {child.item_id for child in plan.children}
    } == completed
    child_calls = [
        payload for _, payload in calls if any(item["item_id"].startswith("pc-") for item in payload["items"])
    ]
    assert child_calls and all(
        request_source_tokens(payload, str(session.prepared.plan.translation_config["model"])) <= 1500
        for payload in child_calls
    )
