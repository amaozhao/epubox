from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from agno.models.openai.like import OpenAILike

from engine import cli
from engine.agents.pool import workflow_limit
from engine.agents.runtime import ModelRuntime
from engine.epub.preparation import PreparationConfig
from engine.execution import atomic
from engine.execution.atomic import run_atomic
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.services import state
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer

KEYS = ("synthetic-key-first", "synthetic-key-second", "synthetic-key-third")


def model():
    value = OpenAILike(id="gpt-3.5-turbo", api_key=KEYS[0], provider="Agnes", max_completion_tokens=10000)
    cast(Any, value)._epubox_keys = KEYS
    return value


def prepared(tmp_path):
    source = make_epub(
        tmp_path / "book.epub",
        {
            name: f"<p>{name} describes reliable systems.</p>"
            for name in ("first.xhtml", "second.xhtml", "third.xhtml")
        },
    )
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="parallel",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "provider": "agnes",
                    "model": "gpt-3.5-turbo",
                    "max_source_tokens": 5000,
                    "max_input_tokens": 50000,
                    "max_output_tokens": 10000,
                    "output_budget_version": 6,
                    "concurrency": 2,
                },
            ),
            StubChecker(),
        )
    )
    assert result.status == "ready"
    return result


def logical_transport(monkeypatch, factory):
    monkeypatch.setattr(ModelRuntime, "_agno_transport", factory)
    original = BodyJournal.runtime

    def create(self, *args, **kwargs):
        runtime = original(self, *args, **kwargs)
        runtime._compact = False
        return runtime

    monkeypatch.setattr(BodyJournal, "runtime", create)


def test_real_journal_runs_three_pinned_workflows_and_resume_does_not_resend(tmp_path: Path, monkeypatch):
    case = prepared(tmp_path)
    calls = []
    active = 0
    peak = 0
    active_documents = {}
    overlap = set()
    document_keys = {}
    commit_sizes = []
    translated_by = {}
    assert case.ready_session is not None and case.prepared is not None
    documents = {item.item_id: item.document_id for item in case.ready_session.index.members}

    def factory(self, bound):
        async def call(kind, payload):
            nonlocal active, peak
            document = documents[payload["items"][0]["item_id"]]
            if active_documents.get(document, 0):
                overlap.add(document)
            active_documents[document] = active_documents.get(document, 0) + 1
            active += 1
            peak = max(peak, active)
            try:
                ids = tuple(item["item_id"] for item in payload["items"])
                calls.append((kind, ids, bound.api_key))
                document_keys.setdefault(document, set()).add(bound.api_key)
                if kind == "translate":
                    translated_by.update({identifier: bound.api_key for identifier in ids})
                else:
                    assert all(translated_by[identifier] == bound.api_key for identifier in ids)
                await asyncio.sleep(0.01)
                return answer(kind, payload)
            finally:
                active -= 1
                active_documents[document] -= 1

        return call

    original_save_many = BodyJournal.save_many

    def save_many(journal, records):
        records = tuple(records)
        commit_sizes.append(len(records))
        original_save_many(journal, records)

    logical_transport(monkeypatch, factory)
    monkeypatch.setattr(BodyJournal, "save_many", save_many)
    events = []
    result = asyncio.run(run_atomic(case.work_dir, model=model(), progress=events.append))

    assert result.status == "translated"
    assert peak == 3
    assert not overlap
    assert all(len(keys) == 1 for keys in document_keys.values())
    assert {key for _, _, key in calls} == set(KEYS)
    assert result.http_attempts == len(calls)
    summaries = [event for event in events if event.get("phase") == "workflow"]
    assert summaries and max(event["runtime"]["http_peak"] for event in summaries) == 3
    assert len(summaries) == len(case.prepared.plan.batch_hashes)
    assert all(event["document_path"].endswith((".xhtml", ".ncx", ".opf")) for event in summaries)
    assert all(event["workflow_timing"]["http_attempts"] == 2 for event in summaries)
    assert all(event["workflow_timing"]["keys_used"] for event in summaries)
    assert any("并行 HTML" in event.get("notice", "") for event in events)
    assert max(commit_sizes) > 1
    before = len(calls)
    assert asyncio.run(run_atomic(case.work_dir, model=model())).status == "translated"
    assert len(calls) == before
    stored = b"".join(path.read_bytes() for path in case.work_dir.rglob("*.json"))
    assert all(key.encode() not in stored for key in KEYS)


def test_timeout_switch_keeps_attempt_evidence_and_valid_review_on_new_key(tmp_path: Path, monkeypatch):
    case = prepared(tmp_path)
    assert case.ready_session is not None
    calls = []
    successful = []
    failed = False
    translated_by = {}

    def factory(self, bound):
        async def call(kind, payload):
            nonlocal failed
            ids = tuple(item["item_id"] for item in payload["items"])
            calls.append((kind, ids, bound.api_key))
            if kind == "translate" and bound.api_key == KEYS[0] and not failed:
                failed = True
                raise TimeoutError("synthetic timeout")
            if kind == "translate":
                translated_by.update({identifier: bound.api_key for identifier in ids})
            else:
                assert all(translated_by[identifier] == bound.api_key for identifier in ids)
            await asyncio.sleep(0)
            successful.extend((kind, identifier) for identifier in ids)
            return answer(kind, payload)

        return call

    logical_transport(monkeypatch, factory)
    result = asyncio.run(run_atomic(case.work_dir, model=model()))
    assert result.status == "translated" and failed
    store = RunStore(case.work_dir)
    requests = [store.read_request(path.stem) for path in state.glob(case.work_dir / "requests", "*.json")]
    switched = [request for request in requests if any(attempt.state == "unknown" for attempt in request.attempts)]
    assert len(switched) == 1
    assert [attempt.state for attempt in switched[0].attempts] == ["unknown", "succeeded"]
    assert [attempt.reservation["attempt_number"] for attempt in switched[0].attempts] == [1, 2]
    assert result.http_attempts == len(calls)
    expected = {(kind, item.item_id) for kind in ("translate", "review") for item in case.ready_session.index.members}
    assert len(successful) == len(expected) and set(successful) == expected
    before = len(calls)
    assert asyncio.run(run_atomic(case.work_dir, model=model())).status == "translated"
    assert len(calls) == before


def test_partial_restart_skips_the_completed_chunk(tmp_path: Path, monkeypatch):
    case = prepared(tmp_path)
    bound = model()
    cast(Any, bound)._epubox_keys = (KEYS[0],)
    calls = []

    def factory(self, configured):
        async def call(kind, payload):
            calls.append((kind, tuple(item["item_id"] for item in payload["items"])))
            return answer(kind, payload)

        return call

    logical_transport(monkeypatch, factory)
    original = atomic.run_workflow
    invocations = 0

    async def stop_after_one(*args, **kwargs):
        nonlocal invocations
        invocations += 1
        if invocations == 2:
            raise OSError("synthetic interruption")
        return await original(*args, **kwargs)

    monkeypatch.setattr(atomic, "run_workflow", stop_after_one)
    assert asyncio.run(run_atomic(case.work_dir, model=bound)).status == "paused"
    completed_calls = tuple(calls)
    assert len(completed_calls) == 2

    monkeypatch.setattr(atomic, "run_workflow", original)
    assert asyncio.run(run_atomic(case.work_dir, model=bound)).status == "translated"
    assert calls[:2] == list(completed_calls)
    assert all(call not in calls[2:] for call in completed_calls)


def test_fatal_document_error_cancels_other_active_documents(tmp_path: Path, monkeypatch):
    case = prepared(tmp_path)
    assert case.ready_session is not None
    first = case.ready_session.index.document_order[0]
    started = set()
    cancelled = set()

    async def fail_one(_ready, batch, *_args, **_kwargs):
        document = batch.items[0].document_id
        started.add(document)
        if document == first:
            while len(started) < len(KEYS):
                await asyncio.sleep(0)
            raise OSError("synthetic store failure")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(document)
            raise

    monkeypatch.setattr(atomic, "run_workflow", fail_one)
    result = asyncio.run(run_atomic(case.work_dir, model=model()))
    assert result.status == "paused" and result.reason == "synthetic store failure"
    assert len(started) == len(KEYS) and cancelled == started - {first}


def test_simultaneous_expected_and_unexpected_failures_preserve_the_unexpected(tmp_path: Path, monkeypatch):
    case = prepared(tmp_path)
    assert case.ready_session is not None
    first, second = case.ready_session.index.document_order[:2]
    started = set()
    release = asyncio.Event()
    cancelled = set()

    async def fail_two(_ready, batch, *_args, **_kwargs):
        document = batch.items[0].document_id
        started.add(document)
        if len(started) == len(KEYS):
            release.set()
        await release.wait()
        if document == first:
            raise OSError("expected global stop")
        if document == second:
            raise AssertionError("unexpected sibling failure")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(document)
            raise

    original_wait = atomic.asyncio.wait

    async def wait_after_siblings_run(*args, **kwargs):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(atomic, "run_workflow", fail_two)
    monkeypatch.setattr(atomic.asyncio, "wait", wait_after_siblings_run)
    with pytest.raises(AssertionError, match="unexpected sibling failure"):
        asyncio.run(run_atomic(case.work_dir, model=model()))
    assert len(started) == len(KEYS) and cancelled == started - {first, second}


def test_all_disabled_keys_pause_before_dispatch(tmp_path, monkeypatch):
    from engine.agents.pool import WorkflowState

    case = prepared(tmp_path)
    bound = model()
    pool_state = WorkflowState(len(KEYS))
    for slot in pool_state.slots:
        slot.disabled = True
    cast(Any, bound)._epubox_pool_state = pool_state

    def factory(self, configured):
        async def forbidden(*args):
            raise AssertionError("disabled keys must not dispatch")

        return forbidden

    monkeypatch.setattr(ModelRuntime, "_agno_transport", factory)
    events = []
    result = asyncio.run(run_atomic(case.work_dir, model=bound, progress=events.append))
    assert result.status == "paused" and result.http_attempts == 0
    assert "key 已停用" in (result.reason or "")
    assert events[-1]["stop_reason"] == result.reason


def test_terms_automatically_dispatch_across_all_unique_keys(tmp_path, monkeypatch):
    from engine.services.terms.runner import TermRunner
    from tests.engine.services.terms.runner import _prepare

    store, _ = _prepare(tmp_path, max_input_tokens=1450)
    active = 0
    peak = 0
    keys = set()

    def factory(self, bound):
        async def call(kind, payload):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            keys.add(bound.api_key)
            try:
                await asyncio.sleep(0.01)
                return {
                    "raw": json.dumps(
                        {
                            "protocol": "epubox-terms-1",
                            "request_id": payload["request_id"],
                            "items": [{"item_id": item["item_id"], "candidates": []} for item in payload["items"]],
                        }
                    )
                }
            finally:
                active -= 1

        return call

    monkeypatch.setattr(ModelRuntime, "_agno_transport", factory)
    bound = model()
    bound.id = "fake"
    bound.max_completion_tokens = 10400
    runner = TermRunner(store, model=bound)
    assert runner.max_concurrency == 3
    result = asyncio.run(runner.run())
    assert result.pending == 0
    assert peak == 3 and keys == set(KEYS)


def test_resolution_independent_batches_overlap_and_commit_each_group(tmp_path, monkeypatch):
    from engine.services.terms.resolution import TermResolutionRunner
    from tests.engine.services.terms.resolution import _add_conflict, _seed_conflict

    store, pool = _seed_conflict(tmp_path)
    pool = _add_conflict(store, pool)
    active = 0
    peak = 0
    keys = set()

    def factory(self, bound):
        async def call(kind, payload):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            keys.add(bound.api_key)
            try:
                await asyncio.sleep(0.01)
                return {
                    "raw": json.dumps(
                        {
                            "protocol": "epubox-term-resolution-2",
                            "request_id": payload["request_id"],
                            "items": [
                                {
                                    "group_id": group["group_id"],
                                    "decision": "defer",
                                    "selected_candidate_ids": [],
                                    "reason": "synthetic review",
                                }
                                for group in payload["items"]
                            ],
                        }
                    )
                }
            finally:
                active -= 1

        return call

    monkeypatch.setattr(ModelRuntime, "_agno_transport", factory)
    bound = model()
    bound.id = "fake"
    bound.max_completion_tokens = 10400
    runner = TermResolutionRunner(store, model=bound)
    monkeypatch.setattr(runner, "_batches", lambda groups: tuple((group,) for group in groups))
    result = asyncio.run(runner.run())
    assert result.status == "closed" and result.pending == 0
    assert peak == 2 and len(keys) == 2
    assert len(runner.decisions()) == len(pool.conflict_groups)


@pytest.mark.parametrize("cap", (None, 1, 2, 8))
def test_cli_operational_cap_changes_capacity_without_changing_preparation_identity(tmp_path, monkeypatch, cap):
    source = make_epub(tmp_path / "book.epub")
    observed = []
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: model())
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())

    async def advance(actual_source, output, root, config, checker, *, model, **kwargs):
        runtime = ModelRuntime(model=model, model_max_output_tokens=10000)
        observed.append((runtime.workflow_capacity, config.translation_config["concurrency"], workflow_limit.get()))
        return cli.RunOutcome("paused", root, "translation")

    monkeypatch.setattr(cli, "_advance_source", advance)
    cli.translate_book(source, concurrency=cap)
    assert observed == [(min(cap or 3, 3), 2, cap)]
    assert workflow_limit.get() is None


def test_explicit_agnes_cap_is_compatible_with_existing_frozen_configuration(tmp_path, monkeypatch):
    source = make_epub(tmp_path / "book.epub")
    root = source.with_suffix("")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    state.initialize(root, source, source_hash, "existing")
    asyncio.run(
        prepare_translation(
            source,
            root,
            PreparationConfig(
                run_id="existing",
                expected_source_hash=source_hash,
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={"provider": "agnes", "model": cli.settings.AGNES_MODEL, "concurrency": 1},
            ),
            StubChecker(),
        )
    )
    before = state.read(root / "preparation.json")
    observed = []

    def resume(root, **kwargs):
        observed.append(workflow_limit.get())
        return cli.RunOutcome("needs_attention", root, "translation")

    monkeypatch.setattr(cli, "resume_book", resume)
    cli.translate_book(source, concurrency=3, explicit_options=frozenset({"concurrency"}))
    assert observed == [3]
    assert state.read(root / "preparation.json") == before
