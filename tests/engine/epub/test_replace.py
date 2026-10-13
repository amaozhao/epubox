from __future__ import annotations

import codecs
import zipfile
from pathlib import Path

import pytest

import engine.epub.replace as replace_module
from engine.epub.replace import replace_resources, replacement_fingerprint


def test_default_replacement_changes_readable_text_only() -> None:
    raw = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        '<title>您好</title><meta name="description" content="您好"/>'
        "<style>.您{color:red}</style><script>const name='您';</script></head>"
        '<body title="您好" data-url="/您"><p>您好 &amp; welcome</p><pre>您好</pre>'
        "<code>您好</code>您好</body></html>"
    ).encode()

    result = replace_resources({"OPS/chapter": raw}, media_types={"OPS/chapter": "application/xhtml+xml"})[
        "OPS/chapter"
    ]

    assert "<title>你好</title>".encode() in result
    assert 'name="description" content="你好"'.encode() in result
    assert 'title="你好"'.encode() in result
    assert "<p>你好 &amp; welcome</p>".encode() in result
    assert "<code>您好</code>你好".encode() in result
    assert "<style>.您{color:red}</style>".encode() in result
    assert "<script>const name='您';</script>".encode() in result
    assert "<pre>您好</pre>".encode() in result
    assert 'data-url="/您"'.encode() in result
    assert result.count(b"\r\n") == raw.count(b"\r\n")


def test_custom_literal_replacement_and_empty_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = codecs.BOM_UTF16_LE + (
        '<?xml version="1.0" encoding="UTF-16"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
        "<docTitle><text>a.b 甲</text></docTitle></ncx>"
    ).encode("utf-16-le")
    monkeypatch.setattr(replace_module, "TEXT_REPLACEMENTS", {"a.b": "标题", "甲": "乙"})

    changed = replace_resources({"toc.ncx": raw})["toc.ncx"]

    assert changed.startswith(codecs.BOM_UTF16_LE)
    assert "标题 乙" in changed[len(codecs.BOM_UTF16_LE) :].decode("utf-16-le")
    assert replace_resources({"toc.ncx": raw}, {})["toc.ncx"] == raw
    assert replacement_fingerprint({"a.b": "标题"}) != replacement_fingerprint({"a.b": "别名"})


def test_source_scan_adds_only_changed_readable_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.epub"
    mixed = "<root><p>您 &amp; <![CDATA[您好 & raw]]></p></root>".encode()
    unchanged = "<root><p>没有称谓</p></root>".encode()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("OPS/notes", mixed)
        archive.writestr("OPS/unchanged", unchanged)
        archive.writestr("OPS/image", b"\x00\x01\x02")
    original_read = zipfile.ZipFile.read

    def read(archive, name, *args, **kwargs):
        assert name != "OPS/image"
        return original_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)

    result = replace_resources(
        {},
        media_types={
            "OPS/notes": "application/xml",
            "OPS/unchanged": "application/xml",
            "OPS/image": "image/png",
        },
        source=source,
    )

    assert set(result) == {"OPS/notes"}
    assert result["OPS/notes"] == "<root><p>你 &amp; <![CDATA[你好 & raw]]></p></root>".encode()
