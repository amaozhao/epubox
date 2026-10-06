"""Trusted persistence for deterministic terminology plans."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.item.atoms import extract_resource
from engine.schemas.bridge import AtomicDocument
from engine.schemas.budget import BudgetLimits
from engine.schemas.contracts import DocumentPlan, JsonValue, PreparationPlan, TermExtractionPlan, parse_contract
from engine.services.atomic import IdentityMismatch
from engine.services.preflight import require_preflight
from engine.services.terms.planning import plan_atomic_terms, plan_term_extraction

if TYPE_CHECKING:
    from engine.services.store import RunStore


def write_plan(store: RunStore, plan: TermExtractionPlan) -> str:
    """Persist exactly one replayed legacy or receipt-bound atomic term plan."""
    preparation = store.read_preparation()
    preparation_hash = store._file_hash(store.root / "preparation.json")
    if plan.source_hash != preparation.source_hash or plan.preparation_hash != preparation_hash:
        raise IdentityMismatch("term plan does not belong to the committed preparation")

    trusted, documents = store._preparation_documents()
    atomic = {document.extractor_version for document in documents.values()} == {ATOMIC_EXTRACTOR_VERSION}
    if atomic:
        inventories = (
            canonical_documents(store, trusted, documents)
            if (store.root / "glossary" / "plan.json").is_file()
            else atomic_documents(store, trusted, documents)
        )
        expected = _atomic_plan(store, trusted, inventories, preparation_hash)
    else:
        trusted, documents = store._trusted_preparation_documents()
        expected = _legacy_plan(store, trusted, documents, preparation_hash)
    if plan != expected:
        kind = "atomic" if atomic else "P1"
        raise IdentityMismatch(f"term plan differs from the deterministic {kind} coverage plan")
    return store._write_immutable(
        store.root / "glossary" / "plan.json",
        plan,
        TermExtractionPlan,
        "epubox-term-plan-1",
    )


def atomic_documents(
    store: RunStore,
    preparation: PreparationPlan | None = None,
    documents: dict[str, DocumentPlan] | None = None,
) -> tuple[AtomicDocument, ...]:
    """Return receipt-verified inventories only when they are the committed P1 documents."""
    preparation = preparation or store.read_preparation()
    report = require_preflight(store, _limits(store, preparation.translation_config), _model(preparation))
    if report.check is None:
        raise IdentityMismatch("atomic documents require a passing preflight receipt")
    inventory_ids = set(report.check.map_hashes)
    disk_ids = {path.stem for path in (store.root / "inventories").glob("*.json")}
    if inventory_ids != disk_ids:
        raise IdentityMismatch("atomic inventory differs from the preflight receipt")
    by_id = {
        document_id: parse_contract(
            store._path("inventories", document_id).read_bytes(),
            AtomicDocument,
            "epubox-atoms-1",
        )
        for document_id in inventory_ids
    }
    documents = documents or {
        document_id: store.read_document(document_id, expected_hash=digest)
        for document_id, digest in preparation.document_hashes.items()
    }
    if set(by_id) != set(documents) or any(
        by_id[document_id].document != document for document_id, document in documents.items()
    ):
        raise IdentityMismatch("atomic inventory documents differ from the committed preparation")
    return tuple(by_id[document_id] for document_id in _ordered_ids(preparation))


def canonical_documents(
    store: RunStore,
    preparation: PreparationPlan,
    documents: dict[str, DocumentPlan],
) -> tuple[AtomicDocument, ...]:
    """Re-extract committed atomic documents from source bytes without a paid-work receipt."""
    try:
        with zipfile.ZipFile(store.root / preparation.source_path) as archive:
            by_id = {
                document_id: extract_resource(
                    archive.read(document.resource.path),
                    document.resource.path,
                    preparation.source_hash,
                    document.resource.media_type,
                )
                for document_id, document in documents.items()
            }
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise IdentityMismatch("atomic document resource is unavailable in source snapshot") from error
    if any(inventory.document != documents[document_id] for document_id, inventory in by_id.items()):
        raise IdentityMismatch("atomic documents differ from canonical source extraction")
    return tuple(by_id[document_id] for document_id in _ordered_ids(preparation))


def _atomic_plan(
    store: RunStore,
    preparation: PreparationPlan,
    inventories: tuple[AtomicDocument, ...],
    preparation_hash: str,
) -> TermExtractionPlan:
    return plan_atomic_terms(
        inventories,
        preparation.user_terms,
        source_hash=preparation.source_hash,
        preparation_hash=preparation_hash,
        auto_extract=store._config_bool(preparation.extraction_config, "auto_extract", True),
        max_primary_chars=store._config_int(preparation.extraction_config, "max_primary_chars", 12_000),
        adjacent_context_views=store._config_int(preparation.extraction_config, "adjacent_context_views", 2),
        context_chars=store._config_int(preparation.extraction_config, "context_chars", 400),
        item_http_limit=store._config_int(preparation.extraction_config, "item_http_limit", 6),
        resolution_group_limit=store._config_int(preparation.extraction_config, "resolution_group_limit", 20),
        extraction_identity=preparation.extraction_config,
    ).plan


def _legacy_plan(store: RunStore, preparation, documents, preparation_hash: str) -> TermExtractionPlan:
    ordered_ids = _ordered_ids(preparation)
    return plan_term_extraction(
        tuple(documents[document_id] for document_id in ordered_ids),
        preparation.user_terms,
        source_hash=preparation.source_hash,
        preparation_hash=preparation_hash,
        auto_extract=store._config_bool(preparation.extraction_config, "auto_extract", True),
        max_primary_chars=store._config_int(preparation.extraction_config, "max_primary_chars", 12_000),
        adjacent_context_views=store._config_int(preparation.extraction_config, "adjacent_context_views", 1),
        context_chars=store._config_int(preparation.extraction_config, "context_chars", 400),
        item_http_limit=store._config_int(preparation.extraction_config, "item_http_limit", 6),
        resolution_group_limit=store._config_int(preparation.extraction_config, "resolution_group_limit", 20),
        reading_edges=tuple(zip(preparation.reading_order, preparation.reading_order[1:], strict=False)),
        extraction_identity=preparation.extraction_config,
    ).plan


def _ordered_ids(preparation) -> tuple[str, ...]:
    return (
        *preparation.reading_order,
        *(document_id for document_id in preparation.document_hashes if document_id not in preparation.reading_order),
    )


def _limits(store: RunStore, config: dict[str, JsonValue]) -> BudgetLimits:
    from engine.services.ready import limits_from_config

    return limits_from_config(config)


def _model(preparation) -> str:
    value = preparation.translation_config.get("model", preparation.extraction_config.get("model"))
    if not isinstance(value, str) or not value:
        raise IdentityMismatch("atomic term plan requires a frozen model")
    return value


__all__ = ["atomic_documents", "canonical_documents", "write_plan"]
