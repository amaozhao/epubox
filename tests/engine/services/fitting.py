import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

import engine.services.ready as ready_module
from engine.agents import wire
from engine.agents.protocol import validate_translation_response
from engine.agents.workflow import run_workflow
from engine.epub.preparation import PreparationConfig
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION, extract_resource
from engine.item.members import MemberIndex, fit_member_payload, validate_member_target
from engine.schemas.budget import BudgetLimits
from engine.services import state
from engine.services.journal import BodyJournal
from engine.services.preflight import preflight_atomic_resources
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession
from engine.services.store import RunStore
from tests.engine.agents.workflow import review_item
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.item.members import glossary, source
from tests.engine.services.preparing import atomic_config


def test_compacted_member_survives_ready_dispatch_review_and_restart(tmp_path):
    body = "<p>" + "<br/>".join(f"├── Service{number}Application.java" for number in range(38)) + "</p>"
    result = asyncio.run(
        prepare_translation(
            make_epub(tmp_path / "book.epub", {"chapter.xhtml": body}),
            tmp_path / "work",
            PreparationConfig(
                run_id="fitting",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={"model": "gpt-3.5-turbo", "output_budget_version": 4},
            ),
            StubChecker(),
        )
    )
    assert result.status == "ready"
    session = ReadySession(RunStore(result.work_dir))
    batch = next(
        b for b in session._prepared_batches.values() if "Service0Application.java" in b.items[0].source_projection
    )
    assert len(batch.items) == 1
    session.verify_batch(batch, initial=True)
    journal = BodyJournal(session.store, session)
    calls = []

    async def transport(stage, payload):
        calls.append(stage)
        items = [
            {"item_id": item["item_id"], "target": item["source"]}
            if stage == "translate"
            else review_item(item, decision="no_change")
            for item in payload["items"]
        ]
        return {
            "raw": json.dumps({"protocol": payload["protocol"], "request_id": payload["request_id"], "items": items})
        }

    finished = asyncio.run(
        run_workflow(
            session.prepared,
            batch,
            session.index,
            journal.runtime(transport=transport),
            session=session,
            save=journal.save,
            records=journal.records(batch.manifest.item_ids),
        )
    )
    assert finished.status == "completed" and calls == ["translate", "review"]
    restarted = BodyJournal(session.store)
    assert restarted.records()[batch.items[0].item_id].status == "reviewed"


def test_v6_context_fallback_omits_only_redundant_constraints_and_keeps_strict_slots():
    raw = source(
        "<ul>"
        + "".join(f'<li>Entry {number}, <a href="#p{number}">{number}</a></li>' for number in range(150))
        + "</ul>"
    )
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    generous = BudgetLimits(10_000, 200_000, 100_000, 300_000, output_version=6)
    report = preflight_atomic_resources((inventory,), {"OPS/chapter.xhtml": raw}, generous, "gpt-3.5-turbo")
    index = MemberIndex((inventory,), report)
    member = next(item for item in index.members if item.atomic_tag == "ul")
    capacity = BudgetLimits(
        2_000,
        50_000,
        8_192,
        32_768,
        output_version=6,
        minimum_source_tokens=500,
        source_tolerance_tokens=1_000,
    )
    payload, measured = fit_member_payload(
        "translate", (member,), glossary(), index, capacity, request_id="tx-" + "0" * 32
    )
    historical, old = fit_member_payload(
        "translate",
        (member,),
        glossary(),
        index,
        replace(capacity, output_version=5),
        request_id="tx-" + "0" * 32,
    )
    roomy, roomy_budget = fit_member_payload(
        "translate",
        (member,),
        glossary(),
        index,
        replace(capacity, context_tokens=60_000),
        request_id="tx-" + "0" * 32,
    )

    assert measured.fits and member.registry and payload["items"][0]["constraints"] == {}
    assert not old.fits and historical["items"][0]["constraints"]
    assert roomy_budget.fits and roomy["items"][0]["constraints"]

    physical = json.loads(wire.messages("translate", payload, "base")[1]["content"])
    slot_ids = physical["items"][0]["slot_ids"]
    complete = {slot_id: f"译文{slot_id}" for slot_id in slot_ids}

    def decoded(target):
        raw_response = json.dumps(
            {
                "protocol": "epubox-text-1",
                "request_id": payload["request_id"],
                "items": [{"item_id": "1", "target": target}],
            }
        )
        canonical = wire.decode(
            "translate",
            raw_response,
            payload["request_id"],
            (member.item_id,),
            version=wire.VERSION,
            sources={member.item_id: member.source_projection},
        )
        return validate_translation_response(
            canonical, payload["request_id"], {member.item_id: member.source_projection}
        )

    accepted = decoded(complete)
    assert member.item_id in accepted.accepted
    validate_member_target(member, accepted.accepted[member.item_id]["target"])
    for invalid in (
        {key: value for key, value in complete.items() if key != slot_ids[-1]},
        complete | {"unexpected": "多余"},
        complete | {slot_ids[-1]: ""},
    ):
        assert member.item_id in decoded(invalid).errors


@pytest.mark.parametrize("fail", (False, True))
@pytest.mark.parametrize("alias", (False, True))
def test_compact_ready_plan_commits_once_or_rolls_back(tmp_path, monkeypatch, fail, alias):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>First.</p><p>Second.</p>"})
    config = atomic_config()
    assert config.run_id is not None
    root = tmp_path / "book"
    if alias:
        linked = tmp_path / "linked"
        linked.symlink_to(tmp_path, target_is_directory=True)
        root = linked / "book"
    state.initialize(root, source, hashlib.sha256(source.read_bytes()).hexdigest(), config.run_id)
    commits = []
    original = state._commit
    verifications = []
    verify_ready = ready_module._verify

    def verify(*args, **kwargs):
        verifications.append(True)
        return verify_ready(*args, **kwargs)

    monkeypatch.setattr(ready_module, "_verify", verify)

    def commit(path, value):
        if any(key.startswith("members/") for key in value["records"]):
            commits.append(path)
        return original(path, value)

    monkeypatch.setattr(state, "_commit", commit)
    if fail:

        def broken(*args, **kwargs):
            raise RuntimeError("ready validation interrupted")

        monkeypatch.setattr(ready_module, "_verify", broken)
        with pytest.raises(RuntimeError, match="ready validation interrupted"):
            asyncio.run(prepare_translation(source, root, config, StubChecker()))
        assert not commits and not list(state.glob(root / "members", "*.json"))
        assert not state.exists(root / "prepared.json")
    else:
        result = asyncio.run(prepare_translation(source, root, config, StubChecker()))
        assert result.status == "ready" and result.ready_session is not None
        assert len(commits) == 1
        BodyJournal(RunStore(root), session=result.ready_session)
        assert len(verifications) == 1


def test_compact_ready_rejects_forged_plan_before_commit(tmp_path, monkeypatch):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>First.</p>"})
    config = atomic_config()
    assert config.run_id is not None
    root = tmp_path / "book"
    state.initialize(root, source, hashlib.sha256(source.read_bytes()).hexdigest(), config.run_id)
    original = ready_module.write_ready

    def forged(store, inventories, report, members, packing, **kwargs):
        batches = packing.batches
        invalid = batches[0].model_copy(update={"context": ("forged context",)})
        changed = packing.model_copy(update={"batches": (invalid, *batches[1:])})
        return original(store, inventories, report, members, changed, **kwargs)

    monkeypatch.setattr(ready_module, "write_ready", forged)
    with pytest.raises(Exception, match="ready request plan changed"):
        asyncio.run(prepare_translation(source, root, config, StubChecker()))
    assert not state.exists(root / "prepared.json")
    assert not list(state.glob(root / "members", "*.json"))


def test_handoff_rejects_frozen_dependency_changed_after_full_validation(tmp_path, monkeypatch):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>First.</p>"})
    config = atomic_config()
    assert config.run_id is not None
    root = tmp_path / "book"
    state.initialize(root, source, hashlib.sha256(source.read_bytes()).hexdigest(), config.run_id)
    original = ready_module._verify

    def changed(store, *args, **kwargs):
        result = original(store, *args, **kwargs)
        value = json.loads(state.read(store.root / "glossary.json"))
        value["warnings"] = ["changed after verification"]
        state.write(store.root / "glossary.json", json.dumps(value).encode())
        return result

    monkeypatch.setattr(ready_module, "_verify", changed)
    with pytest.raises(Exception, match="dependencies changed during preparation handoff"):
        asyncio.run(prepare_translation(source, root, config, StubChecker()))
    assert not state.exists(root / "prepared.json")
    assert not list(state.glob(root / "members", "*.json"))
