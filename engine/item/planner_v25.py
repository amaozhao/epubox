"""Pure frozen-glossary selection and initial v2.5 Unit planning."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import regex

from engine.item.inline import parse_projection, plain_text
from engine.item.planner import PlannerConfig, validate_cut_plan
from engine.item.planner import plan_unit as plan_unit_v23
from engine.schemas import v23
from engine.schemas.v25 import (
    CutPlan,
    DocumentPlan,
    FrozenTerm,
    GlossarySnapshot,
    ItemRecord,
    JsonValue,
    Segment,
    Unit,
    canonical_hash,
    compute_input_hash,
    cut_plan_hash,
    glossary_rules_hash,
    segment_hash,
    validate_cut_plan_coverage,
)

PLANNER_VERSION = "epubox-unit-planner-3"
type TermRole = Literal["target", "context"]


@dataclass(frozen=True)
class TermSelection:
    terms: tuple[FrozenTerm, ...]
    applicability: dict[str, TermRole]
    terms_hash: str
    context_hash: str
    context: dict[str, Any]

    @property
    def selected_term_ids(self) -> tuple[str, ...]:
        return tuple(term.term_id for term in self.terms)


@dataclass(frozen=True)
class UnitPlanInitialization:
    logical_hash: str
    input_hash: str
    cut_plan: CutPlan
    items: dict[str, ItemRecord]


def select_terms(
    unit: Unit,
    document: DocumentPlan,
    glossary: GlossarySnapshot,
    *,
    source_projection: str | None = None,
) -> TermSelection:
    """Select every matching frozen rule without truncation or cross-scope promotion."""
    _validate_inputs(unit, document, glossary)
    projection = unit.source_projection if source_projection is None else source_projection
    target_text = plain_text(projection)
    context = _context_payload(unit, document)
    target_views = [document.source_views[view_id] for view_id in unit.source_view_ids]
    context_views = [document.source_views[view_id] for view_id in unit.context_view_ids]
    hint_text = "\n".join(
        value
        for hint in context["hints"].values()
        if isinstance(hint, dict)
        for value in hint.values()
        if isinstance(value, str)
    )

    selected: list[FrozenTerm] = []
    roles: dict[str, TermRole] = {}
    for term in sorted(glossary.terms, key=lambda value: value.term_id):
        role: TermRole | None = None
        if (
            _scope_applies(term, unit.unit_id, unit.document_id)
            and _term_occurs(term, target_text)
            and any(_term_occurs(term, view.text) for view in target_views)
        ):
            role = "target"
        elif (_scope_applies(term, unit.unit_id, unit.document_id) and _term_occurs(term, hint_text)) or any(
            _scope_applies(term, view.unit_id, view.document_id) and _term_occurs(term, view.text)
            for view in context_views
        ):
            role = "context"
        if role is not None:
            selected.append(term)
            roles[term.term_id] = role

    terms = tuple(selected)
    terms_payload = {"terms": [_term_payload(term, roles[term.term_id]) for term in terms]}
    return TermSelection(
        terms=terms,
        applicability=roles,
        terms_hash=canonical_hash(terms_payload),
        context_hash=canonical_hash(context),
        context=context,
    )


def plan_unit_v25(
    unit: Unit,
    document: DocumentPlan,
    glossary: GlossarySnapshot,
    translation_config: Mapping[str, JsonValue],
    *,
    epoch: int = 0,
) -> UnitPlanInitialization:
    """Build immutable v2.5 Segment identities and the hashes needed by UnitRecord."""
    _validate_inputs(unit, document, glossary)
    planner_config = _planner_config(translation_config)
    unit_selection = select_terms(unit, document, glossary)
    temporary_logical_hash = canonical_hash(
        {"version": PLANNER_VERSION, "unit_id": unit.unit_id, "source_projection": unit.source_projection}
    )
    legacy_unit = v23.Unit(
        unit_id=unit.unit_id,
        document_id=unit.document_id,
        kind=unit.kind,
        source_projection=unit.source_projection,
        node_key=unit.node_key,
        slot_ids=unit.slot_ids,
        registry={
            ref_id: v23.RegistryEntry.model_validate(entry.model_dump(mode="python"))
            for ref_id, entry in unit.registry.items()
        },
        context={"source_context": json.dumps(unit_selection.context, ensure_ascii=False, sort_keys=True)},
        terms=tuple(_term_payload(term, unit_selection.applicability[term.term_id]) for term in unit_selection.terms),
        checks=unit.checks,
        region=unit.region,
        logical_hash=temporary_logical_hash,
    )
    legacy_plan = plan_unit_v23(legacy_unit, planner_config, epoch=epoch)
    validate_cut_plan(legacy_unit, legacy_plan)

    segments: list[Segment] = []
    selections: list[TermSelection] = []
    for legacy in legacy_plan.segments:
        selection = select_terms(unit, document, glossary, source_projection=legacy.source_projection)
        selections.append(selection)
        data = {
            "segment_id": legacy.segment_id,
            "item_id": legacy.item_id,
            "source_start": legacy.source_start,
            "source_end": legacy.source_end,
            "source_projection": legacy.source_projection,
            "selected_term_ids": selection.selected_term_ids,
            "term_applicability": selection.applicability,
            "terms_hash": selection.terms_hash,
            "context_hash": selection.context_hash,
            "virtual_boundaries": legacy.virtual_boundaries,
        }
        segments.append(Segment(**data, segment_hash=segment_hash(data)))

    plan_data = {"plan_epoch": epoch, "segments": tuple(segments)}
    cut_plan = CutPlan(**plan_data, plan_hash=cut_plan_hash(plan_data))
    validate_cut_plan_coverage(cut_plan, _event_count(unit.source_projection))
    if tuple(segment.source_projection for segment in cut_plan.segments) != tuple(
        segment.source_projection for segment in legacy_plan.segments
    ):
        raise ValueError("v2.5 conversion changed a planned source projection")

    rules_hash = glossary_rules_hash(glossary.terms)
    logical_hash = canonical_hash(
        {
            "version": PLANNER_VERSION,
            "source_hash": document.source_hash,
            "unit": unit.model_dump(mode="json"),
            "source_views": [
                document.source_views[view_id].model_dump(mode="json") for view_id in unit.source_view_ids
            ],
            "context_hash": unit_selection.context_hash,
            "freeze_id": glossary.freeze_id,
            "glossary_rules_hash": rules_hash,
            "segments": [
                {
                    "item_id": segment.item_id,
                    "selected_term_ids": list(segment.selected_term_ids),
                    "term_applicability": segment.term_applicability,
                    "terms_hash": segment.terms_hash,
                    "context_hash": segment.context_hash,
                }
                for segment in segments
            ],
            "translation_config": dict(translation_config),
        }
    )
    items = {
        segment.item_id: ItemRecord(
            item_id=segment.item_id,
            segment_id=segment.segment_id,
            selected_term_ids=segment.selected_term_ids,
            term_applicability=segment.term_applicability,
            terms_hash=segment.terms_hash,
            context_hash=segment.context_hash,
        )
        for segment in segments
    }
    return UnitPlanInitialization(
        logical_hash=logical_hash,
        input_hash=compute_input_hash(logical_hash, cut_plan.plan_hash),
        cut_plan=cut_plan,
        items=items,
    )


def _validate_inputs(unit: Unit, document: DocumentPlan, glossary: GlossarySnapshot) -> None:
    planned_unit = next((item for item in document.units if item.unit_id == unit.unit_id), None)
    if unit.document_id != document.document_id or planned_unit != unit:
        raise ValueError("Unit does not belong to the DocumentPlan")
    if document.source_hash != glossary.source_hash:
        raise ValueError("glossary and document source identities differ")
    missing = (set(unit.source_view_ids) | set(unit.context_view_ids)) - document.source_views.keys()
    if missing:
        raise ValueError(f"Unit references unknown source views: {sorted(missing)}")


def _context_payload(unit: Unit, document: DocumentPlan) -> dict[str, Any]:
    return {
        "views": [
            {
                "view_id": view.view_id,
                "unit_id": view.unit_id,
                "document_id": view.document_id,
                "text": view.text,
                "view_hash": view.view_hash,
                "role": "context",
            }
            for view_id in unit.context_view_ids
            for view in (document.source_views[view_id],)
        ],
        "hints": {
            ref_id: _hint_payload(entry)
            for ref_id, entry in sorted(unit.registry.items())
            if entry.hints or entry.kind == "g"
        },
    }


def _hint_payload(entry: Any) -> dict[str, JsonValue]:
    hints: dict[str, JsonValue] = dict(entry.hints)
    if entry.kind == "g":
        excerpt = _excerpt(entry.source_text)
        hints["excerpt"] = excerpt
        hints["excerpt_truncated"] = len(excerpt) < len(entry.source_text)
    return hints


def _term_payload(term: FrozenTerm, role: TermRole) -> dict[str, JsonValue]:
    return {
        "term_id": term.term_id,
        "source": term.source,
        "target": term.target,
        "aliases": list(term.aliases),
        "scope": term.scope.model_dump(mode="json"),
        "mode": term.mode,
        "match_policy": term.match_policy,
        "note": term.note,
        "role": role,
    }


def _scope_applies(term: FrozenTerm, unit_id: str, document_id: str) -> bool:
    if term.scope.kind == "book":
        return True
    if term.scope.kind == "documents":
        return document_id in term.scope.document_ids
    return unit_id in term.scope.unit_ids


def _term_occurs(term: FrozenTerm, text: str) -> bool:
    return any(_contains(text, spelling, term.match_policy == "casefold") for spelling in (term.source, *term.aliases))


def _contains(text: str, spelling: str, casefold: bool) -> bool:
    left = r"(?<!\w)" if spelling[0].isalnum() or spelling[0] == "_" else ""
    right = r"(?!\w)" if spelling[-1].isalnum() or spelling[-1] == "_" else ""
    return re.search(left + re.escape(spelling) + right, text, re.IGNORECASE if casefold else 0) is not None


def _excerpt(value: str, limit: int = 400) -> str:
    selected: list[str] = []
    size = 0
    for match in regex.finditer(r"\X", value):
        cluster = match.group()
        if size + len(cluster) > limit:
            break
        selected.append(cluster)
        size += len(cluster)
    return "".join(selected)


def _event_count(projection: str) -> int:
    return sum(
        1 if event.kind == "marker" else len(regex.findall(r"\X", event.value))
        for event in parse_projection(projection)
    )


def _planner_config(config: Mapping[str, JsonValue]) -> PlannerConfig:
    context = _integer(config, "context_tokens", _integer(config, "max_context_tokens", 8192))
    output = _integer(config, "max_output_tokens", 2048)
    return PlannerConfig(
        context_tokens=context,
        max_input_tokens=_optional_integer(config, "max_input_tokens"),
        max_output_tokens=output,
        review_output_tokens=_integer(config, "review_output_tokens", min(768, output)),
        safety_margin=_integer(config, "safety_margin", 256),
        translation_overhead=_integer(config, "translation_overhead", 256),
        review_overhead=_integer(config, "review_overhead", 512),
        target_ratio=_number(config, "target_ratio", 1.6),
        max_batch_items=_integer(config, "max_batch_items", 8),
    )


def _integer(config: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"translation_config.{key} must be an integer")
    return value


def _optional_integer(config: Mapping[str, JsonValue], key: str) -> int | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"translation_config.{key} must be an integer or null")
    return value


def _number(config: Mapping[str, JsonValue], key: str, default: float) -> float:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"translation_config.{key} must be numeric")
    return float(value)


__all__ = [
    "PLANNER_VERSION",
    "TermSelection",
    "UnitPlanInitialization",
    "plan_unit_v25",
    "select_terms",
]
