from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.core.markup import find_by_element_path, parse_xml_safely, qname_local_name, serialize_xml
from engine.epub.assembly import assemble_document, derive_navigation_projection
from engine.epub.validation import (
    EpubCheckResult,
    EpubValidationError,
    PackageInventory,
    ValidationIssue,
    inspect_epub,
    validate_internal_references,
)
from engine.item.inline import plain_text, projection_identities, validate_projection
from engine.schemas.v25 import (
    BOOK_FORMAT,
    DOCUMENT_FORMAT,
    BookPlan,
    DocumentPlan,
    ItemStatus,
    SourceSlot,
    Unit,
    UnitRecord,
    canonical_hash,
    strict_json_loads,
)
from engine.services.store_v25 import StoreV25

_TRANSLATABLE_ATTRIBUTES = {"alt", "title", "aria-label", "aria-description"}
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


@dataclass(frozen=True)
class PackageVerification:
    output_hash: str
    epubcheck: EpubCheckResult
    checked_documents: tuple[str, ...]
    warnings: tuple[ValidationIssue, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "output_hash": self.output_hash,
            "epubcheck": self.epubcheck.to_dict(),
            "checked_documents": list(self.checked_documents),
            "warnings": [warning.__dict__ for warning in self.warnings],
        }


def validate_assembled_document(
    document: DocumentPlan,
    accepted_targets: Mapping[str, str],
    actual_markup: str,
    *,
    source_to_target: Mapping[str, str] | None = None,
    changeset: Mapping[tuple[str, str], str] | Sequence[tuple[str, str]] = (),
) -> None:
    """Re-read actual XML and prove it consumes the immutable plan and accepted targets."""
    if document.format != DOCUMENT_FORMAT:
        raise TypeError(f"publication validation requires {DOCUMENT_FORMAT}")
    source_tree = parse_xml_safely(document.source_markup)
    actual_tree = parse_xml_safely(actual_markup)
    changed = set(changeset)
    expected_changes = changeset if isinstance(changeset, Mapping) else {}
    actual_nodes: dict[str, etree._Element] = {}

    for node_key, record in document.nodes.items():
        source_node = find_by_element_path(source_tree, record.element_path)
        actual_node = _actual_node(actual_tree, record.element_path, (source_to_target or {}).get(node_key))
        if str(source_node.tag) != record.qname or str(actual_node.tag) != record.qname:
            raise EpubValidationError("node_identity_changed", f"Node identity changed: {node_key}")
        actual_nodes[node_key] = actual_node
        source_attributes = {
            name: value
            for name, value in source_node.attrib.items()
            if qname_local_name(name) not in _TRANSLATABLE_ATTRIBUTES and (node_key, name) not in changed
        }
        actual_attributes = {
            name: value
            for name, value in actual_node.attrib.items()
            if qname_local_name(name) not in _TRANSLATABLE_ATTRIBUTES and (node_key, name) not in changed
        }
        if source_attributes != actual_attributes:
            raise EpubValidationError("frozen_attribute_changed", f"Frozen attributes changed: {node_key}")
        for (changed_node, attribute), expected in expected_changes.items():
            if (
                changed_node == node_key
                and attribute not in {"text", "tail"}
                and actual_node.get(attribute) != expected
            ):
                raise EpubValidationError("changeset_mismatch", f"Declared change is absent: {node_key}/{attribute}")

    if len({id(node) for node in actual_nodes.values()}) != len(actual_nodes):
        raise EpubValidationError("duplicate_sidecar_target", "Multiple source nodes map to one output node")

    units = {unit.unit_id: unit for unit in document.units}
    if set(accepted_targets) != set(units):
        missing = set(units) - set(accepted_targets)
        extra = set(accepted_targets) - set(units)
        raise EpubValidationError(
            "unit_consumption_mismatch",
            f"Accepted target inventory mismatch (missing={sorted(missing)}, extra={sorted(extra)})",
        )

    for slot in document.source_slots.values():
        changed_key = (slot.node_key, slot.attribute_name or slot.field)
        if changed_key in expected_changes:
            if _document_slot_value(document, actual_nodes, slot) != expected_changes[changed_key]:
                raise EpubValidationError("changeset_mismatch", f"Declared slot change is absent: {slot.slot_id}")
            continue
        if any(item.owner_kind == "unit" for item in slot.ranges):
            continue
        if _document_slot_value(document, actual_nodes, slot) != slot.source_value:
            raise EpubValidationError("protected_slot_changed", f"Protected source slot changed: {slot.slot_id}")

    for unit_id, target in accepted_targets.items():
        unit = units[unit_id]
        events = validate_projection(unit, target)
        if unit.kind == "attribute":
            slot = document.source_slots[unit.slot_ids[0]]
            if _document_slot_value(document, actual_nodes, slot) != plain_text(target):
                raise EpubValidationError("target_attribute_mismatch", f"Target attribute mismatch: {unit_id}")
            continue
        expected_identities = tuple(
            ref
            for ref in projection_identities(target)
            if ref in unit.registry and "element" in unit.registry[ref].hints
        )
        if _unit_element_identities(unit, actual_nodes) != expected_identities:
            raise EpubValidationError("inline_identity_mismatch", f"Inline object order mismatch: {unit_id}")
        actual_text = _unit_region_text(unit, actual_nodes)
        if actual_text != _expected_unit_text(document, unit, events):
            raise EpubValidationError("target_text_mismatch", f"Target text mismatch: {unit_id}")


def _expected_unit_text(document: DocumentPlan, unit: Unit, events: Sequence[object]) -> str:
    parts: list[str] = []
    for event in events:
        kind = getattr(event, "kind", None)
        value = getattr(event, "value", None)
        if not isinstance(kind, str) or not isinstance(value, str):
            raise EpubValidationError("invalid_projection_event", "Projection event is invalid")
        if kind == "text":
            parts.append(value)
            continue
        if not value.startswith("=x"):
            continue
        entry = unit.registry[value[1:]]
        slot_id = entry.hints.get("slot_id")
        if slot_id is None:
            continue
        slot = document.source_slots.get(slot_id)
        if slot is None:
            raise EpubValidationError("invalid_slot_reference", f"Unknown protected source slot: {slot_id}")
        try:
            start = int(entry.hints.get("start", "0"))
            end = int(entry.hints.get("end", str(len(slot.source_value))))
        except ValueError as error:
            raise EpubValidationError(
                "invalid_slot_reference", f"Invalid protected source range: {slot_id}"
            ) from error
        if start < 0 or end < start or end > len(slot.source_value):
            raise EpubValidationError("invalid_slot_reference", f"Protected source range is out of bounds: {slot_id}")
        source_text = slot.source_value[start:end]
        if source_text != entry.source_text:
            raise EpubValidationError("invalid_slot_reference", f"Protected source range does not match: {slot_id}")
        parts.append(source_text)
    return "".join(parts)


def stage_epub(
    source_snapshot: Path,
    staged_path: Path,
    replacements: Mapping[str, bytes],
) -> str:
    """Create a candidate EPUB without modifying the source snapshot or untouched resources."""
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_snapshot) as source:
        names = set(source.namelist())
        unknown = set(replacements) - names
        if unknown:
            raise EpubValidationError("unknown_replacement", f"Replacement is not in source EPUB: {min(unknown)}")
        with zipfile.ZipFile(staged_path, "w") as target:
            mimetype = replacements.get("mimetype", source.read("mimetype"))
            if mimetype != b"application/epub+zip":
                raise EpubValidationError("invalid_mimetype", "mimetype cannot be changed")
            target.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
            for info in source.infolist():
                if info.filename == "mimetype":
                    continue
                data = replacements.get(info.filename, source.read(info.filename))
                copied = zipfile.ZipInfo(info.filename, info.date_time)
                copied.comment = info.comment
                copied.extra = info.extra
                copied.internal_attr = info.internal_attr
                copied.external_attr = info.external_attr
                copied.create_system = info.create_system
                copied.flag_bits = info.flag_bits & ~0x1
                target.writestr(copied, data, compress_type=info.compress_type)
    return _sha256_file(staged_path)


def publish_book(
    store: StoreV25,
    output_path: Path,
    checker: object,
    *,
    overwrite: bool = False,
    identity: bool = False,
) -> dict[str, object]:
    """Publish only trusted v2.5 source plans and current accepted UnitRecord targets."""
    if not isinstance(store, StoreV25):
        raise TypeError("publication requires StoreV25")
    plan = store.read_bookplan()
    if plan.format != BOOK_FORMAT:
        raise TypeError(f"publication requires {BOOK_FORMAT}")
    snapshot = store.root / "source.epub"
    inventory = inspect_epub(snapshot, plan.source_hash, checker=checker)
    replacements: dict[str, bytes] = {}
    accepted_by_resource: dict[str, dict[str, str]] = {}
    version_vector: dict[str, int] = {}
    records = {} if identity else {unit_id: store.read_unit(unit_id) for unit_id in plan.unit_ids}
    assembled_documents: dict[str, tuple[DocumentPlan, dict[str, str], dict[str, str]]] = {}

    for document_id, expected_hash in plan.document_hashes.items():
        document = store.read_document(document_id, expected_hash=expected_hash)
        if document.format != DOCUMENT_FORMAT:
            raise TypeError(f"publication requires {DOCUMENT_FORMAT}")
        if not identity:
            _validate_document_coherence(store, document, records)
        document_targets: dict[str, str] = {}
        for unit in document.units:
            if identity:
                document_targets[unit.unit_id] = unit.source_projection
                version_vector[unit.unit_id] = 0
                continue
            record = records[unit.unit_id]
            target = _accepted_target(
                store,
                plan,
                record,
                planned=plan.initial_unit_plans[unit.unit_id] is not None,
            )
            if target is None:
                target = _derived_navigation_target(store, plan, document, unit, record, records)
            if target is None:
                raise EpubValidationError("unit_not_accepted", f"Unit is not currently accepted: {unit.unit_id}")
            document_targets[unit.unit_id] = target
            version_vector[unit.unit_id] = record.revision
        assembled = assemble_document(document, document_targets, identity=identity)
        replacements[document.resource.path] = assembled.markup.encode("utf-8")
        accepted_by_resource[document.resource.path] = document_targets
        assembled_documents[document.resource.path] = (document, document_targets, assembled.source_to_target)

    changesets: dict[str, dict[tuple[str, str], str]] = {}
    if not identity:
        replacements, changesets = _finalize_replacements(inventory, replacements, assembled_documents)
    for path, (document, document_targets, sidecar) in assembled_documents.items():
        validate_assembled_document(
            document,
            document_targets,
            replacements[path].decode("utf-8"),
            source_to_target=sidecar,
            changeset=changesets.get(path, {}),
        )
    staged_path = store.root / "staging" / f"candidate-{uuid.uuid4().hex}.epub"
    stage_epub(snapshot, staged_path, replacements)
    verification = verify_staged_epub(
        snapshot,
        staged_path,
        inventory,
        replacements,
        accepted_targets=accepted_by_resource,
        checker=checker,
        expected_language=None if identity else "zh-Hans",
    )
    if not identity:
        current_records = {unit_id: store.read_unit(unit_id) for unit_id in plan.unit_ids}
        if current_records != records:
            changed = next(unit_id for unit_id in plan.unit_ids if current_records[unit_id] != records[unit_id])
            raise EpubValidationError(
                "unit_changed_during_publication",
                f"Unit changed while publication was being verified: {changed}",
            )
        for document, document_targets, _ in assembled_documents.values():
            for unit in document.units:
                current = current_records[unit.unit_id]
                target = _accepted_target(
                    store,
                    plan,
                    current,
                    planned=plan.initial_unit_plans[unit.unit_id] is not None,
                ) or _derived_navigation_target(store, plan, document, unit, current, current_records)
                if target != document_targets[unit.unit_id]:
                    unit_id = unit.unit_id
                    raise EpubValidationError(
                        "unit_changed_during_publication",
                        f"Unit changed while publication was being verified: {unit_id}",
                    )
    intent = publish_verified(
        staged_path,
        output_path,
        store.root / "publish.json",
        run_id=plan.run_id,
        plan_fingerprint=canonical_hash(plan),
        version_vector=version_vector,
        verification=verification,
        forbidden_paths=(Path(store.read_preparation().source_path), snapshot),
        overwrite=overwrite,
    )
    return {
        "path": str(output_path),
        "sha256": verification.output_hash,
        "verification": verification.to_dict(),
        "publish": intent,
    }


def _accepted_target(store: StoreV25, plan: BookPlan, record: UnitRecord, *, planned: bool) -> str | None:
    target = record.candidate
    if target is None or record.accepted_revision != record.revision:
        return None
    target_hash = canonical_hash(target)
    if record.accepted_target_hash != target_hash or record.unresolved_issues:
        return None
    if record.local_checks.get("passed") is not True or record.local_checks.get("target_hash") != target_hash:
        return None
    review = record.review
    if (
        review is None
        or review.get("protocol") != "epubox-review-2"
        or review.get("plan_epoch") != record.plan_epoch
        or review.get("passed") is not True
        or review.get("revision") != record.revision
        or review.get("input_hash") != record.input_hash
        or review.get("target_hash") != target_hash
    ):
        return None
    if planned and (not record.items or any(item.status != ItemStatus.REVIEWED for item in record.items.values())):
        return None
    raw_item_reviews = review.get("item_reviews")
    if not isinstance(raw_item_reviews, dict):
        return None
    expected_items = set(record.items) if planned else set(raw_item_reviews)
    if not expected_items or set(raw_item_reviews) != expected_items:
        return None
    for item_id in sorted(expected_items):
        item_review = raw_item_reviews.get(item_id)
        item_target_hash = record.items[item_id].target_hash if planned else target_hash
        if (
            not isinstance(item_review, dict)
            or not isinstance(item_review.get("request_id"), str)
            or item_review.get("target_hash") != item_target_hash
            or item_target_hash is None
        ):
            return None
        try:
            manifest = store.read_request(str(item_review["request_id"]))
        except (FileNotFoundError, ValueError):
            return None
        if (
            manifest.stage != "review"
            or manifest.freeze_id != plan.freeze_id
            or manifest.glossary_file_sha256 != plan.glossary_file_sha256
            or manifest.plan_epochs.get(record.unit_id) != record.plan_epoch
            or manifest.revisions.get(record.unit_id) != record.revision
            or manifest.unit_document_ids.get(record.unit_id) != record.document_id
            or manifest.item_unit_ids.get(item_id) != (record.unit_id,)
            or manifest.target_hashes.get(item_id) != item_target_hash
            or not any(
                attempt.state == "succeeded" and item_id in attempt.affected_items for attempt in manifest.attempts
            )
        ):
            return None
    return target


def _derived_navigation_target(
    store: StoreV25,
    plan: BookPlan,
    document: DocumentPlan,
    unit: Unit,
    record: UnitRecord,
    records: Mapping[str, UnitRecord],
) -> str | None:
    derived = record.derived
    if (
        derived is None
        or derived.get("state") != "valid"
        or record.candidate is not None
        or record.accepted_revision is not None
        or record.unresolved_issues
    ):
        return None
    source_unit_id = derived.get("source_unit_id")
    if not isinstance(source_unit_id, str):
        return None
    binding = next(
        (
            item
            for item in document.derived_bindings
            if item.get("kind") == "derived_navigation"
            and item.get("unit_id") == unit.unit_id
            and item.get("source_unit_id") == source_unit_id
        ),
        None,
    )
    source = records.get(source_unit_id)
    if binding is None or source is None or plan.unit_documents.get(source_unit_id) != source.document_id:
        return None
    source_target = _accepted_target(
        store,
        plan,
        source,
        planned=plan.initial_unit_plans[source_unit_id] is not None,
    )
    if source_target is None:
        return None
    expected = derive_navigation_projection(unit, source_target)
    expected_hash = canonical_hash(expected)
    if (
        derived.get("source_revision") != source.revision
        or derived.get("source_target_hash") != source.accepted_target_hash
        or derived.get("target") != expected
        or derived.get("target_hash") != expected_hash
    ):
        return None
    return expected


def _validate_document_coherence(
    store: StoreV25,
    document: DocumentPlan,
    records: Mapping[str, UnitRecord],
) -> None:
    path = store.root / "checks" / f"{document.document_id}.json"
    if not path.is_file():
        raise EpubValidationError("missing_coherence_check", f"Missing coherence check: {document.document_id}")
    try:
        check = strict_json_loads(path.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        raise EpubValidationError(
            "invalid_coherence_check", f"Invalid coherence check: {document.document_id}"
        ) from error
    if not isinstance(check, dict) or check.get("format") != "epubox-check-3":
        raise EpubValidationError("invalid_coherence_check", f"Invalid coherence check: {document.document_id}")
    expected_hash = canonical_hash({key: value for key, value in check.items() if key != "record_hash"})
    if check.get("record_hash") != expected_hash or check.get("document_id") != document.document_id:
        raise EpubValidationError("invalid_coherence_check", f"Coherence hash mismatch: {document.document_id}")
    windows = check.get("windows")
    expected_windows = _coherence_windows(document, records)
    if windows != expected_windows:
        raise EpubValidationError("stale_coherence_windows", f"Coherence windows changed: {document.document_id}")
    participants = {
        unit_id
        for window in expected_windows
        for unit_id in _string_list(window.get("unit_ids"), "coherence window unit_ids")
    }
    expected_versions = {unit_id: records[unit_id].revision for unit_id in sorted(participants)}
    if check.get("candidate_versions") != expected_versions:
        raise EpubValidationError("stale_coherence_versions", f"Coherence versions are stale: {document.document_id}")
    completed = check.get("checks")
    window_ids = {str(window["item_id"]) for window in expected_windows}
    if check.get("status") != "valid" or not isinstance(completed, dict) or set(completed) != window_ids:
        raise EpubValidationError("incomplete_coherence_check", f"Coherence is incomplete: {document.document_id}")
    issues: list[Mapping[str, object]] = []
    for result in completed.values():
        if not isinstance(result, dict):
            raise EpubValidationError("invalid_coherence_check", f"Invalid window result: {document.document_id}")
        raw_issues = result.get("issues")
        if not isinstance(raw_issues, list):
            raise EpubValidationError("invalid_coherence_check", f"Invalid window result: {document.document_id}")
        for issue in raw_issues:
            if not isinstance(issue, dict):
                raise EpubValidationError("invalid_coherence_check", f"Invalid window issues: {document.document_id}")
            issues.append(issue)
    if check.get("issues") != issues or any(issue.get("severity") in {"major", "critical"} for issue in issues):
        raise EpubValidationError("blocking_coherence_issue", f"Coherence has blocking issues: {document.document_id}")


def _coherence_windows(
    document: DocumentPlan,
    records: Mapping[str, UnitRecord],
) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    seen: set[str] = set()
    units = {unit.unit_id: unit for unit in document.units}
    for boundary in document.boundaries:
        edges = boundary.get("relation_edges")
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            left, right, kind = edge.get("from_unit_id"), edge.get("to_unit_id"), edge.get("kind")
            if not all(isinstance(value, str) and value for value in (left, right, kind)):
                continue
            assert isinstance(left, str) and isinstance(right, str) and isinstance(kind, str)
            item_id = "cw-" + canonical_hash([document.document_id, left, right, kind])[:24]
            if item_id in seen or left not in units or right not in units:
                continue
            seen.add(item_id)
            windows.append(
                {
                    "item_id": item_id,
                    "unit_ids": [left, right],
                    "source": [
                        _snippet(units[left].source_projection, tail=True),
                        _snippet(units[right].source_projection),
                    ],
                    "kind": kind,
                }
            )
    for unit in document.units:
        record = records.get(unit.unit_id)
        if record is None or record.cut_plan is None:
            continue
        for index, (left, right) in enumerate(zip(record.cut_plan.segments, record.cut_plan.segments[1:])):
            windows.append(
                {
                    "item_id": "cw-" + canonical_hash([document.document_id, unit.unit_id, "seam", index])[:24],
                    "unit_ids": [unit.unit_id],
                    "source": [_snippet(left.source_projection, tail=True), _snippet(right.source_projection)],
                    "kind": "segment_seam",
                    "segment_item_ids": [left.item_id, right.item_id],
                }
            )
    return windows


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EpubValidationError("invalid_coherence_check", f"{label} must be a string list")
    return value


def _snippet(value: str, *, tail: bool = False, limit: int = 800) -> str:
    return value[-limit:] if tail else value[:limit]


def verify_staged_epub(
    source_snapshot: Path,
    staged_path: Path,
    source_inventory: PackageInventory,
    expected_documents: Mapping[str, bytes],
    *,
    accepted_targets: Mapping[str, Mapping[str, str]],
    checker: object,
    expected_language: str | None = None,
) -> PackageVerification:
    """Independently compare actual package bytes/XML against planned documents and accepted targets."""
    output_hash = _sha256_file(staged_path)
    output_inventory = inspect_epub(staged_path, output_hash, checker=checker)
    if output_inventory.epub_version != source_inventory.epub_version:
        raise EpubValidationError("epub_version_changed", "Output EPUB version differs from source")
    if set(output_inventory.entries) != set(source_inventory.entries):
        raise EpubValidationError("resource_inventory_changed", "Output resource inventory differs from source")
    if output_inventory.opf_path != source_inventory.opf_path:
        raise EpubValidationError("opf_changed", "Output package document path differs from source")
    if output_inventory.spine != source_inventory.spine:
        raise EpubValidationError("spine_changed", "Output reading order differs from source")
    if output_inventory.obfuscated_fonts != source_inventory.obfuscated_fonts:
        raise EpubValidationError("font_obfuscation_changed", "Font obfuscation resources changed")

    warnings = list(output_inventory.warnings)
    with zipfile.ZipFile(source_snapshot) as source, zipfile.ZipFile(staged_path) as output:
        for name in source.namelist():
            if name not in expected_documents and source.read(name) != output.read(name):
                raise EpubValidationError("unexpected_resource_change", f"Unexpected resource change: {name}")
        for path, expected in expected_documents.items():
            actual = output.read(path)
            _compare_document(path, expected, actual)
            if path not in accepted_targets:
                raise EpubValidationError("missing_target_evidence", f"No target evidence supplied for {path}")
        reference_issues = validate_internal_references(output, output_inventory)
        if reference_issues:
            raise EpubValidationError("invalid_references", reference_issues[0].message, issues=reference_issues)
        if expected_language:
            warnings.extend(_validate_final_metadata(output, output_inventory, accepted_targets, expected_language))

    result = output_inventory.epubcheck
    if result is None or not result.passed:
        raise EpubValidationError("output_epubcheck_failed", "Output EPUBCheck did not pass")
    return PackageVerification(output_hash, result, tuple(sorted(expected_documents)), tuple(warnings))


def _finalize_replacements(
    inventory: PackageInventory,
    replacements: Mapping[str, bytes],
    assembled_documents: Mapping[str, tuple[DocumentPlan, dict[str, str], dict[str, str]]],
) -> tuple[dict[str, bytes], dict[str, dict[tuple[str, str], str]]]:
    finalized = dict(replacements)
    changesets: dict[str, dict[tuple[str, str], str]] = {}
    for path in inventory.documents:
        entry = assembled_documents.get(path)
        if entry is None:
            continue
        document, _, sidecar = entry
        assembled_markup = finalized[path].decode("utf-8")
        source_tree = parse_xml_safely(document.source_markup)
        tree = parse_xml_safely(assembled_markup)
        source_nodes = {
            node_key: find_by_element_path(source_tree, record.element_path)
            for node_key, record in document.nodes.items()
        }
        target_nodes = {
            node_key: _actual_node(tree, record.element_path, sidecar.get(node_key))
            for node_key, record in document.nodes.items()
        }
        changes: dict[tuple[str, str], str] = {}
        root_key = next(key for key, record in document.nodes.items() if not record.element_path)
        _set_language(target_nodes[root_key], root_key, "zh-Hans", changes)
        for unit in document.units:
            _set_language(target_nodes[unit.node_key], unit.node_key, "zh-Hans", changes)
        for node_key, source_node in source_nodes.items():
            if _effective_translate(source_node):
                continue
            source_language = _inherited_language(source_node) or "en"
            _set_language(target_nodes[node_key], node_key, source_language, changes)
        finalized[path] = serialize_xml(tree, source_markup=assembled_markup).encode("utf-8")
        changesets[path] = changes

    opf_document, _, opf_sidecar = assembled_documents[inventory.opf_path]
    opf_source = finalized[inventory.opf_path].decode("utf-8")
    tree = parse_xml_safely(opf_source)
    root = tree.getroot()
    opf_nodes = {
        node_key: _actual_node(tree, record.element_path, opf_sidecar.get(node_key))
        for node_key, record in opf_document.nodes.items()
    }
    node_keys = {id(node): key for key, node in opf_nodes.items()}
    changes = changesets.setdefault(inventory.opf_path, {})
    metadata = next((element for element in root.iter() if qname_local_name(element.tag) == "metadata"), None)
    if metadata is None:
        raise EpubValidationError("missing_metadata", "OPF has no metadata element")
    languages = [element for element in metadata if qname_local_name(element.tag) == "language"]
    if not languages:
        raise EpubValidationError("missing_language", "Source OPF has no dc:language")
    languages[0].text = "zh-Hans"
    changes[(node_keys[id(languages[0])], "text")] = "zh-Hans"
    if inventory.epub_version.startswith("3"):
        modified = [
            element
            for element in metadata
            if qname_local_name(element.tag) == "meta" and element.get("property") == "dcterms:modified"
        ]
        if len(modified) != 1:
            raise EpubValidationError("invalid_modified_time", "Source EPUB 3 must have one dcterms:modified")
        keeper = modified[0]
        keeper.text = _utc_now()
        changes[(node_keys[id(keeper)], "text")] = keeper.text
    finalized[inventory.opf_path] = serialize_xml(tree, source_markup=opf_source).encode("utf-8")
    return finalized, changesets


def _set_language(
    element: etree._Element,
    node_key: str,
    language: str,
    changes: dict[tuple[str, str], str],
) -> None:
    element.set("lang", language)
    element.set(_XML_LANG, language)
    changes[(node_key, "lang")] = language
    changes[(node_key, _XML_LANG)] = language


def _effective_translate(element: etree._Element) -> bool:
    translated = True
    for node in [*element.iterancestors()][::-1] + [element]:
        value = (node.get("translate") or "").strip().lower()
        if value in {"no", "false", "0"}:
            translated = False
        elif value in {"yes", "true", "1"}:
            translated = True
    return translated


def _inherited_language(element: etree._Element) -> str | None:
    current: etree._Element | None = element
    while current is not None:
        language = current.get(_XML_LANG) or current.get("lang")
        if language:
            return language
        current = current.getparent()
    return None


def _validate_final_metadata(
    archive: zipfile.ZipFile,
    inventory: PackageInventory,
    accepted_targets: Mapping[str, Mapping[str, str]],
    expected_language: str,
) -> tuple[ValidationIssue, ...]:
    opf = parse_xml_safely(archive.read(inventory.opf_path).decode("utf-8")).getroot()
    languages = [(element.text or "").strip() for element in opf.iter() if qname_local_name(element.tag) == "language"]
    if not languages or languages[0] != expected_language:
        raise EpubValidationError("target_language_missing", "OPF primary language was not finalized")
    if inventory.epub_version.startswith("3"):
        modified = [
            (element.text or "").strip()
            for element in opf.iter()
            if qname_local_name(element.tag) == "meta" and element.get("property") == "dcterms:modified"
        ]
        if len(modified) != 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", modified[0]):
            raise EpubValidationError("invalid_modified_time", "EPUB 3 requires one UTC dcterms:modified value")
    for path in inventory.documents:
        if path not in accepted_targets:
            continue
        root = parse_xml_safely(archive.read(path).decode("utf-8")).getroot()
        if root.get("lang") != expected_language or root.get(_XML_LANG) != expected_language:
            raise EpubValidationError("document_language_missing", f"Target language missing from {path}")
    has_file_as = any(
        qname_local_name(name) == "file-as" or element.get("property") == "file-as"
        for element in opf.iter()
        for name in element.attrib
    )
    return (
        (ValidationIssue("metadata_file_as_preserved", "Ambiguous file-as metadata was preserved", "warning"),)
        if has_file_as
        else ()
    )


def publish_verified(
    staged_path: Path,
    target_path: Path,
    publish_path: Path,
    *,
    run_id: str,
    plan_fingerprint: str,
    version_vector: Mapping[str, int],
    verification: PackageVerification,
    forbidden_paths: Sequence[Path] = (),
    overwrite: bool = False,
) -> dict[str, object]:
    if _sha256_file(staged_path) != verification.output_hash:
        raise EpubValidationError("staged_hash_changed", "Staged EPUB changed after verification")
    _reject_target_alias(target_path, forbidden_paths)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    intent: dict[str, object] = {
        "format": "epubox-publish-1",
        "state": "intended",
        "run_id": run_id,
        "plan_fingerprint": plan_fingerprint,
        "version_vector": dict(sorted(version_vector.items())),
        "target_path": str(target_path.absolute()),
        "target_hash": verification.output_hash,
        "verification": verification.to_dict(),
        "created_at": _utc_now(),
    }

    with _target_lock(target_path):
        _reject_target_alias(target_path, forbidden_paths)
        if target_path.exists() and not overwrite:
            raise FileExistsError(f"Output already exists: {target_path}")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output, staged_path.open("rb") as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            if _sha256_file(temporary) != verification.output_hash:
                raise OSError("Target-filesystem copy hash mismatch")
            _atomic_json(publish_path, intent)
            if overwrite:
                os.replace(temporary, target_path)
            else:
                os.link(temporary, target_path)
                temporary.unlink()
            _fsync_directory(target_path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    intent["state"] = "completed"
    intent["completed_at"] = _utc_now()
    _atomic_json(publish_path, intent)
    return intent


def recover_publication(
    publish_path: Path,
    *,
    plan_fingerprint: str,
    version_vector: Mapping[str, int],
) -> dict[str, object] | None:
    if not publish_path.is_file():
        return None
    data = json.loads(publish_path.read_text(encoding="utf-8"))
    if data.get("format") != "epubox-publish-1":
        raise EpubValidationError("invalid_publish_intent", "Unknown publish intent format")
    if data.get("plan_fingerprint") != plan_fingerprint or data.get("version_vector") != dict(
        sorted(version_vector.items())
    ):
        return None
    target = Path(str(data.get("target_path", "")))
    target_hash = data.get("target_hash")
    if not target.is_file() or not isinstance(target_hash, str) or _sha256_file(target) != target_hash:
        return None
    if data.get("state") != "completed":
        data["state"] = "completed"
        data["completed_at"] = _utc_now()
        _atomic_json(publish_path, data)
    return data


def _actual_node(
    tree: etree._ElementTree,
    source_path: tuple[int, ...],
    target_path: str | None,
) -> etree._Element:
    if target_path:
        if target_path == "/" or all(part.isdigit() for part in target_path.strip("/").split("/")):
            path = tuple(int(part) for part in target_path.strip("/").split("/") if part)
            try:
                return find_by_element_path(tree, path)
            except KeyError as error:
                raise EpubValidationError("invalid_sidecar", f"Invalid target element path: {target_path}") from error
        matches = tree.xpath(target_path)
        if len(matches) != 1 or not isinstance(matches[0], etree._Element):
            raise EpubValidationError("invalid_sidecar", f"Target sidecar path does not resolve once: {target_path}")
        return matches[0]
    try:
        return find_by_element_path(tree, source_path)
    except KeyError as error:
        raise EpubValidationError("missing_node", f"Output is missing source node at {source_path}") from error


def _slot_value(node: etree._Element, slot: SourceSlot) -> str:
    if slot.field == "text":
        return node.text or ""
    if slot.field == "tail":
        return node.tail or ""
    assert slot.attribute_name is not None
    return node.get(slot.attribute_name, "")


def _document_slot_value(
    document: DocumentPlan,
    nodes: Mapping[str, etree._Element],
    slot: SourceSlot,
) -> str:
    boundary = next(
        (
            item
            for item in document.boundaries
            if item.get("kind") == "non_element_tail" and item.get("slot_id") == slot.slot_id
        ),
        None,
    )
    if boundary is None:
        return _slot_value(nodes[slot.node_key], slot)
    parent = nodes[str(boundary["parent_node_key"])]
    raw_index = boundary["child_index"]
    if not isinstance(raw_index, int):
        raise EpubValidationError("invalid_boundary", f"Invalid comment/PI index for slot: {slot.slot_id}")
    child_index = raw_index
    try:
        return parent[child_index].tail or ""
    except IndexError as error:
        raise EpubValidationError("missing_non_element", f"Missing comment/PI for slot: {slot.slot_id}") from error


def _unit_region_text(unit: Unit, nodes: Mapping[str, etree._Element]) -> str:
    region = unit.region
    parent_key = str(region.get("parent_node_key", unit.node_key))
    parent = nodes[parent_key]
    after_key = region.get("after_node_key")
    before_key = region.get("before_node_key")
    after = nodes.get(str(after_key)) if after_key else None
    before = nodes.get(str(before_key)) if before_key else None
    children = list(parent)
    start = children.index(after) + 1 if after is not None else 0
    end = children.index(before) if before is not None else len(children)
    atom_nodes = {
        nodes[entry.source_node_key]
        for entry in unit.registry.values()
        if entry.kind == "x" and "element" in entry.hints
    }
    parts = [(parent.text or "") if after is None else (after.tail or "")]
    for child in children[start:end]:
        parts.append(_visible_text(child, atom_nodes))
        parts.append(child.tail or "")
    return "".join(parts)


def _unit_element_identities(unit: Unit, nodes: Mapping[str, etree._Element]) -> tuple[str, ...]:
    region = unit.region
    parent = nodes[str(region.get("parent_node_key", unit.node_key))]
    after_key = region.get("after_node_key")
    before_key = region.get("before_node_key")
    after = nodes.get(str(after_key)) if after_key else None
    before = nodes.get(str(before_key)) if before_key else None
    children = list(parent)
    start = children.index(after) + 1 if after is not None else 0
    end = children.index(before) if before is not None else len(children)
    refs = {nodes[entry.source_node_key]: ref for ref, entry in unit.registry.items() if "element" in entry.hints}
    return tuple(refs[element] for child in children[start:end] for element in child.iter() if element in refs)


def _visible_text(element: etree._Element, atom_nodes: set[etree._Element]) -> str:
    if element in atom_nodes or not isinstance(element.tag, str):
        return ""
    parts = [element.text or ""]
    for child in element:
        parts.append(_visible_text(child, atom_nodes))
        parts.append(child.tail or "")
    return "".join(parts)


def _compare_document(path: str, expected: bytes, actual: bytes) -> None:
    expected_root = _parse_xml(expected, path)
    actual_root = _parse_xml(actual, path)
    if _tree_signature(expected_root) != _tree_signature(actual_root):
        raise EpubValidationError("document_mismatch", f"Actual document differs from verified plan: {path}")


def _tree_signature(element: ET.Element) -> tuple[object, ...]:
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        element.text,
        element.tail,
        tuple(_tree_signature(child) for child in element),
    )


def _parse_xml(data: bytes, path: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise EpubValidationError("invalid_output_xml", f"Invalid output XML in {path}: {error}") from error


def _reject_target_alias(target: Path, forbidden: Sequence[Path]) -> None:
    resolved_target = target.resolve(strict=False)
    for path in forbidden:
        resolved_forbidden = path.resolve(strict=False)
        if resolved_target == resolved_forbidden:
            raise EpubValidationError("unsafe_output_path", f"Output aliases protected input: {path}")
        if target.exists() and path.exists() and os.path.samefile(target, path):
            raise EpubValidationError("unsafe_output_path", f"Output aliases protected input: {path}")


@contextmanager
def _target_lock(target: Path) -> Iterator[None]:
    lock_path = target.with_name(f".{target.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "PackageVerification",
    "publish_book",
    "publish_verified",
    "recover_publication",
    "stage_epub",
    "validate_assembled_document",
    "verify_staged_epub",
]
