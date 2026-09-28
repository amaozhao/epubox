from __future__ import annotations

from engine.epub import assembly, replacer


def test_v23_assembly_is_independent_of_legacy_replacer_models():
    assert callable(assembly.assemble_document)
    assert not hasattr(replacer, "assemble_document")
    assert not any(hasattr(assembly, name) for name in ("Chunk", "EpubItem", "TranslationStatus"))
