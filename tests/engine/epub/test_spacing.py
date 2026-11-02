from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.epub.cleanup import CLEANUP_POLICY, cleanup_resources
from engine.epub.publish import repack_completed
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker

XHTML = "http://www.w3.org/1999/xhtml"
PATH = "OEBPS/chapter.xhtml"
MEDIA_TYPES = {PATH: "application/xhtml+xml"}


def document(body: str, head: str = "<title>Chapter</title>") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="{XHTML}"><head>{head}</head><body>{body}</body></html>'
    ).encode()


def clean(body: str, head: str = "<title>Chapter</title>") -> tuple[bytes, dict[str, int]]:
    resources, stats = cleanup_resources({PATH: document(body, head)}, media_types=MEDIA_TYPES)
    return resources[PATH], stats


def paragraphs(raw: bytes) -> list[str]:
    root = etree.fromstring(raw)
    return ["".join(node.itertext()) for node in root.findall(f".//{{{XHTML}}}p")]


def test_chinese_words_join_across_empty_index_anchors_without_changing_anchor_identity() -> None:
    raw, stats = clean(
        '<p>搜索 <a id="idx-1" class="calibre9" data-type="indexterm"></a> 引擎，'
        '生<a id="idx-2" data-primary="AI"></a> 成式人工智能。</p>',
        "<title>Chapter</title><style>.calibre9 { background: transparent; width: 0; }</style>",
    )

    assert paragraphs(raw) == ["搜索引擎，生成式人工智能。"]
    assert b'id="idx-1"' in raw and b'id="idx-2"' in raw
    assert b'data-type="indexterm"' in raw and b'data-primary="AI"' in raw
    assert stats["removed_empty_tags"] == 0
    assert stats["removed_spaces"] >= 3


def test_empty_locator_anchor_with_monospace_style_is_transparent_to_chinese_spacing() -> None:
    raw, stats = clean(
        '<p>搜<a id="index-marker" class="mono"></a> 索</p>',
        "<title>Chapter</title><style>.mono { font-family: monospace; }</style>",
    )

    assert '<p>搜<a id="index-marker" class="mono"></a>索</p>'.encode() in raw
    assert stats["removed_empty_tags"] == 0
    assert stats["removed_spaces"] == 1


def test_empty_locator_anchor_inside_code_keeps_original_whitespace_bytes() -> None:
    preserved = '<pre>搜<a id="index-marker" class="mono"></a> 索</pre>'.encode()
    raw, stats = clean(
        preserved.decode(),
        "<title>Chapter</title><style>.mono { font-family: monospace; }</style>",
    )

    assert preserved in raw
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_chinese_words_join_across_formatting_and_ordinary_links_but_urls_stay_exact() -> None:
    raw, _stats = clean(
        '<p><span>搜索</span><em> </em><a href="chapter2.xhtml">引擎 优化</a>方法。</p>'
        '<p><a href="https://example.com/甲%20乙">https://example.com/甲 乙</a></p>'
    )

    assert paragraphs(raw) == ["搜索引擎优化方法。", "https://example.com/甲 乙"]
    assert b'href="chapter2.xhtml"' in raw
    assert b'href="https://example.com/' in raw


def test_chinese_words_join_across_nested_inline_markup() -> None:
    raw, _stats = clean("<p><span>人<span> </span>工</span><strong> 智<em> </em>能</strong></p>")

    assert paragraphs(raw) == ["人工智能"]
    assert b"<strong>" in raw
    assert b"<em>" not in raw


def test_whitespace_only_formatting_node_still_separates_english_words() -> None:
    body = "<p>New<span> </span>York and generative<em> </em>AI</p>"
    raw, stats = clean(body)

    assert paragraphs(raw) == ["New York and generative AI"]
    assert b"<span> </span>" in raw and b"<em> </em>" in raw
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_all_prose_space_encodings_are_removed_between_chinese_characters() -> None:
    raw, stats = clean("<p>普&#32;通&#x20;空&#160;格\u00a0窄\u202f细\u2009全\u3000角</p>")

    assert paragraphs(raw) == ["普通空格窄细全角"]
    assert stats["removed_spaces"] == 7


def test_chinese_punctuation_does_not_keep_adjacent_prose_spaces() -> None:
    raw, _stats = clean("<p>ChatGPT ， 很好；“ 生成式 AI ”（ 2022 年 ）</p>")

    assert paragraphs(raw) == ["ChatGPT，很好；“生成式 AI”（2022 年）"]


def test_chinese_english_and_number_boundaries_keep_authored_spaces() -> None:
    body = "<p>使用 ChatGPT 构建 AI 系统，第 3 章有 10 页，温度 25 °C，重 10 kg。</p>"
    raw, stats = clean(body)

    assert paragraphs(raw) == ["使用 ChatGPT 构建 AI 系统，第 3 章有 10 页，温度 25 °C，重 10 kg。"]
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_chinese_paragraph_edges_are_trimmed_without_crossing_block_boundaries() -> None:
    raw, _stats = clean("<p>  第一段  </p><p> 第二段 </p><table><tr><td>甲 </td><td> 乙</td></tr></table>")

    assert paragraphs(raw) == ["第一段", "第二段"]
    root = etree.fromstring(raw)
    cells = ["".join(node.itertext()) for node in root.findall(f".//{{{XHTML}}}td")]
    assert cells == ["甲", "乙"]
    assert len(cells) == 2


def test_structural_and_explicit_whitespace_boundaries_are_preserved() -> None:
    raw, _stats = clean(
        '<p>搜索<br/> 引擎</p><p xml:space="preserve">搜索 引擎</p>'
        '<pre>搜索 引擎</pre><p class="prewrap">搜索 引擎</p>'
        '<p>公式 <math xmlns="http://www.w3.org/1998/Math/MathML"><mtext>甲 乙</mtext></math> 保留</p>',
        "<title>Chapter</title><style>.prewrap { white-space: pre-wrap; }</style>",
    )

    assert paragraphs(raw) == ["搜索 引擎", "搜索 引擎", "搜索 引擎", "公式 甲 乙 保留"]
    root = etree.fromstring(raw)
    pre = root.find(f".//{{{XHTML}}}pre")
    assert pre is not None and "".join(pre.itertext()) == "搜索 引擎"


def test_visible_empty_anchor_css_remains_a_spacing_boundary() -> None:
    body = (
        '<p>搜索<a id="width" style="display:inline-block;width:1em"></a> 引擎</p>'
        '<p>人工<a id="generated" class="generated"></a> 智能</p>'
    )
    head = '<title>Chapter</title><style>.generated::before { content: "★"; }</style>'

    raw, stats = clean(body, head)

    assert paragraphs(raw) == ["搜索 引擎", "人工 智能"]
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


@pytest.mark.parametrize(
    "style",
    (
        "border:currentcolor none 0",
        "border:currentcolor hidden 2px",
        "border:currentcolor solid 0",
        "border-left-style:solid;border-left-width:0",
        "border-left-width:1px",
        "border-left:solid 1px;border-left-width:0",
        "border-inline-start-style:solid;border-inline-start-width:0",
        "border-inline-start-width:1px",
        "padding-bottom:2px;border-bottom:rgb(0,0,238) dotted 1px",
        "padding-top:2px;border-top:rgb(0,0,238) dotted 1px",
        "background:#fff",
    ),
)
def test_nonadvancing_inline_decoration_does_not_split_chinese_text(style: str) -> None:
    raw, _stats = clean(f'<p>在 <a href="chapter.xhtml" style="{style}">第 5 章</a></p>')

    assert paragraphs(raw) == ["在第 5 章"]


@pytest.mark.parametrize(
    "style",
    (
        "padding-left:2px",
        "padding-right:2px",
        "margin-left:2px",
        "margin-right:2px",
        "border-left:currentcolor dotted 1px",
        "border-right:currentcolor dotted 1px",
        "border-left-style:solid",
        "border-left-width:0;border-left:solid 1px",
        "border-inline-start-style:solid",
        "border-inline-start-style:solid;border-inline-start-width:1px",
        "border-inline-start-style:solid;border-inline-start-width:initial",
        "border-inline-style:solid none;border-inline-width:1px",
        "display:inline-block;width:1em",
    ),
)
def test_horizontal_inline_box_spacing_remains_a_text_boundary(style: str) -> None:
    raw, stats = clean(f'<p>搜索<a id="box" style="{style}"></a> 引擎</p>')

    assert paragraphs(raw) == ["搜索 引擎"]
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_ltr_logical_border_overrides_the_mapped_physical_side() -> None:
    raw, _stats = clean('<p dir="ltr">搜索<a style="border-left:1px solid;border-inline-start:0 solid"></a> 引擎</p>')

    assert paragraphs(raw) == ["搜索引擎"]


@pytest.mark.parametrize("container", ('dir="rtl"', 'style="direction:rtl"'))
def test_inherited_rtl_keeps_physical_and_logical_border_sides_distinct(container: str) -> None:
    raw, stats = clean(
        f'<div {container}><p>搜索<a style="border-left:1px solid;border-inline-start:0 solid"></a> 引擎</p></div>'
    )

    assert paragraphs(raw) == ["搜索 引擎"]
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


@pytest.mark.parametrize("container", ('dir="auto"', 'style="writing-mode:vertical-rl"'))
def test_unresolved_direction_or_vertical_writing_is_preserved(container: str) -> None:
    raw, stats = clean(f"<p {container}>搜索 引擎</p>")

    assert paragraphs(raw) == ["搜索 引擎"]
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_block_box_decoration_does_not_prevent_its_text_edge_cleanup() -> None:
    raw, _stats = clean(
        '<p style="display:block;margin:10px 0 0;padding:0;border:currentcolor none 0;background:#fff"> 搜索 引擎 </p>'
    )

    assert paragraphs(raw) == ["搜索引擎"]


@pytest.mark.parametrize(
    ("selector", "body"),
    (
        ("ol.ol-with-buletless-items > ::before", '<ol class="ol-with-buletless-items"><li></li></ol>'),
        (".container ::before", '<div class="container"><span></span></div>'),
        ("::before", "<div></div>"),
    ),
)
def test_generated_content_with_implicit_universal_subject_does_not_block_cleanup(selector: str, body: str) -> None:
    raw, _stats = clean(
        body + "<p>搜索 引擎</p>",
        f'<title>Chapter</title><style>{selector} {{ content: "★"; }}</style>',
    )

    assert paragraphs(raw) == ["搜索引擎"]


def test_pseudo_element_normalization_does_not_rewrite_attribute_values() -> None:
    raw, stats = clean(
        '<p>搜索<a id="generated" data-marker="::before"></a> 引擎</p>',
        '<title>Chapter</title><style>[data-marker="::before"]::before { content: "★"; }</style>',
    )

    assert paragraphs(raw) == ["搜索 引擎"]
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


@pytest.mark.parametrize(
    ("attributes", "head"),
    (
        ('class="idx" style="width:1em"', "<title>Chapter</title>"),
        ('class="idx" style="display:inline-block;width:1em;width:0"', "<title>Chapter</title>"),
        ('class="idx" style="white-space:pre;white-space:normal"', "<title>Chapter</title>"),
        (
            'class="idx"',
            "<title>Chapter</title><style>.idx { white-space: pre; } a.idx { white-space: normal; }</style>",
        ),
        (
            'class="idx"',
            "<title>Chapter</title><style>a.idx { white-space: pre; } .idx { white-space: normal !important; }</style>",
        ),
    ),
)
def test_only_effective_css_creates_a_spacing_boundary(attributes: str, head: str) -> None:
    raw, _stats = clean(f'<p>搜索<a id="idx" {attributes}></a> 引擎</p>', head)

    assert paragraphs(raw) == ["搜索引擎"]
    assert b'id="idx"' in raw


def test_unrelated_selector_does_not_block_chinese_spacing_cleanup() -> None:
    raw, _stats = clean(
        '<p>搜索<a id="idx" class="calibre9"></a> 引擎</p>',
        "<title>Chapter</title><style>.unrelated .calibre9 { white-space: pre; }</style>",
    )

    assert paragraphs(raw) == ["搜索引擎"]


def test_unsupported_selector_for_an_unused_class_does_not_block_cleanup() -> None:
    raw, _stats = clean(
        '<p>搜索<a id="idx" class="indexterm"></a> 引擎</p>',
        "<title>Chapter</title><style>.unused:state(foo) { white-space: pre; }</style>",
    )

    assert paragraphs(raw) == ["搜索引擎"]


@pytest.mark.parametrize(
    ("href", "label"),
    (
        ("docs/甲%20乙.html", "docs/甲 乙.html"),
        ("/docs/甲%20乙", "/docs/甲 乙"),
        ("//example.com/甲%20乙", "//example.com/甲 乙"),
        ("https://example.com/甲%20乙", "example.com/甲 乙"),
        ("mailto:reader@example.com", "reader@example.com"),
    ),
)
def test_uri_shaped_link_labels_keep_their_display_spacing(href: str, label: str) -> None:
    raw, stats = clean(f'<p><a href="{href}">{label}</a></p>')

    assert paragraphs(raw) == [label]
    assert f'<a href="{href}">{label}</a>'.encode() in raw
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


@pytest.mark.parametrize(
    ("label", "expected"),
    (("Guide: 搜索 引擎", "Guide: 搜索引擎"), ("reader@example.com 的 搜索 引擎", "reader@example.com 的搜索引擎")),
)
def test_prose_link_labels_with_colons_or_email_addresses_still_get_cleaned(label: str, expected: str) -> None:
    raw, _stats = clean(f'<p><a href="chapter.xhtml">{label}</a></p>')

    assert paragraphs(raw) == [expected]


def test_utf16_cleanup_preserves_bom_declaration_and_tag_bytes() -> None:
    declaration = '<?xml version="1.0" encoding="UTF-16"?>\r\n'
    markup = (
        f'<html xmlns="{XHTML}"><head><title>Chapter</title></head><body>'
        '<p class="normal">搜&#160;索<a data-primary="AI" id="idx"></a>&#x20;引擎</p>'
        "</body></html>"
    )
    prefix = b"\xff\xfe" + declaration.encode("utf-16-le")
    raw = prefix + markup.encode("utf-16-le")

    resources, stats = cleanup_resources({PATH: raw}, media_types=MEDIA_TYPES)
    changed = resources[PATH]

    assert changed.startswith(prefix)
    assert paragraphs(changed) == ["搜索引擎"]
    assert '<p class="normal">'.encode("utf-16-le") in changed
    assert '<a data-primary="AI" id="idx"></a>'.encode("utf-16-le") in changed
    assert stats["removed_spaces"] == 2


def test_spacing_cleanup_is_idempotent() -> None:
    first, _stats = clean('<p> 搜索<a id="idx"></a>&#160;引擎 </p>')
    second, stats = cleanup_resources({PATH: first}, media_types=MEDIA_TYPES)

    assert second[PATH] == first
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_formatting_nodes_emptied_by_spacing_cleanup_are_removed_in_the_same_pass() -> None:
    first, first_stats = clean("<p>人<span> </span>工<em> </em>智能</p>")
    second, second_stats = cleanup_resources({PATH: first}, media_types=MEDIA_TYPES)

    assert "<p>人工智能</p>".encode() in first
    assert b"<span>" not in first and b"<em>" not in first
    assert first_stats["removed_empty_tags"] == 2
    assert first_stats["removed_spaces"] == 2
    assert second[PATH] == first
    assert second_stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_repack_applies_chinese_spacing_through_local_css_import(tmp_path: Path) -> None:
    chapter = (
        f'<html xmlns="{XHTML}"><head><title>Spacing</title>'
        '<link rel="stylesheet" href="styles/main.css"/></head><body>'
        '<p>搜索<a id="idx-search" class="calibre9" data-type="indexterm"></a>&#160;引擎，'
        "使用 ChatGPT 构建 AI 系统。</p>"
        '<p><a href="https://example.com/甲%20乙">https://example.com/甲 乙</a></p></body></html>'
    )
    source = make_epub(tmp_path / "spacing-source.epub", {"chapter.xhtml": chapter})
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/styles/main.css", '@import url("nested.css");')
        archive.writestr("OEBPS/styles/nested.css", ".calibre9 { background: transparent; width: 0; }")
    output = tmp_path / "spacing-clean.epub"

    published = repack_completed(source, output, StubChecker())

    verification = published["verification"]
    assert isinstance(verification, dict)
    assert verification["cleanup_policy"] == CLEANUP_POLICY
    cleanup = verification["cleanup_stats"]
    assert isinstance(cleanup, dict) and cleanup["removed_spaces"] >= 1
    with zipfile.ZipFile(output) as archive:
        rendered = archive.read("OEBPS/chapter.xhtml")
    assert (
        '<p>搜索<a id="idx-search" class="calibre9" data-type="indexterm"></a>引擎，使用 ChatGPT 构建 AI 系统。</p>'.encode()
        in rendered
    )
    assert b'id="idx-search"' in rendered
    assert '<a href="https://example.com/甲%20乙">https://example.com/甲 乙</a>'.encode() in rendered


def test_unlinked_stylesheet_does_not_block_spacing_cleanup(tmp_path: Path) -> None:
    source = make_epub(
        tmp_path / "unlinked-style-source.epub",
        {"chapter.xhtml": '<p>搜索<a id="idx" class="calibre9"></a> 引擎</p>'},
    )
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/unused.css", ".calibre9 { white-space: pre; }")
    output = tmp_path / "unlinked-style-clean.epub"

    repack_completed(source, output, StubChecker())

    with zipfile.ZipFile(output) as archive:
        rendered = archive.read("OEBPS/chapter.xhtml")
    assert '<p>搜索<a id="idx" class="calibre9"></a>引擎</p>'.encode() in rendered
