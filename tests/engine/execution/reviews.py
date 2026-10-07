import asyncio
import json
from collections import Counter

import pytest

from engine.agents.workflow import run_workflow
from engine.cli import _write_source_hint
from engine.epub.preparation import PreparationConfig
from engine.epub.publish import publish_atomic
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.orchestrator import run_translation
from engine.schemas.contracts import ItemStatus
from engine.schemas.members import MemberBatch
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.store import RunStore
from tests.engine.agents.workflow import prepare_case
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer


@pytest.mark.parametrize(
    "failure", ("checks", "missing", "timeout", "truncated", "replacement", "duplicate", "needs_attention")
)
def test_failed_reviews_retry_twice_after_later_content_without_retranslation(tmp_path, failure):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    failed_item = case.batch.items[0].item_id
    translated = Counter()
    reviewed = Counter()
    request_ids = []
    normal_reviewed = set()

    async def transport(kind, payload):
        ids = [item["item_id"] for item in payload["items"]]
        if kind == "translate":
            translated.update(ids)
        else:
            reviewed.update(ids)
            normal_reviewed.update(item for item in ids if item != failed_item)
            if failed_item in ids:
                request_ids.append(payload["request_id"])
                if reviewed[failed_item] > 1:
                    assert normal_reviewed == set(translated) - {failed_item}
        response = answer(kind, payload)
        if kind == "review" and failed_item in ids and reviewed[failed_item] <= 2:
            if failure == "timeout":
                raise TimeoutError("review timed out")
            if failure == "truncated":
                return {"raw": '{"items":[' + '"accuracy",' * 754, "finish_reason": "length"}
            value = json.loads(response["raw"])
            item = next(item for item in value["items"] if item["item_id"] == failed_item)
            if failure == "missing":
                value["items"].remove(item)
            elif failure == "replacement":
                item.update(decision="replace", target="⟦-g1⟧坏译文")
            elif failure == "duplicate":
                item.update(decision="replace", target="⟦+b3⟧错误⟦-b3⟧⟦+b3⟧重复⟦-b3⟧")
            elif failure == "needs_attention":
                item.update(
                    decision="needs_attention",
                    issues=[{"code": "accuracy_error", "severity": "critical", "message": "ambiguous wording"}],
                )
            else:
                item["checks"]["accuracy"] = "fail"
            response["raw"] = json.dumps(value)
        return response

    result = asyncio.run(run_translation(case.session.store.root, transport=transport))
    assert result.status == "translated"
    assert reviewed[failed_item] == 3
    assert len(set(request_ids)) == (1 if failure == "timeout" else 3)
    assert all(count == 1 for count in translated.values())
    assert reviewed[case.batch.items[1].item_id] == (3 if failure == "timeout" else 2 if failure == "truncated" else 1)
    saved = BodyJournal(case.session.store)
    assert saved.records()[failed_item].status == ItemStatus.REVIEWED
    before = sum(translated.values()) + sum(reviewed.values())
    again = asyncio.run(run_translation(case.session.store.root, transport=transport))
    assert again.status == "translated" and sum(translated.values()) + sum(reviewed.values()) == before


def test_exhausted_review_keeps_draft_and_blocks_publication_without_source_fallback(tmp_path):
    case = prepare_case(tmp_path, "<p>First.</p>", ("First.",))
    failed_item = case.batch.items[0].item_id
    calls = Counter()

    async def transport(kind, payload):
        calls[kind] += 1
        response = answer(kind, payload)
        if kind == "review":
            value = json.loads(response["raw"])
            for item in value["items"]:
                if item["item_id"] == failed_item:
                    item["checks"]["accuracy"] = "fail"
            response["raw"] = json.dumps(value)
        return response

    result = asyncio.run(run_translation(case.session.store.root, transport=transport))
    assert result.status == "needs_attention"
    record = BodyJournal(case.session.store).records()[failed_item]
    assert record.status == ItemStatus.NEEDS_ATTENTION
    assert record.target_projection == "译文。" and record.target_projection != case.batch.items[0].source_projection
    reviews = [
        req
        for req in BodyJournal(case.session.store)._requests.values()
        if req.stage == "review" and failed_item in req.item_ids
    ]
    assert len(reviews) == 3
    destination = tmp_path / "source-cn.epub"
    _write_source_hint(
        case.session.store.root, tmp_path / "source.epub", case.prepared.plan.source_hash, case.prepared.plan.run_id
    )
    with pytest.raises(IdentityMismatch, match="incomplete"):
        publish_atomic(case.session.store, destination, StubChecker())
    assert not destination.exists()
    before = calls.copy()
    again = asyncio.run(run_translation(case.session.store.root, transport=transport))
    assert again.status == "needs_attention" and calls == before


def test_new_failure_in_split_unit_does_not_reopen_old_terminal_sibling(tmp_path):
    source = make_epub(
        tmp_path / "source.epub", {"chapter.xhtml": "This is a loose sentence with several English words. " * 12}
    )
    preparation = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="split",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "model": "gpt-3.5-turbo",
                    "planner_version": "epubox-member-planner-1",
                    "output_budget_version": 4,
                    "max_source_tokens": 100,
                    "max_input_tokens": 50_000,
                    "max_output_tokens": 10_000,
                    "context_tokens": 60_000,
                    "concurrency": 1,
                },
            ),
            StubChecker(),
        )
    )
    assert preparation.status == "ready"
    journal = BodyJournal(RunStore(preparation.work_dir))
    members = next(ids for ids in journal.session.prepared.plan.unit_members.values() if len(ids) > 1)
    old, fresh = members[:2]
    batches = (
        MemberBatch.model_validate_json(state.read(journal.store.root / "batches" / f"{batch_id}.json"))
        for batch_id in journal.session.prepared.plan.batch_hashes
    )
    original_batch = next(batch for batch in batches if old in batch.manifest.item_ids)

    async def old_failure(kind, payload):
        response = answer(kind, payload)
        if kind == "review":
            value = json.loads(response["raw"])
            for item in value["items"]:
                if item["item_id"] == old:
                    item["checks"]["accuracy"] = "fail"
            response["raw"] = json.dumps(value)
        return response

    asyncio.run(
        run_workflow(
            journal.session.prepared,
            original_batch,
            journal.session.index,
            journal.runtime(transport=old_failure),
            session=journal.session,
            save=journal.save,
        )
    )
    terminal = journal.records()[old]
    assert terminal.status == ItemStatus.NEEDS_ATTENTION
    calls = Counter()

    async def current_failure(kind, payload):
        response = answer(kind, payload)
        if kind == "review":
            value = json.loads(response["raw"])
            for item in value["items"]:
                calls[item["item_id"]] += 1
                if item["item_id"] == fresh:
                    item["checks"]["accuracy"] = "fail"
            response["raw"] = json.dumps(value)
        return response

    result = asyncio.run(run_translation(journal.store.root, transport=current_failure))
    assert result.status == "needs_attention" and calls[old] == 0
    assert BodyJournal(journal.store).records()[old] == terminal
