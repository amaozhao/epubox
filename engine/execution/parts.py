"""Discover and freeze refinements for oversized legacy list members."""

from __future__ import annotations

from collections.abc import Collection

from engine.schemas.contracts import ItemStatus, parse_contract
from engine.schemas.parts import PartPlan
from engine.services import state
from engine.services.failures import CONTENT_FAILURE_LIMIT, failure_budget_available, failure_count
from engine.services.partition import build_plan, plan_path, validate_plan
from engine.services.ready import request_source_limit


def ensure_part_plans(journal, *, restart: bool = False) -> tuple[PartPlan, ...]:
    """Return every persisted part plan, creating plans for eligible parents."""
    with state.batch(journal.store.root):
        return _ensure_part_plans(journal, restart=restart)


def _ensure_part_plans(journal, *, restart: bool = False) -> tuple[PartPlan, ...]:
    session = journal.session
    limit = request_source_limit(session.prepared.plan.translation_config)
    records = journal.records()
    saved: dict[str, PartPlan] = {}
    for path in state.glob(journal.store.root / "parts", "*/plan.json"):
        plan = parse_contract(state.read(path), PartPlan, "epubox-parts-1")
        if plan.parent_id not in session.index.members_by_id:
            continue
        if plan.parent_id in saved:
            raise ValueError(f"duplicate part plan for {plan.parent_id}")
        validate_plan(session, plan)
        saved[plan.parent_id] = plan
    plans: list[PartPlan] = []
    for parent in session.index.members:
        path = plan_path(journal.store.root, parent.item_id)
        if (plan := saved.pop(parent.item_id, None)) is not None:
            plans.append(plan)
            continue
        record = records[parent.item_id]
        if record.status == ItemStatus.REVIEWED or parent.unit_id in session.prepared.plan.derived_sources:
            continue
        try:
            oversized_list = parent.atomic_tag in {"ul", "ol"} and session.index.source_tokens[parent.item_id] > limit
            stage = record.stage if record.stage in {"translate", "review"} else None
            failures = failure_count(journal, record, stage) if stage is not None else 0
            plan = (
                build_plan(session, parent, record)
                if oversized_list
                else build_plan(
                    session,
                    parent,
                    record,
                    trigger_stage=stage,
                    trigger_count=failures,
                    restart=restart,
                )
                if failures >= (1 if restart else CONTENT_FAILURE_LIMIT)
                and (restart or failure_budget_available(journal, parent.item_id))
                else None
            )
        except ValueError:
            continue
        if plan is None:
            continue
        journal.store._atomic_write(path, plan)
        plans.append(plan)
    if saved:
        raise ValueError("part plan references an unknown parent")
    return tuple(plans)


class WithoutRecords:
    """Delegate a journal while hiding records owned by part plans."""

    def __init__(self, journal, excluded: Collection[str]):
        self._journal = journal
        self._excluded = frozenset(excluded)

    def records(self):
        return {
            item_id: record for item_id, record in self._journal.records().items() if item_id not in self._excluded
        }

    def __getattr__(self, name):
        return getattr(self._journal, name)


def owns_request(journal, request) -> bool:
    """Whether every request member belongs to this journal's session."""
    members = journal.session.index.members_by_id
    return all(item_id in members for item_id in request.item_ids)


__all__ = ["WithoutRecords", "ensure_part_plans", "owns_request"]
