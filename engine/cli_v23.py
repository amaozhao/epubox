"""Thin CLI integration for the opt-in JSON-backed engine."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from pathlib import Path
from typing import Any

from engine.agents.models import build_fallback_model, build_primary_model
from engine.agents.runtime_v23 import PROMPT_VERSION
from engine.core.config import settings
from engine.epub.preparation import PreparationConfig, prepare_book, repair_document, resume_preparation
from engine.epub.validation import EpubChecker
from engine.item.extractor import EXTRACTOR_VERSION, extract_document
from engine.item.planner import PlannerConfig, plan_unit
from engine.orchestrator_v23 import TranslationEngine
from engine.schemas.v23 import RunConfig, RunResult, strict_json_loads
from engine.services.store import Store


def checker_command(command: str | None = None) -> EpubChecker:
    configured = command or os.environ.get("EPUBCHECK_COMMAND")
    if configured:
        return EpubChecker(shlex.split(configured))
    tools = Path(__file__).resolve().parent.parent / ".tools" / "epubcheck"
    java = sorted(tools.glob("jdk*/Contents/Home/bin/java"))
    jars = sorted(tools.glob("epubcheck*/epubcheck.jar"))
    if java and jars:
        return EpubChecker((str(java[-1]), "-jar", str(jars[-1])))
    return EpubChecker()


def load_terms(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    raw = strict_json_loads(path.read_bytes())
    if isinstance(raw, dict):
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
            raise ValueError("legacy glossary must map text to text")
        return [
            {"source": key, "target": str(value), "scope": "book", "mode": "required", "note": "user glossary"}
            for key, value in raw.items()
            if str(value).strip()
        ]
    if not isinstance(raw, list):
        raise TypeError("glossary must be a list or a legacy term mapping")
    terms: list[dict[str, Any]] = []
    for term in raw:
        if not isinstance(term, dict) or not isinstance(term.get("source"), str) or not term["source"]:
            raise ValueError("glossary entry requires nonempty source")
        if not isinstance(term.get("target"), str) or term.get("mode") not in {"preferred", "required", "keep_source"}:
            raise ValueError("glossary entry requires target and preferred/required/keep_source mode")
        terms.append(dict(term))
    return terms


def model_for(provider: str, *, max_output_tokens: int) -> Any:
    if provider == "agnes":
        key = settings.AGNES_API_KEY
        model = build_primary_model(max_completion_tokens=max_output_tokens)
    elif provider == "proxy":
        key = settings.CR_PROXY_API_KEY
        model = build_fallback_model(max_completion_tokens=max_output_tokens)
    else:
        raise ValueError("v2.3 provider must be agnes or proxy; existing adapters are retained")
    if not key or key in {"sk-", "your-api-key-here"}:
        raise ValueError(f"{provider} API key is not configured")
    return model


def check_output(source: Path, output: Path, *, overwrite: bool) -> None:
    if source.resolve() == output.resolve() or (output.exists() and os.path.samefile(source, output)):
        raise ValueError("output must not overwrite the source EPUB or its aliases")
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}; explicit --overwrite is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(output.parent, os.W_OK):
        raise PermissionError(f"output directory is not writable: {output.parent}")


async def _run(engine: TranslationEngine, output: Path, checker: EpubChecker, overwrite: bool) -> RunResult:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    interrupts = 0

    def stop() -> None:
        nonlocal interrupts
        interrupts += 1
        engine.request_stop()
        if interrupts > 1 and task is not None:
            task.cancel()

    installed = False
    try:
        try:
            loop.add_signal_handler(signal.SIGINT, stop)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        return await engine.run(output, checker, overwrite=overwrite)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)


def translate_v23(
    source: Path,
    *,
    output: Path | None,
    work_root: Path,
    context_tokens: int | None,
    max_output_tokens: int = 2048,
    http_limit: int = 0,
    concurrency: int = 2,
    provider: str = "agnes",
    glossary: Path | None = None,
    epubcheck: str | None = None,
    overwrite: bool = False,
    progress: Any = None,
    input_tokens: int | None = None,
) -> RunResult:
    if context_tokens is None:
        raise ValueError("v2.3 requires --context-tokens for the configured provider; capacity is not guessed")
    if min(context_tokens, max_output_tokens, concurrency) < 1 or http_limit < 0:
        raise ValueError("invalid request limits")
    output = output or source.with_name(source.stem + "-cn.epub")
    check_output(source, output, overwrite=overwrite)
    model = model_for(provider, max_output_tokens=max_output_tokens)
    terms = load_terms(glossary)
    generation: dict[str, Any] = {"base_url": str(model.base_url), "terms": terms}
    config = RunConfig(
        model=str(model.id),
        provider=provider,
        prompt_version=PROMPT_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        run_http_limit=http_limit,
        max_concurrency=concurrency,
        max_context_tokens=context_tokens,
        max_input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        rpm=settings.AGNES_TEXT_RPM if provider == "agnes" else 1,
        request_timeout_seconds=120,
        generation=generation,
    )
    checker = checker_command(epubcheck)
    prepared = prepare_book(
        source,
        work_root,
        PreparationConfig(run=config, terms=tuple(terms)),
        checker,
        extract_document=extract_document,
        plan_unit=plan_unit,
        planner_config=PlannerConfig(
            context_tokens=context_tokens,
            max_input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            review_output_tokens=max_output_tokens,
        ),
    )
    engine = TranslationEngine(Store(prepared.work_dir), model=model, progress=progress)
    return asyncio.run(_run(engine, output, checker, overwrite))


def resume_v23(
    work_dir: Path,
    *,
    output: Path,
    epubcheck: str | None = None,
    overwrite: bool = False,
    repair_file: Path | None = None,
    retry_units: list[str] | None = None,
    add_unit_http: int = 0,
    add_run_http: int = 0,
    progress: Any = None,
    retry_checks: list[str] | None = None,
    add_check_http: int = 0,
    repair_documents: list[str] | None = None,
) -> RunResult:
    store = Store(work_dir)
    book = store.read_bookplan()
    config = RunConfig.model_validate(book.frozen_config)
    if book.preparation_state != "ready" and config.extractor_version != EXTRACTOR_VERSION:
        raise ValueError("extractor version changed during preparation; start a new run")
    model = model_for(config.provider, max_output_tokens=config.max_output_tokens or 2048)
    if model.id != config.model or str(model.base_url) != config.generation.get("base_url"):
        raise ValueError("provider/model configuration changed; start a new run instead of reusing old results")
    check_output(work_dir / "source.epub", output, overwrite=overwrite or (work_dir / "publish.json").is_file())
    checker = checker_command(epubcheck)
    if not checker.check(work_dir / "source.epub").passed:
        raise ValueError("source EPUB no longer passes the required normative check")
    if book.preparation_state != "ready":
        if config.max_context_tokens is None:
            raise ValueError("unfinished preparation has no frozen provider context capacity")
        resume_preparation(
            work_dir,
            checker,
            extract_document=extract_document,
            plan_unit=plan_unit,
            planner_config=PlannerConfig(
                context_tokens=config.max_context_tokens,
                max_input_tokens=config.max_input_tokens,
                max_output_tokens=config.max_output_tokens or 2048,
                review_output_tokens=config.max_output_tokens or 2048,
            ),
        )
    for document_id in repair_documents or []:
        if config.max_context_tokens is None:
            raise ValueError("document repair requires frozen provider capacity")
        repair_document(
            store,
            document_id,
            checker,
            extract_document=extract_document,
            plan_unit=plan_unit,
            planner_config=PlannerConfig(
                context_tokens=config.max_context_tokens,
                max_input_tokens=config.max_input_tokens,
                max_output_tokens=config.max_output_tokens or 2048,
                review_output_tokens=config.max_output_tokens or 2048,
            ),
        )
    engine = TranslationEngine(store, model=model, progress=progress)
    if retry_units or add_unit_http or add_run_http:
        engine.retry_units(retry_units or [], add_unit_http=add_unit_http, add_run_http=add_run_http)
    if repair_file:
        engine.import_repairs(repair_file)
    if retry_checks:
        engine.retry_checks(retry_checks, add_http=add_check_http)
    return asyncio.run(_run(engine, output, checker, overwrite))
