import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from engine.epub.preparation import PreparationConfig, prepare_book
from engine.epub.validation import EpubCheckResult, EpubValidationError
from engine.services.report import write_report
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub


def error(path: Path, needle: str, *, message: str = 'attribute "data-test" not allowed') -> EpubCheckResult:
    with ZipFile(path) as archive:
        text = archive.read("OEBPS/chapter.xhtml").decode()
    offset = text.index(needle) + len(needle)
    prefix = text[:offset]
    line = prefix.count("\n") + 1
    column = len(prefix.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2
    diagnostic = f"ERROR(RSC-005): {path}/OEBPS/chapter.xhtml({line},{column}): {message}"
    return EpubCheckResult(("stub",), 1, (diagnostic,))


def test_source_errors_allow_preparation_and_are_recorded(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": '<p data-test="1">English.</p>'})

    class Checker:
        def check(self, path: Path) -> EpubCheckResult:
            return error(path, '<p data-test="1">')

    prepared = prepare_book(source, tmp_path / "work", PreparationConfig(run_id="baseline"), Checker())
    assert prepared.inventory.epubcheck is not None
    assert not prepared.inventory.epubcheck.passed
    assert prepared.inventory.warnings[0].code == "source_epubcheck_error"
    assert (prepared.work_dir / "report.json").is_file()
    saved = json.loads(write_report(RunStore(prepared.work_dir), status="paused", phase="terms").read_text())
    assert saved["source_validation"]["passed"] is False
    assert saved["source_validation"]["errors"]


def test_inherited_error_matches_same_element_after_translation_moves_coordinates(tmp_path: Path) -> None:
    from engine.epub.diagnostics import compare

    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": '<p>Before</p><p data-test="1">English.</p>'})
    target = make_epub(tmp_path / "target.epub", {"chapter.xhtml": '<p>中文😀\n前文</p><p data-test="1">译文。</p>'})
    result = compare(source, target, error(source, '<p data-test="1">'), error(target, '<p data-test="1">'))
    assert result["inherited_errors"] == 1


def test_same_count_and_message_on_another_element_is_a_new_error(tmp_path: Path) -> None:
    from engine.epub.diagnostics import compare

    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": '<p data-test="1">Before</p><p>After</p>'})
    target = make_epub(tmp_path / "target.epub", {"chapter.xhtml": '<p>中文</p><p data-test="1">之后</p>'})
    with pytest.raises(EpubValidationError, match="New EPUBCheck"):
        compare(source, target, error(source, '<p data-test="1">'), error(target, '<p data-test="1">'))


def test_unknown_checker_failures_cannot_be_inherited(tmp_path: Path) -> None:
    from engine.epub.diagnostics import compare

    source = make_epub(tmp_path / "source.epub")
    target = make_epub(tmp_path / "target.epub")
    failed = EpubCheckResult(("stub",), 1, ("checker process failed",))
    with pytest.raises(EpubValidationError):
        compare(source, target, failed, failed)


def test_checker_process_failure_stops_before_preparation_commit(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub")

    class Checker:
        def check(self, path: Path) -> EpubCheckResult:
            return EpubCheckResult(("stub",), 1, ("checker process failed",))

    with pytest.raises(EpubValidationError, match="Unrecognized EPUBCheck"):
        prepare_book(source, tmp_path / "work", PreparationConfig(run_id="broken"), Checker())
    assert not list((tmp_path / "work").glob("*/broken/preparation.json"))


def test_duplicate_error_is_new_and_resolved_source_error_is_allowed(tmp_path: Path) -> None:
    from engine.epub.diagnostics import compare

    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": '<p data-test="1">English.</p>'})
    target = make_epub(tmp_path / "target.epub", {"chapter.xhtml": '<p data-test="1">中文。</p>'})
    original = error(source, '<p data-test="1">')
    translated = error(target, '<p data-test="1">')
    with pytest.raises(EpubValidationError, match="New EPUBCheck"):
        compare(source, target, original, EpubCheckResult(("stub",), 1, translated.errors * 2))
    assert compare(source, target, original, EpubCheckResult(("stub",), 0))["inherited_errors"] == 0


def test_warning_only_source_has_honest_diagnostic_report(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub")

    class Checker:
        def check(self, path: Path) -> EpubCheckResult:
            return EpubCheckResult(("stub",), 0, warnings=(f"WARNING(OPF-999): {path}: Original warning",))

    prepared = prepare_book(source, tmp_path / "work", PreparationConfig(run_id="warning"), Checker())
    value = json.loads((prepared.work_dir / "report.json").read_text())["source_validation"]
    assert value["passed"] is True and len(value["warnings"]) == 1


@pytest.mark.asyncio
async def test_malformed_display_report_does_not_block_preparation_advancement(tmp_path, monkeypatch):
    from engine.services import preparation as pipeline

    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": '<p data-test="1">English.</p>'})

    class Checker:
        def check(self, path: Path) -> EpubCheckResult:
            return error(path, '<p data-test="1">')

    prepared = prepare_book(source, tmp_path / "work", PreparationConfig(run_id="display"), Checker())
    store = RunStore(prepared.work_dir)
    (store.root / "report.json").write_text("{broken")
    monkeypatch.setattr(pipeline, "_p1", lambda *args: (store, prepared.preparation, prepared.preparation_hash))
    reached = []

    async def advance(*args, **kwargs):
        reached.append(True)
        return pipeline.PreparationPipelineResult("paused", "terms", store.root, "display", "open")

    monkeypatch.setattr(pipeline, "_advance", advance)
    result = await pipeline.prepare_translation(
        source, tmp_path / "work", PreparationConfig(), Checker(), progress=lambda event: None
    )
    assert result.status == "paused" and reached == [True]
