import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from engine.epub.preparation import PreparationConfig, prepare_book
from engine.epub.validation import EpubCheckResult, EpubValidationError, inspect_epub
from engine.epub.verification import stage_epub, verify_staged_epub
from engine.services import state
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker


def noncanonical_source(tmp_path: Path, *, misplaced: bool, compressed: bool) -> Path:
    source = make_epub(tmp_path / "book.epub")
    with zipfile.ZipFile(source) as archive:
        resources = [(info, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(source, "w") as archive:
        if misplaced:
            archive.writestr("metadata.json", b"{}")
        for info, raw in resources:
            if info.filename == "mimetype":
                info.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
            archive.writestr(info, raw)
    return source


@pytest.mark.parametrize(("misplaced", "compressed"), [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("compact", [False, True])
def test_noncanonical_source_preserves_identity_and_stages_canonical_output(tmp_path, misplaced, compressed, compact):
    source = noncanonical_source(tmp_path, misplaced=misplaced, compressed=compressed)
    original = source.read_bytes()
    source_hash = hashlib.sha256(original).hexdigest()

    class Checker:
        def check(self, path):
            with zipfile.ZipFile(path) as archive:
                first = archive.infolist()[0]
                canonical = first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
            errors = () if canonical else (f"ERROR(PKG-006): {path}(-1,-1): Noncanonical mimetype layout.",)
            return EpubCheckResult(("stub",), 0 if canonical else 1, errors)

    checker = Checker()
    root = tmp_path / "work"
    if compact:
        state.initialize(root, source, source_hash, "layout")
    prepared = prepare_book(source, root, PreparationConfig(run_id="layout"), checker)

    assert prepared.preparation.state == "parsed_ready"
    assert prepared.preparation.source_hash == source_hash
    assert prepared.source_snapshot.read_bytes() == original
    assert source.read_bytes() == original
    assert prepared.inventory.epubcheck is not None and not prepared.inventory.epubcheck.passed
    report = json.loads(state.read(prepared.work_dir / "report.json"))
    assert report["source_validation"]["errors"]

    staged = tmp_path / "staged.epub"
    stage_epub(prepared.source_snapshot, staged, {})
    with zipfile.ZipFile(staged) as archive, zipfile.ZipFile(source) as original_archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        assert {name: archive.read(name) for name in archive.namelist()} == {
            name: original_archive.read(name) for name in original_archive.namelist()
        }
    verified = verify_staged_epub(
        prepared.source_snapshot, staged, prepared.inventory, {}, accepted_targets={}, checker=checker
    )
    assert verified.epubcheck.passed


@pytest.mark.parametrize(("misplaced", "compressed"), [(True, False), (False, True)])
def test_noncanonical_output_is_still_rejected(tmp_path, misplaced, compressed):
    source = noncanonical_source(tmp_path, misplaced=misplaced, compressed=compressed)

    with pytest.raises(EpubValidationError) as error:
        inspect_epub(source, "source", checker=StubChecker(), for_output=True)

    assert error.value.code == "invalid_mimetype_layout"


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("../unsafe", b"bad", "unsafe_path"),
        ("mimetype", b"application/epub+zip", "duplicate_zip_entry"),
    ],
)
def test_noncanonical_source_keeps_zip_safety_checks(tmp_path, name, data, expected):
    source = noncanonical_source(tmp_path, misplaced=True, compressed=False)
    with zipfile.ZipFile(source, "a") as archive:
        if name == "mimetype":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(name, data)
        else:
            archive.writestr(name, data)

    with pytest.raises(EpubValidationError) as error:
        inspect_epub(source, "source", checker=StubChecker())

    assert error.value.code == expected


@pytest.mark.parametrize("mimetype", [None, b"application/not-epub"])
def test_source_mimetype_must_still_exist_and_have_exact_content(tmp_path, mimetype):
    source = noncanonical_source(tmp_path, misplaced=True, compressed=False)
    with zipfile.ZipFile(source) as archive:
        resources = [(info, archive.read(info)) for info in archive.infolist() if info.filename != "mimetype"]
    with zipfile.ZipFile(source, "w") as archive:
        for info, raw in resources:
            archive.writestr(info, raw)
        if mimetype is not None:
            archive.writestr("mimetype", mimetype)

    with pytest.raises(EpubValidationError) as error:
        inspect_epub(source, "source", checker=StubChecker())

    assert error.value.code == "invalid_mimetype"
