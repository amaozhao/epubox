from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from engine.epub.preparation import PreparationConfig, prepare_book
from engine.item.atoms import extract_resource
from engine.item.extractor import extract_document
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import PreparationPlan, canonical_hash, canonical_json_bytes
from engine.services import preflight as preflight_module
from engine.services.atomic import IdentityMismatch
from engine.services.preflight import preflight_atomic_resources, prepare_preflight, require_preflight, write_preflight
from engine.services.store import RunStore
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker

XHTML = "http://www.w3.org/1999/xhtml"


def limits(source: int) -> BudgetLimits:
    return BudgetLimits(
        source_tokens=source,
        input_tokens=50_000,
        output_tokens=10_000,
        context_tokens=60_000,
    )


def inventory(raw: bytes, source_hash: str, path: str = "OEBPS/chapter.xhtml"):
    return extract_resource(raw, path, source_hash)


def source(body: str) -> bytes:
    return f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()


def stored(tmp_path: Path, raw: bytes, *, translation: dict | None = None):
    epub = make_epub(tmp_path / "input.epub", {"chapter.xhtml": raw.decode()})
    snapshot = tmp_path / "source.epub"
    snapshot.write_bytes(epub.read_bytes())
    source_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    atoms = inventory(raw, source_hash)
    legacy = extract_document(raw.decode(), "OEBPS/chapter.xhtml", source_hash)
    preparation = PreparationPlan(
        source_hash=source_hash,
        source_path="source.epub",
        source_epub_version="3.0",
        run_id="run",
        document_hashes={legacy.document_id: canonical_hash(legacy)},
        reading_order=(legacy.document_id,),
        unit_documents={unit.unit_id: legacy.document_id for unit in legacy.units},
        user_terms_hash=canonical_hash(()),
        translation_config=translation or {"chunk_tokens": 5000, "model": "gpt-3.5-turbo"},
    )
    (tmp_path / "documents").mkdir()
    (tmp_path / "documents" / f"{legacy.document_id}.json").write_bytes(canonical_json_bytes(legacy))
    (tmp_path / "preparation.json").write_bytes(canonical_json_bytes(preparation))
    return RunStore(tmp_path), atoms, {"OEBPS/chapter.xhtml": raw}


def test_whole_atomic_item_fails_at_2000_and_passes_at_5000_without_splitting() -> None:
    raw = source("<p>" + "word " * 2600 + "</p>")
    atoms = inventory(raw, "book")

    blocked = preflight_atomic_resources((atoms,), {"OEBPS/chapter.xhtml": raw}, limits(2000), "gpt-3.5-turbo")
    passed = preflight_atomic_resources((atoms,), {"OEBPS/chapter.xhtml": raw}, limits(5000), "gpt-3.5-turbo")

    assert blocked.check is None
    assert blocked.map_hashes and blocked.atoms_hash and blocked.budget_hash
    assert blocked.diagnostics[0].status == "blocked"
    assert blocked.diagnostics[0].atomic_tag == "p"
    assert any(reason.startswith("source budget") for reason in blocked.diagnostics[0].failures)
    assert not blocked.pieces
    assert passed.check is not None and passed.diagnostics[0].status == "passed"
    assert len(passed.pieces) == 1 and passed.pieces[0].item_id == atoms.items[0].item_id


def test_output_shortage_is_a_named_blocking_dimension() -> None:
    raw = source("<p>Small paragraph.</p>")
    atoms = inventory(raw, "book")
    constrained = BudgetLimits(
        source_tokens=5000,
        input_tokens=50_000,
        output_tokens=1,
        context_tokens=60_000,
    )

    report = preflight_atomic_resources((atoms,), {"OEBPS/chapter.xhtml": raw}, constrained, "gpt-3.5-turbo")

    assert report.check is None
    assert any(reason.startswith("output budget") for reason in report.diagnostics[0].failures)


def test_virtual_text_uses_only_verified_source_boundaries_and_stable_pieces() -> None:
    paragraph = "word " * 850
    raw = source('<a href="#linked">linked text</a> ' + paragraph + ". " + paragraph + ". " + paragraph + ".")
    atoms = inventory(raw, "book")
    item = atoms.items[0]
    assert item.atomic_tag is None and item.region.get("safe_boundaries") and item.registry

    first = preflight_atomic_resources((atoms,), {"OEBPS/chapter.xhtml": raw}, limits(2000), "gpt-3.5-turbo")
    second = preflight_atomic_resources((atoms,), {"OEBPS/chapter.xhtml": raw}, limits(2000), "gpt-3.5-turbo")

    assert first == second and first.check is not None
    assert first.diagnostics[0].status == "split"
    assert len(first.pieces) == 2
    assert all(piece.translate.fits and piece.review.fits for piece in first.pieces)
    assert "".join(piece.source_projection for piece in first.pieces) == item.source_projection
    assert all(piece.piece_id.startswith("pc-") for piece in first.pieces)

    data = atoms.model_dump(mode="python")
    data["items"][0]["region"]["safe_boundaries"][0]["byte_offset"] += 1
    data["document"]["units"][0]["region"]["safe_boundaries"][0]["byte_offset"] += 1
    tampered = type(atoms).model_validate(data)
    with pytest.raises(IdentityMismatch, match="safe source boundaries changed"):
        preflight_atomic_resources((tampered,), {"OEBPS/chapter.xhtml": raw}, limits(2000), "gpt-3.5-turbo")


def test_persisted_guard_recomputes_source_atoms_budget_and_frozen_configuration(tmp_path: Path) -> None:
    raw = source("<p>Persisted paragraph.</p>")
    store, atoms, _resources = stored(tmp_path, raw)
    preparation = PreparationPlan.model_validate_json((tmp_path / "preparation.json").read_bytes())
    assert set(preparation.document_hashes) != {atoms.document.document_id}

    written = prepare_preflight(store, limits(5000), "gpt-3.5-turbo")
    validated = require_preflight(store, limits(5000), "gpt-3.5-turbo")

    assert written == validated and validated.check is not None
    assert (tmp_path / "checks" / "preflight.json").is_file()
    assert (tmp_path / "inventories" / f"{atoms.document.document_id}.json").is_file()
    with pytest.raises(IdentityMismatch, match="receipt"):
        require_preflight(store, limits(2000), "gpt-3.5-turbo")

    changed = preparation.model_copy(update={"translation_config": {"chunk_tokens": 2000}})
    (tmp_path / "preparation.json").write_bytes(canonical_json_bytes(changed))
    with pytest.raises(IdentityMismatch, match="preparation identity"):
        require_preflight(store, limits(5000), "gpt-3.5-turbo")


def test_persist_rejects_resource_bytes_not_taken_from_the_frozen_snapshot(tmp_path: Path) -> None:
    raw = source("<p>Frozen paragraph.</p>")
    store, atoms, resources = stored(tmp_path, raw)
    changed = source("<p>Changed paragraph.</p>")

    with pytest.raises((IdentityMismatch, ValueError), match="resource|raw resource"):
        write_preflight(
            store,
            (atoms,),
            {next(iter(resources)): changed},
            limits(5000),
            "gpt-3.5-turbo",
        )

    with zipfile.ZipFile(tmp_path / "source.epub") as archive:
        assert archive.read("OEBPS/chapter.xhtml") == raw


def test_guard_rejects_self_consistent_inventory_that_omits_source_atoms(tmp_path: Path) -> None:
    raw = source('<p>Required paragraph.</p><p id="skip">Omitted paragraph.</p>')
    store, complete, resources = stored(tmp_path, raw)
    prepare_preflight(store, limits(5000), "gpt-3.5-turbo")
    forged = extract_resource(
        raw,
        complete.document.resource.path,
        complete.document.source_hash,
        config={"translate_exceptions": {"#skip": "keep"}},
    )
    assert len(forged.items) < len(complete.items)
    with pytest.raises(IdentityMismatch, match="canonical extraction"):
        write_preflight(store, (forged,), resources, limits(5000), "gpt-3.5-turbo")

    forged_report = preflight_atomic_resources((forged,), resources, limits(5000), "gpt-3.5-turbo")
    preparation_raw = (tmp_path / "preparation.json").read_bytes()
    preparation = PreparationPlan.model_validate_json(preparation_raw)
    record = preflight_module._PreflightRecord(
        preparation_hash=hashlib.sha256(preparation_raw).hexdigest(),
        translation_hash=canonical_hash(preparation.translation_config),
        report=forged_report,
    )
    inventory_path = tmp_path / "inventories" / f"{forged.document.document_id}.json"
    inventory_path.write_bytes(canonical_json_bytes(forged))
    (tmp_path / "checks" / "preflight.json").write_bytes(canonical_json_bytes(record))

    with pytest.raises(IdentityMismatch, match="canonical extraction"):
        require_preflight(store, limits(5000), "gpt-3.5-turbo")


def test_prepare_covers_every_xhtml_navigation_and_metadata_resource(tmp_path: Path) -> None:
    source_path = make_epub(tmp_path / "book.epub")
    prepared = prepare_book(
        source_path,
        tmp_path / "work",
        PreparationConfig(run_id="run", translation_config={"model": "gpt-3.5-turbo"}),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)

    report = prepare_preflight(store, limits(5000), "gpt-3.5-turbo")

    assert report.check is not None
    assert set(report.resource_hashes) == {
        store.read_document(document_id).resource.path for document_id in store.read_preparation().document_hashes
    }
    assert require_preflight(store, limits(5000), "gpt-3.5-turbo") == report
