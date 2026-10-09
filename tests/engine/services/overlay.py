from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from engine.epub.preparation import PreparationConfig, prepare_book
from engine.item.atoms import ADAPTER_VERSION, EXTRACTOR_VERSION
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import canonical_json_bytes
from engine.services import preflight, state
from engine.services.atomic import CorruptRecord
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker

MODEL = "gpt-3.5-turbo"


def limits(*, output: int | None, version: Literal[2, 3, 4, 5, 6, 7], source: int = 5_000) -> BudgetLimits:
    return BudgetLimits(
        source_tokens=source,
        input_tokens=50_000,
        output_tokens=output,
        context_tokens=60_000,
        output_version=version,
        minimum_source_tokens=500,
        source_tolerance_tokens=1_000,
        context_unlimited=True,
    )


def stored(tmp_path: Path) -> tuple[RunStore, Path]:
    source = make_epub(tmp_path / "book.epub", {"chapter.xhtml": "<p>Output overlay paragraph.</p>"})
    root = tmp_path / "book"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    state.initialize(root, source, source_hash, "run")
    prepared = prepare_book(
        source,
        root,
        PreparationConfig(
            run_id="run",
            adapter_version=ADAPTER_VERSION,
            extractor_version=EXTRACTOR_VERSION,
            translation_config={"model": MODEL, "output_budget_version": 6, "max_output_tokens": 1},
        ),
        StubChecker(),
    )
    return RunStore(prepared.work_dir), root


@pytest.mark.parametrize("legacy_version", [2, 3, 4, 5, 6])
def test_legacy_output_block_creates_verified_v7_overlay_without_rewriting_dependencies(
    tmp_path: Path,
    legacy_version: Literal[2, 3, 4, 5, 6],
) -> None:
    store, root = stored(tmp_path)
    legacy_limits = limits(output=1, version=legacy_version)
    legacy = preflight.prepare_preflight(store, legacy_limits, MODEL)
    assert legacy.check is None and any(
        failure.startswith("output budget") for diagnostic in legacy.diagnostics for failure in diagnostic.failures
    )
    accepted_path = root / "glossary" / "results" / "accepted.json"
    accepted = canonical_json_bytes({"accepted": ["unchanged"]})
    state.write(accepted_path, accepted)
    user_terms_path = root / "glossary" / "user.json"
    original_user_terms = state.read(user_terms_path)
    original_receipt = state.read(root / "checks" / "preflight.json")
    original_record = preflight._read(
        root / "checks" / "preflight.json",
        preflight._PreflightRecord,
        "epubox-preflight-record-1",
    )
    original_inventories = {path.name: state.read(path) for path in state.glob(root / "inventories", "*.json")}

    upgraded = preflight.prepare_preflight(store, replace(legacy_limits, output_tokens=None, output_version=7), MODEL)

    assert upgraded.check is not None
    assert upgraded.limits["output_tokens"] is None and upgraded.limits["output_version"] == 7
    assert preflight.receipt_path(root) == root / "checks" / "output.json"
    assert state.is_file(preflight.receipt_path(root))
    assert not (root / "checks" / "output.json").is_file()
    assert state.artifact(preflight.receipt_path(root)) in state.files(root)
    assert state.read(root / "checks" / "preflight.json") == original_receipt
    assert {path.name: state.read(path) for path in state.glob(root / "inventories", "*.json")} == original_inventories
    assert state.read(accepted_path) == accepted
    assert state.read(user_terms_path) == original_user_terms
    overlay_record = preflight._read(
        preflight.receipt_path(root),
        preflight._PreflightRecord,
        "epubox-preflight-record-1",
    )
    assert overlay_record.translation_hash == original_record.translation_hash
    assert overlay_record.preparation_hash == original_record.preparation_hash
    assert preflight.read_preflight(store, legacy_limits, MODEL) == upgraded
    loaded, _inventories = preflight.load_preflight(store, legacy_limits, MODEL)
    assert loaded == upgraded
    assert preflight.preflight_verified(store)
    assert preflight.require_preflight(store, legacy_limits, MODEL) == upgraded
    with pytest.raises(preflight.IdentityMismatch, match="budget limits"):
        preflight.load_preflight(store, replace(legacy_limits, source_tokens=4_999), MODEL)


def test_overlay_tamper_is_rejected(tmp_path: Path) -> None:
    store, root = stored(tmp_path)
    old = limits(output=1, version=6)
    preflight.prepare_preflight(store, old, MODEL)
    preflight.prepare_preflight(store, replace(old, output_tokens=None, output_version=7), MODEL)
    path = preflight.receipt_path(root)
    record = preflight._read(path, preflight._PreflightRecord, "epubox-preflight-record-1")
    changed = record.report.model_copy(update={"budget_hash": "0" * 64})
    state.write(path, canonical_json_bytes(record.model_copy(update={"report": changed}), max_bytes=None))

    with pytest.raises((preflight.IdentityMismatch, CorruptRecord), match="hash|receipt"):
        preflight.load_preflight(store, old, MODEL)


def test_fresh_v7_uses_primary_receipt_and_keeps_source_input_gates(tmp_path: Path) -> None:
    store, root = stored(tmp_path)
    unlimited = limits(output=None, version=7)
    report = preflight.prepare_preflight(store, unlimited, MODEL)

    assert report.check is not None and report.limits["output_tokens"] is None
    assert preflight.receipt_path(root) == root / "checks" / "preflight.json"
    assert not state.is_file(root / "checks" / "output.json")
    legacy_limits = replace(unlimited, output_tokens=1, output_version=6)
    assert preflight.read_preflight(store, legacy_limits, MODEL) == report
    assert preflight.load_preflight(store, legacy_limits, MODEL)[0] == report
    assert preflight.require_preflight(store, legacy_limits, MODEL) == report

    constrained = preflight.preflight_atomic_resources(
        preflight.load_preflight(store, unlimited, MODEL)[1],
        preflight._snapshot_resources(state.snapshot(root), preflight.load_preflight(store, unlimited, MODEL)[1]),
        replace(unlimited, source_tokens=1, source_tolerance_tokens=0),
        MODEL,
    )
    assert constrained.check is None
    assert any(
        failure.startswith(("source budget", "input budget"))
        for diagnostic in constrained.diagnostics
        for failure in diagnostic.failures
    )


@pytest.mark.parametrize("legacy_version", [2, 3, 4, 5, 6])
def test_fresh_v7_primary_accepts_only_the_derived_legacy_context_default(
    tmp_path: Path,
    legacy_version: Literal[2, 3, 4, 5, 6],
) -> None:
    store, _root = stored(tmp_path)
    legacy = replace(limits(output=8_192, version=legacy_version), context_tokens=58_448)
    unlimited = replace(legacy, output_tokens=None, output_version=7, context_tokens=50_256)
    written = preflight.prepare_preflight(store, unlimited, MODEL)

    assert preflight.load_preflight(store, legacy, MODEL)[0] == written
    for changed in (
        replace(legacy, source_tokens=legacy.source_tokens - 1),
        replace(legacy, input_tokens=legacy.input_tokens - 1),
        replace(legacy, context_tokens=58_447),
    ):
        with pytest.raises(preflight.IdentityMismatch, match="budget limits"):
            preflight.load_preflight(store, changed, MODEL)


def test_v7_receipt_preserves_custom_input_safety_and_ratio_with_historical_context_default(
    tmp_path: Path,
) -> None:
    store, _root = stored(tmp_path)
    legacy = replace(
        limits(output=8_192, version=3),
        input_tokens=40_000,
        context_tokens=58_448,
        safety_tokens=512,
        target_ratio=2.0,
    )
    unlimited = replace(legacy, output_tokens=None, output_version=7, context_tokens=50_256)
    written = preflight.prepare_preflight(store, unlimited, MODEL)

    assert preflight.load_preflight(store, legacy, MODEL)[0] == written
    for changed in (
        replace(legacy, input_tokens=39_999),
        replace(legacy, safety_tokens=511),
        replace(legacy, target_ratio=1.9),
    ):
        with pytest.raises(preflight.IdentityMismatch, match="budget limits"):
            preflight.load_preflight(store, changed, MODEL)
