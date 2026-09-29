from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from engine.epub.preparation_v25 import PreparationConfig
from engine.services.preparation_pipeline import prepare_translation
from engine.services.resume_plan import plan_resume
from tests.v23.book_factory import make_epub
from tests.v25.test_preparation_v25 import StubChecker


def _tree(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_read_only_resume_plan_is_stable_and_sends_no_requests(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(run_id="plan-preview", auto_extract=False),
            StubChecker(),
        )
    )
    before = _tree(prepared.work_dir)
    first = plan_resume(prepared.work_dir)
    second = plan_resume(prepared.work_dir)

    assert first == second
    assert first.phase == "translation" and first.actions == ("translate",)
    assert before == _tree(prepared.work_dir)
    assert not list((prepared.work_dir / "requests").glob("*.json"))


def test_old_bookplan_is_located_without_running_legacy_code(tmp_path: Path) -> None:
    (tmp_path / "bookplan.json").write_text('{"format":"epubox-book-1"}')

    preview = plan_resume(tmp_path)

    assert preview.status == "unsupported_format"
    assert preview.actions == ("start_new_run",)


def test_preview_does_not_claim_publication_when_frozen_source_changes(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(run_id="identity-check", auto_extract=False),
            StubChecker(),
        )
    )
    snapshot = prepared.work_dir / "source.epub"
    snapshot.chmod(0o644)
    snapshot.write_bytes(b"tampered")

    preview = plan_resume(prepared.work_dir)

    assert preview.status == "needs_attention"
    assert preview.actions == ("repair_shared_identity",)
    assert "source.epub" in preview.reasons
