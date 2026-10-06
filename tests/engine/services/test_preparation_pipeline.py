from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import engine.services.preparation_pipeline as pipeline_module
from engine.agents.runtime import ProviderError
from engine.epub.preparation import PreparationConfig
from engine.schemas.budget import BudgetLimits
from engine.services.preflight import prepare_preflight
from engine.services.preparation_pipeline import PreparationProgress, prepare_translation, resume_preparation
from engine.services.store import RunStore
from engine.services.terms.planning import TERM_PLANNER_VERSION
from engine.services.terms.runner import TermRunner, TermRunResult
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker


@pytest.fixture(autouse=True)
def verified_preflight_for_legacy_pipeline_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """T15 owns real wiring; legacy integration fixtures explicitly prepare T08."""
    original = pipeline_module._p1

    def verified(*args, **kwargs):
        store, preparation, digest = original(*args, **kwargs)
        config = preparation.translation_config

        def configured(name, default):
            value = config.get(name, default)
            assert type(value) is int
            return value

        context = configured("context_tokens", 32768)
        prepare_preflight(
            store,
            limits=BudgetLimits(
                source_tokens=configured("max_source_tokens", 2000),
                input_tokens=configured("max_input_tokens", context),
                output_tokens=configured("max_output_tokens", 4096),
                context_tokens=context,
            ),
            model=str(config.get("model", preparation.extraction_config["model"])),
        )
        return store, preparation, digest

    monkeypatch.setattr(pipeline_module, "_p1", verified)


def config(*, auto_extract: bool = True, user_terms_path: Path | None = None) -> PreparationConfig:
    return PreparationConfig(
        run_id="pipeline-run",
        user_terms_path=user_terms_path,
        auto_extract=auto_extract,
        extraction_config={
            "strategy": TERM_PLANNER_VERSION,
            "prompt_version": "epubox-v25-3",
            "model": "fake",
            "target_language": "zh-Hans",
            "max_primary_chars": 200,
        },
        translation_config={
            "target_language": "zh-Hans",
            "model": "fake",
            "context_tokens": 4096,
            "max_output_tokens": 512,
        },
    )


def source_book(tmp_path: Path, text: str = "Memory allocation is fast.") -> Path:
    return make_epub(tmp_path / "source.epub", {"chapter.xhtml": f"<p>{text}</p>"})


def test_disabled_extraction_reaches_ready_only_after_freeze_and_every_unit(tmp_path: Path) -> None:
    result = asyncio.run(
        prepare_translation(
            source_book(tmp_path),
            tmp_path / "work",
            config(auto_extract=False),
            StubChecker(),
            term_transport=lambda *_: (_ for _ in ()).throw(AssertionError("disabled extraction called model")),
        )
    )

    store = RunStore(result.work_dir)
    plan = store.read_bookplan()
    assert result.status == "ready"
    assert result.phase == "ready"
    assert result.term_status == "disabled"
    assert result.bookplan == plan
    assert store.read_glossary().extraction_status == "disabled"
    assert len(list((result.work_dir / "units").glob("*.json"))) == plan.required_unit_count
    assert (result.work_dir / "glossary" / "freeze.json").is_file()


def test_p4_builds_one_context_index_for_the_complete_unit_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = pipeline_module.build_context_index

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "build_context_index", counted)
    result = asyncio.run(
        prepare_translation(
            source_book(tmp_path),
            tmp_path / "work",
            config(auto_extract=False),
            StubChecker(),
        )
    )
    assert result.status == "ready"
    assert result.bookplan is not None and result.bookplan.required_unit_count > 1
    assert calls == 1


def test_derived_navigation_is_counted_as_planned_not_as_a_local_problem(tmp_path: Path) -> None:
    progress: list[PreparationProgress] = []
    source = make_epub(
        tmp_path / "source.epub",
        {
            "chapter.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 1</title></head>'
            '<body><h1 id="chapter">Chapter 1</h1><p>Memory allocation is fast.</p></body></html>'
        },
    )
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            config(auto_extract=False),
            StubChecker(),
            progress=progress.append,
        )
    )

    assert result.status == "ready"
    assert result.bookplan is not None
    assert any(
        RunStore(result.work_dir).read_unit(unit_id).derived is not None for unit_id in result.bookplan.unit_ids
    )
    p4 = [event for event in progress if event.phase == "p4"][-1]
    assert p4.succeeded == p4.planned and p4.failed == 0


def test_term_and_resolution_transports_feed_one_frozen_glossary(tmp_path: Path) -> None:
    calls: list[str] = []
    progress: list[PreparationProgress] = []

    async def terms(kind, payload):
        calls.append(kind)
        assert not next((tmp_path / "work").rglob("bookplan.json"), None)
        plan_path = next((tmp_path / "work").rglob("glossary/plan.json"))
        store = RunStore(plan_path.parents[1])
        assert len(list((store.root / "glossary" / "extraction").glob("*.json"))) == len(store.read_term_plan().items)
        response_items = []
        for item in payload["items"]:
            view_id, view_text = next(
                ((view_id, text) for view_id, text in item["views"].items() if "Memory" in text),
                next(iter(item["views"].items())),
            )
            candidates = (
                [
                    {
                        "source": "Memory",
                        "target": target,
                        "category": "term",
                        "aliases": [],
                        "scope_hint": "document",
                        "note": "",
                        "evidence": [{"view_id": view_id, "source_quote": view_text}],
                    }
                    for target in ("内存", "记忆")
                ]
                if "Memory" in view_text
                else []
            )
            response_items.append({"item_id": item["item_id"], "candidates": candidates})
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": response_items,
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    async def resolution(kind, payload):
        calls.append(kind)
        assert not next((tmp_path / "work").rglob("bookplan.json"), None)
        group = payload["items"][0]
        chosen = next(candidate["candidate_id"] for candidate in group["candidates"] if candidate["target"] == "内存")
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-term-resolution-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "group_id": group["group_id"],
                            "decision": "select",
                            "selected_candidate_ids": [chosen],
                            "reason": "Technical memory sense.",
                        }
                    ],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(
        prepare_translation(
            source_book(tmp_path),
            tmp_path / "work",
            config(),
            StubChecker(),
            term_transport=terms,
            resolution_transport=resolution,
            progress=progress.append,
        )
    )

    glossary = RunStore(result.work_dir).read_glossary()
    assert result.status == "ready"
    assert calls[-1] == "resolution"
    assert calls.count("resolution") == 1
    assert calls.count("terms") >= 1
    assert [(term.source, term.target) for term in glossary.terms] == [("Memory", "内存")]
    assert RunStore(result.work_dir).read_candidate_pool().extraction_status == "closed"
    assert progress[0].phase == "terms" and progress[0].planned > 0
    assert any(event.phase == "resolution" and event.planned == 1 for event in progress)
    assert progress[-1].phase == "ready" and progress[-1].pending == 0
    assert progress[-1].http_attempts == len(calls)


def test_successful_term_retry_closes_despite_an_earlier_unknown_attempt(tmp_path: Path) -> None:
    calls = 0

    async def terms(_kind, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("provider timed out after dispatch")
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item["item_id"], "candidates": []} for item in payload["items"]],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(
        prepare_translation(source_book(tmp_path), tmp_path / "work", config(), StubChecker(), term_transport=terms)
    )
    store = RunStore(result.work_dir)
    attempts = [
        attempt.state
        for path in (store.root / "requests").glob("*.json")
        for attempt in store.read_request(path.stem).attempts
    ]

    assert "unknown" in attempts and "succeeded" in attempts
    assert result.status == "ready" and result.phase == "ready"
    assert store.read_glossary().extraction_status == "closed"


@pytest.mark.parametrize("failure", ("schema", "source_evidence"))
def test_all_rejected_terms_freeze_with_disclosed_local_gaps(tmp_path: Path, failure: str) -> None:
    calls = 0

    async def bad_terms(_kind, payload):
        nonlocal calls
        calls += 1
        response_items = []
        for item in payload["items"]:
            _view_id, view_text = next(iter(item["views"].items()))
            response_items.append(
                {
                    "item_id": item["item_id"],
                    "candidates": [
                        {
                            "source": view_text.split()[0],
                            "target": "术语",
                            "category": "term",
                            "evidence": []
                            if failure == "schema"
                            else [{"view_id": "sv-unknown", "source_quote": view_text}],
                        }
                    ],
                }
            )
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": response_items,
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = asyncio.run(
        prepare_translation(
            source_book(tmp_path), tmp_path / "work", config(), StubChecker(), term_transport=bad_terms
        )
    )

    assert calls >= 2
    assert result.status == "needs_attention" and result.phase == "ready"
    assert (result.work_dir / "glossary" / "freeze.json").exists()
    assert (result.work_dir / "bookplan.json").exists()
    glossary = RunStore(result.work_dir).read_glossary()
    assert glossary.extraction_status == "closed_with_gaps" and not glossary.terms


def test_paused_terms_leave_no_pool_or_bookplan_and_resume_from_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_book(tmp_path)
    original_run = TermRunner.run

    async def paused(_runner):
        return TermRunResult("paused", 0, 0, 1, 0)

    monkeypatch.setattr(TermRunner, "run", paused)

    first = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            config(),
            StubChecker(),
        )
    )
    assert first.status == "paused" and first.phase == "terms"
    assert not (first.work_dir / "glossary" / "candidates.json").exists()
    assert not (first.work_dir / "bookplan.json").exists()
    monkeypatch.setattr(TermRunner, "run", original_run)

    async def empty_terms(_kind, payload):
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-terms-1",
                    "request_id": payload["request_id"],
                    "items": [{"item_id": item["item_id"], "candidates": []} for item in payload["items"]],
                }
            ),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    resumed = asyncio.run(
        resume_preparation(
            first.work_dir,
            StubChecker(),
            term_transport=empty_terms,
        )
    )
    assert resumed.status == "ready"
    assert resumed.work_dir == first.work_dir


def test_freeze_intent_replays_glossary_without_source_term_file_or_model(tmp_path: Path) -> None:
    terms = tmp_path / "terms.json"
    terms.write_text('{"Memory":"内存"}', encoding="utf-8")
    source = source_book(tmp_path)
    first = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            config(auto_extract=False, user_terms_path=terms),
            StubChecker(),
        )
    )
    (first.work_dir / "bookplan.json").unlink()
    (first.work_dir / "glossary.json").unlink()
    terms.unlink()

    async def forbidden(*_):
        raise AssertionError("frozen resume must not call a model")

    resumed = asyncio.run(
        resume_preparation(
            first.work_dir,
            StubChecker(),
            term_transport=forbidden,
            resolution_transport=forbidden,
        )
    )
    assert resumed.status == "ready"
    assert RunStore(resumed.work_dir).read_glossary().terms[0].target == "内存"


def test_local_term_failures_close_with_gaps_but_still_commit_p4(tmp_path: Path) -> None:
    async def invalid(_kind, _payload):
        return {"raw": "not-json", "usage": {"input_tokens": 1, "output_tokens": 1}}

    result = asyncio.run(
        prepare_translation(
            source_book(tmp_path),
            tmp_path / "work",
            config(),
            StubChecker(),
            term_transport=invalid,
        )
    )
    store = RunStore(result.work_dir)
    assert result.status == "needs_attention"
    assert result.term_status == "closed_with_gaps"
    assert store.read_bookplan() == result.bookplan
    assert store.read_glossary().extraction_status == "closed_with_gaps"


def test_closed_pool_resumes_freeze_commit_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_book(tmp_path)
    original = RunStore.write_freeze
    failed_root: Path | None = None

    def interrupted(store: RunStore, _freeze):
        nonlocal failed_root
        failed_root = store.root
        raise OSError("injected freeze write interruption")

    monkeypatch.setattr(RunStore, "write_freeze", interrupted)
    with pytest.raises(OSError, match="injected freeze"):
        asyncio.run(
            prepare_translation(
                source,
                tmp_path / "work",
                config(auto_extract=False),
                StubChecker(),
            )
        )
    assert failed_root is not None
    store = RunStore(failed_root)
    assert store.read_candidate_pool().extraction_status == "disabled"
    assert not (failed_root / "glossary" / "freeze.json").exists()
    monkeypatch.setattr(RunStore, "write_freeze", original)

    async def forbidden(*_):
        raise AssertionError("closed pool resume must not call a model")

    resumed = asyncio.run(
        resume_preparation(
            failed_root,
            StubChecker(),
            term_transport=forbidden,
            resolution_transport=forbidden,
        )
    )
    assert resumed.status == "ready"
