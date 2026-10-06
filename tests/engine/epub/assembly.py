from __future__ import annotations

from engine.epub import assembly


def test_assembly_uses_source_plans_without_html_chunk_models():
    assert callable(assembly.assemble_document)
    assert not any(hasattr(assembly, name) for name in ("Chunk", "EpubItem", "TranslationStatus"))
