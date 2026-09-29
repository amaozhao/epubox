from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import cli
from engine.epub.preparation import PreparationConfig
from engine.epub.validation import EpubCheckResult
from engine.item.inline import Event, events_to_projection, parse_projection
from engine.orchestrator import run_translation
from tests.v23.book_factory import make_epub


class StubChecker:
    def check(self, path: Path) -> EpubCheckResult:
        assert path.is_file()
        return EpubCheckResult(("stub-epubcheck",), 0)


def _target(source: str) -> str:
    return events_to_projection(
        tuple(
            Event(kind=event.kind, value="中文内容" + "甲" * min(len(event.value) // 8, 8))
            if event.kind == "text" and event.value.strip()
            else event
            for event in parse_projection(source)
        )
    )


def test_one_pipeline_reaches_verified_epub_with_fake_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<h1>Systems</h1><p>Keep data safe.</p>"})
    output = tmp_path / "translated.epub"
    calls: list[str] = []

    async def transport(stage, payload):
        calls.append(stage)
        if stage == "translate":
            items = [{"item_id": item["item_id"], "target": _target(item["source"])} for item in payload["items"]]
            protocol = "epubox-text-1"
        elif stage == "review":
            items = [
                {
                    "item_id": item["item_id"],
                    "base_revision": item["base_revision"],
                    "decision": "no_change",
                    "checks": {
                        "accuracy": "pass",
                        "fluency": "pass",
                        "terminology": "pass",
                        "bindings": "pass",
                        "script": "pass",
                    },
                    "issues": [],
                }
                for item in payload["items"]
            ]
            protocol = "epubox-review-2"
        else:
            assert stage == "coherence"
            items = [{"item_id": item["item_id"], "unit_ids": [], "issues": []} for item in payload["items"]]
            protocol = "epubox-coherence-1"
        return {
            "raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items}),
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "finish_reason": "stop",
        }

    real_run = run_translation

    async def fake_model_run(work_dir, **_kwargs):
        return await real_run(work_dir, transport=transport)

    monkeypatch.setattr(cli, "run_translation", fake_model_run)
    config = PreparationConfig(
        run_id="single-command",
        auto_extract=False,
        extraction_config={"provider": "agnes", "model": "fake"},
        translation_config={"target_language": "zh-Hans", "model": "fake", "context_tokens": 8192},
    )

    result = asyncio.run(
        cli._advance_source(
            source,
            output,
            tmp_path / "work",
            config,
            StubChecker(),
            model=SimpleNamespace(id="fake"),
            overwrite=False,
        )
    )

    assert result.status == "completed"
    assert output.is_file() and result.output_sha256
    assert result.report_path and result.report_path.is_file()
    report = json.loads(result.report_path.read_text())
    assert report["status"] == "completed"
    assert report["accepted_units"] == report["required_units"]
    assert set(report["coherence_by_document"].values()) == {"valid"}
    assert "translate" in calls and "review" in calls
