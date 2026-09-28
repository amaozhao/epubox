"""Crash-safe local JSON storage for v2.3 runs."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal, TypeVar

from pydantic import BaseModel

from engine.schemas.v23 import (
    Attempt,
    BookPlan,
    Counters,
    CutPlan,
    DocumentPlan,
    DocumentStatus,
    ItemRecord,
    JsonValue,
    RequestManifest,
    Unit,
    UnitRecord,
    Usage,
    canonical_hash,
    canonical_json_bytes,
    compute_input_hash,
    strict_json_loads,
    unit_record_hash,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class StoreError(RuntimeError):
    pass


class StoreLocked(StoreError):
    pass


class CorruptRecord(StoreError):
    pass


class StaleWrite(StoreError):
    pass


class IdentityMismatch(StoreError):
    pass


def _safe_id(value: str) -> str:
    if value in {".", ".."} or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"unsafe record id: {value!r}")
    return value


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        for directory in ("documents", "units", "checks", "requests", "staging"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self._lock_handle: BinaryIO | None = None
        self._lock_depth = 0

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        lock_path = self.root / ".store.lock"
        handle = lock_path.open("a+b")
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as error:
            handle.close()
            raise StoreLocked(f"run store is locked: {self.root}") from error
        self._lock_handle = handle
        self._lock_depth = 1
        try:
            yield
        finally:
            self._lock_depth = 0
            self._lock_handle = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _path(self, directory: str, record_id: str) -> Path:
        return self.root / directory / f"{_safe_id(record_id)}.json"

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _atomic_write(cls, path: Path, value: BaseModel | dict[str, JsonValue]) -> str:
        data = canonical_json_bytes(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            cls._sync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return canonical_hash(value)

    @staticmethod
    def _decode(raw: bytes, path: Path, model: type[ModelT]) -> ModelT:
        try:
            value = strict_json_loads(raw)
            if not isinstance(value, dict):
                raise TypeError("record must be a JSON object")
            return model.model_validate(value)
        except Exception as error:
            raise CorruptRecord(f"invalid {path}: {error}") from error

    @classmethod
    def _read(cls, path: Path, model: type[ModelT]) -> ModelT:
        return cls._decode(path.read_bytes(), path, model)

    def write_document(self, plan: DocumentPlan) -> str:
        with self.lock():
            path = self._path("documents", plan.document_id)
            if path.exists():
                existing = self._read(path, DocumentPlan)
                if existing == plan:
                    return canonical_hash(existing)
                raise StaleWrite(f"immutable DocumentPlan already exists: {plan.document_id}")
            return self._atomic_write(path, plan)

    def read_document(self, document_id: str, *, expected_hash: str | None = None) -> DocumentPlan:
        path = self._path("documents", document_id)
        raw = path.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if expected_hash is not None and actual_hash != expected_hash:
            raise CorruptRecord(f"document hash mismatch for {document_id}")
        return self._decode(raw, path, DocumentPlan)

    def restore_document_exact(self, plan: DocumentPlan) -> str:
        """Explicitly restore one ready-run DocumentPlan only when its bytes match BookPlan."""
        with self.lock():
            bookplan = self.read_bookplan(ready=True)
            try:
                expected_hash = bookplan.document_hashes[plan.document_id]
            except KeyError as error:
                raise IdentityMismatch(f"document is not registered in ready BookPlan: {plan.document_id}") from error
            if plan.source_hash != bookplan.source_hash:
                raise IdentityMismatch(f"document source mismatch: {plan.document_id}")
            data = canonical_json_bytes(plan)
            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != expected_hash:
                raise IdentityMismatch(f"rebuilt DocumentPlan hash mismatch: {plan.document_id}")
            restored_hash = self._atomic_write(self._path("documents", plan.document_id), plan)
            if restored_hash != expected_hash:
                raise CorruptRecord(f"restored DocumentPlan hash mismatch: {plan.document_id}")
            return restored_hash

    def replace_building_document(self, plan: DocumentPlan) -> str:
        """Replace a first-pass plan only while preparation is uncommitted and no request exists."""
        with self.lock():
            bookplan = self.read_bookplan()
            if bookplan.preparation_state != "building":
                raise StaleWrite("ready DocumentPlan is immutable")
            if any((self.root / "requests").glob("*.json")):
                raise StaleWrite("cannot replace DocumentPlan after request creation")
            path = self._path("documents", plan.document_id)
            if not path.exists():
                raise FileNotFoundError(path)
            try:
                current = self._read(path, DocumentPlan)
            except CorruptRecord:
                current = None
            if current is not None:
                mutable_fields = {"derived_bindings", "preparation_issues"}
                current_source = current.model_dump(exclude=mutable_fields)
                replacement_source = plan.model_dump(exclude=mutable_fields)
                if current_source != replacement_source:
                    raise IdentityMismatch("building DocumentPlan replacement changed frozen source plan")
            return self._atomic_write(path, plan)

    def write_bookplan(self, plan: BookPlan) -> str:
        with self.lock():
            path = self.root / "bookplan.json"
            unchanged = False
            if path.exists():
                existing = self._read(path, BookPlan)
                if existing == plan:
                    unchanged = True
                    if existing.preparation_state != "ready":
                        return canonical_hash(existing)
                elif existing.preparation_state == "ready":
                    raise StaleWrite("ready BookPlan is immutable")
            if plan.preparation_state == "ready":
                planned_units: set[str] = set()
                source_units: dict[str, Unit] = {}
                for document_id, expected_hash in plan.document_hashes.items():
                    document = self.read_document(document_id, expected_hash=expected_hash)
                    if document.source_hash != plan.source_hash:
                        raise IdentityMismatch(f"document source mismatch: {document_id}")
                    for unit in document.units:
                        if unit.unit_id in source_units:
                            raise IdentityMismatch(f"duplicate unit across DocumentPlans: {unit.unit_id}")
                        planned_units.add(unit.unit_id)
                        source_units[unit.unit_id] = unit
                        if plan.unit_documents.get(unit.unit_id) != document_id:
                            raise IdentityMismatch(f"unit document mismatch: {unit.unit_id}")
                if planned_units != set(plan.unit_ids):
                    raise IdentityMismatch("BookPlan unit inventory differs from its DocumentPlans")
                for unit_id in plan.unit_ids:
                    record = self.load_unit(unit_id, unit=source_units[unit_id])
                    if record.source_hash != plan.source_hash or record.document_id != plan.unit_documents[unit_id]:
                        raise IdentityMismatch(f"initial UnitRecord identity mismatch: {unit_id}")
            if unchanged:
                return canonical_hash(plan)
            return self._atomic_write(path, plan)

    def read_bookplan(self, *, ready: bool = False) -> BookPlan:
        plan = self._read(self.root / "bookplan.json", BookPlan)
        if ready and plan.preparation_state != "ready":
            raise CorruptRecord("bookplan is not ready")
        return plan

    @staticmethod
    def _with_record_hash(record: UnitRecord) -> UnitRecord:
        return record.model_copy(update={"record_hash": unit_record_hash(record)})

    @staticmethod
    def _validate_record_hash(record: UnitRecord) -> None:
        if record.record_hash is None or record.record_hash != unit_record_hash(record):
            raise CorruptRecord(f"unit record hash mismatch for {record.unit_id}")

    def initialize_unit(
        self,
        record: UnitRecord | Unit,
        cut_plan: CutPlan | None = None,
        *,
        source_hash: str | None = None,
    ) -> UnitRecord:
        source_unit = record if isinstance(record, Unit) else None
        if isinstance(record, Unit):
            if cut_plan is None or source_hash is None:
                raise ValueError("initializing from Unit requires cut_plan and source_hash")
            record = UnitRecord(
                unit_id=record.unit_id,
                document_id=record.document_id,
                source_hash=source_hash,
                logical_hash=record.logical_hash,
                input_hash=compute_input_hash(record.logical_hash, cut_plan.plan_hash),
                plan_epoch=cut_plan.plan_epoch,
                revision=0,
                cut_plan=cut_plan,
                items={
                    segment.item_id: ItemRecord(item_id=segment.item_id, segment_id=segment.segment_id)
                    for segment in cut_plan.segments
                },
                counters=Counters(unit_http_limit=24 * max(1, len(cut_plan.segments))),
            )
        if source_unit is not None:
            self.validate_unit_record(source_unit, record)
        path = self._path("units", record.unit_id)
        with self.lock():
            if path.exists():
                existing = self._read(path, UnitRecord)
                self._validate_record_hash(existing)
                if existing == self._with_record_hash(record):
                    return existing
                raise StaleWrite(f"unit already exists: {record.unit_id}")
            bookplan_path = self.root / "bookplan.json"
            if bookplan_path.exists() and self._read(bookplan_path, BookPlan).preparation_state == "ready":
                raise StaleWrite("cannot initialize a missing UnitRecord after BookPlan is ready")
            if record.record_version != 0 or record.record_hash is not None:
                raise ValueError("initial UnitRecord must start at record_version zero without a record_hash")
            initial_counts = record.counters.model_dump(exclude={"unit_http_limit"})
            if any(initial_counts.values()) or record.candidate is not None or record.accepted_revision is not None:
                raise ValueError("initial UnitRecord cannot contain spent counters or accepted target state")
            stored = self._with_record_hash(record)
            self._atomic_write(path, stored)
            return stored

    @staticmethod
    def validate_unit_record(unit: Unit, record: UnitRecord) -> None:
        if (record.unit_id, record.document_id, record.logical_hash) != (
            unit.unit_id,
            unit.document_id,
            unit.logical_hash,
        ):
            raise IdentityMismatch(f"UnitRecord source identity mismatch: {unit.unit_id}")
        if record.cut_plan is None:
            if record.input_hash is not None or record.items:
                raise IdentityMismatch(f"unplanned UnitRecord has executable state: {unit.unit_id}")
            return
        from engine.item.planner import validate_cut_plan

        validate_cut_plan(unit, record.cut_plan)
        expected_input_hash = compute_input_hash(unit.logical_hash, record.cut_plan.plan_hash)
        if record.input_hash != expected_input_hash:
            raise IdentityMismatch(f"UnitRecord input hash mismatch: {unit.unit_id}")
        segments = {segment.item_id: segment for segment in record.cut_plan.segments}
        if set(record.items) != set(segments):
            raise IdentityMismatch(f"UnitRecord item inventory differs from CutPlan: {unit.unit_id}")
        for item_id, item in record.items.items():
            if item.segment_id != segments[item_id].segment_id:
                raise IdentityMismatch(f"ItemRecord segment mismatch: {unit.unit_id}/{item_id}")

    def load_unit(self, unit_id: str, *, unit: Unit | None = None) -> UnitRecord:
        record = self._read(self._path("units", unit_id), UnitRecord)
        self._validate_record_hash(record)
        if unit is not None:
            self.validate_unit_record(unit, record)
        return record

    @staticmethod
    def _validate_monotonic_update(current: UnitRecord, updated: UnitRecord) -> None:
        if (current.unit_id, current.document_id, current.source_hash, current.logical_hash) != (
            updated.unit_id,
            updated.document_id,
            updated.source_hash,
            updated.logical_hash,
        ):
            raise IdentityMismatch("immutable UnitRecord identity changed")
        if updated.plan_epoch < current.plan_epoch or updated.revision < current.revision:
            raise StaleWrite("plan_epoch and revision cannot decrease")
        if updated.plan_epoch > current.plan_epoch and updated.revision <= current.revision:
            raise StaleWrite("replanning must start a new target revision")
        current_counters = current.counters.model_dump()
        updated_counters = updated.counters.model_dump()
        for key, old_value in current_counters.items():
            new_value = updated_counters[key]
            if isinstance(old_value, int) and new_value < old_value:
                raise StaleWrite(f"counter cannot decrease: {key}")
        for item_id, current_item in current.items.items():
            updated_item = updated.items.get(item_id)
            if updated_item is None and updated.plan_epoch == current.plan_epoch:
                raise StaleWrite(f"current CutPlan item cannot be removed: {item_id}")
            if updated_item is None:
                continue
            for stage, old_count in current_item.attempts.items():
                if updated_item.attempts.get(stage, 0) < old_count:
                    raise StaleWrite(f"item attempt counter cannot decrease: {item_id}/{stage}")
        if len(updated.history) < len(current.history) or updated.history[: len(current.history)] != current.history:
            raise StaleWrite("UnitRecord history is append-only")

    def save_unit(self, record: UnitRecord, *, expected_record_version: int | None = None) -> UnitRecord:
        path = self._path("units", record.unit_id)
        with self.lock():
            current = self._read(path, UnitRecord)
            self._validate_record_hash(current)
            expected = record.record_version if expected_record_version is None else expected_record_version
            if current.record_version != expected:
                raise StaleWrite(
                    f"unit {record.unit_id} record_version is {current.record_version}, expected {expected}"
                )
            self._validate_monotonic_update(current, record)
            stored = self._with_record_hash(record.model_copy(update={"record_version": current.record_version + 1}))
            self._atomic_write(path, stored)
            return stored

    def merge_item(
        self,
        unit_id: str,
        item: ItemRecord,
        *,
        plan_epoch: int,
        revision: int,
        input_hash: str,
    ) -> UnitRecord:
        path = self._path("units", unit_id)
        with self.lock():
            current = self._read(path, UnitRecord)
            self._validate_record_hash(current)
            if (current.plan_epoch, current.revision, current.input_hash) != (plan_epoch, revision, input_hash):
                raise StaleWrite(f"late item result rejected for {unit_id}/{item.item_id}")
            if current.cut_plan is None or item.item_id not in {
                segment.item_id for segment in current.cut_plan.segments
            }:
                raise IdentityMismatch(f"item does not belong to current CutPlan: {item.item_id}")
            previous = current.items.get(item.item_id)
            if previous == item:
                return current
            if previous is not None:
                for stage, count in previous.attempts.items():
                    if item.attempts.get(stage, 0) < count:
                        raise StaleWrite(f"item attempt counter cannot decrease: {item.item_id}/{stage}")
            if previous is not None and previous.target_hash is not None and previous.target_hash != item.target_hash:
                raise StaleWrite(f"conflicting result for {unit_id}/{item.item_id}")
            items = dict(current.items)
            items[item.item_id] = item
            stored = self._with_record_hash(
                current.model_copy(update={"items": items, "record_version": current.record_version + 1})
            )
            self._atomic_write(path, stored)
            return stored

    def write_request(self, manifest: RequestManifest) -> RequestManifest:
        path = self._path("requests", manifest.request_id)
        with self.lock():
            if path.exists():
                existing = self._read(path, RequestManifest)
                if existing == manifest:
                    return existing
                raise StaleWrite(f"request already exists: {manifest.request_id}")
            self._atomic_write(path, manifest)
            return manifest

    def read_request(self, request_id: str) -> RequestManifest:
        return self._read(self._path("requests", request_id), RequestManifest)

    def reserve_attempt(self, request_id: str, attempt: Attempt) -> RequestManifest:
        if attempt.state != "reserved":
            raise ValueError("new attempts must be reserved before dispatch")
        path = self._path("requests", request_id)
        with self.lock():
            manifest = self._read(path, RequestManifest)
            existing = next((entry for entry in manifest.attempts if entry.attempt_id == attempt.attempt_id), None)
            if existing is not None:
                if existing == attempt:
                    return manifest
                raise StaleWrite(f"attempt_id already has different data: {attempt.attempt_id}")
            if not set(attempt.affected_items).issubset(manifest.item_ids):
                raise IdentityMismatch("attempt affects items outside its request")
            updated = manifest.model_copy(update={"attempts": (*manifest.attempts, attempt)})
            self._atomic_write(path, updated)
            return updated

    def finish_attempt(
        self,
        request_id: str,
        attempt_id: str,
        *,
        state: Literal["sent", "succeeded", "failed", "unknown"],
        usage: Usage | None = None,
        error: str | None = None,
        sent_at: str | None = None,
        finished_at: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> RequestManifest:
        path = self._path("requests", request_id)
        with self.lock():
            manifest = self._read(path, RequestManifest)
            attempts = list(manifest.attempts)
            for index, attempt in enumerate(attempts):
                if attempt.attempt_id != attempt_id:
                    continue
                updated_attempt = attempt.model_copy(
                    update={
                        "state": state,
                        "usage": usage if usage is not None else attempt.usage,
                        "error": error,
                        "sent_at": sent_at if sent_at is not None else attempt.sent_at,
                        "finished_at": finished_at if finished_at is not None else attempt.finished_at,
                        "metadata": metadata if metadata is not None else attempt.metadata,
                    }
                )
                if attempt == updated_attempt:
                    return manifest
                if attempt.state in {"succeeded", "failed"}:
                    raise StaleWrite(f"attempt already finished: {attempt_id}")
                attempts[index] = updated_attempt
                updated = manifest.model_copy(update={"attempts": tuple(attempts)})
                self._atomic_write(path, updated)
                return updated
            raise IdentityMismatch(f"unknown attempt: {attempt_id}")

    def write_document_status(self, status: DocumentStatus) -> DocumentStatus:
        with self.lock():
            path = self._path("checks", status.document_id)
            if path.exists():
                current = self._read(path, DocumentStatus)
                if status.http_attempts < current.http_attempts or status.repair_rounds < current.repair_rounds:
                    raise StaleWrite("DocumentStatus attempt counters cannot decrease")
            self._atomic_write(path, status)
        return status

    def read_document_status(self, document_id: str) -> DocumentStatus:
        return self._read(self._path("checks", document_id), DocumentStatus)

    def write_report(self, report: dict[str, JsonValue]) -> str:
        with self.lock():
            return self._atomic_write(self.root / "report.json", report)

    def scan_documents(self, document_hashes: dict[str, str]) -> tuple[dict[str, DocumentPlan], dict[str, str]]:
        valid: dict[str, DocumentPlan] = {}
        invalid: dict[str, str] = {}
        for document_id, expected_hash in document_hashes.items():
            try:
                valid[document_id] = self.read_document(document_id, expected_hash=expected_hash)
            except (FileNotFoundError, StoreError, ValueError) as error:
                invalid[document_id] = str(error)
        return valid, invalid

    def scan_units(
        self,
        unit_ids: tuple[str, ...] | list[str],
        *,
        units: dict[str, Unit] | None = None,
    ) -> tuple[dict[str, UnitRecord], dict[str, str]]:
        valid: dict[str, UnitRecord] = {}
        invalid: dict[str, str] = {}
        resolved_units = units
        expected_source_hash: str | None = None
        if (self.root / "bookplan.json").exists():
            plan = self.read_bookplan()
            if plan.preparation_state == "ready":
                expected_source_hash = plan.source_hash
                if resolved_units is None:
                    documents, _ = self.scan_documents(plan.document_hashes)
                    resolved_units = {unit.unit_id: unit for document in documents.values() for unit in document.units}
        for unit_id in unit_ids:
            try:
                unit = resolved_units.get(unit_id) if resolved_units is not None else None
                if resolved_units is not None and unit is None:
                    raise IdentityMismatch(f"source Unit unavailable for persisted record: {unit_id}")
                record = self.load_unit(unit_id, unit=unit)
                if expected_source_hash is not None and record.source_hash != expected_source_hash:
                    raise IdentityMismatch(f"UnitRecord source hash mismatch: {unit_id}")
                valid[unit_id] = record
            except (FileNotFoundError, StoreError, ValueError) as error:
                invalid[unit_id] = str(error)
        return valid, invalid


__all__ = [
    "CorruptRecord",
    "IdentityMismatch",
    "StaleWrite",
    "Store",
    "StoreError",
    "StoreLocked",
]
