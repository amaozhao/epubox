import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from engine import cli
from engine.epub.preparation import prepare_book
from engine.schemas.contracts import ItemStatus
from engine.services.atomic import AtomicStore, StoreLocked
from engine.services.coherence import load_budget_overrides
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.support import ready_store


@pytest.mark.asyncio
async def test_internal_translation_error_keeps_a_failed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, _ = ready_store(tmp_path)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("unexpected translation bug")

    monkeypatch.setattr(cli, "run_translation", broken)
    with pytest.raises(RuntimeError, match="unexpected translation bug"):
        await cli._finish("ready", "translation", store.root, tmp_path / "out.epub", object(), object(), False)

    report = json.loads((store.root / "report.json").read_text())
    assert report["status"] == "failed"
    assert "RuntimeError: unexpected translation bug" in (store.root / "internal-error.txt").read_text()


def test_internal_preparation_error_keeps_a_failed_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    work_root = tmp_path / "work"
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())

    async def broken(*_args, **_kwargs):
        raise RuntimeError("unexpected preparation bug")

    monkeypatch.setattr(cli, "prepare_translation", broken)
    with pytest.raises(RuntimeError, match="unexpected preparation bug"):
        cli.translate_book(source, work_root=work_root)

    run_dir = next(
        path for path in (work_root / hashlib.sha256(source.read_bytes()).hexdigest()).iterdir() if path.is_dir()
    )
    report = json.loads((run_dir / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["phase"] == "preparation"
    assert "RuntimeError: unexpected preparation bug" in (run_dir / "internal-error.txt").read_text()


def test_translate_command_routes_only_through_preparation_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    captured = {}
    model = SimpleNamespace(id=cli.settings.AGNES_MODEL)
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())

    async def advance(actual_source, output, work_root, config, checker, *, model, overwrite, progress):
        captured.update(
            source=actual_source,
            output=output,
            work_root=work_root,
            config=config,
            model=model,
            overwrite=overwrite,
        )
        return cli.RunOutcome("paused", tmp_path / "work", "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    result = cli.translate_book(source, work_root=tmp_path / "work", provider="agnes")

    assert result.status == "paused"
    assert captured["source"] == source.resolve()
    assert captured["config"].auto_extract is True
    assert captured["config"].extraction_config["model"] == model.id
    assert captured["config"].extraction_config["resolution_protocol_version"] == "epubox-term-resolution-2"
    assert captured["config"].translation_config["target_language"] == "zh-Hans"


def test_default_paths_are_beside_source_independent_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_epub(tmp_path / "library" / "novel.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())
    outputs: list[Path] = []

    async def advance(actual_source, output, actual_work_root, config, actual_checker, **_kwargs):
        outputs.append(output)
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    result = cli.translate_book(source)

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.work_dir.parent == source.parent / source.stem / source_hash
    assert outputs == [source.with_name("novel-cn.epub")]


def test_source_identity_change_before_snapshot_never_starts_the_new_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>First edition.</p>"})
    replacement = make_epub(tmp_path / "replacement.epub", {"chapter.xhtml": "<p>Second edition.</p>"})
    replacement_bytes = replacement.read_bytes()
    old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    new_hash = hashlib.sha256(replacement_bytes).hexdigest()
    root = source.parent / source.stem
    source_root = root / old_hash
    source_root.mkdir(parents=True)
    accounting = source_root / "accounting.json"
    accounting.write_bytes(b'{"http_attempts":31}')
    hash_calls = 0
    model_sends = 0
    original_hash = cli._sha256_file

    def hash_then_change(path: Path) -> str:
        nonlocal hash_calls
        digest = original_hash(path)
        if hash_calls == 0:
            source.write_bytes(replacement_bytes)
        hash_calls += 1
        return digest

    class Model:
        async def ainvoke(self, *_args, **_kwargs):
            nonlocal model_sends
            model_sends += 1
            raise AssertionError("source mismatch must fail before any model request")

    monkeypatch.setattr(cli, "_sha256_file", hash_then_change)
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: Model())
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())

    with pytest.raises(cli.IdentityMismatch, match="source EPUB identity changed"):
        cli.translate_book(source)

    assert hash_calls == 1
    assert model_sends == 0
    assert accounting.read_bytes() == b'{"http_attempts":31}'
    assert not (root / new_hash).exists()
    assert not list(root.glob(".source-*.tmp"))
    assert not list(root.rglob("requests/*.json"))


def test_output_alias_never_overwrites_source(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    alias = tmp_path / "alias.epub"
    alias.symlink_to(source)

    with pytest.raises(ValueError, match="aliases"):
        cli.check_output(source.resolve(), alias, overwrite=True)


def test_repeating_translate_command_reuses_the_same_prepared_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    seen_run_ids: list[str | None] = []
    resumed: list[Path] = []
    checker = StubChecker()
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: checker)

    async def advance(actual_source, output, actual_work_root, config, actual_checker, **_kwargs):
        seen_run_ids.append(config.run_id)
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    async def resume(work_dir, *_args, **_kwargs):
        resumed.append(work_dir)
        return cli.RunOutcome("paused", work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    monkeypatch.setattr(cli, "_advance_work_dir", resume)
    first = cli.translate_book(source)
    second = cli.translate_book(source)

    assert first.work_dir == second.work_dir
    assert seen_run_ids == [first.work_dir.name]
    assert resumed == [first.work_dir]


def test_default_work_roots_separate_same_named_books_in_different_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = [
        make_epub(tmp_path / directory / "book.epub", {"chapter.xhtml": "<p>Same book.</p>"})
        for directory in ("one", "two")
    ]
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())

    async def advance(actual_source, _output, actual_work_root, config, actual_checker, **_kwargs):
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    results = [cli.translate_book(source) for source in sources]

    source_hash = hashlib.sha256(sources[0].read_bytes()).hexdigest()
    assert [result.work_dir.parent for result in results] == [
        source.parent / source.stem / source_hash for source in sources
    ]


def test_default_work_root_migrates_legacy_progress_without_record_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "library" / "book.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    cwd = tmp_path / "old-directory"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    legacy_root = cwd / "work"
    model_calls = 0

    class ForbiddenModel:
        id = cli.settings.AGNES_MODEL

        async def ainvoke(self, *_args, **_kwargs):
            nonlocal model_calls
            model_calls += 1
            raise AssertionError("migration must not repeat model work")

    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: ForbiddenModel())
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())

    async def prepare(actual_source, _output, actual_work_root, config, actual_checker, **_kwargs):
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", prepare)
    legacy = cli.translate_book(source, work_root=legacy_root)
    (legacy.work_dir / "bookplan.json").write_text("{}")
    before = {
        path.relative_to(legacy.work_dir): path.read_bytes() for path in legacy.work_dir.rglob("*") if path.is_file()
    }

    async def forbidden_prepare(*_args, **_kwargs):
        raise AssertionError("migration must resume the frozen run")

    resumed_paths: list[Path] = []

    async def resume(actual_work_dir, *_args, **_kwargs):
        resumed_paths.append(actual_work_dir)
        return cli.RunOutcome("paused", actual_work_dir, "translation")

    monkeypatch.setattr(cli, "_advance_source", forbidden_prepare)
    monkeypatch.setattr(cli, "_advance_work_dir", resume)
    migrated = cli.translate_book(source)

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    expected = source.parent / source.stem / source_hash / legacy.work_dir.name
    after = {
        path.relative_to(migrated.work_dir): path.read_bytes()
        for path in migrated.work_dir.rglob("*")
        if path.is_file()
    }
    assert migrated.work_dir == expected
    assert resumed_paths == [expected]
    assert not legacy.work_dir.exists()
    assert after == before
    assert model_calls == 0


def test_default_work_root_refuses_symlinked_run_without_moving_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "library" / "book.epub", {"chapter.xhtml": "<p>Keep progress.</p>"})
    monkeypatch.chdir(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    legacy = tmp_path / "work" / digest
    run = legacy / "saved-run"
    run.mkdir(parents=True)
    checkpoint = run / "checkpoint.json"
    checkpoint.write_bytes(b'{"http_attempts":17}')
    dangling = legacy / "missing-run"
    dangling.symlink_to(tmp_path / "offline" / "missing-run", target_is_directory=True)
    model_calls = 0

    def build(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return object()

    monkeypatch.setattr(cli, "build_run_model", build)
    with pytest.raises(cli.IdentityMismatch, match="run checkpoint"):
        cli.translate_book(source)

    assert model_calls == 0
    assert checkpoint.read_bytes() == b'{"http_attempts":17}'
    assert dangling.is_symlink()
    assert legacy.is_dir()
    assert not (source.parent / source.stem / digest).exists()


@pytest.mark.parametrize("locked_directory", ["source", "run"])
def test_default_work_root_does_not_move_a_locked_checkpoint(tmp_path, monkeypatch, locked_directory) -> None:
    source = make_epub(tmp_path / "library" / "book.epub", {"chapter.xhtml": "<p>Keep progress.</p>"})
    monkeypatch.chdir(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    legacy = tmp_path / "work" / digest
    run = legacy / "saved-run"
    run.mkdir(parents=True)
    checkpoint = run / "checkpoint.json"
    checkpoint.write_text('{"accepted":1}')
    with (
        AtomicStore(legacy if locked_directory == "source" else run).lock(blocking=False),
        pytest.raises(StoreLocked),
    ):
        cli._default_work_root(source, digest)
    assert checkpoint.read_text() == '{"accepted":1}'
    assert not (source.parent / source.stem / digest).exists()


def test_default_work_root_never_merges_competing_checkpoints(tmp_path, monkeypatch) -> None:
    source = make_epub(tmp_path / "library" / "book.epub", {"chapter.xhtml": "<p>Keep progress.</p>"})
    monkeypatch.chdir(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    roots = (tmp_path / "work" / digest, source.parent / source.stem / digest)
    for index, root in enumerate(roots):
        root.mkdir(parents=True)
        (root / "checkpoint.json").write_text(str(index))
    with pytest.raises(cli.IdentityMismatch, match="multiple checkpoint locations"):
        cli._default_work_root(source, digest)
    assert [(root / "checkpoint.json").read_text() for root in roots] == ["0", "1"]


def test_default_work_root_refuses_dangling_legacy_checkpoint_link(tmp_path, monkeypatch) -> None:
    source = make_epub(tmp_path / "library" / "book.epub", {"chapter.xhtml": "<p>Keep progress.</p>"})
    monkeypatch.chdir(tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    legacy = tmp_path / "work" / digest
    legacy.parent.mkdir()
    missing = tmp_path / "offline-disk" / digest
    legacy.symlink_to(missing, target_is_directory=True)
    with pytest.raises(cli.IdentityMismatch, match="symbolic link"):
        cli._default_work_root(source, digest)
    assert legacy.is_symlink() and legacy.readlink() == missing
    assert not (source.parent / source.stem / digest).exists()


def test_default_work_root_refuses_dangling_destination_link(tmp_path: Path) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Keep progress.</p>"})
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = source.parent / source.stem
    root.mkdir()
    destination = root / digest
    destination.symlink_to(tmp_path / "offline" / digest, target_is_directory=True)

    with pytest.raises(cli.IdentityMismatch, match="symbolic link"):
        cli._default_work_root(source, digest)
    assert destination.is_symlink()


def test_explicit_work_root_refuses_dangling_parent_before_model_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Keep progress.</p>"})
    missing = tmp_path / "offline"
    link = tmp_path / "worklink"
    link.symlink_to(missing, target_is_directory=True)
    model_calls = 0

    def build(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return object()

    monkeypatch.setattr(cli, "build_run_model", build)
    with pytest.raises(cli.IdentityMismatch, match="dangling symbolic link"):
        cli.translate_book(source, work_root=link / "nested")

    assert model_calls == 0
    assert link.is_symlink()
    assert not missing.exists()


def test_run_discovery_refuses_dangling_link_before_model_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Keep progress.</p>"})
    work_root = tmp_path / "work"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_root = work_root / digest
    source_root.mkdir(parents=True)
    checkpoint = source_root / "usage.json"
    checkpoint.write_bytes(b'{"http_attempts":23}')
    dangling = source_root / "missing-run"
    dangling.symlink_to(tmp_path / "offline" / "missing-run", target_is_directory=True)
    model_calls = 0

    def build(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return object()

    monkeypatch.setattr(cli, "build_run_model", build)
    with pytest.raises(cli.IdentityMismatch, match="run checkpoint"):
        cli.translate_book(source, work_root=work_root)

    assert model_calls == 0
    assert checkpoint.read_bytes() == b'{"http_attempts":23}'
    assert dangling.is_symlink()
    assert {path.name for path in source_root.iterdir() if path.name != ".store.lock"} == {"missing-run", "usage.json"}


def test_translate_command_resumes_frozen_bookplan_without_reentering_p1_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"frozen source")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    work_root = tmp_path / "work"
    work_dir = work_root / source_hash / "legacy-run"
    work_dir.mkdir(parents=True)
    (work_dir / "bookplan.json").write_text("{}")
    provider_calls = 0

    class ForbiddenModel:
        async def ainvoke(self, *_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            raise AssertionError("provider must not be called while routing the frozen BookPlan")

    async def forbidden_preparation(*_args, **_kwargs):
        raise AssertionError("a frozen BookPlan must not re-enter P1")

    async def resume(actual_work_dir, *_args, **_kwargs):
        assert actual_work_dir == work_dir
        return cli.RunOutcome("paused", actual_work_dir, "translation")

    monkeypatch.setattr(cli, "_existing_run_id", lambda *_args, **_kwargs: "legacy-run")
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: ForbiddenModel())
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())
    monkeypatch.setattr(cli, "prepare_translation", forbidden_preparation)
    monkeypatch.setattr(cli, "_advance_work_dir", resume)

    result = cli.translate_book(source, work_root=work_root)

    assert result.work_dir == work_dir
    assert result.phase == "translation"
    assert provider_calls == 0


def test_translate_refuses_to_restart_paid_work_with_changed_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    work_root = tmp_path / "work"
    checker = StubChecker()
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: checker)

    async def advance(actual_source, output, actual_work_root, config, actual_checker, **_kwargs):
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    cli.translate_book(source, work_root=work_root)

    with pytest.raises(ValueError, match="different frozen configuration"):
        cli.translate_book(source, work_root=work_root, context_tokens=8192)


def test_translate_refuses_changed_user_terms_without_repeating_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    terms = tmp_path / "terms.json"
    terms.write_text('{"data":"数据"}')
    checker = StubChecker()
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: checker)

    async def advance(actual_source, output, actual_work_root, config, actual_checker, **_kwargs):
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    cli.translate_book(source, work_root=tmp_path / "work", glossary=terms)
    terms.write_text('{"data":"资料"}')

    with pytest.raises(ValueError, match="different frozen configuration"):
        cli.translate_book(source, work_root=tmp_path / "work", glossary=terms)


def test_same_source_execution_lock_prevents_concurrent_translate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"fixture")
    source_root = tmp_path / "work" / hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())

    with AtomicStore(source_root).lock(blocking=False), pytest.raises(StoreLocked):
        cli.translate_book(source, work_root=tmp_path / "work")


def test_resume_and_translate_share_the_source_execution_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work_dir = tmp_path / "source-hash" / "run-id"
    work_dir.mkdir(parents=True)
    (work_dir / "source.epub").write_bytes(b"fixture")
    preparation = SimpleNamespace(
        run_id=work_dir.name,
        source_hash=work_dir.parent.name,
        source_path="source.epub",
        extraction_config={"provider": "agnes", "model": "frozen-model", "max_output_tokens": 128},
    )
    monkeypatch.setattr(cli, "RunStore", lambda *_: SimpleNamespace(read_preparation=lambda: preparation))
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id="frozen-model"))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())

    with AtomicStore(work_dir.parent).lock(blocking=False), pytest.raises(StoreLocked):
        cli.resume_book(work_dir, output=tmp_path / "target.epub")


def test_translate_refuses_ambiguous_compatible_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    work_root = tmp_path / "work"
    checker = StubChecker()
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: checker)
    saved_config = None

    async def advance(actual_source, output, actual_work_root, config, actual_checker, **_kwargs):
        nonlocal saved_config
        saved_config = config
        prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
        return cli.RunOutcome("paused", prepared.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    cli.translate_book(source, work_root=work_root)
    assert saved_config is not None
    from dataclasses import replace

    prepare_book(source, work_root, replace(saved_config, run_id="another-run"), checker)
    with pytest.raises(ValueError, match="multiple matching runs"):
        cli.translate_book(source, work_root=work_root)


def test_translate_ignores_preparation_without_a_committed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    work_root = tmp_path / "work"
    abandoned = work_root / hashlib.sha256(source.read_bytes()).hexdigest() / "interrupted-p1"
    abandoned.mkdir(parents=True)
    (abandoned / "source.epub").write_bytes(source.read_bytes())
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())
    seen: list[str | None] = []

    async def advance(_source, _output, _work_root, config, _checker, **_kwargs):
        seen.append(config.run_id)
        return cli.RunOutcome("paused", abandoned, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    cli.translate_book(source, work_root=work_root)
    assert len(seen) == 1 and seen[0] is not None and seen[0] != abandoned.name


def test_resume_uses_frozen_model_and_snapshot_without_user_term_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "run"
    work_dir.mkdir()
    (work_dir / "source.epub").write_bytes(b"fixture")
    preparation = SimpleNamespace(
        run_id=work_dir.name,
        source_hash=work_dir.parent.name,
        source_path="source.epub",
        extraction_config={"provider": "agnes", "model": "frozen-model", "max_output_tokens": 128},
    )
    monkeypatch.setattr(cli, "RunStore", lambda *_: SimpleNamespace(read_preparation=lambda: preparation))
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id="frozen-model"))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: object())
    called = []

    async def advance(actual_work_dir, *_args, **_kwargs):
        called.append(actual_work_dir)
        return cli.RunOutcome("paused", actual_work_dir, "resolution")

    monkeypatch.setattr(cli, "_advance_work_dir", advance)
    result = cli.resume_book(work_dir, output=tmp_path / "target.epub")

    assert result.status == "paused"
    assert called == [work_dir.resolve()]


def test_outcome_report_is_derived_from_the_same_work_directory(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli, "RunStore", lambda root: SimpleNamespace(root=root))

    def fake_report(store, **fields):
        calls.append((store.root, fields))
        path = store.root / "report.json"
        path.write_text('{"http":{"actual_attempts":4}}')
        return path

    monkeypatch.setattr(cli, "write_report", fake_report)
    result = cli._record(cli.RunOutcome("needs_attention", tmp_path, "translation"))

    assert result.report_path == tmp_path / "report.json"
    assert result.http_attempts == 4
    assert calls[0][0] == tmp_path
    assert calls[0][1]["status"] == "needs_attention"


def test_bad_manual_target_cannot_grant_run_budget(tmp_path: Path) -> None:
    store, _, _ = ready_store(tmp_path)
    limits_path = store._path("checks", "run-limits")
    with pytest.raises(ValueError, match="unknown retry Unit"):
        cli._authorize_resume_actions(
            store,
            retry_units=("ghost",),
            add_unit_http=6,
            add_run_http=100,
            retry_checks=(),
            add_check_http=0,
            repair_file=None,
            authorization_id="invalid-unit",
        )
    assert not limits_path.exists()


def test_stale_repair_cannot_grant_run_budget(tmp_path: Path) -> None:
    store, unit, _ = ready_store(tmp_path)
    repair = tmp_path / "stale.json"
    repair.write_text('{"unit_id":"' + unit.unit_id + '","base_revision":5,"plan_epoch":0,"target":"中文"}')
    with pytest.raises(ValueError, match="stale"):
        cli._authorize_resume_actions(
            store,
            retry_units=(),
            add_unit_http=0,
            add_run_http=100,
            retry_checks=(),
            add_check_http=0,
            repair_file=repair,
            authorization_id="invalid-repair",
        )
    assert not store._path("checks", "run-limits").exists()


def test_same_manual_authorization_replay_does_not_add_budget_twice(tmp_path: Path) -> None:
    store, _, _ = ready_store(tmp_path)

    def authorize(amount: int) -> None:
        cli._authorize_resume_actions(
            store,
            retry_units=(),
            add_unit_http=0,
            add_run_http=amount,
            retry_checks=(),
            add_check_http=0,
            repair_file=None,
            authorization_id="approval-1",
        )

    authorize(6)
    authorize(6)
    assert load_budget_overrides(store)["add_run_http"] == 6
    with pytest.raises(ValueError, match="different resume action"):
        authorize(7)
    assert load_budget_overrides(store)["add_run_http"] == 6


def test_same_retry_authorization_does_not_add_logical_grant_twice(tmp_path: Path) -> None:
    store, unit, initial = ready_store(tmp_path)
    item_id = next(iter(initial.items))
    item = initial.items[item_id].model_copy(
        update={
            "status": ItemStatus.NEEDS_ATTENTION,
            "failure": {"stage": "translate", "code": "request_failed", "message": "manual retry"},
        }
    )
    store.save_unit(
        initial.model_copy(update={"record_version": initial.record_version + 1, "items": {item_id: item}}),
        expected_record_version=initial.record_version,
    )

    for _ in range(2):
        cli._authorize_resume_actions(
            store,
            retry_units=(unit.unit_id,),
            add_unit_http=0,
            add_run_http=0,
            retry_checks=(),
            add_check_http=0,
            repair_file=None,
            authorization_id="same-retry",
        )

    record = store.read_unit(unit.unit_id)
    assert record.counters[f"explicit_retry_translate:{item_id}"] == 1


def test_only_known_empty_term_freeze_can_be_superseded(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "bookplan.json").touch()
    old_extraction = {"prompt_version": "epubox-v25-2", "model": "same"}
    old_translation = {"prompt_version": "epubox-v25-2", "model": "same"}
    preparation = SimpleNamespace(
        extraction_config=old_extraction,
        translation_config=old_translation,
        user_terms=(),
        user_terms_hash="terms-hash",
        document_hashes={},
        unit_documents={},
    )
    unit = SimpleNamespace(accepted_revision=None)
    store = SimpleNamespace(
        root=tmp_path,
        read_bookplan=lambda: SimpleNamespace(unit_ids=("u1",)),
        read_glossary=lambda: SimpleNamespace(terms=(), extraction_status="closed_with_gaps"),
        read_candidate_pool=lambda: SimpleNamespace(candidates=()),
        read_unit=lambda _id: unit,
        read_term_plan=lambda: SimpleNamespace(items=(SimpleNamespace(item_id="te1"),)),
        read_extraction=lambda _id: SimpleNamespace(
            status="succeeded_with_rejections",
            candidates=(),
            diagnostics=({"reason": "candidate 0: evidence must contain 1 to 64 citations"},),
        ),
    )
    monkeypatch.setattr(cli, "load_user_terms", lambda *_args, **_kwargs: ((), "terms-hash"))
    desired_extraction = old_extraction | {"prompt_version": "epubox-v25-3"}
    desired_translation = old_translation

    assert cli._superseded_empty_term_run(
        cast(Any, store), preparation, desired_extraction, desired_translation, cli.PreparationConfig()
    )
    unit.accepted_revision = 0
    assert not cli._superseded_empty_term_run(
        cast(Any, store), preparation, desired_extraction, desired_translation, cli.PreparationConfig()
    )


def test_empty_term_repair_requires_opt_in_and_then_resumes_from_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_hash = "a" * 64
    source_root = tmp_path / source_hash
    old_run = source_root / "old-run"
    old_run.mkdir(parents=True)
    (old_run / "preparation.json").write_text("{}")
    (old_run / "bookplan.json").write_text("{}")
    preparation = SimpleNamespace(
        source_hash=source_hash,
        run_id="old-run",
        extraction_config={"prompt_version": "epubox-v25-2"},
        translation_config={"prompt_version": "epubox-v25-2"},
        user_terms=(),
        user_terms_hash="terms-hash",
        document_hashes={},
        unit_documents={},
    )
    store = SimpleNamespace(
        root=old_run,
        read_preparation=lambda: preparation,
        read_glossary=lambda: {"terms": []},
        _trusted_preparation_documents=lambda: None,
    )
    config = cli.PreparationConfig()
    monkeypatch.setattr(cli, "RunStore", lambda _path: store)
    monkeypatch.setattr(cli, "load_user_terms", lambda *_args, **_kwargs: ((), "terms-hash"))
    monkeypatch.setattr(cli, "_frozen_extraction_config", lambda _config: {"prompt_version": "epubox-v25-3"})
    monkeypatch.setattr(cli, "_frozen_translation_config", lambda _config: {"prompt_version": "epubox-v25-2"})
    monkeypatch.setattr(cli, "_superseded_empty_term_run", lambda *_args: True)

    with pytest.raises(ValueError, match="--repair-terms"):
        cli._existing_run_id(source_root, source_hash, config)
    assert not (source_root / "term-repair.json").exists()

    replacement = cli._existing_run_id(source_root, source_hash, config, repair_terms=True)
    marker = cli.strict_json_loads((source_root / "term-repair.json").read_bytes())
    assert isinstance(marker, dict)
    assert marker["old_run_id"] == "old-run"
    assert marker["replacement_run_id"] == replacement
    assert cli._existing_run_id(source_root, source_hash, config) == replacement


def test_valid_v25_2_bookplan_resumes_with_v25_3_term_prompt_without_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_hash = "b" * 64
    source_root = tmp_path / source_hash
    old_run = source_root / "old-run"
    old_run.mkdir(parents=True)
    (old_run / "preparation.json").write_text("{}")
    (old_run / "bookplan.json").write_text("{}")
    old_extraction = {"prompt_version": "epubox-v25-2", "auto_extract": False, "model": "same"}
    expected_extraction = old_extraction | {"prompt_version": "epubox-v25-3"}
    translation = {"prompt_version": "epubox-v25-2", "model": "same"}
    preparation = SimpleNamespace(
        source_hash=source_hash,
        run_id="old-run",
        extraction_config=old_extraction,
        translation_config=translation,
        user_terms=("useful-term",),
        user_terms_hash="terms-hash",
        document_hashes={},
        unit_documents={},
    )
    store = SimpleNamespace(
        root=old_run,
        read_preparation=lambda: preparation,
        read_bookplan=lambda: SimpleNamespace(unit_ids=()),
        read_glossary=lambda: SimpleNamespace(terms=("useful-term",), extraction_status="closed"),
        _trusted_preparation_documents=lambda: None,
    )
    monkeypatch.setattr(cli, "RunStore", lambda _path: store)
    monkeypatch.setattr(cli, "_frozen_extraction_config", lambda _config: expected_extraction)
    monkeypatch.setattr(cli, "_frozen_translation_config", lambda _config: translation)
    monkeypatch.setattr(cli, "load_user_terms", lambda *_args, **_kwargs: (("useful-term",), "terms-hash"))

    assert cli._existing_run_id(source_root, source_hash, cli.PreparationConfig()) == "old-run"


def test_existing_run_without_resolution_protocol_resumes_as_v1() -> None:
    actual = {"prompt_version": "epubox-v25-3", "model": "same"}
    expected = actual | {"resolution_protocol_version": "epubox-term-resolution-2"}
    translation = {"prompt_version": "epubox-v25-2", "model": "same"}

    assert cli._legacy_resolution_protocol_run(actual, expected, translation, translation)


def test_existing_v1_resolution_run_is_selected_by_the_same_translate_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_hash = "c" * 64
    source_root = tmp_path / source_hash
    run_dir = source_root / "v1-run"
    run_dir.mkdir(parents=True)
    (run_dir / "preparation.json").write_text("{}")
    actual_extraction = {"prompt_version": "epubox-v25-3", "model": "same"}
    expected_extraction = actual_extraction | {"resolution_protocol_version": "epubox-term-resolution-2"}
    translation = {"prompt_version": "epubox-v25-2", "model": "same"}
    preparation = SimpleNamespace(
        source_hash=source_hash,
        run_id="v1-run",
        extraction_config=actual_extraction,
        translation_config=translation,
        user_terms=(),
        user_terms_hash="terms-hash",
        document_hashes={},
        unit_documents={},
    )
    store = SimpleNamespace(
        root=run_dir,
        read_preparation=lambda: preparation,
        _trusted_preparation_documents=lambda: None,
    )
    monkeypatch.setattr(cli, "RunStore", lambda _path: store)
    monkeypatch.setattr(cli, "_frozen_extraction_config", lambda _config: expected_extraction)
    monkeypatch.setattr(cli, "_frozen_translation_config", lambda _config: translation)
    monkeypatch.setattr(cli, "load_user_terms", lambda *_args, **_kwargs: ((), "terms-hash"))

    assert cli._existing_run_id(source_root, source_hash, cli.PreparationConfig()) == "v1-run"
