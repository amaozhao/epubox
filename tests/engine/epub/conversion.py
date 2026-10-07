from pathlib import Path
from zipfile import ZipFile

import pytest

from engine.epub.upgrade import upgrade_package
from engine.epub.validation import EpubCheckResult, EpubValidationError, inspect_epub
from engine.epub.verification import file_hash, stage_epub, verify_baseline, verify_staged_epub
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


@pytest.mark.parametrize("version", ("2.0", "2.0.1", "2.1", "3.0", "3.3"))
def test_upgrade_is_staged_and_verified_without_changing_original_resources(tmp_path: Path, version: str):
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>中文正文。</p>"}, version=version)
    original = source.read_bytes()
    inventory = inspect_epub(source, file_hash(source), checker=StubChecker())
    replacements = upgrade_package(source, inventory, {})
    output = tmp_path / "target.epub"
    stage_epub(source, output, replacements, allow_additions=True)
    verification = verify_staged_epub(
        source,
        output,
        inventory,
        replacements,
        accepted_targets={path: {} for path in replacements},
        checker=StubChecker(),
        upgraded=True,
    )
    current = inspect_epub(output, file_hash(output), checker=StubChecker())
    assert current.epub_version == "3.0" and current.nav_path
    assert current.spine == inventory.spine
    assert verification.to_dict()["epub_version"] == "3.0"
    assert source.read_bytes() == original
    with ZipFile(source) as old, ZipFile(output) as new:
        for path in old.namelist():
            if path not in replacements:
                assert old.read(path) == new.read(path)


def test_upgrade_verification_rejects_unapproved_resource_and_non3_output(tmp_path: Path):
    source = make_epub(tmp_path / "source.epub", version="2.0")
    inventory = inspect_epub(source, file_hash(source), checker=StubChecker())
    output = tmp_path / "target.epub"
    stage_epub(source, output, {})
    with pytest.raises(EpubValidationError, match="3.0"):
        verify_staged_epub(source, output, inventory, {}, accepted_targets={}, checker=StubChecker(), upgraded=True)
    replacements = upgrade_package(source, inventory, {}) | {"extra.xml": b"<extra/>"}
    stage_epub(source, output, replacements, allow_additions=True)
    with pytest.raises(EpubValidationError, match="resource"):
        verify_staged_epub(
            source,
            output,
            inventory,
            replacements,
            accepted_targets={path: {} for path in replacements},
            checker=StubChecker(),
            upgraded=True,
        )


def test_upgraded_form_properties_are_accepted_only_in_output_inspection(tmp_path: Path):
    source = make_epub(
        tmp_path / "source.epub",
        {"chapter.xhtml": '<form action="#form" id="form"><input type="text"/></form>'},
        version="2.0",
    )
    inventory = inspect_epub(source, file_hash(source), checker=StubChecker())
    replacements = upgrade_package(source, inventory, {})
    output = tmp_path / "target.epub"
    stage_epub(source, output, replacements, allow_additions=True)
    verify_staged_epub(
        source,
        output,
        inventory,
        replacements,
        accepted_targets={p: {} for p in replacements},
        checker=StubChecker(),
        upgraded=True,
    )
    with pytest.raises(EpubValidationError, match="Script-generated"):
        inspect_epub(output, file_hash(output), checker=StubChecker())


def test_recovery_checks_actual_package_version_instead_of_trusting_upgrade_marker(tmp_path: Path):
    source = make_epub(tmp_path / "source.epub", version="2.0")
    proof = {"epub_version": "3.0", "epubcheck": EpubCheckResult(("stub",), 0).to_dict()}
    with pytest.raises(EpubValidationError, match="not EPUB 3.0"):
        verify_baseline(source, source, proof)


@pytest.mark.parametrize("field,value", (("returncode", False), ("errors", ["error"]), ("passed", False)))
def test_recovery_rejects_inconsistent_clean_epub3_check_result(tmp_path: Path, field, value):
    source = make_epub(tmp_path / "source.epub")
    check = EpubCheckResult(("stub",), 0).to_dict() | {field: value}
    with pytest.raises(EpubValidationError, match="evidence"):
        verify_baseline(source, source, {"epub_version": "3.0", "epubcheck": check})
