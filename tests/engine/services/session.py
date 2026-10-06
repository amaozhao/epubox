import asyncio
import hashlib
import json
from zipfile import ZipFile

import pytest

from engine.epub.preparation import PreparationConfig
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.services.atomic import IdentityMismatch
from engine.services.preparation import prepare_translation
from engine.services.session import find, remember
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


def prepared(source, root):
    return asyncio.run(
        prepare_translation(
            source,
            root,
            PreparationConfig(
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={"output_budget_version": 3},
            ),
            StubChecker(),
        )
    )


def test_active_book_session_resolves_frozen_snapshot_without_original_repreparation(tmp_path):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    result = prepared(source, source.with_suffix(""))
    remember(source, result.work_dir)

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert find(source, digest) == result.work_dir
    with pytest.raises(IdentityMismatch):
        find(source, "0" * 64)


def test_active_book_session_rechecks_original_instead_of_trusting_caller_hash(tmp_path):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    result = prepared(source, source.with_suffix(""))
    remember(source, result.work_dir)
    stale = hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_bytes(b"changed")
    before = (source.with_suffix("") / "active.json").read_bytes()

    with pytest.raises(IdentityMismatch, match="changed while locating"):
        find(source, stale)
    assert (source.with_suffix("") / "active.json").read_bytes() == before


def test_session_registration_rejects_original_changed_after_snapshot(tmp_path):
    from engine import cli

    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    result = prepared(source, source.with_suffix(""))
    assert result.prepared is not None
    preparation = result.prepared.preparation
    cli._write_source_hint(result.work_dir, source, preparation.source_hash, preparation.run_id)
    source.write_bytes(b"changed")

    with pytest.raises(IdentityMismatch, match="changed before session registration"):
        remember(source, result.work_dir)
    assert not (source.with_suffix("") / "active.json").exists()


def test_active_book_session_rejects_path_escape_before_creating_directories(tmp_path):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    result = prepared(source, source.with_suffix(""))
    remember(source, result.work_dir)
    path = source.with_suffix("") / "active.json"
    value = json.loads(path.read_text())
    value["work_dir"] = "../outside"
    path.write_text(json.dumps(value))
    with pytest.raises(IdentityMismatch):
        find(source, value["original_hash"])
    assert not (tmp_path / "outside").exists()


def test_active_book_session_missing_directory_does_not_create_a_blank_run(tmp_path):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    result = prepared(source, source.with_suffix(""))
    remember(source, result.work_dir)
    path = source.with_suffix("") / "active.json"
    value = json.loads(path.read_text())
    value["work_dir"] = value["snapshot_hash"] + "/missing"
    path.write_text(json.dumps(value))
    with pytest.raises(IdentityMismatch):
        find(source, value["original_hash"])
    assert not (source.with_suffix("") / value["work_dir"]).exists()


def test_original_book_command_reuses_registered_repaired_snapshot(tmp_path, monkeypatch):
    from engine import cli

    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    repaired = source.with_suffix("") / "input" / "source.epub"
    repaired.parent.mkdir(parents=True)
    repaired.write_bytes(source.read_bytes())
    with ZipFile(repaired, "a") as archive:
        archive.comment = b"repaired container"
    result = prepared(repaired, source.with_suffix("") / "current")
    assert result.prepared is not None
    preparation = result.prepared.preparation
    cli._write_source_hint(result.work_dir, repaired, preparation.source_hash, preparation.run_id)
    remember(source, result.work_dir)
    remember(source, result.work_dir)
    hint = json.loads((result.work_dir / "source.json").read_text())
    assert hint["source_hash"] == preparation.source_hash
    assert hint["aliases"] == [
        {
            "original_path": str(source.resolve()),
            "source_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
            "st_dev": source.stat().st_dev,
            "st_ino": source.stat().st_ino,
        }
    ]
    calls = []

    def resume(work_dir, **kwargs):
        calls.append((work_dir, kwargs["output"], kwargs["_automatic"]))
        return cli.RunOutcome("needs_attention", work_dir, "translation")

    monkeypatch.setattr(cli, "resume_book", resume)
    monkeypatch.setattr(cli, "_advance_source", lambda *args, **kwargs: pytest.fail("must not reprepare"))
    outcome = cli.translate_book(source)
    assert outcome.work_dir == result.work_dir
    assert calls == [(result.work_dir, tmp_path / "book-cn.epub", True)]


def test_active_session_rejects_dangling_index_before_creating_any_run(tmp_path):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    root = source.with_suffix("")
    root.mkdir()
    (root / "active.json").symlink_to(root / "missing.json")

    with pytest.raises(IdentityMismatch, match="index.*symbolic link"):
        find(source)
    assert {path.name for path in root.iterdir()} == {"active.json"}


@pytest.mark.parametrize("name", ("preparation.json", "source.epub"))
def test_active_session_rejects_checkpoint_symlinks_before_reading(tmp_path, name):
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    result = prepared(source, source.with_suffix(""))
    remember(source, result.work_dir)
    target = result.work_dir / name
    saved = tmp_path / f"saved-{name}"
    target.rename(saved)
    target.symlink_to(saved)
    before = (source.with_suffix("") / "active.json").read_bytes()

    with pytest.raises(IdentityMismatch, match="symbolic link"):
        find(source)
    assert (source.with_suffix("") / "active.json").read_bytes() == before


def test_mixed_snapshot_find_rejects_source_record_without_original_alias(tmp_path):
    from engine import cli

    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Original.</p>"})
    repaired = source.with_suffix("") / "input" / "source.epub"
    repaired.parent.mkdir(parents=True)
    repaired.write_bytes(source.read_bytes())
    with ZipFile(repaired, "a") as archive:
        archive.comment = b"repaired container"
    result = prepared(repaired, source.with_suffix("") / "current")
    assert result.prepared is not None
    preparation = result.prepared.preparation
    cli._write_source_hint(result.work_dir, repaired, preparation.source_hash, preparation.run_id)
    remember(source, result.work_dir)
    hint_path = result.work_dir / "source.json"
    hint = json.loads(hint_path.read_text())
    hint.pop("aliases")
    hint_path.write_text(json.dumps(hint))
    before = {
        path.relative_to(result.work_dir): path.read_bytes() for path in result.work_dir.rglob("*") if path.is_file()
    }

    with pytest.raises(IdentityMismatch, match="does not protect"):
        find(source)

    after = {
        path.relative_to(result.work_dir): path.read_bytes() for path in result.work_dir.rglob("*") if path.is_file()
    }
    assert after == before
