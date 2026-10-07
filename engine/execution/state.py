"""The single v2.5 translation and review executor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from engine.agents.protocol import ProtocolError
from engine.agents.runtime import (
    MAX_MODEL_INPUT_TOKENS,
    PROMPT_VERSION,
    ModelRuntime,
    RequestError,
)
from engine.execution.utility import (
    _nonnegative_int,
    _optional_positive_int,
    _planner_config,
    _positive_int,
    _unit_limit,
)
from engine.item.context import build_context_index
from engine.schemas.contracts import (
    Attempt,
    ItemStatus,
    JsonValue,
    RequestManifest,
    UnitRecord,
    Usage,
)
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.coherence import (
    load_budget_overrides,
)
from engine.services.store import RunStore


class TranslationPaused(RuntimeError):
    pass


class _DocumentCoherenceBudgetExhausted(RequestError):
    pass


@dataclass(frozen=True)
class TranslationRunResult:
    status: Literal["translated", "needs_attention", "paused", "failed"]
    accepted_units: int
    needs_attention_units: int
    pending_items: int
    http_attempts: int
    predicted_http_requests: int
    reason: str | None = None


@dataclass(frozen=True)
class _Job:
    stage: Literal["translate", "review"]
    unit_id: str
    item_id: str


class State:
    if TYPE_CHECKING:

        def _apply_response(self, manifest: RequestManifest, jobs: tuple[_Job, ...], raw: str | bytes) -> None: ...

        def _fail_item(
            self,
            record: UnitRecord,
            item_id: str,
            stage: str,
            message: str,
            *,
            retry: bool,
            code: str = "request_failed",
            details: Mapping[str, JsonValue] | None = None,
        ) -> None: ...

        def _mark_truncated_batch(
            self, manifest: RequestManifest, jobs: tuple[_Job, ...], raw: str | bytes
        ) -> None: ...

    """Run only persisted BookPlan-3 work; every network result is saved item by item."""

    def __init__(
        self,
        store: RunStore,
        *,
        model: Any = None,
        transport: Any = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.store = store
        self.progress = progress
        self.book = store.read_bookplan()
        self.preparation = store.read_preparation()
        self.documents = {
            document_id: store.read_document(document_id, expected_hash=digest)
            for document_id, digest in self.book.document_hashes.items()
        }
        self.units = {unit.unit_id: unit for document in self.documents.values() for unit in document.units}
        self.derived_bindings: dict[str, str] = {}
        for document in self.documents.values():
            for binding in document.derived_bindings:
                if binding.get("kind") != "derived_navigation":
                    continue
                unit_id, source_unit_id = binding.get("unit_id"), binding.get("source_unit_id")
                if (
                    not isinstance(unit_id, str)
                    or not isinstance(source_unit_id, str)
                    or unit_id not in self.units
                    or source_unit_id not in self.units
                    or unit_id in self.derived_bindings
                ):
                    raise IdentityMismatch("invalid frozen derived navigation binding")
                self.derived_bindings[unit_id] = source_unit_id
        self.glossary = store.read_glossary()
        self.terms = {term.term_id: term for term in self.glossary.terms}
        self.records = {unit_id: store.read_unit(unit_id) for unit_id in self.book.unit_ids}
        for unit_id, source_unit_id in self.derived_bindings.items():
            derived = self.records[unit_id].derived
            if derived != {"state": "blocked_dependency", "source_unit_id": source_unit_id} and not (
                derived is not None
                and derived.get("state") == "valid"
                and derived.get("source_unit_id") == source_unit_id
            ):
                raise IdentityMismatch(f"derived Unit is absent or differs from its frozen binding: {unit_id}")
        if any(
            record.derived is not None and unit_id not in self.derived_bindings
            for unit_id, record in self.records.items()
        ):
            raise IdentityMismatch("UnitRecord contains an unfrozen derived dependency")
        self.config = self.book.translation_config
        if transport is None and self.config.get("prompt_version") != PROMPT_VERSION:
            raise ValueError("frozen translation prompt differs from the current runtime; start a new run")
        self.planner_config = _planner_config(self.config)
        self.output_tokens = self.planner_config.max_output_tokens
        self.max_batch_items = self.planner_config.max_batch_items
        self.reading_edges = tuple(
            zip(self.preparation.reading_order, self.preparation.reading_order[1:], strict=False)
        )
        self.context_chars = _positive_int(self.config.get("context_chars"), 400)
        self.context_index = build_context_index(self.documents, self.reading_edges, self.context_chars)
        planned_translation = sum(_unit_limit(record) for record in self.records.values())
        term_limit = store.read_term_plan().extraction_http_limit
        configured_limit = _nonnegative_int(self.config.get("run_http_limit"), 0)
        self._hard_run_limit = bool(configured_limit)
        self.run_limit = configured_limit or term_limit + planned_translation
        self.budget_overrides = load_budget_overrides(store)
        self.run_limit += int(self.budget_overrides["add_run_http"])
        self.checks: dict[str, dict[str, Any]] = {}
        self._rebuild_journal()
        self.runtime = ModelRuntime(
            model=model,
            transport=transport,
            rpm=_optional_positive_int(self.config.get("rpm")),
            tpm=_optional_positive_int(self.config.get("tpm")),
            max_inflight=_positive_int(self.config.get("concurrency"), 2),
            model_max_output_tokens=self.output_tokens,
            reserve_attempt=self._reserve,
            finish_attempt=self._finish,
            persist_response=self.store.save_model_response,
            prior_input_limit_breach=max(
                (
                    attempt.usage.input_tokens
                    for request in self._request_cache.values()
                    for attempt in request.attempts
                    if attempt.usage is not None and attempt.usage.input_tokens > MAX_MODEL_INPUT_TOKENS
                ),
                default=None,
            ),
        )
        self.predicted_http_requests = 2 * max(
            max((len(record.items) for record in self.records.values()), default=0),
            (sum(len(record.items) for record in self.records.values()) + self.max_batch_items - 1)
            // self.max_batch_items,
        )

    def _rebuild_journal(self) -> None:
        self._request_cache = {
            path.stem: self.store.read_request(path.stem)
            for path in sorted(state.glob(self.store.root / "requests", "*.json"))
        }
        self._spent_total = sum(len(request.attempts) for request in self._request_cache.values())
        self._actual_attempt_ids = {
            attempt.attempt_id
            for request in self._request_cache.values()
            for attempt in request.attempts
            if attempt.state != "reserved"
        }
        self._actual_http_total = len(self._actual_attempt_ids)
        self._spent_by_unit: dict[str, int] = {unit_id: 0 for unit_id in self.book.unit_ids}
        self._coherence_spent_by_document: dict[str, int] = {
            document_id: 0 for document_id in self.book.document_hashes
        }
        self._logical_by_item_stage: dict[tuple[str, str], int] = {}
        self._logical_by_item_stage_revision: dict[tuple[str, str, int], int] = {}
        self._logical_requests_seen: set[tuple[str, str]] = set()
        for request in self._request_cache.values():
            if request.stage != "coherence":
                for unit_id in {
                    unit_id for item_id in request.item_ids for unit_id in request.item_unit_ids.get(item_id, ())
                }:
                    self._spent_by_unit[unit_id] = self._spent_by_unit.get(unit_id, 0) + len(request.attempts)
            elif request.unit_document_ids:
                document_ids = set(request.unit_document_ids.values())
                if len(document_ids) == 1:
                    document_id = next(iter(document_ids))
                    self._coherence_spent_by_document[document_id] = self._coherence_spent_by_document.get(
                        document_id, 0
                    ) + len(request.attempts)
            if any(attempt.state != "reserved" for attempt in request.attempts):
                for item_id in request.item_ids:
                    self._logical_by_item_stage[(item_id, request.stage)] = (
                        self._logical_by_item_stage.get((item_id, request.stage), 0) + 1
                    )
                    self._logical_requests_seen.add((request.request_id, item_id))
                    participants = request.item_unit_ids.get(item_id, ())
                    if len(participants) == 1:
                        revision = request.revisions.get(participants[0])
                        if revision is not None:
                            revision_key = item_id, request.stage, revision
                            self._logical_by_item_stage_revision[revision_key] = (
                                self._logical_by_item_stage_revision.get(revision_key, 0) + 1
                            )

    def _spent(self, unit_id: str | None = None) -> int:
        return self._spent_total if unit_id is None else self._spent_by_unit.get(unit_id, 0)

    def _unit_limit(self, record: UnitRecord) -> int:
        additions = self.budget_overrides.get("add_unit_http", {})
        extra = additions.get(record.unit_id, 0) if isinstance(additions, dict) else 0
        return _unit_limit(record) + int(extra)

    def _logical_calls(self, item_id: str, stage: str, revision: int | None = None) -> int:
        if revision is not None:
            return self._logical_by_item_stage_revision.get((item_id, stage, revision), 0)
        return self._logical_by_item_stage.get((item_id, stage), 0)

    def _reserve(self, request_id: str, attempt: Any) -> None:
        with self.store.lock():
            manifest = self.store.read_request(request_id)
            unit_ids = tuple(
                dict.fromkeys(
                    unit_id for item_id in attempt.affected_items for unit_id in manifest.item_unit_ids[item_id]
                )
            )
            if self._spent() >= self.run_limit:
                raise TranslationPaused("run HTTP budget exhausted")
            if manifest.stage == "coherence":
                document_ids = {manifest.unit_document_ids[unit_id] for unit_id in unit_ids}
                if len(document_ids) != 1:
                    raise ValueError("coherence request must belong to one document")
                document_id = next(iter(document_ids))
                spent = self._coherence_spent_by_document.get(document_id, 0)
                check_additions = self.budget_overrides.get("add_check_http", {})
                extra = check_additions.get(document_id, 0) if isinstance(check_additions, dict) else 0
                if spent >= int(self.checks[document_id]["http_limit"]) + int(extra):
                    raise _DocumentCoherenceBudgetExhausted(
                        f"document coherence HTTP budget exhausted: {document_id}", attempts=0
                    )
                manifest = self.store.reserve_attempt(
                    request_id, Attempt.model_validate(attempt.model_dump(mode="python"))
                )
                self._request_cache[request_id] = manifest
                self._spent_total += 1
                self._coherence_spent_by_document[document_id] = spent + 1
                return
            records = {unit_id: self.store.read_unit(unit_id) for unit_id in unit_ids}
            for unit_id, record in records.items():
                used = max(record.counters.get("http_attempts", 0), self._spent_by_unit.get(unit_id, 0))
                if used >= self._unit_limit(record):
                    raise RequestError(f"Unit HTTP budget exhausted: {unit_id}", attempts=0)
            manifest = self.store.reserve_attempt(
                request_id, Attempt.model_validate(attempt.model_dump(mode="python"))
            )
            self._request_cache[request_id] = manifest
            self._spent_total += 1
            for unit_id in unit_ids:
                self._spent_by_unit[unit_id] = self._spent_by_unit.get(unit_id, 0) + 1
            for unit_id, record in records.items():
                counters = dict(record.counters)
                counters["http_attempts"] = max(counters.get("http_attempts", 0), self._spent_by_unit.get(unit_id, 0))
                counters.setdefault("unit_http_limit", self._unit_limit(record))
                self.records[unit_id] = self.store.save_unit(
                    record.model_copy(update={"record_version": record.record_version + 1, "counters": counters}),
                    expected_record_version=record.record_version,
                )

    def _finish(self, request_id: str, attempt_id: str, **fields: Any) -> None:
        usage = fields.get("usage")
        if usage is not None:
            fields["usage"] = Usage.model_validate(usage.model_dump(mode="python"))
        manifest = self.store.finish_attempt(request_id, attempt_id, **fields)
        self._request_cache[request_id] = manifest
        if fields.get("state") != "reserved" and attempt_id not in self._actual_attempt_ids:
            self._actual_attempt_ids.add(attempt_id)
            self._actual_http_total += 1
        if fields.get("state") != "reserved":
            for item_id in manifest.item_ids:
                key = request_id, item_id
                if key in self._logical_requests_seen:
                    continue
                self._logical_requests_seen.add(key)
                logical_key = item_id, manifest.stage
                self._logical_by_item_stage[logical_key] = self._logical_by_item_stage.get(logical_key, 0) + 1
                participants = manifest.item_unit_ids.get(item_id, ())
                if len(participants) == 1:
                    revision = manifest.revisions.get(participants[0])
                    if revision is not None:
                        revision_key = item_id, manifest.stage, revision
                        self._logical_by_item_stage_revision[revision_key] = (
                            self._logical_by_item_stage_revision.get(revision_key, 0) + 1
                        )

    def _save(self, record: UnitRecord, **updates: Any) -> UnitRecord:
        saved = self.store.save_unit(
            record.model_copy(update={"record_version": record.record_version + 1, **updates}),
            expected_record_version=record.record_version,
        )
        self.records[record.unit_id] = saved
        return saved

    def _recover_in_flight(self) -> None:
        for record in tuple(self.records.values()):
            items = dict(record.items)
            changed = False
            for item_id, item in items.items():
                if item.status != ItemStatus.IN_FLIGHT:
                    continue
                items[item_id] = item.model_copy(
                    update={
                        "status": ItemStatus.RETRY_WAIT,
                        "next_action": item.stage,
                        "failure": {"code": "resume_unknown", "message": "prior request outcome was not applied"},
                    }
                )
                changed = True
            if changed:
                self._save(record, items=items)

    def _journaled_response(self, manifest: RequestManifest) -> Any | None:
        for attempt in reversed(manifest.attempts):
            response = self.store.read_model_response(manifest.stage, manifest.request_id, attempt.attempt_id)
            if response is None or attempt.state not in {"sent", "unknown", "succeeded"}:
                continue
            if attempt.state != "succeeded":
                usage = response.usage
                self._finish(
                    manifest.request_id,
                    attempt.attempt_id,
                    state="succeeded",
                    usage=None
                    if usage is None
                    else Usage(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        known_cost=usage.known_cost,
                    ),
                    metadata=dict(response.metadata),
                )
            if response.usage is not None and response.usage.input_tokens > MAX_MODEL_INPUT_TOKENS:
                self.runtime._actual_input_limit_breached = response.usage.input_tokens
            return response
        return None

    def _replay_item_responses(self) -> None:
        for manifest in tuple(self._request_cache.values()):
            if manifest.stage not in {"translate", "review"}:
                continue
            stage: Literal["translate", "review"] = "translate" if manifest.stage == "translate" else "review"
            jobs: list[_Job] = []
            for item_id in manifest.item_ids:
                unit_ids = manifest.item_unit_ids.get(item_id, ())
                if len(unit_ids) != 1:
                    continue
                record = self.records.get(unit_ids[0])
                item = record.items.get(item_id) if record is not None else None
                if item is not None and item.status == ItemStatus.IN_FLIGHT and item.request_id == manifest.request_id:
                    jobs.append(_Job(stage, unit_ids[0], item_id))
            if not jobs:
                continue
            response = self._journaled_response(manifest)
            if response is None:
                continue
            if response.finish_reason == "length":
                self._mark_truncated_batch(manifest, tuple(jobs), response.raw)
                continue
            try:
                self._apply_response(manifest, tuple(jobs), response.raw)
            except ProtocolError as error:
                for job in jobs:
                    self._fail_item(
                        self.records[job.unit_id],
                        job.item_id,
                        job.stage,
                        str(error),
                        retry=True,
                        code=(
                            "translation_protocol_rejected" if job.stage == "translate" else "review_protocol_rejected"
                        ),
                    )
