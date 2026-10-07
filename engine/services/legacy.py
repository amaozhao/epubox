"""Locate compatible historical checkpoints without changing saved plans."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from engine.agents.runtime import (
    PROMPT_VERSION,
    RESOLUTION_PROTOCOL_VERSION,
    TERM_PROMPT_VERSION,
)
from engine.epub.preparation import (
    PreparationConfig,
    _frozen_extraction_config,
    _frozen_translation_config,
)
from engine.item.atoms import ADAPTER_VERSION as ATOMIC_ADAPTER_VERSION
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.item.context import PLANNER_VERSION
from engine.item.extractor import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.item.planner import MAX_SOURCE_TOKENS
from engine.schemas.contracts import (
    JsonValue,
    UnsupportedFormatError,
    canonical_hash,
    canonical_json_bytes,
    strict_json_loads,
)
from engine.services import state
from engine.services.atomic import AtomicStore, IdentityMismatch, safe_id
from engine.services.store import RunStore
from engine.services.terms.inputs import load_user_terms
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION, TERM_PLANNER_VERSION


def locate(source: Path, source_hash: str) -> Path | None:
    """Select one verified historical session before initializing flat storage."""
    root = source.with_name(source.stem)
    if not (root / source_hash).exists():
        candidates = {Path("work").resolve() / source_hash, Path(__file__).resolve().parents[2] / "work" / source_hash}
        if any(path.is_dir() for path in candidates):
            _default_work_root(source, source_hash)
    directory = root / source_hash
    if not directory.is_dir():
        return None
    found = []
    for run in directory.iterdir():
        if run.is_symlink():
            raise IdentityMismatch("run checkpoint must not be a symbolic link")
        if not (run / "preparation.json").is_file():
            continue
        store = RunStore(run)
        preparation = store.read_preparation()
        if preparation.source_hash != source_hash or preparation.run_id != run.name:
            raise IdentityMismatch("legacy session identity changed")
        store._trusted_preparation_documents()
        found.append(run)
    if len(found) > 1:
        raise IdentityMismatch("multiple historical sessions exist without an active session")
    return found[0] if found else None


def _default_work_root(source: Path, source_hash: str) -> Path:
    """Place artifacts beside the source and safely adopt one legacy checkpoint."""
    root = source.with_name(source.stem)
    destination = root / source_hash
    legacy_candidates = {
        Path("work").resolve() / source_hash,
        (Path(__file__).resolve().parents[2] / "work").resolve() / source_hash,
    }
    if root.is_symlink():
        raise IdentityMismatch("source-adjacent work directory must not be a symbolic link")
    with AtomicStore(root).lock(blocking=False):
        if destination.is_symlink() or any(path != destination and path.is_symlink() for path in legacy_candidates):
            raise IdentityMismatch("checkpoint directory must not be a symbolic link")
        legacy = sorted(path for path in legacy_candidates if path != destination and state.is_dir(path))
        if not legacy:
            return root
        if state.exists(destination) or len(legacy) != 1:
            raise IdentityMismatch("multiple checkpoint locations exist; use resume with the intended work directory")
        old = legacy[0]
        with AtomicStore(old).lock(blocking=False), ExitStack() as locks:
            runs = sorted(old.iterdir())
            if any(run.is_symlink() for run in runs):
                raise IdentityMismatch("run checkpoint must not be a symbolic link")
            for run in runs:
                if state.is_dir(run):
                    locks.enter_context(AtomicStore(run).lock(blocking=False))
            # ponytail: rename is deliberately same-filesystem only; cross-device migration stays explicit.
            old.rename(destination)
            AtomicStore.sync_directory(root)
            AtomicStore.sync_directory(old.parent)
    return root


def _resolve_work_root(work_root: Path) -> Path:
    absolute = work_root.absolute()
    if any(path.is_symlink() and not state.exists(path) for path in reversed((absolute, *absolute.parents))):
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
        if not state.is_file(run_dir / "preparation.json"):
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
        if state.is_file(marker_path):
            marker = strict_json_loads(state.read(marker_path))
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
    translation.pop("minimum_source_tokens", None)
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
    previous.pop("minimum_source_tokens", None)
    previous["planner_version"] = "epubox-member-planner-1"
    prior_extraction = dict(current[0])
    prior_extraction["max_output_tokens"] = 4096
    prior_output = dict(current[1])
    prior_output["max_output_tokens"] = 4096
    prior = dict(prior_output)
    prior["output_budget_version"] = 5
    previous["max_output_tokens"] = 4096
    legacy_extraction = _frozen_extraction_config(legacy)
    legacy_translation = _frozen_translation_config(legacy)
    legacy_prior_extraction = dict(legacy_extraction) | {"max_output_tokens": 4096}
    legacy_prior_translation = dict(legacy_translation) | {"max_output_tokens": 4096}
    return (
        current,
        (current[0], dict(current[1]) | {"output_budget_version": 5}),
        (prior_extraction, prior_output),
        (prior_extraction, prior),
        (prior_extraction, previous),
        (prior_extraction, previous | {"output_budget_version": 2}),
        (prior_extraction, previous | {"output_budget_version": 3}),
        (prior_extraction, previous | {"output_budget_version": 4}),
        (legacy_extraction, legacy_translation),
        (legacy_prior_extraction, legacy_prior_translation),
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
        and state.is_file(store.root / "bookplan.json")
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
        or state.exists(store.root / "publish.json")
        or not state.exists(store.root / "bookplan.json")
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
