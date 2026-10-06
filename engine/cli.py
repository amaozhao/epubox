"""One command path from source EPUB to a verified translated publication."""

from __future__ import annotations

import asyncio
import hashlib
import os
import traceback
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from engine.agents.models import build_run_model
from engine.agents.runtime import (
    ATOMIC_PROMPT_VERSION,
    MAX_MODEL_INPUT_TOKENS,
    PROMPT_VERSION,
    RESOLUTION_PROTOCOL_VERSION,
    TERM_PROMPT_VERSION,
)
from engine.core.config import resolve_chunk_limit, settings
from engine.epub.checker import checker_for_source
from engine.epub.preparation import (
    PreparationConfig,
    _frozen_extraction_config,
    _frozen_translation_config,
    _sha256_file,
)
from engine.epub.publication import publish_book, recover_publication
from engine.item.atoms import ADAPTER_VERSION as ATOMIC_ADAPTER_VERSION
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.item.context import PLANNER_VERSION
from engine.item.extractor import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.item.planner import MAX_SOURCE_TOKENS
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
    UnsupportedFormatError,
    canonical_hash,
    canonical_json_bytes,
    strict_json_loads,
)
from engine.services.atomic import AtomicStore, IdentityMismatch, StoreLocked, safe_id
from engine.services.coherence import _read as read_coherence_record
from engine.services.coherence import add_http_budget, retry_document_check
from engine.services.preparation import PreparationProgress, prepare_translation, resume_preparation
from engine.services.report import write_report
from engine.services.store import RunStore
from engine.services.terms.inputs import load_user_terms
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION, TERM_PLANNER_VERSION

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
        "output_budget_version": 3,
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
        from engine.services.session import find, validate_options

        if active := find(source, source_hash):
            validate_options(active, config, explicit_options)
            completed = _completed_run_outcome(active, output, epubcheck)
            if completed is not None:
                return completed
            return resume_book(
                active,
                output=output,
                epubcheck=epubcheck,
                overwrite=overwrite,
                progress=progress,
                _automatic=not explicit_options,
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


def _default_work_root(source: Path, source_hash: str) -> Path:
    """Place artifacts beside the source and safely adopt one legacy checkpoint."""
    root = source.with_name(source.stem)
    destination = root / source_hash
    legacy_candidates = {
        Path("work").resolve() / source_hash,
        (Path(__file__).resolve().parents[1] / "work").resolve() / source_hash,
    }
    if root.is_symlink():
        raise IdentityMismatch("source-adjacent work directory must not be a symbolic link")
    with AtomicStore(root).lock(blocking=False):
        if destination.is_symlink() or any(path != destination and path.is_symlink() for path in legacy_candidates):
            raise IdentityMismatch("checkpoint directory must not be a symbolic link")
        legacy = sorted(path for path in legacy_candidates if path != destination and path.is_dir())
        if not legacy:
            return root
        if destination.exists() or len(legacy) != 1:
            raise IdentityMismatch("multiple checkpoint locations exist; use resume with the intended work directory")
        old = legacy[0]
        with AtomicStore(old).lock(blocking=False), ExitStack() as locks:
            runs = sorted(old.iterdir())
            if any(run.is_symlink() for run in runs):
                raise IdentityMismatch("run checkpoint must not be a symbolic link")
            for run in runs:
                if run.is_dir():
                    locks.enter_context(AtomicStore(run).lock(blocking=False))
            # ponytail: rename is deliberately same-filesystem only; cross-device migration stays explicit.
            old.rename(destination)
            AtomicStore.sync_directory(root)
            AtomicStore.sync_directory(old.parent)
    return root


def _resolve_work_root(work_root: Path) -> Path:
    absolute = work_root.absolute()
    if any(path.is_symlink() and not path.exists() for path in reversed((absolute, *absolute.parents))):
        raise IdentityMismatch("work root contains a dangling symbolic link")
    return work_root.resolve()


def _existing_run_id(
    source_root: Path,
    source_hash: str,
    config: PreparationConfig,
    *,
    repair_terms: bool = False,
    explicit_limit: bool = False,
) -> str | None:
    expected_configs = _expected_configs(config)
    expected_extraction, expected_translation = expected_configs[0]
    matches: list[str] = []
    other_runs: list[Path] = []
    repairable_runs: list[tuple[RunStore, Any]] = []
    for run_dir in sorted(source_root.iterdir()):
        if run_dir.is_symlink():
            raise IdentityMismatch("run checkpoint must not be a symbolic link")
        if not (run_dir / "preparation.json").is_file():
            continue
        store = RunStore(run_dir)
        try:
            preparation = store.read_preparation()
        except UnsupportedFormatError:
            other_runs.append(run_dir)
            continue
        if preparation.source_hash != source_hash or preparation.run_id != run_dir.name:
            raise IdentityMismatch(f"preparation identity differs from run directory: {run_dir}")
        exact_match = any(
            preparation.extraction_config == extraction and preparation.translation_config == translation
            for extraction, translation in expected_configs
        )
        implicit_limit_match = not explicit_limit and _implicit_limit_match(
            preparation.extraction_config,
            expected_extraction,
            preparation.translation_config,
            expected_translation,
        )
        compatible = next(
            (
                (extraction, translation)
                for extraction, translation in expected_configs
                if _legacy_frozen_term_run(store, preparation, extraction, translation)
                or _legacy_resolution_protocol_run(
                    preparation.extraction_config,
                    extraction,
                    preparation.translation_config,
                    translation,
                )
            ),
            None,
        )
        if not exact_match and not implicit_limit_match and compatible is None:
            other_runs.append(run_dir)
            continue
        terms, terms_hash = load_user_terms(
            config.user_terms_path,
            document_ids=preparation.document_hashes,
            unit_ids=preparation.unit_documents,
        )
        if preparation.user_terms != terms or preparation.user_terms_hash != terms_hash:
            other_runs.append(run_dir)
            continue
        store._trusted_preparation_documents()
        if (
            compatible is not None
            and _legacy_frozen_term_run(store, preparation, *compatible)
            and _superseded_empty_term_run(store, preparation, *compatible, config)
        ):
            repairable_runs.append((store, preparation))
            continue
        matches.append(preparation.run_id)
    if len(matches) > 1:
        raise ValueError(f"multiple matching runs exist for this EPUB; use resume with one work directory: {matches}")
    if matches:
        return matches[0]
    if len(repairable_runs) > 1:
        raise ValueError("multiple empty-term runs require repair; use a separate work root")
    if repairable_runs:
        store, preparation = repairable_runs[0]
        marker_path = source_root / "term-repair.json"
        expected_marker = {
            "format": "epubox-term-repair-1",
            "source_hash": source_hash,
            "old_run_id": preparation.run_id,
            "old_glossary_hash": canonical_hash(store.read_glossary()),
            "extraction_config_hash": canonical_hash(expected_extraction),
            "translation_config_hash": canonical_hash(expected_translation),
            "user_terms_hash": preparation.user_terms_hash,
            "reason": "epubox-v25-2 rejected every automatic term candidate",
        }
        if marker_path.is_file():
            marker = strict_json_loads(marker_path.read_bytes())
            if not isinstance(marker, dict) or any(marker.get(key) != value for key, value in expected_marker.items()):
                raise ValueError("term repair marker does not match this source, run, or frozen configuration")
            replacement_run_id = marker.get("replacement_run_id")
            if not isinstance(replacement_run_id, str):
                raise ValueError("term repair marker has no replacement run identity")
            return safe_id(replacement_run_id)
        if not repair_terms:
            raise ValueError(
                "the existing epubox-v25-2 run rejected every automatic term candidate; "
                "run this command once with --repair-terms to authorize a new terminology pass, "
                "then ordinary translate commands will resume it"
            )
        replacement_run_id = uuid.uuid4().hex
        AtomicStore.atomic_write_bytes(
            marker_path,
            canonical_json_bytes(expected_marker | {"replacement_run_id": replacement_run_id}),
        )
        return replacement_run_id
    if repair_terms:
        raise ValueError("--repair-terms requires an eligible epubox-v25-2 empty-term run")
    if other_runs:
        raise ValueError(
            "existing run has a different frozen configuration; use the original options or a new work root"
        )
    return None


def _expected_configs(config: PreparationConfig) -> tuple[tuple[dict[str, JsonValue], dict[str, JsonValue]], ...]:
    current = (_frozen_extraction_config(config), _frozen_translation_config(config))
    if config.adapter_version != ATOMIC_ADAPTER_VERSION or config.extractor_version != ATOMIC_EXTRACTOR_VERSION:
        return (current,)
    extraction = dict(config.extraction_config)
    extraction["strategy"] = TERM_PLANNER_VERSION
    translation = dict(config.translation_config)
    translation.update(
        {
            "planner_version": PLANNER_VERSION,
            "prompt_version": PROMPT_VERSION,
            "max_source_tokens": MAX_SOURCE_TOKENS,
        }
    )
    translation.pop("max_input_tokens", None)
    translation.pop("input_budget_version", None)
    translation.pop("output_budget_version", None)
    translation.pop("rpm", None)
    legacy = replace(
        config,
        extraction_config=extraction,
        translation_config=translation,
        adapter_version=ADAPTER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
    )
    previous = dict(current[1])
    previous.pop("output_budget_version", None)
    return (
        current,
        (current[0], previous),
        (current[0], previous | {"output_budget_version": 2}),
        (_frozen_extraction_config(legacy), _frozen_translation_config(legacy)),
    )


def _implicit_limit_match(
    actual_extraction: Mapping[str, JsonValue],
    expected_extraction: Mapping[str, JsonValue],
    actual_translation: Mapping[str, JsonValue],
    expected_translation: Mapping[str, JsonValue],
) -> bool:
    return (
        actual_extraction == expected_extraction
        and actual_extraction.get("strategy") == ATOMIC_TERM_PLANNER_VERSION
        and {key: value for key, value in actual_translation.items() if key != "max_source_tokens"}
        == {key: value for key, value in expected_translation.items() if key != "max_source_tokens"}
    )


def _legacy_frozen_term_run(
    store: RunStore,
    preparation: Any,
    expected_extraction: Mapping[str, JsonValue],
    expected_translation: Mapping[str, JsonValue],
) -> bool:
    return (
        preparation.extraction_config.get("prompt_version") == PROMPT_VERSION
        and expected_extraction.get("prompt_version") == TERM_PROMPT_VERSION
        and _without_term_versions(preparation.extraction_config) == _without_term_versions(expected_extraction)
        and preparation.translation_config == expected_translation
        and (store.root / "bookplan.json").is_file()
    )


def _legacy_resolution_protocol_run(
    actual_extraction: Mapping[str, JsonValue],
    expected_extraction: Mapping[str, JsonValue],
    actual_translation: Mapping[str, JsonValue],
    expected_translation: Mapping[str, JsonValue],
) -> bool:
    return (
        "resolution_protocol_version" not in actual_extraction
        and expected_extraction.get("resolution_protocol_version") == RESOLUTION_PROTOCOL_VERSION
        and {k: v for k, v in expected_extraction.items() if k != "resolution_protocol_version"}
        == dict(actual_extraction)
        and actual_translation == expected_translation
    )


def _without_term_versions(config: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {k: v for k, v in config.items() if k not in {"prompt_version", "resolution_protocol_version"}}


def _superseded_empty_term_run(
    store: RunStore,
    preparation: Any,
    expected_extraction: Mapping[str, JsonValue],
    expected_translation: Mapping[str, JsonValue],
    config: PreparationConfig,
) -> bool:
    if (
        preparation.extraction_config.get("prompt_version") != PROMPT_VERSION
        or expected_extraction.get("prompt_version") != TERM_PROMPT_VERSION
        or _without_term_versions(preparation.extraction_config) != _without_term_versions(expected_extraction)
        or preparation.translation_config != expected_translation
        or (store.root / "publish.json").exists()
        or not (store.root / "bookplan.json").exists()
    ):
        return False
    terms, terms_hash = load_user_terms(
        config.user_terms_path,
        document_ids=preparation.document_hashes,
        unit_ids=preparation.unit_documents,
    )
    if preparation.user_terms != terms or preparation.user_terms_hash != terms_hash:
        return False
    book = store.read_bookplan()
    glossary = store.read_glossary()
    if glossary.terms or glossary.extraction_status != "closed_with_gaps" or store.read_candidate_pool().candidates:
        return False
    if any(store.read_unit(unit_id).accepted_revision is not None for unit_id in book.unit_ids):
        return False
    return any(
        record.status == "succeeded_with_rejections"
        and not record.candidates
        and any(str(diagnostic.get("reason", "")).startswith("candidate ") for diagnostic in record.diagnostics)
        for item in store.read_term_plan().items
        for record in (store.read_extraction(item.item_id),)
    )


def _completed_run_outcome(work_dir: Path, output: Path, epubcheck: str | None = None) -> RunOutcome | None:
    publish_path = work_dir / "publish.json"
    if not publish_path.is_file():
        return None
    store = RunStore(work_dir)
    if (work_dir / "prepared.json").is_file():
        from engine.epub.publish import recover_atomic
        from engine.services.ready import read_ready

        published = recover_atomic(store, output, checker=checker_for_source(work_dir / "source.epub", epubcheck))
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
            and _sha256_file(work_dir / "source.epub") != plan.source_hash
        ):
            raise IdentityMismatch("source snapshot changed after publication")
        verify_baseline(
            work_dir / "source.epub", output, verification, checker_for_source(work_dir / "source.epub", epubcheck)
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
    if work_dir.name != preparation.run_id or work_dir.parent.name != preparation.source_hash:
        raise IdentityMismatch("resume work directory does not match its frozen run identity")
    if (work_dir / "prepared.json").is_file():
        from engine.epub.publish import validate_atomic_output

        validate_atomic_output(store, output)
    completed = _completed_run_outcome(work_dir, output, epubcheck)
    if completed is not None:
        return completed
    source = work_dir / preparation.source_path
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
    with AtomicStore(work_dir.parent).lock(blocking=False):
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
    repair_hash = hashlib.sha256(repair_file.read_bytes()).hexdigest() if repair_file is not None else None
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
        if marker.exists():
            saved = strict_json_loads(marker.read_bytes())
            if not isinstance(saved, dict) or saved.get("action_hash") != action_hash:
                raise ValueError("authorization_id was already used for a different resume action")
            return
        atomic = (store.root / "prepared.json").is_file()
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
            if not book_path.exists():
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
    raw = strict_json_loads(path.read_bytes())
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
            _record_internal_failure(work_root / failure_hash / safe_id(config.run_id), "preparation", error)
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
    if not (work_dir / "prepared.json").is_file() and not (work_dir / "bookplan.json").is_file():
        return _record(RunOutcome("needs_attention", work_dir, phase, reason=reason or "preparation is incomplete"))
    try:
        translated: TranslationRunResult = await run_translation(work_dir, model=model, progress=progress)
    except Exception as error:
        _record_internal_failure(work_dir, "translation", error)
        raise
    store = RunStore(work_dir)
    atomic = (work_dir / "prepared.json").is_file()
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
    saved = strict_json_loads(report.read_bytes())
    http = saved.get("http") if isinstance(saved, dict) else None
    attempts = http.get("actual_attempts") if isinstance(http, dict) else None
    if type(attempts) is not int:
        raise ValueError("run report did not contain HTTP accounting")
    return replace(outcome, report_path=report, http_attempts=attempts)


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
    if digest != source_hash or work_dir.name != run_id:
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
    if path.exists() or path.is_symlink():
        source_record(path, data)
        return
    AtomicStore.atomic_write_bytes(path, encoded)


def _preparation_reason(prepared: Any) -> str | None:
    details = [str(failure) for diagnostic in prepared.diagnostics for failure in diagnostic.failures]
    return "；".join(filter(None, (prepared.reason, *details))) or None


__all__ = ["RunOutcome", "RunStatus", "check_output", "resume_book", "translate_book"]
