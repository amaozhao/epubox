from __future__ import annotations

import codecs
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.core.markup import parse_xml_bytes, qname_local_name
from engine.epub.upgrade import _relative, _resolve, upgrade_package
from engine.epub.validation import ManifestItem, PackageInventory


def _book(
    path: Path,
    version: str,
    *,
    nav: bool = False,
    nav_property: bool = True,
    ncx: bool = True,
    cover_id: str = "cover",
) -> PackageInventory:
    chapter = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
        b'<body><h1>Heading</h1><figure><svg xmlns="http://www.w3.org/2000/svg"/></figure></body></html>'
    )
    epub3 = version.startswith("3")
    nav_properties = ' properties="nav"' if nav_property else ""
    nav_item = f'<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"{nav_properties}/>' if nav else ""
    ncx_item = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>' if ncx else ""
    namespace = (
        "http://openebook.org/namespaces/oeb-package/1.0/" if version == "1.0" else "http://www.idpf.org/2007/opf"
    )
    metadata_start = '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    metadata_end = "</metadata>"
    if version == "1.0":
        metadata_start += "<dc-metadata>"
        metadata_end = "</dc-metadata>" + metadata_end
    scheme = "" if epub3 else ' opf:scheme="uuid" xmlns:opf="http://www.idpf.org/2007/opf"'
    creator = "" if epub3 else ' opf:role="aut" opf:file-as="Author, A" xmlns:opf="http://www.idpf.org/2007/opf"'
    modified = '<meta property="dcterms:modified">2026-10-08T00:00:00Z</meta>' if epub3 else ""
    opf = (
        f'<package xmlns="{namespace}" version="{version}" unique-identifier="uid">'
        f"{metadata_start}"
        f'<dc:identifier id="uid"{scheme}>urn:test:book</dc:identifier><dc:title>Book</dc:title><dc:language>en</dc:language>'
        f"<dc:creator{creator}>A Author</dc:creator>"
        f"{modified}{'' if epub3 else f'<meta name="cover" content="{cover_id}"/>'}{metadata_end}<manifest>"
        f'<item id="c1" href="chapter.xhtml" media-type="{"application/xhtml+xml" if epub3 else "text/html"}"{' properties="svg"' if epub3 else ""}/>'
        f'<item id="cover" href="cover.jpg" media-type="image/jpeg"{' properties="cover-image"' if epub3 else ""}/>'
        f'{ncx_item}{nav_item}</manifest><spine toc="ncx"><itemref idref="c1"/></spine>'
        '<guide><reference type="toc" title="Contents" href="chapter.xhtml"/></guide></package>'
    ).encode()
    toc = (
        b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="one">'
        b'<navLabel><text>Old label</text></navLabel><content src="chapter.xhtml#one"/>'
        b'<navPoint id="two"><navLabel><text>Child</text></navLabel><content src="chapter.xhtml#two"/>'
        b'</navPoint></navPoint></navMap><pageList><pageTarget id="p1"><navLabel><text>1</text></navLabel>'
        b'<content src="chapter.xhtml#p1"/></pageTarget></pageList></ncx>'
    )
    existing_nav = (
        b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        b'<head><title>Contents</title></head><body><nav epub:type="toc"><ol><li>'
        b'<a href="chapter.xhtml">Existing</a></li></ol></nav></body></html>'
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/chapter.xhtml", chapter)
        archive.writestr("OEBPS/cover.jpg", b"jpeg")
        if ncx:
            archive.writestr("OEBPS/toc.ncx", toc)
        if nav:
            archive.writestr("OEBPS/nav.xhtml", existing_nav)
    manifest = [
        ManifestItem("c1", "OEBPS/chapter.xhtml", "text/html"),
        ManifestItem("cover", "OEBPS/cover.jpg", "image/jpeg"),
    ]
    if ncx:
        manifest.append(ManifestItem("ncx", "OEBPS/toc.ncx", "application/x-dtbncx+xml"))
    if nav:
        manifest.append(ManifestItem("nav", "OEBPS/nav.xhtml", "application/xhtml+xml", ("nav",)))
    with ZipFile(path) as archive:
        entries = {item.filename: item.file_size for item in archive.infolist()}
    return PackageInventory(
        "a" * 64,
        version,
        "OEBPS/content.opf",
        entries,
        tuple(manifest),
        ("c1",),
        {"c1": True},
        ("OEBPS/chapter.xhtml", "OEBPS/nav.xhtml") if nav else ("OEBPS/chapter.xhtml",),
        "OEBPS/nav.xhtml" if nav and nav_property else None,
        "OEBPS/toc.ncx" if ncx else None,
    )


def _elements(data: bytes, name: str) -> list[etree._Element]:
    return [element for element in parse_xml_bytes(data).getroot().iter() if qname_local_name(element.tag) == name]


@pytest.mark.parametrize("version", ["1.0", "2.0", "2.0.1", "3.3"])
def test_upgrade_converts_legacy_packages_to_epub3(tmp_path: Path, version: str) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, version)
    translated_ncx = (
        b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap><navPoint id="one">'
        b'<navLabel><text>Translated</text></navLabel><content src="chapter.xhtml#one"/></navPoint>'
        b"</navMap></ncx>"
    )

    upgraded = upgrade_package(source, inventory, {"OEBPS/toc.ncx": translated_ncx})

    package = parse_xml_bytes(upgraded[inventory.opf_path]).getroot()
    assert package.get("version") == "3.0"
    modified = [
        element.text
        for element in _elements(upgraded[inventory.opf_path], "meta")
        if element.get("property") == "dcterms:modified"
    ]
    assert len(modified) == 1 and modified[0] is not None
    assert upgraded == upgrade_package(source, inventory, {"OEBPS/toc.ncx": translated_ncx})
    creator = _elements(upgraded[inventory.opf_path], "creator")[0]
    assert not any(qname_local_name(name) in {"role", "file-as"} for name in creator.attrib)
    refinements = {
        (element.get("property"), element.text) for element in _elements(upgraded[inventory.opf_path], "meta")
    }
    if not version.startswith("3"):
        assert ("role", "aut") in refinements and ("file-as", "Author, A") in refinements
        assert ("identifier-type", "uuid") in refinements
    items = {element.get("id"): element for element in _elements(upgraded[inventory.opf_path], "item")}
    assert items["c1"].get("media-type") == "application/xhtml+xml"
    assert "svg" in items["c1"].get("properties", "").split()
    assert "cover-image" in items["cover"].get("properties", "").split()
    nav = next(item for item in items.values() if "nav" in item.get("properties", "").split())
    nav_path = "OEBPS/" + str(nav.get("href"))
    assert nav_path in upgraded
    assert "Translated" in upgraded[nav_path].decode()
    assert upgraded["OEBPS/toc.ncx"] == translated_ncx


def test_upgrade_keeps_valid_epub3_package_bytes_and_filters_unchanged_replacements(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "3.0", nav=True)
    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path)
        chapter = archive.read("OEBPS/chapter.xhtml")
    translated = chapter.replace(b"Heading", b"Translated")

    upgraded = upgrade_package(
        source,
        inventory,
        {inventory.opf_path: opf, "OEBPS/chapter.xhtml": translated},
    )

    assert inventory.opf_path not in upgraded
    assert upgraded == {"OEBPS/chapter.xhtml": translated}


def test_upgrade_replaces_calendar_invalid_modified_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "3.0", nav=True)
    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path).replace(b"2026-10-08T00:00:00Z", b"2026-99-99T00:00:00Z")

    upgraded = upgrade_package(source, inventory, {inventory.opf_path: opf})

    modified = [
        item.text
        for item in _elements(upgraded[inventory.opf_path], "meta")
        if item.get("property") == "dcterms:modified"
    ]
    assert len(modified) == 1 and modified[0] != "2026-99-99T00:00:00Z"


def test_upgrade_reuses_existing_epub3_navigation(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "3.3", nav=True)
    with ZipFile(source) as archive:
        existing = archive.read("OEBPS/nav.xhtml")

    upgraded = upgrade_package(source, inventory, {})

    assert "OEBPS/nav.xhtml" not in upgraded or upgraded["OEBPS/nav.xhtml"] == existing
    package = upgraded[inventory.opf_path]
    assert parse_xml_bytes(package).getroot().get("version") == "3.0"
    nav_items = [item for item in _elements(package, "item") if "nav" in item.get("properties", "").split()]
    assert len(nav_items) == 1


def test_upgrade_detects_existing_toc_without_manifest_property(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0", nav=True, nav_property=False)

    upgraded = upgrade_package(source, inventory, {})

    package = upgraded[inventory.opf_path]
    items = [item for item in _elements(package, "item") if "nav" in item.get("properties", "").split()]
    assert len(items) == 1 and items[0].get("href") == "nav.xhtml"
    assert not any(path.endswith("nav-1.xhtml") for path in upgraded)


def test_upgrade_builds_navigation_from_translated_spine_without_ncx(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0", ncx=False)
    translated = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Title</title></head>'
        b'<body><h1 id="top">Translated heading</h1></body></html>'
    )

    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path).replace(b">en<", b">zh-Hans<")
    upgraded = upgrade_package(
        source,
        inventory,
        {inventory.opf_path: opf, "OEBPS/chapter.xhtml": translated},
    )

    package = upgraded[inventory.opf_path]
    nav_item = next(item for item in _elements(package, "item") if "nav" in item.get("properties", "").split())
    nav = upgraded["OEBPS/" + str(nav_item.get("href"))].decode()
    assert "Translated heading" in nav and 'href="chapter.xhtml#top"' in nav
    assert 'lang="zh-Hans"' in nav and ">目录<" in nav and ">定位<" in nav


def test_upgrade_falls_back_to_spine_when_ncx_has_no_toc_entries(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    translated = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Title</title></head>'
        b"<body><h1>Translated fallback</h1></body></html>"
    )

    upgraded = upgrade_package(
        source,
        inventory,
        {
            "OEBPS/chapter.xhtml": translated,
            "OEBPS/toc.ncx": b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap/></ncx>',
        },
    )

    nav_path = next(path for path in upgraded if path.endswith("nav.xhtml"))
    assert "Translated fallback" in upgraded[nav_path].decode()


def test_upgrade_maps_only_known_legacy_guide_landmarks(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0", ncx=False)
    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path).replace(
            b'<guide><reference type="toc" title="Contents" href="chapter.xhtml"/></guide>',
            b'<guide><reference type="text" title="Start" href="chapter.xhtml#start"/>'
            b'<reference type="title-page" title="Title" href="chapter.xhtml"/>'
            b'<reference type="vendor-custom" title="Custom" href="chapter.xhtml"/></guide>',
        )

    upgraded = upgrade_package(source, inventory, {inventory.opf_path: opf})

    nav_path = next(path for path in upgraded if path.endswith("nav.xhtml"))
    nav = parse_xml_bytes(upgraded[nav_path]).getroot()
    types = [
        item.get("{http://www.idpf.org/2007/ops}type") for item in nav.iter() if qname_local_name(item.tag) == "a"
    ]
    assert "bodymatter" in types and "titlepage" in types
    assert "vendor-custom" not in types
    assert b"chapter.xhtml#start" in upgraded[nav_path]


def test_upgrade_does_not_mark_html_cover_as_cover_image(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0", cover_id="c1")

    upgraded = upgrade_package(source, inventory, {})

    items = {item.get("id"): item for item in _elements(upgraded[inventory.opf_path], "item")}
    assert "cover-image" not in items["c1"].get("properties", "").split()


def test_upgrade_replaces_obsolete_epub2_manifest_requirements_with_properties(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path).replace(
            b'<item id="c1" href="chapter.xhtml" media-type="text/html"/>',
            b'<item id="c1" href="chapter.xhtml" media-type="text/html" '
            b'required-namespace="http://www.w3.org/2000/svg" required-modules="svg" '
            b'fallback-style="css" fallback="cover"/>',
        )

    upgraded = upgrade_package(source, inventory, {inventory.opf_path: opf})

    item = next(value for value in _elements(upgraded[inventory.opf_path], "item") if value.get("id") == "c1")
    assert not ({"required-namespace", "required-modules", "fallback-style"} & set(item.attrib))
    assert item.get("fallback") == "cover"
    assert "svg" in item.get("properties", "").split()


@pytest.mark.parametrize(
    "body",
    [
        '<form><input type="text"/></form>',
        '<p onclick="this.hidden=true">Interactive</p>',
    ],
)
def test_upgrade_declares_scripted_forms_and_event_handlers(tmp_path: Path, body: str) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    document = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Title</title></head><body>' + body + "</body></html>"
    ).encode()

    upgraded = upgrade_package(source, inventory, {"OEBPS/chapter.xhtml": document})

    item = next(value for value in _elements(upgraded[inventory.opf_path], "item") if value.get("id") == "c1")
    assert "scripted" in item.get("properties", "").split()


def test_upgrade_declares_legacy_epub_switch_property(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    document = (
        b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        b"<head><title>Title</title></head><body><epub:switch><epub:default>"
        b"<p>Fallback</p></epub:default></epub:switch></body></html>"
    )

    upgraded = upgrade_package(source, inventory, {"OEBPS/chapter.xhtml": document})

    item = next(value for value in _elements(upgraded[inventory.opf_path], "item") if value.get("id") == "c1")
    assert "switch" in item.get("properties", "").split()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_upgrade_removes_unused_legacy_epub_prefix_without_changing_text(tmp_path: Path, version: str) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, version)
    document = (
        b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        b'epub:prefix="z3998: http://www.daisy.org/z3998/2012/vocab/structure/#">'
        b'<head><title>Title</title><style>pre::before{content:"  x  "}</style>'
        b'<script>const x = "  keep  ";</script></head><body><pre>  &lt;x&gt;  </pre></body></html>'
    )

    upgraded = upgrade_package(source, inventory, {"OEBPS/chapter.xhtml": document})

    result = upgraded["OEBPS/chapter.xhtml"]
    root = parse_xml_bytes(result).getroot()
    assert not any(qname_local_name(name) == "prefix" for name in root.attrib)
    assert "<x>" in "".join(root.itertext())
    for protected in (
        b'<style>pre::before{content:"  x  "}</style>',
        b'<script>const x = "  keep  ";</script>',
        b"<pre>  &lt;x&gt;  </pre>",
    ):
        assert protected in result


def test_upgrade_merges_aliased_epub_prefix_into_existing_prefix_attribute(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    document = (
        b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ops="http://www.idpf.org/2007/ops" '
        b'prefix="foo: urn:foo" ops:prefix="z3998: urn:z3998 custom: urn:custom" '
        b'ops:type="z3998:poem custom:part foo:book"><head><title>Title</title></head>'
        b"<body><p>Text</p></body></html>"
    )

    upgraded = upgrade_package(source, inventory, {"OEBPS/chapter.xhtml": document})

    result = upgraded["OEBPS/chapter.xhtml"]
    root = parse_xml_bytes(result).getroot()
    declarations = root.get("prefix", "")
    assert "foo: urn:foo" in declarations
    assert "z3998: urn:z3998" in declarations
    assert "custom: urn:custom" in declarations
    assert b"ops:prefix" not in result and len(re.findall(rb"\sprefix=", result)) == 1


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_upgrade_replaces_legacy_xhtml_doctype_without_serializing_body(tmp_path: Path, encoding: str) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    declaration = "utf-16" if encoding.startswith("utf-16") else encoding
    text = (
        f'<?xml version="1.0" encoding="{declaration}"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        '<style>pre::before { content: "  keep  "; }</style>'
        '<script>const value = "  keep  ";</script></head>'
        "<body><pre>  &lt;code&gt;  </pre></body></html>"
    )
    document = (codecs.BOM_UTF16_LE if encoding == "utf-16-le" else b"") + text.encode(encoding)

    upgraded = upgrade_package(source, inventory, {"OEBPS/chapter.xhtml": document})

    result = upgraded["OEBPS/chapter.xhtml"]
    assert "<!DOCTYPE html>" in result.decode(encoding)
    assert "XHTML 1.1" not in result.decode(encoding)
    for protected in (
        '<style>pre::before { content: "  keep  "; }</style>',
        '<script>const value = "  keep  ";</script>',
        "<pre>  &lt;code&gt;  </pre>",
    ):
        assert protected.encode(encoding) in result


def test_upgrade_rewrites_dtd_named_entities_without_touching_escaped_or_special_regions(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    document = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
        b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title title="A &copy; B">Title</title></head>'
        b"<body><p>A&nbsp;B &copy; C &amp;nbsp;</p><pre>&copy; code</pre>"
        b"<![CDATA[literal &copy;]]><!-- literal &nbsp; --><?keep &copy;?></body></html>"
    )
    before = tuple(parse_xml_bytes(document).getroot().itertext())

    upgraded = upgrade_package(source, inventory, {"OEBPS/chapter.xhtml": document})

    result = upgraded["OEBPS/chapter.xhtml"]
    root = parse_xml_bytes(result).getroot()
    assert tuple(root.itertext()) == before
    normal = result.split(b"<![CDATA[", 1)[0]
    assert b"&nbsp;" not in normal and b"&copy;" not in normal
    assert b"&amp;nbsp;" in result
    assert b"<![CDATA[literal &copy;]]>" in result
    assert b"<!-- literal &nbsp; -->" in result
    assert b"<?keep &copy;?>" in result


def test_upgrade_decodes_archive_paths_and_preserves_encoded_fragments() -> None:
    assert _resolve("OEBPS/content.opf", "Text/chapter%20one.xhtml#part%201") == "OEBPS/Text/chapter one.xhtml"
    assert _relative("OEBPS/nav.xhtml", "OEBPS/Text/chapter one.xhtml#part 1") == ("Text/chapter%20one.xhtml#part%201")
