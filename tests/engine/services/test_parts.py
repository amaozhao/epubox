from __future__ import annotations

import asyncio
import json

import pytest

from engine.agents.workflow import _manifest, run_workflow
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_json_bytes
from engine.services import frames, state
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.partition import build_plan, plan_path
from engine.services.parts import (
    JointPartSession,
    PartJournal,
    composite_parts,
    open_parts,
    verify_composite,
    verify_composites,
)
from tests.engine.agents.test_workflow import review_item
from tests.engine.execution.test_atomic import answer as _answer
from tests.engine.services.test_partition import _case


def _two_part_case(tmp_path):
    from engine.services.preparation import prepare_translation
    from engine.services.ready import ReadySession
    from engine.services.store import RunStore
    from tests.engine.epub.test_factory import make_epub
    from tests.engine.epub.test_preparation import StubChecker
    from tests.engine.services.test_preparing import atomic_config

    lists = "".join(
        "<ul>" + "".join(f"<li>{label} {number}: " + "word " * 110 + "</li>" for number in range(14)) + "</ul>"
        for label in ("Alpha", "Beta")
    )
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": lists})
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(max_source_tokens=3000, max_input_tokens=50_000, context_tokens=60_000),
            StubChecker(),
        )
    )
    assert result.status == "ready"
    session = ReadySession(RunStore(result.work_dir))
    parents = tuple(member for member in session.index.members if session.index.source_tokens[member.item_id] > 1500)
    assert len(parents) == 2
    return session, parents


def _manual_composites(tmp_path):
    from engine.services.manual import import_codex_translation

    session, parents = _two_part_case(tmp_path)
    root = BodyJournal(session.store, session)
    plans = []
    composed = []
    for parent in parents:
        plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
        assert plan is not None
        state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
        part = PartJournal.initialize(root, plan)
        for child in plan.children:
            existing = part.records((child.item_id,))[child.item_id]
            target = child.source_projection.replace("Alpha", "甲项").replace("Beta", "乙项").replace("word", "文字")
            import_codex_translation(part, child, existing, target, user_authorized=True)
        parent_record = part.compose()
        assert parent_record is not None
        plans.append(plan)
        composed.append(parent_record)
    return root, tuple(plans), tuple(composed)


def test_part_journal_persists_proves_composes_and_restarts_without_http(tmp_path) -> None:
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    part = PartJournal.initialize(root, plan)

    opened, initial = open_parts(session, parent.item_id)
    assert opened == plan
    assert all(record.status == ItemStatus.PENDING for record in initial.values())
    assert all(not root._requests[batch.manifest.request_id].attempts for batch in part.pending_batches())
    batch_count = len(part.pending_batches())

    calls: list[str] = []
    failed_review = False

    async def transport(kind, payload):
        nonlocal failed_review
        calls.append(kind)
        if kind == "translate":
            items = [
                {
                    "item_id": item["item_id"],
                    "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
                }
                for item in payload["items"]
            ]
            protocol = "epubox-text-1"
        else:
            items = []
            for item in payload["items"]:
                if item["item_id"] == plan.children[0].item_id and not failed_review:
                    decision = review_item(item, decision="needs_attention")
                    decision["issues"] = ["retry once"]
                    failed_review = True
                else:
                    decision = review_item(item, decision="no_change")
                items.append(decision)
            protocol = "epubox-review-2"
        return {
            "raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items}),
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    async def execute() -> None:
        runtime = root.runtime(transport=transport)
        for batch in part.pending_batches():
            with root.activate(part):
                result = await run_workflow(
                    session.prepared,
                    batch,
                    part.session.index,
                    runtime,
                    session=part.session,
                    save=part.save,
                    records=part.records(batch.manifest.item_ids),
                )
            assert result.status in {"completed", "needs_attention"}
        first = part.records((plan.children[0].item_id,))[plan.children[0].item_id]
        assert first.status == ItemStatus.NEEDS_ATTENTION
        reviewed = {
            item_id: record for item_id, record in part.records().items() if record.status == ItemStatus.REVIEWED
        }
        assert part.retry_units((parent.unit_id,)) == (plan.children[0].item_id,)
        assert all(part.records((item_id,))[item_id] == record for item_id, record in reviewed.items())
        for batch in part.pending_batches():
            with root.activate(part):
                result = await run_workflow(
                    session.prepared,
                    batch,
                    part.session.index,
                    runtime,
                    session=part.session,
                    save=part.save,
                    records=part.records(batch.manifest.item_ids),
                )
            assert result.status == "completed"

    asyncio.run(execute())
    composed = part.compose()
    assert composed is not None and composed.status == ItemStatus.REVIEWED
    assert calls.count("translate") == batch_count
    assert calls.count("review") == batch_count + 1
    assert composite_parts(session, composed)[1] == part.records()

    before = len(calls)
    resumed = BodyJournal(session.store)
    reopened = resumed.parts(parent.item_id)
    assert reopened.pending_batches() == ()
    assert resumed.records((parent.item_id,))[parent.item_id] == composed
    assert len(calls) == before

    child = plan.children[0]
    child_record = reopened.records((child.item_id,))[child.item_id]
    state.write(
        reopened.session.result_path(child.item_id),
        canonical_json_bytes(child_record.model_copy(update={"checks": child_record.checks | {"tampered": True}})),
    )
    with pytest.raises(IdentityMismatch, match="persisted part results"):
        composite_parts(session, composed)

    # A self-consistent parent summary is still not a provider/manual proof.
    from engine.schemas.contracts import canonical_hash
    from engine.services.partition import merge_targets
    from engine.services.parts import verify_composite

    assert child_record.target_projection is not None
    forged_text = child_record.target_projection.replace("条目", "伪造", 1)
    assert forged_text != child_record.target_projection
    forged_child = child_record.model_copy(
        update={"target_projection": forged_text, "target_hash": canonical_hash(forged_text)}
    )
    state.write(reopened.session.result_path(child.item_id), canonical_json_bytes(forged_child))
    children = reopened.records() | {child.item_id: forged_child}
    merged = merge_targets(parent, plan, {key: value.target_projection or "" for key, value in children.items()})
    summary = {
        "plan_hash": canonical_hash(plan),
        "child_hashes": {key: canonical_hash(value) for key, value in children.items()},
        "merged_target_hash": canonical_hash(merged),
    }
    forged_parent = composed.model_copy(
        update={
            "target_projection": merged,
            "target_hash": canonical_hash(merged),
            "checks": composed.checks | {"parts": summary},
        }
    )
    assert composite_parts(session, forged_parent)[1] == children
    with pytest.raises(IdentityMismatch):
        verify_composite(root, forged_parent)


def test_authentic_parent_draft_seeds_review_only_children(tmp_path, monkeypatch) -> None:
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    parent_batch = next(
        batch for batch in session._prepared_batches.values() if parent.item_id in batch.manifest.item_ids
    )
    root_calls: list[str] = []
    monkeypatch.setattr("engine.agents.wire.enforce_source_limit", lambda *_args, **_kwargs: None)

    async def parent_transport(kind, payload):
        root_calls.append(kind)
        if kind == "translate":
            items = [
                {
                    "item_id": item["item_id"],
                    "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
                }
                for item in payload["items"]
            ]
            protocol = "epubox-text-1"
        else:
            items = []
            for item in payload["items"]:
                decision = review_item(item, decision="needs_attention")
                decision["issues"] = ["manual review"]
                items.append(decision)
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    async def seed_parent() -> None:
        await root.runtime(transport=parent_transport).invoke(
            "translate",
            parent_batch.payload,
            _manifest(parent_batch),
        )
        root.recover_results()

    asyncio.run(seed_parent())
    seed = root.records((parent.item_id,))[parent.item_id]
    assert seed.status == ItemStatus.LOCAL_VALID and seed.target_projection is not None
    plan = build_plan(session, parent, seed)
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    part = PartJournal.initialize(root, plan)
    assert all(record.status == ItemStatus.LOCAL_VALID for record in part.records().values())

    child_calls: list[str] = []

    async def review_only(kind, payload):
        child_calls.append(kind)
        assert kind == "review"
        items = [review_item(item, decision="no_change") for item in payload["items"]]
        return {
            "raw": json.dumps({"protocol": "epubox-review-2", "request_id": payload["request_id"], "items": items})
        }

    async def review_children() -> None:
        runtime = root.runtime(transport=review_only)
        for batch in part.pending_batches():
            with root.activate(part):
                result = await run_workflow(
                    session.prepared,
                    batch,
                    part.session.index,
                    runtime,
                    session=part.session,
                    save=part.save,
                    records=part.records(batch.manifest.item_ids),
                )
            assert result.status == "completed"

    asyncio.run(review_children())
    assert child_calls and set(child_calls) == {"review"}
    composed = part.compose()
    assert composed is not None
    for name in ("translation_frame", "translation_epoch", "review_epoch"):
        assert composed.checks.get(name) == seed.checks.get(name)
    restarted = BodyJournal(session.store)
    assert restarted.records((parent.item_id,))[parent.item_id] == composed
    # Historical review batches may still need this parent beside an accepted sibling.
    assert seed.target_hash is not None
    draft = restarted._review_target(parent.item_id, seed.target_hash, 7)
    assert draft.target_projection == seed.target_projection
    assert draft.checks["translation_frame"] == seed.checks["translation_frame"]
    assert draft.checks["review_epoch"] == 7


def test_restart_rebuilds_review_frame_with_fresh_and_retried_siblings(tmp_path) -> None:
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    part = PartJournal.initialize(root, plan)
    batch = part.pending_batches()[0]
    fresh = {item.item_id for item in batch.items[:3]}
    retained = batch.items[3].item_id

    async def first_pass(kind, payload):
        if kind == "translate":
            items = [
                {
                    "item_id": item["item_id"],
                    "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
                }
                for item in payload["items"]
                if item["item_id"] == retained
            ]
            return {
                "raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": items})
            }
        decision = review_item(payload["items"][0], decision="needs_attention")
        decision["issues"] = ["retry retained draft"]
        return {
            "raw": json.dumps(
                {"protocol": "epubox-review-2", "request_id": payload["request_id"], "items": [decision]}
            )
        }

    async def run_first() -> None:
        with root.activate(part):
            result = await run_workflow(
                session.prepared,
                batch,
                part.session.index,
                root.runtime(transport=first_pass),
                session=part.session,
                save=part.save,
                records=part.records(batch.manifest.item_ids),
            )
        assert result.status == "needs_attention"

    asyncio.run(run_first())
    assert set(part.retry_units((parent.unit_id,))) == set(batch.manifest.item_ids)

    class StopAfterReviewWasPersisted(BaseException):
        pass

    review_request_id = ""
    replaced = batch.items[0].item_id
    replacement = ""

    async def second_pass(kind, payload):
        nonlocal replacement, review_request_id
        if kind == "translate":
            items = [
                {
                    "item_id": item["item_id"],
                    "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
                }
                for item in payload["items"]
                if item["item_id"] in fresh
            ]
            return {
                "raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": items})
            }
        review_request_id = payload["request_id"]
        items = []
        for item in payload["items"]:
            if item["item_id"] == replaced:
                replacement = item["target"].replace("文字", "文本", 1)
                items.append(review_item(item, decision="replace", target=replacement))
            else:
                items.append(review_item(item, decision="no_change"))
        return {
            "raw": json.dumps({"protocol": "epubox-review-2", "request_id": payload["request_id"], "items": items})
        }

    def interrupted_save(_record) -> None:
        raise AssertionError("group save should be used")

    def interrupted_save_many(records) -> None:
        if any(record.status == ItemStatus.REVIEWED for record in records):
            raise StopAfterReviewWasPersisted
        part.save_many(records)

    interrupted_save.save_many = interrupted_save_many  # type: ignore[attr-defined]

    async def run_second() -> None:
        with root.activate(part), pytest.raises(StopAfterReviewWasPersisted):
            await run_workflow(
                session.prepared,
                batch,
                part.session.index,
                root.runtime(transport=second_pass),
                session=part.session,
                save=interrupted_save,
                records=part.records(batch.manifest.item_ids),
            )

    asyncio.run(run_second())
    assert review_request_id
    resumed = BodyJournal(session.store)
    reopened = resumed.parts(parent.item_id)
    saved = resumed._requests[review_request_id]
    assert reopened._review_batch(saved).manifest == saved.model_copy(update={"attempts": ()})
    assert saved.attempts[-1].state == "succeeded" and replacement
    reopened.recover_results()
    recovered = reopened.records((*fresh, retained))
    assert all(record.status == ItemStatus.REVIEWED for record in recovered.values())
    assert recovered[replaced].target_projection == replacement
    verified = BodyJournal(session.store).parts(parent.item_id)
    assert verified.records((replaced,))[replaced] == recovered[replaced]


def test_restart_skips_rebuilding_a_review_request_that_never_dispatched(tmp_path) -> None:
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    part = PartJournal.initialize(root, plan)
    batch = part.pending_batches()[0]

    class StopAfterPlanningReview(BaseException):
        pass

    original_prepare = part._prepare

    def stop_after_planning(stage, payload, context) -> None:
        original_prepare(stage, payload, context)
        if stage == "review":
            raise StopAfterPlanningReview

    part._prepare = stop_after_planning  # type: ignore[method-assign]

    async def transport(kind, payload):
        assert kind == "translate"
        items = [
            {
                "item_id": item["item_id"],
                "target": item["source"].replace("Entry", "条目").replace("word", "文字"),
            }
            for item in payload["items"]
        ]
        return {"raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": items})}

    async def plan_review() -> None:
        with root.activate(part), pytest.raises(StopAfterPlanningReview):
            await run_workflow(
                session.prepared,
                batch,
                part.session.index,
                root.runtime(transport=transport),
                session=part.session,
                save=part.save,
                records=part.records(batch.manifest.item_ids),
            )

    asyncio.run(plan_review())
    review = next(
        request
        for request in root._requests.values()
        if request.stage == "review" and set(request.item_ids).issubset(batch.manifest.item_ids)
    )
    assert review.attempts == ()

    changed_id = review.item_ids[0]
    current = part._records[changed_id]
    part._records[changed_id] = current.model_copy(update={"checks": current.checks | {"translation_epoch": 1}})
    with pytest.raises(IdentityMismatch, match=review.request_id):
        part._review_batch(review)

    rebuilt: list[str] = []
    original = part._review_batch

    def tracking(request):
        rebuilt.append(request.request_id)
        return original(request)

    part._review_batch = tracking  # type: ignore[method-assign]
    try:
        frames.warm(part)
    finally:
        part._review_batch = original  # type: ignore[method-assign]

    assert review.request_id not in rebuilt
    assert review.request_id not in getattr(part, "_active_frames", set())


def test_reading_part_summary_does_not_repack_translation_requests(tmp_path, monkeypatch):
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    PartJournal.initialize(root, plan)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("reading a saved part summary must not pack model requests")

    monkeypatch.setattr("engine.services.partition.pack_members", forbidden)
    opened, records = open_parts(session, parent.item_id)
    assert opened == plan and set(records) == {child.item_id for child in plan.children}


def test_completed_composite_proof_restores_frames_without_repacking(tmp_path, monkeypatch):
    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    part = PartJournal.initialize(root, plan)

    async def transport(kind, payload):
        if kind == "translate":
            items = [
                {"item_id": item["item_id"], "target": item["source"].replace("Entry", "条目")}
                for item in payload["items"]
            ]
            return {
                "raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": items})
            }
        return _answer(kind, payload)

    async def complete() -> None:
        runtime = root.runtime(transport=transport)
        try:
            for batch in part.pending_batches():
                with root.activate(part):
                    result = await run_workflow(
                        session.prepared,
                        batch,
                        part.session.index,
                        runtime,
                        session=part.session,
                        save=part.save,
                        records=part.records(batch.manifest.item_ids),
                    )
                assert result.status == "completed"
        finally:
            await runtime.aclose()

    asyncio.run(complete())
    composed = part.compose()
    assert composed is not None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("completed part proof must restore saved frames instead of repacking")

    monkeypatch.setattr("engine.services.partition.pack_members", forbidden)
    verify_composite(root, composed)


def test_composite_verification_reuses_the_bound_child_snapshot(tmp_path, monkeypatch):
    import engine.services.parts as parts_module

    session, parent = _case(tmp_path)
    root = BodyJournal(session.store, session)
    plan = build_plan(session, parent, root.records((parent.item_id,))[parent.item_id])
    assert plan is not None
    state.write(plan_path(session.store.root, parent.item_id), canonical_json_bytes(plan))
    part = PartJournal.initialize(root, plan)

    # Use the normal manual proof helper so every child remains independently proven.
    from engine.services.manual import import_codex_translation

    for child in plan.children:
        existing = part.records((child.item_id,))[child.item_id]
        target = child.source_projection.replace("Entry", "条目").replace("word", "文字")
        import_codex_translation(part, child, existing, target, user_authorized=True)
    composed = part.compose()
    assert composed is not None

    calls = 0
    original = parts_module.open_parts

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(parts_module, "open_parts", counted)
    monkeypatch.setattr(
        "engine.services.partition.pack_members",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual composite proof must restore its persisted request wire")
        ),
    )
    parts_module.verify_composite(root, composed)
    assert calls == 1


def test_joint_part_session_rejects_a_request_crossing_plans(tmp_path):
    from engine.services.partition import PartSession

    root, plans, _composed = _manual_composites(tmp_path)
    left = next(iter(PartSession(root.session, plans[0])._prepared_batches.values()))
    right = next(iter(PartSession(root.session, plans[1])._prepared_batches.values()))
    item_ids = (left.items[0].item_id, right.items[0].item_id)
    forged = left.model_copy(
        update={
            "items": (left.items[0], right.items[0]),
            "manifest": left.manifest.model_copy(update={"item_ids": item_ids}),
        }
    )
    joint = JointPartSession(root.session, plans)

    with pytest.raises(IdentityMismatch, match="crosses immutable plans"):
        joint.verify_batch(forged)


def test_joint_composite_proof_rejects_parent_and_child_tampering(tmp_path):
    root, plans, composed = _manual_composites(tmp_path)
    changed_summary = dict(composed[0].checks["parts"])
    changed_summary["merged_target_hash"] = "0" * 64
    forged_parent = composed[0].model_copy(update={"checks": composed[0].checks | {"parts": changed_summary}})
    with pytest.raises(IdentityMismatch, match="persisted part results"):
        verify_composites(root, (forged_parent, composed[1]))

    child = plans[1].children[0]
    child_path = root.store.root / "parts" / child.parent_item_id / "results" / f"{child.item_id}.json"
    saved = ItemRecord.model_validate_json(state.read(child_path))
    state.write(
        child_path,
        canonical_json_bytes(saved.model_copy(update={"checks": saved.checks | {"tampered": True}})),
    )
    with pytest.raises(IdentityMismatch, match="persisted part results"):
        verify_composites(root, composed)
