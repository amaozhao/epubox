from __future__ import annotations

import pytest

from engine.epub.publication import validate_assembled_document
from engine.epub.replacer import assemble_document
from engine.epub.validation import EpubValidationError
from engine.item.extractor import extract_document


def _document(source: str):
    return extract_document(source, "OEBPS/chapter.xhtml", "source-hash")


def _identity_targets(document):
    return {unit.unit_id: unit.source_projection for unit in document.units}


def test_identity_validation_keeps_a_protected_tail_space_at_its_real_position():
    document = _document('<html><body><p>A2A. <i>See</i> <a href="#agent">agent‐to‐agent</a></p></body></html>')
    paragraph = document.units[0]
    assert paragraph.source_projection == "A2A. ⟦+g1⟧See⟦-g1⟧⟦=x1⟧⟦+g2⟧agent‐to‐agent⟦-g2⟧"

    targets = _identity_targets(document)
    assembled = assemble_document(document, targets)
    validate_assembled_document(
        document,
        targets,
        assembled.markup,
        source_to_target=assembled.source_to_target,
    )


def test_chinese_target_expands_protected_slot_events_without_model_markup():
    document = _document('<html><body><p>A2A. <i>See</i> <a href="#agent">agent‐to‐agent</a></p></body></html>')
    unit = document.units[0]
    targets = {unit.unit_id: "A2A。⟦+g1⟧参见⟦-g1⟧⟦=x1⟧⟦+g2⟧代理到代理⟦-g2⟧"}
    assembled = assemble_document(document, targets)

    validate_assembled_document(
        document,
        targets,
        assembled.markup,
        source_to_target=assembled.source_to_target,
    )
    assert "A2A。<i>参见</i> <a" in assembled.markup


def test_repeated_equal_protected_values_are_checked_by_event_position():
    document = _document("<html><body><p>A<i>B</i> <i>C</i> <b>D</b></p></body></html>")
    unit = document.units[0]
    assert [entry.source_text for entry in unit.registry.values() if "slot_id" in entry.hints] == [" ", " "]

    targets = _identity_targets(document)
    assembled = assemble_document(document, targets)
    validate_assembled_document(
        document,
        targets,
        assembled.markup,
        source_to_target=assembled.source_to_target,
    )


def test_actual_deleted_target_or_protected_text_is_rejected():
    document = _document('<html><body><p>A2A. <i>See</i> <a href="#agent">agent‐to‐agent</a></p></body></html>')
    unit = document.units[0]
    targets = {unit.unit_id: "A2A。⟦+g1⟧参见⟦-g1⟧⟦=x1⟧⟦+g2⟧代理到代理⟦-g2⟧"}
    assembled = assemble_document(document, targets)

    with pytest.raises(EpubValidationError) as deleted_target:
        validate_assembled_document(
            document,
            targets,
            assembled.markup.replace("代理到代理", "代理代理"),
            source_to_target=assembled.source_to_target,
        )
    assert deleted_target.value.code == "target_text_mismatch"

    with pytest.raises(EpubValidationError) as deleted_space:
        validate_assembled_document(
            document,
            targets,
            assembled.markup.replace("</i> <a", "</i><a"),
            source_to_target=assembled.source_to_target,
        )
    assert deleted_space.value.code in {"protected_slot_changed", "target_text_mismatch"}
