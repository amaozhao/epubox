from __future__ import annotations

import asyncio
from pathlib import Path
from zipfile import ZipFile

from engine.epub.preparation import PreparationConfig
from engine.execution.atomic import _pending_batches, run_atomic
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.schemas.contracts import canonical_json_bytes
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession
from engine.services.store import RunStore
from tests.engine.agents.workflow import MODEL
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer


def test_first_dispatch_merges_navigation_across_locally_derived_titles(tmp_path: Path):
    source = make_epub(
        tmp_path / "source.epub",
        {"chapter.xhtml": '<p id="left">First.</p><h1 id="match">Matched title</h1><p id="right">Last.</p>'},
    )
    labels = (("left", "Left entry"), ("match", "Matched title"), ("right", "Right entry"))
    with ZipFile(source) as archive:
        resources = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    resources["OEBPS/nav.xhtml"] = (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
        + "".join(f'<li><a href="chapter.xhtml#{anchor}">{label}</a></li>' for anchor, label in labels)
        + "</ol></nav></body></html>"
    ).encode()
    resources["OEBPS/toc.ncx"] = (
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        "<docTitle><text>Book title</text></docTitle><navMap>"
        + "".join(
            f'<navPoint id="n{n}" playOrder="{n}"><navLabel><text>{label}</text></navLabel>'
            f'<content src="chapter.xhtml#{anchor}"/></navPoint>'
            for n, (anchor, label) in enumerate(labels, 1)
        )
        + "</navMap></ncx>"
    ).encode()
    with ZipFile(source, "w") as archive:
        for path, data in resources.items():
            archive.writestr(path, data)
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="navigation",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "model": MODEL,
                    "max_source_tokens": 5000,
                    "max_input_tokens": 50000,
                    "max_output_tokens": 10000,
                    "context_tokens": 60000,
                },
            ),
            StubChecker(),
        )
    )
    assert result.prepared is not None
    session = ReadySession(RunStore(result.work_dir))
    journal = BodyJournal(session.store, session=session)
    initial = tuple(session._prepared_batches.values())
    original_plan = canonical_json_bytes(session.prepared.plan)
    derived = {item.item_id for item in session.index.members if item.unit_id in session.prepared.plan.derived_sources}
    assert len(derived) == 2
    model_nav = {item.item_id for batch in initial if batch.items[0].channel == "navigation" for item in batch.items}
    scheduled = _pending_batches(journal, initial)
    nav_batches = [batch for batch in scheduled if batch.items[0].channel == "navigation"]
    assert len(nav_batches) == 2  # One per file, regardless of the derived gaps.
    assert {item.item_id for batch in nav_batches for item in batch.items} == model_nav
    assert not derived & model_nav
    calls = []

    async def transport(stage, payload):
        ids = {item["item_id"] for item in payload["items"]}
        assert not ids & derived
        if ids & model_nav:
            calls.append((stage, ids))
        return answer(stage, payload)

    translated = asyncio.run(run_atomic(session.store.root, transport=transport))
    assert translated.status == "translated", translated.reason
    assert len(calls) == 4 and sum(stage == "translate" for stage, _ in calls) == 2
    assert canonical_json_bytes(ReadySession(session.store).prepared.plan) == original_plan
    before = len(calls)
    assert asyncio.run(run_atomic(session.store.root, transport=transport)).status == "translated"
    assert len(calls) == before
