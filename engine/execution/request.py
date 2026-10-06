"""The single v2.5 translation and review executor."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from engine.agents.protocol import ProtocolError, validate_translation_response
from engine.agents.runtime import (
    MAX_MODEL_INPUT_TOKENS,
    MalformedEnvelopeError,
    RequestError,
    model_input_budget,
    request_messages,
    wire_hash,
)
from engine.agents.terms import validate_review_response
from engine.core.tokens import count_tokens
from engine.execution.review import Review
from engine.execution.state import _Job
from engine.execution.utility import (
    _bindings,
    _segment,
    _term_payload,
)
from engine.item.context import build_context
from engine.item.inline import (
    ProjectionError,
)
from engine.item.planner import (
    MAX_SOURCE_TOKENS,
    recommended_output_tokens,
    source_token_count,
)
from engine.schemas.contracts import (
    ItemStatus,
    RequestManifest,
    canonical_hash,
)
from engine.services.atomic import IdentityMismatch


class Request(Review):
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
