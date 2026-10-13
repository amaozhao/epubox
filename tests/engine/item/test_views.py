from __future__ import annotations

import pytest

import engine.item.views as views_module
from engine.item.structure import extract_document
from engine.item.views import SourceViewError, derive_source_views, validate_source_views
from engine.schemas.contracts import (
    DocumentPlan as V25DocumentPlan,
)
from engine.schemas.contracts import (
    NodeRecord,
    RegistryEntry,
    ResourceRecord,
    SlotRange,
    SourceSlot,
    Unit,
    canonical_hash,
    source_view_hash_payload,
)
from engine.schemas.internal import DocumentPlan


def _plan(body: str) -> DocumentPlan:
    source = f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head><body>{body}</body></html>'
    return extract_document(source, "OPS/chapter.xhtml", "source-hash")


def _paragraph_views(document: DocumentPlan):
    unit = next(unit for unit in document.units if unit.kind == "paragraph")
    result = derive_source_views(document)
    return unit, [result.source_views[view_id] for view_id in result.unit_source_view_ids[unit.unit_id]]


def _as_v25(document: DocumentPlan) -> V25DocumentPlan:
    derived = derive_source_views(document)
    return V25DocumentPlan(
        document_id=document.document_id,
        source_hash=document.source_hash,
        resource=ResourceRecord.model_validate(document.resource.model_dump()),
        adapter_version=document.adapter_version,
        extractor_version=document.extractor_version,
        source_markup=document.source_markup,
        nodes={key: NodeRecord.model_validate(value.model_dump()) for key, value in document.nodes.items()},
        source_slots={
            key: SourceSlot(
                slot_id=value.slot_id,
                node_key=value.node_key,
                field=value.field,
                source_value=value.source_value,
                ranges=tuple(SlotRange.model_validate(part.model_dump()) for part in value.ranges),
                attribute_name=value.attribute_name,
            )
            for key, value in document.source_slots.items()
        },
        source_views=derived.source_views,
        units=tuple(
            Unit(
                unit_id=value.unit_id,
                document_id=value.document_id,
                kind=value.kind,
                source_projection=value.source_projection,
                node_key=value.node_key,
                slot_ids=value.slot_ids,
                registry={
                    key: RegistryEntry.model_validate(entry.model_dump()) for key, entry in value.registry.items()
                },
                source_view_ids=derived.unit_source_view_ids[value.unit_id],
                checks=value.checks,
                region=value.region,
            )
            for value in document.units
        ),
        boundaries=document.boundaries,
        derived_bindings=document.derived_bindings,
        preparation_issues=document.preparation_issues,
    )


def test_inline_formatting_keeps_one_phrase_and_exact_slot_refs() -> None:
    document = _plan("<p>Use <em>source</em> text.</p>")
    unit, views = _paragraph_views(document)

    assert [view.text for view in views] == ["Use source text."]
    assert [ref.slot_id for ref in views[0].source_refs] == list(unit.slot_ids)
    assert (
        "".join(document.source_slots[ref.slot_id].source_value[ref.start : ref.end] for ref in views[0].source_refs)
        == views[0].text
    )


def test_inline_formatting_does_not_break_a_word() -> None:
    document = _plan("<p>Use pro<em>cess</em>or text.</p>")
    _, views = _paragraph_views(document)

    assert [view.text for view in views] == ["Use processor text."]


def test_hard_protection_splits_views_and_never_exposes_code() -> None:
    document = _plan("<p>A<code>foo()</code>B</p>")
    _, views = _paragraph_views(document)

    assert [view.text for view in views] == ["A", "B"]
    assert all("foo()" not in view.text for view in views)


def test_comment_tail_uses_recorded_non_element_parent() -> None:
    document = _plan("<p>Before<!--keep-->after</p>")
    _, views = _paragraph_views(document)

    assert [view.text for view in views] == ["Before", "after"]


def test_ordinary_spaces_survive_but_structural_indentation_creates_no_view() -> None:
    document = _plan("  \n<p>Use <em>source</em> text.</p>\n  ")
    result = derive_source_views(document)
    texts = [view.text for view in result.source_views.values()]

    assert "Use source text." in texts
    assert all(text.strip() for text in texts)
    assert all("\n" not in text for text in texts)


def test_repeated_values_in_two_slots_keep_their_own_source_refs() -> None:
    document = _plan("<p>same<em>same</em></p>")
    unit, views = _paragraph_views(document)

    assert [view.text for view in views] == ["samesame"]
    assert [ref.slot_id for ref in views[0].source_refs] == list(unit.slot_ids)
    assert len({ref.slot_id for ref in views[0].source_refs}) == 2


def test_repeated_slot_values_cannot_hide_reversed_source_ownership() -> None:
    document = _plan("<p>same<em>same</em></p>")
    unit = next(unit for unit in document.units if unit.kind == "paragraph")
    bad_unit = unit.model_copy(update={"slot_ids": tuple(reversed(unit.slot_ids))})
    bad_document = document.model_copy(
        update={"units": tuple(bad_unit if item.unit_id == unit.unit_id else item for item in document.units)}
    )

    with pytest.raises(SourceViewError, match="active projection domain"):
        derive_source_views(bad_document)


def test_projection_value_mismatch_is_rejected_instead_of_fuzzy_matched() -> None:
    document = _plan("<p>same<em>same</em></p>")
    unit = next(unit for unit in document.units if unit.kind == "paragraph")
    bad_unit = unit.model_copy(update={"source_projection": unit.source_projection.replace("same", "else", 1)})
    bad_document = document.model_copy(
        update={"units": tuple(bad_unit if item.unit_id == unit.unit_id else item for item in document.units)}
    )

    with pytest.raises(SourceViewError, match="projection/source mismatch"):
        derive_source_views(bad_document)


def test_slot_ownership_mismatch_is_rejected() -> None:
    document = _plan("<p>owned text</p>")
    unit = next(unit for unit in document.units if unit.kind == "paragraph")
    bad_unit = unit.model_copy(update={"slot_ids": ()})
    bad_document = document.model_copy(
        update={"units": tuple(bad_unit if item.unit_id == unit.unit_id else item for item in document.units)}
    )

    with pytest.raises(SourceViewError, match="slot ownership disagrees"):
        derive_source_views(bad_document)


def test_document_indexes_are_built_once_per_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _plan("<p>one</p><p>two</p><p>three</p>")
    original = views_module._source_view_index
    calls = 0

    def counting_index(document: DocumentPlan):
        nonlocal calls
        calls += 1
        return original(document)

    monkeypatch.setattr(views_module, "_source_view_index", counting_index)

    derive_source_views(document)

    assert calls == 1


def test_validation_replays_source_instead_of_trusting_a_recomputed_view_hash() -> None:
    document = _as_v25(_plan("<p>source text</p>"))
    validate_source_views(document)
    original = next(view for view in document.source_views.values() if view.text == "source text")
    data = {
        "unit_id": original.unit_id,
        "document_id": original.document_id,
        "text": "forged text",
        "source_refs": original.source_refs,
        "view_kind": original.view_kind,
    }
    forged = original.model_copy(
        update={"text": data["text"], "view_hash": canonical_hash(source_view_hash_payload(**data))}
    )
    forged_document = document.model_copy(update={"source_views": document.source_views | {original.view_id: forged}})

    with pytest.raises(SourceViewError, match="do not match frozen"):
        validate_source_views(forged_document)
