from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from engine.orchestrator_v23 import TranslationEngine
from engine.schemas.v23 import ItemStatus
from tests.v23.test_orchestrator_v23 import _make_store


class BatchTransport:
    def __init__(
        self,
        *,
        missing: Iterable[str] = (),
        truncate_first_batch: bool = False,
    ) -> None:
        self.missing = set(missing)
        self.truncate_first_batch = truncate_first_batch
        self.truncated = False
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        item_ids = tuple(item["item_id"] for item in payload["items"])
        self.calls.append((kind, item_ids))
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
                items.append(
                    {
                        "item_id": request_item["item_id"],
                        "base_revision": request_item["base_revision"],
                        "decision": "no_change",
                        "checks": {
                            "accuracy": "pass",
                            "fluency": "pass",
                            "terminology": "pass" if applicability["terminology"] else "not_applicable",
                            "bindings": "pass" if applicability["bindings"] else "not_applicable",
                            "script": "pass",
                        },
                        "issues": [],
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
