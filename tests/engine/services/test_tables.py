import asyncio
import hashlib
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from engine import cli
from engine.item.budget import request_source_tokens
from engine.item.inline import validate_projection
from engine.services import state, tables
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.partition import build_plan
from engine.services.preflight import prepare_preflight, receipt_path
from engine.services.preparation import prepare_translation, resume_preparation
from engine.services.ready import ReadySession, limits_from_config
from engine.services.store import RunStore
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.execution.test_atomic import answer
from tests.engine.services.test_preparing import atomic_config, forbidden


def blocked_checkpoint(tmp_path, monkeypatch):
    source = make_epub(
        tmp_path / "book.epub",
        {
            "chapter.xhtml": "<table><tbody>"
            + "".join(f"<tr><td>{'word ' * 900}</td></tr>" for _ in range(3))
            + "</tbody></table>"
        },
    )
    root = source.with_suffix("")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    state.initialize(root, source, digest, "atomic-run")
    cli._write_source_hint(root, source, digest, "atomic-run")
    with monkeypatch.context() as patch:
        patch.setattr(tables, "split_table", lambda *_args: ())
        result = asyncio.run(
            prepare_translation(
                source,
                root,
                atomic_config(
                    max_source_tokens=1500,
                    source_hard_limit=1500,
                    max_input_tokens=50_000,
                    context_unlimited=True,
                    output_budget_version=7,
                ),
                StubChecker(),
                term_transport=forbidden,
            )
        )
    assert result.phase == "preflight" and result.status == "needs_attention"
    return source, root


def test_failed_table_preflight_upgrades_without_replacing_old_receipt_or_source(tmp_path, monkeypatch):
    source, root = blocked_checkpoint(tmp_path, monkeypatch)
    prior = state.read(root / "checks/preflight.json")
    source_bytes = source.read_bytes()
    preparation = state.read(root / "preparation.json")
    inventories = {path.name: state.read(path) for path in state.glob(root / "inventories", "*.json")}

    result = asyncio.run(resume_preparation(root, StubChecker(), term_transport=forbidden))

    assert result.status == "ready", result.reason
    assert receipt_path(root) == root / "checks/rows.json"
    assert state.read(root / "checks/preflight.json") == prior
    assert state.read(root / "preparation.json") == preparation
    assert {path.name: state.read(path) for path in state.glob(root / "inventories", "*.json")} == inventories
    assert source.read_bytes() == source_bytes
    assert not list(state.glob(root / "requests", "*.json"))
    session = ReadySession(RunStore(root))
    rows = [member for member in session.index.members if member.atomic_tag == "table"]
    assert len(rows) == 3
    assert all(session.index.source_tokens[member.item_id] <= 1500 for member in rows)
    assert asyncio.run(resume_preparation(root, StubChecker(), term_transport=forbidden)).status == "ready"
    preparation_record = RunStore(root).read_preparation()
    assert prepare_preflight(
        RunStore(root), limits_from_config(preparation_record.translation_config, output_unlimited=True), "fake"
    ).passed


def test_table_preflight_upgrade_rejects_existing_dispatched_work(tmp_path, monkeypatch):
    _source, root = blocked_checkpoint(tmp_path, monkeypatch)
    store = RunStore(root)
    preparation = store.read_preparation()
    state.write(root / "requests/saved.json", b"{}")
    prior = state.read(root / "checks/preflight.json")

    with pytest.raises(IdentityMismatch, match="no dispatched work"):
        prepare_preflight(store, limits_from_config(preparation.translation_config, output_unlimited=True), "fake")

    assert not state.exists(root / "checks/rows.json")
    assert state.read(root / "checks/preflight.json") == prior


def test_split_table_translates_reviews_publishes_and_resumes_without_new_requests(tmp_path, monkeypatch):
    source, root = blocked_checkpoint(tmp_path, monkeypatch)
    original = source.read_bytes()
    runtime = BodyJournal.runtime
    calls = []

    async def transport(stage, payload):
        calls.append(stage)
        assert request_source_tokens(payload, "fake") <= 1500
        for item in payload["items"]:
            validate_projection(item["source"])
        return answer(stage, payload)

    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id="fake"))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())
    monkeypatch.setattr(
        BodyJournal,
        "runtime",
        lambda self, model=None, **kwargs: runtime(
            self, model=model, transport=transport, progress=kwargs.get("progress")
        ),
    )
    output = source.with_name("book-cn.epub")
    result = cli.resume_book(root, output=output)

    assert result.status == "completed", result.reason
    assert set(calls) == {"translate", "review"}
    with ZipFile(output) as archive:
        rendered = archive.read("OEBPS/chapter.xhtml")
        assert rendered.count(b"<table>") == 1 and rendered.count(b"<tbody>") == 1
        assert rendered.count(b"<tr>") == 3 and b"word" not in rendered
        assert "译文".encode() in rendered
    count = len(calls)
    assert cli.resume_book(root, output=output).status == "completed"
    assert len(calls) == count
    assert source.read_bytes() == original


def test_row_members_are_not_further_split_into_cells_and_old_json_has_no_padding_fields(tmp_path, monkeypatch):
    _source, root = blocked_checkpoint(tmp_path, monkeypatch)
    assert asyncio.run(resume_preparation(root, StubChecker(), term_transport=forbidden)).status == "ready"
    session = ReadySession(RunStore(root))
    for member in session.index.members:
        if member.atomic_tag == "table":
            assert build_plan(session, member, trigger_stage="translate", trigger_count=10) is None
        else:
            saved = json.loads(member.model_dump_json())
            assert "source_prefix" not in saved and "source_suffix" not in saved
            assert type(member).model_validate(saved) == member


def test_direct_row_members_without_padding_are_not_refined_into_cells(tmp_path):
    source = make_epub(
        tmp_path / "book.epub",
        {"chapter.xhtml": "<table>" + "".join(f"<tr><td>{'word ' * 900}</td></tr>" for _ in range(3)) + "</table>"},
    )
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(max_source_tokens=1500, source_hard_limit=1500, output_budget_version=7),
            StubChecker(),
            term_transport=forbidden,
        )
    )
    assert result.status == "ready"
    session = ReadySession(RunStore(result.work_dir))
    rows = [member for member in session.index.members if member.atomic_tag == "table"]
    assert len(rows) == 3
    for row in rows:
        assert not row.source_prefix and not row.source_suffix
        assert build_plan(session, row, trigger_stage="translate", trigger_count=10) is None
