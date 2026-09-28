from __future__ import annotations

from pathlib import Path

import pytest

from engine.core.markup import UnsafeMarkupError, parse_xml_safely
from engine.epub.assembly import assemble_document
from engine.epub.publication import validate_assembled_document
from engine.item.extractor import extract_document, select_primary_title
from engine.item.inline import ProjectionError, validate_projection
from engine.schemas.v23 import DocumentPlan


def _plan(body: str, config: dict | None = None) -> DocumentPlan:
    source = (
        '<?xml version="1.0"?>\r\n<!DOCTYPE html>\r\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Example book</title></head>'
        f"<body>{body}</body></html>"
    )
    return extract_document(source, "OEBPS/chapter.xhtml", "source-hash", config=config)


def _identity_targets(document: DocumentPlan) -> dict[str, str]:
    return {unit.unit_id: unit.source_projection for unit in document.units}


def test_extract_keeps_source_crlf_and_assigns_body_tails_alt_and_nested_lists_once():
    document = _plan(
        "Bare <em>words</em> here.<p>Use <code>finally</code> now"
        '<img src="diagram.png" alt="Diagram text"/> after.</p>'
        "<ul><li>Outer<ul><li>Inner text</li></ul></li></ul>"
    )

    assert "\r\n" in document.source_markup
    assert any(unit.kind == "body_text_region" and "Bare" in unit.source_projection for unit in document.units)
    assert any(unit.kind == "attribute" and unit.source_projection == "Diagram text" for unit in document.units)
    assert sum("Outer" in unit.source_projection for unit in document.units) == 1
    assert sum("Inner text" in unit.source_projection for unit in document.units) == 1
    assert all(
        [(item.start, item.end) for item in slot.ranges]
        == list(zip([0, *[item.end for item in slot.ranges[:-1]]], [item.end for item in slot.ranges]))
        for slot in document.source_slots.values()
        if slot.source_value and slot.ranges
    )

    restored = assemble_document(document, {}, identity=True).markup
    assert restored.count("finally") == 1
    assert restored.count(" now") == 1
    assert restored.count(" after.") == 1


def test_translate_inheritance_and_existing_chinese_are_preserved_locally():
    document = _plan(
        '<p translate="no">Keep this <span translate="yes">Translate me</span>. 已有中文 and English.</p>'
    )
    unit = next(unit for unit in document.units if unit.kind == "paragraph")
    assert "Keep this " not in unit.source_projection
    assert "已有中文" not in unit.source_projection
    assert "Translate me" in unit.source_projection
    assert "and English" not in unit.source_projection

    targets = _identity_targets(document)
    targets[unit.unit_id] = unit.source_projection.replace("Translate me", "翻译我")
    result = assemble_document(document, targets).markup
    assert "Keep this " in result
    assert "已有中文" in result
    assert "翻译我" in result and "and English" in result


def test_same_parent_reorder_preserves_link_identity_and_attributes_apply_after_atoms():
    document = _plan(
        '<p>Compared with <a href="#ssd">an SSD</a>, <a href="#hdd">an HDD</a> is slower.</p>'
        '<p>See the chart.<img src="chart.png" alt="Performance chart"/></p>'
    )
    paragraph = next(unit for unit in document.units if unit.kind == "paragraph" and "g2" in unit.registry)
    attribute = next(unit for unit in document.units if unit.kind == "attribute")
    targets = _identity_targets(document)
    targets[paragraph.unit_id] = "⟦+g2⟧机械硬盘⟦-g2⟧比⟦+g1⟧固态硬盘⟦-g1⟧慢。"
    targets[attribute.unit_id] = "性能图"

    result = assemble_document(document, targets).markup
    assert '<a href="#hdd">机械硬盘</a>' in result
    assert '<a href="#ssd">固态硬盘</a>' in result
    assert 'src="chart.png" alt="性能图"' in result


def test_json_only_identity_restore_preserves_doctype_pi_comments_and_unit_contract():
    document = _plan("<?inside keep?><p>Hello<!--comment--> tail.</p>")
    loaded = DocumentPlan.model_validate_json(document.model_dump_json())
    result = assemble_document(loaded, {}, identity=True)

    assert "<!DOCTYPE html>" in result.markup
    assert "<?inside keep?>" in result.markup
    assert "<!--comment--> tail." in result.markup
    assert set(result.source_to_target) == set(document.nodes)
    assert any(boundary["kind"] == "non_element_tail" for boundary in document.boundaries)
    assert loaded == document


def test_safe_xml_allows_trusted_epub2_entities_but_blocks_local_external_entities():
    trusted = (
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"><html><body>&nbsp;</body></html>'
    )
    assert parse_xml_safely(trusted).getroot()[0].text == "\xa0"

    with pytest.raises(UnsafeMarkupError, match="external entity"):
        parse_xml_safely('<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><x>&leak;</x>')


def test_context_terms_and_model_config_are_frozen_into_logical_hash():
    body = "<h2>Recovery</h2><p>" + ("Long previous context. " * 30) + "</p><p>Use memory safely.</p>"
    config = {
        "book_title": "Reliable systems",
        "model": "model-a",
        "provider": "provider-a",
        "prompt_version": "prompt-1",
        "protocol_version": "epubox-text-1",
        "generation": {"temperature": 0},
        "terms": [
            {"source": "memory", "target": "内存", "scope": "Recovery", "mode": "required", "note": "noun"},
            {"source": "unused", "target": "未使用", "scope": "Other", "mode": "preferred", "note": ""},
        ],
    }
    first = _plan(body, config)
    unit = next(item for item in first.units if "memory safely" in item.source_projection)

    assert unit.context["book_title"] == "Reliable systems"
    assert unit.context["title"] == "Example book"
    assert unit.context["section"] == "Recovery"
    assert len(unit.context["previous"]) <= 400
    assert unit.context["previous"].endswith("context. ")
    assert unit.terms == (
        {"source": "memory", "target": "内存", "scope": "Recovery", "mode": "required", "note": "noun"},
    )

    second = _plan(body, {**config, "model": "model-b"})
    assert [item.unit_id for item in first.units] == [item.unit_id for item in second.units]
    assert [item.logical_hash for item in first.units] != [item.logical_hash for item in second.units]
    renamed = _plan(body, {**config, "book_title": "Renamed book"})
    assert (
        unit.logical_hash
        != next(item for item in renamed.units if "memory safely" in item.source_projection).logical_hash
    )
    revised_terms = _plan(body, {**config, "terms": [{**config["terms"][0], "target": "记忆体"}]})
    assert (
        unit.logical_hash
        != next(item for item in revised_terms.units if "memory safely" in item.source_projection).logical_hash
    )
    assert _plan("<p>Text.</p>", {"terms": []}).units
    with pytest.raises(ValueError, match="must not be empty"):
        _plan("<p>Text.</p>", {"terms": {"": "空词"}})


def test_explicit_table_footnote_and_cross_document_binding_context_is_recorded():
    document = _plan(
        '<table><tr><th id="name" scope="col">Name</th></tr>'
        '<tr><td headers="name">Widget</td></tr></table>'
        '<p>Read the note<a xmlns:epub="http://www.idpf.org/2007/ops" epub:type="noteref" href="#n1">1</a>.</p>'
        '<aside id="n1"><p>Important footnote detail.</p></aside>'
        '<h2 id="next">Next section</h2><p><a href="other.xhtml#topic">Other chapter</a></p>'
    )
    cell = next(unit for unit in document.units if "Widget" in unit.source_projection)
    note = next(unit for unit in document.units if "Read the note" in unit.source_projection)

    assert cell.context["table_position"] == "row 2, column 1"
    assert cell.context["table_headers"] == "Name"
    assert note.context["footnote"] == "Important footnote detail."
    assert any(
        binding["kind"] == "href_candidate" and binding["href"] == "other.xhtml#topic"
        for binding in document.derived_bindings
    )
    assert any(
        binding["kind"] == "title_candidate" and binding["fragment"] == "next" for binding in document.derived_bindings
    )


def test_hard_boundaries_lock_ancestor_and_user_exceptions_are_explicit():
    document = _plan(
        '<p id="frozen">Do not translate.</p><p><span>A<br/>B</span><em>C</em></p>',
        {"translate_exceptions": {"#frozen": "keep"}},
    )
    assert not any("Do not translate" in unit.source_projection for unit in document.units)
    unit = next(item for item in document.units if "A" in item.source_projection)
    assert unit.source_projection == ("⟦+g1⟧⟦+b1⟧A⟦-b1⟧⟦=x1⟧⟦+b2⟧B⟦-b2⟧⟦-g1⟧⟦+g2⟧C⟦-g2⟧")
    assert unit.registry["g1"].movement == "fixed"
    assert unit.registry["g2"].movement == "same_parent"
    assert unit.registry["b1"].fixed_order == ("b1", "x1", "b2")
    with pytest.raises(ProjectionError, match="protected range"):
        validate_projection(
            unit,
            "⟦+g1⟧⟦+b1⟧B A⟦-b1⟧⟦=x1⟧⟦+b2⟧⟦-b2⟧⟦-g1⟧⟦+g2⟧C⟦-g2⟧",
        )

    with pytest.raises(ValueError, match="exact #id"):
        _plan("<p>Text</p>", {"translate_exceptions": {"p:first-child": "keep"}})


def test_pagebreak_with_number_and_empty_pagebreak_are_hard_atoms():
    document = _plan(
        '<p>A<span xmlns:epub="http://www.idpf.org/2007/ops" epub:type="pagebreak">35</span>B'
        '<span id="Page_36" role="doc-pagebreak" aria-label="Page 36"/>C</p>'
    )
    unit = next(item for item in document.units if item.kind == "paragraph")
    page_atoms = [entry for entry in unit.registry.values() if entry.boundary_type == "page"]

    assert [entry.source_text for entry in page_atoms] == ["35", ""]
    assert unit.source_projection == ("⟦+b1⟧A⟦-b1⟧⟦=x1⟧⟦+b2⟧B⟦-b2⟧⟦=x2⟧⟦+b3⟧C⟦-b3⟧")
    assert any(item.kind == "attribute" and item.source_projection == "Page 36" for item in document.units)


def test_role_token_lists_recognize_pagebreak_and_noteref_without_ids():
    document = _plan(
        '<p>A<span role="  DOC-pagebreak presentation  "/>B'
        '<a role=" presentation DOC-noteref " href="#note">1</a>C</p>'
        '<aside id="note"><p>Footnote text.</p></aside>'
    )
    unit = next(item for item in document.units if item.kind == "paragraph" and "A" in item.source_projection)

    assert any(entry.boundary_type == "page" and not entry.source_text for entry in unit.registry.values())
    assert any(entry.boundary_type == "footnote" and entry.source_text == "1" for entry in unit.registry.values())
    assert not any(entry.kind == "g" and not entry.source_text for entry in unit.registry.values())


def test_empty_id_and_legacy_name_anchors_are_atoms_but_nonempty_links_remain_groups():
    document = _plan(
        '<p>A<span id="spot"/>B<a name="legacy"/>C'
        '<a id="normal" href="#spot">label</a>'
        '<a id="image-link" href="#spot"><img src="icon.png"/></a></p>'
    )
    unit = next(item for item in document.units if item.kind == "paragraph")
    anchors = [entry for entry in unit.registry.values() if entry.boundary_type == "anchor"]
    links = [entry for entry in unit.registry.values() if entry.kind == "g" and entry.hints.get("element") == "a"]

    assert len(anchors) == 2
    assert {entry.source_text for entry in links} == {"label", ""}
    assert any(entry.boundary_type == "media" for entry in unit.registry.values())


def test_pagebreak_text_cannot_cross_boundary_and_identity_passes_independent_validation():
    document = _plan(
        '<p>Before <span xmlns:epub="http://www.idpf.org/2007/ops" '
        'epub:type="pagebreak" id="p35" aria-label="Page 35"/> after</p>'
    )
    unit = next(item for item in document.units if item.kind == "paragraph")
    with pytest.raises(ProjectionError, match="text moved"):
        validate_projection(
            unit,
            "⟦+b1⟧Before after⟦-b1⟧⟦=x1⟧⟦+b2⟧⟦-b2⟧",
        )

    targets = _identity_targets(document)
    assembled = assemble_document(document, targets)
    validate_assembled_document(
        document,
        targets,
        assembled.markup,
        source_to_target=assembled.source_to_target,
    )


def test_ncx_navlabel_records_its_content_target_for_cross_document_binding():
    source = (
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="n1">'
        '<navLabel><text>Chapter one</text></navLabel><content src="chapter1.xhtml#start"/>'
        "</navPoint></navMap></ncx>"
    )
    document = extract_document(source, "OEBPS/toc.ncx", "source-hash", "application/x-dtbncx+xml")

    assert any(
        binding["kind"] == "href_candidate"
        and binding["href"] == "chapter1.xhtml#start"
        and binding["source_unit_id"]
        and binding["source_text"] == "Chapter one"
        for binding in document.derived_bindings
    )


def test_opf_explicit_main_title_wins_over_an_earlier_language_alternative():
    source = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:language>en</dc:language><dc:title id="fr" xml:lang="fr">Titre français</dc:title>'
        '<dc:title id="en" xml:lang="en">English title</dc:title>'
        '<meta refines="#en" property="title-type">main</meta></metadata></package>'
    )
    tree = parse_xml_safely(source)
    primary = select_primary_title(tree)
    assert primary is not None and primary.text == "English title"

    document = extract_document(source, "OEBPS/content.opf", "source-hash", "application/oebps-package+xml")
    titles = [unit.source_projection for unit in document.units if unit.kind == "metadata_title"]
    assert titles == ["English title"]
    assert "ambiguous_primary_title" not in {str(issue["code"]) for issue in document.preparation_issues}


def test_opf_uses_a_unique_primary_language_match_without_guessing_by_order():
    source = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:language>en</dc:language><dc:title xml:lang="fr">Titre</dc:title>'
        '<dc:title xml:lang="en">English title</dc:title></metadata></package>'
    )
    document = extract_document(source, "OEBPS/content.opf", "source-hash", "application/oebps-package+xml")
    assert [unit.source_projection for unit in document.units if unit.kind == "metadata_title"] == ["English title"]


def test_opf_preserves_ambiguous_titles_and_descriptions_with_an_issue():
    source = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>First</dc:title><dc:title>Second</dc:title>"
        "<dc:description>First description</dc:description>"
        "<dc:description>Second description</dc:description></metadata></package>"
    )
    document = extract_document(source, "OEBPS/content.opf", "source-hash", "application/oebps-package+xml")
    assert not [unit for unit in document.units if unit.kind in {"metadata_title", "metadata_description"}]
    assert {str(issue["code"]) for issue in document.preparation_issues} >= {
        "ambiguous_primary_title",
        "ambiguous_text_description",
    }


def test_styles_scan_only_linked_local_roots_and_resolves_percent_encoded_paths_and_imports():
    source = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title>'
        '<link rel="alternate stylesheet" href="../Styles/main%2Ecss"/></head>'
        '<body><p>Use <a href="#term">the term</a>.</p></body></html>'
    )
    safe = extract_document(
        source,
        "OEBPS/Text/chapter.xhtml",
        "source-hash",
        styles={
            "OEBPS/Styles/main.css": '.note { color: blue; } @import "theme.css";',
            "OEBPS/Styles/theme.css": "em { font-style: italic; }",
            "OEBPS/Styles/unrelated.css": "a:first-child { color: red; }",
        },
    )
    safe_link = next(unit for unit in safe.units if "g1" in unit.registry)
    assert safe_link.registry["g1"].reorder_allowed is True

    missing_import = extract_document(
        source,
        "OEBPS/Text/chapter.xhtml",
        "source-hash",
        styles={"OEBPS/Styles/main.css": '@import "missing.css"; a { color: blue; }'},
    )
    assert next(unit for unit in missing_import.units if "g1" in unit.registry).registry["g1"].reorder_allowed is False


@pytest.mark.parametrize(
    "head, body",
    [
        ('<link rel="stylesheet" href="https://example.com/book.css"/>', '<p>Use <a href="#x">text</a>.</p>'),
        ('<link rel="stylesheet" href="missing.css"/>', '<p>Use <a href="#x">text</a>.</p>'),
        ("<style>a:first-child { color: red; }</style>", '<p>Use <a href="#x">text</a>.</p>'),
        ("", '<p>Use <a href="#x" style="display:inline-grid">text</a>.</p>'),
    ],
)
def test_external_missing_or_inline_sensitive_styles_lock_reordering(head: str, body: str):
    source = (
        f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title>{head}</head><body>{body}</body></html>'
    )
    document = extract_document(source, "OEBPS/chapter.xhtml", "source-hash", styles={})
    link = next(unit for unit in document.units if "g1" in unit.registry)
    assert link.registry["g1"].reorder_allowed is False


def test_styles_lock_only_potentially_affected_groups_and_inherited_descendants():
    source = (
        '<html><head><title>Book</title><link rel="stylesheet" href="book.css"/></head><body><article>'
        '<p class="danger"><em>lead</em><a href="#d">danger</a></p>'
        '<p class="safe"><em>lead</em><a href="#s">safe</a></p>'
        '<p class="rtl"><span><a href="#r">rtl</a></span><em>tail</em></p>'
        "</article></body></html>"
    )
    document = extract_document(
        source,
        "OEBPS/chapter.xhtml",
        "source-hash",
        styles={
            "OEBPS/book.css": (
                '@namespace epub "http://www.idpf.org/2007/ops"; '
                ".danger > a:first-child { color: red; } "
                ".rtl { direction: rtl; } article { display: block; }"
            )
        },
    )
    danger = next(unit for unit in document.units if "danger" in unit.source_projection)
    safe = next(unit for unit in document.units if "safe" in unit.source_projection)
    rtl = next(unit for unit in document.units if "rtl" in unit.source_projection)

    assert all(entry.movement == "locked" for entry in danger.registry.values() if entry.kind == "g")
    assert all(entry.movement == "same_parent" for entry in safe.registry.values() if entry.kind == "g")
    assert all(entry.movement == "locked" for entry in rtl.registry.values() if entry.kind == "g")


def test_unknown_selector_still_uses_document_fallback():
    source = (
        '<html><head><title>Book</title><link rel="stylesheet" href="book.css"/></head>'
        '<body><p>Use <a href="#x">text</a>.</p><p>More <em>prose</em>.</p></body></html>'
    )
    document = extract_document(
        source,
        "OEBPS/chapter.xhtml",
        "source-hash",
        styles={"OEBPS/book.css": "svg|a { color: red; }"},
    )
    assert all(
        entry.movement == "locked" for unit in document.units for entry in unit.registry.values() if entry.kind == "g"
    )


def test_attribute_selectors_use_stable_rightmost_candidates_without_false_empty_sets():
    source = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Book</title>'
        '<link rel="stylesheet" href="book.css"/></head><body>'
        '<p data-kind="x"><em>emphasis</em><a href="#x">link</a></p>'
        '<p><span epub:type="keyword">keyword</span><a href="#y">other</a></p>'
        "</body></html>"
    )
    document = extract_document(
        source,
        "OEBPS/chapter.xhtml",
        "source-hash",
        styles={"OEBPS/book.css": "p[data-kind] > em:first-child, span[epub|type]:first-child { color: red; }"},
    )
    emphasis = next(unit for unit in document.units if "emphasis" in unit.source_projection)
    keyword = next(unit for unit in document.units if "keyword" in unit.source_projection)
    assert all(entry.movement == "locked" for entry in emphasis.registry.values() if entry.kind == "g")
    assert all(entry.movement == "locked" for entry in keyword.registry.values() if entry.kind == "g")


def test_order_sensitive_descendants_lock_their_movable_inline_ancestors():
    source = (
        '<html><head><title>Book</title><link rel="stylesheet" href="book.css"/></head><body>'
        "<p><strong>lead</strong><span><em>nested</em></span><i>tail</i></p>"
        '<p><b class="foo">lead</b><span class="bar"><a href="#x">nested link</a></span><em>tail</em></p>'
        "</body></html>"
    )
    document = extract_document(
        source,
        "OEBPS/chapter.xhtml",
        "source-hash",
        styles={"OEBPS/book.css": "strong + span em:first-child, .foo + .bar a { color: red; }"},
    )
    nested = next(
        unit
        for unit in document.units
        if "nested" in unit.source_projection and "nested link" not in unit.source_projection
    )
    nested_link = next(unit for unit in document.units if "nested link" in unit.source_projection)
    assert next(entry for entry in nested.registry.values() if entry.source_text == "nested").movement == "locked"
    assert (
        next(entry for entry in nested_link.registry.values() if entry.source_text == "nested link").movement
        == "locked"
    )
    assert next(entry for entry in nested_link.registry.values() if entry.source_text == "tail").movement == "locked"


def test_order_sensitive_block_candidate_locks_transparent_anchor_group():
    source = (
        '<html><head><title>Book</title><link rel="stylesheet" href="book.css"/></head><body>'
        '<a href="#x"><div>A</div><section>B</section></a></body></html>'
    )
    document = extract_document(
        source,
        "OEBPS/chapter.xhtml",
        "source-hash",
        styles={"OEBPS/book.css": "div:first-child { color: red; }"},
    )
    unit = next(item for item in document.units if "A" in item.source_projection and "B" in item.source_projection)
    assert all(entry.movement == "locked" for entry in unit.registry.values() if entry.kind == "g")


def test_order_sensitive_descendant_inside_block_locks_block_parent_domain():
    source = (
        '<html><head><title>Book</title><link rel="stylesheet" href="book.css"/></head><body>'
        '<a href="#x"><div><em>A</em></div><section>B</section></a></body></html>'
    )
    document = extract_document(
        source,
        "OEBPS/chapter.xhtml",
        "source-hash",
        styles={"OEBPS/book.css": "div:first-child em { color: red; }"},
    )
    unit = next(item for item in document.units if "A" in item.source_projection and "B" in item.source_projection)
    assert next(entry for entry in unit.registry.values() if entry.source_text == "A").movement == "locked"
    assert next(entry for entry in unit.registry.values() if entry.source_text == "B").movement == "locked"


def test_inherited_external_and_inline_direction_lock_descendants_but_not_unrelated_units():
    external = (
        '<html><head><title>Book</title><link rel="stylesheet" href="book.css"/></head><body>'
        '<div dir="rtl"><p><a href="#r">rtl external</a></p></div>'
        '<p><a href="#s">safe external</a></p></body></html>'
    )
    document = extract_document(
        external,
        "OEBPS/chapter.xhtml",
        "source-hash",
        styles={"OEBPS/book.css": "div[dir] { direction: rtl; }"},
    )
    rtl = next(unit for unit in document.units if "rtl external" in unit.source_projection)
    safe = next(unit for unit in document.units if "safe external" in unit.source_projection)
    assert next(entry for entry in rtl.registry.values() if entry.kind == "g").movement == "locked"
    assert next(entry for entry in safe.registry.values() if entry.kind == "g").movement == "same_parent"

    inline = _plan('<p style="direction:rtl"><a href="#r">rtl inline</a></p><p><a href="#s">safe inline</a></p>')
    rtl_inline = next(unit for unit in inline.units if "rtl inline" in unit.source_projection)
    safe_inline = next(unit for unit in inline.units if "safe inline" in unit.source_projection)
    assert next(entry for entry in rtl_inline.registry.values() if entry.kind == "g").movement == "locked"
    assert next(entry for entry in safe_inline.registry.values() if entry.kind == "g").movement == "same_parent"


def test_real_rust_fixture_monospace_command_is_a_bounded_code_atom():
    source = Path("tests/chapter1.xhtml").read_bytes().decode("utf-8")
    document = extract_document(source, "OEBPS/chapter1.xhtml", "source-hash")
    matches = [
        entry
        for unit in document.units
        for entry in unit.registry.values()
        if entry.boundary_type == "code" and entry.source_text == "cargo check"
    ]

    assert matches
    assert all(entry.hints["readonly"] == "cargo check" for entry in matches)


def test_monospace_prose_remains_translatable_while_semantic_code_stays_frozen():
    document = _plan(
        '<p><span class="SANS_TheSansMonoCd_W5Regular_11">'
        "This ordinary sentence remains natural prose</span> and press <kbd>Ctrl</kbd>.</p>"
    )
    unit = next(item for item in document.units if "ordinary sentence" in item.source_projection)

    assert "This ordinary sentence remains natural prose" in unit.source_projection
    assert any(entry.kind == "g" and "ordinary sentence" in entry.source_text for entry in unit.registry.values())
    assert any(
        entry.kind == "x" and entry.boundary_type == "code" and entry.source_text == "Ctrl"
        for entry in unit.registry.values()
    )
