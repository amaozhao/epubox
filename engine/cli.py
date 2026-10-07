"""One command path from source EPUB to a verified translated publication."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import traceback
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from engine.agents.models import build_run_model
from engine.agents.runtime import (
    ATOMIC_PROMPT_VERSION,
    MAX_MODEL_INPUT_TOKENS,
    RESOLUTION_PROTOCOL_VERSION,
    TERM_PROMPT_VERSION,
)
from engine.core.config import resolve_chunk_limit, settings
from engine.epub.checker import checker_for_source
from engine.epub.preparation import (
    PreparationConfig,
    _frozen_translation_config,
    _sha256_file,
)
from engine.epub.publication import publish_book, recover_publication
from engine.item.atoms import ADAPTER_VERSION as ATOMIC_ADAPTER_VERSION
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.orchestrator import (
    TranslationRunResult,
    import_repair_file,
    retry_failed_units,
    run_translation,
    validate_repair_file,
    validate_retry_failed_units,
)
from engine.schemas.contracts import (
    JsonValue,
    canonical_hash,
    canonical_json_bytes,
    strict_json_loads,
)
from engine.services import state
from engine.services.atomic import AtomicStore, IdentityMismatch, StoreLocked, safe_id
from engine.services.coherence import _read as read_coherence_record
from engine.services.coherence import add_http_budget, retry_document_check
from engine.services.legacy import _default_work_root, _existing_run_id, _resolve_work_root
from engine.services.preparation import PreparationProgress, prepare_translation, resume_preparation
from engine.services.report import write_report
from engine.services.store import RunStore
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION

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
    if source.resolve() == output.resolve() or (state.exists(output) and os.path.samefile(source, output)):
        raise ValueError("output must not overwrite the source EPUB or its aliases")
    if state.exists(output) and not overwrite:
        raise FileExistsError(f"output already exists: {output}; explicit --overwrite is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(output.parent, os.W_OK):
        raise PermissionError(f"output directory is not writable: {output.parent}")


def translate_book(
    source: Path,
    *,
    output: Path | None = None,
    work_root: Path | None = None,
    glossary: Path | None = None,
    auto_extract: bool = True,
    provider: str = "agnes",
    context_tokens: int = 32768,
    max_input_tokens: int = MAX_MODEL_INPUT_TOKENS,
    max_output_tokens: int = 4096,
    limit: int | None = None,
    http_limit: int = 0,
    concurrency: int = 2,
    epubcheck: str | None = None,
    overwrite: bool = False,
    repair_terms: bool = False,
    progress: ProgressCallback | None = None,
    explicit_options: frozenset[str] | None = None,
) -> RunOutcome:
    """Start and advance all durable gates with one user command."""
    _emit_start(progress)
    source = source.resolve(strict=True)
    output = output or source.with_name(f"{source.stem}-cn.epub")
    provider = _provider(provider)
    if min(context_tokens, max_input_tokens, max_output_tokens, concurrency) < 1 or http_limit < 0:
        raise ValueError("model limits must be positive and HTTP limit non-negative")
    chunk_limit = resolve_chunk_limit(limit)
    model_id = _model_id(provider)
    translation_config: dict[str, JsonValue] = {
        "target_language": "zh-Hans",
        "provider": provider,
        "model": model_id,
        "planner_version": "epubox-member-planner-1",
        "prompt_version": ATOMIC_PROMPT_VERSION,
        "context_tokens": context_tokens,
        "max_input_tokens": max_input_tokens,
        "max_source_tokens": chunk_limit,
        "max_output_tokens": max_output_tokens,
        "input_budget_version": 2,
        "output_budget_version": 4,
        "run_http_limit": http_limit,
        "concurrency": concurrency,
    }
    if provider == "agnes":
        translation_config["rpm"] = settings.AGNES_TEXT_RPM
    extraction_config: dict[str, JsonValue] = {
        "strategy": ATOMIC_TERM_PLANNER_VERSION,
        "prompt_version": TERM_PROMPT_VERSION,
        "resolution_protocol_version": RESOLUTION_PROTOCOL_VERSION,
        "provider": provider,
        "model": model_id,
        "target_language": "zh-Hans",
    }
    config = PreparationConfig(
        user_terms_path=glossary,
        auto_extract=auto_extract,
        extraction_config=extraction_config,
        translation_config=translation_config,
        adapter_version=ATOMIC_ADAPTER_VERSION,
        extractor_version=ATOMIC_EXTRACTOR_VERSION,
    )
    from engine.services.session import infer

    explicit_options = explicit_options if explicit_options is not None else infer(locals())
    source_hash = _sha256_file(source)
    config = replace(config, expected_source_hash=source_hash)
    if work_root is None:
        return _translate_adjacent(source, output, config, epubcheck, overwrite, progress, explicit_options)
    if state.compact(work_root):
        return _translate_adjacent(
            source, output, config, epubcheck, overwrite, progress, explicit_options, root=work_root
        )
    work_root = _resolve_work_root(work_root) if work_root is not None else _default_work_root(source, source_hash)
    with AtomicStore(work_root / source_hash).lock(blocking=False):
        resumable_work_dir = None
        if run_id := _existing_run_id(
            work_root / source_hash,
            source_hash,
            config,
            repair_terms=repair_terms,
            explicit_limit=limit is not None,
        ):
            config = replace(config, run_id=run_id)
            resumable_work_dir = work_root / source_hash / run_id
            completed = _completed_run_outcome(resumable_work_dir, output, epubcheck)
            if completed is not None:
                return completed
        else:
            config = replace(config, run_id=uuid.uuid4().hex)
        check_output(source, output, overwrite=overwrite)
        assert config.run_id is not None
        _write_source_hint(work_root / source_hash / config.run_id, source, source_hash, config.run_id)
        model = build_run_model(provider, model_id, max_output_tokens=max_output_tokens)
        checker = checker_for_source(source, epubcheck)
        if resumable_work_dir is not None:
            from engine.services.session import remember

            if resumable_work_dir.is_relative_to(source.with_name(source.stem).resolve()):
                remember(source, resumable_work_dir)
            return asyncio.run(
                _advance_work_dir(
                    resumable_work_dir,
                    output,
                    checker,
                    model=model,
                    overwrite=overwrite,
                    progress=progress,
                )
            )
        return asyncio.run(
            _advance_source(
                source, output, work_root, config, checker, model=model, overwrite=overwrite, progress=progress
            )
        )


def _translate_adjacent(
    source: Path,
    output: Path,
    config: PreparationConfig,
    epubcheck: str | None,
    overwrite: bool,
    progress: ProgressCallback | None,
    explicit: frozenset[str],
    *,
    root: Path | None = None,
) -> RunOutcome:
    root = root or source.with_name(source.stem)
    if root.is_symlink():
        raise IdentityMismatch("source-adjacent work directory must not be a symbolic link")
    root = root.resolve()
    if output.resolve().is_relative_to(root):
        raise ValueError("output must stay outside the translation work directory")
    with AtomicStore(root, compact=True).lock(blocking=False):
        return _advance_adjacent(source, output, config, epubcheck, overwrite, progress, explicit, root)


def _advance_adjacent(source, output, config, epubcheck, overwrite, progress, explicit, root) -> RunOutcome:
    from engine.services.legacy import locate
    from engine.services.session import find, validate_options

    active = find(source, config.expected_source_hash)
    if active is None and not state.compact(root):
        active = locate(source, str(config.expected_source_hash))
    if state.compact(root):
        if state.exists(root / "migration.json"):
            _finish_migration(root, source, str(config.expected_source_hash), epubcheck)
        active = root
    if active is not None and active != root:
        validate_options(active, config, explicit)
        saved = RunStore(active).read_preparation()
        if saved.source_hash != config.expected_source_hash:
            return resume_book(
                active,
                output=output,
                epubcheck=epubcheck,
                overwrite=overwrite,
                progress=progress,
                _automatic=not explicit,
            )
        with (
            AtomicStore(active.parent, compact=False).lock(blocking=False),
            AtomicStore(active, compact=False).lock(blocking=False),
        ):
            _initialize_state(root, source, saved.source_hash, saved.run_id, legacy_workdir=active)
            _finish_migration(root, source, saved.source_hash, epubcheck, _locked=True)
            active = root

    if active is not None and state.exists(root / "preparation.json"):
        if _replan_local(root, config):
            validate_options(root, config, explicit)
            previous = RunStore(root).read_preparation()
            config = replace(
                config,
                auto_extract=bool(previous.extraction_config.get("auto_extract", True)),
                extraction_config=dict(previous.extraction_config),
                translation_config=dict(previous.translation_config) | {"output_budget_version": 4},
            )
            state.reset_records(root)
            if progress:
                progress(
                    {"phase": "preflight", "notice": "旧计划尚未发起接口请求，按修正后的导航和预算规则重新准备。"}
                )
        else:
            validate_options(root, config, explicit)
            return resume_book(
                root,
                output=output,
                epubcheck=epubcheck,
                overwrite=overwrite,
                progress=progress,
                _automatic=not explicit,
            )
    if state.compact(root):
        run_id = str(state.header(root)["run_id"])
    else:
        run_id = uuid.uuid4().hex
        _initialize_state(root, source, str(config.expected_source_hash), run_id)
    config = replace(config, run_id=run_id)
    with AtomicStore(root).lock(blocking=False):
        check_output(source, output, overwrite=overwrite)
        _write_source_hint(root, source, str(config.expected_source_hash), run_id)
        translation = config.translation_config
        output_tokens = translation.get("max_output_tokens")
        if type(output_tokens) is not int or output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        model = build_run_model(
            str(translation["provider"]),
            str(translation["model"]),
            max_output_tokens=output_tokens,
        )
        return asyncio.run(
            _advance_source(
                source,
                output,
                root,
                config,
                checker_for_source(source, epubcheck),
                model=model,
                overwrite=overwrite,
                progress=progress,
            )
        )


def _finish_migration(
    root: Path, source: Path, source_hash: str, epubcheck: str | None, *, _locked: bool = False
) -> None:
    from engine.epub.preparation import _extract_source
    from engine.epub.validation import inspect_epub

    marker = strict_json_loads(state.read(root / "migration.json"))
    if (
        not isinstance(marker, dict)
        or marker.get("format") != "epubox-migration-1"
        or marker.get("source_hash") != source_hash
    ):
        raise IdentityMismatch("legacy migration record is invalid")
    identity = state.header(root)
    old = Path(str(marker.get("legacy_workdir")))
    if (
        marker.get("run_id") != identity["run_id"]
        or old.name != identity["run_id"]
        or old.parent.name != source_hash
        or not old.is_relative_to(root)
    ):
        raise IdentityMismatch("legacy migration path does not belong to this book")
    if any(path.is_symlink() for path in (old, *old.parents) if path.is_relative_to(root)):
        raise IdentityMismatch("legacy migration path must not contain symbolic links")
    with ExitStack() as locks:
        if old.exists() and not _locked:
            locks.enter_context(AtomicStore(old.parent, compact=False).lock(blocking=False))
            locks.enter_context(AtomicStore(old, compact=False).lock(blocking=False))
        inventory = inspect_epub(source, source_hash, checker=checker_for_source(source, epubcheck))
        _extract_source(source, root / "source", inventory)
        RunStore(root)._trusted_preparation_documents()
        if old.exists():
            for path in old.rglob("*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.name not in {"source.epub", ".store.lock"}
                    and state.read(root / path.relative_to(old)) != path.read_bytes()
                ):
                    raise IdentityMismatch("legacy record changed while it was imported")
            shutil.rmtree(old)
        parent = old.parent
        if parent.exists() and not [path for path in parent.iterdir() if path.name != ".store.lock"]:
            (parent / ".store.lock").unlink(missing_ok=True)
            parent.rmdir()
        (root / ".store.lock").unlink(missing_ok=True)
        (root / "active.json").unlink(missing_ok=True)
        state.unlink(root / "migration.json")


def _replan_local(root: Path, config: PreparationConfig) -> bool:
    if (
        state.exists(root / "prepared.json")
        or state.exists(root / "bookplan.json")
        or list(state.glob(root / "requests", "*.json"))
    ):
        return False
    path = root / "checks" / "preflight.json"
    if not state.exists(path):
        return False
    value = strict_json_loads(state.read(path))
    saved = RunStore(root).read_preparation()
    if saved.user_terms:
        return False
    if saved.translation_config == _frozen_translation_config(config):
        return False
    report = value.get("report") if isinstance(value, dict) else None
    return isinstance(report, dict) and report.get("check") is None


def _initialize_state(
    root: Path, source: Path, source_hash: str, run_id: str, *, legacy_workdir: Path | None = None
) -> None:
    try:
        state.initialize(
            root, source, source_hash, run_id, legacy_workdir=legacy_workdir, _legacy_locked=legacy_workdir is not None
        )
    except state.StateError as error:
        raise IdentityMismatch(f"source EPUB identity changed before snapshot: {error}") from error


def _completed_run_outcome(work_dir: Path, output: Path, epubcheck: str | None = None) -> RunOutcome | None:
    publish_path = work_dir / "publish.json"
    if not state.is_file(publish_path):
        return None
    store = RunStore(work_dir)
    if state.is_file(work_dir / "prepared.json"):
        from engine.epub.publish import recover_atomic
        from engine.services.ready import read_ready

        published = recover_atomic(store, output, checker=checker_for_source(state.snapshot(work_dir), epubcheck))
        if published is None:
            return None
        count = read_ready(store).plan.required_unit_count
        return _completed_outcome(work_dir, output, count, str(published["sha256"]))
    plan = store.read_bookplan()
    versions = {unit_id: store.read_unit(unit_id).revision for unit_id in plan.unit_ids}
    published = recover_publication(
        publish_path,
        plan_fingerprint=canonical_hash(plan),
        version_vector=versions,
    )
    if published is None or Path(str(published["target_path"])).resolve() != output.resolve():
        return None
    verification = published.get("verification")
    if isinstance(verification, dict):
        from engine.epub.verification import verify_baseline

        if (
            isinstance(verification.get("baseline"), dict)
            and _sha256_file(state.snapshot(work_dir)) != plan.source_hash
        ):
            raise IdentityMismatch("source snapshot changed after publication")
        verify_baseline(
            state.snapshot(work_dir), output, verification, checker_for_source(state.snapshot(work_dir), epubcheck)
        )
    return _completed_outcome(work_dir, output, plan.required_unit_count, str(published["target_hash"]))


def _completed_outcome(work_dir: Path, output: Path, count: int, digest: str) -> RunOutcome:
    return _record(
        RunOutcome(
            "completed",
            work_dir,
            "publication",
            accepted_units=count,
            required_units=count,
            output_path=output,
            output_sha256=digest,
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
    authorization_id: str | None = None,
    _automatic: bool = False,
) -> RunOutcome:
    """Resume only the saved source and run identities, without the original user term file."""
    work_dir = work_dir.resolve(strict=True)
    store = RunStore(work_dir)
    if any(type(value) is not int or value < 0 for value in (add_unit_http, add_run_http, add_check_http)):
        raise ValueError("HTTP budget additions must be non-negative")
    if (add_unit_http and not retry_units) or (add_check_http and not retry_checks):
        raise ValueError("Unit/check budget additions require explicit target IDs")
    preparation = store.read_preparation()
    identity = state.header(work_dir) if state.compact(work_dir) else None
    if (
        identity is None and (work_dir.name != preparation.run_id or work_dir.parent.name != preparation.source_hash)
    ) or (
        identity is not None
        and (identity["run_id"] != preparation.run_id or identity["source"]["hash"] != preparation.source_hash)
    ):
        raise IdentityMismatch("resume work directory does not match its frozen run identity")
    if state.is_file(work_dir / "prepared.json"):
        from engine.epub.publish import validate_atomic_output

        validate_atomic_output(store, output)
    completed = _completed_run_outcome(work_dir, output, epubcheck)
    if completed is not None:
        return completed
    source = state.snapshot(work_dir)
    check_output(source, output, overwrite=overwrite)
    extraction = preparation.extraction_config
    model_config = (
        preparation.translation_config if extraction.get("strategy") == ATOMIC_TERM_PLANNER_VERSION else extraction
    )
    provider = _provider(str(model_config["provider"]))
    output_tokens = model_config.get("max_output_tokens", 4096)
    if type(output_tokens) is not int or output_tokens < 1:
        raise ValueError("frozen max_output_tokens must be a positive integer")
    model = build_run_model(
        provider,
        str(model_config["model"]),
        max_output_tokens=output_tokens,
    )
    checker = checker_for_source(source, epubcheck)
    with AtomicStore(work_dir if state.compact(work_dir) else work_dir.parent).lock(blocking=False):
        _authorize_resume_actions(
            store,
            retry_units=retry_units,
            add_unit_http=add_unit_http,
            add_run_http=add_run_http,
            retry_checks=retry_checks,
            add_check_http=add_check_http,
            repair_file=repair_file,
            authorization_id=authorization_id,
        )
        return asyncio.run(
            _advance_work_dir(
                work_dir,
                output,
                checker,
                model=model,
                overwrite=overwrite,
                progress=progress,
                automatic=_automatic,
            )
        )


def _authorize_resume_actions(
    store: RunStore,
    *,
    retry_units: tuple[str, ...],
    add_unit_http: int,
    add_run_http: int,
    retry_checks: tuple[str, ...],
    add_check_http: int,
    repair_file: Path | None,
    authorization_id: str | None,
) -> None:
    if not any((retry_units, add_unit_http, add_run_http, retry_checks, add_check_http, repair_file)):
        return
    preparation = store.read_preparation()
    repair_hash = hashlib.sha256(state.read(repair_file)).hexdigest() if repair_file is not None else None
    action = {
        "run_id": preparation.run_id,
        "retry_units": sorted(set(retry_units)),
        "retry_checks": sorted(set(retry_checks)),
        "add_unit_http": add_unit_http,
        "add_run_http": add_run_http,
        "add_check_http": add_check_http,
        "repair_hash": repair_hash,
    }
    action_hash = canonical_hash(action)
    identity = safe_id(authorization_id or f"auto-{action_hash[:24]}")
    marker = store.root / "checks" / "manual-actions" / f"{identity}.json"
    with store.lock():
        if state.exists(marker):
            saved = strict_json_loads(state.read(marker))
            if not isinstance(saved, dict) or saved.get("action_hash") != action_hash:
                raise ValueError("authorization_id was already used for a different resume action")
            return
        atomic = state.is_file(store.root / "prepared.json")
        journal = None
        if atomic:
            if retry_checks or add_check_http or repair_file is not None:
                raise ValueError("Atomic runs do not support legacy coherence retries or repair files")
            if add_unit_http:
                raise ValueError("Atomic retries use --add-run-http instead of per-Unit HTTP additions")
            if retry_units:
                from engine.services.journal import BodyJournal

                for unit_id in retry_units:
                    safe_id(unit_id)
                journal = BodyJournal(store)
                journal.validate_retry_units(retry_units)
        book_path = store.root / "bookplan.json"
        if not atomic and (retry_units or retry_checks or repair_file is not None):
            if not state.exists(book_path):
                raise ValueError("Unit/check retry and repair require a ready BookPlan")
            book = store.read_bookplan()
            for unit_id in retry_units:
                safe_id(unit_id)
                if unit_id not in book.unit_ids:
                    raise ValueError(f"unknown retry Unit: {unit_id}")
                store.read_unit(unit_id)
            if retry_units:
                validate_retry_failed_units(store, retry_units, add_unit_http=add_unit_http)
            for document_id in retry_checks:
                safe_id(document_id)
                if document_id not in book.document_hashes:
                    raise ValueError(f"unknown retry Document: {document_id}")
                check = read_coherence_record(store._path("checks", document_id))
                if check.get("document_id") != document_id:
                    raise ValueError(f"coherence check identity mismatch: {document_id}")
            if repair_file is not None:
                try:
                    validate_repair_file(store, repair_file)
                except ValueError:
                    if not _repair_already_applied(store, repair_file):
                        raise
        add_http_budget(
            store,
            authorization_id=identity,
            action_context_hash=action_hash,
            add_run_http=add_run_http,
            add_unit_http={unit_id: add_unit_http for unit_id in retry_units} if add_unit_http and not atomic else {},
            add_check_http={document_id: add_check_http for document_id in retry_checks} if add_check_http else {},
        )
        if repair_file is not None and not _repair_already_applied(store, repair_file):
            import_repair_file(store, repair_file)
        if retry_units:
            if journal is not None:
                journal.retry_units(retry_units)
            else:
                retry_failed_units(store, retry_units)
        for document_id in retry_checks:
            retry_document_check(store, document_id)
        store._base.atomic_write_bytes(
            marker, canonical_json_bytes({"format": "epubox-action-1", "action_hash": action_hash})
        )


def _repair_already_applied(store: RunStore, path: Path) -> bool:
    raw = strict_json_loads(state.read(path))
    if not isinstance(raw, dict):
        return False
    unit_id = raw.get("unit_id")
    if not isinstance(unit_id, str):
        return False
    record = store.read_unit(unit_id)
    base = raw.get("base_revision")
    if type(base) is not int or record.revision != base + 1 or record.plan_epoch != raw.get("plan_epoch"):
        return False
    targets = raw.get("targets")
    if targets is None and isinstance(raw.get("target"), str) and len(record.items) == 1:
        targets = {next(iter(record.items)): raw["target"]}
    return isinstance(targets, dict) and all(
        item_id in record.items and record.items[item_id].target_projection == target
        for item_id, target in targets.items()
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
    try:
        prepared = await prepare_translation(
            source, work_root, config, checker, model=model, progress=_preparation_progress(progress)
        )
        from engine.services.session import remember

        if prepared.work_dir.resolve().is_relative_to(source.with_name(source.stem).resolve()):
            remember(source, prepared.work_dir)
    except StoreLocked:
        raise
    except Exception as error:
        if config.run_id is not None:
            failure_hash = config.expected_source_hash or _sha256_file(source)
            _record_internal_failure(
                work_root if state.compact(work_root) else work_root / failure_hash / safe_id(config.run_id),
                "preparation",
                error,
            )
        raise
    return await _finish(
        prepared.status,
        prepared.phase,
        prepared.work_dir,
        output,
        checker,
        model,
        overwrite,
        progress,
        reason=_preparation_reason(prepared),
    )


async def _advance_work_dir(
    work_dir: Path,
    output: Path,
    checker: object,
    *,
    model: object,
    overwrite: bool,
    progress: ProgressCallback | None = None,
    automatic: bool = False,
) -> RunOutcome:
    try:
        prepared = await resume_preparation(work_dir, checker, model=model, progress=_preparation_progress(progress))
        if automatic:
            from engine.services.session import reopen

            reopen(work_dir)
    except StoreLocked:
        raise
    except Exception as error:
        _record_internal_failure(work_dir, "preparation", error)
        raise
    return await _finish(
        prepared.status,
        prepared.phase,
        prepared.work_dir,
        output,
        checker,
        model,
        overwrite,
        progress,
        reason=_preparation_reason(prepared),
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
    *,
    reason: str | None = None,
) -> RunOutcome:
    if preparation_status == "paused":
        return _record(RunOutcome("paused", work_dir, phase))
    if preparation_status not in {"ready", "needs_attention"}:
        return _record(
            RunOutcome("failed", work_dir, phase, reason=f"unknown preparation status: {preparation_status}")
        )
    if not state.is_file(work_dir / "prepared.json") and not state.is_file(work_dir / "bookplan.json"):
        return _record(RunOutcome("needs_attention", work_dir, phase, reason=reason or "preparation is incomplete"))
    try:
        translated: TranslationRunResult = await run_translation(work_dir, model=model, progress=progress)
    except Exception as error:
        _record_internal_failure(work_dir, "translation", error)
        raise
    store = RunStore(work_dir)
    atomic = state.is_file(work_dir / "prepared.json")
    if atomic:
        from engine.services.ready import read_ready

        count = read_ready(store).plan.required_unit_count
    else:
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
    try:
        if atomic:
            from engine.epub.publish import publish_atomic

            published = publish_atomic(store, output, checker, overwrite=overwrite)
        else:
            published = publish_book(store, output, checker, overwrite=overwrite)
    except Exception as error:
        _record_internal_failure(work_dir, "publication", error)
        raise
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
        RunStore(outcome.work_dir),
        status=outcome.status,
        phase=outcome.phase,
        output_path=outcome.output_path,
        output_sha256=outcome.output_sha256,
        reason=outcome.reason,
    )
    saved = strict_json_loads(state.read(report))
    http = saved.get("http") if isinstance(saved, dict) else None
    attempts = http.get("actual_attempts") if isinstance(http, dict) else None
    if type(attempts) is not int:
        raise ValueError("run report did not contain HTTP accounting")
    return replace(outcome, report_path=state.artifact(report), http_attempts=attempts)


def _record_internal_failure(work_dir: Path, phase: str, error: Exception) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        AtomicStore.atomic_write_bytes(work_dir / "internal-error.txt", traceback.format_exc().encode())
        _record(RunOutcome("failed", work_dir, phase, reason=f"{type(error).__name__}: {error}"))
    except Exception as reporting_error:  # noqa: BLE001 - preserve the original failure
        fallback = {
            "format": "epubox-failure-report-1",
            "status": "failed",
            "phase": phase,
            "reason": f"{type(error).__name__}: {error}",
            "work_dir": str(work_dir),
            "report_incomplete": True,
        }
        AtomicStore.atomic_write_bytes(work_dir / "report.json", canonical_json_bytes(fallback))
        error.add_note(f"full diagnostic report unavailable: {reporting_error}")


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
                "notice": event.notice,
            }
        )

    return emit


def _emit_start(progress: ProgressCallback | None) -> None:
    if progress is not None:
        progress(
            {
                "phase": "startup",
                "execution_state": "running",
                "planned": 0,
                "succeeded": 0,
                "failed": 0,
                "pending": 0,
                "http_attempts": 0,
            }
        )


def _write_source_hint(work_dir: Path, source: Path, source_hash: str, run_id: str) -> None:
    if work_dir.is_symlink():
        raise IdentityMismatch("run checkpoint must not be a symbolic link")
    from engine.services.session import fingerprint, source_record

    source = source.resolve(strict=True)
    digest, after = fingerprint(source)
    if (
        digest != source_hash
        or (state.header(work_dir)["run_id"] if state.compact(work_dir) else work_dir.name) != run_id
    ):
        raise IdentityMismatch("source EPUB identity changed before the original-path record was created")
    data = {
        "format": "epubox-source-1",
        "run_id": run_id,
        "source_hash": source_hash,
        "original_path": str(source),
        "st_dev": after.st_dev,
        "st_ino": after.st_ino,
    }
    path = work_dir / "source.json"
    work_dir.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(data)
    if state.exists(path) or path.is_symlink():
        source_record(path, data)
        return
    AtomicStore.atomic_write_bytes(path, encoded)


def _preparation_reason(prepared: Any) -> str | None:
    details = [str(failure) for diagnostic in prepared.diagnostics for failure in diagnostic.failures]
    return "；".join(filter(None, (prepared.reason, *details))) or None


__all__ = ["RunOutcome", "RunStatus", "check_output", "resume_book", "translate_book"]
