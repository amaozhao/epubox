"""Replay the real book's empty index-anchor slots without changing its checkpoint."""

import asyncio
import json
from pathlib import Path

import pytest
from lxml import etree  # type: ignore[attr-defined]

from engine.agents import wire
from engine.epub.assembly import assemble_document
from engine.epub.preparation import PreparationConfig
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.item.inline import plain_text
from engine.orchestrator import run_translation
from engine.services.journal import BodyJournal
from engine.services.preparation import prepare_translation
from engine.services.store import RunStore
from tests.engine.agents.workflow import MODEL
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.execution.atomic import answer

TRANSLATIONS = (
    (
        "除了安全性，另一个运维方面的考量是",
        "模型选择",
        "。",
        "Backstage 插件支持的模型",
        "来自",
        "Anthropic",
        "、",
        "OpenAI",
        "和",
        "Amazon Bedrock",
        "。",
        "模型选择不仅影响成本，还影响",
        "性能",
        "和能力。",
        "例如，较小且速度更快的模型",
        "如",
        "Haiku",
        "来自 Anthropic，可能足以胜任简单的聊天助手，而功能更强大的模型",
        "如",
        "Opus",
        "可能适合复杂的故障排查任务。",
        "我们必须仔细权衡模型成本与处理所需复杂度的能力。根据我们的经验，为不同任务分层使用不同智能体的效果最好。",
    ),
    (
        "我们还着重衡量",
        "关键绩效指标",
        "（",
        "KPI",
        "），这些指标与",
        "开发者生产力有关，",
        "例如",
        "上市时间",
        "和",
        "平均恢复时间（MTTR）",
        "。",
        "通过",
        "分析数据，我们可以判断人工智能功能是否确实缩短了开发者从构想到在生产环境运行服务所需的时间。",
        "例如，我们可以跟踪开发者创建新服务所需的时间，并观察引入人工智能创建功能后是否有所减少。",
    ),
)


@pytest.mark.parametrize("defect", ("empty", "missing", "duplicate"))
def test_real_anchor_slots_repair_with_diagnostics_and_resume(tmp_path, defect):
    body = Path(__file__).with_name("anchors.xhtml").read_text()
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": body})
    prepared = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            PreparationConfig(
                run_id="repair",
                auto_extract=False,
                adapter_version=ADAPTER_VERSION,
                extractor_version=EXTRACTOR_VERSION,
                translation_config={
                    "model": MODEL,
                    "max_source_tokens": 5000,
                    "max_input_tokens": 50000,
                    "max_output_tokens": 10000,
                    "context_tokens": 60000,
                    "output_budget_version": 5,
                },
            ),
            StubChecker(),
        )
    )
    store = RunStore(prepared.work_dir)
    journal = BodyJournal(store)
    members = [member for member in journal.session.index.members if member.channel == "body"]
    assert len(members) == 2
    slots = {
        member.item_id: {str(i): text for i, text in enumerate(TRANSLATIONS[n], 1)} for n, member in enumerate(members)
    }
    assert [len(value) for value in slots.values()] == [22, 14]
    failed_once = set()
    repairs = []

    async def transport(kind, payload):
        if kind == "review":
            return answer(kind, payload)
        physical = json.loads(wire.messages(kind, payload, "base")[1]["content"])
        values = []
        for item in physical["items"]:
            member_id = payload["items"][int(item["item_id"]) - 1]["item_id"]
            target = dict(slots.get(member_id, {key: "译文。" for key in item["slot_ids"]}))
            value = {"item_id": item["item_id"], "target": target}
            if member_id in slots and member_id not in failed_once:
                failed_once.add(member_id)
                if defect == "empty":
                    target["5" if len(target) == 22 else "7"] = " "
                elif defect == "missing":
                    del target[str(len(target))]
                else:
                    values.append(value)
            elif member_id in slots:
                assert {entry["item_id"] for entry in payload["items"]} == set(slots)
                repairs.append(item["repair"])
            values.append(value)
        raw = json.dumps({"protocol": payload["protocol"], "request_id": payload["request_id"], "items": values})
        canonical = wire.decode(
            kind,
            raw,
            payload["request_id"],
            [item["item_id"] for item in payload["items"]],
            version=wire.VERSION,
            sources={item["item_id"]: item["source"] for item in payload["items"]},
        )
        return {"raw": canonical, "finish_reason": "stop"}

    result = asyncio.run(run_translation(store.root, transport=transport))
    assert result.status == "translated", result.reason
    assert len(repairs) == 2
    assert all(
        ("invalid=" if defect == "empty" else "missing=" if defect == "missing" else "duplicate item_id") in value
        for value in repairs
    )
    recovered = BodyJournal(store)
    inventory = next(
        value
        for value in recovered.session.index.inventories
        if value.document.resource.path.endswith("chapter.xhtml")
    )
    filled = assemble_document(inventory.document, recovered.parent_targets())
    original = etree.fromstring(body.encode())
    translated = etree.fromstring(filled.markup.encode())
    assert [(node.tag, dict(node.attrib)) for node in original.iter()] == [
        (node.tag, dict(node.attrib)) for node in translated.iter()
    ]
    texts = []
    for record in recovered.records(tuple(member.item_id for member in members)).values():
        assert record.target_projection is not None
        texts.append(plain_text(record.target_projection))
    assert "Backstage 插件支持的模型 来自 Anthropic" in texts[0]
    assert "有关， 例如" in texts[1]

    async def no_dispatch(kind, payload):
        pytest.fail("accepted records must not be sent again")

    resumed = asyncio.run(run_translation(store.root, transport=no_dispatch))
    assert resumed.status == "translated" and resumed.http_attempts == result.http_attempts
