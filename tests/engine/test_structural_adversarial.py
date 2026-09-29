from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from engine.core.styles import ReorderPolicy, scan_css, scan_stylesheets
from engine.epub.assembly import assemble_document
from engine.epub.publication import stage_epub, validate_assembled_document, verify_staged_epub
from engine.epub.validation import EpubCheckResult, EpubValidationError, inspect_epub
from engine.item.extractor import extract_document
from engine.item.inline import ProjectionError, validate_projection
from tests.engine.epub.book_factory import make_epub


class StubChecker:
    def check(self, path: Path) -> EpubCheckResult:
        assert path.is_file()
        return EpubCheckResult(("stub-epubcheck",), 0)


def _source(body: str, head: str = "") -> str:
    return (
        f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Book</title>{head}</head><body>{body}</body></html>'
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_epub(path: Path, replacements: dict[str, bytes], additions: dict[str, bytes] | None = None) -> None:
    additions = additions or {}
    temporary = path.with_suffix(".tmp.epub")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            data = replacements.get(info.filename, source.read(info.filename))
            target.writestr(
                info.filename,
                data,
                compress_type=zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED,
            )
        for name, data in additions.items():
            target.writestr(name, data)
    temporary.replace(path)


def test_t02_same_label_link_and_emphasis_reorder_keep_source_identity() -> None:
    document = extract_document(
        _source('<div><p><a href="#note">same</a> and <em>same</em></p></div><div><p id="note">Note.</p></div>'),
        "OPS/chapter.xhtml",
        "source-sha",
    )
    unit = next(unit for unit in document.units if unit.kind == "paragraph" and len(unit.registry) == 2)
    refs = {entry.hints["element"]: ref for ref, entry in unit.registry.items()}
    target = f"⟦+{refs['em']}⟧强调对象⟦-{refs['em']}⟧位于⟦+{refs['a']}⟧链接对象⟦-{refs['a']}⟧之前。"
    targets = {item.unit_id: item.source_projection for item in document.units} | {unit.unit_id: target}

    assembled = assemble_document(document, targets)

    assert "<em>强调对象</em>" in assembled.markup
    assert '<a href="#note">链接对象</a>' in assembled.markup
    assert assembled.markup.index("<em>强调对象</em>") < assembled.markup.index('<a href="#note">链接对象</a>')
    validate_assembled_document(
        document,
        targets,
        assembled.markup,
        source_to_target=assembled.source_to_target,
    )


def test_t03_bad_marker_candidates_are_rejected_without_poisoning_later_units() -> None:
    document = extract_document(
        _source(
            '<div><p><a href="#n">First</a> and <em>second</em>.</p></div><div><p id="n">Later content.</p></div>'
        ),
        "OPS/chapter.xhtml",
        "source-sha",
    )
    marked = next(unit for unit in document.units if len(unit.registry) == 2)
    later = next(unit for unit in document.units if "Later content" in unit.source_projection)
    source = marked.source_projection
    refs = tuple(marked.registry)
    bad = (
        source.replace(f"⟦-{refs[0]}⟧", "", 1),
        source.replace(f"⟦+{refs[0]}⟧", f"⟦+{refs[0]}⟧⟦+{refs[0]}⟧", 1),
        source.replace(refs[0], "g-missing", 1),
        source + "⟦=x-extra⟧",
    )

    for candidate in bad:
        with pytest.raises(ProjectionError):
            validate_projection(marked, candidate)
    validate_projection(later, "后续内容。")
    targets = {unit.unit_id: unit.source_projection for unit in document.units}
    targets[later.unit_id] = "后续内容。"
    assembled = assemble_document(document, targets)
    assert "后续内容。" in assembled.markup


def test_t07_first_child_and_unknown_css_lock_reorder_without_touching_unrelated_source() -> None:
    head = '<link rel="stylesheet" href="styles/book.css" data-trace="keep"/>'
    markup = _source("<p><em>first</em><em>second</em></p>", head)
    styles = {"OPS/styles/book.css": "em:first-child { color: blue }"}
    locked = extract_document(markup, "OPS/chapter.xhtml", "source-sha", styles=styles)
    unit = next(unit for unit in locked.units if unit.kind == "paragraph")

    assert all(entry.movement == "locked" and not entry.reorder_allowed for entry in unit.registry.values())
    scan = scan_stylesheets(styles, roots=("OPS/styles/book.css",))
    assert scan.policy == ReorderPolicy.LOCKED
    assert scan.locked_selectors == ("em:first-child",)
    assert scan_css("@unknown thing { em { color: red } }").policy == ReorderPolicy.UNKNOWN
    unknown = extract_document(
        _source("<p><em>first</em><em>second</em></p>", "<style>@unknown thing { em {color:red} }</style>"),
        "OPS/unknown.xhtml",
        "source-sha",
    )
    unknown_unit = next(unit for unit in unknown.units if unit.kind == "paragraph")
    assert all(entry.movement == "locked" for entry in unknown_unit.registry.values())
    assert 'data-trace="keep"' in assemble_document(locked, {}, identity=True).markup


def test_t17_actual_assembled_mutations_delete_duplicate_href_and_alt_are_rejected() -> None:
    document = extract_document(
        _source(
            '<div><p>Lead <a href="#note">link</a> tail<img src="chart.png" alt="Chart"/></p></div><div><p id="note">Note.</p></div>'
        ),
        "OPS/chapter.xhtml",
        "source-sha",
    )
    paragraph = next(unit for unit in document.units if "Lead" in unit.source_projection)
    attribute = next(unit for unit in document.units if unit.kind == "attribute")
    targets = {unit.unit_id: unit.source_projection for unit in document.units}
    targets[paragraph.unit_id] = (
        paragraph.source_projection.replace("Lead ", "开始 ").replace("link", "链接").replace(" tail", " 结尾")
    )
    targets[attribute.unit_id] = "图表"
    assembled = assemble_document(document, targets)
    validate_assembled_document(document, targets, assembled.markup, source_to_target=assembled.source_to_target)

    mutations = (
        assembled.markup.replace("开始 ", "", 1),
        assembled.markup.replace(" 结尾", " 结尾 结尾", 1),
        assembled.markup.replace('href="#note"', 'href="#wrong"', 1),
        assembled.markup.replace('alt="图表"', 'alt=""', 1),
    )
    codes = []
    for mutation in mutations:
        with pytest.raises(EpubValidationError) as error:
            validate_assembled_document(document, targets, mutation, source_to_target=assembled.source_to_target)
        codes.append(error.value.code)
    assert codes == [
        "target_text_mismatch",
        "target_text_mismatch",
        "frozen_attribute_changed",
        "target_attribute_mismatch",
    ]


def test_t20_epub2_and_font_obfuscation_preserve_package_boundaries(tmp_path: Path) -> None:
    epub2 = make_epub(tmp_path / "book2.epub", version="2.0")
    inventory2 = inspect_epub(epub2, _sha256(epub2), checker=StubChecker())
    assert inventory2.epub_version == "2.0" and inventory2.ncx_path == "OEBPS/toc.ncx"

    font = make_epub(tmp_path / "font.epub")
    with zipfile.ZipFile(font) as archive:
        opf = archive.read("OEBPS/content.opf").replace(
            b"</manifest>", b'<item id="font" href="font.otf" media-type="font/otf"/></manifest>'
        )
    encryption = (
        b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        b'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        b'<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
        b'<enc:CipherData><enc:CipherReference URI="OEBPS/font.otf"/></enc:CipherData>'
        b"</enc:EncryptedData></encryption>"
    )
    _rewrite_epub(
        font,
        {"OEBPS/content.opf": opf},
        {"OEBPS/font.otf": b"font-bytes", "META-INF/encryption.xml": encryption},
    )
    inventory = inspect_epub(font, _sha256(font), checker=StubChecker())
    assert inventory.obfuscated_fonts == ("OEBPS/font.otf",)
    staged = tmp_path / "staged.epub"
    stage_epub(font, staged, {})
    verified = verify_staged_epub(
        font,
        staged,
        inventory,
        {},
        accepted_targets={},
        checker=StubChecker(),
    )
    assert verified.epubcheck.passed
    with zipfile.ZipFile(staged) as archive:
        assert archive.read("OEBPS/font.otf") == b"font-bytes"
        assert archive.read("META-INF/encryption.xml") == encryption

    encrypted_body = encryption.replace(
        b"http://www.idpf.org/2008/embedding",
        b"http://www.w3.org/2001/04/xmlenc#aes256-cbc",
    ).replace(b"OEBPS/font.otf", b"OEBPS/chapter1.xhtml")
    _rewrite_epub(font, {"META-INF/encryption.xml": encrypted_body})
    with pytest.raises(EpubValidationError) as error:
        inspect_epub(font, _sha256(font), checker=StubChecker())
    assert any(issue.code == "encrypted_content" for issue in error.value.issues)
