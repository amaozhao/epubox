"""The single v2.5 translation and review executor."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from engine.agents.protocol import ProtocolError, validate_coherence_response, validate_translation_response
from engine.agents.runtime import (
    MAX_MODEL_INPUT_TOKENS,
    PROMPT_VERSION,
    InputBudgetError,
    MalformedEnvelopeError,
    ModelRuntime,
    RequestError,
    RuntimePaused,
    model_input_budget,
    request_messages,
    wire_hash,
)
from engine.agents.terms import validate_review_response
from engine.core.quality import find_degenerate_translation
from engine.core.tokens import count_tokens
from engine.epub.assembly import derive_navigation_projection
from engine.item.context import build_context, build_context_index, plan_unit
from engine.item.inline import (
    Event,
    ProjectionError,
    events_to_projection,
    parse_projection,
    plain_text,
    validate_projection,
)
from engine.item.planner import (
    MAX_SOURCE_TOKENS,
    PlannerConfig,
    batch_request,
    recommended_output_tokens,
    source_token_count,
)
from engine.schemas.contracts import (
    Attempt,
    DocumentPlan,
    FrozenTerm,
    ItemRecord,
    ItemStatus,
    JsonValue,
    RequestManifest,
    Segment,
    SourceRef,
    SourceTextView,
    Unit,
    UnitRecord,
    Usage,
    canonical_hash,
    strict_json_loads,
)
from engine.services.atomic import IdentityMismatch, StoreError
from engine.services.coherence import (
    load_budget_overrides,
    pending_windows,
    prepare_document_check,
    save_document_check,
    save_window_result,
    window_payload,
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


class TranslationEngine:
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
            for path in sorted((self.store.root / "requests").glob("*.json"))
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

    def _replay_coherence_responses(self, document_id: str, check: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        pending = {
            str(item["item_id"]): item
            for item in (window_payload(window, self.records) for window in pending_windows(check))
        }
        revised = False
        for manifest in tuple(self._request_cache.values()):
            if manifest.stage != "coherence" or not set(manifest.item_ids).intersection(pending):
                continue
            if set(manifest.unit_document_ids.values()) != {document_id}:
                continue
            current_ids = tuple(item_id for item_id in manifest.item_ids if item_id in pending)
            if not current_ids or any(
                self.records[unit_id].revision != revision or self.records[unit_id].accepted_revision != revision
                for unit_id, revision in manifest.revisions.items()
            ):
                continue
            response = self._journaled_response(manifest)
            if response is None:
                continue
            if response.finish_reason == "length":
                truncated = [pending[item_id] for item_id in current_ids]
                if len(truncated) == 1:
                    item_id = current_ids[0]
                    check = save_window_result(
                        self.store,
                        check,
                        item_id,
                        [
                            {
                                "code": "coherence_request_failed",
                                "severity": "major",
                                "message": "model response was truncated",
                            }
                        ],
                    )
                    pending.pop(item_id, None)
                else:
                    midpoint = len(truncated) // 2
                    check = self._save_coherence_splits(
                        check,
                        manifest.request_id,
                        (truncated[:midpoint], truncated[midpoint:]),
                    )
                continue
            allowed = {item_id: set(manifest.item_unit_ids[item_id]) for item_id in current_ids}
            try:
                parsed = validate_coherence_response(response.raw, manifest.request_id, allowed)
            except ProtocolError:
                continue
            for item_id in current_ids:
                result = parsed.accepted.get(item_id)
                item = pending[item_id]
                if result is None or manifest.input_hashes.get(item_id) != canonical_hash(item):
                    continue
                issues = [dict(issue) for issue in result["issues"]]
                check = save_window_result(self.store, check, item_id, issues)
                pending.pop(item_id, None)
                affected = tuple(str(value) for value in result["unit_ids"])
                if any(issue.get("severity") in {"major", "critical"} for issue in issues) and affected:
                    revised |= self._schedule_coherence_revision(document_id, affected, issues)
        self.checks[document_id] = check
        return check, revised

    async def run(self) -> TranslationRunResult:
        with self.store.lock(blocking=False):
            return await self._run_locked()

    async def _run_locked(self) -> TranslationRunResult:
        paused_reason: str | None = None
        try:
            self._prepare_checks()
            self._replay_item_responses()
            self._recover_in_flight()
            self._emit_progress("translation", "running")
            while not paused_reason:
                while True:
                    self._advance_local_state()
                    jobs = self._ready_jobs()
                    if not jobs:
                        if self._recover_terminal_items():
                            continue
                        break
                    batches = self._pack_jobs(jobs)
                    if not batches:
                        break
                    for batch in batches:
                        try:
                            await self._run_batch(batch)
                        except (TranslationPaused, RuntimePaused) as error:
                            paused_reason = str(error)
                            break
                        self._emit_progress(batch[0].stage, "running")
                    if paused_reason:
                        break
                if paused_reason:
                    break
                paused_reason, revised = await self._run_coherence()
                self._emit_progress("coherence", "running")
                if not revised:
                    break
        except StoreError as error:
            return self._result("failed", str(error))
        if paused_reason:
            return self._result("paused", paused_reason)
        status, reason = self._outcome()
        return self._result(status, reason)

    def _prepare_checks(self) -> None:
        initial = not self.checks
        self.checks = {
            document_id: prepare_document_check(self.store, document, self.records)
            for document_id, document in self.documents.items()
        }
        if initial and not self._hard_run_limit:
            self.run_limit += sum(int(check["http_limit"]) for check in self.checks.values())
        if initial:
            self.predicted_http_requests += sum(
                len(self._pack_coherence_items([window_payload(window, self.records) for window in check["windows"]]))
                for check in self.checks.values()
            )

    async def _run_coherence(self) -> tuple[str | None, bool]:
        self._prepare_checks()
        revised = False
        for document_id in self.book.document_hashes:
            check = self.checks[document_id]
            check, replayed_revision = self._replay_coherence_responses(document_id, check)
            revised |= replayed_revision
            items = [window_payload(window, self.records) for window in pending_windows(check)]
            batches = list(self._pack_pending_coherence_items(check, items))
            while batches:
                batch = batches.pop(0)
                pending_batch = tuple(batch)
                probe_payload = {
                    "protocol": "epubox-coherence-1",
                    "request_id": "co-" + "0" * 32,
                    "items": pending_batch,
                }
                input_tokens = model_input_budget("coherence", probe_payload)["estimated_input_tokens"]
                if input_tokens > self._coherence_input_limit():
                    item = pending_batch[0]
                    item_id = str(item["item_id"])
                    check = save_window_result(
                        self.store,
                        check,
                        item_id,
                        [
                            {
                                "code": "coherence_input_oversized",
                                "severity": "major",
                                "message": (
                                    f"coherence input exceeds {self._coherence_input_limit()} tokens: {input_tokens}"
                                ),
                            }
                        ],
                    )
                    self.checks[document_id] = check
                    continue
                revisions: list[tuple[tuple[str, ...], list[dict[str, JsonValue]]]] = []
                split_batches: list[tuple[dict[str, Any], ...]] = []
                for content_attempt in range(2):
                    request_id = "co-" + uuid4().hex
                    payload = {
                        "protocol": "epubox-coherence-1",
                        "request_id": request_id,
                        "items": pending_batch,
                    }
                    output_tokens = self.output_tokens
                    item_units: dict[str, tuple[str, ...]] = {}
                    item_vectors: dict[str, dict[str, int]] = {}
                    for item in pending_batch:
                        raw_ids = item["unit_ids"]
                        if not isinstance(raw_ids, list) or not all(isinstance(value, str) for value in raw_ids):
                            raise ValueError("coherence window Unit IDs are invalid")
                        item_id = str(item["item_id"])
                        item_units[item_id] = tuple(dict.fromkeys(raw_ids))
                        item_vectors[item_id] = {
                            unit_id: self.records[unit_id].revision for unit_id in item_units[item_id]
                        }
                    unit_ids = tuple(dict.fromkeys(unit_id for ids in item_units.values() for unit_id in ids))
                    item_ids = tuple(item_units)
                    manifest = RequestManifest(
                        request_id=request_id,
                        stage="coherence",
                        owner_kind="translation_item",
                        owner_id=item_ids[0],
                        item_ids=item_ids,
                        input_hashes={str(item["item_id"]): canonical_hash(item) for item in pending_batch},
                        wire_hash=wire_hash("coherence", payload, output_tokens),
                        record_versions={unit_id: self.records[unit_id].record_version for unit_id in unit_ids},
                        item_unit_ids=item_units,
                        unit_document_ids={unit_id: self.records[unit_id].document_id for unit_id in unit_ids},
                        plan_epochs={unit_id: self.records[unit_id].plan_epoch for unit_id in unit_ids},
                        revisions={unit_id: self.records[unit_id].revision for unit_id in unit_ids},
                        target_hashes={str(item["item_id"]): canonical_hash(item["target"]) for item in pending_batch},
                        glossary_file_sha256=self.book.glossary_file_sha256,
                        freeze_id=self.book.freeze_id,
                        term_ids_by_item={item_id: () for item_id in item_ids},
                        terms_hashes={item_id: canonical_hash({"terms": []}) for item_id in item_ids},
                        context_hashes={
                            str(item["item_id"]): canonical_hash(
                                {
                                    "source": item["source"],
                                    "target": item["target"],
                                    "versions": item_vectors[str(item["item_id"])],
                                }
                            )
                            for item in pending_batch
                        },
                    )
                    manifest = self.store.write_request(manifest)
                    self._request_cache[request_id] = manifest
                    encoded = json.dumps(request_messages("coherence", payload), ensure_ascii=False, sort_keys=True)
                    batch_error = ""
                    document_budget_error: str | None = None
                    try:
                        response = await self.runtime.invoke(
                            "coherence",
                            payload,
                            {
                                "request_id": request_id,
                                "item_ids": item_ids,
                                "estimated_tokens": count_tokens(encoded) + output_tokens,
                                "output_tokens": output_tokens,
                            },
                        )
                        if response.get("finish_reason") == "length":
                            if len(pending_batch) > 1:
                                midpoint = len(pending_batch) // 2
                                split_batches = [pending_batch[:midpoint], pending_batch[midpoint:]]
                                check = self._save_coherence_splits(
                                    check,
                                    request_id,
                                    tuple(split_batches),
                                )
                                break
                            raise ProtocolError("model response was truncated")
                        parsed = validate_coherence_response(
                            response["raw"], request_id, {item_id: set(ids) for item_id, ids in item_units.items()}
                        )
                    except (RuntimePaused, TranslationPaused) as error:
                        return str(error), revised
                    except InputBudgetError as error:
                        parsed = None
                        batch_error = str(error)
                    except MalformedEnvelopeError as error:
                        parsed = None
                        batch_error = str(error)
                    except _DocumentCoherenceBudgetExhausted as error:
                        parsed = None
                        document_budget_error = str(error)
                        batch_error = str(error)
                    except RequestError as error:
                        if (
                            error.status_code is None
                            or not 400 <= error.status_code < 500
                            or error.status_code in {401, 402, 403, 429}
                        ):
                            return str(error), revised
                        parsed = None
                        batch_error = str(error)
                    except ProtocolError as error:
                        parsed = None
                        batch_error = str(error)

                    if document_budget_error is not None:
                        for item in pending_batch:
                            check = save_window_result(
                                self.store,
                                check,
                                str(item["item_id"]),
                                [
                                    {
                                        "code": "coherence_request_failed",
                                        "severity": "major",
                                        "message": document_budget_error[:2000],
                                    }
                                ],
                            )
                            self.checks[document_id] = check
                        break

                    failed: list[dict[str, Any]] = []
                    failure_messages: dict[str, str] = {}
                    for item in pending_batch:
                        item_id = str(item["item_id"])
                        vector = item_vectors[item_id]
                        if any(
                            self.records[unit_id].revision != revision
                            or self.records[unit_id].accepted_revision != revision
                            for unit_id, revision in vector.items()
                        ):
                            continue
                        if parsed is None or item_id not in parsed.accepted:
                            failed.append(item)
                            failure_messages[item_id] = (
                                parsed.errors.get(item_id, "coherence item missing")
                                if parsed is not None
                                else batch_error
                            )
                            continue
                        issues = [dict(issue) for issue in parsed.accepted[item_id]["issues"]]
                        check = save_window_result(self.store, check, item_id, issues)
                        self.checks[document_id] = check
                        affected = tuple(str(value) for value in parsed.accepted[item_id]["unit_ids"])
                        if any(issue.get("severity") in {"major", "critical"} for issue in issues) and affected:
                            revisions.append((affected, issues))
                    if not failed:
                        break
                    if content_attempt == 0:
                        pending_batch = tuple(failed)
                        continue
                    for item in failed:
                        item_id = str(item["item_id"])
                        issues = [
                            {
                                "code": "coherence_request_failed",
                                "severity": "major",
                                "message": failure_messages[item_id][:2000],
                            }
                        ]
                        check = save_window_result(self.store, check, item_id, issues)
                        self.checks[document_id] = check
                if split_batches:
                    batches[0:0] = split_batches
                for affected, issues in revisions:
                    revised |= self._schedule_coherence_revision(document_id, affected, issues)
        return None, revised

    def _pack_coherence_items(self, items: Sequence[dict[str, Any]]) -> tuple[tuple[dict[str, Any], ...], ...]:
        batches: list[tuple[dict[str, Any], ...]] = []
        current: list[dict[str, Any]] = []
        for item in items:
            candidate = [*current, item]
            payload = {"protocol": "epubox-coherence-1", "request_id": "co-" + "0" * 32, "items": candidate}
            tokens = model_input_budget("coherence", payload)["estimated_input_tokens"]
            if current and (
                tokens > self._coherence_input_limit()
                or self._coherence_min_output_tokens(candidate) > self.output_tokens
            ):
                batches.append(tuple(current))
                current = [item]
            else:
                current = candidate
        if current:
            batches.append(tuple(current))
        return tuple(batches)

    @staticmethod
    def _coherence_min_output_tokens(items: Sequence[dict[str, Any]]) -> int:
        response = {
            "protocol": "epubox-coherence-1",
            "request_id": "co-" + "0" * 32,
            "items": [{"item_id": str(item["item_id"]), "unit_ids": [], "issues": []} for item in items],
        }
        return count_tokens(json.dumps(response, ensure_ascii=False, separators=(",", ":")))

    def _pack_pending_coherence_items(
        self, check: Mapping[str, Any], items: Sequence[dict[str, Any]]
    ) -> tuple[tuple[dict[str, Any], ...], ...]:
        split_ids = check.get("pending_splits", {})
        if not isinstance(split_ids, dict):
            split_ids = {}
        forced: dict[str, list[dict[str, Any]]] = {}
        ordinary: list[dict[str, Any]] = []
        for item in items:
            split_id = split_ids.get(str(item["item_id"]))
            if isinstance(split_id, str):
                forced.setdefault(split_id, []).append(item)
            else:
                ordinary.append(item)
        return (*[tuple(group) for group in forced.values()], *self._pack_coherence_items(ordinary))

    def _save_coherence_splits(
        self,
        check: Mapping[str, Any],
        request_id: str,
        groups: Sequence[Sequence[dict[str, Any]]],
    ) -> dict[str, Any]:
        split_ids = dict(check.get("pending_splits", {}))
        for index, group in enumerate(groups):
            for item in group:
                split_ids[str(item["item_id"])] = f"{request_id}:{index}"
        updated = save_document_check(self.store, dict(check) | {"pending_splits": split_ids})
        self.checks[str(check["document_id"])] = updated
        return updated

    def _coherence_input_limit(self) -> int:
        if self.runtime.tpm is None:
            return MAX_MODEL_INPUT_TOKENS
        return min(MAX_MODEL_INPUT_TOKENS, self.runtime.tpm - self.output_tokens)

    def _schedule_coherence_revision(
        self,
        document_id: str,
        affected: tuple[str, ...],
        issues: Sequence[Mapping[str, JsonValue]],
    ) -> bool:
        check = self.checks[document_id]
        if int(check.get("coherence_revision_rounds", 0)) >= 1:
            return False
        unit_ids = tuple(
            unit_id
            for unit_id in dict.fromkeys(affected)
            if unit_id in self.records
            and self.records[unit_id].document_id == document_id
            and self.records[unit_id].accepted_revision == self.records[unit_id].revision
        )
        if not unit_ids:
            return False
        check = dict(check)
        check["coherence_revision_rounds"] = 1
        self.checks[document_id] = save_document_check(self.store, check)
        message = "; ".join(str(issue.get("message", "coherence issue")) for issue in issues)[:2000]
        for unit_id in unit_ids:
            record = self.store.read_unit(unit_id)
            if (
                record.cut_plan is not None
                and len(record.cut_plan.segments) > 1
                and self._upgrade_cut_plan(unit_id, "coherence seam conflict")
            ):
                continue
            items = {
                item_id: item.model_copy(
                    update={
                        "status": ItemStatus.LOCAL_VALID,
                        "checks": {},
                        "request_id": None,
                        "failure": None,
                        "next_action": "review",
                    }
                )
                for item_id, item in record.items.items()
            }
            counters = dict(record.counters)
            counters["coherence_revision_rounds"] = counters.get("coherence_revision_rounds", 0) + 1
            counters["review_cycle"] = counters.get("review_cycle", 0) + 1
            unresolved = tuple(
                issue for issue in record.unresolved_issues if issue.get("code") != "blocking_coherence"
            ) + (
                {
                    "stage": "coherence",
                    "code": "blocking_coherence",
                    "message": message,
                    "document_id": document_id,
                },
            )
            self._save(
                record,
                revision=record.revision + 1,
                items=items,
                candidate=None,
                accepted_revision=None,
                accepted_target_hash=None,
                local_checks={},
                review=None,
                unresolved_issues=unresolved,
                counters=counters,
            )
        return True

    def _upgrade_cut_plan(self, unit_id: str, reason: str) -> bool:
        record = self.records[unit_id]
        if record.cut_plan is None or record.plan_epoch >= 1 or record.counters.get("replan_attempts", 0) >= 1:
            return False
        unit = self.units[unit_id]
        initialized = plan_unit(
            unit,
            self.documents[unit.document_id],
            self.glossary,
            self.preparation.translation_config,
            epoch=record.plan_epoch + 1,
            documents=self.documents,
            reading_edges=self.reading_edges,
            context_chars=self.context_chars,
            context_index=self.context_index,
            planning_target_ratio=max(
                _positive_number(self.config.get("target_ratio"), 1.6) * 2,
                3.2,
            ),
        )
        if initialized.logical_hash != record.logical_hash:
            raise ValueError("CutPlan upgrade changed immutable logical_hash")
        old_ranges = tuple((segment.source_start, segment.source_end) for segment in record.cut_plan.segments)
        new_ranges = tuple((segment.source_start, segment.source_end) for segment in initialized.cut_plan.segments)
        if new_ranges == old_ranges:
            return False
        counters = dict(record.counters)
        counters["replan_attempts"] = counters.get("replan_attempts", 0) + 1
        counters.setdefault("unit_http_limit", self._unit_limit(record))
        self._save(
            record,
            input_hash=initialized.input_hash,
            plan_epoch=initialized.cut_plan.plan_epoch,
            revision=record.revision + 1,
            cut_plan=initialized.cut_plan,
            items=initialized.items,
            candidate=None,
            accepted_revision=None,
            accepted_target_hash=None,
            local_checks={},
            review=None,
            unresolved_issues=tuple(
                issue
                for issue in record.unresolved_issues
                if issue.get("code") not in {"planning_retry", "blocking_review"}
            ),
            counters=counters,
        )
        _ = reason
        return True

    def _outcome(self) -> tuple[Literal["translated", "needs_attention"], str | None]:
        if all(self._unit_complete(record) for record in self.records.values()) and all(
            check.get("status") == "valid" for check in self.checks.values()
        ):
            return "translated", None
        if any(
            issue.get("code") in {"blocking_review", "blocking_coherence"}
            for record in self.records.values()
            for issue in record.unresolved_issues
        ):
            return "needs_attention", "translation has unresolved blocking review issues"
        if any(check.get("status") == "blocked_dependency" for check in self.checks.values()):
            return "needs_attention", "chapter coherence is blocked by missing Unit acceptance"
        if any(check.get("status") == "needs_attention" for check in self.checks.values()):
            return "needs_attention", "chapter coherence reported blocking issues"
        return "needs_attention", None

    def _result(self, status: str, reason: str | None) -> TranslationRunResult:
        accepted = sum(self._unit_complete(record) for record in self.records.values())
        attention = sum(_record_needs_attention(record) for record in self.records.values())
        pending = sum(
            item.status in {ItemStatus.PENDING, ItemStatus.IN_FLIGHT, ItemStatus.RETRY_WAIT}
            for record in self.records.values()
            for item in record.items.values()
        )
        result = TranslationRunResult(
            status=status,  # type: ignore[arg-type]
            accepted_units=accepted,
            needs_attention_units=attention,
            pending_items=pending,
            http_attempts=self._actual_http_total,
            predicted_http_requests=self.predicted_http_requests,
            reason=reason,
        )
        final_phase = (
            "coherence" if all(self._unit_complete(record) for record in self.records.values()) else "translation"
        )
        self._emit_progress(final_phase, "stopped", result=result)
        return result

    def _emit_progress(
        self,
        phase: str,
        execution_state: Literal["running", "stopped"],
        *,
        result: TranslationRunResult | None = None,
    ) -> None:
        if self.progress is None:
            return
        accepted = (
            result.accepted_units
            if result is not None
            else sum(self._unit_complete(record) for record in self.records.values())
        )
        attention = (
            result.needs_attention_units
            if result is not None
            else sum(_record_needs_attention(record) for record in self.records.values())
        )
        pending = (
            result.pending_items
            if result is not None
            else sum(
                item.status in {ItemStatus.PENDING, ItemStatus.IN_FLIGHT, ItemStatus.RETRY_WAIT}
                for record in self.records.values()
                for item in record.items.values()
            )
        )
        items = tuple(item for record in self.records.values() for item in record.items.values())
        self.progress(
            {
                "phase": phase,
                "execution_state": execution_state,
                "accepted_units": accepted,
                "required_units": self.book.required_unit_count,
                "translated_items": sum(item.target_projection is not None for item in items),
                "reviewed_items": sum(item.status == ItemStatus.REVIEWED for item in items),
                "required_items": len(items),
                "retrying_items": sum(item.status == ItemStatus.RETRY_WAIT for item in items),
                "waiting_derived_units": sum(
                    record.derived is not None and record.derived.get("state") == "blocked_dependency"
                    for record in self.records.values()
                ),
                "pending_items": pending,
                "needs_attention_units": attention,
                "http_attempts": result.http_attempts if result is not None else self._actual_http_total,
            }
        )

    def _unit_complete(self, record: UnitRecord) -> bool:
        if record.derived is None:
            return record.accepted_revision == record.revision
        source_unit_id = record.derived.get("source_unit_id")
        source = self.records.get(source_unit_id) if isinstance(source_unit_id, str) else None
        if (
            source is None
            or source.accepted_revision != source.revision
            or source.candidate is None
            or source.accepted_target_hash != canonical_hash(source.candidate)
            or record.derived.get("state") != "valid"
            or record.derived.get("source_revision") != source.revision
            or record.derived.get("source_target_hash") != source.accepted_target_hash
        ):
            return False
        try:
            target = derive_navigation_projection(self.units[record.unit_id], source.candidate)
        except ValueError:
            return False
        return (
            record.candidate is None
            and record.accepted_revision is None
            and record.derived.get("target") == target
            and record.derived.get("target_hash") == canonical_hash(target)
        )

    def _advance_local_state(self) -> None:
        for unit_id in self.book.unit_ids:
            record = self.records[unit_id]
            if record.accepted_revision is not None or record.cut_plan is None:
                continue
            if all(item.target_projection is not None for item in record.items.values()) and record.candidate is None:
                try:
                    candidate = _merge_candidate(self.units[unit_id], record)
                except (ValueError, ProtocolError) as error:
                    self._fail_remaining(record, "local_validation", str(error))
                    continue
                target_hash = canonical_hash(candidate)
                record = self._save(
                    record,
                    candidate=candidate,
                    local_checks={
                        "passed": True,
                        "revision": record.revision,
                        "input_hash": record.input_hash,
                        "target_hash": target_hash,
                    },
                )
            if (
                record.candidate is not None
                and record.items
                and all(item.status == ItemStatus.REVIEWED for item in record.items.values())
            ):
                if any(
                    issue.get("code") in {"blocking_review", "blocking_coherence"}
                    for issue in record.unresolved_issues
                ):
                    continue
                target_hash = canonical_hash(record.candidate)
                item_reviews: dict[str, dict[str, JsonValue]] = {}
                invalid = False
                for item_id, item in record.items.items():
                    if item.request_id is None:
                        invalid = True
                        break
                    manifest = self.store.read_request(item.request_id)
                    if (
                        manifest.stage != "review"
                        or manifest.freeze_id != self.book.freeze_id
                        or manifest.glossary_file_sha256 != self.book.glossary_file_sha256
                        or manifest.item_unit_ids.get(item_id) != (record.unit_id,)
                        or manifest.plan_epochs.get(record.unit_id) != record.plan_epoch
                        or manifest.revisions.get(record.unit_id) != record.revision
                        or manifest.input_hashes.get(item_id) != record.input_hash
                        or manifest.terms_hashes.get(item_id) != _segment(record, item_id).terms_hash
                        or manifest.context_hashes.get(item_id) != _segment(record, item_id).context_hash
                        or manifest.target_hashes.get(item_id) != item.target_hash
                        or not any(
                            attempt.state == "succeeded" and item_id in attempt.affected_items
                            for attempt in manifest.attempts
                        )
                    ):
                        invalid = True
                        break
                    item_reviews[item_id] = {
                        "request_id": item.request_id,
                        "target_hash": item.target_hash,
                    }
                if invalid:
                    self._fail_remaining(record, "review_identity", "Segment review manifest is invalid")
                    continue
                self._save(
                    record,
                    accepted_revision=record.revision,
                    accepted_target_hash=target_hash,
                    review={
                        "protocol": "epubox-review-2",
                        "item_reviews": item_reviews,
                        "plan_epoch": record.plan_epoch,
                        "passed": True,
                        "revision": record.revision,
                        "input_hash": record.input_hash,
                        "target_hash": target_hash,
                    },
                )
        self._advance_derived_navigation()

    def _advance_derived_navigation(self) -> None:
        for unit_id in self.book.unit_ids:
            record = self.records[unit_id]
            derived = record.derived
            if derived is None:
                continue
            source_unit_id = str(derived["source_unit_id"])
            if self.derived_bindings.get(unit_id) != source_unit_id:
                raise IdentityMismatch(f"derived Unit differs from its frozen binding: {unit_id}")
            source = self.records.get(source_unit_id)
            source_ready = (
                source is not None
                and source.accepted_revision == source.revision
                and source.candidate is not None
                and source.accepted_target_hash == canonical_hash(source.candidate)
            )
            current = source is not None and (
                derived.get("state") == "valid"
                and source_ready
                and derived.get("source_revision") == source.revision
                and derived.get("source_target_hash") == source.accepted_target_hash
            )
            if current:
                continue
            if derived.get("state") == "valid":
                record = self._save(
                    record,
                    revision=record.revision + 1,
                    candidate=None,
                    accepted_revision=None,
                    accepted_target_hash=None,
                    local_checks={},
                    review=None,
                    derived={"state": "blocked_dependency", "source_unit_id": source_unit_id},
                )
            if not source_ready or source is None or source.candidate is None:
                continue
            target = derive_navigation_projection(self.units[unit_id], source.candidate)
            target_hash = canonical_hash(target)
            self._save(
                record,
                candidate=None,
                accepted_revision=None,
                accepted_target_hash=None,
                local_checks={
                    "passed": True,
                    "kind": "derived_navigation",
                    "source_unit_id": source_unit_id,
                    "source_revision": source.revision,
                    "source_target_hash": source.accepted_target_hash,
                    "target_hash": target_hash,
                },
                derived={
                    "state": "valid",
                    "source_unit_id": source_unit_id,
                    "source_revision": source.revision,
                    "source_target_hash": source.accepted_target_hash,
                    "target": target,
                    "target_hash": target_hash,
                },
            )

    def _ready_jobs(self) -> list[_Job]:
        jobs: list[_Job] = []
        for unit_id in self.book.unit_ids:
            record = self.records[unit_id]
            if record.accepted_revision is not None or record.cut_plan is None:
                continue
            if max(record.counters.get("http_attempts", 0), self._spent_by_unit.get(unit_id, 0)) >= self._unit_limit(
                record
            ):
                self._fail_remaining(record, "unit_budget", "Unit HTTP budget exhausted")
                continue
            for segment in record.cut_plan.segments:
                item = record.items[segment.item_id]
                stage: Literal["translate", "review"] = "translate" if item.target_projection is None else "review"
                if item.status not in {
                    ItemStatus.PENDING,
                    ItemStatus.RETRY_WAIT,
                    ItemStatus.LOCAL_VALID,
                    ItemStatus.CANDIDATE,
                }:
                    continue
                limit = self._logical_limit(record, item.item_id, stage)
                logical_calls = self._logical_calls(
                    item.item_id, stage, record.revision if stage == "review" else None
                )
                if logical_calls >= limit:
                    self._fail_item(
                        record,
                        item.item_id,
                        stage,
                        "logical attempt limit exhausted",
                        retry=False,
                        code="logical_attempt_limit",
                    )
                    record = self.records[unit_id]
                    continue
                jobs.append(_Job(stage, unit_id, item.item_id))
        return jobs

    def _logical_limit(self, record: UnitRecord, item_id: str, stage: str) -> int:
        base = 3 if stage == "translate" else 2
        return (
            base
            + int(self._automatic_recovery_active(record, item_id, stage))
            + record.counters.get(f"explicit_retry_{stage}:{item_id}", 0)
        )

    @staticmethod
    def _automatic_recovery_active(record: UnitRecord, item_id: str, stage: str) -> bool:
        return bool(record.counters.get(f"automatic_recovery_{stage}:{item_id}", 0)) and (
            stage == "translate"
            or record.counters.get(f"automatic_recovery_review_revision:{item_id}") == record.revision
        )

    def _recover_terminal_items(self) -> bool:
        """Grant one durable retry only for classified model-content failures."""
        if self._spent() >= self.run_limit:
            return False
        changed = False
        for unit_id in self.book.unit_ids:
            record = self.records[unit_id]
            if record.derived is not None or record.cut_plan is None or record.accepted_revision is not None:
                continue
            if max(record.counters.get("http_attempts", 0), self._spent_by_unit.get(unit_id, 0)) >= self._unit_limit(
                record
            ):
                continue
            translate_failures: list[tuple[str, str]] = []
            review_failures: list[tuple[str, list[dict[str, JsonValue]]]] = []
            for item_id, item in record.items.items():
                if item.status != ItemStatus.NEEDS_ATTENTION or item.failure is None:
                    continue
                classified = _recoverable_failure(item) or self._journaled_review_failure(record, item_id, item)
                if classified is None:
                    continue
                stage, kind, issues = classified
                marker = f"automatic_recovery_{stage}:{item_id}"
                if record.counters.get(marker, 0):
                    continue
                if stage == "review":
                    review_failures.append((item_id, issues))
                else:
                    translate_failures.append((item_id, kind))
            replanned = False
            for item_id, kind in translate_failures:
                record = self.records[unit_id]
                if item_id not in record.items:
                    continue
                if kind == "truncated" and self._upgrade_cut_plan(unit_id, "automatic truncated response recovery"):
                    changed = replanned = True
                    break
                counters = dict(record.counters)
                counters[f"automatic_recovery_translate:{item_id}"] = 1
                item = record.items[item_id].model_copy(
                    update={"status": ItemStatus.RETRY_WAIT, "next_action": "translate"}
                )
                record = self._save(record, items=dict(record.items) | {item_id: item}, counters=counters)
                changed = True
            if replanned:
                continue
            if review_failures:
                counters = dict(record.counters)
                unresolved = list(record.unresolved_issues)
                recovered_revision = record.revision + 1
                for item_id, issues in review_failures:
                    counters[f"automatic_recovery_review:{item_id}"] = 1
                    counters[f"automatic_recovery_review_revision:{item_id}"] = recovered_revision
                    issue_values: list[JsonValue] = [dict(issue) for issue in issues]
                    unresolved.append(
                        {
                            "stage": "review",
                            "code": "blocking_review",
                            "item_id": item_id,
                            "message": "review requires a validated replacement",
                            "issues": issue_values,
                        }
                    )
                counters["review_cycle"] = counters.get("review_cycle", 0) + 1
                items = {
                    item_id: item
                    if item.target_projection is None
                    else item.model_copy(
                        update={
                            "status": ItemStatus.LOCAL_VALID,
                            "checks": {},
                            "request_id": None,
                            "failure": None,
                            "next_action": "review",
                        }
                    )
                    for item_id, item in record.items.items()
                }
                record = self._save(
                    record,
                    revision=recovered_revision,
                    items=items,
                    candidate=None,
                    accepted_revision=None,
                    accepted_target_hash=None,
                    local_checks={},
                    review=None,
                    unresolved_issues=tuple(unresolved),
                    counters=counters,
                )
                changed = True
        return changed

    def _journaled_review_failure(
        self, record: UnitRecord, item_id: str, item: ItemRecord
    ) -> tuple[Literal["review"], str, list[dict[str, JsonValue]]] | None:
        """Recover one truncated legacy review failure from its immutable validated response."""
        failure = item.failure or {}
        request_id = item.request_id
        if (
            item.target_projection is None
            or failure.get("stage") != "review"
            or failure.get("code") != "request_failed"
            or not isinstance(request_id, str)
        ):
            return None
        manifest = self._request_cache.get(request_id)
        if (
            manifest is None
            or manifest.stage != "review"
            or manifest.freeze_id != self.book.freeze_id
            or manifest.glossary_file_sha256 != self.book.glossary_file_sha256
            or manifest.item_unit_ids.get(item_id) != (record.unit_id,)
        ):
            return None
        if (
            manifest.revisions.get(record.unit_id) != record.revision
            or manifest.plan_epochs.get(record.unit_id) != record.plan_epoch
            or manifest.input_hashes.get(item_id) != record.input_hash
            or manifest.target_hashes.get(item_id) != item.target_hash
        ):
            return None
        segment = _segment(record, item_id)
        if (
            manifest.terms_hashes.get(item_id) != segment.terms_hash
            or manifest.context_hashes.get(item_id) != segment.context_hash
        ):
            return None
        expected = {
            item_id: {
                "base_revision": record.revision,
                "terminology_applicable": any(value == "target" for value in segment.term_applicability.values()),
                "bindings_applicable": bool(self.units[record.unit_id].registry),
            }
        }
        for attempt in reversed(manifest.attempts):
            if attempt.state != "succeeded":
                continue
            response = self.store.read_model_response("review", request_id, attempt.attempt_id)
            if response is None or response.finish_reason == "length":
                continue
            try:
                parsed = validate_review_response(response.raw, request_id, expected)
            except ProtocolError:
                return None
            result = parsed.accepted.get(item_id)
            if result is None or result.get("decision") != "needs_attention":
                return None
            issues = _normalized_review_issues(result.get("issues"))
            return ("review", "needs_attention", issues) if issues else None
        return None

    def _pack_jobs(self, jobs: Sequence[_Job]) -> list[tuple[_Job, ...]]:
        result: list[tuple[_Job, ...]] = []
        for stage in ("translate", "review"):
            staged: list[_Job] = []
            for job in (job for job in jobs if job.stage == stage):
                try:
                    batch_request(
                        [self._payload_item(job)],
                        self.planner_config,
                        stage="translation" if stage == "translate" else "review",
                    )
                except ValueError as error:
                    if not self._upgrade_cut_plan(job.unit_id, str(error)):
                        self._fail_item(
                            self.records[job.unit_id],
                            job.item_id,
                            stage,
                            str(error),
                            retry=False,
                            code=(
                                "translation_output_oversized" if stage == "translate" else "review_output_oversized"
                            ),
                        )
                    continue
                staged.append(job)
            forced: dict[str, list[_Job]] = {}
            singletons: list[_Job] = []
            ordinary: list[_Job] = []
            for job in staged:
                record = self.records[job.unit_id]
                failure = record.items[job.item_id].failure or {}
                if self._automatic_recovery_active(record, job.item_id, job.stage):
                    singletons.append(job)
                    continue
                split_id = failure.get("split_id") if failure.get("code") == "truncated_batch" else None
                if isinstance(split_id, str):
                    forced.setdefault(split_id, []).append(job)
                else:
                    ordinary.append(job)
            result.extend(tuple(group) for group in forced.values())
            result.extend((job,) for job in singletons)
            chunks: list[list[_Job]] = []
            current: list[_Job] = []
            for job in ordinary:
                if len(current) >= self.max_batch_items:
                    chunks.append(current)
                    current = []
                current.append(job)
            if current:
                chunks.append(current)
            for chunk in chunks:
                payloads = [self._payload_item(job) for job in chunk]
                try:
                    groups = batch_request(
                        payloads, self.planner_config, stage="translation" if stage == "translate" else "review"
                    )
                except ValueError as error:
                    _ = error
                    result.extend((job,) for job in chunk)
                    continue
                by_id = {job.item_id: job for job in chunk}
                result.extend(tuple(by_id[str(item["item_id"])] for item in group) for group in groups)
        return result

    def _mark_truncated_batch(self, manifest: RequestManifest, jobs: tuple[_Job, ...], raw: str | bytes) -> None:
        try:
            self._apply_response(manifest, jobs, raw)
        except ProtocolError:
            pass
        pending = [
            job
            for job in jobs
            if self.records[job.unit_id].items[job.item_id].status in {ItemStatus.IN_FLIGHT, ItemStatus.RETRY_WAIT}
        ]
        if len(pending) == 1:
            job = pending[0]
            self._fail_item(
                self.records[job.unit_id],
                job.item_id,
                job.stage,
                "model response was truncated",
                retry=False,
                code="model_response_truncated",
            )
            return
        if not pending:
            return
        midpoint = len(pending) // 2
        for index, group in enumerate((pending[:midpoint], pending[midpoint:])):
            for job in group:
                self._fail_item(
                    self.records[job.unit_id],
                    job.item_id,
                    job.stage,
                    "model response was truncated; retrying a smaller batch",
                    retry=True,
                    code="model_response_truncated",
                )
                record = self.records[job.unit_id]
                item = record.items[job.item_id]
                if item.status != ItemStatus.RETRY_WAIT:
                    continue
                failure = dict(item.failure or {}) | {
                    "code": "truncated_batch",
                    "split_id": f"{manifest.request_id}:{index}",
                }
                self._save(
                    record, items=dict(record.items) | {job.item_id: item.model_copy(update={"failure": failure})}
                )

    async def _run_batch(self, jobs: tuple[_Job, ...]) -> None:
        if not jobs:
            return
        stage = jobs[0].stage
        request_id = "tx-" + uuid4().hex
        items = [self._payload_item(job) for job in jobs]
        payload = {
            "protocol": "epubox-text-1" if stage == "translate" else "epubox-review-2",
            "request_id": request_id,
            "items": items,
        }
        if stage == "translate":
            payload["target_language"] = "zh-Hans"
        output_tokens = recommended_output_tokens(
            items, self.planner_config, stage="translation" if stage == "translate" else "review"
        )
        input_tokens = model_input_budget(stage, payload)["estimated_input_tokens"]
        if input_tokens > MAX_MODEL_INPUT_TOKENS:
            if len(jobs) > 1:
                midpoint = len(jobs) // 2
                await self._run_batch(jobs[:midpoint])
                await self._run_batch(jobs[midpoint:])
            else:
                job = next(iter(jobs))
                self._fail_item(
                    self.records[job.unit_id],
                    job.item_id,
                    stage,
                    f"model input exceeds {MAX_MODEL_INPUT_TOKENS} tokens: {input_tokens}",
                    retry=False,
                    code="model_input_oversized",
                )
            return
        if output_tokens > self.output_tokens:
            if len(jobs) > 1:
                midpoint = len(jobs) // 2
                await self._run_batch(jobs[:midpoint])
                await self._run_batch(jobs[midpoint:])
                return
            upgraded: set[str] = set()
            for job in jobs:
                if job.unit_id not in upgraded and self._upgrade_cut_plan(
                    job.unit_id, "request output budget exceeded"
                ):
                    upgraded.add(job.unit_id)
                elif job.unit_id not in upgraded:
                    self._fail_item(
                        self.records[job.unit_id],
                        job.item_id,
                        stage,
                        "request output budget exceeded",
                        retry=False,
                        code=("translation_output_oversized" if stage == "translate" else "review_output_oversized"),
                    )
            return
        manifest = self._manifest(request_id, stage, jobs, payload, output_tokens)
        manifest = self.store.write_request(manifest)
        self._request_cache[request_id] = manifest
        for unit_id in dict.fromkeys(job.unit_id for job in jobs):
            record = self.records[unit_id]
            items = dict(record.items)
            for job in jobs:
                if job.unit_id == unit_id:
                    items[job.item_id] = items[job.item_id].model_copy(
                        update={
                            "stage": stage,
                            "status": ItemStatus.IN_FLIGHT,
                            "request_id": request_id,
                            "failure": None,
                        }
                    )
            self._save(record, items=items)
        encoded = json.dumps(request_messages(stage, payload), ensure_ascii=False, sort_keys=True)
        estimated_tokens = count_tokens(encoded) + output_tokens
        try:
            response = await self.runtime.invoke(
                stage,
                payload,
                {
                    "request_id": request_id,
                    "item_ids": tuple(job.item_id for job in jobs),
                    "estimated_tokens": estimated_tokens,
                    "output_tokens": output_tokens,
                },
            )
            if response.get("finish_reason") == "length":
                self._mark_truncated_batch(manifest, jobs, response["raw"])
                return
            self._apply_response(manifest, jobs, response["raw"])
        except (ProtocolError, MalformedEnvelopeError, RequestError) as error:
            for job in jobs:
                retry = not isinstance(error, RequestError) or error.attempts > 0
                code = (
                    "translation_protocol_rejected"
                    if stage == "translate" and isinstance(error, (ProtocolError, MalformedEnvelopeError))
                    else "review_protocol_rejected"
                    if stage == "review" and isinstance(error, (ProtocolError, MalformedEnvelopeError))
                    else "request_failed"
                )
                self._fail_item(self.records[job.unit_id], job.item_id, stage, str(error), retry=retry, code=code)

    def _manifest(
        self,
        request_id: str,
        stage: Literal["translate", "review"],
        jobs: tuple[_Job, ...],
        payload: dict[str, Any],
        output_tokens: int,
    ) -> RequestManifest:
        records = {job.unit_id: self.records[job.unit_id] for job in jobs}
        segments = {job.item_id: _segment(records[job.unit_id], job.item_id) for job in jobs}
        return RequestManifest(
            request_id=request_id,
            stage=stage,
            owner_kind="translation_item",
            owner_id=jobs[0].item_id,
            item_ids=tuple(job.item_id for job in jobs),
            input_hashes={job.item_id: records[job.unit_id].input_hash or "" for job in jobs},
            wire_hash=wire_hash(stage, payload, output_tokens),
            record_versions={unit_id: record.record_version + 1 for unit_id, record in records.items()},
            item_unit_ids={job.item_id: (job.unit_id,) for job in jobs},
            unit_document_ids={unit_id: record.document_id for unit_id, record in records.items()},
            plan_epochs={unit_id: record.plan_epoch for unit_id, record in records.items()},
            revisions={unit_id: record.revision for unit_id, record in records.items()},
            target_hashes={
                job.item_id: records[job.unit_id].items[job.item_id].target_hash or ""
                for job in jobs
                if stage == "review"
            },
            glossary_file_sha256=self.book.glossary_file_sha256,
            freeze_id=self.book.freeze_id,
            term_ids_by_item={job.item_id: segments[job.item_id].selected_term_ids for job in jobs},
            terms_hashes={job.item_id: segments[job.item_id].terms_hash for job in jobs},
            context_hashes={job.item_id: segments[job.item_id].context_hash for job in jobs},
        )

    def _payload_item(self, job: _Job) -> dict[str, Any]:
        record = self.records[job.unit_id]
        unit = self.units[job.unit_id]
        segment = _segment(record, job.item_id)
        if source_token_count(segment.source_projection) > MAX_SOURCE_TOKENS:
            raise IdentityMismatch(f"source Segment exceeds {MAX_SOURCE_TOKENS} tokens: {segment.segment_id}")
        context = build_context(
            unit,
            self.documents[unit.document_id],
            documents=self.documents,
            reading_edges=self.reading_edges,
            context_chars=self.context_chars,
            context_index=self.context_index,
        )
        if canonical_hash(context) != segment.context_hash:
            raise ValueError(f"context identity changed: {job.item_id}")
        terms = [
            _term_payload(self.terms[term_id], segment.term_applicability[term_id])
            for term_id in segment.selected_term_ids
        ]
        if canonical_hash({"terms": terms}) != segment.terms_hash:
            raise ValueError(f"term identity changed: {job.item_id}")
        payload: dict[str, Any] = {
            "item_id": job.item_id,
            "source": segment.source_projection,
            "context": context,
            "terms": terms,
            "hints": context["hints"],
            "constraints": {
                ref_id: {
                    "parent_ref": entry.parent_ref,
                    "movement": entry.movement,
                    "fixed_order": list(entry.fixed_order),
                }
                for ref_id, entry in unit.registry.items()
            },
        }
        if job.stage == "review":
            item = record.items[job.item_id]
            payload.update(
                {
                    "target": item.target_projection,
                    "base_revision": record.revision,
                    "applicability": {
                        "terminology": any(value == "target" for value in segment.term_applicability.values()),
                        "bindings": bool(unit.registry),
                    },
                    "bindings": _bindings(segment.source_projection, item.target_projection or "", unit),
                }
            )
            required: list[Any] = [
                str(issue.get("message", ""))
                for issue in record.unresolved_issues
                if issue.get("code") == "blocking_coherence"
            ]
            required.extend(
                dict(issue)
                for issue in record.unresolved_issues
                if issue.get("code") == "blocking_review" and issue.get("item_id") == job.item_id
            )
            if required:
                payload["required_revision"] = required
        return payload

    def _apply_response(self, manifest: RequestManifest, jobs: tuple[_Job, ...], raw: str | bytes) -> None:
        if manifest.stage == "translate":
            parsed = validate_translation_response(raw, manifest.request_id, set(manifest.item_ids))
            for job in jobs:
                if job.item_id not in parsed.accepted:
                    message = parsed.errors.get(job.item_id, "translation item missing")
                    self._fail_item(
                        self.records[job.unit_id],
                        job.item_id,
                        "translate",
                        message,
                        retry=True,
                        code="translation_protocol_rejected",
                    )
                    continue
                try:
                    self._apply_translation(manifest, job, str(parsed.accepted[job.item_id]["target"]))
                except ProjectionError as error:
                    self._fail_item(
                        self.records[job.unit_id],
                        job.item_id,
                        "translate",
                        str(error),
                        retry=True,
                        code="projection_protocol_rejected",
                    )
                except (ValueError, ProtocolError) as error:
                    self._fail_item(
                        self.records[job.unit_id],
                        job.item_id,
                        "translate",
                        str(error),
                        retry=True,
                        code="translation_protocol_rejected",
                    )
            return
        expected = {
            job.item_id: {
                "base_revision": manifest.revisions[job.unit_id],
                "terminology_applicable": any(
                    value == "target"
                    for value in _segment(self.records[job.unit_id], job.item_id).term_applicability.values()
                ),
                "bindings_applicable": bool(self.units[job.unit_id].registry),
            }
            for job in jobs
        }
        parsed = validate_review_response(raw, manifest.request_id, expected)
        for job in jobs:
            if job.item_id not in parsed.accepted:
                message = parsed.errors.get(job.item_id, "review item missing")
                self._fail_item(
                    self.records[job.unit_id],
                    job.item_id,
                    "review",
                    message,
                    retry=True,
                    code="review_protocol_rejected",
                )
                continue
            try:
                self._apply_review(
                    manifest,
                    job,
                    parsed.accepted[job.item_id],
                    parsed.rejected_suggestions.get(job.item_id, ()),
                )
            except (ValueError, ProtocolError) as error:
                self._fail_item(
                    self.records[job.unit_id],
                    job.item_id,
                    "review",
                    str(error),
                    retry=True,
                    code="review_protocol_rejected",
                )

    def _apply_translation(self, manifest: RequestManifest, job: _Job, target: str) -> None:
        record = self._current_for(manifest, job)
        unit = self.units[job.unit_id]
        segment = _segment(record, job.item_id)
        validate_projection(segment.source_projection, target, unit.registry)
        degeneration = find_degenerate_translation(plain_text(segment.source_projection), plain_text(target))
        if degeneration:
            raise ProtocolError(degeneration)
        item = record.items[job.item_id].model_copy(
            update={
                "status": ItemStatus.LOCAL_VALID,
                "target_projection": target,
                "target_hash": canonical_hash(target),
                "request_id": manifest.request_id,
                "failure": None,
                "next_action": "review",
            }
        )
        self._save(record, items=dict(record.items) | {job.item_id: item}, candidate=None, review=None)

    def _apply_review(
        self,
        manifest: RequestManifest,
        job: _Job,
        result: Mapping[str, Any],
        rejected_suggestions: tuple[str, ...],
    ) -> None:
        record = self._current_for(manifest, job)
        item = record.items[job.item_id]
        feedback = _feedback(
            record,
            job.item_id,
            manifest.request_id,
            result.get("term_suggestions", ()),
            rejected_suggestions,
            self.documents[record.document_id],
            self.units[job.unit_id],
        )
        decision = result["decision"]
        blocking_review = any(
            issue.get("code") == "blocking_review" and issue.get("item_id") == job.item_id
            for issue in record.unresolved_issues
        )
        if blocking_review and decision != "replace":
            self._fail_item(
                record,
                job.item_id,
                "review",
                "automatic review recovery requires a replacement target",
                retry=False,
                code="review_replacement_required",
            )
            return
        if decision == "needs_attention":
            issue_values: list[JsonValue] = [dict(issue) for issue in result.get("issues", ())]
            self._fail_item(
                record,
                job.item_id,
                "review",
                str(result.get("issues", "review needs attention")),
                retry=False,
                code="review_needs_attention",
                details={"issues": issue_values},
            )
            if feedback:
                current = self.records[job.unit_id]
                self._save(current, term_feedback=_dedupe_feedback((*current.term_feedback, *feedback)))
            return
        if decision == "replace":
            review_cycle = record.counters.get("review_cycle", 0)
            replacement_key = f"replacement_cycle:{job.item_id}"
            has_item_cycles = any(key.startswith("replacement_cycle:") for key in record.counters)
            if record.counters.get(replacement_key) == review_cycle or (
                not has_item_cycles and record.counters.get("replacement_cycle") == review_cycle
            ):
                self._fail_item(
                    record,
                    job.item_id,
                    "review",
                    "replacement review limit exhausted",
                    retry=False,
                    code="replacement_review_limit_exhausted",
                )
                return
            target = str(result["target"])
            segment = _segment(record, job.item_id)
            validate_projection(segment.source_projection, target, self.units[job.unit_id].registry)
            counters = dict(record.counters) | {"replacement_cycle": review_cycle, replacement_key: review_cycle}
            replaced = item.model_copy(
                update={
                    "status": ItemStatus.LOCAL_VALID,
                    "target_projection": target,
                    "target_hash": canonical_hash(target),
                    "checks": {"replacement_requested": True, "previous": result["checks"]},
                    "request_id": manifest.request_id,
                    "failure": None,
                    "next_action": "review",
                }
            )
            revised_items = {
                item_id: current.model_copy(
                    update={
                        "status": ItemStatus.LOCAL_VALID,
                        "checks": {},
                        "request_id": None,
                        "failure": None,
                        "next_action": "review",
                    }
                )
                for item_id, current in record.items.items()
            }
            revised_items[job.item_id] = replaced
            self._save(
                record,
                revision=record.revision + 1,
                items=revised_items,
                candidate=None,
                local_checks={},
                review=None,
                accepted_revision=None,
                accepted_target_hash=None,
                counters=counters,
                term_feedback=_dedupe_feedback((*record.term_feedback, *feedback)),
                unresolved_issues=tuple(
                    issue
                    for issue in record.unresolved_issues
                    if issue.get("code") != "blocking_coherence"
                    and not (issue.get("code") == "blocking_review" and issue.get("item_id") == job.item_id)
                ),
            )
            return
        if any(issue.get("code") == "blocking_coherence" for issue in record.unresolved_issues):
            self._fail_item(
                record,
                job.item_id,
                "review",
                "coherence revision requires a replacement target",
                retry=False,
                code="review_replacement_required",
            )
            return
        reviewed = item.model_copy(
            update={
                "status": ItemStatus.REVIEWED,
                "checks": {"decision": "no_change", "checks": result["checks"], "issues": result["issues"]},
                "request_id": manifest.request_id,
                "failure": None,
                "next_action": None,
            }
        )
        self._save(
            record,
            items=dict(record.items) | {job.item_id: reviewed},
            term_feedback=_dedupe_feedback((*record.term_feedback, *feedback)),
        )

    def _current_for(self, manifest: RequestManifest, job: _Job) -> UnitRecord:
        record = self.store.read_unit(job.unit_id)
        item = record.items[job.item_id]
        segment = _segment(record, job.item_id)
        if (
            manifest.stage != job.stage
            or manifest.freeze_id != self.book.freeze_id
            or manifest.glossary_file_sha256 != self.book.glossary_file_sha256
            or manifest.item_unit_ids.get(job.item_id) != (job.unit_id,)
            or manifest.unit_document_ids.get(job.unit_id) != record.document_id
            or record.record_version < manifest.record_versions[job.unit_id]
            or item.request_id != manifest.request_id
            or record.input_hash != manifest.input_hashes[job.item_id]
            or record.plan_epoch != manifest.plan_epochs[job.unit_id]
            or record.revision != manifest.revisions[job.unit_id]
            or segment.selected_term_ids != manifest.term_ids_by_item[job.item_id]
            or segment.terms_hash != manifest.terms_hashes[job.item_id]
            or segment.context_hash != manifest.context_hashes[job.item_id]
            or (manifest.stage == "review" and item.target_hash != manifest.target_hashes[job.item_id])
        ):
            raise ProtocolError(f"stale response identity: {job.item_id}")
        self.records[job.unit_id] = record
        return record

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
    ) -> None:
        current = self.store.read_unit(record.unit_id)
        item = current.items[item_id]
        limit = self._logical_limit(current, item_id, stage)
        blocking_review = stage == "review" and any(
            issue.get("code") == "blocking_review" and issue.get("item_id") == item_id
            for issue in current.unresolved_issues
        )
        retry = (
            retry
            and not blocking_review
            and self._logical_calls(item_id, stage, current.revision if stage == "review" else None) < limit
            and max(current.counters.get("http_attempts", 0), self._spent_by_unit.get(current.unit_id, 0))
            < self._unit_limit(current)
        )
        failed = item.model_copy(
            update={
                "stage": stage,
                "status": ItemStatus.RETRY_WAIT if retry else ItemStatus.NEEDS_ATTENTION,
                "failure": {"stage": stage, "code": code, "message": message[:2000], **dict(details or {})},
                "next_action": stage if retry else "repair",
            }
        )
        self._save(current, items=dict(current.items) | {item_id: failed})

    def _fail_remaining(self, record: UnitRecord, code: str, message: str) -> None:
        current = self.store.read_unit(record.unit_id)
        items = {
            item_id: item
            if item.status == ItemStatus.REVIEWED
            else item.model_copy(
                update={
                    "status": ItemStatus.NEEDS_ATTENTION,
                    "failure": {"stage": item.stage, "code": code, "message": message[:2000]},
                    "next_action": "repair",
                }
            )
            for item_id, item in current.items.items()
        }
        self._save(current, items=items)


async def run_translation(
    work_dir: Path | str,
    *,
    model: Any = None,
    transport: Any = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> TranslationRunResult:
    return await TranslationEngine(RunStore(work_dir), model=model, transport=transport, progress=progress).run()


def retry_failed_units(store: RunStore, unit_ids: Sequence[str]) -> tuple[UnitRecord, ...]:
    """Explicitly reactivate selected failures without resetting counters."""
    with store.lock():
        validate_retry_failed_units(store, unit_ids)
        updated: list[UnitRecord] = []
        for unit_id in dict.fromkeys(unit_ids):
            record = store.read_unit(unit_id)
            counters = dict(record.counters)
            items = {
                item_id: item.model_copy(
                    update={"status": ItemStatus.RETRY_WAIT, "failure": None, "next_action": item.stage}
                )
                if item.status == ItemStatus.NEEDS_ATTENTION
                else item
                for item_id, item in record.items.items()
            }
            for item_id, item in record.items.items():
                if item.status != ItemStatus.NEEDS_ATTENTION:
                    continue
                stage = _item_failure_stage(item)
                key = f"explicit_retry_{stage}:{item_id}"
                counters[key] = counters.get(key, 0) + 1
            if items == record.items:
                continue
            updated.append(
                store.save_unit(
                    record.model_copy(
                        update={"record_version": record.record_version + 1, "items": items, "counters": counters}
                    ),
                    expected_record_version=record.record_version,
                )
            )
        return tuple(updated)


def _item_failure_stage(item: ItemRecord) -> Literal["translate", "review"]:
    failure = item.failure or {}
    return "review" if failure.get("stage") == "review" or item.target_projection is not None else "translate"


def _recoverable_failure(
    item: ItemRecord,
) -> tuple[Literal["translate", "review"], str, list[dict[str, JsonValue]]] | None:
    failure = item.failure or {}
    stage = failure.get("stage")
    code = failure.get("code")
    message = failure.get("message")
    if not isinstance(message, str):
        return None
    if stage == "translate":
        if item.target_projection is not None:
            return None
        if code == "model_response_truncated" or (
            code == "request_failed" and message == "model response was truncated"
        ):
            return "translate", "truncated", []
        typed = {"translation_protocol_rejected", "projection_protocol_rejected", "translation_output_oversized"}
        prefixes = (
            "unknown projection marker:",
            "marker inventory mismatch",
            "crossed or unmatched target close marker:",
            "unclosed target reference:",
            "duplicate target reference:",
            "text moved across a protected range or boundary",
        )
        if code in typed or (code == "request_failed" and message.startswith(prefixes)):
            return "translate", "protocol", []
        return None
    if stage != "review" or item.target_projection is None:
        return None
    if code == "replacement_review_limit_exhausted" or (
        code == "request_failed" and message == "replacement review limit exhausted"
    ):
        return "review", "replacement", [{"code": "replacement_limit", "severity": "major", "message": message}]
    if code == "review_needs_attention":
        issues = failure.get("issues")
    elif code == "request_failed":
        try:
            issues = ast.literal_eval(message)
        except (SyntaxError, ValueError):
            return None
    else:
        return None
    normalized = _normalized_review_issues(issues)
    return ("review", "needs_attention", normalized) if normalized else None


def _normalized_review_issues(issues: object) -> list[dict[str, JsonValue]]:
    if not isinstance(issues, list) or not issues:
        return []
    normalized: list[dict[str, JsonValue]] = []
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or not all(isinstance(issue.get(key), str) and issue.get(key) for key in ("code", "severity", "message"))
            or issue.get("severity") not in {"minor", "major", "critical"}
        ):
            return []
        normalized.append({key: issue[key] for key in ("code", "severity", "message")})
    return normalized


def validate_retry_failed_units(
    store: RunStore,
    unit_ids: Sequence[str],
    *,
    add_unit_http: int = 0,
) -> None:
    """Validate an entire retry action without changing Unit state."""
    if type(add_unit_http) is not int or add_unit_http < 0:
        raise ValueError("add_unit_http must be a non-negative integer")
    overrides = load_budget_overrides(store)
    additions = overrides.get("add_unit_http", {})
    for unit_id in dict.fromkeys(unit_ids):
        record = store.read_unit(unit_id)
        extra = additions.get(unit_id, 0) if isinstance(additions, dict) else 0
        used = max(record.counters.get("http_attempts", 0), _journal_spent_for_unit(store, unit_id))
        if used >= _unit_limit(record) + int(extra) + add_unit_http:
            raise ValueError(f"Unit HTTP budget remains exhausted: {unit_id}")


def import_repair_file(store: RunStore, path: Path | str) -> UnitRecord:
    """Import one complete version-bound target and require the normal review gate."""
    repaired = validate_repair_file(store, path)
    return store.save_unit(repaired, expected_record_version=repaired.record_version - 1)


def validate_repair_file(store: RunStore, path: Path | str) -> UnitRecord:
    """Validate a repair and return the proposed record without writing it."""
    value = strict_json_loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise TypeError("repair file must contain one JSON object")
    unit_id = value.get("unit_id")
    if not isinstance(unit_id, str):
        raise TypeError("repair file requires string unit_id")
    record = store.read_unit(unit_id)
    if value.get("base_revision") != record.revision or value.get("plan_epoch") != record.plan_epoch:
        raise ValueError("repair file is stale")
    if record.cut_plan is None:
        raise ValueError("repair target requires a CutPlan")
    preparation = store.read_preparation()
    document_id = preparation.unit_documents[unit_id]
    document = store.read_document(document_id, expected_hash=preparation.document_hashes[document_id])
    unit = next(item for item in document.units if item.unit_id == unit_id)
    raw_targets = value.get("targets")
    if raw_targets is None and len(record.items) == 1 and isinstance(value.get("target"), str):
        raw_targets = {next(iter(record.items)): value["target"]}
    if not isinstance(raw_targets, dict) or set(raw_targets) != set(record.items):
        raise ValueError("repair file must provide every current item target")
    items = dict(record.items)
    for item_id, target in raw_targets.items():
        if not isinstance(item_id, str) or not isinstance(target, str) or not target:
            raise ValueError("repair targets must be non-empty strings keyed by item_id")
        segment = _segment(record, item_id)
        validate_projection(segment.source_projection, target, unit.registry)
        items[item_id] = items[item_id].model_copy(
            update={
                "stage": "review",
                "status": ItemStatus.LOCAL_VALID,
                "target_projection": target,
                "target_hash": canonical_hash(target),
                "checks": {"manual_repair": True},
                "request_id": None,
                "failure": None,
                "next_action": "review",
            }
        )
    return record.model_copy(
        update={
            "record_version": record.record_version + 1,
            "revision": record.revision + 1,
            "items": items,
            "candidate": None,
            "accepted_revision": None,
            "accepted_target_hash": None,
            "local_checks": {},
            "review": None,
            "unresolved_issues": (),
        }
    )


def _segment(record: UnitRecord, item_id: str) -> Segment:
    if record.cut_plan is None:
        raise ValueError("Unit has no CutPlan")
    return next(segment for segment in record.cut_plan.segments if segment.item_id == item_id)


def _unit_limit(record: UnitRecord) -> int:
    planned = len(record.cut_plan.segments) if record.cut_plan is not None else 1
    return record.counters.get("unit_http_limit", 24 * max(1, planned))


def _journal_spent_for_unit(store: RunStore, unit_id: str) -> int:
    return sum(
        len(request.attempts)
        for path in (store.root / "requests").glob("*.json")
        for request in (store.read_request(path.stem),)
        if any(unit_id in request.item_unit_ids.get(item_id, ()) for item_id in request.item_ids)
    )


def _record_needs_attention(record: UnitRecord) -> bool:
    if record.derived is not None:
        return False
    return (
        record.cut_plan is None
        or any(item.status == ItemStatus.NEEDS_ATTENTION for item in record.items.values())
        or any(issue.get("code") in {"blocking_review", "blocking_coherence"} for issue in record.unresolved_issues)
    )


def _merge_candidate(unit: Unit, record: UnitRecord) -> str:
    if record.cut_plan is None:
        raise ValueError("Unit has no CutPlan")
    merged: list[Event] = []
    for segment in record.cut_plan.segments:
        target = record.items[segment.item_id].target_projection
        if target is None:
            raise ValueError("candidate is missing a Segment target")
        target_events = list(validate_projection(segment.source_projection, target, unit.registry))
        prefix = [value for value in segment.virtual_boundaries if value.startswith("+")]
        suffix = [value for value in segment.virtual_boundaries if value.startswith("-")]
        if [event.value for event in target_events[: len(prefix)]] != prefix:
            raise ValueError("virtual opening boundary moved")
        if suffix and [event.value for event in target_events[-len(suffix) :]] != suffix:
            raise ValueError("virtual closing boundary moved")
        merged.extend(target_events[len(prefix) : len(target_events) - len(suffix) if suffix else None])
    projection = events_to_projection(merged)
    validate_projection(unit, projection)
    return projection


def _term_payload(term: FrozenTerm, role: str) -> dict[str, JsonValue]:
    return {
        "term_id": term.term_id,
        "source": term.source,
        "target": term.target,
        "aliases": list(term.aliases),
        "scope": term.scope.model_dump(mode="json"),
        "mode": term.mode,
        "match_policy": term.match_policy,
        "note": term.note,
        "role": role,
    }


def _ranges(projection: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    stack: list[str] = []
    for event in parse_projection(projection):
        if event.kind == "text":
            for ref in stack:
                result[ref].append(event.value)
        elif event.value.startswith("+g"):
            stack.append(event.value[1:])
            result.setdefault(event.value[1:], [])
        elif event.value.startswith("-g"):
            stack.pop()
    return {ref: "".join(text) for ref, text in result.items()}


def _bindings(source: str, target: str, unit: Unit) -> list[dict[str, str]]:
    source_ranges, target_ranges = _ranges(source), _ranges(target)
    bindings = [
        {"ref": ref, "source": text, "target": target_ranges.get(ref, "")} for ref, text in source_ranges.items()
    ]
    bindings.extend(
        {"ref": ref, "source": entry.source_text[:400], "target_context": ""}
        for ref, entry in unit.registry.items()
        if entry.boundary_type == "footnote" and f"⟦={ref}⟧" in source
    )
    return bindings


def _feedback(
    record: UnitRecord,
    item_id: str,
    request_id: str,
    suggestions: Sequence[Mapping[str, Any]],
    rejected: tuple[str, ...],
    document: DocumentPlan,
    unit: Unit,
) -> tuple[dict[str, JsonValue], ...]:
    views = {view_id: document.source_views[view_id] for view_id in unit.source_view_ids}
    feedback: list[dict[str, JsonValue]] = [
        {"kind": "rejected_term_suggestion", "item_id": item_id, "request_id": request_id, "message": message}
        for message in rejected
    ]
    for suggestion in suggestions:
        evidence = suggestion.get("evidence", ())
        source = suggestion.get("source")
        verified = (
            [_verified_feedback_evidence(document, views, source, citation) for citation in evidence]
            if isinstance(source, str) and isinstance(evidence, list)
            else []
        )
        if not evidence or not verified or any(citation is None for citation in verified):
            feedback.append(
                {
                    "kind": "rejected_term_suggestion",
                    "item_id": item_id,
                    "request_id": request_id,
                    "message": "term suggestion evidence does not match the frozen Unit source views",
                }
            )
            continue
        saved = dict(suggestion)
        saved["evidence"] = [citation for citation in verified if citation is not None]
        feedback.append(
            {
                "kind": "term_suggestion",
                "unit_id": record.unit_id,
                "item_id": item_id,
                "request_id": request_id,
                "base_revision": record.revision,
                "suggestion": saved,
            }
        )
    return tuple(feedback)


def _verified_feedback_evidence(
    document: DocumentPlan,
    views: Mapping[str, SourceTextView],
    source: str,
    citation: object,
) -> dict[str, JsonValue] | None:
    if not isinstance(citation, dict):
        return None
    view_id, quote = citation.get("view_id"), citation.get("source_quote")
    if not isinstance(view_id, str) or not isinstance(quote, str) or view_id not in views:
        return None
    view = views[view_id]
    starts = _occurrences(view.text, quote)
    if len(starts) != 1 or not _has_bounded_occurrence(quote, source):
        return None
    refs = _slice_view_refs(document, view.source_refs, starts[0], starts[0] + len(quote), quote)
    if refs is None:
        return None
    result: dict[str, JsonValue] = {
        "view_id": view_id,
        "source_quote": quote,
        "view_hash": view.view_hash,
        "source_refs": refs,
    }
    return result


def _slice_view_refs(
    document: DocumentPlan,
    refs: Sequence[SourceRef],
    start: int,
    end: int,
    expected: str,
) -> list[JsonValue] | None:
    result: list[JsonValue] = []
    rebuilt: list[str] = []
    cursor = 0
    for ref in refs:
        length = ref.end - ref.start
        overlap_start = max(start, cursor)
        overlap_end = min(end, cursor + length)
        if overlap_start < overlap_end:
            source_start = ref.start + overlap_start - cursor
            source_end = ref.start + overlap_end - cursor
            result.append({"slot_id": ref.slot_id, "start": source_start, "end": source_end})
            rebuilt.append(document.source_slots[ref.slot_id].source_value[source_start:source_end])
        cursor += length
    return result if cursor >= end and "".join(rebuilt) == expected else None


def _occurrences(text: str, phrase: str) -> tuple[int, ...]:
    if not phrase:
        return ()
    result: list[int] = []
    start = 0
    while (position := text.find(phrase, start)) >= 0:
        result.append(position)
        start = position + 1
    return tuple(result)


def _has_bounded_occurrence(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return any(
        (not _word_char(phrase[0]) or start == 0 or not _word_char(text[start - 1]))
        and (
            not _word_char(phrase[-1]) or start + len(phrase) == len(text) or not _word_char(text[start + len(phrase)])
        )
        for start in _occurrences(text, phrase)
    )


def _word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _dedupe_feedback(values: Sequence[dict[str, JsonValue]]) -> tuple[dict[str, JsonValue], ...]:
    unique: dict[str, dict[str, JsonValue]] = {}
    for value in values:
        unique.setdefault(canonical_hash(value), value)
    return tuple(unique.values())


def _planner_config(config: Mapping[str, JsonValue]) -> PlannerConfig:
    context = _positive_int(config.get("context_tokens", config.get("max_context_tokens")), 8192)
    output = _positive_int(config.get("max_output_tokens"), 2048)
    return PlannerConfig(
        context_tokens=context,
        max_source_tokens=_positive_int(config.get("max_source_tokens"), MAX_SOURCE_TOKENS),
        max_input_tokens=_optional_positive_int(config.get("max_input_tokens")),
        max_output_tokens=output,
        review_output_tokens=_positive_int(config.get("review_output_tokens"), min(768, output)),
        safety_margin=_positive_int(config.get("safety_margin"), 256),
        translation_overhead=_positive_int(config.get("translation_overhead"), 256),
        review_overhead=_positive_int(config.get("review_overhead"), 512),
        target_ratio=_positive_number(config.get("target_ratio"), 1.6),
        max_batch_items=_positive_int(config.get("max_batch_items"), 8),
    )


def _nonnegative_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 0:
        raise ValueError("configuration value must be a non-negative integer")
    return value


def _positive_int(value: Any, default: int) -> int:
    result = _nonnegative_int(value, default)
    if result < 1:
        raise ValueError("configuration value must be positive")
    return result


def _optional_positive_int(value: Any) -> int | None:
    return None if value is None else _positive_int(value, 1)


def _positive_number(value: Any, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError("configuration value must be a positive number")
    return float(value)


__all__ = [
    "TranslationEngine",
    "TranslationRunResult",
    "import_repair_file",
    "retry_failed_units",
    "run_translation",
    "validate_repair_file",
    "validate_retry_failed_units",
]
