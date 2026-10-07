from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine.epub.assembly import assemble_document
from engine.epub.preparation import PreparationConfig
from engine.execution.atomic import _pending_batches, run_atomic
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.item.members import pack_members
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession, limits_for
from engine.services.store import RunStore
from tests.engine.agents.workflow import MODEL
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer

NCX = "http://www.daisy.org/z3986/2005/ncx/"


def _signature(node: etree._Element):
    text = None if node.tag == f"{{{NCX}}}text" else node.text
    return node.tag, tuple(sorted(node.attrib.items())), text, node.tail, tuple(_signature(child) for child in node)


def _book(path: Path) -> Path:
    headings = tuple(f"Heading {number:03d}" for number in range(1, 185))
    chapter = "".join(f'<h1 id="h{number:03d}">{label}</h1>' for number, label in enumerate(headings, 1))
    source = make_epub(path, {"chapter.xhtml": chapter}, version="2.0")
    points = [
        f'<navPoint id="n{number}" playOrder="{number}" class="chapter">'
        f"<navLabel><text>{label}</text></navLabel>"
        f'<content src="chapter.xhtml#h{number:03d}"/></navPoint>'
        for number, label in enumerate(headings, 1)
    ]
    points.extend(
        f'<navPoint id="n{number}" playOrder="{number}" class="appendix">'
        f"<navLabel><text>Independent {number}</text></navLabel>"
        '<content src="chapter.xhtml#h001"/></navPoint>'
        for number in range(185, 199)
    )
    ncx = (
        f'<ncx xmlns="{NCX}" version="2005-1" xml:lang="en">'
        '<head><meta name="dtb:uid" content="fixed"/></head>'
        "<docTitle><text>Independent book title</text></docTitle><navMap>"
        f'<navPoint id="group" playOrder="0" class="group"><navLabel><text>{headings[0]}</text></navLabel>'
        '<content src="chapter.xhtml#h001"/>'
        + "".join(points[1:10])
        + "</navPoint>"
        + "".join(points[10:])
        + "</navMap></ncx>"
    ).encode()
    with ZipFile(source) as archive:
        resources = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    resources["OEBPS/toc.ncx"] = ncx
    with ZipFile(source, "w") as archive:
        for resource, data in resources.items():
            archive.writestr(resource, data)
    return source


@pytest.mark.parametrize("output_version", [5, 6])
def test_ncx_document_batches_only_unmatched_labels_and_refills_text_nodes(
    tmp_path: Path, output_version: int
) -> None:
    source = _book(tmp_path / "source.epub")
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id=f"ncx-{output_version}",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "model": MODEL,
                    "max_source_tokens": 2000,
                    "minimum_source_tokens": 500,
                    "max_input_tokens": 32768,
                    "max_output_tokens": 10000,
                    "context_tokens": 32768,
                    "output_budget_version": output_version,
                },
            ),
            StubChecker(),
        )
    )
    assert result.prepared is not None
    session = ReadySession(RunStore(result.work_dir))
    inventory = next(item for item in session.index.inventories if item.document.resource.path.endswith("toc.ncx"))
    document = inventory.document
    assert len(document.units) == len(inventory.items) == 199

    derived = {item.item_id for item in inventory.items if item.unit_id in session.prepared.plan.derived_sources}
    model_items = {item.item_id for item in inventory.items} - derived
    assert len(derived) == 184
    assert len(model_items) == 15

    production_limits = replace(limits_for(session.prepared.preparation), output_tokens=4096)
    production_plan = pack_members(
        "translate",
        tuple(item for item in session.index.members if item.item_id in model_items),
        session.prepared.glossary,
        session.index,
        production_limits,
        tokenizer_model=MODEL,
        sparse=True,
    )
    assert not production_plan.blocked and len(production_plan.batches) == 1

    journal = BodyJournal(session.store, session=session)
    scheduled = _pending_batches(journal, tuple(session._prepared_batches.values()))
    ncx_batches = [batch for batch in scheduled if set(batch.manifest.item_ids) & model_items]
    assert len(ncx_batches) == 1
    assert set(ncx_batches[0].manifest.item_ids) == model_items
    assert ncx_batches[0].budget.source_tokens < 500

    calls: list[tuple[str, set[str]]] = []

    async def transport(stage, payload):
        ids = {item["item_id"] for item in payload["items"]}
        if ids & model_items:
            calls.append((stage, ids))
        return answer(stage, payload)

    translated = asyncio.run(run_atomic(session.store.root, transport=transport))
    assert translated.status == "translated", translated.reason
    assert calls == [("translate", model_items), ("review", model_items)]

    completed = BodyJournal(session.store)
    records = completed.records(tuple(model_items))
    review_plan = pack_members(
        "review",
        tuple(item for item in session.index.members if item.item_id in model_items),
        session.prepared.glossary,
        session.index,
        production_limits,
        targets=records,
        revisions={record.item_id: 0 for record in records.values()},
        tokenizer_model=MODEL,
        sparse=True,
    )
    assert not review_plan.blocked and len(review_plan.batches) == 1

    targets = completed.parent_targets()
    assembled = assemble_document(document, {unit.unit_id: targets[unit.unit_id] for unit in document.units})
    source_root = etree.fromstring(document.source_markup.encode())
    target_root = etree.fromstring(assembled.markup.encode())
    assert _signature(target_root) == _signature(source_root)
    assert [node.text for node in target_root.iter(f"{{{NCX}}}text")] == ["译文。"] * 199

    before = len(calls)
    resumed = asyncio.run(run_atomic(session.store.root, transport=transport))
    assert resumed.status == "translated"
    assert len(calls) == before
