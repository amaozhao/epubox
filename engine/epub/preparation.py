from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import unquote, urlsplit

from engine.epub.validation import EpubChecker, PackageInventory, ZipLimits, inspect_epub
from engine.item.extractor import ADAPTER_VERSION, select_primary_title
from engine.schemas.v23 import (
    BookPlan,
    Counters,
    CutPlan,
    DocumentPlan,
    DocumentStatus,
    FailureRecord,
    JsonValue,
    ResourceRecord,
    RunConfig,
    UnitRecord,
    canonical_hash,
)
from engine.services.store import CorruptRecord, StaleWrite, Store, StoreError


@dataclass(frozen=True)
class PreparationConfig:
    run: RunConfig
    adapter_version: str = ADAPTER_VERSION
    output_policy_hash: str = "preserve-source-resources-1"
    run_id: str | None = None
    zip_limits: ZipLimits = field(default_factory=ZipLimits)
    terms: tuple[dict[str, JsonValue], ...] = ()
    translate_exceptions: tuple[dict[str, JsonValue], ...] = ()


@dataclass(frozen=True)
class PreparedBook:
    work_dir: Path
    source_snapshot: Path
    inventory: PackageInventory
    bookplan: BookPlan


def prepare_book(
    source: Path,
    work_root: Path,
    config: PreparationConfig,
    checker: EpubChecker | object,
    *,
    extract_document: Callable[..., DocumentPlan],
    plan_unit: Callable[..., CutPlan],
    planner_config: object,
    _source_path: str | None = None,
) -> PreparedBook:
    """Snapshot, validate, persist every document, then commit the ready BookPlan last."""
    source = source.resolve(strict=True)
    if config.run.target_language != "zh-Hans":
        raise ValueError(f"Unsupported target language: {config.run.target_language}")
    work_root.mkdir(parents=True, exist_ok=True)
    temporary_snapshot, source_hash = _stable_snapshot(source, work_root)
    run_id = config.run_id or uuid.uuid4().hex
    work_dir = work_root / source_hash / run_id
    snapshot = work_dir / "source.epub"
    try:
        work_dir.mkdir(parents=True, exist_ok=False)
        os.replace(temporary_snapshot, snapshot)
        snapshot.chmod(0o444)
    except FileExistsError:
        temporary_snapshot.unlink(missing_ok=True)
        if not snapshot.is_file() or _sha256_file(snapshot) != source_hash:
            raise OSError("Existing preparation snapshot does not match the source")
    except BaseException:
        temporary_snapshot.unlink(missing_ok=True)
        raise

    store = Store(work_dir)
    with store.lock(blocking=False):
        inventory = inspect_epub(snapshot, source_hash, checker=checker, limits=config.zip_limits)
        resources = _resource_records(snapshot, inventory)
        building_config = _frozen_config(config)
        base = BookPlan(
            source_hash=source_hash,
            source_path=_source_path or str(source),
            source_epub_version=inventory.epub_version,
            run_id=run_id,
            resources=resources,
            reading_order=tuple(_manifest_path(inventory, item_id) for item_id in inventory.spine),
            required_unit_count=0,
            frozen_config=building_config,
            output_policy_hash=config.output_policy_hash,
            preparation_issues=_inventory_issues(inventory),
        )
        bookplan_path = work_dir / "bookplan.json"
        if bookplan_path.exists():
            bookplan = store.read_bookplan()
            _validate_resumable_preparation(bookplan, base, store)
        else:
            store.write_bookplan(base)
            bookplan = base

        documents = _existing_documents(store, source_hash, bookplan.document_hashes)
        by_resource = {document.resource.path: document for document in documents}
        records: dict[str, UnitRecord] = {}
        document_hashes = dict(bookplan.document_hashes)
        unit_ids = list(bookplan.unit_ids)
        unit_documents = dict(bookplan.unit_documents)

        with zipfile.ZipFile(snapshot) as archive:
            manifest_by_path = {item.path: item for item in inventory.manifest}
            book_title = _book_title(archive.read(inventory.opf_path).decode("utf-8"))
            styles = _stylesheets(archive, inventory)
            document_paths = tuple(dict.fromkeys((*inventory.documents, inventory.ncx_path, inventory.opf_path)))
            for resource_path in (path for path in document_paths if path):
                raw = archive.read(resource_path)
                document = by_resource.get(resource_path)
                if document is None or document.source_markup.encode("utf-8") != raw:
                    document = extract_document(
                        raw.decode("utf-8"),
                        resource_path,
                        source_hash,
                        media_type=resources[resource_path].media_type,
                        config={
                            **config.run.model_dump(mode="json"),
                            "adapter_version": config.adapter_version,
                            "book_title": book_title,
                            "terms": list(config.terms),
                            "translate_exceptions": list(config.translate_exceptions),
                            "properties": list(manifest_by_path.get(resource_path, _empty_manifest_item()).properties),
                        },
                        styles=styles,
                    )
                    _validate_document_version(document, config)
                    document_hash = _write_preparation_document(store, document)
                    document = store.read_document(document.document_id, expected_hash=document_hash)
                    by_resource[resource_path] = document
                    if all(existing.document_id != document.document_id for existing in documents):
                        documents.append(document)
                else:
                    _validate_document_version(document, config)
                    document_hash = canonical_hash(document)

                document, document_records = _initialize_document_units(
                    store, document, source_hash, plan_unit, planner_config
                )
                if canonical_hash(document) != document_hash:
                    document_hash = store.replace_building_document(document)
                    by_resource[resource_path] = document
                    documents = [document if item.document_id == document.document_id else item for item in documents]
                document_hashes[document.document_id] = document_hash
                for unit_id, record in document_records.items():
                    records[unit_id] = record
                    if unit_id not in unit_ids:
                        unit_ids.append(unit_id)
                    unit_documents[unit_id] = document.document_id
                bookplan = base.model_copy(
                    update={
                        "document_hashes": document_hashes,
                        "unit_ids": tuple(unit_ids),
                        "unit_documents": unit_documents,
                        "required_unit_count": len(unit_ids),
                    }
                )
                store.write_bookplan(bookplan)

        documents = [by_resource[path] for path in document_paths if path]
        documents = _resolve_derived_bindings(documents)
        document_hashes = {}
        for document in documents:
            document_hashes[document.document_id] = _write_preparation_document(store, document)
            for unit in document.units:
                records[unit.unit_id] = store.load_unit(unit.unit_id, unit=unit)
        unit_ids = [unit.unit_id for document in documents for unit in document.units]
        unit_documents = {unit.unit_id: document.document_id for document in documents for unit in document.units}
        bookplan = bookplan.model_copy(
            update={
                "document_hashes": document_hashes,
                "unit_ids": tuple(unit_ids),
                "unit_documents": unit_documents,
                "required_unit_count": len(unit_ids),
            }
        )
        store.write_bookplan(bookplan)

        from engine.item.planner import initial_coherence_windows

        total_coherence_limit = 0
        initial_coherence_limits: dict[str, int] = {}
        initial_window_ids: dict[str, tuple[str, ...]] = {}
        for document in documents:
            windows = initial_coherence_windows(document, records)
            http_limit = 6 * len(windows)
            total_coherence_limit += http_limit
            initial_coherence_limits[document.document_id] = http_limit
            initial_window_ids[document.document_id] = tuple(str(window["item_id"]) for window in windows)
            store.write_document_status(
                DocumentStatus(
                    document_id=document.document_id,
                    windows=windows,
                    http_limit=http_limit,
                    status="pending" if windows else "valid",
                )
            )

        default_run_limit = sum(record.counters.unit_http_limit for record in records.values()) + total_coherence_limit
        resolved_run = config.run.model_copy(
            update={
                "run_http_limit": min(config.run.run_http_limit, default_run_limit)
                if config.run.run_http_limit
                else default_run_limit,
                "coherence_http_limit": min(config.run.coherence_http_limit, total_coherence_limit)
                if config.run.coherence_http_limit
                else total_coherence_limit,
                "generation": building_config["generation"],
            }
        )
        bookplan = bookplan.model_copy(
            update={
                "preparation_state": "ready",
                "document_hashes": document_hashes,
                "initial_coherence_limits": initial_coherence_limits,
                "initial_coherence_windows": initial_window_ids,
                "frozen_config": resolved_run.model_dump(mode="json"),
            }
        )
        store.write_bookplan(bookplan)
        if store.read_bookplan(ready=True) != bookplan:
            raise OSError("BookPlan round-trip mismatch")
        return PreparedBook(work_dir, snapshot, inventory, bookplan)


def resume_preparation(
    work_dir: Path,
    checker: EpubChecker | object,
    *,
    extract_document: Callable[..., DocumentPlan],
    plan_unit: Callable[..., CutPlan],
    planner_config: object,
) -> PreparedBook:
    """Finish a trusted building run using only its immutable source snapshot."""
    work_dir = work_dir.resolve(strict=True)
    store = Store(work_dir)
    bookplan = store.read_bookplan()
    if bookplan.preparation_state != "building":
        raise StaleWrite("ready preparation cannot be restarted")
    snapshot = work_dir / "source.epub"
    if not snapshot.is_file() or _sha256_file(snapshot) != bookplan.source_hash:
        raise OSError("Preparation source snapshot hash mismatch")
    expected_dir = work_dir.parent.parent / bookplan.source_hash / bookplan.run_id
    if expected_dir != work_dir:
        raise ValueError("Work directory does not match its BookPlan identity")
    run = RunConfig.model_validate(bookplan.frozen_config)
    terms = _config_entries(run.generation.get("terms"), "terms")
    exceptions = _config_entries(run.generation.get("translate_exceptions"), "translate_exceptions")
    return prepare_book(
        snapshot,
        work_dir.parent.parent,
        PreparationConfig(
            run=run,
            output_policy_hash=bookplan.output_policy_hash,
            run_id=bookplan.run_id,
            terms=terms,
            translate_exceptions=exceptions,
        ),
        checker,
        extract_document=extract_document,
        plan_unit=plan_unit,
        planner_config=planner_config,
        _source_path=bookplan.source_path,
    )


def repair_document(
    store: Store,
    document_id: str,
    checker: EpubChecker | object,
    *,
    extract_document: Callable[..., DocumentPlan],
    plan_unit: Callable[..., CutPlan],
    planner_config: object,
) -> str:
    """Explicitly restore one ready DocumentPlan only after an exact deterministic rebuild."""
    bookplan = store.read_bookplan(ready=True)
    expected_hash = bookplan.document_hashes.get(document_id)
    if expected_hash is None:
        raise ValueError(f"Document is not registered by the ready BookPlan: {document_id}")
    snapshot = store.root / "source.epub"
    if not snapshot.is_file() or _sha256_file(snapshot) != bookplan.source_hash:
        raise OSError("Preparation source snapshot hash mismatch")
    run = RunConfig.model_validate(bookplan.frozen_config)
    terms = _config_entries(run.generation.get("terms"), "terms")
    exceptions = _config_entries(run.generation.get("translate_exceptions"), "translate_exceptions")
    with tempfile.TemporaryDirectory(prefix="epubox-document-repair-") as temporary:
        rebuilt = prepare_book(
            snapshot,
            Path(temporary),
            PreparationConfig(
                run=run,
                output_policy_hash=bookplan.output_policy_hash,
                run_id=f"repair-{uuid.uuid4().hex}",
                terms=terms,
                translate_exceptions=exceptions,
            ),
            checker,
            extract_document=extract_document,
            plan_unit=plan_unit,
            planner_config=planner_config,
        )
        rebuilt_store = Store(rebuilt.work_dir)
        candidate = rebuilt_store.read_document(document_id)
        if canonical_hash(candidate) != expected_hash:
            raise ValueError("Rebuilt DocumentPlan does not match the ready BookPlan; start a new run")
        return store.restore_document_exact(candidate)


def _resource_records(snapshot: Path, inventory: PackageInventory) -> dict[str, ResourceRecord]:
    manifest_by_path = {item.path: item for item in inventory.manifest}
    linear_by_id = inventory.spine_linear
    with zipfile.ZipFile(snapshot) as archive:
        records: dict[str, ResourceRecord] = {}
        for path in inventory.entries:
            if archive.getinfo(path).is_dir():
                continue
            item = manifest_by_path.get(path)
            records[path] = ResourceRecord(
                path=path,
                media_type=item.media_type if item else _container_media_type(path),
                source_sha256=hashlib.sha256(archive.read(path)).hexdigest(),
                properties=item.properties if item else (),
                linear=linear_by_id.get(item.item_id) if item else None,
            )
        return records


def _validate_document_version(document: DocumentPlan, config: PreparationConfig) -> None:
    if document.extractor_version != config.run.extractor_version:
        raise ValueError(f"Extractor version mismatch: {document.extractor_version} != {config.run.extractor_version}")
    if document.adapter_version != config.adapter_version:
        raise ValueError(f"Adapter version mismatch: {document.adapter_version} != {config.adapter_version}")


def _frozen_config(config: PreparationConfig) -> dict[str, JsonValue]:
    generation = dict(config.run.generation)
    if config.terms:
        generation["terms"] = list(config.terms)
    if config.translate_exceptions:
        generation["translate_exceptions"] = list(config.translate_exceptions)
    return config.run.model_copy(update={"generation": generation}).model_dump(mode="json")


def _config_entries(value: JsonValue | None, name: str) -> tuple[dict[str, JsonValue], ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Frozen {name} must be a list of objects")
    return tuple(dict(cast("dict[str, JsonValue]", item)) for item in value)


def _inventory_issues(inventory: PackageInventory) -> tuple[dict[str, JsonValue], ...]:
    return tuple(
        {
            "scope": "book",
            "stage": "preparation",
            "code": warning.code,
            "message": warning.message,
            "severity": warning.severity,
            "resource": warning.resource,
        }
        for warning in inventory.warnings
    )


def _validate_resumable_preparation(bookplan: BookPlan, expected: BookPlan, store: Store) -> None:
    if bookplan.preparation_state == "ready":
        raise StaleWrite("ready preparation cannot be restarted")
    identity = (
        bookplan.source_hash,
        bookplan.source_path,
        bookplan.source_epub_version,
        bookplan.run_id,
        bookplan.resources,
        bookplan.reading_order,
        bookplan.frozen_config,
        bookplan.output_policy_hash,
    )
    expected_identity = (
        expected.source_hash,
        expected.source_path,
        expected.source_epub_version,
        expected.run_id,
        expected.resources,
        expected.reading_order,
        expected.frozen_config,
        expected.output_policy_hash,
    )
    if identity != expected_identity:
        raise StaleWrite("unfinished preparation identity/configuration changed")
    if any((store.root / "requests").glob("*.json")):
        raise StaleWrite("unfinished preparation has request records")


def _existing_documents(
    store: Store,
    source_hash: str,
    document_hashes: dict[str, str],
) -> list[DocumentPlan]:
    documents: list[DocumentPlan] = []
    for document_id, expected_hash in sorted(document_hashes.items()):
        try:
            document = store.read_document(document_id, expected_hash=expected_hash)
        except (OSError, ValueError, StoreError):
            continue
        if document.source_hash == source_hash:
            documents.append(document)
    return documents


def _write_preparation_document(store: Store, document: DocumentPlan) -> str:
    try:
        return store.write_document(document)
    except (CorruptRecord, StaleWrite):
        return store.replace_building_document(document)


def _initialize_document_units(
    store: Store,
    document: DocumentPlan,
    source_hash: str,
    plan_unit: Callable[..., CutPlan],
    planner_config: object,
) -> tuple[DocumentPlan, dict[str, UnitRecord]]:
    from engine.item.planner import PlanningError

    records: dict[str, UnitRecord] = {}
    issues = list(document.preparation_issues)
    for unit in document.units:
        unit_path = store.root / "units" / f"{unit.unit_id}.json"
        if unit_path.exists():
            record = store.load_unit(unit.unit_id, unit=unit)
        else:
            try:
                cut_plan = plan_unit(unit, planner_config, epoch=0)
                record = store.initialize_unit(unit, cut_plan, source_hash=source_hash)
            except PlanningError as error:
                failure = FailureRecord(
                    scope="unit",
                    stage="preparation",
                    code="unit_planning_failed",
                    message=str(error),
                    plan_epoch=0,
                    revision=0,
                    retry_action="repair",
                )
                record = store.initialize_unit(
                    UnitRecord(
                        unit_id=unit.unit_id,
                        document_id=unit.document_id,
                        source_hash=source_hash,
                        logical_hash=unit.logical_hash,
                        plan_epoch=0,
                        unresolved_issues=(failure,),
                        counters=Counters(unit_http_limit=24),
                    )
                )
        records[unit.unit_id] = record
        if record.cut_plan is None and not any(issue.get("unit_id") == unit.unit_id for issue in issues):
            issues.append(
                {
                    "scope": "unit",
                    "stage": "preparation",
                    "code": "unit_planning_failed",
                    "message": record.unresolved_issues[0].message
                    if record.unresolved_issues
                    else "Unit is unplanned",
                    "unit_id": unit.unit_id,
                    "node_key": unit.node_key,
                    "resource": document.resource.path,
                }
            )
    return document.model_copy(update={"preparation_issues": tuple(issues)}), records


def _stable_snapshot(source: Path, work_root: Path) -> tuple[Path, str]:
    temporary = work_root / f".source-{uuid.uuid4().hex}.tmp"
    try:
        for _ in range(3):
            temporary.unlink(missing_ok=True)
            before = _sha256_file(source)
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
            after = _sha256_file(source)
            if before == after == _sha256_file(temporary):
                return temporary, before
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink(missing_ok=True)
    raise OSError("Source EPUB kept changing while creating immutable snapshot")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(inventory: PackageInventory, item_id: str) -> str:
    return next(item.path for item in inventory.manifest if item.item_id == item_id)


def _container_media_type(path: str) -> str:
    if path == "mimetype":
        return "text/plain"
    if path.endswith(".xml"):
        return "application/xml"
    if path.endswith(".opf"):
        return "application/oebps-package+xml"
    return "application/octet-stream"


def _book_title(opf_markup: str) -> str:
    from engine.core.markup import parse_xml_safely

    root = parse_xml_safely(opf_markup).getroot()
    title = select_primary_title(root)
    return "" if title is None else "".join(title.itertext()).strip()


def _stylesheets(archive: zipfile.ZipFile, inventory: PackageInventory) -> dict[str, str]:
    return {
        item.path: archive.read(item.path).decode("utf-8")
        for item in inventory.manifest
        if item.media_type == "text/css"
    }


def _resolve_derived_bindings(documents: list[DocumentPlan]) -> list[DocumentPlan]:
    from engine.item.inline import parse_projection

    unit_by_id = {unit.unit_id: unit for document in documents for unit in document.units}
    titles: dict[tuple[str, str, str], list[str]] = {}
    for document in documents:
        for candidate in document.derived_bindings:
            if candidate.get("kind") != "title_candidate":
                continue
            unit_id = str(candidate.get("source_unit_id", ""))
            unit = unit_by_id.get(unit_id)
            if unit is None or any(entry.kind == "x" for entry in unit.registry.values()):
                continue
            key = (
                document.resource.path,
                str(candidate.get("fragment", "")),
                str(candidate.get("source_text", "")),
            )
            titles.setdefault(key, []).append(unit_id)

    resolved: list[DocumentPlan] = []
    for document in documents:
        bindings: list[dict[str, JsonValue]] = [
            dict(candidate) for candidate in document.derived_bindings if candidate.get("kind") != "derived_navigation"
        ]
        for candidate in document.derived_bindings:
            if candidate.get("kind") != "href_candidate":
                continue
            target_unit_id = str(candidate.get("source_unit_id", ""))
            target_unit = unit_by_id.get(target_unit_id)
            if target_unit is None or any(entry.kind == "x" for entry in target_unit.registry.values()):
                continue
            nonempty_text_events = [
                event
                for event in parse_projection(target_unit.source_projection)
                if event.kind == "text" and event.value.strip()
            ]
            if len(nonempty_text_events) != 1:
                continue
            parsed = urlsplit(str(candidate.get("href", "")))
            if parsed.scheme or parsed.netloc:
                continue
            resource = (
                _resolve_resource(document.resource.path, parsed.path) if parsed.path else document.resource.path
            )
            key = (resource, unquote(parsed.fragment), str(candidate.get("source_text", "")))
            matches = titles.get(key, [])
            if len(matches) != 1:
                continue
            bindings.append(
                {
                    "kind": "derived_navigation",
                    "unit_id": target_unit_id,
                    "source_unit_id": matches[0],
                    "source_resource": document.resource.path,
                    "target_resource": resource,
                    "fragment": unquote(parsed.fragment),
                    "source_text": key[2],
                }
            )
        resolved.append(document.model_copy(update={"derived_bindings": tuple(bindings)}))
    return resolved


def _resolve_resource(base_resource: str, href: str) -> str:
    path = PurePosixPath(base_resource).parent / unquote(href)
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError(f"Resource reference escapes package: {href}")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


class _EmptyManifestItem:
    properties: tuple[str, ...] = ()


def _empty_manifest_item() -> _EmptyManifestItem:
    return _EmptyManifestItem()


__all__ = ["PreparationConfig", "PreparedBook", "prepare_book", "repair_document", "resume_preparation"]
