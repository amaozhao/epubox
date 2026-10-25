from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from engine.epub.cleanup import CLEANUP_POLICY, cleanup_resources
from tests.engine.epub.test_factory import make_epub

XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
PATH = "OEBPS/chapter.xhtml"
MEDIA_TYPES = {PATH: "application/xhtml+xml"}


def document(body: str, head: str = "<title>Chapter</title>") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<html xmlns="{XHTML}" xmlns:epub="{EPUB}"><head>{head}</head><body>{body}</body></html>'
    ).encode()


def clean(raw: bytes, *, source: Path | None = None, media_types=None):
    return cleanup_resources(
        {PATH: raw},
        media_types=MEDIA_TYPES if media_types is None else media_types,
        source=source,
    )


def test_removes_only_truly_empty_unattributed_formatting_tags_without_losing_tails() -> None:
    raw = document("<p>前<span></span> 后<em/>文<strong></strong>。<em>重要</em><span> </span>尾</p>")

    resources, stats = clean(raw)
    changed = resources[PATH]

    assert b"<span></span>" not in changed
    assert b"<em/>" not in changed
    assert b"<strong></strong>" not in changed
    assert "<p>前后文。<em>重要</em><span> </span>尾</p>".encode() in changed
    assert stats["removed_empty_tags"] == 3
    assert stats["changed_documents"] == 1
    assert stats["removed_spaces"] >= 1


def test_preserves_empty_index_anchors_and_any_semantic_attributes_byte_for_byte() -> None:
    body = (
        '<nav epub:type="index"><ol><li><a id="index-a" name="index-a" href="#entry"></a>'
        '<span id="entry"></span><em class="locator"></em><strong style="display:block"></strong>'
        '<span data-page="12"></span><em aria-label="marker"></em></li></ol></nav>'
    )
    raw = document(body)

    resources, stats = clean(raw)

    assert resources[PATH] == raw
    assert stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_removes_chinese_ascii_spacing_across_inline_markup_and_empty_anchors_only() -> None:
    raw = document(
        '<p>   中 <em>文</em> 字 <a id="note"></a> ，结束。</p>'
        "<p>Barry O'Reilly writes well.</p>"
        "<p>Use <em>very important</em> words.</p>"
    )

    resources, stats = clean(raw)
    changed = resources[PATH]

    assert '<p>中<em>文</em>字<a id="note"></a>，结束。</p>'.encode() in changed
    assert b"<p>Barry O'Reilly writes well.</p>" in changed
    assert b"<p>Use <em>very important</em> words.</p>" in changed
    assert stats["removed_empty_tags"] == 0
    assert stats["removed_spaces"] >= 4
    assert stats["changed_documents"] == 1


def test_protected_content_nbsp_urls_xml_space_and_css_white_space_remain_exact(tmp_path: Path) -> None:
    code = b"<pre>  if value:\n    return value</pre><code> a  b </code><kbd> x y </kbd><samp> x y </samp>"
    math = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mtext>甲 乙</mtext></math>'.encode()
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>甲 乙</text></svg>'.encode()
    preserved = (
        '<p xml:space="preserve">甲 乙</p><p class="prewrap">甲 乙</p>'
        '<p><a href="https://example.com/">https://example.com/甲 乙</a></p>'
        "<p>甲&#160;乙</p>"
    ).encode()
    raw = document((code + math + svg + preserved).decode())
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Source.</p>"})
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/styles/book.css", ".prewrap { white-space: pre-wrap; }")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    resources, stats = cleanup_resources(
        {PATH: raw},
        media_types=MEDIA_TYPES | {"OEBPS/styles/book.css": "text/css"},
        source=source,
    )
    changed = resources[PATH]

    assert code in changed
    assert math in changed
    assert svg in changed
    assert preserved in changed
    assert changed == raw
    assert stats["changed_documents"] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_css_generated_content_background_and_layout_protect_bare_empty_tags(tmp_path: Path) -> None:
    raw = document("<span></span><em></em><strong></strong>")
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Source.</p>"})
    stylesheet = (
        'span:empty::before { content: "★"; }\n'
        "em { background: url(image.png) no-repeat; }\n"
        "strong { display: inline-block; width: 1em; }"
    )
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/styles/book.css", stylesheet)

    resources, stats = cleanup_resources(
        {PATH: raw},
        media_types=MEDIA_TYPES | {"OEBPS/styles/book.css": "text/css"},
        source=source,
    )

    assert resources[PATH] == raw
    assert stats["removed_empty_tags"] == 0
    assert stats["changed_documents"] == 0


def test_byte_patches_preserve_declaration_entities_namespaces_and_line_endings() -> None:
    prefix = (
        b'<?xml version="1.0" encoding="UTF-8"?>\r\n'
        b'<!DOCTYPE html SYSTEM "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\r\n'
    )
    raw = (
        prefix
        + (
            f'<html xmlns="{XHTML}" xmlns:epub="{EPUB}"><head><title> 标题 </title></head>'
            '<body><p epub:type="index">中文 &amp; 符号&nbsp;保留</p><span></span></body></html>'
        ).encode()
    )

    resources, stats = clean(raw)
    changed = resources[PATH]

    assert changed.startswith(prefix)
    assert f'xmlns="{XHTML}" xmlns:epub="{EPUB}"'.encode() in changed
    assert b"<title> \xe6\xa0\x87\xe9\xa2\x98 </title>" in changed
    assert b"&amp;" in changed and b"&nbsp;" in changed
    assert b"<span></span>" not in changed
    assert changed.count(b"\r\n") == raw.count(b"\r\n")
    assert stats["removed_empty_tags"] == 1
    assert stats["changed_documents"] == 1


def test_cleanup_is_idempotent_and_ignores_non_xhtml_resources() -> None:
    raw = document("<p>  中文 空格。</p><span></span>")
    image = b"\x00<span></span>\xff"
    first, first_stats = cleanup_resources(
        {PATH: raw, "OEBPS/image.bin": image},
        media_types=MEDIA_TYPES | {"OEBPS/image.bin": "application/octet-stream"},
    )
    second, second_stats = cleanup_resources(
        first,
        media_types=MEDIA_TYPES | {"OEBPS/image.bin": "application/octet-stream"},
    )

    assert CLEANUP_POLICY
    assert first["OEBPS/image.bin"] == image
    assert first_stats["changed_documents"] == 1
    assert second == first
    assert second_stats == {"removed_empty_tags": 0, "removed_spaces": 0, "changed_documents": 0}


def test_ordinary_body_background_does_not_disable_text_cleanup() -> None:
    raw = document("<p>  中文 空 格。</p>", head="<style>body { background: white; }</style>")
    resources, _stats = clean(raw)
    assert "<p>中文空格。</p>".encode() in resources[PATH]


def test_nested_empty_wrappers_are_removed_in_one_idempotent_pass() -> None:
    raw = document('<p>前<span><em/></span> 后<span id="index"><strong/></span>文。</p>')
    resources, stats = clean(raw)
    assert '<p>前后<span id="index"></span>文。</p>'.encode() in resources[PATH]
    assert stats["removed_empty_tags"] == 3
    repeated, again = cleanup_resources(resources, media_types=MEDIA_TYPES)
    assert repeated == resources and again["changed_documents"] == 0
