"""Bounded, resumable extraction of terminology from frozen source views."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from engine.agents.protocol_v23 import ProtocolError
from engine.agents.protocol_v25 import validate_terms_response
from engine.agents.runtime_v23 import PROMPT_VERSION_V25, ModelRuntime, RequestError, RuntimePaused, wire_hash
from engine.core.tokens import count_tokens
from engine.schemas.v25 import (
    Attempt,
    ExtractionItem,
    RequestManifest,
    TermExtractionRecord,
    Usage,
)
from engine.services.store_v25 import StoreV25


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

    def __init__(self, store: StoreV25, *, model: Any = None, transport: Any = None):
        self.store = store
        self.preparation = store.read_preparation()
        self.plan = store.read_term_plan()
        store.write_term_plan(self.plan)
        self.documents = {
            document_id: store.read_document(document_id, expected_hash=digest)
            for document_id, digest in self.preparation.document_hashes.items()
        }
        config = self.preparation.extraction_config
        if config.get("prompt_version") != PROMPT_VERSION_V25 or config.get("target_language") != "zh-Hans":
            raise ValueError("frozen terminology prompt or target language does not match this runtime")
        if not isinstance(config.get("model"), str) or not config["model"]:
            raise ValueError("frozen terminology model identity is missing")
        if model is not None and getattr(model, "id", config["model"]) != config["model"]:
            raise ValueError("provider model differs from frozen terminology identity")
        self.config = config
        self.output_tokens = _positive_int(config.get("max_output_tokens"), 4096)
        self.run_limit = _nonnegative_int(config.get("run_http_limit"), 0)
        self.runtime = ModelRuntime(
            model=model,
            transport=transport,
            rpm=_optional_positive_int(config.get("rpm")),
            tpm=_optional_positive_int(config.get("tpm")),
            max_inflight=_positive_int(config.get("concurrency"), 2),
            model_max_output_tokens=self.output_tokens,
            reserve_attempt=self._reserve,
            finish_attempt=self._finish,
        )

    def _requests(self) -> tuple[RequestManifest, ...]:
        return tuple(
            self.store.read_request(path.stem) for path in sorted((self.store.root / "requests").glob("*.json"))
        )

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
            item_id = manifest.owner_id
            if self._spent() >= self.plan.extraction_http_limit or (
                self.run_limit and self._spent() >= self.run_limit
            ):
                raise TermBudgetPaused("run terminology HTTP budget exhausted")
            if manifest.stage == "terms":
                item = next(entry for entry in self.plan.items if entry.item_id == item_id)
                item_limit = item.http_limit
            elif manifest.stage == "resolution":
                item_limit = 3
            else:
                raise ValueError("terminology runner cannot reserve a translation request")
            if self._spent(item_id) >= item_limit:
                raise RequestError(f"term preparation item HTTP budget exhausted: {item_id}", attempts=0)
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

    def _payload(self, item: ExtractionItem, request_id: str) -> dict[str, Any]:
        document = self.documents[item.document_id]
        views = []
        for interval in item.primary_ranges:
            view_id = interval["view_id"]
            view = document.source_views[str(view_id)]
            start, end = interval["start"], interval["end"]
            if type(start) is not int or type(end) is not int:
                raise ValueError("planned primary range must use integer offsets")
            views.append({"view_id": view_id, "unit_id": view.unit_id, "text": view.text[start:end]})
        all_views = {
            view_id: view for source in self.documents.values() for view_id, view in source.source_views.items()
        }
        context = []
        for interval in item.context_ranges:
            view_id, start, end = interval["view_id"], interval["start"], interval["end"]
            if not isinstance(view_id, str) or type(start) is not int or type(end) is not int:
                raise ValueError("planned context range has invalid identity")
            view = all_views[view_id]
            context.append(
                {"view_id": view_id, "unit_id": view.unit_id, "text": view.text[start:end], "start": start, "end": end}
            )
        terms = {term.term_id: term for term in self.preparation.user_terms}
        user_terms = [
            terms[term_id].model_dump(mode="json") | {"role": role}
            for role, ids in (("target", item.user_term_ids), ("context", item.context_user_term_ids))
            for term_id in ids
        ]
        return {
            "protocol": "epubox-terms-1",
            "request_id": request_id,
            "target_language": self.config["target_language"],
            "items": [
                {
                    "item_id": item.item_id,
                    "document_id": item.document_id,
                    "views": views,
                    "context": context,
                    "hints": [],
                    "user_terms": user_terms,
                }
            ],
        }

    async def run(self) -> TermRunResult:
        if not self.plan.auto_extract:
            return TermRunResult("disabled", 0, 0, 0, self._spent(actual=True))
        if not self.plan.items:
            return TermRunResult("not_required", 0, 0, 0, self._spent(actual=True))
        with self.store.lock():
            for item in self.plan.items:
                self._record(item)
        paused = False
        for item in self.plan.items:
            record = self._record(item)
            if record.status in {"succeeded", "succeeded_with_rejections", "failed_exhausted", "unplannable"}:
                continue
            while self._logical_calls(item.item_id) < 2 and self._spent(item.item_id) < item.http_limit:
                request_id = f"tr-{uuid4().hex}"
                payload = self._payload(item, request_id)
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                estimated_tokens = count_tokens(encoded) + self.output_tokens
                tpm = _optional_positive_int(self.config.get("tpm"))
                if tpm is not None and estimated_tokens > tpm:
                    record = self._save(
                        record, status="unplannable", diagnostics=(*record.diagnostics, {"reason": "tpm_budget"})
                    )
                    break
                self.store.write_request(
                    RequestManifest(
                        request_id=request_id,
                        stage="terms",
                        owner_kind="extraction_item",
                        owner_id=item.item_id,
                        item_ids=(item.item_id,),
                        input_hashes={item.item_id: item.extraction_input_hash},
                        wire_hash=wire_hash("terms", payload, self.output_tokens),
                    )
                )
                record = self._save(record, status="in_flight", request_ids=(*record.request_ids, request_id))
                try:
                    response = await self.runtime.invoke(
                        "terms",
                        payload,
                        {
                            "request_id": request_id,
                            "item_ids": (item.item_id,),
                            "estimated_tokens": estimated_tokens,
                            "output_tokens": self.output_tokens,
                        },
                    )
                    if response.get("finish_reason") == "length":
                        raise ProtocolError("term response was truncated")
                    parsed = validate_terms_response(response["raw"], request_id, {item.item_id})
                    if parsed.errors or parsed.missing or parsed.unknown:
                        raise ProtocolError(
                            f"invalid term item response: {parsed.errors or parsed.missing or parsed.unknown}"
                        )
                    from engine.services.term_candidates import (
                        CandidateProposal,
                        EvidenceProposal,
                        validate_candidate_proposals,
                    )

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
                        for value in parsed.accepted[item.item_id]
                    )
                    checked = validate_candidate_proposals(self.documents[item.document_id], item, proposals)
                    current_rejections = (
                        *checked.diagnostics,
                        *({"reason": message} for message in parsed.rejected_candidates.get(item.item_id, ())),
                    )
                    diagnostics = (*record.diagnostics, *current_rejections)
                    record = self._save(
                        record,
                        status="succeeded_with_rejections" if current_rejections else "succeeded",
                        candidates=checked.candidates,
                        diagnostics=diagnostics,
                        counters={
                            "http_attempts": self._spent(item.item_id, actual=True),
                            "reserved_attempts": self._spent(item.item_id),
                        },
                    )
                    break
                except (TermBudgetPaused, RuntimePaused):
                    paused = True
                    record = self._save(record, status="pending")
                    break
                except (ProtocolError, RequestError) as error:
                    if isinstance(error, RequestError) and error.attempts == 0:
                        record = self._save(
                            record,
                            status="unplannable",
                            diagnostics=(*record.diagnostics, {"reason": str(error), "request_id": request_id}),
                        )
                        break
                    record = self._save(
                        record,
                        status="retry_wait",
                        diagnostics=(*record.diagnostics, {"reason": str(error), "request_id": request_id}),
                        counters={
                            "http_attempts": self._spent(item.item_id, actual=True),
                            "reserved_attempts": self._spent(item.item_id),
                        },
                    )
            if paused:
                break
            if record.status in {"pending", "in_flight", "retry_wait"}:
                self._save(record, status="failed_exhausted")
        records = [self._record(item) for item in self.plan.items]
        succeeded = sum(record.status in {"succeeded", "succeeded_with_rejections"} for record in records)
        failed = sum(record.status in {"failed_exhausted", "unplannable"} for record in records)
        pending = len(records) - succeeded - failed
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
