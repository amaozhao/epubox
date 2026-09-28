"""v2.5 source-plan adapter over the proven v2.3 XML extractor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from engine.item.extractor import (
    ADAPTER_VERSION,
    EXTRACTOR_VERSION,
)
from engine.item.extractor import (
    extract_document as extract_document_v23,
)
from engine.item.source_views import SourceViewGap, derive_source_views
from engine.schemas.v23 import DocumentPlan as V23DocumentPlan
from engine.schemas.v25 import (
    DocumentPlan,
    NodeRecord,
    RegistryEntry,
    ResourceRecord,
    SlotRange,
    SourceSlot,
    Unit,
)


def extract_document(
    source_markup: str,
    resource_path: str,
    source_hash: str,
    media_type: str = "application/xhtml+xml",
    config: Mapping[str, Any] | None = None,
    styles: Any = None,
) -> DocumentPlan:
    """Extract a v2.5 immutable source plan without duplicating XML ownership logic."""

    source = extract_document_v23(source_markup, resource_path, source_hash, media_type, config, styles)
    return _to_v25(source)


def _to_v25(source: V23DocumentPlan) -> DocumentPlan:
    derived = derive_source_views(source)
    issues = (*source.preparation_issues, *(_gap_issue(gap) for gap in derived.coverage_gaps))
    return DocumentPlan(
        document_id=source.document_id,
        source_hash=source.source_hash,
        resource=ResourceRecord.model_validate(source.resource.model_dump(mode="python")),
        adapter_version=source.adapter_version,
        extractor_version=source.extractor_version,
        source_markup=source.source_markup,
        nodes={key: NodeRecord.model_validate(node.model_dump(mode="python")) for key, node in source.nodes.items()},
        source_slots={
            key: SourceSlot(
                slot_id=slot.slot_id,
                node_key=slot.node_key,
                field=slot.field,
                source_value=slot.source_value,
                ranges=tuple(SlotRange.model_validate(part.model_dump(mode="python")) for part in slot.ranges),
                attribute_name=slot.attribute_name,
            )
            for key, slot in source.source_slots.items()
        },
        source_views=derived.source_views,
        units=tuple(
            Unit(
                unit_id=unit.unit_id,
                document_id=unit.document_id,
                kind=unit.kind,
                source_projection=unit.source_projection,
                node_key=unit.node_key,
                slot_ids=unit.slot_ids,
                registry={
                    key: RegistryEntry.model_validate(entry.model_dump(mode="python"))
                    for key, entry in unit.registry.items()
                },
                source_view_ids=derived.unit_source_view_ids[unit.unit_id],
                checks=unit.checks,
                region=unit.region,
            )
            for unit in source.units
        ),
        boundaries=source.boundaries,
        derived_bindings=source.derived_bindings,
        preparation_issues=issues,
    )


def _gap_issue(gap: SourceViewGap) -> dict[str, Any]:
    return {
        "scope": "unit",
        "stage": "source_view",
        "code": "source_view_gap",
        "unit_id": gap.unit_id,
        "reason": gap.reason,
        "source_refs": [ref.model_dump(mode="json") for ref in gap.source_refs],
    }


__all__ = ["ADAPTER_VERSION", "EXTRACTOR_VERSION", "extract_document"]
