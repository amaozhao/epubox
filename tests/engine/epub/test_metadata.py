from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from engine.core.markup import parse_xml_bytes, qname_local_name
from engine.epub.publish import publish_atomic
from engine.epub.upgrade import upgrade_package
from engine.services.journal import BodyJournal
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.epub.test_publish import _case
from tests.engine.epub.test_upgrade import _book, _elements


@pytest.mark.parametrize("agent", ["creator", "contributor"])
def test_upgrade_recovers_empty_agent_from_unique_file_as_refinement(tmp_path: Path, agent: str) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "3.0", nav=True)
    replacement = (
        f'<dc:{agent} id="agent1"></dc:{agent}>'
        '<meta refines="#agent1" property="file-as">Kreps, Sarah E.</meta>'
        '<meta refines="#agent1" property="role" scheme="marc:relators">aut</meta>'
        '<meta refines="#agent1" property="display-seq">1</meta>'
        '<dc:creator id="agent2">James Patton Rogers</dc:creator>'
        "<dc:publisher></dc:publisher>"
        '<meta property="dcterms:publisher">Oxford University Press</meta>'
    ).encode()
    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path).replace(b"<dc:creator>A Author</dc:creator>", replacement)

    upgraded = upgrade_package(source, inventory, {inventory.opf_path: opf})
    metadata = upgraded[inventory.opf_path]

    recovered = next(element for element in _elements(metadata, agent) if element.get("id") == "agent1")
    assert recovered.text == "Kreps, Sarah E."
    assert [(element.get("id"), element.text) for element in _elements(metadata, "creator") if element.get("id")] == [
        *([("agent1", "Kreps, Sarah E.")] if agent == "creator" else []),
        ("agent2", "James Patton Rogers"),
    ]
    refinements = [element for element in _elements(metadata, "meta") if element.get("refines") == "#agent1"]
    assert [(element.get("property"), element.text) for element in refinements] == [
        ("file-as", "Kreps, Sarah E."),
        ("role", "aut"),
        ("display-seq", "1"),
    ]
    assert not _elements(metadata, "publisher")
    assert any(
        element.get("property") == "dcterms:publisher" and element.text == "Oxford University Press"
        for element in _elements(metadata, "meta")
    )


def test_upgrade_does_not_invent_empty_creator_when_file_as_refinements_are_ambiguous(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "3.0", nav=True)
    replacement = (
        b'<dc:creator id="creator1"></dc:creator>'
        b'<meta refines="#creator1" property="file-as">Kreps, Sarah E.</meta>'
        b'<meta refines="#creator1" property="file-as">Rogers, James Patton</meta>'
    )
    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path).replace(b"<dc:creator>A Author</dc:creator>", replacement)

    upgraded = upgrade_package(source, inventory, {inventory.opf_path: opf})
    creator = next(
        element for element in _elements(upgraded[inventory.opf_path], "creator") if element.get("id") == "creator1"
    )

    assert not (creator.text or "").strip()
    assert [
        element.text
        for element in _elements(upgraded[inventory.opf_path], "meta")
        if element.get("refines") == "#creator1" and element.get("property") == "file-as"
    ] == ["Kreps, Sarah E.", "Rogers, James Patton"]


def test_upgrade_syncs_existing_ncx_uid_to_authoritative_opf_identifier(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    inventory = _book(source, "2.0")
    assert inventory.ncx_path is not None
    old_uid = b"urn:isbn:9780197795668"
    expected_uid = b"32305487"
    ncx = (
        b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><head>'
        b'<meta name="dtb:uid" content="' + old_uid + b'"/>'
        b'</head><navMap><navPoint id="one"><navLabel><text>Translated label</text></navLabel>'
        b'<content src="chapter.xhtml#one"/></navPoint></navMap></ncx>'
    )
    with ZipFile(source) as archive:
        opf = archive.read(inventory.opf_path).replace(b"urn:test:book", expected_uid)

    upgraded = upgrade_package(
        source,
        inventory,
        {inventory.opf_path: opf, inventory.ncx_path: ncx},
    )

    assert upgraded[inventory.ncx_path] == ncx.replace(old_uid, expected_uid)
    package = parse_xml_bytes(upgraded[inventory.opf_path]).getroot()
    identifier = next(element for element in package.iter() if qname_local_name(element.tag) == "identifier")
    assert identifier.get("id") == package.get("unique-identifier") == "uid"
    assert identifier.text == expected_uid.decode()


def test_atomic_publication_recovers_empty_creator_and_keeps_translated_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.engine.agents.test_workflow as workflow_fixture

    def metadata_book(path: Path, documents: dict[str, str]) -> Path:
        source = make_epub(path, documents)
        with ZipFile(source) as archive:
            resources = {item.filename: archive.read(item.filename) for item in archive.infolist()}
        resources["OEBPS/content.opf"] = resources["OEBPS/content.opf"].replace(
            b"<dc:language>en</dc:language>",
            (
                b'<dc:creator id="creator1"></dc:creator>'
                b'<meta refines="#creator1" property="file-as">Kreps, Sarah E.</meta>'
                b'<meta refines="#creator1" property="role" scheme="marc:relators">aut</meta>'
                b'<meta refines="#creator1" property="display-seq">1</meta>'
                b'<dc:creator id="creator2">James Patton Rogers</dc:creator>'
                b'<meta refines="#creator2" property="file-as">Rogers, James Patton</meta>'
                b"<dc:language>en</dc:language>"
            ),
        )
        with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
            for name, raw in resources.items():
                archive.writestr(name, raw, compress_type=ZIP_STORED if name == "mimetype" else ZIP_DEFLATED)
        return source

    monkeypatch.setattr(workflow_fixture, "make_epub", metadata_book)
    case = _case(tmp_path, "<p>English body.</p>", ("English body.",))
    targets = {
        item.item_id: item.source_projection.replace("English body.", "中文正文。")
        for inventory in case.index.inventories
        for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "book-cn.epub"

    publish_atomic(case.session.store, output, StubChecker())

    with ZipFile(output) as archive:
        metadata = archive.read("OEBPS/content.opf")
        chapter = archive.read("OEBPS/chapter.xhtml").decode()
    assert [(element.get("id"), element.text) for element in _elements(metadata, "creator")] == [
        ("creator1", "Kreps, Sarah E."),
        ("creator2", "James Patton Rogers"),
    ]
    assert [
        (element.get("property"), element.text)
        for element in _elements(metadata, "meta")
        if element.get("refines") == "#creator1"
    ] == [
        ("file-as", "Kreps, Sarah E."),
        ("role", "aut"),
        ("display-seq", "1"),
    ]
    assert "中文正文。" in chapter
