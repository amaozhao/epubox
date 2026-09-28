from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from engine.orchestrator_v23 import BatchJob, Job, TranslationEngine
from engine.schemas.v23 import Attempt, ItemStatus
from tests.v23.test_orchestrator_v23 import _make_store


class BatchTransport:
    def __init__(
        self,
        *,
        missing: Iterable[str] = (),
        truncate_first_batch: bool = False,
        bad_batch_stages: Iterable[str] = (),
        always_bad_stages: Iterable[str] = (),
        bad_once_stages: Iterable[str] = (),
        replace_once: str | None = None,
    ) -> None:
        self.missing = set(missing)
        self.truncate_first_batch = truncate_first_batch
        self.truncated = False
        self.bad_batch_stages = set(bad_batch_stages)
        self.always_bad_stages = set(always_bad_stages)
        self.bad_once_stages = set(bad_once_stages)
        self.bad_stages_sent: set[str] = set()
        self.replace_once = replace_once
        self.replaced = False
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        item_ids = tuple(item["item_id"] for item in payload["items"])
        self.calls.append((kind, item_ids))
        if kind in self.always_bad_stages or (kind in self.bad_once_stages and kind not in self.bad_stages_sent):
            self.bad_stages_sent.add(kind)
            return {"raw": "{bad", "usage": {"input_tokens": 10, "output_tokens": 1}}
        if kind in self.bad_batch_stages and kind not in self.bad_stages_sent and len(item_ids) > 1:
            self.bad_stages_sent.add(kind)
            return {"raw": "{bad", "usage": {"input_tokens": 10, "output_tokens": 1}}
        if self.truncate_first_batch and not self.truncated and kind == "translate" and len(item_ids) > 1:
            self.truncated = True
            return {"raw": "", "usage": {}, "finish_reason": "length"}
        if kind == "translate":
            items = [
                {"item_id": item_id, "target": f"译文-{item_id}"}
                for item_id in item_ids
                if item_id not in self.missing
            ]
            protocol = "epubox-text-1"
        elif kind == "review":
            items = []
            for request_item in payload["items"]:
                applicability = request_item["applicability"]
                replace = request_item["item_id"] == self.replace_once and not self.replaced
                if replace:
                    self.replaced = True
                items.append(
                    {
                        "item_id": request_item["item_id"],
                        "base_revision": request_item["base_revision"],
                        "decision": "replace" if replace else "no_change",
                        "checks": {
                            "accuracy": "pass",
                            "fluency": "pass",
                            "terminology": "pass" if applicability["terminology"] else "not_applicable",
                            "bindings": "pass" if applicability["bindings"] else "not_applicable",
                            "script": "pass",
                        },
                        "issues": [],
                        **({"target": f"修订-{request_item['item_id']}"} if replace else {}),
                    }
                )
            protocol = "epubox-review-1"
        else:
            window = payload["items"][0]
            items = [{"item_id": window["item_id"], "unit_ids": [], "issues": []}]
            protocol = "epubox-coherence-1"
        return {
            "raw": json.dumps(
                {"protocol": protocol, "request_id": payload["request_id"], "items": items},
                ensure_ascii=False,
            ),
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


def _engine(store: Any, transport: BatchTransport) -> TranslationEngine:
    engine = TranslationEngine(store, transport=transport)
    engine.config = engine.config.model_copy(update={"max_context_tokens": 16_000, "max_output_tokens": 2_048})
    return engine


@pytest.mark.asyncio
async def test_partial_batch_saves_valid_items_and_scopes_the_missing_item(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("One.",)), ("u2", ("Two.",)), ("u3", ("Three.",)), ("u4", ("Four.",))),),
        concurrency=1,
    )
    transport = BatchTransport(missing={"u2:e0:s0"})

    report = await _engine(store, transport).execute()

    assert transport.calls[0] == (
        "translate",
        ("u1:e0:s0", "u2:e0:s0", "u3:e0:s0", "u4:e0:s0"),
    )
    assert report["outcome"] == "needs_attention"
    assert store.load_unit("u2").items["u2:e0:s0"].status == ItemStatus.NEEDS_ATTENTION
    assert all(store.load_unit(unit_id).accepted_revision == 0 for unit_id in ("u1", "u3", "u4"))
    first_request = next(
        store.read_request(path.stem)
        for path in (tmp_path / "requests").glob("*.json")
        if len(store.read_request(path.stem).item_ids) == 4
    )
    assert len(first_request.attempts) == 1
    assert all(store.load_unit(unit_id).counters.http_attempts == 2 for unit_id in ("u1", "u3", "u4"))


@pytest.mark.asyncio
async def test_malformed_translation_batch_is_repaired_as_single_items_once(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("One.",)), ("u2", ("Two.",)), ("u3", ("Three.",)), ("u4", ("Four.",))),),
        concurrency=1,
    )
    transport = BatchTransport(bad_batch_stages={"translate"})

    report = await _engine(store, transport).execute()

    translation_calls = [item_ids for kind, item_ids in transport.calls if kind == "translate"]
    assert [len(item_ids) for item_ids in translation_calls] == [4, 1, 1, 1, 1]
    assert report["ready_to_publish"] is True
    for unit_id in ("u1", "u2", "u3", "u4"):
        item = store.load_unit(unit_id).items[f"{unit_id}:e0:s0"]
        assert item.attempts["translate"] == 2
        assert item.attempts["translate_protocol_repair"] == 1


@pytest.mark.asyncio
async def test_root_protocol_failure_is_persisted_at_request_scope(tmp_path: Path) -> None:
    store = _make_store(tmp_path, ((("u1", ("One.",)), ("u2", ("Two.",))),))
    engine = TranslationEngine(store, transport=BatchTransport())
    engine._load()
    jobs = tuple(Job("translate", unit_id, f"{unit_id}:e0:s0", 0, 0, "d1") for unit_id in ("u1", "u2"))
    _, manifest = engine._make_batch_request(BatchJob(jobs))
    await engine._reserve(
        manifest.request_id,
        Attempt(
            attempt_id="attempt-1",
            affected_items=manifest.item_ids,
            created_at="2026-09-28T00:00:00+00:00",
        ),
    )

    for job in jobs:
        engine._protocol_failure(job, manifest, "invalid complete JSON")

    for unit_id in ("u1", "u2"):
        item = store.load_unit(unit_id).items[f"{unit_id}:e0:s0"]
        assert item.status == ItemStatus.RETRY_WAIT
        assert item.failure is not None and item.failure.scope == "request"
        assert item.attempts["translate_protocol_repair"] == 1


@pytest.mark.asyncio
async def test_malformed_review_batch_preserves_replacement_recheck_budget(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("One.",)), ("u2", ("Two.",)), ("u3", ("Three.",))),),
        concurrency=1,
    )
    transport = BatchTransport(bad_batch_stages={"review"}, replace_once="u1:e0:s0")

    report = await _engine(store, transport).execute()

    review_calls = [item_ids for kind, item_ids in transport.calls if kind == "review"]
    assert len(review_calls[0]) == 3
    assert all(len(item_ids) == 1 for item_ids in review_calls[1:])
    assert report["ready_to_publish"] is True
    revised = store.load_unit("u1")
    assert revised.revision == revised.accepted_revision == 1
    assert revised.items["u1:e0:s0"].attempts["review"] == 3
    assert revised.items["u1:e0:s0"].attempts["review_protocol_repair"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "initial_http"), [("translate", 2), ("review", 3)])
async def test_explicit_retry_starts_one_new_protocol_repair_cycle(
    tmp_path: Path, stage: str, initial_http: int
) -> None:
    store = _make_store(tmp_path, ((("u1", ("One.",)),),), concurrency=1)

    first = await _engine(store, BatchTransport(always_bad_stages={stage})).execute()

    assert first["outcome"] == "needs_attention"
    failed = store.load_unit("u1")
    item_id = "u1:e0:s0"
    assert failed.counters.http_attempts == initial_http
    assert failed.items[item_id].attempts[f"{stage}_protocol_repair"] == 1

    TranslationEngine(store, transport=BatchTransport()).retry_units(["u1"])
    restarted = store.load_unit("u1").items[item_id]
    assert restarted.attempts[f"{stage}_protocol_repair_cycle_start"] == 1
    assert store.load_unit("u1").counters.http_attempts == initial_http

    second = await _engine(store, BatchTransport(bad_once_stages={stage})).execute()

    repaired = store.load_unit("u1")
    assert second["ready_to_publish"] is True
    assert repaired.items[item_id].attempts[f"{stage}_protocol_repair"] == 2
    assert repaired.items[item_id].attempts[f"{stage}_protocol_repair_cycle_start"] == 1
    assert repaired.counters.http_attempts > initial_http


@pytest.mark.asyncio
async def test_truncated_batch_shrinks_without_blocking_other_units(tmp_path: Path) -> None:
    definitions = tuple((f"u{index}", (f"Text {index}.",)) for index in range(1, 7))
    store = _make_store(tmp_path, (definitions,), concurrency=2)
    transport = BatchTransport(truncate_first_batch=True)

    report = await _engine(store, transport).execute()

    assert transport.truncated is True
    assert report["ready_to_publish"] is True
    assert all(store.load_unit(f"u{index}").accepted_revision == 0 for index in range(1, 7))
    assert any(kind == "translate" and len(item_ids) == 4 for kind, item_ids in transport.calls)
    assert any(kind == "translate" and len(item_ids) == 2 for kind, item_ids in transport.calls)


@pytest.mark.asyncio
async def test_batch_http_accounting_and_unit_budget_are_request_scoped(tmp_path: Path) -> None:
    store = _make_store(
        tmp_path,
        ((("u1", ("One.",)), ("u2", ("Two.",)), ("u3", ("Three.",)), ("u4", ("Four.",))),),
        concurrency=1,
        run_http_limit=2,
    )
    exhausted = store.load_unit("u1")
    store.save_unit(
        exhausted.model_copy(
            update={
                "counters": exhausted.counters.model_copy(update={"http_attempts": exhausted.counters.unit_http_limit})
            }
        )
    )
    transport = BatchTransport()

    report = await _engine(store, transport).execute()

    assert report["http_attempts"] == 2
    assert store.load_unit("u1").items["u1:e0:s0"].status == ItemStatus.NEEDS_ATTENTION
    assert all("u1:e0:s0" not in item_ids for _, item_ids in transport.calls)
    assert all(store.load_unit(unit_id).accepted_revision == 0 for unit_id in ("u2", "u3", "u4"))
    assert store.load_unit("u2").counters.http_attempts == 2


def test_reconcile_reserved_batch_restores_logical_and_aggregate_attempts(tmp_path: Path) -> None:
    store = _make_store(tmp_path, ((("u1", ("One.",)), ("u2", ("Two.",))),))
    engine = TranslationEngine(store, transport=BatchTransport())
    engine._load()
    jobs = tuple(Job("translate", unit_id, f"{unit_id}:e0:s0", 0, 0, "d1") for unit_id in ("u1", "u2"))
    _, manifest = engine._make_batch_request(BatchJob(jobs))
    for index in range(2):
        store.reserve_attempt(
            manifest.request_id,
            Attempt(
                attempt_id=f"attempt-{index}",
                affected_items=manifest.item_ids,
                created_at=f"2026-09-28T00:00:0{index}+00:00",
            ),
        )

    recovered = TranslationEngine(store, transport=BatchTransport())
    recovered._load()

    for unit_id in ("u1", "u2"):
        record = recovered.records[unit_id]
        assert record.counters.http_attempts == 2
        assert record.counters.translation_attempts == 1
        assert record.items[f"{unit_id}:e0:s0"].attempts["translate"] == 1
