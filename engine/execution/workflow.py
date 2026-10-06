"""The single v2.5 translation and review executor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from engine.agents.protocol import ProtocolError
from engine.agents.terms import validate_review_response
from engine.epub.assembly import derive_navigation_projection
from engine.execution.coherence import Coherence
from engine.execution.state import TranslationRunResult, _Job
from engine.execution.utility import (
    _merge_candidate,
    _normalized_review_issues,
    _record_needs_attention,
    _recoverable_failure,
    _segment,
)
from engine.item.planner import (
    batch_request,
)
from engine.schemas.contracts import (
    ItemRecord,
    ItemStatus,
    JsonValue,
    UnitRecord,
    canonical_hash,
)
from engine.services.atomic import IdentityMismatch


class Workflow(Coherence):
    if TYPE_CHECKING:

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

        def _fail_remaining(self, record: UnitRecord, code: str, message: str) -> None: ...

        def _payload_item(self, job: _Job) -> dict[str, Any]: ...

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
