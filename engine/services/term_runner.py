"""Bounded, resumable extraction of terminology from frozen source views."""

from __future__ import annotations

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
from engine.agents.term_protocol import validate_terms_response
from engine.schemas.contracts import (
    Attempt,
    ExtractionItem,
    RequestManifest,
    TermExtractionRecord,
    Usage,
)
from engine.services.coherence import load_budget_overrides
from engine.services.store import RunStore

_TERM_OUTPUT_TOKENS_PER_ITEM = 1_300


class TermBudgetPaused(RuntimeError):
    pass


@dataclass(frozen=True)
class TermRunResult:
    status: Literal["closed", "closed_with_gaps", "paused", "disabled", "not_required"]
    succeeded: int
    failed: int
    pending: int
    http_attempts: int


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
        config = self.preparation.extraction_config
        if config.get("prompt_version") != TERM_PROMPT_VERSION or config.get("target_language") != "zh-Hans":
            raise ValueError("frozen terminology prompt or target language does not match this runtime")
        if not isinstance(config.get("model"), str) or not config["model"]:
            raise ValueError("frozen terminology model identity is missing")
        if model is not None and getattr(model, "id", config["model"]) != config["model"]:
            raise ValueError("provider model differs from frozen terminology identity")
        self.config = config
        self.output_tokens = _positive_int(config.get("max_output_tokens"), 4096)
        configured_limit = _nonnegative_int(config.get("run_http_limit"), 0)
        self.run_limit = (configured_limit or self.plan.extraction_http_limit) + int(
            load_budget_overrides(store)["add_run_http"]
        )
        self.prior_input_limit_breach = self._prior_input_limit_breach()
        self.runtime = ModelRuntime(
            model=model,
            transport=transport,
            rpm=_optional_positive_int(config.get("rpm")),
            tpm=_optional_positive_int(config.get("tpm")),
            max_inflight=_positive_int(config.get("concurrency"), 2),
            model_max_output_tokens=self.output_tokens,
            reserve_attempt=self._reserve,
            finish_attempt=self._finish,
            persist_response=self.store.save_model_response,
            prior_input_limit_breach=self.prior_input_limit_breach,
        )

    def _requests(self) -> tuple[RequestManifest, ...]:
        return tuple(
            self.store.read_request(path.stem) for path in sorted((self.store.root / "requests").glob("*.json"))
        )

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
        return sum(
            (sum(attempt.state != "reserved" for attempt in request.attempts) if actual else len(request.attempts))
            for request in self._requests()
            if item_id is None or item_id in request.item_ids
        )

    def _logical_calls(self, item_id: str) -> int:
        return sum(
            request.stage == "terms"
            and item_id in request.item_ids
            and any(attempt.state != "reserved" for attempt in request.attempts)
            for request in self._requests()
        )

    def _reserve(self, request_id: str, attempt: Any) -> None:
        with self.store.lock():
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
            self.store.reserve_attempt(request_id, Attempt.model_validate(attempt.model_dump(mode="python")))

    def _finish(self, request_id: str, attempt_id: str, **fields: Any) -> None:
        usage = fields.get("usage")
        if usage is not None:
            fields["usage"] = Usage.model_validate(usage.model_dump(mode="python"))
        self.store.finish_attempt(request_id, attempt_id, **fields)

    def _record(self, item: ExtractionItem) -> TermExtractionRecord:
        if self.store._path("glossary/extraction", item.item_id).exists():
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
        return self.store.save_extraction(
            record.model_copy(update={"record_version": record.record_version + 1, **updates}),
            expected_record_version=record.record_version,
        )

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
        all_views = {
            view_id: view for source in self.documents.values() for view_id, view in source.source_views.items()
        }
        context: dict[str, list[str]] = {}
        for interval in item.context_ranges:
            view_id, start, end = interval["view_id"], interval["start"], interval["end"]
            if not isinstance(view_id, str) or type(start) is not int or type(end) is not int:
                raise ValueError("planned context range has invalid identity")
            view = all_views[view_id]
            context.setdefault(view_id, []).append(view.text[start:end])
        terms = {term.term_id: term for term in self.preparation.user_terms}
        user_terms = [
            terms[term_id].model_dump(mode="json") | {"role": role}
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
        return {
            "protocol": "epubox-terms-1",
            "request_id": request_id,
            "target_language": self.config["target_language"],
            "items": [self._payload_item(item, feedback.get(item.item_id, ())) for item in items],
        }

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
        from engine.services.term_candidates import CandidateProposal, EvidenceProposal, validate_candidate_proposals

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
        if record.status != "in_flight" or not record.request_ids:
            return record
        request_id = record.request_ids[-1]
        request = self.store.read_request(request_id)
        for attempt in reversed(request.attempts):
            response = self.store.read_term_response(request_id, attempt.attempt_id)
            if response is None:
                continue
            if attempt.state in {"sent", "unknown"}:
                usage = response.usage
                self.store.finish_attempt(
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
                if response.finish_reason == "length":
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
        tpm = _optional_positive_int(self.config.get("tpm"))
        if tpm is not None:
            limits.append(max(0, tpm - self.output_tokens))
        return min(limits)

    @staticmethod
    def _feedback(record: TermExtractionRecord) -> tuple[str, ...]:
        if record.status != "retry_wait":
            return ()
        return tuple(str(entry.get("reason", "")) for entry in record.diagnostics[-3:])

    def _close_exhausted(
        self,
        items: tuple[ExtractionItem, ...],
        records: dict[str, TermExtractionRecord],
    ) -> None:
        for item in items:
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
    ) -> tuple[tuple[ExtractionItem, ...], dict[str, Any] | None, int]:
        eligible = tuple(
            item
            for item in items
            if records[item.item_id].status in {"pending", "in_flight", "retry_wait"}
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
        # ponytail: historical p90 output is ~1,300 tokens/item; replace with adaptive sizing if models drift.
        item_cap = min(256, max(1, self.output_tokens // _TERM_OUTPUT_TOKENS_PER_ITEM))
        for item in pool[:item_cap]:
            proposed = (*batch, item)
            proposed_payload = self._payload(
                proposed,
                request_id,
                {entry.item_id: self._feedback(records[entry.item_id]) for entry in proposed},
            )
            proposed_input = model_input_budget("terms", proposed_payload)["estimated_input_tokens"]
            if proposed_input > input_limit:
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

    async def run(self) -> TermRunResult:
        if not self.plan.auto_extract:
            return TermRunResult("disabled", 0, 0, 0, self._spent(actual=True))
        if not self.plan.items:
            return TermRunResult("not_required", 0, 0, 0, self._spent(actual=True))
        with self.store.lock():
            for item in self.plan.items:
                self._record(item)
        items = self.plan.items
        records = {item.item_id: self._replay_response(item, self._record(item)) for item in items}
        if self.prior_input_limit_breach is not None:
            final_records = [records[item.item_id] for item in items]
            succeeded = sum(record.status in {"succeeded", "succeeded_with_rejections"} for record in final_records)
            failed = sum(record.status in {"failed_exhausted", "unplannable"} for record in final_records)
            return TermRunResult(
                "paused", succeeded, failed, len(final_records) - succeeded - failed, self._spent(actual=True)
            )
        paused = False
        while not paused:
            self._close_exhausted(items, records)
            request_id = f"tr-{uuid4().hex}"
            batch, payload, estimated_input = self._next_batch(items, records, request_id)
            if not batch:
                if any(record.status in {"pending", "in_flight", "retry_wait"} for record in records.values()):
                    continue
                break
            assert payload is not None
            item_ids = tuple(item.item_id for item in batch)
            self.store.write_request(
                RequestManifest(
                    request_id=request_id,
                    stage="terms",
                    owner_kind="extraction_item",
                    owner_id=item_ids[0],
                    item_ids=item_ids,
                    input_hashes={item.item_id: item.extraction_input_hash for item in batch},
                    wire_hash=wire_hash("terms", payload, self.output_tokens),
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
                        "estimated_tokens": estimated_input,
                        "output_tokens": self.output_tokens,
                    },
                )
                if response.get("finish_reason") == "length":
                    raise ProtocolError("term response was truncated")
                expected = set(item_ids)
                for item in batch:
                    record = records[item.item_id]
                    try:
                        records[item.item_id] = self._accept_response(
                            item, record, request_id, response["raw"], expected
                        )
                    except ProtocolError as error:
                        self._mark_batch_error((item,), records, request_id, error)
            except (TermBudgetPaused, RuntimePaused):
                paused = True
                for item in batch:
                    records[item.item_id] = self._save(records[item.item_id], status="pending")
            except (ProtocolError, RequestError) as error:
                self._mark_batch_error(
                    batch,
                    records,
                    request_id,
                    error,
                    unplannable=isinstance(error, RequestError) and error.attempts == 0,
                )
        if not paused:
            self._close_exhausted(items, records)
        final_records = [records[item.item_id] for item in items]
        succeeded = sum(record.status in {"succeeded", "succeeded_with_rejections"} for record in final_records)
        failed = sum(record.status in {"failed_exhausted", "unplannable"} for record in final_records)
        pending = len(final_records) - succeeded - failed
        status: Literal["closed", "closed_with_gaps", "paused", "disabled", "not_required"] = (
            "paused" if paused or pending else "closed_with_gaps" if failed else "closed"
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
