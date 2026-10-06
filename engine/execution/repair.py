"""The single v2.5 translation and review executor."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from engine.execution.engine import TranslationEngine
from engine.execution.state import TranslationRunResult
from engine.execution.utility import (
    _journal_spent_for_unit,
    _segment,
    _unit_limit,
)
from engine.item.inline import (
    validate_projection,
)
from engine.schemas.contracts import (
    ItemRecord,
    ItemStatus,
    UnitRecord,
    canonical_hash,
    strict_json_loads,
)
from engine.services.coherence import (
    load_budget_overrides,
)
from engine.services.store import RunStore


async def run_translation(
    work_dir: Path | str,
    *,
    model: Any = None,
    transport: Any = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> TranslationRunResult:
    if (Path(work_dir) / "prepared.json").is_file():
        from engine.execution.atomic import run_atomic

        return await run_atomic(work_dir, model=model, transport=transport, progress=progress)
    return await TranslationEngine(RunStore(work_dir), model=model, transport=transport, progress=progress).run()


def retry_failed_units(store: RunStore, unit_ids: Sequence[str]) -> tuple[UnitRecord, ...]:
    """Explicitly reactivate selected failures without resetting counters."""
    with store.lock():
        validate_retry_failed_units(store, unit_ids)
        updated: list[UnitRecord] = []
        for unit_id in dict.fromkeys(unit_ids):
            record = store.read_unit(unit_id)
            counters = dict(record.counters)
            items = {
                item_id: item.model_copy(
                    update={"status": ItemStatus.RETRY_WAIT, "failure": None, "next_action": item.stage}
                )
                if item.status == ItemStatus.NEEDS_ATTENTION
                else item
                for item_id, item in record.items.items()
            }
            for item_id, item in record.items.items():
                if item.status != ItemStatus.NEEDS_ATTENTION:
                    continue
                stage = _item_failure_stage(item)
                key = f"explicit_retry_{stage}:{item_id}"
                counters[key] = counters.get(key, 0) + 1
            if items == record.items:
                continue
            updated.append(
                store.save_unit(
                    record.model_copy(
                        update={"record_version": record.record_version + 1, "items": items, "counters": counters}
                    ),
                    expected_record_version=record.record_version,
                )
            )
        return tuple(updated)


def _item_failure_stage(item: ItemRecord) -> Literal["translate", "review"]:
    failure = item.failure or {}
    return "review" if failure.get("stage") == "review" or item.target_projection is not None else "translate"


def validate_retry_failed_units(
    store: RunStore,
    unit_ids: Sequence[str],
    *,
    add_unit_http: int = 0,
) -> None:
    """Validate an entire retry action without changing Unit state."""
    if type(add_unit_http) is not int or add_unit_http < 0:
        raise ValueError("add_unit_http must be a non-negative integer")
    overrides = load_budget_overrides(store)
    additions = overrides.get("add_unit_http", {})
    for unit_id in dict.fromkeys(unit_ids):
        record = store.read_unit(unit_id)
        extra = additions.get(unit_id, 0) if isinstance(additions, dict) else 0
        used = max(record.counters.get("http_attempts", 0), _journal_spent_for_unit(store, unit_id))
        if used >= _unit_limit(record) + int(extra) + add_unit_http:
            raise ValueError(f"Unit HTTP budget remains exhausted: {unit_id}")


def import_repair_file(store: RunStore, path: Path | str) -> UnitRecord:
    """Import one complete version-bound target and require the normal review gate."""
    repaired = validate_repair_file(store, path)
    return store.save_unit(repaired, expected_record_version=repaired.record_version - 1)


def validate_repair_file(store: RunStore, path: Path | str) -> UnitRecord:
    """Validate a repair and return the proposed record without writing it."""
    value = strict_json_loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise TypeError("repair file must contain one JSON object")
    unit_id = value.get("unit_id")
    if not isinstance(unit_id, str):
        raise TypeError("repair file requires string unit_id")
    record = store.read_unit(unit_id)
    if value.get("base_revision") != record.revision or value.get("plan_epoch") != record.plan_epoch:
        raise ValueError("repair file is stale")
    if record.cut_plan is None:
        raise ValueError("repair target requires a CutPlan")
    preparation = store.read_preparation()
    document_id = preparation.unit_documents[unit_id]
    document = store.read_document(document_id, expected_hash=preparation.document_hashes[document_id])
    unit = next(item for item in document.units if item.unit_id == unit_id)
    raw_targets = value.get("targets")
    if raw_targets is None and len(record.items) == 1 and isinstance(value.get("target"), str):
        raw_targets = {next(iter(record.items)): value["target"]}
    if not isinstance(raw_targets, dict) or set(raw_targets) != set(record.items):
        raise ValueError("repair file must provide every current item target")
    items = dict(record.items)
    for item_id, target in raw_targets.items():
        if not isinstance(item_id, str) or not isinstance(target, str) or not target:
            raise ValueError("repair targets must be non-empty strings keyed by item_id")
        segment = _segment(record, item_id)
        validate_projection(segment.source_projection, target, unit.registry)
        items[item_id] = items[item_id].model_copy(
            update={
                "stage": "review",
                "status": ItemStatus.LOCAL_VALID,
                "target_projection": target,
                "target_hash": canonical_hash(target),
                "checks": {"manual_repair": True},
                "request_id": None,
                "failure": None,
                "next_action": "review",
            }
        )
    return record.model_copy(
        update={
            "record_version": record.record_version + 1,
            "revision": record.revision + 1,
            "items": items,
            "candidate": None,
            "accepted_revision": None,
            "accepted_target_hash": None,
            "local_checks": {},
            "review": None,
            "unresolved_issues": (),
        }
    )
