"""One command path from source EPUB to a verified translated publication."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from engine.agents.models import build_run_model
from engine.agents.runtime_v23 import PROMPT_VERSION_V25
from engine.core.config import settings
from engine.epub.checker import checker_for_source
from engine.epub.preparation_v25 import PreparationConfig
from engine.epub.publication import publish_book
from engine.item.planner_v25 import PLANNER_VERSION
from engine.orchestrator import TranslationRunResult, run_translation
from engine.schemas.v25 import JsonValue
from engine.services.preparation_pipeline import prepare_translation, resume_preparation
from engine.services.store_v25 import StoreV25
from engine.services.term_planning import TERM_PLANNER_VERSION

type RunStatus = Literal["completed", "paused", "needs_attention", "failed"]


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
        "prompt_version": PROMPT_VERSION_V25,
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
    return asyncio.run(_advance_source(source, output, work_root, config, checker, model=model, overwrite=overwrite))


def resume_book(
    work_dir: Path,
    *,
    output: Path,
    epubcheck: str | None = None,
    overwrite: bool = False,
) -> RunOutcome:
    """Resume only the saved source and run identities, without the original user term file."""
    work_dir = work_dir.resolve(strict=True)
    store = StoreV25(work_dir)
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
    return asyncio.run(_advance_work_dir(work_dir, output, checker, model=model, overwrite=overwrite))


async def _advance_source(
    source: Path,
    output: Path,
    work_root: Path,
    config: PreparationConfig,
    checker: object,
    *,
    model: object,
    overwrite: bool,
) -> RunOutcome:
    prepared = await prepare_translation(source, work_root, config, checker, model=model)
    return await _finish(prepared.status, prepared.phase, prepared.work_dir, output, checker, model, overwrite)


async def _advance_work_dir(
    work_dir: Path,
    output: Path,
    checker: object,
    *,
    model: object,
    overwrite: bool,
) -> RunOutcome:
    prepared = await resume_preparation(work_dir, checker, model=model)
    return await _finish(prepared.status, prepared.phase, prepared.work_dir, output, checker, model, overwrite)


async def _finish(
    preparation_status: str,
    phase: str,
    work_dir: Path,
    output: Path,
    checker: object,
    model: object,
    overwrite: bool,
) -> RunOutcome:
    if preparation_status == "paused":
        return RunOutcome("paused", work_dir, phase)
    if preparation_status not in {"ready", "needs_attention"}:
        return RunOutcome("failed", work_dir, phase, reason=f"unknown preparation status: {preparation_status}")
    translated: TranslationRunResult = await run_translation(work_dir, model=model)
    store = StoreV25(work_dir)
    count = store.read_bookplan().required_unit_count
    if translated.status != "translated":
        return RunOutcome(
            translated.status,
            work_dir,
            "translation",
            accepted_units=translated.accepted_units,
            required_units=count,
            http_attempts=translated.http_attempts,
            reason=translated.reason,
        )
    published = publish_book(store, output, checker, overwrite=overwrite)
    return RunOutcome(
        "completed",
        work_dir,
        phase="publication",
        accepted_units=translated.accepted_units,
        required_units=count,
        http_attempts=translated.http_attempts,
        output_path=output,
        output_sha256=str(published["sha256"]),
    )


__all__ = ["RunOutcome", "RunStatus", "check_output", "resume_book", "translate_book"]
