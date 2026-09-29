import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import cli
from engine.epub.preparation import prepare_book
from engine.services.atomic_store import AtomicStore, StoreLocked
from engine.services.coherence import load_budget_overrides
from tests.v23.book_factory import make_epub
from tests.v25.test_orchestrator import ready_store
from tests.v25.test_preparation_v25 import StubChecker


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
    assert captured["config"].translation_config["target_language"] == "zh-Hans"


def test_repeating_translate_command_reuses_the_same_prepared_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    work_root = tmp_path / "work"
    seen_run_ids: list[str | None] = []
    checker = StubChecker()
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: checker)

    async def advance(actual_source, output, actual_work_root, config, actual_checker, **_kwargs):
        seen_run_ids.append(config.run_id)
        if len(seen_run_ids) == 1:
            prepared = prepare_book(actual_source, actual_work_root, config, actual_checker)
            return cli.RunOutcome("paused", prepared.work_dir, "terms")
        return cli.RunOutcome("paused", first.work_dir, "terms")

    monkeypatch.setattr(cli, "_advance_source", advance)
    first = cli.translate_book(source, work_root=work_root)
    second = cli.translate_book(source, work_root=work_root)

    assert first.work_dir == second.work_dir
    assert seen_run_ids == [None, first.work_dir.name]


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
    assert seen == [None]


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
        return store.root / "report.json"

    monkeypatch.setattr(cli, "write_report", fake_report)
    result = cli._record(cli.RunOutcome("needs_attention", tmp_path, "translation"))

    assert result.report_path == tmp_path / "report.json"
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
