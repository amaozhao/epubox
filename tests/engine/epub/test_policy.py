from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from engine.epub.publish import publish_atomic
from engine.epub.validation import EpubValidationError, inspect_epub
from engine.epub.verification import stage_epub, verify_staged_epub
from engine.services.journal import BodyJournal
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.epub.test_publish import _case


def test_stage_and_verify_explicit_font_removal(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/unused.ttf", b"font bytes")
    inventory = inspect_epub(source, "source", checker=StubChecker())
    staged = tmp_path / "staged.epub"
    removed = frozenset({"OEBPS/unused.ttf"})
    stage_epub(source, staged, {}, removed_paths=removed)
    verify_staged_epub(
        source, staged, inventory, {}, accepted_targets={}, checker=StubChecker(), removed_paths=removed
    )
    with zipfile.ZipFile(staged) as archive:
        assert "OEBPS/unused.ttf" not in archive.namelist()
    with pytest.raises(EpubValidationError, match="inventory"):
        verify_staged_epub(source, staged, inventory, {}, accepted_targets={}, checker=StubChecker())


def test_verifier_rejects_deletion_of_nonfont_resource(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("OEBPS/image.png", b"image bytes")
    inventory = inspect_epub(source, "source", checker=StubChecker())
    staged = tmp_path / "staged.epub"
    removed = frozenset({"OEBPS/image.png"})
    stage_epub(source, staged, {}, removed_paths=removed)
    with pytest.raises(EpubValidationError, match="font"):
        verify_staged_epub(
            source, staged, inventory, {}, accepted_targets={}, checker=StubChecker(), removed_paths=removed
        )


@pytest.mark.parametrize("via_cli", [False, True])
def test_atomic_publication_cleans_fonts_and_repacks_old_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_cli: bool
) -> None:
    case = _case(tmp_path, '<p style="font-family: Publisher; font-weight: bold">Hello world.</p>', ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "reader.epub"
    result = publish_atomic(case.session.store, output, StubChecker())
    proof = result["verification"]
    assert isinstance(proof, dict) and proof["font_policy"] == "reader-fonts-v1"
    with zipfile.ZipFile(output) as archive:
        chapter = archive.read("OEBPS/chapter.xhtml")
        assert b"Publisher" not in chapter
        assert b"font-weight: bold" in chapter
    intent_path = case.session.store.root / "publish.json"
    intent = json.loads(intent_path.read_bytes())
    intent["verification"].pop("font_policy")
    intent_path.write_text(json.dumps(intent))
    if via_cli:
        from engine import cli

        monkeypatch.setattr(cli, "checker_for_source", lambda *args: StubChecker())
        monkeypatch.setattr(cli, "build_run_model", lambda *args, **kwargs: pytest.fail("model constructed"))
        monkeypatch.setattr(cli, "_record", lambda outcome: outcome)
        outcome = cli._completed_run_outcome(case.session.store.root, output)
        assert outcome is not None and outcome.status == "completed"
        assert json.loads(intent_path.read_bytes())["verification"]["font_policy"] == "reader-fonts-v1"
    else:
        result = publish_atomic(case.session.store, output, StubChecker())
        proof = result["verification"]
        assert isinstance(proof, dict) and proof["font_policy"] == "reader-fonts-v1"


def _with_fonts(path: Path, documents=None, *, version="3.0") -> Path:
    source = make_epub(path, documents, version=version)
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf").replace(
            b"</manifest>",
            b'<item id="font" href="fonts/Book.ttf" media-type="font/ttf"/>'
            b'<item id="css" href="style.css" media-type="text/css"/></manifest>',
        )
        chapter = archive.read("OEBPS/chapter.xhtml").replace(
            b"</head>", b'<link rel="stylesheet" type="text/css" href="style.css"/></head>'
        )
    staged = path.with_suffix(".candidate.epub")
    stage_epub(
        source,
        staged,
        {
            "OEBPS/content.opf": opf,
            "OEBPS/chapter.xhtml": chapter,
            "OEBPS/style.css": b"@font-face {font-family:Publisher;src:url(fonts/Book.ttf)} "
            b"p {font-family:Publisher;line-height:1.5} strong {font-weight:700}",
            "OEBPS/fonts/Book.ttf": b"synthetic font bytes",
            "META-INF/encryption.xml": b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b'<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
            b'<EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
            b'<CipherData><CipherReference URI="OEBPS/fonts/Book.ttf"/></CipherData>'
            b"</EncryptedData></encryption>",
        },
        allow_additions=True,
    )
    staged.replace(source)
    return source


def test_atomic_publication_removes_font_manifest_and_obfuscation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.engine.agents.test_workflow as workflow_fixture

    monkeypatch.setattr(workflow_fixture, "make_epub", _with_fonts)
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    source_bytes = (tmp_path / "source.epub").read_bytes()
    output = tmp_path / "reader.epub"

    publish_atomic(case.session.store, output, StubChecker())

    assert (tmp_path / "source.epub").read_bytes() == source_bytes
    with zipfile.ZipFile(output) as archive:
        assert "OEBPS/fonts/Book.ttf" not in archive.namelist()
        assert "META-INF/encryption.xml" not in archive.namelist()
        assert b'id="font"' not in archive.read("OEBPS/content.opf")
        assert b"Publisher" not in archive.read("OEBPS/style.css")
        assert b"font-weight:700" in archive.read("OEBPS/style.css")
        assert b"line-height:1.5" in archive.read("OEBPS/style.css")


@pytest.mark.parametrize("identity", [False, True])
def test_legacy_publication_cleans_embedded_fonts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: bool
) -> None:
    import tests.engine.epub.test_publication as legacy
    from engine.epub.publication import publish_book

    monkeypatch.setattr(legacy, "make_epub", _with_fonts)
    source, store, plan, documents = legacy._prepared(tmp_path)
    monkeypatch.setattr(store, "read_bookplan", lambda: plan)
    if not identity:
        records = legacy._accepted_records(documents)
        manifests = legacy._review_manifests(plan, records)
        legacy._coherence_checks(store, documents, records)
        monkeypatch.setattr(store, "read_unit", lambda unit_id: records[unit_id])
        monkeypatch.setattr(store, "read_request", lambda request_id: manifests[request_id])
    output = tmp_path / "reader.epub"
    before = source.read_bytes()

    result = publish_book(store, output, StubChecker(), identity=identity)

    assert source.read_bytes() == before
    proof = result["verification"]
    assert isinstance(proof, dict) and proof["font_policy"] == "reader-fonts-v1"
    with zipfile.ZipFile(output) as archive:
        assert "OEBPS/fonts/Book.ttf" not in archive.namelist()
        assert "META-INF/encryption.xml" not in archive.namelist()
        assert b"Publisher" not in archive.read("OEBPS/style.css")


@pytest.mark.parametrize(
    "removed", [frozenset({"missing.ttf"}), frozenset({"mimetype"}), frozenset({"OEBPS/content.opf"})]
)
def test_stage_rejects_invalid_or_conflicting_removals(tmp_path: Path, removed: frozenset[str]) -> None:
    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf")
    with pytest.raises(EpubValidationError, match="Removed resources"):
        stage_epub(source, tmp_path / "target.epub", {"OEBPS/content.opf": opf}, removed_paths=removed)


def test_extensionless_font_mime_case_is_consistent_during_cleanup_and_verification(tmp_path: Path) -> None:
    from engine.epub.styles import use_reader_fonts

    source = make_epub(tmp_path / "source.epub")
    with zipfile.ZipFile(source) as archive:
        opf = archive.read("OEBPS/content.opf").replace(
            b"</manifest>", b'<item id="font" href="fontdata" media-type="Font/TTF"/></manifest>'
        )
    staged = tmp_path / "with-font.epub"
    stage_epub(source, staged, {"OEBPS/content.opf": opf, "OEBPS/fontdata": b"font"}, allow_additions=True)
    inventory = inspect_epub(staged, "source", checker=StubChecker())
    replacements, removed = use_reader_fonts(staged, inventory, {})
    output = tmp_path / "output.epub"
    stage_epub(staged, output, replacements, removed_paths=removed)
    verify_staged_epub(
        staged,
        output,
        inventory,
        replacements,
        accepted_targets={path: {} for path in replacements},
        checker=StubChecker(),
        removed_paths=removed,
        reader_fonts=True,
    )
    assert removed == frozenset({"OEBPS/fontdata"})


def test_verification_rejects_modified_stylesheet_after_cleanup(tmp_path: Path) -> None:
    from engine.epub.styles import use_reader_fonts

    source = _with_fonts(tmp_path / "source.epub", {"chapter.xhtml": "<p>Text</p>"})
    inventory = inspect_epub(source, "source", checker=StubChecker())
    replacements, removed = use_reader_fonts(source, inventory, {})
    output = tmp_path / "output.epub"
    stage_epub(source, output, replacements | {"OEBPS/style.css": b"p{display:none}"}, removed_paths=removed)
    with pytest.raises(EpubValidationError, match="Stylesheet differs"):
        verify_staged_epub(
            source,
            output,
            inventory,
            replacements,
            accepted_targets={path: {} for path in replacements},
            checker=StubChecker(),
            removed_paths=removed,
            reader_fonts=True,
        )
