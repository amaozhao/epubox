from __future__ import annotations

import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import UnsafeMarkupError, parse_xml_safely
from engine.epub.parsing import parse_resource

XHTML = "http://www.w3.org/1999/xhtml"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
OPF = "http://www.idpf.org/2007/opf"


def test_namespace_overrides_html_media_type_and_preserves_xml_nodes() -> None:
    data = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        f'<html xmlns="{XHTML}"><head><!--note--><?keep value?></head>'
        "<body><p><![CDATA[a < b]]></p></body></html>"
    ).encode()

    parsed = parse_resource(data, "text/html; charset=utf-8")

    assert parsed.raw is data
    assert parsed.kind == "xhtml"
    assert parsed.encoding == "utf-8"
    assert parsed.tree is not None
    rendered = etree.tostring(parsed.tree, encoding="unicode")
    assert "<!--note-->" in rendered
    assert "<?keep value?>" in rendered
    assert "<![CDATA[a < b]]>" in rendered
    assert "\r\n" in parsed.text


def test_prefixed_xhtml_namespace_does_not_depend_on_one_prefix() -> None:
    data = f'<x:html xmlns:x="{XHTML}"><x:body/></x:html>'.encode()

    parsed = parse_resource(data, "text/html")

    assert parsed.kind == "xhtml"
    assert parsed.tree is not None


def test_standard_xhtml_entities_are_resolved_from_the_offline_dtd() -> None:
    data = (
        b'<!DOCTYPE html SYSTEM "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
        + f'<html xmlns="{XHTML}"><body><p>&nbsp;</p></body></html>'.encode()
    )

    parsed = parse_resource(data, "application/xhtml+xml")

    assert parsed.tree is not None
    paragraph = parsed.tree.find(f".//{{{XHTML}}}p")
    assert paragraph is not None and paragraph.text == "\N{NO-BREAK SPACE}"


@pytest.mark.parametrize(
    ("namespace", "root", "media_type", "kind"),
    [
        (NCX, "ncx", "application/x-dtbncx+xml", "ncx"),
        (OPF, "package", "application/oebps-package+xml", "opf"),
        ("urn:example", "document", "application/xml", "xml"),
    ],
)
def test_xml_resource_classification_uses_exact_namespace(
    namespace: str, root: str, media_type: str, kind: str
) -> None:
    parsed = parse_resource(f'<{root} xmlns="{namespace}"/>'.encode(), media_type)

    assert parsed.kind == kind
    assert parsed.tree is not None


@pytest.mark.parametrize(
    ("encoding", "prefix"),
    [("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")],
)
def test_bom_and_declared_encoding_are_validated(encoding: str, prefix: bytes) -> None:
    declared = "UTF-8" if encoding == "utf-8" else encoding.upper()
    markup = f'<?xml version="1.0" encoding="{declared}"?><root>café</root>'
    codec = "utf-8" if encoding == "utf-8" else encoding
    data = prefix + markup.encode(codec)

    parsed = parse_resource(data, "application/xml")

    assert parsed.encoding == encoding
    assert parsed.text.endswith("<root>café</root>")
    assert parsed.tree is not None and parsed.tree.getroot().text == "café"


def test_utf16_without_bom_uses_xml_byte_order_signature() -> None:
    data = '<?xml version="1.0" encoding="UTF-16LE"?><root>中文</root>'.encode("utf-16-le")

    parsed = parse_resource(data, "application/xml")

    assert parsed.encoding == "utf-16-le"
    assert parsed.tree is not None and parsed.tree.getroot().text == "中文"


def test_genuine_html_is_diagnostic_only_even_when_a_parser_could_repair_it() -> None:
    data = b"<!doctype html><html><body><p>one<p>two</body></html>"

    parsed = parse_resource(data, "text/html")

    assert parsed.kind == "html"
    assert parsed.tree is None
    assert parsed.diagnostics == ("genuine HTML requires verified source mapping before translation",)


def test_well_formed_html_fragment_is_not_trusted_as_generic_xml() -> None:
    parsed = parse_resource(b"<div><p>Translate me</p></div>", "text/html")

    assert parsed.kind == "html"
    assert parsed.tree is None
    assert "genuine HTML" in parsed.diagnostics[0]


def test_fake_xhtml_namespace_in_html_content_does_not_change_the_root_kind() -> None:
    data = (
        f'<!-- <html xmlns="{XHTML}"> --><html><head><script>'
        f"const fake = '<html xmlns=\"{XHTML}\">';</script></head><body><p>one<p>two</body></html>"
    ).encode()

    parsed = parse_resource(data, "text/html")

    assert parsed.kind == "html"
    assert parsed.tree is None


def test_malformed_xhtml_root_is_not_downgraded_to_repairable_html() -> None:
    data = f'<html xmlns="{XHTML}"><body><p>broken</body></html>'.encode()

    with pytest.raises(UnsafeMarkupError):
        parse_resource(data, "text/html")


def test_xml_media_with_wrong_root_namespace_is_diagnostic_only() -> None:
    parsed = parse_resource(b'<html xmlns="urn:not-xhtml"><body/></html>', "application/xhtml+xml")

    assert parsed.kind == "xml"
    assert parsed.tree is not None
    assert parsed.diagnostics == ("media type application/xhtml+xml does not match the root namespace",)


@pytest.mark.parametrize(
    "data",
    [
        b'<?xml version="1.0" encoding="UTF-16"?><root/>',
        b"<root><broken></root>",
        b'<!DOCTYPE root SYSTEM "file:///etc/passwd"><root>&secret;</root>',
        b'<!DOCTYPE root [<!ENTITY secret SYSTEM "https://example.com/a">]><root>&secret;</root>',
        b'<!DOCTYPE root [<!ENTITY safe "expanded">]><root>&safe;</root>',
    ],
)
def test_invalid_encoding_broken_xml_and_external_entities_fail_clearly(data: bytes) -> None:
    with pytest.raises(UnsafeMarkupError):
        parse_resource(data, "application/xml")


def test_string_parser_uses_the_same_doctype_policy_without_reinterpreting_encoding() -> None:
    safe = '<?xml version="1.0" encoding="UTF-16"?><root>decoded text</root>'
    assert parse_xml_safely(safe).getroot().text == "decoded text"

    with pytest.raises(UnsafeMarkupError, match="internal DTD"):
        parse_xml_safely('<!DOCTYPE root [<!ENTITY custom "expanded">]><root>&custom;</root>')
