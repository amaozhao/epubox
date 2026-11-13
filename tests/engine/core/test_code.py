from __future__ import annotations

import hashlib

import pytest
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.core.code import CODE_CLASSIFICATION_VERSION, CodeContext, monospace_family
from engine.core.styles import CodeStyleError
from engine.item.atoms import extract_resource

XHTML = "http://www.w3.org/1999/xhtml"


def _root(body: str, head: str = "") -> etree._Element:
    return etree.fromstring(f'<html xmlns="{XHTML}"><head>{head}</head><body>{body}</body></html>'.encode())


def _by_id(root: etree._Element, value: str) -> etree._Element:
    return next(node for node in root.iter() if node.get("id") == value)


def rendered_listing(ident: str, *lines: str) -> str:
    return (
        f'<div id="{ident}" class="class41">'
        + "".join(
            f'<div class="class{42 if index == 0 else 44}"><div class="calibre1">'
            f'<span class="class43"><span class="bookspan">{line}</span></span> </div></div>'
            for index, line in enumerate(lines)
        )
        + "</div>"
    )


def test_flattened_rust_listings_are_protected_before_extraction():
    root = _root(
        rendered_listing(
            "loop", "fn main() {", "&#160;&#160;for (index, value) in samples.iter().enumerate() {", "}", "}"
        )
        + rendered_listing("struct", "pub struct GpuMetrics {", "&#160;&#160;pub index: &#160;u32,", "}")
        + rendered_listing("config", "[dependencies]", 'thiserror = "2"')
        + rendered_listing("terminal", "cargo build --release", "Finished release profile")
        + rendered_listing("prose", "Index of documents", "use this feature; then restart.")
    )
    path = "OPS/chapter.xhtml"
    assert CodeContext(root, path).regions == {
        _by_id(root, name): "block" for name in ("loop", "struct", "config", "terminal")
    }
    data = etree.tostring(root)
    fresh = extract_resource(data, path, "source")
    assert len(fresh.items) == 2
    assert "Index of documents" in fresh.items[0].source_projection
    assert "use this feature; then restart." in fresh.items[1].source_projection
    frozen = extract_resource(data, path, "source", extractor_version="epubox-atomic-7")
    assert any("samples.iter().enumerate()" in item.source_projection for item in frozen.items)
    assert any("pub index:" in item.source_projection for item in frozen.items)


@pytest.mark.parametrize(
    ("block_class", "line_class", "text_class"),
    (("class-0-1", "class-0-2", "bookSpan"), ("class48", "class39", "bookspan")),
)
def test_generated_python_listing_is_protected_but_same_markup_for_prose_stays_translatable(
    block_class: str, line_class: str, text_class: str
) -> None:
    def listing(ident: str, first: str, second: str) -> str:
        return (
            f'<div id="{ident}" class="{block_class}">'
            f'<span class="{line_class}"><span class="{text_class}">{first}</span></span><br/>'
            f'<span class="{line_class}"><span class="{text_class}">{second}</span></span></div>'
        )

    root = _root(
        listing("python", "def render(row):", "&#160;&#160;&#160;&#160;return row[0]")
        + listing("fields", "name: str = Field(min_length=1)", "&#160;&#160;&#160;&#160;count: int")
        + listing("prose", "Note: important", "Example: basic")
    )
    path = "OPS/chapter.xhtml"
    assert CodeContext(root, path).regions == {
        _by_id(root, "python"): "block",
        _by_id(root, "fields"): "block",
    }

    data = etree.tostring(root)
    frozen = extract_resource(data, path, "source", extractor_version="epubox-atomic-6")
    fresh = extract_resource(data, path, "source")
    assert len(frozen.items) == 3
    assert len(fresh.items) == 1
    assert "Note: important" in fresh.items[0].source_projection


def test_semantic_code_elements_form_maximal_nonoverlapping_regions() -> None:
    root = _root(
        '<p id="p">Before <code id="code">x <var id="var">n</var></code> after.</p>'
        '<pre id="pre"><samp id="samp">log</samp></pre><kbd id="kbd">K</kbd><tt id="tt">T</tt>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert CODE_CLASSIFICATION_VERSION == "code-context-4"
    assert context.regions == {
        _by_id(root, "code"): "inline",
        _by_id(root, "pre"): "block",
        _by_id(root, "kbd"): "inline",
        _by_id(root, "tt"): "inline",
    }
    assert context.is_code(_by_id(root, "var"))
    assert context.is_code(_by_id(root, "samp"))
    assert not context.is_code(_by_id(root, "p"))


def test_program_code_protects_lines_without_swallowing_caption() -> None:
    root = _root(
        '<div class="ProgramCode" id="listing"><div class="Caption" id="caption">Example listing</div>'
        '<div class="LineGroup" id="group"><div class="FixedLine" id="line">let x = 1;</div></div></div>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "group"): "block"}
    assert context.is_code(_by_id(root, "line"))
    assert not context.is_code(_by_id(root, "caption"))
    assert not context.is_code(_by_id(root, "listing"))


@pytest.mark.parametrize("class_name", ("codeblock", "programlisting", "sourceCode"))
def test_explicit_block_code_classes_are_semantic_even_without_css(class_name: str) -> None:
    root = _root(f'<div id="block" class="chapter {class_name}">cargo test</div>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "block"): "block"}


@pytest.mark.parametrize("class_name", ("language-rust", "language-python", "hljs", "prettify"))
def test_language_and_highlighter_classes_are_explicit_code(class_name: str) -> None:
    root = _root(f'<p id="block" class="{class_name}">let value = 1;</p>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "block"): "block"}


def test_highlight_class_alone_does_not_swallow_ordinary_emphasis() -> None:
    root = _root(
        '<p id="ordinary" class="highlight">Important ordinary prose.</p>'
        '<div id="container" class="highlight"><code id="code">value</code></div>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "inline"}
    assert not context.is_code(_by_id(root, "ordinary"))


@pytest.mark.parametrize("tag", ("p", "div"))
def test_generic_code_class_on_block_element_is_code_case_insensitively(tag: str) -> None:
    root = _root(f'<{tag} id="block" class="Code">let value = 1;</{tag}>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "block"): "block"}


@pytest.mark.parametrize("tag", ("ol", "table", "section", "article", "blockquote"))
def test_strong_code_class_supports_explicit_block_containers(tag: str) -> None:
    content = "<li>let value = 1;</li>" if tag == "ol" else "<div>let value = 1;</div>"
    root = _root(f'<{tag} id="block" class="programlisting">{content}</{tag}>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "block"): "block"}


@pytest.mark.parametrize("tag", ("li", "thead", "tbody", "tfoot", "tr", "td", "th", "dt", "dd"))
def test_strong_code_class_supports_atomic_block_elements(tag: str) -> None:
    root = _root(f'<{tag} id="block" class="code">OutPoint</{tag}>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "block"): "block"}


def test_code_table_cell_does_not_swallow_adjacent_prose_cell() -> None:
    root = _root(
        '<table><tr><td id="code" class="code">OutPoint</td><td id="prose">Natural description.</td></tr></table>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}
    assert not context.is_code(_by_id(root, "prose"))


@pytest.mark.parametrize(
    ("attributes", "tag"),
    (
        ('class="language-rust"', "li"),
        ('style="white-space:pre-wrap;font-family:monospace"', "td"),
    ),
)
def test_language_and_styled_code_use_shared_block_boundaries(attributes: str, tag: str) -> None:
    root = _root(f'<{tag} id="code" {attributes}>let value = 1;</{tag}>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}


def test_strong_code_figure_partitions_caption_from_element_wrapped_code() -> None:
    root = _root(
        '<figure id="figure" class="sourceCode"><figcaption id="caption">Example in Rust</figcaption>'
        '<div id="code">let value = 1;</div></figure>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}
    assert not context.is_code(_by_id(root, "caption"))
    assert not context.is_code(_by_id(root, "figure"))


def test_strong_code_container_with_caption_and_raw_text_fails_closed() -> None:
    root = _root('<figure class="sourceCode">let value = 1;<figcaption>Example</figcaption></figure>')

    with pytest.raises(ValueError, match="cannot partition code container"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_program_code_caption_is_an_inheritance_barrier_but_explicit_code_inside_survives() -> None:
    root = _root(
        '<div class="ProgramCode" style="white-space:pre-wrap;font-family:monospace">'
        '<div class="Caption" id="caption"><p id="prose">Natural description.</p>'
        '<code id="literal">OutPoint</code><span id="direct" style="font-family:monospace">flag</span></div>'
        '<div class="LineGroup" id="lines"><div class="FixedLine">let x=1;</div></div></div>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {
        _by_id(root, "literal"): "inline",
        _by_id(root, "direct"): "inline",
        _by_id(root, "lines"): "block",
    }
    assert not context.is_code(_by_id(root, "caption"))
    assert not context.is_code(_by_id(root, "prose"))


def test_explicit_inline_code_classes_and_fonts_work_on_common_markup_tags() -> None:
    root = _root(
        '<p id="p">Use <em id="em" class="codeformatted code">literal</em>, '
        '<a id="a" class="mono" href="#x">OutPoint</a>, '
        '<strong id="strong" style="font-family:Consolas">Result</strong>, '
        '<i id="i" class="literal">flag</i>, <b id="b" class="monospace">byte</b>, '
        '<mark id="mark" class="code">token</mark>.</p>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {
        _by_id(root, "em"): "inline",
        _by_id(root, "a"): "inline",
        _by_id(root, "strong"): "inline",
        _by_id(root, "i"): "inline",
        _by_id(root, "b"): "inline",
        _by_id(root, "mark"): "inline",
    }
    assert not context.is_code(_by_id(root, "p"))


@pytest.mark.parametrize("class_name", ("code-block", "code_block", "CodeBlock", "source-code", "source_code"))
def test_exact_block_code_class_aliases_are_supported(class_name: str) -> None:
    root = _root(f'<div id="code" class="{class_name}">let x = 1;</div>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}


@pytest.mark.parametrize("class_name", ("inline-code", "inline_code", "code-inline", "CodeInline"))
@pytest.mark.parametrize("tag", ("span", "em"))
def test_exact_inline_code_class_aliases_are_supported(class_name: str, tag: str) -> None:
    root = _root(f'<p>Use <{tag} id="code" class="{class_name}">OutPoint</{tag}>.</p>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "inline"}


@pytest.mark.parametrize("class_name", ("no-code", "decode", "code-caption"))
def test_code_like_substrings_are_not_treated_as_explicit_code_classes(class_name: str) -> None:
    root = _root(f'<p id="prose" class="{class_name}">Natural description.</p>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {}


def test_document_container_highlighter_class_does_not_mark_the_whole_book_as_code() -> None:
    root = etree.fromstring(
        f'<html xmlns="{XHTML}" class="hljs language-rust"><head><title>Book</title></head>'
        '<body class="hljs"><p id="prose">Ordinary prose.</p><code id="code" title="opaque">x</code></body></html>'.encode()
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "inline"}
    assert _by_id(root, "code").get("title") == "opaque"
    assert not context.is_code(_by_id(root, "prose"))


@pytest.mark.parametrize("class_name", ("code", "mono", "monospace", "literal"))
def test_inline_code_classes_protect_the_whole_nested_span(class_name: str) -> None:
    root = _root(f'<p id="p">Use <span id="inline" class="{class_name}"><b id="nested">OutPoint</b>()</span> now.</p>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "inline"): "inline"}
    assert context.is_code(_by_id(root, "nested"))
    assert not context.is_code(_by_id(root, "p"))


def test_inline_monospace_style_is_code_but_quoted_generic_is_not() -> None:
    root = _root(
        '<p><span id="real" style="font-family: Menlo, Consolas, monospace">OutPoint</span>'
        '<span id="quoted" style="font-family: \'monospace\'">ordinary words</span></p>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "real"): "inline"}
    assert not context.is_code(_by_id(root, "quoted"))


@pytest.mark.parametrize(
    "family",
    (
        "Menlo",
        "Monaco",
        "Consolas",
        "Courier New",
        "'Liberation Mono'",
        '"DejaVu Sans Mono"',
        "Source Code Pro",
        "JetBrains Mono",
        "Fira Code",
        "ui-monospace",
    ),
)
def test_known_system_monospace_family_is_recognized_without_generic_fallback(family: str) -> None:
    assert monospace_family(family)


def test_quoted_generic_monospace_is_not_a_generic_family() -> None:
    assert not monospace_family("'monospace'")


def test_generic_family_may_appear_at_any_position_in_the_family_list() -> None:
    assert monospace_family("monospace, monospace")
    assert monospace_family("monospace, serif")
    assert not monospace_family("serif, monospace")


def test_generic_family_before_a_named_face_keeps_the_named_face_selection() -> None:
    root = _root(
        '<p><span id="literal" style="font-family: monospace, monospace">std::cout</span>'
        '<span id="prose" style="font-family: serif, monospace">ordinary words</span></p>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "literal"): "inline"}
    assert not context.is_code(_by_id(root, "prose"))


def test_generic_family_mixed_with_other_names_still_fails_closed() -> None:
    root = _root('<p><span id="bad" style="font-family: monospace monospace">std::cout</span></p>')

    with pytest.raises(CodeStyleError):
        CodeContext(root, "OPS/chapter.xhtml")


def test_calibre_numbered_code_classes_are_recognized_as_code() -> None:
    root = _root(
        '<div class="programcode1" id="PC1"><div class="unorderedlist">'
        '<div class="fixedline1" id="line">@Html.ActionLink("Home", "Index")</div>'
        "</div></div>"
        '<p class="chapter1" id="prose">Ordinary words.</p>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "line"): "block"}
    assert not context.is_code(_by_id(root, "prose"))


def test_context_two_replays_before_numbered_calibre_classes_and_generic_reordering() -> None:
    root = _root('<div class="programcode1"><div id="line" class="fixedline1">let value = 1;</div></div>')

    legacy = CodeContext(root, "OPS/chapter.xhtml", policy="code-context-2")
    current = CodeContext(root, "OPS/chapter.xhtml", policy="code-context-3")

    assert legacy.regions == {}
    assert current.regions == {_by_id(root, "line"): "block"}

    generic = _root('<span id="generic" style="font-family:monospace,serif">literal</span>')
    with pytest.raises(CodeStyleError):
        CodeContext(generic, "OPS/chapter.xhtml", policy="code-context-2")
    assert CodeContext(generic, "OPS/chapter.xhtml", policy="code-context-3").regions == {
        _by_id(generic, "generic"): "inline"
    }


def test_context_two_does_not_recognize_new_generated_python_class_shape() -> None:
    root = _root(
        '<div id="python" class="class48">'
        '<span class="class39"><span class="bookspan">def render(row):</span></span><br/>'
        '<span class="class39"><span class="bookspan">&#160;&#160;return row[0]</span></span></div>'
    )

    assert CodeContext(root, "OPS/chapter.xhtml", policy="code-context-2").regions == {}
    assert CodeContext(root, "OPS/chapter.xhtml", policy="code-context-3").regions == {_by_id(root, "python"): "block"}


def test_block_requires_preformatted_whitespace_and_explicit_monospace() -> None:
    root = _root(
        '<p id="code" style="white-space:break-spaces;font:12px Menlo,Consolas,monospace">'
        "INFO  retry=2\n// keep indentation</p>"
        '<p id="mono" style="font-family:monospace">ordinary monospace prose</p>'
        '<p id="prewrap" style="white-space:pre-wrap">ordinary wrapped prose</p>'
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}
    assert not context.is_code(_by_id(root, "mono"))
    assert not context.is_code(_by_id(root, "prewrap"))


def test_linked_and_imported_css_participate_in_cascade_and_hash_only_used_styles() -> None:
    head = '<link rel="stylesheet" href="../styles/base.css"/>'
    root = _root(
        '<div class="listing" id="outer"><p id="code">let value = 1;</p></div><p class="unused">prose</p>',
        head,
    )
    styles = {
        "OPS/../styles/ignored.css": ".unused { white-space:pre; font-family:monospace }",
        "styles/base.css": '@import "nested/code.css"; .listing { white-space: normal }',
        "styles/nested/code.css": (
            ".listing { white-space: pre-wrap !important; font-family: Menlo, Consolas, monospace }"
        ),
    }
    context = CodeContext(root, "OPS/chapter.xhtml", styles)

    assert context.regions == {_by_id(root, "outer"): "block"}
    assert context.stylesheet_hashes == {
        path: hashlib.sha256(styles[path].encode()).hexdigest()
        for path in ("styles/base.css", "styles/nested/code.css")
    }


@pytest.mark.parametrize(
    "pseudo_rule",
    (
        "p.dropcap::first-letter, p.dropcap::before { white-space:pre-wrap; font-family:monospace }",
        (
            "@media (min-width: 1px) {"
            "p.dropcap::first-letter, p.dropcap::before { white-space:pre-wrap; font-family:monospace }"
            "}"
        ),
    ),
)
def test_pseudo_element_font_and_whitespace_do_not_classify_originating_prose_as_code(
    pseudo_rule: str,
) -> None:
    root = _root(
        '<p id="prose" class="dropcap">Natural prose.</p><span id="code" class="literal">let value = 1;</span>',
        f"<style>{pseudo_rule}.literal {{ white-space:pre-wrap; font-family:monospace }}</style>",
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert not context.is_code(_by_id(root, "prose"))
    assert context.regions == {_by_id(root, "code"): "inline"}


def test_declared_namespace_prefix_matches_xhtml_code_selector() -> None:
    root = _root(
        '<p id="code" class="listing">let value = 1;</p>',
        '<style>@namespace h "http://www.w3.org/1999/xhtml";'
        "h|p.listing { white-space:pre-wrap; font-family:monospace }</style>",
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}


def test_default_namespace_applies_to_unprefixed_selector() -> None:
    root = _root(
        '<p id="code" class="listing">let value = 1;</p>',
        '<style>@namespace "http://www.w3.org/1999/xhtml";'
        "p.listing { white-space:pre-wrap; font-family:monospace }</style>",
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}


def test_imported_stylesheet_uses_its_own_namespace_scope() -> None:
    root = _root(
        '<p id="code" class="listing">let value = 1;</p>',
        '<link rel="stylesheet" href="base.css"/>',
    )
    styles = {
        "OPS/base.css": '@namespace s "http://www.w3.org/2000/svg"; @import "nested.css";',
        "OPS/nested.css": (
            '@namespace h "http://www.w3.org/1999/xhtml"; h|p.listing { white-space:pre-wrap; font-family:monospace }'
        ),
    }
    context = CodeContext(root, "OPS/chapter.xhtml", styles)

    assert context.regions == {_by_id(root, "code"): "block"}
    assert set(context.stylesheet_hashes) == set(styles)


@pytest.mark.parametrize("imported", (False, True))
def test_linked_and_imported_stylesheet_accepts_decoded_leading_bom(imported: bool) -> None:
    root = _root(
        '<p id="code" class="listing">let value = 1;</p>',
        '<link rel="stylesheet" href="base.css"/>',
    )
    rule = "\ufeff.listing { white-space:pre-wrap; font-family:monospace }"
    styles = {"OPS/base.css": '@import "nested.css";', "OPS/nested.css": rule} if imported else {"OPS/base.css": rule}
    context = CodeContext(root, "OPS/chapter.xhtml", styles)

    assert context.regions == {_by_id(root, "code"): "block"}


def test_linked_stylesheet_with_decoded_nul_fails_closed() -> None:
    root = _root(
        '<p class="listing">possibly code</p>',
        '<link rel="stylesheet" href="style.css"/>',
    )

    with pytest.raises(ValueError, match="NUL"):
        CodeContext(
            root,
            "OPS/chapter.xhtml",
            {"OPS/style.css": ".listing\x00 { white-space:pre-wrap; font-family:monospace }"},
        )


def test_undeclared_namespace_prefix_fails_closed() -> None:
    root = _root(
        '<p class="listing">possibly code</p>',
        "<style>h|p.listing { white-space:pre-wrap; font-family:monospace }</style>",
    )

    with pytest.raises(ValueError, match="undeclared namespace prefix"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_embedded_css_specificity_order_important_and_inheritance_are_respected() -> None:
    root = _root(
        '<div id="outer" class="listing"><p id="child">line one\n line two</p></div>',
        "<style>"
        ".listing { white-space: normal; font-family: serif }"
        "div.listing { white-space: pre-wrap !important; font-family: Menlo, monospace }"
        "#outer { white-space: normal }"
        "</style>",
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "outer"): "block"}
    assert context.is_code(_by_id(root, "child"))
    assert context.stylesheet_hashes == {}


def test_specificity_ignores_attribute_string_syntax_and_counts_escaped_class_once() -> None:
    root = _root(
        '<p id="code" class="foo:bar" data-label=".fake">let value = 1;</p>',
        '<style>[data-label=".fake"] { white-space:normal; font-family:serif }'
        ".foo\\:bar { white-space:pre-wrap; font-family:monospace }</style>",
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {_by_id(root, "code"): "block"}


def test_repeated_linked_stylesheet_participates_each_time_in_source_order() -> None:
    root = _root(
        '<p id="code" class="listing">line one\n line two</p>',
        '<link rel="stylesheet" href="style.css"/>'
        "<style>.listing { white-space:normal; font-family:serif }</style>"
        '<link rel="stylesheet" href="style.css"/>',
    )
    styles = {"OPS/style.css": ".listing { white-space:pre-wrap; font-family:monospace }"}
    context = CodeContext(root, "OPS/chapter.xhtml", styles)

    assert context.regions == {_by_id(root, "code"): "block"}


def test_unlinked_styles_are_ignored() -> None:
    root = _root('<p id="text" class="listing">ordinary text</p>')
    context = CodeContext(
        root,
        "OPS/chapter.xhtml",
        {"styles/unlinked.css": ".listing { white-space:pre-wrap; font-family:monospace }"},
    )

    assert context.regions == {}
    assert context.stylesheet_hashes == {}


def test_missing_local_stylesheet_fails_closed() -> None:
    root = _root(
        '<p class="listing">possibly code</p>',
        '<link rel="stylesheet" href="../styles/missing.css"/>',
    )

    with pytest.raises(ValueError, match="missing stylesheet.*styles/missing.css"):
        CodeContext(root, "OPS/chapter.xhtml", {})


@pytest.mark.parametrize(
    "style",
    (
        "white-space:pre-wrap;font-family:var(--code-font)",
        "white-space:var(--code-space);font-family:monospace",
    ),
)
def test_dynamic_code_related_style_fails_closed(style: str) -> None:
    root = _root(f'<p class="listing" style="{style}">possibly code</p>')

    with pytest.raises(ValueError, match="dynamic code style"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_dynamic_ordinary_style_does_not_block_the_book() -> None:
    root = _root('<p id="text" style="font-family:var(--body-font)">ordinary prose</p>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {}


@pytest.mark.parametrize(
    "style",
    (
        "font-family:var(--code-font)",
        "font:12px var(--code-font)",
        "font-family:monospace serif",
    ),
)
def test_direct_dynamic_or_invalid_inline_font_fails_closed(style: str) -> None:
    root = _root(f'<p>Use <span style="{style}">OutPoint</span> here.</p>')

    with pytest.raises(ValueError, match="dynamic code style"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_inherited_dynamic_body_font_does_not_block_ordinary_inline_text() -> None:
    root = _root('<div style="font-family:var(--body-font)"><p>Use <span id="text">ordinary words</span>.</p></div>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {}


def test_all_reset_prevents_stale_code_properties_from_protecting_prose() -> None:
    root = _root('<p id="text" style="white-space:pre-wrap;font-family:monospace;all:initial">ordinary prose</p>')
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {}


@pytest.mark.parametrize(
    "href",
    ("https://example.com/code.css", "//example.com/code.css"),
)
def test_remote_stylesheet_fails_closed(href: str) -> None:
    root = _root('<p class="language-rust">let x = 1;</p>', f'<link rel="stylesheet" href="{href}"/>')

    with pytest.raises(ValueError, match="remote stylesheet"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_remote_import_fails_closed() -> None:
    root = _root('<p class="listing">possibly code</p>', '<link rel="stylesheet" href="style.css"/>')

    with pytest.raises(ValueError, match="remote stylesheet"):
        CodeContext(root, "OPS/chapter.xhtml", {"OPS/style.css": '@import "https://example.com/code.css";'})


@pytest.mark.parametrize("selector", (":is(.listing, .code)", ":where(.listing)", ":not(.prose)", ":has(code)"))
def test_complex_functional_selector_for_code_properties_fails_closed(selector: str) -> None:
    root = _root('<p class="listing">possibly code</p>', f"<style>{selector} {{ font-family:monospace }}</style>")

    with pytest.raises(ValueError, match="selector"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_nth_child_selector_with_of_clause_fails_closed() -> None:
    root = _root(
        '<div><p class="listing">one</p><p class="listing">two</p></div>',
        "<style>p:nth-child(2 of .listing) { font-family:monospace }</style>",
    )

    with pytest.raises(ValueError, match="selector"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_conditional_rule_that_can_change_code_classification_fails_closed() -> None:
    root = _root(
        '<p id="listing" class="listing">code</p>',
        "<style>.listing { white-space:pre-wrap; font-family:monospace }"
        "@media print { .listing { white-space:normal; font-family:serif } }</style>",
    )

    with pytest.raises(ValueError, match="conditional code style"):
        CodeContext(root, "OPS/chapter.xhtml")


def test_conditional_ordinary_typography_does_not_block_the_book() -> None:
    root = _root(
        '<p id="text">ordinary prose</p>',
        "<style>@media print { p { font-family:serif } }</style>",
    )
    context = CodeContext(root, "OPS/chapter.xhtml")

    assert context.regions == {}


def test_conditional_inherited_style_that_changes_descendant_code_fails_closed() -> None:
    root = _root(
        '<p id="listing">inherited code layout</p>',
        "<style>body { white-space:pre-wrap; font-family:monospace }"
        "@media print { body { white-space:normal; font-family:serif } }</style>",
    )

    with pytest.raises(ValueError, match="conditional code style"):
        CodeContext(root, "OPS/chapter.xhtml")


SHELL = (
    '<p id="shell" class="class_s1BD"><span class="num">1 </span>'
    '<span class="comment">#!/bin/bash</span><br/>'
    '<span class="num">&#160;2 </span><span class="keyword">set</span> -euo pipefail<br/>'
    '<span class="num">&#160;<span id="page_593"/>3 </span>'
    '<span class="variable">HANDOFF_FILE</span>=&quot;${1:?missing handoff file path}&quot;<br/>'
    '<span class="num">4 </span>git worktree add ../attempt -b attempt<br/></p>'
)


@pytest.mark.parametrize("page_anchor", ("", '<span id="page_594"/>'))
def test_numbered_highlighted_shell_is_preserved_without_swallowing_prose(page_anchor):
    shell = SHELL.replace('<span class="num">4 ', page_anchor + '<span class="num">4 ')
    root = etree.fromstring(
        ('<html xmlns="http://www.w3.org/1999/xhtml"><body>' + shell + "<p>Translate this.</p></body></html>").encode()
    )
    block = next(node for node in root.iter() if node.get("id") == "shell")
    assert CodeContext(root, "OPS/chapter.xhtml").regions == {block: "block"}


def test_numbered_prose_or_broken_line_sequence_cannot_claim_shell_preservation():
    for body in (SHELL.replace("#!/bin/bash", "A shell script starts here."), SHELL.replace("4 </span>", "8 </span>")):
        root = etree.fromstring(
            ('<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + "</body></html>").encode()
        )
        assert not CodeContext(root, "OPS/chapter.xhtml").regions


def test_atomic7_frozen_shell_inventory_keeps_its_original_members():
    raw = ('<html xmlns="http://www.w3.org/1999/xhtml"><body>' + SHELL + "</body></html>").encode()
    frozen = extract_resource(raw, "OPS/chapter.xhtml", "source", extractor_version="epubox-atomic-7")
    assert any("#!/bin/bash" in item.source_projection for item in frozen.items)
    assert not extract_resource(raw, "OPS/chapter.xhtml", "source").items
