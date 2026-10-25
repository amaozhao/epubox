from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass

import pytest

from engine.agents.workflow import run_workflow
from engine.epub.fill import fill_resource
from engine.item.atoms import extract_resource
from engine.schemas.contracts import ItemStatus
from tests.engine.agents.test_workflow import ReadyCase, prepare_case, review_item, runtime

XHTML = "http://www.w3.org/1999/xhtml"
CODE = 'OutPoint::<span style="color:#795da3">new</span>("quoted\tvalue")<!-- exact code comment -->'
BODY = f'<p>Explain <span style="font-family:monospace">{CODE}</span> clearly.</p>'
ATOM = re.compile(r"⟦=x[^⟧]+⟧")


@dataclass(frozen=True)
class CodeCase:
    ready: ReadyCase
    raw: bytes


@pytest.fixture(scope="module")
def code_case(tmp_path_factory: pytest.TempPathFactory) -> CodeCase:
    root = tmp_path_factory.mktemp("code-workflow")
    preview = (f'<html xmlns="{XHTML}"><head><title>Chapter</title></head><body>{BODY}</body></html>').encode()
    extracted = extract_resource(preview, "OEBPS/chapter.xhtml", "preview")
    projection = next(item.source_projection for item in extracted.items if item.kind == "p")
    ready = prepare_case(root, BODY, (projection,))
    inventory = next(
        value for value in ready.index.inventories if value.document.resource.path.endswith("chapter.xhtml")
    )
    with zipfile.ZipFile(ready.session.store.root / "source.epub") as archive:
        raw = archive.read(inventory.document.resource.path)
    return CodeCase(ready, raw)


@pytest.mark.asyncio
async def test_code_atom_is_opaque_to_translation_and_review_but_fills_source_exactly(code_case: CodeCase) -> None:
    case = code_case.ready
    calls: list[str] = []

    async def transport(kind, payload):
        calls.append(kind)
        item = payload["items"][0]
        if kind == "translate":
            assert "OutPoint" not in item["source"]
            assert ATOM.search(item["source"])
            target = item["source"].replace("Explain ", "说明").replace(" clearly.", "。")
            items = [{"item_id": item["item_id"], "target": target}]
            protocol = "epubox-text-1"
        else:
            assert "OutPoint" not in item["source"] and "OutPoint" not in item["target"]
            assert ATOM.search(item["target"])
            target = item["target"].replace("说明", "请说明")
            items = [review_item(item, decision="replace", target=target)]
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    result = await run_workflow(
        case.prepared,
        case.batch,
        case.index,
        runtime(transport),
        session=case.session,
        save=lambda _record: None,
    )

    member = case.batch.items[0]
    assert result.status == "completed" and calls == ["translate", "review"]
    assert result.results[member.item_id].target_projection is not None
    inventory = next(
        value for value in case.index.inventories if value.document.resource.path.endswith("chapter.xhtml")
    )
    targets = {item.item_id: item.source_projection for item in inventory.items}
    targets[member.item_id] = result.results[member.item_id].target_projection or ""
    filled = fill_resource(code_case.raw, inventory, targets)
    assert f'<span style="font-family:monospace">{CODE}</span>'.encode() in filled
    assert "请说明".encode() in filled


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["delete", "duplicate", "forge", "leak"])
async def test_translation_cannot_delete_duplicate_forge_or_leak_a_code_atom(code_case: CodeCase, attack: str) -> None:
    case = code_case.ready
    calls: list[str] = []

    async def transport(kind, payload):
        calls.append(kind)
        assert kind == "translate"
        item = payload["items"][0]
        marker = ATOM.search(item["source"])
        assert marker is not None and "OutPoint" not in item["source"]
        target = item["source"].replace("Explain ", "说明").replace(" clearly.", "。")
        if attack == "delete":
            target = target.replace(marker.group(0), "")
        elif attack == "duplicate":
            target = target.replace(marker.group(0), marker.group(0) * 2)
        elif attack == "forge":
            target = target.replace(marker.group(0), "⟦=xforged⟧")
        else:
            target += ' OutPoint::new("quoted\tvalue")'
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-text-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item["item_id"], "target": target}],
                }
            )
        }

    result = await run_workflow(
        case.prepared,
        case.batch,
        case.index,
        runtime(transport),
        session=case.session,
        save=lambda _record: None,
    )

    record = result.results[case.batch.items[0].item_id]
    assert result.status == "needs_attention" and calls == ["translate"]
    assert record.status == ItemStatus.NEEDS_ATTENTION and record.target_projection is None


@pytest.mark.asyncio
async def test_review_cannot_remove_a_code_atom_from_an_accepted_translation(code_case: CodeCase) -> None:
    case = code_case.ready
    calls: list[str] = []

    async def transport(kind, payload):
        calls.append(kind)
        item = payload["items"][0]
        if kind == "translate":
            target = item["source"].replace("Explain ", "说明").replace(" clearly.", "。")
            items = [{"item_id": item["item_id"], "target": target}]
            protocol = "epubox-text-1"
        else:
            marker = ATOM.search(item["target"])
            assert marker is not None
            items = [review_item(item, decision="replace", target=item["target"].replace(marker.group(0), ""))]
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    result = await run_workflow(
        case.prepared,
        case.batch,
        case.index,
        runtime(transport),
        session=case.session,
        save=lambda _record: None,
    )

    record = result.results[case.batch.items[0].item_id]
    assert result.status == "needs_attention" and calls == ["translate", "review"]
    assert record.status == ItemStatus.NEEDS_ATTENTION
    assert record.target_projection is not None and ATOM.search(record.target_projection)
