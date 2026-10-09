"""Bounded, resumable extraction of terminology from frozen source views."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from engine.agents.protocol import ProtocolError
from engine.agents.runtime import (
    MAX_MODEL_INPUT_TOKENS,
    TERM_PROMPT_VERSION,
    ModelRuntime,
    RequestError,
    RuntimePaused,
    model_input_budget,
    wire_hash,
)
from engine.agents.terms import validate_terms_response
from engine.schemas.contracts import (
    Attempt,
    ExtractionItem,
    RequestManifest,
    TermExtractionRecord,
    Usage,
)
from engine.services import state
from engine.services.atomic import StoreError
from engine.services.coherence import load_budget_overrides
from engine.services.store import RunStore
from engine.services.terms.planning import (
    ATOMIC_TERM_PLANNER_VERSION,
    _tail_start,
    _term_applies,
    _term_occurs,
    _unit_lanes,
)


class TermBudgetPaused(RuntimeError):
    pass


def _response_truncated(finish_reason: object) -> bool:
    return finish_reason in {"length", "max_tokens"}


@dataclass(frozen=True)
class TermRunResult:
    status: Literal["closed", "closed_with_gaps", "paused", "disabled", "not_required"]
    succeeded: int
    failed: int
    pending: int
    http_attempts: int
    reason: str | None = None


class TermRunner:
    """One writer owns extraction records; HTTP attempts are durable before dispatch."""

    def __init__(self, store: RunStore, *, model: Any = None, transport: Any = None):
        self.store = store
        self.preparation = store.read_preparation()
        self.plan = store.read_term_plan()
        store.write_term_plan(self.plan)
        self.documents = {
            document_id: store.read_document(document_id, expected_hash=digest)
            for document_id, digest in self.preparation.document_hashes.items()
        }
        self._source_views = {
            view_id: view for document in self.documents.values() for view_id, view in document.source_views.items()
        }
        self._view_lanes = {}
        for document in self.documents.values():
            lanes = _unit_lanes(document)
            self._view_lanes.update(
                {view_id: lanes[unit.unit_id] for unit in document.units for view_id in unit.source_view_ids}
            )
        self._user_terms = {term.term_id: term for term in self.preparation.user_terms}
        self._records: dict[str, TermExtractionRecord] = {}
        config = self.preparation.extraction_config
        if config.get("prompt_version") != TERM_PROMPT_VERSION or config.get("target_language") != "zh-Hans":
            raise ValueError("frozen terminology prompt or target language does not match this runtime")
        if not isinstance(config.get("model"), str) or not config["model"]:
            raise ValueError("frozen terminology model identity is missing")
        if model is not None and getattr(model, "id", config["model"]) != config["model"]:
            raise ValueError("provider model differs from frozen terminology identity")
        self.config = config
        # Historical output limits are retained only as TPM traffic estimates.
        self.output_tokens = _positive_int(config.get("max_output_tokens"), 4096)
        self.max_concurrency = _positive_int(config.get("concurrency"), 2)
        configured_limit = _nonnegative_int(config.get("run_http_limit"), 0)
        self.run_limit = (configured_limit or self.plan.extraction_http_limit) + int(
            load_budget_overrides(store)["add_run_http"]
        )
        self._rebuild_journal()
        self.prior_input_limit_breach = self._prior_input_limit_breach()
        timeout = config.get("request_timeout_seconds", 120.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("request timeout must be positive seconds")
        self.runtime = ModelRuntime(
            model=model,
            transport=transport,
            rpm=_optional_positive_int(config.get("rpm")),
            tpm=_optional_positive_int(config.get("tpm")),
            max_inflight=self.max_concurrency,
            model_max_output_tokens=None,
            reserve_attempt=self._reserve,
            finish_attempt=self._finish,
            persist_response=self.store.save_model_response,
            prior_input_limit_breach=self.prior_input_limit_breach,
            input_budget_version=2,
            request_timeout_seconds=float(timeout),
        )
        self.max_concurrency = self.runtime.workflow_capacity

    def _requests(self) -> tuple[RequestManifest, ...]:
        for path in sorted(state.glob(self.store.root / "requests", "*.json")):
            modified = state.stat(path).st_mtime_ns
            if self._request_mtimes.get(path.stem) != modified:
                self._cache_request(self.store.read_request(path.stem))
                self._request_mtimes[path.stem] = modified
        return tuple(self._request_cache[request_id] for request_id in sorted(self._request_cache))

    def _rebuild_journal(self) -> None:
        self._request_cache: dict[str, RequestManifest] = {}
        self._spent_total = 0
        self._actual_http_total = 0
        self._spent_by_item: dict[str, int] = {}
        self._actual_by_item: dict[str, int] = {}
        self._logical_terms_by_item: dict[str, int] = {}
        self._request_mtimes: dict[str, int] = {}
        for path in sorted(state.glob(self.store.root / "requests", "*.json")):
            self._cache_request(self.store.read_request(path.stem))
            self._request_mtimes[path.stem] = state.stat(path).st_mtime_ns

    def _cache_request(self, request: RequestManifest) -> None:
        previous = self._request_cache.get(request.request_id)
        if previous == request:
            return
        if previous is not None:
            self._index_request(previous, -1)
        self._request_cache[request.request_id] = request
        self._index_request(request, 1)
        path = self.store._path("requests", request.request_id)
        if state.exists(path):
            self._request_mtimes[request.request_id] = state.stat(path).st_mtime_ns

    def _index_request(self, request: RequestManifest, direction: int) -> None:
        reserved = len(request.attempts)
        actual = sum(attempt.state != "reserved" for attempt in request.attempts)
        self._spent_total += direction * reserved
        self._actual_http_total += direction * actual
        for item_id in request.item_ids:
            self._spent_by_item[item_id] = self._spent_by_item.get(item_id, 0) + direction * reserved
            self._actual_by_item[item_id] = self._actual_by_item.get(item_id, 0) + direction * actual
            if request.stage == "terms" and actual:
                self._logical_terms_by_item[item_id] = self._logical_terms_by_item.get(item_id, 0) + direction

    def _prior_input_limit_breach(self) -> int | None:
        observed: list[int] = []
        for request in self._requests():
            for attempt in request.attempts:
                if attempt.usage is not None:
                    observed.append(attempt.usage.input_tokens)
                response = self.store.read_model_response(request.stage, request.request_id, attempt.attempt_id)
                if response is not None and response.usage is not None:
                    observed.append(response.usage.input_tokens)
        return max((value for value in observed if value > MAX_MODEL_INPUT_TOKENS), default=None)

    def _spent(self, item_id: str | None = None, *, actual: bool = False) -> int:
        if item_id is None:
            return self._actual_http_total if actual else self._spent_total
        return (self._actual_by_item if actual else self._spent_by_item).get(item_id, 0)

    def _logical_calls(self, item_id: str) -> int:
        return self._logical_terms_by_item.get(item_id, 0)

    def _reserve(self, request_id: str, attempt: Any) -> None:
        with self.store.lock():
            self._require_preflight()
            manifest = self.store.read_request(request_id)
            if self._spent() >= self.plan.extraction_http_limit or self._spent() >= self.run_limit:
                raise TermBudgetPaused("run terminology HTTP budget exhausted")
            if manifest.stage == "terms":
                limits = {item.item_id: item.http_limit for item in self.plan.items}
                exhausted = next(
                    (item_id for item_id in manifest.item_ids if self._spent(item_id) >= limits[item_id]),
                    None,
                )
                if exhausted is not None:
                    raise RequestError(f"term preparation item HTTP budget exhausted: {exhausted}", attempts=0)
            elif manifest.stage == "resolution":
                item_id = manifest.owner_id
                if self._spent(item_id) >= 3:
                    raise RequestError(f"term preparation item HTTP budget exhausted: {item_id}", attempts=0)
            else:
                raise ValueError("terminology runner cannot reserve a translation request")
            updated = self.store.reserve_attempt(request_id, Attempt.model_validate(attempt.model_dump(mode="python")))
            self._cache_request(updated)

    def _finish(self, request_id: str, attempt_id: str, **fields: Any) -> None:
        usage = fields.get("usage")
        if usage is not None:
            fields["usage"] = Usage.model_validate(usage.model_dump(mode="python"))
        self._cache_request(self.store.finish_attempt(request_id, attempt_id, **fields))

    def _record(self, item: ExtractionItem) -> TermExtractionRecord:
        if state.exists(self.store._path("glossary/extraction", item.item_id)):
            return self.store.read_extraction(item.item_id)
        return self.store.save_extraction(
            TermExtractionRecord(
                item_id=item.item_id,
                document_id=item.document_id,
                view_ids=item.view_ids,
                extraction_input_hash=item.extraction_input_hash,
            )
        )

    def _save(self, record: TermExtractionRecord, **updates: Any) -> TermExtractionRecord:
        saved = self.store.save_extraction(
            record.model_copy(update={"record_version": record.record_version + 1, **updates}),
            expected_record_version=record.record_version,
        )
        self._records[record.item_id] = saved
        return saved

    def _payload_item(self, item: ExtractionItem, retry_feedback: tuple[str, ...] = ()) -> dict[str, Any]:
        document = self.documents[item.document_id]
        views: dict[str, str] = {}
        for interval in item.primary_ranges:
            view_id = interval["view_id"]
            view = document.source_views[str(view_id)]
            start, end = interval["start"], interval["end"]
            if type(start) is not int or type(end) is not int:
                raise ValueError("planned primary range must use integer offsets")
            views[str(view_id)] = view.text[start:end]
        context: dict[str, list[str]] = {}
        for interval in item.context_ranges:
            view_id, start, end = interval["view_id"], interval["start"], interval["end"]
            if not isinstance(view_id, str) or type(start) is not int or type(end) is not int:
                raise ValueError("planned context range has invalid identity")
            view = self._source_views[view_id]
            context.setdefault(view_id, []).append(view.text[start:end])
        user_terms = [
            self._user_terms[term_id].model_dump(mode="json") | {"role": role}
            for role, ids in (("target", item.user_term_ids), ("context", item.context_user_term_ids))
            for term_id in ids
        ]
        return {
            "item_id": item.item_id,
            "document_id": item.document_id,
            "views": views,
            "context": context,
            "hints": [],
            "user_terms": user_terms,
            **({"retry_feedback": list(retry_feedback)} if retry_feedback else {}),
        }

    def _payload(
        self,
        items: tuple[ExtractionItem, ...],
        request_id: str,
        feedback: dict[str, tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        feedback = feedback or {}
        payload = {
            "protocol": "epubox-terms-1",
            "request_id": request_id,
            "target_language": self.config["target_language"],
            "items": [self._payload_item(item, feedback.get(item.item_id, ())) for item in items],
        }
        # Context belongs to the complete request, never independently to every item.
        ranges = [interval for item in items for interval in item.primary_ranges]
        first = items[0]
        view_order = {
            view_id: index
            for index, view_id in enumerate(
                view_id for unit in self.documents[first.document_id].units for view_id in unit.source_view_ids
            )
        }
        first_range = first.primary_ranges[0]
        first_view, first_start, _ = _interval(first_range)
        first_position = (view_order[first_view], first_start)
        candidates: dict[tuple[str, int, int], dict[str, Any]] = {}
        for item in items:
            for interval in item.context_ranges:
                view_id, start, end = _interval(interval)
                view = self._source_views[view_id]
                if view.document_id != first.document_id or view_id not in view_order:
                    continue
                if self._view_lanes[view_id] != self._view_lanes[first_view]:
                    continue
                if (view_order[view_id], end) > first_position:
                    continue
                if any(
                    _interval(part)[0] == view_id and start < _interval(part)[2] and _interval(part)[1] < end
                    for part in ranges
                ):
                    continue
                start = max(start, _tail_start(view.text, end, 400))
                candidates[(view_id, start, end)] = {"view_id": view_id, "text": view.text[start:end]}
        ordered = sorted(candidates, key=lambda value: (view_order[value[0]], value[2]))[-2:]
        shared_channel = all(
            item.document_id == first.document_id
            and all(self._view_lanes[view_id] == self._view_lanes[first_view] for view_id in item.view_ids)
            for item in items
        )
        payload["context"] = [candidates[key] for key in ordered] if shared_channel else []
        for wire_item in payload["items"]:
            wire_item.pop("context", None)
        self._filter_context_terms(payload)
        return payload

    def _filter_context_terms(self, payload: dict[str, Any]) -> None:
        for wire_item in payload["items"]:
            wire_item["user_terms"] = [
                term
                for term in wire_item["user_terms"]
                if term["role"] == "target"
                or any(
                    _term_applies(self._user_terms[term["term_id"]], self._source_views[context["view_id"]])
                    and _term_occurs(self._user_terms[term["term_id"]], context["text"])
                    for context in payload["context"]
                )
            ]

    def _accept_response(
        self,
        item: ExtractionItem,
        record: TermExtractionRecord,
        request_id: str,
        raw: str,
        expected_item_ids: set[str] | None = None,
    ) -> TermExtractionRecord:
        parsed = validate_terms_response(raw, request_id, expected_item_ids or {item.item_id})
        if item.item_id in parsed.errors:
            raise ProtocolError(f"invalid term item response: {parsed.errors[item.item_id]}")
        if item.item_id in parsed.missing:
            raise ProtocolError(f"term response omitted item: {item.item_id}")
        values = parsed.accepted[item.item_id]
        rejected = parsed.rejected_candidates.get(item.item_id, ())
        from engine.schemas.contracts import TermCandidateRejection, canonical_hash

        schema_rejections = tuple(
            TermCandidateRejection(
                rejection_id=f"tcr-{canonical_hash({'item_id': item.item_id, 'request_id': request_id, 'candidate_index': entry.candidate_index})[:24]}",
                extraction_item_id=item.item_id,
                request_id=request_id,
                candidate_index=entry.candidate_index,
                reason=entry.reason,
                source=entry.source,
                target=entry.target,
                category=entry.category,
            )
            for entry in parsed.schema_rejections.get(item.item_id, ())
        )
        merged_rejections = {
            rejection.rejection_id: rejection for rejection in (*record.rejections, *schema_rejections)
        }
        rejections = tuple(merged_rejections[key] for key in sorted(merged_rejections))
        if not values and rejected:
            return self._save(
                record,
                status="retry_wait",
                rejections=rejections,
                diagnostics=(
                    *record.diagnostics,
                    *({"code": "rejected_schema", "reason": reason, "request_id": request_id} for reason in rejected),
                ),
                counters={
                    "http_attempts": self._spent(item.item_id, actual=True),
                    "reserved_attempts": self._spent(item.item_id),
                },
            )
        from engine.services.terms.candidates import CandidateProposal, EvidenceProposal, validate_candidate_proposals

        proposals = tuple(
            CandidateProposal(
                source=value["source"],
                target=value["target"],
                category=value["category"],
                aliases=tuple(value["aliases"]),
                scope_hint=value["scope_hint"],
                note=value["note"],
                evidence=tuple(EvidenceProposal(**citation) for citation in value["evidence"]),
            )
            for value in values
        )
        checked = validate_candidate_proposals(self.documents[item.document_id], item, proposals)
        merged = {candidate.candidate_id: candidate for candidate in (*record.candidates, *checked.candidates)}
        candidates = tuple(merged[candidate_id] for candidate_id in sorted(merged))
        diagnostics = (
            *record.diagnostics,
            *(diagnostic | {"request_id": request_id} for diagnostic in checked.diagnostics),
            *({"code": "rejected_schema", "reason": message, "request_id": request_id} for message in rejected),
        )
        if proposals and not any(candidate.status == "proposed" for candidate in checked.candidates):
            return self._save(
                record,
                status="retry_wait",
                candidates=candidates,
                rejections=rejections,
                diagnostics=diagnostics,
                counters={
                    "http_attempts": self._spent(item.item_id, actual=True),
                    "reserved_attempts": self._spent(item.item_id),
                },
            )
        return self._save(
            record,
            status="succeeded_with_rejections" if diagnostics else "succeeded",
            candidates=candidates,
            rejections=rejections,
            diagnostics=diagnostics,
            counters={
                "http_attempts": self._spent(item.item_id, actual=True),
                "reserved_attempts": self._spent(item.item_id),
            },
        )

    def _replay_response(self, item: ExtractionItem, record: TermExtractionRecord) -> TermExtractionRecord:
        if record.status not in {"pending", "in_flight"} or not record.request_ids:
            return record
        request_id = record.request_ids[-1]
        request = self.store.read_request(request_id)
        for attempt in reversed(request.attempts):
            response = self.store.read_term_response(request_id, attempt.attempt_id)
            if response is None:
                continue
            if attempt.state in {"sent", "unknown"}:
                usage = response.usage
                self._finish(
                    request_id,
                    attempt.attempt_id,
                    state="succeeded",
                    usage=(
                        Usage(
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            known_cost=usage.known_cost,
                        )
                        if usage is not None
                        else None
                    ),
                    finished_at=datetime.now(UTC).isoformat(),
                    metadata=response.metadata,
                )
            elif attempt.state != "succeeded":
                raise ValueError("journaled term response has an invalid attempt state")
            try:
                if _response_truncated(response.finish_reason):
                    raise ProtocolError("term response was truncated")
                return self._accept_response(item, record, request_id, response.raw, set(request.item_ids))
            except ProtocolError as error:
                return self._save(
                    record,
                    status="retry_wait",
                    diagnostics=(*record.diagnostics, {"reason": str(error), "request_id": request_id}),
                )
        return record

    def _input_limit(self) -> int:
        limits = [MAX_MODEL_INPUT_TOKENS]
        input_cap = self.config.get("max_input_tokens", self.preparation.translation_config.get("max_input_tokens"))
        if input_cap is not None:
            limits.append(_positive_int(input_cap, MAX_MODEL_INPUT_TOKENS))
        tpm = _optional_positive_int(self.config.get("tpm"))
        if tpm is not None:
            limits.append(tpm)
        return min(limits)

    def _require_preflight(self) -> None:
        from engine.services.preflight import (
            load_preflight,
            preflight_fingerprint,
            preflight_verified,
            require_preflight,
        )
        from engine.services.ready import limits_for

        config = self.preparation.translation_config
        limits = limits_for(self.preparation)
        try:
            fingerprint = preflight_fingerprint(self.store)
            if getattr(self, "_preflight_fingerprint", None) == fingerprint or preflight_verified(self.store):
                self._preflight_fingerprint = fingerprint
                return
            model = str(config.get("model", self.config["model"]))
            if self.preparation.extraction_config.get("strategy") == ATOMIC_TERM_PLANNER_VERSION:
                load_preflight(self.store, limits=limits, model=model)
            else:
                require_preflight(self.store, limits=limits, model=model)
            self._preflight_fingerprint = preflight_fingerprint(self.store)
        except (OSError, ValueError, StoreError) as error:
            raise TermBudgetPaused(f"atomic preflight is required before paid dispatch: {error}") from error

    def _request_fits(self, kind, payload: dict[str, Any]) -> bool:
        budget = model_input_budget(kind, payload, algorithm_version=2)
        return budget["estimated_input_tokens"] <= self._input_limit()

    def _guard_dispatch(self, kind, payload: dict[str, Any]) -> None:
        self._require_preflight()
        if not self._request_fits(kind, payload):
            raise TermBudgetPaused("complete terminology request exceeds frozen input/TPM limits")

    @staticmethod
    def _feedback(record: TermExtractionRecord) -> tuple[str, ...]:
        if record.status != "retry_wait":
            return ()
        return tuple(str(entry.get("reason", "")) for entry in record.diagnostics[-3:])

    def _close_exhausted(
        self,
        items: tuple[ExtractionItem, ...],
        records: dict[str, TermExtractionRecord],
        active_item_ids: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        for item in items:
            if item.item_id in active_item_ids:
                continue
            record = records[item.item_id]
            if record.status in {"pending", "in_flight", "retry_wait"} and (
                self._logical_calls(item.item_id) >= 2 or self._spent(item.item_id) >= item.http_limit
            ):
                records[item.item_id] = self._save(record, status="failed_exhausted")

    def _next_batch(
        self,
        items: tuple[ExtractionItem, ...],
        records: dict[str, TermExtractionRecord],
        request_id: str,
        active_item_ids: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[tuple[ExtractionItem, ...], dict[str, Any] | None, int]:
        eligible = tuple(
            item
            for item in items
            if item.item_id not in active_item_ids
            and records[item.item_id].status in {"pending", "in_flight", "retry_wait"}
            and self._logical_calls(item.item_id) < 2
            and self._spent(item.item_id) < item.http_limit
        )
        if not eligible:
            return (), None, 0

        first = eligible[0]
        record = records[first.item_id]
        pool = eligible
        if record.request_ids:
            # Truncated batches are bisected; other item-level failures stay isolated.
            previous = self.store.read_request(record.request_ids[-1])
            if (
                len(previous.item_ids) > 1
                and record.diagnostics
                and record.diagnostics[-1].get("reason") == "term response was truncated"
            ):
                midpoint = (len(previous.item_ids) + 1) // 2
                halves = (previous.item_ids[:midpoint], previous.item_ids[midpoint:])
                group = next(half for half in halves if first.item_id in half)
                by_id = {item.item_id: item for item in eligible}
                pool = tuple(by_id[item_id] for item_id in group if item_id in by_id)
            else:
                pool = (first,)
        batch: list[ExtractionItem] = []
        payload: dict[str, Any] | None = None
        estimated_input = 0
        input_limit = self._input_limit()
        for item in pool[:256]:
            proposed = (*batch, item)
            proposed_payload = self._payload(
                proposed,
                request_id,
                {entry.item_id: self._feedback(records[entry.item_id]) for entry in proposed},
            )
            while proposed_payload["context"] and not self._request_fits("terms", proposed_payload):
                proposed_payload["context"].pop(0)
                self._filter_context_terms(proposed_payload)
            proposed_input = model_input_budget("terms", proposed_payload, algorithm_version=2)[
                "estimated_input_tokens"
            ]
            if proposed_input > input_limit or not self._request_fits("terms", proposed_payload):
                if not batch:
                    records[item.item_id] = self._save(
                        records[item.item_id],
                        status="unplannable",
                        diagnostics=(
                            *records[item.item_id].diagnostics,
                            {
                                "reason": "model_input_budget",
                                "estimated_input_tokens": proposed_input,
                                "input_limit": input_limit,
                            },
                        ),
                    )
                    break
                continue
            batch.append(item)
            payload = proposed_payload
            estimated_input = proposed_input
        return tuple(batch), payload, estimated_input

    def _mark_batch_error(
        self,
        batch: tuple[ExtractionItem, ...],
        records: dict[str, TermExtractionRecord],
        request_id: str,
        error: Exception,
        *,
        unplannable: bool = False,
    ) -> None:
        for item in batch:
            record = records[item.item_id]
            records[item.item_id] = self._save(
                record,
                status="unplannable" if unplannable else "retry_wait",
                diagnostics=(*record.diagnostics, {"reason": str(error), "request_id": request_id}),
                counters={
                    "http_attempts": self._spent(item.item_id, actual=True),
                    "reserved_attempts": self._spent(item.item_id),
                },
            )

    async def _process_batch(
        self,
        batch: tuple[ExtractionItem, ...],
        payload: dict[str, Any],
        request_id: str,
        estimated_input: int,
        records: dict[str, TermExtractionRecord],
        dispatch_stopped: asyncio.Event,
        pause_reasons: list[str],
    ) -> str | None:
        if dispatch_stopped.is_set():
            return pause_reasons[0] if pause_reasons else None
        try:
            self._guard_dispatch("terms", payload)
        except TermBudgetPaused as error:
            dispatch_stopped.set()
            if not pause_reasons:
                pause_reasons.append(str(error))
            return pause_reasons[0]
        item_ids = tuple(item.item_id for item in batch)
        self._cache_request(
            self.store.write_request(
                RequestManifest(
                    request_id=request_id,
                    stage="terms",
                    owner_kind="extraction_item",
                    owner_id=item_ids[0],
                    item_ids=item_ids,
                    input_hashes={item.item_id: item.extraction_input_hash for item in batch},
                    wire_hash=wire_hash("terms", payload, None),
                    output_unlimited=True,
                )
            )
        )
        for item in batch:
            record = records[item.item_id]
            records[item.item_id] = self._save(
                record, status="in_flight", request_ids=(*record.request_ids, request_id)
            )
        try:
            response = await self.runtime.invoke(
                "terms",
                payload,
                {
                    "request_id": request_id,
                    "item_ids": item_ids,
                    "estimated_tokens": estimated_input + self.output_tokens,
                    "estimated_output_tokens": self.output_tokens,
                    "output_tokens": None,
                },
            )
            if _response_truncated(response.get("finish_reason")):
                raise ProtocolError("term response was truncated")
        except (TermBudgetPaused, RuntimePaused) as error:
            dispatch_stopped.set()
            if not pause_reasons:
                pause_reasons.append(str(error))
            for item in batch:
                records[item.item_id] = self._save(records[item.item_id], status="pending")
            return pause_reasons[0]
        except (ProtocolError, RequestError) as error:
            self._mark_batch_error(
                batch,
                records,
                request_id,
                error,
                unplannable=isinstance(error, RequestError) and error.attempts == 0,
            )
            return None

        expected = set(item_ids)
        for item in batch:
            try:
                records[item.item_id] = self._accept_response(
                    item, records[item.item_id], request_id, response["raw"], expected
                )
            except ProtocolError as error:
                self._mark_batch_error((item,), records, request_id, error)
        return None

    async def run(self) -> TermRunResult:
        if not self.plan.auto_extract:
            return TermRunResult("disabled", 0, 0, 0, self._spent(actual=True))
        if not self.plan.items:
            return TermRunResult("not_required", 0, 0, 0, self._spent(actual=True))
        with self.store.lock():
            for item in self.plan.items:
                self._record(item)
        items = self.plan.items
        records = self._records
        records.clear()
        for item in items:
            records[item.item_id] = self._replay_response(item, self._record(item))
        if self.prior_input_limit_breach is not None:
            final_records = [records[item.item_id] for item in items]
            succeeded = sum(record.status in {"succeeded", "succeeded_with_rejections"} for record in final_records)
            failed = sum(record.status in {"failed_exhausted", "unplannable"} for record in final_records)
            return TermRunResult(
                "paused",
                succeeded,
                failed,
                len(final_records) - succeeded - failed,
                self._spent(actual=True),
                "provider reported input over limit; future dispatch is stopped: "
                f"{self.prior_input_limit_breach} > {MAX_MODEL_INPUT_TOKENS}",
            )
        paused_reason: str | None = None
        pause_reasons: list[str] = []
        active_item_ids: set[str] = set()
        dispatch_stopped = asyncio.Event()
        active_tasks: dict[asyncio.Task[str | None], tuple[ExtractionItem, ...]] = {}
        try:
            while True:
                while not dispatch_stopped.is_set() and len(active_tasks) < self.max_concurrency:
                    self._close_exhausted(items, records, active_item_ids)
                    request_id = f"tr-{uuid4().hex}"
                    batch, payload, estimated_input = self._next_batch(items, records, request_id, active_item_ids)
                    if not batch:
                        break
                    assert payload is not None
                    item_ids = tuple(item.item_id for item in batch)
                    active_item_ids.update(item_ids)
                    task = asyncio.create_task(
                        self._process_batch(
                            batch,
                            payload,
                            request_id,
                            estimated_input,
                            records,
                            dispatch_stopped,
                            pause_reasons,
                        )
                    )
                    active_tasks[task] = batch

                if not active_tasks:
                    if paused_reason:
                        break
                    self._close_exhausted(items, records, active_item_ids)
                    if any(record.status in {"pending", "in_flight", "retry_wait"} for record in records.values()):
                        continue
                    break

                done, _ = await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    batch = active_tasks.pop(task)
                    item_ids = tuple(item.item_id for item in batch)
                    try:
                        paused_reason = task.result() or paused_reason
                    finally:
                        active_item_ids.difference_update(item_ids)
        except BaseException as original:
            for task in active_tasks:
                if not task.done():
                    task.cancel()
            peer_results = await asyncio.gather(*active_tasks, return_exceptions=True)
            peer_errors = [
                result
                for result in peer_results
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
            ]
            if peer_errors:
                raise BaseExceptionGroup("concurrent terminology batches failed", [original, *peer_errors])
            raise
        if not paused_reason:
            self._close_exhausted(items, records, active_item_ids)
        final_records = [records[item.item_id] for item in items]
        succeeded = sum(record.status in {"succeeded", "succeeded_with_rejections"} for record in final_records)
        failed = sum(record.status in {"failed_exhausted", "unplannable"} for record in final_records)
        pending = len(final_records) - succeeded - failed
        status: Literal["closed", "closed_with_gaps", "paused", "disabled", "not_required"] = (
            "paused" if paused_reason or pending else "closed_with_gaps" if failed else "closed"
        )
        return TermRunResult(status, succeeded, failed, pending, self._spent(actual=True), paused_reason)

    def progress_snapshot(self) -> TermRunResult:
        if not self.plan.auto_extract:
            return TermRunResult("disabled", 0, 0, 0, self._spent(actual=True))
        if not self.plan.items:
            return TermRunResult("not_required", 0, 0, 0, self._spent(actual=True))
        records = tuple(self._records.values())
        succeeded = sum(record.status in {"succeeded", "succeeded_with_rejections"} for record in records)
        failed = sum(record.status in {"failed_exhausted", "unplannable"} for record in records)
        pending = len(self.plan.items) - succeeded - failed
        status: Literal["closed", "closed_with_gaps", "paused"] = (
            "paused" if pending else "closed_with_gaps" if failed else "closed"
        )
        return TermRunResult(status, succeeded, failed, pending, self._spent(actual=True))


def _nonnegative_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 0:
        raise ValueError("HTTP limits must be non-negative integers")
    return value


def _positive_int(value: Any, default: int) -> int:
    result = _nonnegative_int(value, default)
    if result < 1:
        raise ValueError("model limits must be positive integers")
    return result


def _optional_positive_int(value: Any) -> int | None:
    return None if value is None else _positive_int(value, 1)


def _interval(interval) -> tuple[str, int, int]:
    view_id, start, end = interval["view_id"], interval["start"], interval["end"]
    if not isinstance(view_id, str) or type(start) is not int or type(end) is not int:
        raise ValueError("planned range requires a view ID and integer offsets")
    return view_id, start, end
