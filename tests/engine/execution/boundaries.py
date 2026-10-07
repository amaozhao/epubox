import asyncio
import json
from collections import Counter
from zipfile import ZipFile

import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine.agents.runtime import model_input_budget, request_messages
from engine.cli import _write_source_hint
from engine.epub.fill import fill_resource
from engine.epub.publish import publish_atomic
from engine.item.atoms import extract_resource
from engine.item.members import validate_member_target
from engine.orchestrator import run_translation
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from tests.engine.agents.workflow import prepare_case
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer

BODY = '<p>The <code>uv</code> package manager<span id="index-one"></span> is used to manage dependencies and virtual environments.<span></span> It <span id="index-two"></span>functions similarly to pip but is faster due to its Rust implementation and efficient dependency resolution.</p>'
SOURCE = "⟦+b1⟧The ⟦-b1⟧⟦=x1⟧⟦+b2⟧ package manager⟦-b2⟧⟦=x2⟧⟦+b3⟧ is used to manage dependencies and virtual environments.⟦+g1⟧⟦-g1⟧ It ⟦-b3⟧⟦=x3⟧⟦+b4⟧functions similarly to pip but is faster due to its Rust implementation and efficient dependency resolution.⟦-b4⟧"
BAD = "⟦+b1⟧⟦-b1⟧⟦=x1⟧uv⟦+b2⟧ 包管理器⟦-b2⟧⟦=x2⟧⟦+b3⟧ 用于管理依赖项和虚拟环境。⟦+g1⟧⟦-g1⟧ 它⟦-b3⟧⟦=x3⟧⟦+b4⟧ 的功能类似于 pip，但由于其 Rust 实现和高效的依赖解析机制，速度更快。⟦-b4⟧"
GOOD = BAD.replace("⟦+b1⟧⟦-b1⟧", "⟦+b1⟧该⟦-b1⟧").replace("⟦=x1⟧uv", "⟦=x1⟧")


@pytest.mark.parametrize("succeeds", (True, False))
def test_boundary_failure_retries_without_weakening_fill_validation(tmp_path, succeeds):
    case = prepare_case(tmp_path, BODY, (SOURCE,))
    item = case.batch.items[0]
    with pytest.raises(ValueError, match="text moved across"):
        validate_member_target(item, BAD)
    validate_member_target(item, GOOD)
    physical = request_messages("translate", case.batch.payload, compact=True)
    assert "never duplicate its hinted content" in physical[0]["content"]
    assert "keep originally nonempty ranges nonempty" in physical[0]["content"]
    assert (
        model_input_budget("translate", case.batch.payload, compact=True)["cl100k_tokens"]
        < model_input_budget("translate", case.batch.payload)["cl100k_tokens"]
    )
    calls = Counter()
    events = []

    async def transport(kind, payload):
        response = answer(kind, payload)
        if kind == "translate":
            result = json.loads(response["raw"])
            for translated in result["items"]:
                calls[translated["item_id"]] += 1
                if translated["item_id"] == item.item_id:
                    translated["target"] = GOOD if succeeds and calls[item.item_id] > 1 else BAD
            response["raw"] = json.dumps(result)
        return response

    result = asyncio.run(run_translation(case.session.store.root, transport=transport, progress=events.append))
    assert result.status == ("translated" if succeeds else "needs_attention")
    assert calls[item.item_id] == (2 if succeeds else 3)
    assert all(count == 1 for key, count in calls.items() if key != item.item_id)
    assert all(event.get("phase") != "waiting" for event in events)
    _write_source_hint(
        case.session.store.root, tmp_path / "source.epub", case.prepared.plan.source_hash, case.prepared.plan.run_id
    )
    output = tmp_path / "source-cn.epub"
    if not succeeds:
        with pytest.raises(IdentityMismatch, match="incomplete"):
            publish_atomic(case.session.store, output, StubChecker())
        assert not output.exists()
        return
    assert BodyJournal(case.session.store).records()[item.item_id].target_projection == GOOD
    publish_atomic(case.session.store, output, StubChecker())
    with ZipFile(output) as archive:
        root = etree.fromstring(archive.read("OEBPS/chapter.xhtml"))
    assert root.xpath("//*[local-name()='code']/text()") == ["uv"]
    assert "".join(root.itertext()).count("uv") == 1
    assert root.xpath("//*[@id='index-one']") and root.xpath("//*[@id='index-two']")


def test_code_and_pre_bytes_remain_local_and_are_restored_unchanged():
    raw = b'<html xmlns="http://www.w3.org/1999/xhtml"><head/><body><p>Use <code data-kind="sample">INLINE_CODE_SECRET</code> now.</p><pre class="code">if x &lt; 3:\n    print("PRE_CODE_SECRET")</pre></body></html>'
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    assert len(inventory.items) == 1
    item = inventory.items[0]
    assert "INLINE_CODE_SECRET" not in item.source_projection and "PRE_CODE_SECRET" not in item.source_projection
    target = item.source_projection.replace("Use ", "使用 ").replace(" now.", " 即可。")
    result = fill_resource(raw, inventory, {item.item_id: target})
    assert b'<code data-kind="sample">INLINE_CODE_SECRET</code>' in result
    assert b'<pre class="code">if x &lt; 3:\n    print("PRE_CODE_SECRET")</pre>' in result


@pytest.mark.parametrize("embedded", (False, True))
def test_nested_pre_code_is_protected_at_the_outermost_node_only(embedded):
    block = '<pre class="sample"><code class="python">def example():\n    return 1</code></pre>'
    body = "<p>Before " + block + " after.</p>" if embedded else "<p>Before.</p>" + block + "<p>After.</p>"
    raw = ('<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>' + body + "</body></html>").encode()
    inventory = extract_resource(raw, "OPS/chapter.xhtml", "book")
    protected = [
        entry for item in inventory.items for entry in item.registry.values() if entry.boundary_type == "code"
    ]
    assert len(protected) == (1 if embedded else 0)
    assert all(entry.hints["element"] == "pre" for entry in protected)
    assert all("def example" not in item.source_projection for item in inventory.items)
    targets = {
        item.item_id: item.source_projection.replace("Before", "之前")
        .replace("after", "之后")
        .replace("After", "之后")
        for item in inventory.items
    }
    rendered = fill_resource(raw, inventory, targets)
    assert rendered.count(block.encode()) == 1


def test_missing_translation_id_is_retried_without_guessing_or_retranslating_siblings(tmp_path):
    case = prepare_case(tmp_path, "<p>First.</p><p>Second.</p>", ("First.", "Second."))
    broken = case.batch.items[0].item_id
    counts = Counter()

    async def transport(kind, payload):
        response = answer(kind, payload)
        if kind == "translate":
            data = json.loads(response["raw"])
            for item in data["items"]:
                counts[item["item_id"]] += 1
                if item["item_id"] == broken and counts[broken] == 1:
                    item["item_id"] += "2"
            response["raw"] = json.dumps(data)
        return response

    result = asyncio.run(run_translation(case.session.store.root, transport=transport))
    assert result.status == "translated" and counts[broken] == 2
    assert counts[case.batch.items[1].item_id] == 1
    records = BodyJournal(case.session.store).records()
    assert broken + "2" not in records
