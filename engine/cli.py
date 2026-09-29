"""One command path from source EPUB to a verified translated publication."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from engine.agents.models import build_run_model
from engine.agents.runtime import PROMPT_VERSION
from engine.core.config import settings
from engine.epub.checker import checker_for_source
from engine.epub.preparation_v25 import PreparationConfig
from engine.epub.publication import publish_book
from engine.item.planner_v25 import PLANNER_VERSION
from engine.orchestrator import TranslationRunResult, import_repair_file, retry_failed_units, run_translation
from engine.schemas.v25 import JsonValue
from engine.services.coherence import add_http_budget, retry_document_check
from engine.services.preparation_pipeline import PreparationProgress, prepare_translation, resume_preparation
from engine.services.report import write_report
from engine.services.store_v25 import StoreV25
from engine.services.term_planning import TERM_PLANNER_VERSION

type RunStatus = Literal["completed", "paused", "needs_attention", "failed"]
type ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus
    work_dir: Path
    phase: str
    accepted_units: int = 0
    required_units: int = 0
    http_attempts: int = 0
    output_path: Path | None = None
    output_sha256: str | None = None
    reason: str | None = None
    report_path: Path | None = None


def _provider(value: str) -> str:
    provider = "cr_proxy" if value == "proxy" else value
    if provider not in {"agnes", "cr_proxy"}:
        raise ValueError("provider must be agnes or proxy")
    return provider


def _model_id(provider: str) -> str:
    return settings.AGNES_MODEL if provider == "agnes" else settings.CR_PROXY_MODEL


def check_output(source: Path, output: Path, *, overwrite: bool) -> None:
    if source.resolve() == output.resolve() or (output.exists() and os.path.samefile(source, output)):
        raise ValueError("output must not overwrite the source EPUB or its aliases")
    if output.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output}; explicit --overwrite is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(output.parent, os.W_OK):
        raise PermissionError(f"output directory is not writable: {output.parent}")


def translate_book(
    source: Path,
    *,
    output: Path | None = None,
    work_root: Path = Path("work"),
    glossary: Path | None = None,
    auto_extract: bool = True,
    provider: str = "agnes",
    context_tokens: int = 32768,
    max_output_tokens: int = 4096,
    http_limit: int = 0,
    concurrency: int = 2,
    epubcheck: str | None = None,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
) -> RunOutcome:
    """Start and advance all durable gates with one user command."""
    source = source.resolve(strict=True)
    output = output or source.with_name(f"{source.stem}-zh-Hans.epub")
    check_output(source, output, overwrite=overwrite)
    provider = _provider(provider)
    if min(context_tokens, max_output_tokens, concurrency) < 1 or http_limit < 0:
        raise ValueError("model limits must be positive and HTTP limit non-negative")
    model_id = _model_id(provider)
    model = build_run_model(provider, model_id, max_output_tokens=max_output_tokens)
    checker = checker_for_source(source, epubcheck)
    translation_config: dict[str, JsonValue] = {
        "target_language": "zh-Hans",
        "provider": provider,
        "model": model_id,
        "planner_version": PLANNER_VERSION,
        "context_tokens": context_tokens,
        "max_output_tokens": max_output_tokens,
        "run_http_limit": http_limit,
        "concurrency": concurrency,
    }
    extraction_config: dict[str, JsonValue] = {
        "strategy": TERM_PLANNER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "provider": provider,
        "model": model_id,
        "target_language": "zh-Hans",
    }
    config = PreparationConfig(
        user_terms_path=glossary,
        auto_extract=auto_extract,
        extraction_config=extraction_config,
        translation_config=translation_config,
    )
    return asyncio.run(
        _advance_source(
            source, output, work_root, config, checker, model=model, overwrite=overwrite, progress=progress
        )
    )


def resume_book(
    work_dir: Path,
    *,
    output: Path,
    epubcheck: str | None = None,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
    retry_units: tuple[str, ...] = (),
    add_unit_http: int = 0,
    add_run_http: int = 0,
    retry_checks: tuple[str, ...] = (),
    add_check_http: int = 0,
    repair_file: Path | None = None,
) -> RunOutcome:
    """Resume only the saved source and run identities, without the original user term file."""
    work_dir = work_dir.resolve(strict=True)
    store = StoreV25(work_dir)
    if any(value < 0 for value in (add_unit_http, add_run_http, add_check_http)):
        raise ValueError("HTTP budget additions must be non-negative")
    if add_unit_http and not retry_units or add_check_http and not retry_checks:
        raise ValueError("Unit/check budget additions require explicit target IDs")
    preparation = store.read_preparation()
    source = work_dir / preparation.source_path
    check_output(source, output, overwrite=overwrite)
    extraction = preparation.extraction_config
    provider = _provider(str(extraction["provider"]))
    output_tokens = extraction.get("max_output_tokens", 4096)
    if type(output_tokens) is not int or output_tokens < 1:
        raise ValueError("frozen max_output_tokens must be a positive integer")
    model = build_run_model(
        provider,
        str(extraction["model"]),
        max_output_tokens=output_tokens,
    )
    checker = checker_for_source(source, epubcheck)
    if add_run_http:
        add_http_budget(store, add_run_http=add_run_http)
    return asyncio.run(
        _advance_work_dir(
            work_dir,
            output,
            checker,
            model=model,
            overwrite=overwrite,
            progress=progress,
            retry_units=retry_units,
            add_unit_http=add_unit_http,
            retry_checks=retry_checks,
            add_check_http=add_check_http,
            repair_file=repair_file,
        )
    )


async def _advance_source(
    source: Path,
    output: Path,
    work_root: Path,
    config: PreparationConfig,
    checker: object,
    *,
    model: object,
    overwrite: bool,
    progress: ProgressCallback | None = None,
) -> RunOutcome:
    prepared = await prepare_translation(
        source, work_root, config, checker, model=model, progress=_preparation_progress(progress)
    )
    return await _finish(
        prepared.status, prepared.phase, prepared.work_dir, output, checker, model, overwrite, progress
    )


async def _advance_work_dir(
    work_dir: Path,
    output: Path,
    checker: object,
    *,
    model: object,
    overwrite: bool,
    progress: ProgressCallback | None = None,
    retry_units: tuple[str, ...] = (),
    add_unit_http: int = 0,
    retry_checks: tuple[str, ...] = (),
    add_check_http: int = 0,
    repair_file: Path | None = None,
) -> RunOutcome:
    prepared = await resume_preparation(work_dir, checker, model=model, progress=_preparation_progress(progress))
    if prepared.status != "paused" and (retry_units or retry_checks or repair_file is not None):
        store = StoreV25(work_dir)
        if add_unit_http or add_check_http:
            add_http_budget(
                store,
                add_unit_http={unit_id: add_unit_http for unit_id in retry_units},
                add_check_http={document_id: add_check_http for document_id in retry_checks},
            )
        if repair_file is not None:
            import_repair_file(store, repair_file)
        if retry_units:
            retry_failed_units(store, retry_units)
        for document_id in retry_checks:
            retry_document_check(store, document_id)
    return await _finish(
        prepared.status, prepared.phase, prepared.work_dir, output, checker, model, overwrite, progress
    )


async def _finish(
    preparation_status: str,
    phase: str,
    work_dir: Path,
    output: Path,
    checker: object,
    model: object,
    overwrite: bool,
    progress: ProgressCallback | None = None,
) -> RunOutcome:
    if preparation_status == "paused":
        return _record(RunOutcome("paused", work_dir, phase))
    if preparation_status not in {"ready", "needs_attention"}:
        return _record(
            RunOutcome("failed", work_dir, phase, reason=f"unknown preparation status: {preparation_status}")
        )
    translated: TranslationRunResult = await run_translation(work_dir, model=model, progress=progress)
    store = StoreV25(work_dir)
    count = store.read_bookplan().required_unit_count
    if translated.status != "translated":
        return _record(
            RunOutcome(
                translated.status,
                work_dir,
                "translation",
                accepted_units=translated.accepted_units,
                required_units=count,
                http_attempts=translated.http_attempts,
                reason=translated.reason,
            )
        )
    published = publish_book(store, output, checker, overwrite=overwrite)
    return _record(
        RunOutcome(
            "completed",
            work_dir,
            phase="publication",
            accepted_units=translated.accepted_units,
            required_units=count,
            http_attempts=translated.http_attempts,
            output_path=output,
            output_sha256=str(published["sha256"]),
        )
    )


def _record(outcome: RunOutcome) -> RunOutcome:
    report = write_report(
        StoreV25(outcome.work_dir),
        status=outcome.status,
        phase=outcome.phase,
        output_path=outcome.output_path,
        output_sha256=outcome.output_sha256,
        reason=outcome.reason,
    )
    return replace(outcome, report_path=report)


def _preparation_progress(progress: ProgressCallback | None) -> Callable[[PreparationProgress], None] | None:
    if progress is None:
        return None

    def emit(event: PreparationProgress) -> None:
        progress(
            {
                "phase": event.phase,
                "execution_state": "running",
                "planned": event.planned,
                "succeeded": event.succeeded,
                "failed": event.failed,
                "pending": event.pending,
                "http_attempts": event.http_attempts,
            }
        )

    return emit


__all__ = ["RunOutcome", "RunStatus", "check_output", "resume_book", "translate_book"]
