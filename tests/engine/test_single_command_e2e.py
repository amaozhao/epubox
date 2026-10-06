from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import cli
from engine.agents.runtime import PROMPT_VERSION, TERM_PROMPT_VERSION, ProviderError
from engine.epub.preparation import PreparationConfig
from engine.epub.validation import EpubCheckResult
from engine.item.inline import Event, events_to_projection, parse_projection
from engine.item.planner import MAX_SOURCE_TOKENS
from engine.item.unit_planner import PLANNER_VERSION
from engine.orchestrator import run_translation
from engine.services.terms.planning import TERM_PLANNER_VERSION
from tests.engine.epub.factory import make_epub


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
        extraction_config={
            "strategy": TERM_PLANNER_VERSION,
            "prompt_version": TERM_PROMPT_VERSION,
            "provider": "agnes",
            "model": "fake",
            "target_language": "zh-Hans",
        },
        translation_config={
            "target_language": "zh-Hans",
            "provider": "agnes",
            "model": "fake",
            "planner_version": PLANNER_VERSION,
            "prompt_version": PROMPT_VERSION,
            "context_tokens": 8192,
            "max_source_tokens": MAX_SOURCE_TOKENS,
            "max_output_tokens": 4096,
            "run_http_limit": 0,
            "concurrency": 2,
        },
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

    monkeypatch.setattr(cli, "_model_id", lambda *_args: "fake")
    monkeypatch.setattr(
        cli,
        "build_run_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("completed replay must not need credentials")),
    )
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())
    calls_before = list(calls)
    repeated = cli.translate_book(
        source, output=output, work_root=tmp_path / "work", auto_extract=False, context_tokens=8192
    )

    assert repeated.status == "completed"
    assert repeated.work_dir == result.work_dir
    assert repeated.output_sha256 == result.output_sha256
    assert calls == calls_before

    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id="fake"))
    output.unlink()
    republished = cli.translate_book(
        source, output=output, work_root=tmp_path / "work", auto_extract=False, context_tokens=8192
    )
    assert republished.status == "completed" and output.is_file()
    assert republished.work_dir == result.work_dir
    assert calls == calls_before

    output.write_bytes(output.read_bytes() + b"tampered")
    with pytest.raises(FileExistsError, match="output already exists"):
        cli.translate_book(source, output=output, work_root=tmp_path / "work", auto_extract=False, context_tokens=8192)
    assert calls == calls_before


def test_same_translate_command_resumes_without_resending_saved_translations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": "<p>Keep data safe.</p>"})
    output = tmp_path / "translated.epub"
    translated: list[str] = []
    pause_review = True

    async def transport(stage, payload):
        nonlocal pause_review
        if stage == "review" and pause_review:
            pause_review = False
            raise ProviderError("review temporarily unavailable", status_code=401)
        if stage == "translate":
            translated.extend(item["item_id"] for item in payload["items"])
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
                        "terminology": "pass" if item["applicability"]["terminology"] else "not_applicable",
                        "bindings": "pass" if item["applicability"]["bindings"] else "not_applicable",
                        "script": "pass",
                    },
                    "issues": [],
                }
                for item in payload["items"]
            ]
            protocol = "epubox-review-2"
        else:
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
    monkeypatch.setattr(cli, "build_run_model", lambda *_args, **_kwargs: SimpleNamespace(id=cli.settings.AGNES_MODEL))
    monkeypatch.setattr(cli, "checker_for_source", lambda *_args: StubChecker())

    first = cli.translate_book(source, output=output, work_root=tmp_path / "work", auto_extract=False, concurrency=1)
    first_translated = set(translated)
    assert first.status == "paused" and first_translated

    second = cli.translate_book(source, output=output, work_root=tmp_path / "work", auto_extract=False, concurrency=1)

    assert second.status == "completed"
    assert second.work_dir == first.work_dir
    assert not first_translated.intersection(translated[len(first_translated) :])
