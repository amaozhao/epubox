from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from engine.epub.styles import FONT_POLICY, use_reader_fonts
from engine.epub.validation import ManifestItem, PackageInventory


def _book(tmp_path: Path, files: dict[str, bytes], manifest: tuple[ManifestItem, ...]) -> tuple[Path, PackageInventory]:
    source = tmp_path / "book.epub"
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=ZIP_STORED)
        for path, data in files.items():
            archive.writestr(path, data)
    with ZipFile(source) as archive:
        entries = {item.filename: item.file_size for item in archive.infolist()}
    return source, PackageInventory(
        "a" * 64,
        "3.0",
        "OEBPS/content.opf",
        entries,
        manifest,
        ("chapter",),
        {"chapter": True},
        ("OEBPS/chapter.xhtml",),
        None,
        None,
    )


def test_reader_font_policy_cleans_css_markup_and_embedded_fonts(tmp_path: Path) -> None:
    opf = b'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata>
<meta refines="#font" property="display-seq">1</meta><meta property="keep">yes</meta>
</metadata><manifest>
<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="styles/main.css" media-type="text/css"/>
<item id="font" href="fonts/cjk.woff2" media-type="font/woff2"/>
<item id="fallback" href="image.png" media-type="image/png" fallback="font"/>
<item id="art" href="fonts/logo.svg" media-type="image/svg+xml"/>
</manifest><spine><itemref idref="chapter"/></spine></package>'''
    css = b'''@charset "UTF-8";
/* untouched */
@font-face { font-family: "Bad CJK"; src: url(../fonts/cjk.woff2) format("woff2"); }
h1 { font-family: "Bad CJK", serif; color: red }
code { font-family: monospace }
@media screen { strong { font: italic 700 1.25em/1.4 "Bad CJK", sans-serif; } }
@supports (display:grid) { em {font-family: Named} }
@keyframes pulse { from { font: 12px Named; opacity: 0 } to { opacity: 1 } }
b { font: menu !important }
.important { font: italic 700 16px Named !important }
@page { @top-left { font-family: Named } }
'''
    chapter = b'''<html xmlns="http://www.w3.org/1999/xhtml"><head>
<link rel="stylesheet" href="styles/main.css"/><link rel="preload" as="font" href="fonts/cjk.woff2"/>
<style>.escaped\\[name p &gt; em { font-family: Named; font-weight: 800 } @media print { i { font: italic 14px Named } }</style>
<style><![CDATA[h3 { font-family: Named }]]></style>
</head><body data-keep="A&amp;B"><h1 style="font-family: 'Bad CJK'; background:url('cover.png'); color:blue"><strong>\xe6\xa0\x87\xe9\xa2\x98</strong></h1>
<svg xmlns="http://www.w3.org/2000/svg"><text font-family="Named" style="font: bold 10px Named">T</text></svg>
<font face="Legacy"><em>\xe6\x96\x9c\xe4\xbd\x93</em></font></body></html>'''
    encryption = b'''<?xml version="1.0"?><encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<EncryptedData><EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/><CipherData><CipherReference URI="OEBPS/fonts/cjk.woff2"/></CipherData></EncryptedData>
</encryption>'''
    files = {
        "OEBPS/content.opf": opf,
        "OEBPS/chapter.xhtml": chapter,
        "OEBPS/styles/main.css": css,
        "OEBPS/fonts/cjk.woff2": b"font",
        "OEBPS/fonts/orphan.ttf": b"orphan",
        "OEBPS/fonts/logo.svg": b"<svg/>",
        "OEBPS/image.png": b"png",
        "META-INF/encryption.xml": encryption,
    }
    manifest = (
        ManifestItem("chapter", "OEBPS/chapter.xhtml", "application/xhtml+xml"),
        ManifestItem("css", "OEBPS/styles/main.css", "text/css"),
        ManifestItem("font", "OEBPS/fonts/cjk.woff2", "font/woff2"),
        ManifestItem("fallback", "OEBPS/image.png", "image/png"),
        ManifestItem("art", "OEBPS/fonts/logo.svg", "image/svg+xml"),
    )
    source, inventory = _book(tmp_path, files, manifest)

    replacements, removed = use_reader_fonts(source, inventory, {})

    assert FONT_POLICY == "reader-fonts-v1"
    assert removed == frozenset(
        {"OEBPS/fonts/cjk.woff2", "OEBPS/fonts/orphan.ttf", "META-INF/encryption.xml"}
    )
    assert "@font-face" not in replacements["OEBPS/styles/main.css"].decode()
    rendered_css = replacements["OEBPS/styles/main.css"].decode()
    assert "h1 { font-family:inherit; color: red " in rendered_css
    assert "code { font-family: monospace }" in rendered_css
    assert "font-style:italic" in rendered_css and "font-weight:700" in rendered_css
    assert "font-size:1.25em" in rendered_css and "line-height:1.4" in rendered_css
    assert "font-family:inherit" in rendered_css
    assert "@supports" in rendered_css and "font-family:inherit" in rendered_css
    assert "@keyframes" in rendered_css and "font-size:12px" in rendered_css
    assert "font:inherit!important" in rendered_css.replace(" ", "")
    compact_css = rendered_css.replace(" ", "")
    assert "font-style:italic!important" in compact_css
    assert "font-family:inherit!important" in compact_css
    assert "@top-left { font-family:inherit" in rendered_css
    rendered_chapter = replacements["OEBPS/chapter.xhtml"]
    assert b'rel="preload"' not in rendered_chapter
    assert b"<strong>\xe6\xa0\x87\xe9\xa2\x98</strong>" in rendered_chapter
    assert b"<em>\xe6\x96\x9c\xe4\xbd\x93</em>" in rendered_chapter
    assert b'data-keep="A&amp;B"' in rendered_chapter
    assert b"font-family:inherit" in rendered_chapter
    assert b'font-family="inherit"' in rendered_chapter
    assert b'face=' not in rendered_chapter
    assert b"cover.png" in rendered_chapter
    assert b"&apos;" in rendered_chapter or b"&quot;" in rendered_chapter
    assert b"p &gt; em" in rendered_chapter
    assert b"<![CDATA[h3 { font-family:inherit" in rendered_chapter
    assert b"]]>" in rendered_chapter
    rendered_opf = replacements["OEBPS/content.opf"]
    assert b'id="font"' not in rendered_opf
    assert b'refines="#font"' not in rendered_opf
    assert b'fallback="font"' not in rendered_opf
    assert b'id="art"' in rendered_opf

    repeated, repeated_removed = use_reader_fonts(source, inventory, replacements)
    assert repeated == replacements
    assert repeated_removed == removed


def test_reader_font_policy_uses_pending_replacements_and_preserves_noops(tmp_path: Path) -> None:
    opf = b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>'
    chapter = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p data-x="1">Text</p></body></html>'
    files = {"OEBPS/content.opf": opf, "OEBPS/chapter.xhtml": chapter}
    manifest = (ManifestItem("chapter", "OEBPS/chapter.xhtml", "application/xhtml+xml"),)
    source, inventory = _book(tmp_path, files, manifest)
    pending = chapter.replace(b"Text", b"Translated")

    replacements, removed = use_reader_fonts(source, inventory, {"OEBPS/chapter.xhtml": pending})

    assert replacements == {"OEBPS/chapter.xhtml": pending}
    assert removed == frozenset()
    with ZipFile(source) as archive:
        assert archive.read("OEBPS/chapter.xhtml") == chapter


def test_reader_font_policy_rejects_invalid_css_without_touching_source(tmp_path: Path) -> None:
    opf = b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="css" href="bad.css" media-type="text/css"/></manifest><spine><itemref idref="chapter"/></spine></package>'
    chapter = b'<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>'
    bad_css = b"h1 { color: red; /* unterminated"
    files = {"OEBPS/content.opf": opf, "OEBPS/chapter.xhtml": chapter, "OEBPS/bad.css": bad_css}
    manifest = (
        ManifestItem("chapter", "OEBPS/chapter.xhtml", "application/xhtml+xml"),
        ManifestItem("css", "OEBPS/bad.css", "text/css"),
    )
    source, inventory = _book(tmp_path, files, manifest)

    with pytest.raises(ValueError, match="CSS"):
        use_reader_fonts(source, inventory, {})

    with ZipFile(source) as archive:
        assert archive.read("OEBPS/bad.css") == bad_css


def test_reader_font_policy_rejects_font_used_as_non_typographic_content(tmp_path: Path) -> None:
    opf = b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="font" href="icon.woff2" media-type="font/woff2"/></manifest><spine><itemref idref="chapter"/></spine></package>'
    chapter = b'''<html xmlns="http://www.w3.org/1999/xhtml"><body><span style="font-family:Icons;background:url(icon.woff2)">icon</span></body></html>'''
    files = {
        "OEBPS/content.opf": opf,
        "OEBPS/chapter.xhtml": chapter,
        "OEBPS/icon.woff2": b"icons",
    }
    manifest = (
        ManifestItem("chapter", "OEBPS/chapter.xhtml", "application/xhtml+xml"),
        ManifestItem("font", "OEBPS/icon.woff2", "font/woff2"),
    )
    source, inventory = _book(tmp_path, files, manifest)

    with pytest.raises(ValueError, match="inline CSS"):
        use_reader_fonts(source, inventory, {})


@pytest.mark.parametrize(
    ("chapter", "css", "message"),
    [
        (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><span>\ue900</span></body></html>'.encode(),
            b"",
            "private-use text glyph",
        ),
        (
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body><span class="icon"/></body></html>',
            br'.icon::before { content: "\e900"; font-family: Icons }',
            "private-use generated glyph",
        ),
    ],
)
def test_reader_font_policy_rejects_private_use_icon_glyphs(
    tmp_path: Path,
    chapter: bytes,
    css: bytes,
    message: str,
) -> None:
    css_item = b'<item id="css" href="icons.css" media-type="text/css"/>' if css else b""
    opf = (
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
        b'<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
        + css_item
        + b'<item id="font" href="icon.woff2" media-type="font/woff2"/>'
        b'</manifest><spine><itemref idref="chapter"/></spine></package>'
    )
    files = {
        "OEBPS/content.opf": opf,
        "OEBPS/chapter.xhtml": chapter,
        "OEBPS/icon.woff2": b"icons",
    }
    manifest = [
        ManifestItem("chapter", "OEBPS/chapter.xhtml", "application/xhtml+xml"),
        ManifestItem("font", "OEBPS/icon.woff2", "font/woff2"),
    ]
    if css:
        files["OEBPS/icons.css"] = css
        manifest.append(ManifestItem("css", "OEBPS/icons.css", "text/css"))
    source, inventory = _book(tmp_path, files, tuple(manifest))

    with pytest.raises(ValueError, match=message):
        use_reader_fonts(source, inventory, {})

    with ZipFile(source) as archive:
        assert archive.read("OEBPS/chapter.xhtml") == chapter
