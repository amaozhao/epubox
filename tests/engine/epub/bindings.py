from __future__ import annotations

from engine.epub.bindings import resolve_derived_navigation
from engine.item.atoms import extract_resource
from engine.item.extractor import extract_document


def _document(markup: str, path: str):
    return extract_document(markup, path, "source-sha")


def _xhtml(body: str) -> str:
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title></head><body>{body}</body></html>'


def _derived(document):
    return [binding for binding in document.derived_bindings if binding.get("kind") == "derived_navigation"]


def test_simple_local_navigation_label_binds_to_one_exact_title_without_mutating_source_units() -> None:
    chapter = _document(_xhtml('<h1 id="intro">Introduction</h1><p>Body text.</p>'), "OPS/chapter.xhtml")
    navigation = _document(
        _xhtml('<nav><ol><li><a href="chapter.xhtml#intro">Introduction</a></li></ol></nav>'),
        "OPS/nav.xhtml",
    )
    source_units = tuple(document.units for document in (chapter, navigation))
    source_slots = tuple(document.source_slots for document in (chapter, navigation))

    resolved = resolve_derived_navigation((chapter, navigation))
    repeated = resolve_derived_navigation(resolved)

    binding = _derived(resolved[1])[0]
    assert binding["target_resource"] == "OPS/chapter.xhtml"
    assert binding["fragment"] == "intro"
    assert binding["source_text"] == "Introduction"
    assert tuple(document.units for document in resolved) == source_units
    assert tuple(document.source_slots for document in resolved) == source_slots
    assert repeated == resolved


def test_external_mismatched_ambiguous_and_escaping_links_are_not_derived() -> None:
    chapter = _document(
        _xhtml("<h1>Same title</h1><h2>Same title</h2>"),
        "OPS/chapter.xhtml",
    )
    navigation = _document(
        _xhtml(
            '<nav><a href="https://example.com/chapter.xhtml">Same title</a>'
            '<a href="chapter.xhtml">Different label</a>'
            '<a href="chapter.xhtml">Same title</a>'
            '<a href="../../outside.xhtml">Same title</a>'
            '<a href="chapter.xhtml#missing">Same title</a></nav>'
        ),
        "OPS/nav.xhtml",
    )

    resolved = resolve_derived_navigation((chapter, navigation))

    assert _derived(resolved[1]) == []


def test_unique_unfragmented_chapter_title_is_derived() -> None:
    chapter = _document(_xhtml("<h1>Overview</h1>"), "OPS/chapter.xhtml")
    navigation = _document(_xhtml('<nav><a href="chapter.xhtml">Overview</a></nav>'), "OPS/nav.xhtml")

    resolved = resolve_derived_navigation((chapter, navigation))

    assert _derived(resolved[1])[0]["fragment"] == ""


def test_protected_title_and_multi_text_navigation_are_not_reused() -> None:
    protected = _document(
        _xhtml('<h1 id="protected">Intro <code>x</code></h1>'),
        "OPS/protected.xhtml",
    )
    simple = _document(_xhtml('<h1 id="simple">Intro duction</h1>'), "OPS/simple.xhtml")
    navigation = _document(
        _xhtml(
            '<nav><a href="protected.xhtml#protected">Intro x</a>'
            '<a href="simple.xhtml#simple">Intro <span>duction</span></a></nav>'
        ),
        "OPS/nav.xhtml",
    )

    resolved = resolve_derived_navigation((protected, simple, navigation))

    assert _derived(resolved[2]) == []


def test_atomic_epub_navigation_anchor_binds_to_matching_heading() -> None:
    chapter = extract_resource(
        _xhtml('<h1 id="intro">Introduction</h1><p>Body text.</p>').encode(),
        "OPS/chapter.xhtml",
        "source-sha",
    ).document
    navigation = extract_resource(
        (
            b'<html xmlns="http://www.w3.org/1999/xhtml" '
            b'xmlns:epub="http://www.idpf.org/2007/ops"><head/><body>'
            b'<nav epub:type="toc"><ol><li><a class="entry" href="chapter.xhtml#intro">'
            b"Introduction</a></li></ol></nav></body></html>"
        ),
        "OPS/nav.xhtml",
        "source-sha",
    ).document

    resolved = resolve_derived_navigation((chapter, navigation))

    binding = _derived(resolved[1])[0]
    label = next(unit for unit in resolved[1].units if unit.kind == "navigation")
    assert binding["unit_id"] == label.unit_id
    assert binding["source_unit_id"] == next(unit.unit_id for unit in chapter.units if unit.kind == "heading")
    assert label.region["navigation_anchor"] is True
