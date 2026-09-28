"""Crash-safe storage for the v2.5 preparation and terminology gates."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict

from engine.item.source_views import validate_source_views
from engine.schemas.v25 import (
    CANDIDATES_FORMAT,
    DOCUMENT_FORMAT,
    EXTRACTION_RECORD_FORMAT,
    FREEZE_FORMAT,
    GLOSSARY_FORMAT,
    PREPARATION_FORMAT,
    REQUEST_FORMAT,
    TERM_PLAN_FORMAT,
    Attempt,
    CandidatePool,
    DocumentPlan,
    FreezeIntent,
    GlossarySnapshot,
    JsonValue,
    PreparationPlan,
    RequestManifest,
    TermExtractionPlan,
    TermExtractionRecord,
    TermPreparation,
    UnsupportedFormatError,
    Usage,
    UserTerm,
    candidate_pool_record_hash,
    canonical_hash,
    parse_contract,
    validate_term_scopes,
)
from engine.services.store import CorruptRecord, IdentityMismatch, StaleWrite, Store
from engine.services.term_planning import plan_term_extraction

ModelT = TypeVar("ModelT", bound=BaseModel)
USER_TERMS_FORMAT = "epubox-user-terms-1"


class UserTermsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["epubox-user-terms-1"] = USER_TERMS_FORMAT
    terms: tuple[UserTerm, ...] = ()


class StoreV25:
    """Persist P1-P3 without requiring a translation-ready BookPlan.

    V07 owns Unit/BookPlan writes and must call ``validate_cut_plan_coverage``
    before making any Unit executable.
    """

    def __init__(self, root: Path | str):
        self._base = Store(root)
        self.root = self._base.root
        (self.root / "glossary" / "extraction").mkdir(parents=True, exist_ok=True)

    def lock(self, *, blocking: bool = True):
        return self._base.lock(blocking=blocking)

    def _path(self, directory: str, record_id: str) -> Path:
        return self._base._path(directory, record_id)

    @staticmethod
    def _atomic_write(path: Path, value: BaseModel | dict[str, JsonValue]) -> str:
        return Store._atomic_write(path, value)

    @staticmethod
    def _file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _read_contract(path: Path, model: type[ModelT], expected_format: str) -> ModelT:
        try:
            return parse_contract(path.read_bytes(), model, expected_format)
        except UnsupportedFormatError:
            raise
        except Exception as error:
            raise CorruptRecord(f"invalid {path}: {error}") from error

    def _write_immutable(self, path: Path, value: BaseModel, model: type[ModelT], expected_format: str) -> str:
        if path.exists():
            existing = self._read_contract(path, model, expected_format)
            if existing == value:
                return self._file_hash(path)
            raise StaleWrite(f"immutable {path.name} already exists")
        return self._atomic_write(path, value)

    def write_document(self, plan: DocumentPlan) -> str:
        validate_source_views(plan)
        with self.lock():
            return self._write_immutable(
                self._path("documents", plan.document_id), plan, DocumentPlan, DOCUMENT_FORMAT
            )

    def read_document(self, document_id: str, *, expected_hash: str | None = None) -> DocumentPlan:
        path = self._path("documents", document_id)
        if expected_hash is not None and self._file_hash(path) != expected_hash:
            raise CorruptRecord(f"document hash mismatch for {document_id}")
        return self._read_contract(path, DocumentPlan, DOCUMENT_FORMAT)

    def write_user_terms(self, terms: tuple[UserTerm, ...]) -> str:
        copy = UserTermsFile(terms=terms)
        with self.lock():
            return self._write_immutable(self.root / "glossary" / "user.json", copy, UserTermsFile, USER_TERMS_FORMAT)

    def read_user_terms(self) -> UserTermsFile:
        return self._read_contract(self.root / "glossary" / "user.json", UserTermsFile, USER_TERMS_FORMAT)

    def write_preparation(self, plan: PreparationPlan) -> str:
        """Commit parsed_ready only after the complete P1 inventory is durable."""
        with self.lock():
            path = self.root / "preparation.json"
            if plan.source_path != "source.epub":
                raise IdentityMismatch("preparation source_path must name the immutable source snapshot")
            source_path = self.root / plan.source_path
            if not source_path.is_file() or self._file_hash(source_path) != plan.source_hash:
                raise IdentityMismatch("source snapshot is missing or does not match preparation source_hash")

            disk_ids = {entry.stem for entry in (self.root / "documents").glob("*.json")}
            if disk_ids != set(plan.document_hashes):
                raise IdentityMismatch("preparation document inventory differs from durable DocumentPlans")

            unit_documents: dict[str, str] = {}
            for document_id, expected_hash in plan.document_hashes.items():
                document = self.read_document(document_id, expected_hash=expected_hash)
                validate_source_views(document)
                if document.source_hash != plan.source_hash:
                    raise IdentityMismatch(f"document source mismatch: {document_id}")
                for unit in document.units:
                    if unit.unit_id in unit_documents:
                        raise IdentityMismatch(f"duplicate Unit across DocumentPlans: {unit.unit_id}")
                    unit_documents[unit.unit_id] = document_id
            if unit_documents != plan.unit_documents:
                raise IdentityMismatch("preparation Unit inventory differs from its DocumentPlans")

            user_copy = self.read_user_terms()
            if user_copy.terms != plan.user_terms or canonical_hash(user_copy.terms) != plan.user_terms_hash:
                raise IdentityMismatch("preparation user terms differ from glossary/user.json")
            validate_term_scopes(plan.user_terms, set(plan.document_hashes), set(plan.unit_documents))
            if path.exists():
                return self._write_immutable(path, plan, PreparationPlan, PREPARATION_FORMAT)
            return self._atomic_write(path, plan)

    def read_preparation(self) -> PreparationPlan:
        return self._read_contract(self.root / "preparation.json", PreparationPlan, PREPARATION_FORMAT)

    def write_term_plan(self, plan: TermExtractionPlan) -> str:
        with self.lock():
            preparation = self.read_preparation()
            if (
                preparation.source_path != "source.epub"
                or self._file_hash(self.root / preparation.source_path) != preparation.source_hash
            ):
                raise IdentityMismatch("preparation source snapshot identity changed")
            if plan.source_hash != preparation.source_hash or plan.preparation_hash != self._file_hash(
                self.root / "preparation.json"
            ):
                raise IdentityMismatch("term plan does not belong to the committed preparation")
            disk_ids = {entry.stem for entry in (self.root / "documents").glob("*.json")}
            if disk_ids != set(preparation.document_hashes):
                raise IdentityMismatch("term plan document inventory differs from preparation")
            documents = {
                document_id: self.read_document(document_id, expected_hash=document_hash)
                for document_id, document_hash in preparation.document_hashes.items()
            }
            try:
                with zipfile.ZipFile(self.root / preparation.source_path) as archive:
                    for document in documents.values():
                        validate_source_views(document)
                        raw = archive.read(document.resource.path)
                        if hashlib.sha256(
                            raw
                        ).hexdigest() != document.resource.source_sha256 or raw != document.source_markup.encode(
                            "utf-8"
                        ):
                            raise IdentityMismatch(
                                f"DocumentPlan no longer matches source snapshot: {document.document_id}"
                            )
            except (KeyError, zipfile.BadZipFile) as error:
                raise IdentityMismatch("DocumentPlan resource is unavailable in source snapshot") from error
            ordered_ids = (
                *preparation.reading_order,
                *(
                    document_id
                    for document_id in preparation.document_hashes
                    if document_id not in preparation.reading_order
                ),
            )
            reading_edges = tuple(zip(preparation.reading_order, preparation.reading_order[1:], strict=False))
            extraction_config = preparation.extraction_config
            expected = plan_term_extraction(
                tuple(documents[document_id] for document_id in ordered_ids),
                preparation.user_terms,
                source_hash=preparation.source_hash,
                preparation_hash=self._file_hash(self.root / "preparation.json"),
                auto_extract=self._config_bool(extraction_config, "auto_extract", True),
                max_primary_chars=self._config_int(extraction_config, "max_primary_chars", 12_000),
                adjacent_context_views=self._config_int(extraction_config, "adjacent_context_views", 1),
                context_chars=self._config_int(extraction_config, "context_chars", 400),
                item_http_limit=self._config_int(extraction_config, "item_http_limit", 6),
                resolution_group_limit=self._config_int(extraction_config, "resolution_group_limit", 20),
                reading_edges=reading_edges,
                extraction_identity=extraction_config,
            ).plan
            if plan != expected:
                raise IdentityMismatch("term plan differs from the deterministic P1 coverage plan")
            return self._write_immutable(
                self.root / "glossary" / "plan.json", plan, TermExtractionPlan, TERM_PLAN_FORMAT
            )

    @staticmethod
    def _config_int(config: dict[str, JsonValue], name: str, default: int) -> int:
        value = config.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise IdentityMismatch(f"invalid integer extraction configuration: {name}")
        return value

    @staticmethod
    def _config_bool(config: dict[str, JsonValue], name: str, default: bool) -> bool:
        value = config.get(name, default)
        if not isinstance(value, bool):
            raise IdentityMismatch(f"invalid boolean extraction configuration: {name}")
        return value

    def read_term_plan(self) -> TermExtractionPlan:
        return self._read_contract(self.root / "glossary" / "plan.json", TermExtractionPlan, TERM_PLAN_FORMAT)

    def save_extraction(
        self, record: TermExtractionRecord, *, expected_record_version: int | None = None
    ) -> TermExtractionRecord:
        path = self._path("glossary/extraction", record.item_id)
        with self.lock():
            if (self.root / "glossary" / "freeze.json").exists():
                raise StaleWrite("extraction records are sealed after freeze")
            item = next((item for item in self.read_term_plan().items if item.item_id == record.item_id), None)
            if item is None:
                raise IdentityMismatch(f"unknown extraction item: {record.item_id}")
            identity = (record.document_id, record.view_ids, record.extraction_input_hash)
            if identity != (item.document_id, item.view_ids, item.extraction_input_hash):
                raise IdentityMismatch(f"extraction record identity mismatch: {record.item_id}")
            if not path.exists():
                if record.record_version != 0 or expected_record_version not in {None, 0}:
                    raise StaleWrite("new extraction records must start at record_version 0")
            else:
                existing = self._read_contract(path, TermExtractionRecord, EXTRACTION_RECORD_FORMAT)
                if expected_record_version is None or existing.record_version != expected_record_version:
                    raise StaleWrite(
                        f"extraction record version changed: expected {expected_record_version}, "
                        f"found {existing.record_version}"
                    )
                if identity != (existing.document_id, existing.view_ids, existing.extraction_input_hash):
                    raise IdentityMismatch(f"immutable extraction identity changed: {record.item_id}")
                if record.record_version != existing.record_version + 1:
                    raise StaleWrite("extraction record_version must increase by exactly one")
            self._atomic_write(path, record)
            return record

    def read_extraction(self, item_id: str) -> TermExtractionRecord:
        return self._read_contract(
            self._path("glossary/extraction", item_id),
            TermExtractionRecord,
            EXTRACTION_RECORD_FORMAT,
        )

    @staticmethod
    def _with_candidate_hash(pool: CandidatePool) -> CandidatePool:
        return pool.model_copy(update={"record_hash": candidate_pool_record_hash(pool)})

    def save_candidate_pool(self, pool: CandidatePool, *, expected_record_version: int | None = None) -> CandidatePool:
        path = self.root / "glossary" / "candidates.json"
        stored = self._with_candidate_hash(pool)
        with self.lock():
            if (self.root / "glossary" / "freeze.json").exists():
                raise StaleWrite("candidate pool is sealed after freeze")
            plan = self.read_term_plan()
            if (
                stored.source_hash != plan.source_hash
                or stored.preparation_hash != plan.preparation_hash
                or stored.term_plan_hash != plan.plan_hash
            ):
                raise IdentityMismatch("candidate pool does not belong to the term plan")
            if not path.exists():
                if stored.record_version != 0 or expected_record_version not in {None, 0}:
                    raise StaleWrite("new candidate pools must start at record_version 0")
            else:
                existing = self.read_candidate_pool()
                if expected_record_version is None or existing.record_version != expected_record_version:
                    raise StaleWrite(
                        f"candidate pool version changed: expected {expected_record_version}, "
                        f"found {existing.record_version}"
                    )
                if stored.record_version != existing.record_version + 1:
                    raise StaleWrite("candidate pool record_version must increase by exactly one")
            self._atomic_write(path, stored)
            return stored

    def read_candidate_pool(self) -> CandidatePool:
        pool = self._read_contract(self.root / "glossary" / "candidates.json", CandidatePool, CANDIDATES_FORMAT)
        if pool.record_hash is None:
            raise CorruptRecord("candidate pool is missing record_hash")
        return pool

    def write_freeze(self, freeze: FreezeIntent) -> str:
        with self.lock():
            plan = self.read_term_plan()
            pool = self.read_candidate_pool()
            records = {item.item_id: self.read_extraction(item.item_id) for item in plan.items}
            preparation = self.read_preparation()
            TermPreparation(
                plan=plan,
                records=records,
                candidates=pool,
                unit_documents=preparation.unit_documents,
                freeze=freeze,
            )
            if freeze.user_terms_hash != preparation.user_terms_hash:
                raise IdentityMismatch("freeze user terms hash differs from preparation")
            if freeze.snapshot_payload.extraction_config_hash != canonical_hash(preparation.extraction_config):
                raise IdentityMismatch("freeze extraction configuration differs from preparation")
            expected_user_terms = {term.term_id: term.model_dump(mode="json") for term in preparation.user_terms}
            frozen_user_terms = {
                term.term_id: term.model_dump(mode="json", exclude={"candidate_ids", "evidence", "frequency"})
                for term in freeze.snapshot_payload.terms
                if term.origin == "user"
            }
            if frozen_user_terms != expected_user_terms:
                raise IdentityMismatch("frozen user rules differ from the committed user copy")
            validate_term_scopes(
                freeze.snapshot_payload.terms,
                set(preparation.document_hashes),
                set(preparation.unit_documents),
            )
            return self._write_immutable(self.root / "glossary" / "freeze.json", freeze, FreezeIntent, FREEZE_FORMAT)

    def read_freeze(self) -> FreezeIntent:
        return self._read_contract(self.root / "glossary" / "freeze.json", FreezeIntent, FREEZE_FORMAT)

    def write_glossary(self, glossary: GlossarySnapshot) -> str:
        with self.lock():
            freeze = self.read_freeze()
            expected = GlossarySnapshot.model_validate(
                freeze.snapshot_payload.model_dump(mode="python") | {"format": GLOSSARY_FORMAT}
            )
            if glossary != expected:
                raise IdentityMismatch("glossary snapshot differs from the committed freeze payload")
            return self._write_immutable(self.root / "glossary.json", glossary, GlossarySnapshot, GLOSSARY_FORMAT)

    def read_glossary(self) -> GlossarySnapshot:
        return self._read_contract(self.root / "glossary.json", GlossarySnapshot, GLOSSARY_FORMAT)

    def write_request(self, manifest: RequestManifest) -> RequestManifest:
        path = self._path("requests", manifest.request_id)
        with self.lock():
            if manifest.stage in {"terms", "resolution"} and (self.root / "glossary" / "freeze.json").exists():
                raise StaleWrite("term preparation requests are sealed after freeze")
            if manifest.stage == "terms":
                plan_items = {item.item_id: item for item in self.read_term_plan().items}
                for item_id in manifest.item_ids:
                    item = plan_items.get(item_id)
                    if item is None:
                        raise IdentityMismatch(f"term request references unknown plan item: {item_id}")
                    record = self.read_extraction(item_id)
                    if (
                        manifest.input_hashes[item_id] != item.extraction_input_hash
                        or record.extraction_input_hash != item.extraction_input_hash
                    ):
                        raise IdentityMismatch(f"term request input does not match its extraction item: {item_id}")
            elif manifest.stage == "resolution":
                groups: dict[str, dict[str, JsonValue]] = {}
                for group in self.read_candidate_pool().conflict_groups:
                    group_id = group.get("group_id")
                    if isinstance(group_id, str):
                        groups[group_id] = group
                for group_id in manifest.item_ids:
                    group = groups.get(group_id)
                    if group is None:
                        raise IdentityMismatch(f"resolution request references unknown conflict group: {group_id}")
                    input_hash = group.get("group_input_hash", group.get("input_hash"))
                    if not isinstance(input_hash, str) or manifest.input_hashes[group_id] != input_hash:
                        raise IdentityMismatch(f"resolution request input does not match conflict group: {group_id}")
            self._write_immutable(path, manifest, RequestManifest, REQUEST_FORMAT)
            return manifest

    def read_request(self, request_id: str) -> RequestManifest:
        return self._read_contract(self._path("requests", request_id), RequestManifest, REQUEST_FORMAT)

    def reserve_attempt(self, request_id: str, attempt: Attempt) -> RequestManifest:
        if attempt.state != "reserved":
            raise ValueError("new attempts must be reserved before dispatch")
        path = self._path("requests", request_id)
        with self.lock():
            manifest = self.read_request(request_id)
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
            manifest = self.read_request(request_id)
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
                if updated_attempt == attempt:
                    return manifest
                if attempt.state in {"succeeded", "failed"}:
                    raise StaleWrite(f"attempt already finished: {attempt_id}")
                attempts[index] = updated_attempt
                updated = manifest.model_copy(update={"attempts": tuple(attempts)})
                self._atomic_write(path, updated)
                return updated
            raise IdentityMismatch(f"unknown attempt: {attempt_id}")


__all__ = ["USER_TERMS_FORMAT", "StoreV25", "UserTermsFile"]
