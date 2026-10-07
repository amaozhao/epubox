from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from engine.epub.preparation import PreparationConfig
from engine.services import state
from engine.services.preparation import prepare_translation
from engine.services.resume import plan_resume
from engine.services.session import find, remember
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


def test_compact_preparation_resumes_from_one_state_file(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Memory allocation is fast.</p>"})
    root = source.with_suffix("")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    state.initialize(root, source, digest, "compact-run")
    config = PreparationConfig(
        run_id="compact-run",
        auto_extract=False,
        extraction_config={"model": "fake", "target_language": "zh-Hans"},
        translation_config={"model": "fake", "target_language": "zh-Hans"},
    )

    result = asyncio.run(prepare_translation(source, root, config, StubChecker()))
    remember(source, result.work_dir)

    assert result.work_dir == root
    assert find(source) == root
    assert plan_resume(root).phase == "translation"
    assert {path.name for path in root.iterdir()} == {"source", "state.json"}
