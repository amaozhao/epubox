from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from engine.agents.workflow import run_workflow
from engine.epub.preparation import PreparationConfig
from engine.execution.atomic import _pending_batches
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.schemas.contracts import ItemStatus
from engine.services.atomic import IdentityMismatch
from engine.services.attributes import contextual_retry_feedback, source_retry_context
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession
from engine.services.store import RunStore
from tests.engine.agents.test_physical import Model
from tests.engine.agents.test_runtime import FakeOpenAIClient
from tests.engine.agents.test_workflow import prepare_case, review_item
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker


def _ready_session(tmp_path, body: str, *, source_limit: int = 5000) -> ReadySession:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": body})
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="context",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "model": "gpt-3.5-turbo",
                    "max_source_tokens": source_limit,
                    "source_hard_limit": source_limit,
                    "minimum_source_tokens": min(10, source_limit),
                    "max_input_tokens": 50_000,
                    "max_output_tokens": 10_000,
                    "context_tokens": 60_000,
                },
            ),
            StubChecker(),
        )
    )
    assert result.status == "ready"
    return ReadySession(RunStore(result.work_dir))


def test_attribute_retry_adds_verified_source_context_without_touching_sibling_body(tmp_path) -> None:
    parent_text = (
        "Working with paths and the filesystem / Reading and writing files / "
        "Handling CSV, JSON, and TOML files / Building a file organizer tool"
    )
    case = prepare_case(
        tmp_path,
        '<p class="Intro-Text"><img alt="Bullet" src="images/check.png"/> '
        f"<b>{parent_text}</b></p><p>Sibling body stays.</p>",
        ("Bullet",),
        output_budget_version=6,
    )
    journal = BodyJournal(case.session.store, case.session)
    member = case.batch.items[0]
    sibling = next(item for item in case.session.index.members if item.source_projection == "Sibling body stays.")
    sibling_before = journal.records((sibling.item_id,))[sibling.item_id]
    assert case.batch.payload["context"] == []
    initial_items = case.batch.payload["items"]
    assert isinstance(initial_items, list) and isinstance(initial_items[0], dict)
    assert initial_items[0]["source"] == "Bullet"

    class Completions:
        def __init__(self, mode):
            self.mode = mode

        async def create(self, **kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            if payload["protocol"] == "epubox-text-1":
                item = payload["items"][0]
                value = "子弹" if self.mode == "fail" else "项目符号"
                items = [
                    {
                        "item_id": item["item_id"],
                        "target": {slot: value for slot in item["slot_ids"]} if "slot_ids" in item else value,
                    }
                ]
            elif self.mode == "fail":
                items = [review_item(payload["items"][0], decision="needs_attention")]
                items[0]["issues"] = ["context_ambiguous: the meaning cannot be determined from 'Bullet' alone."]
            else:
                assert '"source_context":' in feedback
                item = payload["items"][0]
                target = {slot: "项目符号" for slot in item["slot_ids"]} if "slot_ids" in item else "项目符号"
                reviewed = review_item(item, decision="replace", target="项目符号")
                reviewed["target"] = target
                items = [reviewed]
            raw = json.dumps(
                {"protocol": payload["protocol"], "request_id": payload["request_id"], "items": items},
                ensure_ascii=False,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
                usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
                id=f"context-{self.mode}",
                model="test-model",
                system_fingerprint=None,
            )

    def run(mode, batch):
        runner = journal.runtime(model=Model(FakeOpenAIClient(Completions(mode))))
        return asyncio.run(
            run_workflow(
                case.prepared,
                batch,
                case.index,
                runner,
                session=case.session,
                save=journal.save,
                records=journal.records(batch.manifest.item_ids),
            )
        )

    failed = run("fail", case.batch)
    assert failed.status == "needs_attention"
    journal.retry_units((member.unit_id,))
    retry = next(
        batch
        for batch in _pending_batches(journal, tuple(case.session._prepared_batches.values()))
        if member.item_id in batch.manifest.item_ids
    )
    feedback = retry.manifest.feedback_by_item[member.item_id]
    assert len(feedback) <= 1200
    context = feedback.split("\n", 1)[1]
    assert json.loads(context)["source_context"]["attribute"] == "alt"
    bounded = source_retry_context(journal, member.item_id, max_chars=200)
    assert len(bounded) <= 200 and isinstance(json.loads(bounded), dict)
    assert contextual_retry_feedback(journal, journal.records((member.item_id,))[member.item_id], feedback) == feedback
    for evidence in (
        '"element":"img"',
        '"attribute":"alt"',
        '"alt":"Bullet"',
        '"src":"images/check.png"',
        parent_text,
    ):
        assert evidence in feedback

    completed = run("repair", retry)
    assert completed.status == "completed", completed.issues
    assert journal.records((member.item_id,))[member.item_id].target_projection == "项目符号"
    assert journal.records((sibling.item_id,))[sibling.item_id] == sibling_before
    assert sibling_before.status == ItemStatus.PENDING

    cold = BodyJournal(case.session.store)
    cold.verify_persisted_proofs()
    request = next(
        request
        for request in cold._requests.values()
        if request.stage == "review" and request.feedback_by_item.get(member.item_id) == feedback
    )
    assert request.item_ids == (member.item_id,)


def test_source_retry_context_decodes_entities_and_preserves_nonattribute_feedback(tmp_path, monkeypatch) -> None:
    case = prepare_case(
        tmp_path,
        '<p><img alt="A &amp; B" src="images/a&amp;b.png"/> <b>Visible &amp; parent</b></p>',
        ("A & B",),
        output_budget_version=6,
    )
    journal = BodyJournal(case.session.store, case.session)
    member = case.batch.items[0]

    def reject_full_book_reextraction():
        pytest.fail("one attribute retry must not re-extract the whole book")

    monkeypatch.setattr(journal.store, "_trusted_preparation_documents", reject_full_book_reextraction)
    payload = json.loads(source_retry_context(journal, member.item_id))
    assert payload["source_context"]["attributes"] == {"alt": "A & B", "src": "images/a&b.png"}
    assert payload["source_context"]["parent"]["text"] == "Visible & parent"

    body = next(item for item in case.session.index.members if item.channel == "body")
    record = journal.records((body.item_id,))[body.item_id]
    assert source_retry_context(journal, body.item_id) == ""
    assert contextual_retry_feedback(journal, record, "x" * 1300) == "x" * 1200


def test_split_attribute_context_uses_one_verified_source_node(tmp_path) -> None:
    session = _ready_session(
        tmp_path, f'<p><img alt="{"Bullet " * 100}" src="images/check.png"/></p>', source_limit=50
    )
    journal = BodyJournal(session.store, session)
    members = [member for member in session.index.members if member.channel == "attribute"]
    assert len(members) > 1 and all(member.piece_count == len(members) for member in members)
    contexts = [json.loads(source_retry_context(journal, member.item_id)) for member in members]
    assert all(value["source_context"]["element"] == "img" for value in contexts)
    assert all(value["source_context"]["attribute"] == "alt" for value in contexts)
    assert all(value["source_context"]["attributes"]["src"] == "images/check.png" for value in contexts)


def test_source_retry_context_rejects_snapshot_tampering(tmp_path) -> None:
    case = prepare_case(tmp_path, '<p><img alt="Bullet" src="images/check.png"/></p>', ("Bullet",))
    journal = BodyJournal(case.session.store, case.session)
    snapshot = case.session.store.root / "source.epub"
    snapshot.chmod(0o644)
    snapshot.write_bytes(snapshot.read_bytes() + b"tampered")

    with pytest.raises(IdentityMismatch, match="source|snapshot|changed"):
        source_retry_context(journal, case.batch.items[0].item_id)


@pytest.mark.parametrize("tamper", ["node", "slot"])
def test_source_retry_context_rejects_forged_node_or_slot_identity(tmp_path, tamper: str) -> None:
    case = prepare_case(tmp_path, '<p><img alt="Bullet" src="images/check.png"/></p>', ("Bullet",))
    journal = BodyJournal(case.session.store, case.session)
    member = case.batch.items[0]
    inventory = case.session.index.inventories_by_id[member.document_id]
    parent = next(item for item in inventory.items if item.item_id == member.parent_item_id)
    document = inventory.document
    if tamper == "node":
        nodes = dict(document.nodes)
        nodes[parent.node_key] = nodes[parent.node_key].model_copy(update={"qname": "forged"})
        forged_document = document.model_copy(update={"nodes": nodes})
    else:
        slots = dict(document.source_slots)
        slot_id = parent.slot_ids[0]
        slots[slot_id] = slots[slot_id].model_copy(update={"source_value": "forged"})
        forged_document = document.model_copy(update={"source_slots": slots})
    case.session.index.inventories_by_id = {
        **case.session.index.inventories_by_id,
        member.document_id: inventory.model_copy(update={"document": forged_document}),
    }

    with pytest.raises(IdentityMismatch, match="document|source|node|slot"):
        source_retry_context(journal, member.item_id)
