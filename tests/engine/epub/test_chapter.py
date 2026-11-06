from __future__ import annotations

import zipfile

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.agents.wire import _decode_slots
from engine.epub.publish import publish_atomic
from engine.services.journal import BodyJournal
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.epub.test_publish import XHTML, _case


def test_export_normalizes_chapter_numbers_without_changing_heading_structure(tmp_path, monkeypatch):
    sources = (
        "⟦+g1⟧Chapter ⟦+g2⟧3⟦-g2⟧⟦-g1⟧ Human and Machine Creativity",
        "⟦+g1⟧Chapter 2. ⟦-g1⟧A Tour of Rust",
        "⟦+b1⟧⟦+g1⟧Chapter 4:⟦-g1⟧⟦-b1⟧⟦=x1⟧⟦+b2⟧Can Machines Think?⟦-b2⟧",
        "Chapter 5: —Machine Creativity: How Far?",
        "Chapter 2.0: Versioning",
        "⟦+g1⟧Chapter 6⟦-g1⟧⟦+g2⟧.⟦-g2⟧⟦=x1⟧",
        "Rust?",
    )
    case = _case(
        tmp_path,
        '<h1 id="c3"><span class="cn" epub:type="label">Chapter <span class="cn1" epub:type="ordinal">'
        "3</span></span> Human and Machine Creativity</h1>"
        '<h1 class="calibre11"><span class="firstname">Chapter 2. </span>A Tour of Rust</h1>'
        '<h1 id="question"><span>Chapter 4:</span><br/>Can Machines Think?</h1>'
        '<h1 id="plain">Chapter 5: —Machine Creativity: How Far?</h1>'
        '<h2 id="version">Chapter 2.0: Versioning</h2>'
        '<h2 id="styled"><span>Chapter 6</span><span>.</span> <em>Rust?</em></h2>',
        sources,
    )
    slots: dict[str, dict[str, str]] = dict(
        zip(
            sources,
            (
                {"1": "第", "2": "3", "3": "章人类与机器的创造力"},
                {"1": "第 2 章。", "2": "Rust 之旅"},
                {"1": "第 4 章：", "2": "机器会思考吗？"},
                {"1": "第 9 章——机器的创造力：能走多远？"},
                {"1": "第 2.0 章：版本管理"},
                {"1": "第 6 章", "2": "。"},
                {"1": "Rust？"},
            ),
            strict=True,
        )
    )
    slots["Chapter 1"] = {"1": "第 1 章。"}
    targets = {
        item.item_id: _decode_slots(item.source_projection, slots[item.source_projection], preserve_whitespace=True)
        if item.source_projection in slots
        else item.source_projection
        for document in case.index.inventories
        for item in document.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "chapter-headings.epub"
    publish_atomic(case.session.store, output, StubChecker())

    with zipfile.ZipFile(output) as archive:
        root = etree.fromstring(archive.read("OEBPS/chapter.xhtml"))
        nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
        ncx = etree.fromstring(archive.read("OEBPS/toc.ncx"))
    chapter3, chapter2, question, plain = root.findall(f".//{{{XHTML}}}h1")
    (label,) = chapter3
    (ordinal,) = label
    assert chapter3.get("id") == "c3"
    assert label.get("class") == "cn" and label.get("{http://www.idpf.org/2007/ops}type") == "label"
    assert ordinal.get("class") == "cn1" and ordinal.get("{http://www.idpf.org/2007/ops}type") == "ordinal"
    assert (label.text, ordinal.text, ordinal.tail, label.tail) == ("第", "3", "章", " 人类与机器的创造力")
    (rust_label,) = chapter2
    assert chapter2.get("class") == "calibre11" and rust_label.get("class") == "firstname"
    assert (rust_label.text, rust_label.tail) == ("第2章", " Rust 之旅")
    question_label, line_break = question
    assert question_label.text == "第4章" and question_label.tail is None
    assert line_break.tag == f"{{{XHTML}}}br" and line_break.tail == "机器会思考吗？"
    assert plain.text == "第5章 ——机器的创造力：能走多远？"
    version, styled = root.findall(f".//{{{XHTML}}}h2")
    assert version.text == "第 2.0 章：版本管理"
    styled_label, punctuation, emphasis = styled
    assert styled_label.text == "第6章" and punctuation.text is None and punctuation.tail == " "
    assert emphasis.tag == f"{{{XHTML}}}em" and emphasis.text == "Rust？"
    assert nav.find(f".//{{{XHTML}}}a").text == "第1章"
    assert ncx.find(".//{*}navLabel/{*}text").text == "第1章"
