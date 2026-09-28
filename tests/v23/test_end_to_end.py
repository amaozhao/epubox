"""Real files and real coordinator, with model/network responses explicitly replaced."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from engine.epub.preparation import PreparationConfig, prepare_book
from engine.item.extractor import EXTRACTOR_VERSION, extract_document
from engine.item.inline import events_to_projection, parse_projection
from engine.item.planner import PlannerConfig, plan_unit
from engine.orchestrator_v23 import TranslationEngine
from engine.schemas.v23 import RunConfig
from engine.services.store import Store
from tests.v23.book_factory import make_epub
from tests.v23.test_package_publish import StubChecker


def prepared(tmp_path: Path, version: str):
    source = make_epub(tmp_path / "source.epub", version=version)
    config = RunConfig(
        model="fake",
        provider="test",
        prompt_version="test-1",
        extractor_version=EXTRACTOR_VERSION,
        run_http_limit=300,
        max_context_tokens=16000,
        max_output_tokens=2048,
    )
    return prepare_book(
        source,
        tmp_path / "work",
        PreparationConfig(config),
        StubChecker(),
        extract_document=extract_document,
        plan_unit=plan_unit,
        planner_config=PlannerConfig(context_tokens=16000, max_output_tokens=2048),
    )


async def model_double(kind, payload):
    response = {"protocol": payload["protocol"], "request_id": payload["request_id"], "items": []}
    for item in payload["items"]:
        if kind == "translate":
            events = [
                event.model_copy(update={"value": "译文"}) if event.kind == "text" and event.value.strip() else event
                for event in parse_projection(item["source"])
            ]
            response["items"].append({"item_id": item["item_id"], "target": events_to_projection(events)})
        elif kind == "review":
            response["items"].append(
                {
                    "item_id": item["item_id"],
                    "base_revision": item["base_revision"],
                    "decision": "no_change",
                    "checks": {key: "pass" for key in ("accuracy", "fluency", "terminology", "bindings", "script")},
                    "issues": [],
                }
            )
        else:
            response["items"].append({"item_id": item["item_id"], "unit_ids": item["unit_ids"], "issues": []})
    return {"raw": json.dumps(response, ensure_ascii=False), "usage": None, "finish_reason": "stop"}


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_json_to_model_double_to_published_epub_and_idempotent_resume(tmp_path: Path, version: str):
    book = prepared(tmp_path, version)
    output = tmp_path / "translated.epub"
    engine = TranslationEngine(Store(book.work_dir), transport=model_double)
    result = asyncio.run(engine.run(output, StubChecker()))
    assert result.status == "completed", (result.model_dump(), (book.work_dir / "report.json").read_text())
    assert output.exists() and result.output_sha256
    assert result.reader_check == {"status": "not_run"}
    original_attempts = engine.http_attempts

    async def no_new_call(*_):
        raise AssertionError("accepted run and publication must resume without a new model request")

    resumed = TranslationEngine(Store(book.work_dir), transport=no_new_call)
    again = asyncio.run(resumed.run(output, StubChecker()))
    assert again.status == "completed"
    assert again.output_sha256 == result.output_sha256
    assert resumed.http_attempts == original_attempts


def test_identity_reloads_only_json_in_a_fresh_process(tmp_path: Path):
    book = prepared(tmp_path, "3.0")
    output = tmp_path / "identity.epub"
    script = """
from pathlib import Path
import sys
from engine.services.store import Store
from engine.epub.publication import publish_book
from tests.v23.test_package_publish import StubChecker
store=Store(sys.argv[1])
book=store.read_bookplan(ready=True)
assert book.required_unit_count > 0
result=publish_book(store, {}, Path(sys.argv[2]), StubChecker(), identity=True)
assert result['sha256']
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(book.work_dir), str(output)],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr
    assert output.exists()
    assert not list((book.work_dir / "requests").glob("*.json"))
