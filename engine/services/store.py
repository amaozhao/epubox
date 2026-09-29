"""Crash-safe storage and identity gates for one translation run."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict

from engine.item.extractor import validate_source_relations
from engine.item.inline import events_to_projection, parse_projection
from engine.item.planner import MAX_SOURCE_TOKENS, _atomize, _range_stacks, _segment_events, source_token_count
from engine.item.source_views import validate_source_views
from engine.item.unit_planner import build_context_index, initial_derived_navigation, plan_unit
from engine.schemas.contracts import (
    BOOK_FORMAT,
    CANDIDATES_FORMAT,
    DOCUMENT_FORMAT,
    EXTRACTION_RECORD_FORMAT,
    FREEZE_FORMAT,
    GLOSSARY_FORMAT,
    PREPARATION_FORMAT,
    REQUEST_FORMAT,
    TERM_PLAN_FORMAT,
    UNIT_FORMAT,
    Attempt,
    BookPlan,
    CandidatePool,
    DocumentPlan,
    FreezeIntent,
    GlossarySnapshot,
    ItemStatus,
    JsonValue,
    PreparationPlan,
    RequestManifest,
    TermExtractionPlan,
    TermExtractionRecord,
    TermPreparation,
    Unit,
    UnitRecord,
    UnsupportedFormatError,
    Usage,
    UserTerm,
    candidate_pool_record_hash,
    canonical_hash,
    canonical_json_bytes,
    parse_contract,
    unit_record_hash,
    validate_cut_plan_coverage,
    validate_term_scopes,
)
from engine.services.atomic_store import AtomicStore, CorruptRecord, IdentityMismatch, StaleWrite
from engine.services.term_planning import plan_term_extraction

ModelT = TypeVar("ModelT", bound=BaseModel)
USER_TERMS_FORMAT = "epubox-user-terms-1"


class UserTermsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["epubox-user-terms-1"] = USER_TERMS_FORMAT
    terms: tuple[UserTerm, ...] = ()


class RunStore:
    """Persist P1-P3 without requiring a translation-ready BookPlan.

    V07 owns Unit/BookPlan writes and must call ``validate_cut_plan_coverage``
    before making any Unit executable.
    """

    def __init__(self, root: Path | str):
        self._base = AtomicStore(
            root,
            directories=("documents", "units", "checks", "requests", "staging", "glossary/extraction"),
        )
        self.root = self._base.root
        self._unit_context_cache: (
            tuple[
                tuple[tuple[int, int], ...],
                PreparationPlan,
                dict[str, DocumentPlan],
                FreezeIntent,
                GlossarySnapshot,
            ]
            | None
        ) = None

    def lock(self, *, blocking: bool = True):
        return self._base.lock(blocking=blocking)

    def _path(self, directory: str, record_id: str) -> Path:
        return self._base.path(directory, record_id)

    @staticmethod
    def _atomic_write(path: Path, value: BaseModel | dict[str, JsonValue]) -> str:
        return AtomicStore.atomic_write_bytes(path, canonical_json_bytes(value))

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
        validate_source_relations(plan)
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
                validate_source_relations(document)
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
            preparation, documents = self._trusted_preparation_documents()
            if plan.source_hash != preparation.source_hash or plan.preparation_hash != self._file_hash(
                self.root / "preparation.json"
            ):
                raise IdentityMismatch("term plan does not belong to the committed preparation")
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

    def _trusted_preparation_documents(self) -> tuple[PreparationPlan, dict[str, DocumentPlan]]:
        preparation = self.read_preparation()
        if (
            preparation.source_path != "source.epub"
            or self._file_hash(self.root / preparation.source_path) != preparation.source_hash
        ):
            raise IdentityMismatch("preparation source snapshot identity changed")
        disk_ids = {entry.stem for entry in (self.root / "documents").glob("*.json")}
        if disk_ids != set(preparation.document_hashes):
            raise IdentityMismatch("document inventory differs from preparation")
        documents = {
            document_id: self.read_document(document_id, expected_hash=document_hash)
            for document_id, document_hash in preparation.document_hashes.items()
        }
        from engine.item.structural_extractor import EXTRACTOR_VERSION

        if any(document.extractor_version != EXTRACTOR_VERSION for document in documents.values()):
            raise IdentityMismatch("preparation uses an obsolete extractor version; start a new run")
        try:
            with zipfile.ZipFile(self.root / preparation.source_path) as archive:
                for document in documents.values():
                    validate_source_views(document)
                    validate_source_relations(document)
                    raw = archive.read(document.resource.path)
                    if hashlib.sha256(
                        raw
                    ).hexdigest() != document.resource.source_sha256 or raw != document.source_markup.encode("utf-8"):
                        raise IdentityMismatch(
                            f"DocumentPlan no longer matches source snapshot: {document.document_id}"
                        )
        except (KeyError, zipfile.BadZipFile) as error:
            raise IdentityMismatch("DocumentPlan resource is unavailable in source snapshot") from error
        return preparation, documents

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

    @staticmethod
    def _with_unit_hash(record: UnitRecord) -> UnitRecord:
        return record.model_copy(update={"record_hash": unit_record_hash(record)})

    def _unit_source(self, unit_id: str) -> tuple[PreparationPlan, DocumentPlan, Unit, FreezeIntent, GlossarySnapshot]:
        fingerprint = self._shared_fingerprint()
        if self._unit_context_cache is None or self._unit_context_cache[0] != fingerprint:
            preparation, documents = self._trusted_preparation_documents()
            freeze, glossary = self._trusted_frozen_glossary(preparation)
            fingerprint = self._shared_fingerprint()
            self._unit_context_cache = (fingerprint, preparation, documents, freeze, glossary)
        _, preparation, documents, freeze, glossary = self._unit_context_cache
        document_id = preparation.unit_documents.get(unit_id)
        if document_id is None:
            raise IdentityMismatch(f"unknown UnitRecord source Unit: {unit_id}")
        document = documents[document_id]
        unit = next((item for item in document.units if item.unit_id == unit_id), None)
        if unit is None:
            raise IdentityMismatch(f"Unit inventory does not contain {unit_id}")
        return preparation, document, unit, freeze, glossary

    def _shared_fingerprint(self) -> tuple[tuple[int, int], ...]:
        paths = (
            self.root / "source.epub",
            self.root / "preparation.json",
            self.root / "documents",
            self.root / "glossary.json",
            self.root / "glossary" / "freeze.json",
            self.root / "glossary" / "plan.json",
            self.root / "glossary" / "candidates.json",
            self.root / "glossary" / "extraction",
        )
        return tuple((status.st_mtime_ns, status.st_size) for path in paths for status in (path.stat(),))

    def _validate_unit_record(self, record: UnitRecord) -> None:
        preparation, document, unit, _, glossary = self._unit_source(record.unit_id)
        if (record.document_id, record.source_hash) != (document.document_id, preparation.source_hash):
            raise IdentityMismatch(f"UnitRecord source identity mismatch: {record.unit_id}")
        term_ids = {term.term_id for term in glossary.terms}
        if record.cut_plan is None:
            if record.logical_hash is not None or record.input_hash is not None or record.items:
                raise IdentityMismatch(f"unplanned UnitRecord contains executable state: {record.unit_id}")
            return
        atoms = _atomize(parse_projection(unit.source_projection))
        validate_cut_plan_coverage(record.cut_plan, len(atoms))
        stacks = _range_stacks(atoms)
        for segment in record.cut_plan.segments:
            expected_events = _segment_events(atoms, stacks, segment.source_start, segment.source_end)
            if segment.source_projection != events_to_projection(expected_events):
                raise IdentityMismatch(f"CutPlan segment does not reconstruct source: {segment.segment_id}")
            if source_token_count(segment.source_projection) > MAX_SOURCE_TOKENS:
                raise IdentityMismatch(
                    f"CutPlan segment exceeds {MAX_SOURCE_TOKENS} source tokens: {segment.segment_id}"
                )
            if segment.virtual_boundaries != tuple(event.value for event in expected_events if event.virtual):
                raise IdentityMismatch(f"CutPlan virtual boundaries are invalid: {segment.segment_id}")
            if not set(segment.selected_term_ids).issubset(term_ids):
                raise IdentityMismatch(f"CutPlan references an unknown frozen term: {segment.segment_id}")

    def _trusted_frozen_glossary(
        self, preparation: PreparationPlan | None = None
    ) -> tuple[FreezeIntent, GlossarySnapshot]:
        preparation = preparation or self.read_preparation()
        term_plan = self.read_term_plan()
        pool = self.read_candidate_pool()
        records = {item.item_id: self.read_extraction(item.item_id) for item in term_plan.items}
        freeze = self.read_freeze()
        TermPreparation(
            plan=term_plan,
            records=records,
            candidates=pool,
            unit_documents=preparation.unit_documents,
            freeze=freeze,
        )
        if (
            freeze.preparation_hash != self._file_hash(self.root / "preparation.json")
            or freeze.source_hash != preparation.source_hash
            or freeze.user_terms_hash != preparation.user_terms_hash
            or freeze.snapshot_payload.extraction_config_hash != canonical_hash(preparation.extraction_config)
        ):
            raise IdentityMismatch("freeze identity differs from preparation")
        glossary = self.read_glossary()
        expected = GlossarySnapshot.model_validate(
            freeze.snapshot_payload.model_dump(mode="python") | {"format": GLOSSARY_FORMAT}
        )
        if glossary != expected:
            raise IdentityMismatch("glossary does not replay the committed freeze payload")
        return freeze, glossary

    def save_unit(self, record: UnitRecord, *, expected_record_version: int | None = None) -> UnitRecord:
        path = self._path("units", record.unit_id)
        with self.lock():
            self._validate_unit_record(record)
            stored = self._with_unit_hash(record)
            if not path.exists():
                if (self.root / "bookplan.json").exists():
                    raise StaleWrite("cannot create a missing UnitRecord after BookPlan ready")
                if stored.record_version != 0 or expected_record_version not in {None, 0}:
                    raise StaleWrite("new UnitRecords must start at record_version 0")
            else:
                current = self.read_unit(record.unit_id)
                if expected_record_version is None or current.record_version != expected_record_version:
                    raise StaleWrite(
                        f"UnitRecord version changed: expected {expected_record_version}, "
                        f"found {current.record_version}"
                    )
                if (stored.unit_id, stored.document_id, stored.source_hash, stored.logical_hash) != (
                    current.unit_id,
                    current.document_id,
                    current.source_hash,
                    current.logical_hash,
                ):
                    raise IdentityMismatch("immutable UnitRecord identity changed")
                if stored.record_version != current.record_version + 1:
                    raise StaleWrite("UnitRecord record_version must increase by exactly one")
                if stored.plan_epoch < current.plan_epoch or stored.revision < current.revision:
                    raise StaleWrite("UnitRecord plan_epoch and revision cannot decrease")
                for name, amount in current.counters.items():
                    if stored.counters.get(name, 0) < amount:
                        raise StaleWrite(f"UnitRecord counter cannot decrease: {name}")
            self._atomic_write(path, stored)
            return stored

    def read_unit(self, unit_id: str) -> UnitRecord:
        record = self._read_contract(self._path("units", unit_id), UnitRecord, UNIT_FORMAT)
        if record.record_hash is None or record.record_hash != unit_record_hash(record):
            raise CorruptRecord(f"unit record hash mismatch for {unit_id}")
        self._validate_unit_record(record)
        return record

    def write_bookplan(self, plan: BookPlan) -> str:
        """Commit translation-ready state only after every P4 dependency is trusted."""
        path = self.root / "bookplan.json"
        with self.lock():
            if path.exists():
                return self._write_immutable(path, plan, BookPlan, BOOK_FORMAT)
            preparation, documents = self._trusted_preparation_documents()
            preparation_hash = self._file_hash(self.root / "preparation.json")
            freeze, glossary = self._trusted_frozen_glossary(preparation)
            freeze_hash = self._file_hash(self.root / "glossary" / "freeze.json")
            glossary_hash = self._file_hash(self.root / "glossary.json")
            expected_identity = (
                preparation.source_hash,
                preparation.run_id,
                preparation_hash,
                glossary_hash,
                freeze_hash,
                freeze.freeze_id,
                preparation.document_hashes,
                preparation.unit_documents,
                preparation.translation_config,
            )
            actual_identity = (
                plan.source_hash,
                plan.run_id,
                plan.preparation_hash,
                plan.glossary_file_sha256,
                plan.freeze_file_sha256,
                plan.freeze_id,
                plan.document_hashes,
                plan.unit_documents,
                plan.translation_config,
            )
            if actual_identity != expected_identity:
                raise IdentityMismatch("BookPlan identity differs from P1/P3 inputs")

            ordered_documents = (
                *preparation.reading_order,
                *(
                    document_id
                    for document_id in preparation.document_hashes
                    if document_id not in preparation.reading_order
                ),
            )
            expected_units = tuple(
                unit.unit_id for document_id in ordered_documents for unit in documents[document_id].units
            )
            reading_edges = tuple(zip(preparation.reading_order, preparation.reading_order[1:], strict=False))
            context_chars = self._config_int(preparation.translation_config, "context_chars", 400)
            context_index = build_context_index(documents, reading_edges, context_chars)
            if plan.unit_ids != expected_units or plan.required_unit_count != len(expected_units):
                raise IdentityMismatch("BookPlan Unit inventory differs from DocumentPlans")
            disk_units = {entry.stem for entry in (self.root / "units").glob("*.json")}
            if disk_units != set(expected_units):
                raise IdentityMismatch("ready BookPlan requires exactly one UnitRecord per Unit")
            for unit_id in expected_units:
                record = self.read_unit(unit_id)
                expected_plan_hash = record.cut_plan.plan_hash if record.cut_plan is not None else None
                if plan.initial_unit_plans.get(unit_id) != expected_plan_hash:
                    raise IdentityMismatch(f"BookPlan initial Unit plan mismatch: {unit_id}")
                if record.record_version != 0 or record.plan_epoch != 0 or record.revision != 0:
                    raise IdentityMismatch(f"initial UnitRecord version is not zero: {unit_id}")
                if record.candidate is not None or record.accepted_revision is not None or record.review is not None:
                    raise IdentityMismatch(f"initial UnitRecord already contains translation state: {unit_id}")
                if record.term_feedback:
                    raise IdentityMismatch(f"initial UnitRecord already contains term feedback: {unit_id}")
                document = documents[record.document_id]
                unit = next(unit for unit in document.units if unit.unit_id == unit_id)
                expected_derived = initial_derived_navigation(unit, document, documents=documents)
                if record.cut_plan is not None:
                    if expected_derived is not None or record.derived is not None:
                        raise IdentityMismatch(f"derived Unit has a model CutPlan: {unit_id}")
                    expected = plan_unit(
                        unit,
                        document,
                        glossary,
                        preparation.translation_config,
                        documents=documents,
                        reading_edges=reading_edges,
                        context_chars=context_chars,
                        context_index=context_index,
                    )
                    if (
                        record.logical_hash != expected.logical_hash
                        or record.input_hash != expected.input_hash
                        or record.cut_plan != expected.cut_plan
                        or record.items != expected.items
                    ):
                        raise IdentityMismatch(f"initial UnitRecord differs from frozen Unit plan: {unit_id}")
                elif expected_derived is not None:
                    if (
                        record.derived != expected_derived
                        or record.logical_hash is not None
                        or record.input_hash is not None
                        or record.items
                    ):
                        raise IdentityMismatch(f"initial derived Unit differs from frozen binding: {unit_id}")
                elif record.derived is not None:
                    raise IdentityMismatch(f"UnitRecord has an unfrozen derived binding: {unit_id}")
                if any(
                    item.status != ItemStatus.PENDING
                    or item.target_projection is not None
                    or item.request_id is not None
                    or item.failure is not None
                    for item in record.items.values()
                ):
                    raise IdentityMismatch(f"initial UnitRecord already contains item results: {unit_id}")

            requests = tuple(
                self.read_request(request_path.stem)
                for request_path in sorted((self.root / "requests").glob("*.json"))
            )
            term_requests = tuple(request for request in requests if request.stage in {"terms", "resolution"})
            succeeded_owners = {
                (request.stage, request.owner_kind, request.owner_id)
                for request in term_requests
                if any(attempt.state == "succeeded" for attempt in request.attempts)
            }
            for request in term_requests:
                if (request.stage, request.owner_kind, request.owner_id) not in succeeded_owners and any(
                    attempt.state in {"sent", "unknown"}
                    or (
                        attempt.state == "reserved"
                        and any(
                            value is not None
                            for value in (attempt.sent_at, attempt.finished_at, attempt.usage, attempt.error)
                        )
                    )
                    for attempt in request.attempts
                ):
                    raise IdentityMismatch(f"term request is not terminal: {request.request_id}")
            return self._atomic_write(path, plan)

    def read_bookplan(self) -> BookPlan:
        plan = self._read_contract(self.root / "bookplan.json", BookPlan, BOOK_FORMAT)
        preparation, documents = self._trusted_preparation_documents()
        freeze, glossary = self._trusted_frozen_glossary(preparation)
        if (
            plan.preparation_hash != self._file_hash(self.root / "preparation.json")
            or plan.glossary_file_sha256 != self._file_hash(self.root / "glossary.json")
            or plan.freeze_file_sha256 != self._file_hash(self.root / "glossary" / "freeze.json")
            or plan.source_hash != preparation.source_hash
            or plan.freeze_id != freeze.freeze_id
            or glossary.freeze_id != freeze.freeze_id
            or plan.document_hashes != preparation.document_hashes
            or plan.unit_documents != preparation.unit_documents
            or plan.translation_config != preparation.translation_config
        ):
            raise CorruptRecord("BookPlan dependency hash mismatch")
        source_units = {unit.unit_id for document in documents.values() for unit in document.units}
        disk_units = {entry.stem for entry in (self.root / "units").glob("*.json")}
        ordered_documents = (
            *preparation.reading_order,
            *(
                document_id
                for document_id in preparation.document_hashes
                if document_id not in preparation.reading_order
            ),
        )
        expected_units = tuple(
            unit.unit_id for document_id in ordered_documents for unit in documents[document_id].units
        )
        if plan.unit_ids != expected_units or set(expected_units) != source_units or disk_units != source_units:
            raise CorruptRecord("BookPlan Unit inventory mismatch")
        for unit_id in plan.unit_ids:
            self.read_unit(unit_id)
        return plan

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


__all__ = ["USER_TERMS_FORMAT", "RunStore", "UserTermsFile"]
