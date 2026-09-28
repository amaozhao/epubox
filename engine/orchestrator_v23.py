"""JSON-backed, failure-isolating coordinator for the opt-in v2.3 engine."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.agents.protocol_v23 import (
    ProtocolError,
    validate_coherence_response,
    validate_review_response,
    validate_translation_response,
)
from engine.agents.runtime_v23 import PROMPT_VERSION, ModelRuntime, RequestError, RuntimePaused, Stage
from engine.item.inline import ProjectionError, plain_text, validate_projection
from engine.schemas.v23 import (
    Attempt,
    DocumentPlan,
    DocumentStatus,
    FailureRecord,
    ItemRecord,
    ItemStatus,
    RequestManifest,
    RunConfig,
    RunResult,
    Unit,
    UnitRecord,
    Usage,
    canonical_hash,
    canonical_json_bytes,
    compute_input_hash,
    is_accepted,
    strict_json_loads,
)
from engine.services.store import CorruptRecord, IdentityMismatch, StaleWrite, Store, StoreError


@dataclass(frozen=True)
class Job:
    stage: Stage
    unit_id: str
    item_id: str
    revision: int
    plan_epoch: int
    document_id: str

    @property
    def key(self) -> tuple[str, str, str, int, int]:
        return self.stage, self.unit_id, self.item_id, self.revision, self.plan_epoch


@dataclass(frozen=True)
class BatchJob:
    jobs: tuple[Job, ...]

    @property
    def stage(self) -> Stage:
        return self.jobs[0].stage

    @property
    def unit_ids(self) -> set[str]:
        return {job.unit_id for job in self.jobs if job.unit_id}


class UnitBudgetExhausted(RuntimeError):
    pass


class TranslationEngine:
    """One coordinator owns records; workers return scoped results, never whole records."""

    def __init__(self, store: Store, *, model: Any = None, transport: Any = None, progress: Any = None):
        self.store = store
        self.book = store.read_bookplan(ready=True)
        self.config = RunConfig.model_validate(self.book.frozen_config)
        if transport is None and self.config.prompt_version != PROMPT_VERSION:
            raise ValueError("frozen prompt version differs from this engine; start a new run")
        self.run_http_limit = self.config.run_http_limit
        self.documents: dict[str, DocumentPlan] = {}
        self.units: dict[str, Unit] = {}
        self.records: dict[str, UnitRecord] = {}
        self.bad_records: dict[str, str] = {}
        self.bad_documents: dict[str, str] = {}
        self.checks: dict[str, DocumentStatus] = {}
        self.ready: deque[Job] = deque()
        self.queued: set[tuple[str, str, str, int, int]] = set()
        self.manifests: dict[str, RequestManifest] = {}
        self.attempt_keys: set[tuple[str, str]] = set()
        self.http_attempts = 0
        self.batch_limits: dict[Stage, int] = {"translate": 4, "review": 4, "coherence": 1}
        self.stop_status: str | None = None
        self.stop_reason: str | None = None
        self.progress = progress
        self.runtime = ModelRuntime(
            model=model,
            transport=transport,
            rpm=self.config.rpm,
            tpm=self.config.tpm,
            max_inflight=self.config.max_concurrency,
            request_timeout_seconds=self.config.request_timeout_seconds or 120,
            model_max_output_tokens=self.config.max_output_tokens or 2048,
            reserve_attempt=self._reserve,
            finish_attempt=self._finish,
        )

    def request_stop(self) -> None:
        self.stop_status = "paused"
        self.stop_reason = "user_cancelled"

    def _load(self) -> None:
        source = self.store.root / "source.epub"
        if hashlib.sha256(source.read_bytes()).hexdigest() != self.book.source_hash:
            raise IdentityMismatch("source snapshot hash differs from the ready plan")
        limits_path = self.store.root / "checks" / "run-limits.json"
        if limits_path.exists():
            limits = strict_json_loads(limits_path.read_bytes())
            if not isinstance(limits, dict) or limits.get("format") != "epubox-run-limits-1":
                raise CorruptRecord("invalid run limit override")
            configured = limits.get("run_http_limit")
            if not isinstance(configured, int) or configured < self.run_http_limit:
                raise CorruptRecord("run limit cannot decrease")
            self.run_http_limit = configured
        self.documents, self.bad_documents = self.store.scan_documents(self.book.document_hashes)
        self.records, self.bad_records = self.store.scan_units(self.book.unit_ids)
        for document_id, document in self.documents.items():
            if document.source_hash != self.book.source_hash:
                raise IdentityMismatch(f"document belongs to another source: {document_id}")
            for unit in document.units:
                if unit.unit_id not in self.book.unit_ids:
                    raise IdentityMismatch(f"unlisted source unit: {unit.unit_id}")
                self.units[unit.unit_id] = unit
        for unit_id, record in list(self.records.items()):
            unit = self.units.get(unit_id)
            if unit is None:
                self.bad_records[unit_id] = "source document missing or damaged"
                self.records.pop(unit_id)
                continue
            expected_input = (
                compute_input_hash(unit.logical_hash, record.cut_plan.plan_hash) if record.cut_plan else None
            )
            if (
                record.source_hash != self.book.source_hash
                or record.document_id != unit.document_id
                or record.logical_hash != unit.logical_hash
                or record.input_hash != expected_input
            ):
                self.bad_records[unit_id] = "unit identity/input hash mismatch"
                self.records.pop(unit_id)
                continue
            try:
                self._validate_saved_target(unit, record)
            except (ProjectionError, ValueError) as error:
                self.bad_records[unit_id] = str(error)
                self.records.pop(unit_id)
        self._reconcile_attempts()
        for document_id in self.documents:
            try:
                self.checks[document_id] = self.store.read_document_status(document_id)
            except (FileNotFoundError, CorruptRecord):
                # A check is derived, but its spent budget is reconstructed from the attempt journal.
                self.checks[document_id] = DocumentStatus(document_id=document_id)
        self._reconcile_check_attempts()
        for unit_id, record in list(self.records.items()):
            recovered = dict(record.items)
            changed = False
            for item_id, item in recovered.items():
                if item.status in {ItemStatus.IN_FLIGHT, ItemStatus.RETRY_WAIT}:
                    recovered[item_id] = item.model_copy(
                        update={
                            "status": ItemStatus.LOCAL_VALID
                            if item.target_projection is not None
                            else ItemStatus.PENDING,
                            "next_action": "review" if item.target_projection is not None else "translate",
                        }
                    )
                    changed = True
            if changed:
                self._save(record.model_copy(update={"items": recovered}))

    def _validate_saved_target(self, unit: Unit, record: UnitRecord) -> None:
        if record.cut_plan is None:
            return
        from engine.item.planner import merge_segments

        for item in record.items.values():
            if item.target_projection is not None and canonical_hash(item.target_projection) != item.target_hash:
                raise ValueError("saved item target hash mismatch")
            if item.target_projection is not None:
                self._validate_candidate(
                    unit, self._segment(record, item.item_id).source_projection, item.target_projection
                )
        if record.candidate is not None:
            targets = {k: v.target_projection for k, v in record.items.items() if v.target_projection is not None}
            if len(targets) != len(record.cut_plan.segments):
                raise ValueError("complete candidate has missing segments")
            merged = merge_segments(unit, record.cut_plan, targets)
            if merged != record.candidate or canonical_hash(merged) != record.target_hash:
                raise ValueError("saved candidate differs from its segments")
            validate_projection(unit, merged)
        if is_accepted(record):
            if not record.review or record.review.get("input_hash") != record.input_hash:
                raise ValueError("accepted record lacks matching review input")
            if (
                record.review.get("target_hash") != record.target_hash
                or record.review.get("revision") != record.revision
            ):
                raise ValueError("accepted record has stale review")
            if record.local_checks.get("target_hash") != record.target_hash:
                raise ValueError("accepted record has stale local checks")

    def _save(self, record: UnitRecord) -> UnitRecord:
        saved = self.store.save_unit(record)
        self.records[saved.unit_id] = saved
        return saved

    def _reconcile_attempts(self) -> None:
        by_unit: dict[str, int] = {}
        logical_by_unit: dict[tuple[str, str], int] = {}
        logical: dict[tuple[str, str, str], int] = {}
        self.attempt_keys.clear()
        self.manifests.clear()
        self.http_attempts = 0
        for path in sorted((self.store.root / "requests").glob("*.json")):
            manifest = self.store.read_request(path.stem)
            self.manifests[manifest.request_id] = manifest
            if manifest.attempts and manifest.stage != "coherence":
                for unit_id, item_id in zip(manifest.unit_ids, manifest.item_ids, strict=True):
                    key = unit_id, item_id, manifest.stage
                    logical[key] = logical.get(key, 0) + 1
                    unit_key = unit_id, manifest.stage
                    logical_by_unit[unit_key] = logical_by_unit.get(unit_key, 0) + 1
            for attempt in manifest.attempts:
                key = manifest.request_id, attempt.attempt_id
                if key in self.attempt_keys:
                    continue
                self.attempt_keys.add(key)
                self.http_attempts += 1
                if manifest.stage != "coherence":
                    for unit_id in set(manifest.unit_ids):
                        by_unit[unit_id] = by_unit.get(unit_id, 0) + 1
        for unit_id, record in list(self.records.items()):
            items = dict(record.items)
            for item_id, item in items.items():
                counts = dict(item.attempts)
                for stage in ("translate", "review"):
                    counts[stage] = max(counts.get(stage, 0), logical.get((unit_id, item_id, stage), 0))
                items[item_id] = item.model_copy(update={"attempts": counts})
            counters = record.counters.model_copy(
                update={
                    "http_attempts": max(record.counters.http_attempts, by_unit.get(unit_id, 0)),
                    "translation_attempts": max(
                        record.counters.translation_attempts,
                        logical_by_unit.get((unit_id, "translate"), 0),
                    ),
                    "review_attempts": max(
                        record.counters.review_attempts,
                        logical_by_unit.get((unit_id, "review"), 0),
                    ),
                }
            )
            if items != record.items or counters != record.counters:
                self._save(record.model_copy(update={"items": items, "counters": counters}))

    def _reconcile_check_attempts(self) -> None:
        counts: dict[str, int] = {}
        for manifest in self.manifests.values():
            if manifest.stage == "coherence" and manifest.document_id:
                counts[manifest.document_id] = counts.get(manifest.document_id, 0) + len(manifest.attempts)
        for document_id, check in self.checks.items():
            rounds = max(
                (
                    record.counters.coherence_revision_rounds
                    for record in self.records.values()
                    if record.document_id == document_id
                ),
                default=0,
            )
            if counts.get(document_id, 0) > check.http_attempts or rounds > check.repair_rounds:
                updated = check.model_copy(
                    update={
                        "http_attempts": max(counts.get(document_id, 0), check.http_attempts),
                        "repair_rounds": max(rounds, check.repair_rounds),
                    }
                )
                self.checks[document_id] = self.store.write_document_status(updated)

    async def _reserve(self, request_id: str, attempt: Attempt) -> None:
        if self.stop_status:
            raise RuntimePaused(self.stop_reason or self.stop_status)
        manifest = self.manifests[request_id]
        if (request_id, attempt.attempt_id) in self.attempt_keys:
            raise IdentityMismatch("attempt_id already consumed")
        limit = self.run_http_limit
        if self.http_attempts >= limit:
            self.stop_status, self.stop_reason = "paused", "run_http_limit"
            raise RuntimePaused("run HTTP budget exhausted")
        if manifest.stage == "coherence":
            check = self.checks[manifest.document_id or ""]
            if check.http_attempts >= check.http_limit:
                raise UnitBudgetExhausted("document coherence HTTP budget exhausted")
        else:
            for unit_id in manifest.unit_ids:
                record = self.records[unit_id]
                if record.counters.http_attempts >= record.counters.unit_http_limit:
                    raise UnitBudgetExhausted(f"unit HTTP budget exhausted: {unit_id}")
                if (
                    record.revision != manifest.revisions[unit_id]
                    or record.plan_epoch != manifest.plan_epochs[unit_id]
                ):
                    raise StaleWrite("request became stale before dispatch")
        # The attempt journal is committed first. A crash before unit updates remains charged as a reservation.
        first_http_attempt = not manifest.attempts
        self.store.reserve_attempt(request_id, attempt)
        self.manifests[request_id] = self.store.read_request(request_id)
        self.attempt_keys.add((request_id, attempt.attempt_id))
        self.http_attempts += 1
        if manifest.stage == "coherence":
            check = self.checks[manifest.document_id or ""]
            self.checks[check.document_id] = self.store.write_document_status(
                check.model_copy(update={"http_attempts": check.http_attempts + 1})
            )
        else:
            for unit_id in manifest.unit_ids:
                record = self.records[unit_id]
                items = dict(record.items)
                for item_id in manifest.item_ids:
                    if item_id in items:
                        counts = dict(items[item_id].attempts)
                        if first_http_attempt:
                            counts[manifest.stage] = counts.get(manifest.stage, 0) + 1
                        items[item_id] = items[item_id].model_copy(
                            update={
                                "status": ItemStatus.IN_FLIGHT,
                                "request_id": request_id,
                                "attempt_id": attempt.attempt_id,
                                "attempts": counts,
                                "stage": manifest.stage,
                            }
                        )
                counter_updates = {"http_attempts": record.counters.http_attempts + 1}
                if first_http_attempt:
                    name = "translation_attempts" if manifest.stage == "translate" else "review_attempts"
                    counter_updates[name] = getattr(record.counters, name) + 1
                self._save(
                    record.model_copy(
                        update={
                            "items": items,
                            "counters": record.counters.model_copy(update=counter_updates),
                        }
                    )
                )

    async def _finish(self, request_id: str, attempt_id: str, **kwargs: Any) -> None:
        self.store.finish_attempt(request_id, attempt_id, **kwargs)
        self.manifests[request_id] = self.store.read_request(request_id)
        usage: Usage | None = kwargs.get("usage")
        if usage is not None:
            manifest = self.manifests[request_id]
            # Request usage is authoritative; unit token attribution is only exact for a single target unit.
            if len(manifest.unit_ids) == 1 and manifest.stage != "coherence":
                record = self.records[manifest.unit_ids[0]]
                self._save(
                    record.model_copy(
                        update={
                            "counters": record.counters.model_copy(
                                update={
                                    "input_tokens": record.counters.input_tokens + usage.input_tokens,
                                    "output_tokens": record.counters.output_tokens + usage.output_tokens,
                                }
                            )
                        }
                    )
                )

    def _enqueue(self, job: Job) -> None:
        if job.key not in self.queued:
            self.ready.append(job)
            self.queued.add(job.key)

    def _queue_unit(self, unit_id: str) -> None:
        record = self.records.get(unit_id)
        if record is None or record.cut_plan is None or is_accepted(record) or record.derived:
            return
        missing = [item for item in record.items.values() if item.target_projection is None]
        if missing:
            for item in missing:
                if item.status in {ItemStatus.PENDING, ItemStatus.RETRY_WAIT}:
                    self._enqueue(
                        Job("translate", unit_id, item.item_id, record.revision, record.plan_epoch, record.document_id)
                    )
            return
        if record.candidate is None:
            self._complete_candidate(unit_id)
            record = self.records[unit_id]
        # Review one segment of a unit at a time: a revision cannot invalidate a concurrently reviewed sibling.
        if any(key[0] == "review" and key[1] == unit_id for key in self.queued):
            return
        for item in record.items.values():
            if item.status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE, ItemStatus.RETRY_WAIT}:
                self._enqueue(
                    Job("review", unit_id, item.item_id, record.revision, record.plan_epoch, record.document_id)
                )
                return
        self._accept_if_reviewed(unit_id)

    def _complete_candidate(self, unit_id: str) -> None:
        from engine.item.planner import merge_segments

        record = self.records[unit_id]
        unit = self.units[unit_id]
        if record.cut_plan is None:
            return
        targets = {k: item.target_projection for k, item in record.items.items() if item.target_projection is not None}
        for segment in record.cut_plan.segments:
            target = targets.get(segment.item_id)
            if target is not None:
                self._validate_candidate(unit, segment.source_projection, target)
        candidate = merge_segments(unit, record.cut_plan, targets)
        validate_projection(unit, candidate)
        target_hash = canonical_hash(candidate)
        self._save(
            record.model_copy(
                update={
                    "candidate": candidate,
                    "target_hash": target_hash,
                    "local_checks": {
                        "passed": True,
                        "input_hash": record.input_hash,
                        "revision": record.revision,
                        "target_hash": target_hash,
                    },
                }
            )
        )

    @staticmethod
    def _validate_candidate(unit: Unit, source: str, target: str) -> None:
        """The same local gate applies to every translation, revision, repair and reload."""
        from html import escape

        from engine.agents.verifier import (
            EnglishResidualDecision,
            classify_untranslated_english_texts,
            find_degenerate_translation,
        )

        validate_projection(source, target, unit.registry)
        original_text, target_text = plain_text(source), plain_text(target)
        if not target_text.strip():
            raise ProjectionError("translation has no language text")
        degeneration = find_degenerate_translation(escape(original_text), escape(target_text))
        if degeneration:
            raise ProjectionError(degeneration)
        keep_entire = any(
            term.get("mode") == "keep_source" and term.get("source") == original_text for term in unit.terms
        )
        if not keep_entire and any(
            finding.decision == EnglishResidualDecision.FAIL
            for finding in classify_untranslated_english_texts(escape(target_text))
        ):
            raise ProjectionError("untranslated English prose remains in the candidate")

    def _accept_if_reviewed(self, unit_id: str) -> None:
        record = self.records[unit_id]
        if (
            not record.candidate
            or not record.items
            or any(item.status != ItemStatus.REVIEWED for item in record.items.values())
        ):
            return
        remaining_issues = tuple(issue for issue in record.unresolved_issues if issue.code != "blocking_coherence")
        if remaining_issues != record.unresolved_issues:
            record = self._save(record.model_copy(update={"unresolved_issues": remaining_issues}))
        if record.unresolved_issues:
            return
        self._save(
            record.model_copy(
                update={
                    "accepted_revision": record.revision,
                    "accepted_target_hash": record.target_hash,
                    "review": {
                        "passed": True,
                        "input_hash": record.input_hash,
                        "revision": record.revision,
                        "target_hash": record.target_hash,
                        "items": {k: item.checks for k, item in record.items.items()},
                    },
                }
            )
        )

    def _segment(self, record: UnitRecord, item_id: str) -> Any:
        if record.cut_plan is None:
            raise IdentityMismatch("unit has no executable cut plan")
        return next(segment for segment in record.cut_plan.segments if segment.item_id == item_id)

    @staticmethod
    def _logical_attempts(item: ItemRecord, stage: Stage) -> int:
        cycle_start = item.attempts.get(f"{stage}_cycle_start", 0)
        repairs = item.attempts.get(f"{stage}_protocol_repair", 0)
        repair_start = item.attempts.get(f"{stage}_protocol_repair_cycle_start", 0)
        return item.attempts.get(stage, 0) - cycle_start - (repairs - repair_start)

    @staticmethod
    def _has_protocol_repair(item: ItemRecord, stage: Stage) -> bool:
        return item.attempts.get(f"{stage}_protocol_repair", 0) > 0

    def _payload_item(self, unit: Unit, record: UnitRecord, item_id: str, stage: str) -> dict[str, Any]:
        segment = self._segment(record, item_id)
        item = record.items[item_id]
        from engine.item.inline import projection_identities

        mentioned = set(projection_identities(segment.source_projection))
        refs = {key: ref for key, ref in unit.registry.items() if key in mentioned}
        payload: dict[str, Any] = {
            "item_id": item_id,
            "source": segment.source_projection,
            "context": unit.context,
            "terms": [dict(term) for term in unit.terms],
            "hints": {
                key: (
                    {
                        "excerpt": ref.source_text[:400],
                        "excerpt_truncated": str(len(ref.source_text) > 400).lower(),
                        **ref.hints,
                    }
                    if ref.kind == "g"
                    else dict(ref.hints)
                )
                for key, ref in refs.items()
            },
            "constraints": {
                key: {"parent_ref": ref.parent_ref, "movement": ref.movement, "fixed_order": list(ref.fixed_order)}
                for key, ref in refs.items()
            },
        }
        if stage == "review":
            payload.update(
                {
                    "target": item.target_projection,
                    "base_revision": record.revision,
                    "applicability": {"terminology": bool(unit.terms), "bindings": bool(refs)},
                    "bindings": self._bindings(unit, segment.source_projection, item.target_projection or ""),
                }
            )
        if item.failure:
            payload["validation_error"] = item.failure.message[:1000]
        continuity = [issue.message for issue in record.unresolved_issues if issue.code == "blocking_coherence"]
        if continuity:
            payload["required_revision"] = continuity
        return payload

    @staticmethod
    def _bindings(unit: Unit, source: str, target: str) -> list[dict[str, Any]]:
        from engine.item.inline import parse_projection

        def ranges(projection: str) -> dict[str, str]:
            text: dict[str, list[str]] = {}
            stack: list[str] = []
            for event in parse_projection(projection):
                if event.kind == "text":
                    for ref in stack:
                        text[ref].append(event.value)
                elif event.value.startswith("+"):
                    stack.append(event.value[1:])
                    text[event.value[1:]] = []
                elif event.value.startswith("-"):
                    stack.pop()
            return {ref: "".join(value) for ref, value in text.items() if ref.startswith("g")}

        original, translated = ranges(source), ranges(target)
        bindings = [
            {"ref": ref, "source": value, "target": translated.get(ref, "")} for ref, value in original.items()
        ]
        events = parse_projection(target)
        for index, event in enumerate(events):
            if event.kind != "marker" or not event.value.startswith("="):
                continue
            entry = unit.registry[event.value[1:]]
            if entry.boundary_type in {"footnote", "noteref"} or entry.hints.get("role") == "footnote":
                bindings.append(
                    {
                        "ref": event.value[1:],
                        "source": entry.source_text[:400],
                        "target_context": plain_text(events[max(0, index - 2) : index + 3]),
                    }
                )
        return bindings

    def _make_request(self, job: Job) -> tuple[dict[str, Any], RequestManifest]:
        return self._make_batch_request(BatchJob((job,)))

    def _make_batch_request(self, batch: BatchJob) -> tuple[dict[str, Any], RequestManifest]:
        jobs = batch.jobs
        if not jobs or any(job.stage != batch.stage for job in jobs):
            raise ValueError("a request batch must contain one stage")
        request_id = "r" + uuid.uuid4().hex
        if batch.stage == "coherence":
            if len(jobs) != 1:
                raise ValueError("coherence windows are dispatched singly")
            job = jobs[0]
            check = self.checks[job.document_id]
            window = next(value for value in check.windows if value["item_id"] == job.item_id)
            raw_ids = window["unit_ids"]
            if not isinstance(raw_ids, list) or not all(isinstance(value, str) for value in raw_ids):
                raise CorruptRecord("invalid coherence window unit IDs")
            ids = tuple(str(value) for value in raw_ids)
            payload = {"protocol": "epubox-coherence-1", "request_id": request_id, "items": [dict(window)]}
            revisions = {key: self.records[key].revision for key in ids}
            epochs = {key: self.records[key].plan_epoch for key in ids}
            inputs = {key: self.records[key].input_hash or "" for key in ids}
            targets = {key: self.records[key].target_hash or "" for key in ids}
        else:
            if len({job.unit_id for job in jobs}) != len(jobs):
                raise ValueError("a batch cannot contain two items from one unit")
            payload_items = []
            revisions: dict[str, int] = {}
            epochs: dict[str, int] = {}
            inputs: dict[str, str] = {}
            targets: dict[str, str] = {}
            for job in jobs:
                record = self.records[job.unit_id]
                unit = self.units[job.unit_id]
                item = record.items[job.item_id]
                allowed = 3 if job.stage == "translate" else 2
                if self._logical_attempts(item, job.stage) >= allowed:
                    raise UnitBudgetExhausted(f"{job.stage} logical attempt limit exhausted: {job.unit_id}")
                payload_items.append(self._payload_item(unit, record, job.item_id, job.stage))
                revisions[job.unit_id] = record.revision
                epochs[job.unit_id] = record.plan_epoch
                inputs[job.unit_id] = record.input_hash or ""
                targets[job.item_id] = item.target_hash or ""
            payload = {
                "protocol": "epubox-text-1" if batch.stage == "translate" else "epubox-review-1",
                "request_id": request_id,
                "items": payload_items,
            }
            if batch.stage == "translate":
                payload["target_language"] = "zh-Hans"
            ids = tuple(job.unit_id for job in jobs)
        manifest = RequestManifest(
            request_id=request_id,
            stage=batch.stage,
            document_id=jobs[0].document_id if len({job.document_id for job in jobs}) == 1 else None,
            unit_ids=ids,
            item_ids=tuple(job.item_id for job in jobs),
            revisions=revisions,
            plan_epochs=epochs,
            input_hashes=inputs,
            target_hashes=targets,
            wire_hash=self._wire_hash(batch.stage, payload),
        )
        self.store.write_request(manifest)
        self.manifests[request_id] = manifest
        return payload, manifest

    def _output_cap(self, stage: Stage, payload: dict[str, Any]) -> int:
        from engine.item.planner import PlannerConfig, recommended_output_tokens

        maximum = self.config.max_output_tokens or 2048
        if stage == "coherence":
            return maximum
        cap = recommended_output_tokens(
            payload["items"],
            PlannerConfig(
                context_tokens=self.config.max_context_tokens or 8192,
                max_output_tokens=maximum,
                review_output_tokens=maximum,
            ),
            stage="translation" if stage == "translate" else "review",
        )
        if cap > maximum:
            raise RequestError("required target exceeds configured output cap", status_code=413)
        return cap

    def _wire_hash(self, stage: Stage, payload: dict[str, Any]) -> str:
        from engine.agents.runtime_v23 import wire_hash

        return wire_hash(stage, payload, self._output_cap(stage, payload))

    async def _invoke(self, job: Job) -> tuple[RequestManifest, Any]:
        return await self._invoke_batch(BatchJob((job,)))

    def _request_budget(self, stage: Stage, payload: dict[str, Any]) -> tuple[int, int]:
        from engine.agents.runtime_v23 import request_messages
        from engine.item.chunker import count_tokens
        from engine.schemas.v23 import canonical_json_bytes

        inputs = count_tokens(canonical_json_bytes(request_messages(stage, payload)).decode())
        outputs = self._output_cap(stage, payload)
        if self.config.max_input_tokens is not None and inputs > self.config.max_input_tokens:
            raise RequestError("final request exceeds configured input capacity", status_code=413)
        estimated = inputs + outputs
        if self.config.max_context_tokens is not None and estimated + 256 > self.config.max_context_tokens:
            raise RequestError("final request exceeds configured context capacity", status_code=413)
        return estimated, outputs

    async def _invoke_batch(self, batch: BatchJob) -> tuple[RequestManifest, Any]:
        payload, manifest = self._make_batch_request(batch)
        estimated, outputs = self._request_budget(batch.stage, payload)
        result = await self.runtime.invoke(
            batch.stage,
            payload,
            {
                "request_id": manifest.request_id,
                "item_ids": manifest.item_ids,
                "estimated_tokens": estimated,
                "output_tokens": outputs,
            },
        )
        if result.get("finish_reason") in {"length", "max_tokens"}:
            raise RequestError("output token budget exhausted", status_code=413)
        return manifest, result["raw"]

    def _apply_translation(self, job: Job, manifest: RequestManifest, raw: str | bytes) -> None:
        parsed = validate_translation_response(raw, manifest.request_id, {job.item_id})
        if job.item_id not in parsed.accepted:
            raise ProtocolError(parsed.errors.get(job.item_id, "missing translation item"))
        self._apply_translation_item(job, manifest, parsed.accepted[job.item_id])

    def _apply_translation_item(self, job: Job, manifest: RequestManifest, decision: dict[str, Any]) -> None:
        record = self.records[job.unit_id]
        target = decision["target"]
        segment = self._segment(record, job.item_id)
        unit = self.units[job.unit_id]
        self._validate_candidate(unit, segment.source_projection, target)
        item = record.items[job.item_id].model_copy(
            update={
                "target_projection": target,
                "target_hash": canonical_hash(target),
                "status": ItemStatus.LOCAL_VALID,
                "failure": None,
                "next_action": "review",
            }
        )
        self.records[job.unit_id] = self.store.merge_item(
            job.unit_id,
            item,
            plan_epoch=job.plan_epoch,
            revision=job.revision,
            input_hash=manifest.input_hashes[job.unit_id],
        )
        record = self.records[job.unit_id]
        self._save(
            record.model_copy(
                update={
                    "unresolved_issues": tuple(
                        issue for issue in record.unresolved_issues if issue.item_id != job.item_id
                    ),
                }
            )
        )

    def _apply_review(self, job: Job, manifest: RequestManifest, raw: str | bytes) -> None:
        unit = self.units[job.unit_id]
        record = self.records[job.unit_id]
        from engine.item.inline import projection_identities

        relevant_refs = projection_identities(self._segment(record, job.item_id).source_projection)
        parsed = validate_review_response(
            raw,
            manifest.request_id,
            {
                job.item_id: {
                    "base_revision": manifest.revisions[job.unit_id],
                    "terminology_applicable": bool(unit.terms),
                    "bindings_applicable": bool(relevant_refs),
                },
            },
        )
        if record.items[job.item_id].target_hash != manifest.target_hashes[job.item_id]:
            raise StaleWrite("reviewed target changed")
        if job.item_id not in parsed.accepted:
            raise ProtocolError(parsed.errors.get(job.item_id, "missing review item"))
        self._apply_review_item(job, manifest, parsed.accepted[job.item_id])

    def _apply_review_item(self, job: Job, manifest: RequestManifest, decision: dict[str, Any]) -> None:
        unit = self.units[job.unit_id]
        record = self.records[job.unit_id]
        if record.items[job.item_id].target_hash != manifest.target_hashes[job.item_id]:
            raise StaleWrite("reviewed target changed")
        if decision["decision"] == "needs_attention":
            self._fail(job, "blocking_review", str(decision["issues"]), retry=False)
            return
        if decision["decision"] == "replace":
            item = record.items[job.item_id]
            if self._logical_attempts(item, "review") >= 2:
                self._fail(
                    job, "blocking_revision_limit", "revision review requested another replacement", retry=False
                )
                return
            target = decision["target"]
            self._validate_candidate(unit, self._segment(record, job.item_id).source_projection, target)
            history = (
                *record.history,
                {
                    "revision": record.revision,
                    "candidate": record.candidate,
                    "accepted_revision": record.accepted_revision,
                    "review": record.review,
                },
            )
            items = dict(record.items)
            items[job.item_id] = item.model_copy(
                update={
                    "target_projection": target,
                    "target_hash": canonical_hash(target),
                    "checks": {},
                    "status": ItemStatus.LOCAL_VALID,
                    "failure": None,
                }
            )
            for item_id, sibling in items.items():
                if item_id != job.item_id:
                    items[item_id] = sibling.model_copy(update={"inherited_from_revision": record.revision})
            self._save(
                record.model_copy(
                    update={
                        "revision": record.revision + 1,
                        "items": items,
                        "candidate": None,
                        "target_hash": None,
                        "local_checks": {},
                        "review": None,
                        "history": history,
                        "unresolved_issues": tuple(
                            issue
                            for issue in record.unresolved_issues
                            if issue.item_id != job.item_id and issue.code != "blocking_coherence"
                        ),
                    }
                )
            )
            self._complete_candidate(job.unit_id)
            return
        item = record.items[job.item_id].model_copy(
            update={
                "status": ItemStatus.REVIEWED,
                "checks": {
                    "checks": decision["checks"],
                    "issues": decision["issues"],
                    "target_hash": record.items[job.item_id].target_hash,
                    "input_hash": record.input_hash,
                },
                "failure": None,
                "next_action": None,
            }
        )
        items = {**record.items, job.item_id: item}
        self._save(
            record.model_copy(
                update={
                    "items": items,
                    "unresolved_issues": tuple(
                        issue for issue in record.unresolved_issues if issue.item_id != job.item_id
                    ),
                }
            )
        )

    def _apply_batch(self, batch: BatchJob, manifest: RequestManifest, raw: str | bytes) -> None:
        if batch.stage == "translate":
            parsed = validate_translation_response(raw, manifest.request_id, set(manifest.item_ids))
        elif batch.stage == "review":
            from engine.item.inline import projection_identities

            expected = {}
            for job in batch.jobs:
                unit = self.units[job.unit_id]
                record = self.records[job.unit_id]
                expected[job.item_id] = {
                    "base_revision": manifest.revisions[job.unit_id],
                    "terminology_applicable": bool(unit.terms),
                    "bindings_applicable": bool(
                        projection_identities(self._segment(record, job.item_id).source_projection)
                    ),
                }
            parsed = validate_review_response(raw, manifest.request_id, expected)
        else:
            self._apply_coherence(batch.jobs[0], manifest, raw)
            return

        for job in batch.jobs:
            decision = parsed.accepted.get(job.item_id)
            if decision is None:
                message = parsed.errors.get(job.item_id, "missing response item")
                self._fail(job, "invalid_response", message, retry=job.stage == "translate")
                continue
            try:
                if job.stage == "translate":
                    self._apply_translation_item(job, manifest, decision)
                else:
                    self._apply_review_item(job, manifest, decision)
            except (ProtocolError, ProjectionError, StaleWrite) as error:
                self._fail(job, "invalid_response", str(error), retry=job.stage == "translate")

    def _protocol_failure(self, job: Job, manifest: RequestManifest, message: str) -> None:
        if job.stage == "coherence":
            check = self.checks[job.document_id]
            repairs = 0
            for event in reversed(check.retry_history):
                if event.get("action") == "explicit_retry":
                    break
                if (
                    event.get("action") == "automatic_protocol_repair"
                    and event.get("window_id") == job.item_id
                    and event.get("summary_hash") == check.summary_hash
                ):
                    repairs += 1
            if repairs >= 1:
                self._fail(job, "invalid_response", message)
                return
            issue = FailureRecord(
                scope="request",
                stage="coherence",
                code="coherence_protocol_retry",
                message=message[:2000],
                request_id=manifest.request_id,
                item_id=job.item_id,
                plan_epoch=0,
                revision=0,
                retry_action="automatic",
            )
            updated = check.model_copy(
                update={
                    "status": (
                        "needs_attention"
                        if any(existing.retry_action != "automatic" for existing in check.issues)
                        else "blocked_dependency"
                        if check.dependency_ids
                        else "pending"
                    ),
                    "issues": (
                        *tuple(
                            existing
                            for existing in check.issues
                            if not (existing.code == "coherence_protocol_retry" and existing.item_id == job.item_id)
                        ),
                        issue,
                    ),
                    "retry_history": (
                        *check.retry_history,
                        {
                            "action": "automatic_protocol_repair",
                            "window_id": job.item_id,
                            "summary_hash": check.summary_hash,
                            "request_id": manifest.request_id,
                            "reason": message[:1000],
                            "http_attempts": check.http_attempts,
                        },
                    ),
                }
            )
            self.checks[job.document_id] = self.store.write_document_status(updated)
            return
        record = self.records[job.unit_id]
        if record.revision != job.revision or record.plan_epoch != job.plan_epoch:
            return
        item = record.items[job.item_id]
        repair_key = f"{job.stage}_protocol_repair"
        repair_count = item.attempts.get(repair_key, 0)
        repair_cycle_start = item.attempts.get(f"{repair_key}_cycle_start", 0)
        retry = repair_count - repair_cycle_start < 1
        attempts = dict(item.attempts)
        if retry:
            attempts[repair_key] = repair_count + 1
        failure = FailureRecord(
            scope="request",
            stage=job.stage,
            code="invalid_response_envelope",
            message=message[:2000],
            request_id=manifest.request_id,
            item_id=job.item_id,
            plan_epoch=job.plan_epoch,
            revision=job.revision,
            retry_action="automatic" if retry else "explicit_retry",
        )
        updated = item.model_copy(
            update={
                "status": ItemStatus.RETRY_WAIT if retry else ItemStatus.NEEDS_ATTENTION,
                "failure": failure,
                "next_action": job.stage if retry else "repair",
                "attempts": attempts,
            }
        )
        self._save(
            record.model_copy(
                update={
                    "items": {**record.items, job.item_id: updated},
                    "unresolved_issues": (
                        *tuple(issue for issue in record.unresolved_issues if issue.item_id != job.item_id),
                        failure,
                    ),
                }
            )
        )

    def _fail(self, job: Job, code: str, message: str, *, retry: bool = False) -> None:
        if job.stage == "coherence":
            check = self.checks[job.document_id]
            issue = FailureRecord(
                scope="document",
                stage="coherence",
                code=code,
                message=message[:2000],
                item_id=job.item_id,
                plan_epoch=0,
                revision=0,
                retry_action="explicit_retry",
            )
            self.checks[job.document_id] = self.store.write_document_status(
                check.model_copy(update={"status": "needs_attention", "issues": (*check.issues, issue)})
            )
            return
        record = self.records[job.unit_id]
        if record.revision != job.revision or record.plan_epoch != job.plan_epoch:
            return
        item = record.items[job.item_id]
        allowed = 3 if job.stage == "translate" else 2
        retry = retry and self._logical_attempts(item, job.stage) < allowed
        failure = FailureRecord(
            scope="item",
            stage=job.stage,
            code=code,
            message=message[:2000],
            request_id=item.request_id,
            item_id=job.item_id,
            plan_epoch=job.plan_epoch,
            revision=job.revision,
            retry_action="automatic" if retry else "explicit_retry",
        )
        items = dict(record.items)
        items[job.item_id] = item.model_copy(
            update={
                "status": ItemStatus.RETRY_WAIT if retry else ItemStatus.NEEDS_ATTENTION,
                "failure": failure,
                "next_action": job.stage if retry else "repair",
            }
        )
        self._save(
            record.model_copy(
                update={
                    "items": items,
                    "unresolved_issues": (
                        *tuple(issue for issue in record.unresolved_issues if issue.item_id != job.item_id),
                        failure,
                    ),
                }
            )
        )

    def _narrative_units(self, document: DocumentPlan) -> list[Unit]:
        excluded = {"attribute", "metadata", "opf_title", "opf_description", "head_title", "nav", "navigation"}
        return [unit for unit in document.units if unit.kind not in excluded and not unit.region.get("attribute_name")]

    def _windows(self, document: DocumentPlan) -> list[dict[str, Any]]:
        from engine.item.planner import initial_coherence_windows

        return list(initial_coherence_windows(document, self.records))

    @staticmethod
    def _passed_coherence_checks(
        checks: dict[str, Any], windows: list[dict[str, Any]] | tuple[dict[str, Any], ...]
    ) -> dict[str, Any]:
        expected = {str(window["item_id"]): {str(unit_id) for unit_id in window["unit_ids"]} for window in windows}
        passed: dict[str, Any] = {}
        for item_id, item in checks.items():
            unit_ids = expected.get(item_id)
            if unit_ids is None:
                continue
            try:
                raw = canonical_json_bytes({"protocol": "epubox-coherence-1", "request_id": "cached", "items": [item]})
                parsed = validate_coherence_response(raw, "cached", {item_id: unit_ids})
            except (ProtocolError, TypeError, ValueError):
                continue
            accepted = parsed.accepted.get(item_id)
            if accepted is not None and not any(
                issue["severity"] in {"major", "critical"} for issue in accepted["issues"]
            ):
                passed[item_id] = accepted
        return passed

    @staticmethod
    def _coherence_status(windows: list[dict[str, Any]] | tuple[dict[str, Any], ...], checks: dict[str, Any]) -> str:
        return "valid" if all(str(window["item_id"]) in checks for window in windows) else "pending"

    @staticmethod
    def _window_scope(window: dict[str, Any]) -> str:
        return "unit" if window.get("scope") == "unit" else "chapter"

    def _window_versions(self, window: dict[str, Any]) -> dict[str, int]:
        return {
            str(unit_id): self.records[str(unit_id)].revision
            for unit_id in window["unit_ids"]
            if str(unit_id) in self.records
        }

    def _window_ready(
        self,
        window: dict[str, Any],
        windows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> bool:
        unit_ids = (
            {
                str(unit_id)
                for candidate in windows
                if self._window_scope(candidate) == "chapter"
                for unit_id in candidate["unit_ids"]
            }
            if self._window_scope(window) == "chapter"
            else {str(unit_id) for unit_id in window["unit_ids"]}
        )
        return all(unit_id in self.records and is_accepted(self.records[unit_id]) for unit_id in unit_ids)

    def _current_coherence_state(
        self, windows: list[dict[str, Any]] | tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, int], dict[str, dict[str, int]], tuple[str, ...]]:
        chapter_ids = {
            str(unit_id)
            for window in windows
            if self._window_scope(window) == "chapter"
            for unit_id in window["unit_ids"]
        }
        chapter_versions = {
            unit_id: self.records[unit_id].revision for unit_id in chapter_ids if unit_id in self.records
        }
        independent_versions = {
            str(window["item_id"]): self._window_versions(window)
            for window in windows
            if self._window_scope(window) == "unit"
        }
        missing = {
            unit_id for unit_id in chapter_ids if unit_id not in self.records or not is_accepted(self.records[unit_id])
        }
        for window in windows:
            if self._window_scope(window) == "unit":
                missing.update(
                    str(unit_id)
                    for unit_id in window["unit_ids"]
                    if str(unit_id) not in self.records or not is_accepted(self.records[str(unit_id)])
                )
        return chapter_versions, independent_versions, tuple(sorted(missing))

    @staticmethod
    def _without_protocol_issue(check: DocumentStatus, item_id: str) -> DocumentStatus:
        issues = tuple(
            issue
            for issue in check.issues
            if not (issue.code == "coherence_protocol_retry" and issue.item_id == item_id)
        )
        return check if issues == check.issues else check.model_copy(update={"issues": issues})

    def _queue_coherence(self, document_id: str) -> None:
        document = self.documents[document_id]
        check = self.checks.get(document_id, DocumentStatus(document_id=document_id))
        windows = self._windows(document)
        chapter_versions, independent_versions, missing = self._current_coherence_state(windows)
        passed = self._passed_coherence_checks(check.checks, windows)
        same_chapter = check.candidate_versions == chapter_versions
        old_window_ids = {str(window["item_id"]) for window in check.windows}
        old_chapter_ids = {
            str(window["item_id"]) for window in check.windows if self._window_scope(window) == "chapter"
        }
        current_chapter_ids = {str(window["item_id"]) for window in windows if self._window_scope(window) == "chapter"}
        same_chapter_windows = old_chapter_ids == current_chapter_ids
        checks: dict[str, Any] = {}
        window_versions: dict[str, dict[str, int]] = {}
        valid_bindings: set[str] = set()
        for window in windows:
            item_id = str(window["item_id"])
            if self._window_scope(window) == "chapter":
                binding_valid = same_chapter_windows and same_chapter
            else:
                current = independent_versions[item_id]
                binding_valid = item_id in old_window_ids and check.window_versions.get(item_id) == current
                window_versions[item_id] = current
            if binding_valid:
                valid_bindings.add(item_id)
                if item_id in passed:
                    checks[item_id] = passed[item_id]
        issues = tuple(issue for issue in check.issues if issue.item_id is None or issue.item_id in valid_bindings)
        terminal_windows = {
            issue.item_id for issue in issues if issue.item_id is not None and issue.retry_action != "automatic"
        }
        fallback_base = max(0, check.http_limit - check.extra_http_limit) if check.http_limit else 6 * len(windows)
        limit = self.book.initial_coherence_limits.get(document_id, fallback_base) + check.extra_http_limit
        if len(checks) == len(windows) and not missing:
            status = "valid"
        elif terminal_windows:
            status = "needs_attention"
        elif missing:
            status = "blocked_dependency"
        else:
            status = "pending"
        check = check.model_copy(
            update={
                "candidate_versions": chapter_versions,
                "window_versions": window_versions,
                "dependency_ids": missing,
                "windows": tuple(windows),
                "http_limit": limit,
                "summary_hash": canonical_hash(chapter_versions),
                "checks": checks,
                "issues": issues,
                "status": status,
            }
        )
        self.checks[document_id] = self.store.write_document_status(check)
        for window in windows:
            item_id = str(window["item_id"])
            if item_id not in check.checks and item_id not in terminal_windows and self._window_ready(window, windows):
                self._enqueue(Job("coherence", "", item_id, 0, 0, document_id))

    def _apply_coherence(self, job: Job, manifest: RequestManifest, raw: str | bytes) -> None:
        check = self.checks[job.document_id]
        window = next(
            (window for window in check.windows if str(window["item_id"]) == job.item_id),
            None,
        )
        if window is None or not self._window_ready(window, check.windows):
            return
        chapter_versions, _, _ = self._current_coherence_state(check.windows)
        if self._window_scope(window) == "chapter" and chapter_versions != check.candidate_versions:
            return
        for unit_id, revision in manifest.revisions.items():
            if self.records[unit_id].revision != revision or not is_accepted(self.records[unit_id]):
                return
        parsed = validate_coherence_response(raw, manifest.request_id, {job.item_id: set(manifest.unit_ids)})
        if job.item_id not in parsed.accepted:
            raise ProtocolError(parsed.errors.get(job.item_id, "missing coherence window"))
        check = self._without_protocol_issue(check, job.item_id)
        self.checks[job.document_id] = check
        item = parsed.accepted[job.item_id]
        blocking = [issue for issue in item["issues"] if issue["severity"] in {"major", "critical"}]
        if blocking:
            ids = item.get("unit_ids", list(manifest.unit_ids))
            if check.repair_rounds >= 1:
                self._fail(job, "blocking_coherence", str(blocking), retry=False)
                return
            self.checks[job.document_id] = self.store.write_document_status(
                check.model_copy(
                    update={
                        "repair_rounds": check.repair_rounds + 1,
                        "status": "blocked_dependency",
                        "dependency_ids": tuple(sorted(set(check.dependency_ids) | set(ids))),
                    }
                )
            )
            for unit_id in ids:
                if unit_id not in manifest.unit_ids:
                    raise ProtocolError("coherence referenced a unit outside the window")
                record = self.records[unit_id]
                # Re-review the whole affected Unit with the concrete continuity issue; never patch a window.
                items = {
                    key: value.model_copy(
                        update={
                            "status": ItemStatus.LOCAL_VALID,
                            "attempts": {
                                **value.attempts,
                                "review_cycle_start": value.attempts.get("review", 0),
                                "review_protocol_repair_cycle_start": value.attempts.get("review_protocol_repair", 0),
                            },
                        }
                    )
                    for key, value in record.items.items()
                }
                issue = FailureRecord(
                    scope="unit",
                    stage="coherence",
                    code="blocking_coherence",
                    message=str(blocking),
                    plan_epoch=record.plan_epoch,
                    revision=record.revision,
                    retry_action="repair",
                )
                self._save(
                    record.model_copy(
                        update={
                            "items": items,
                            "accepted_revision": None,
                            "accepted_target_hash": None,
                            "review": None,
                            "unresolved_issues": (*record.unresolved_issues, issue),
                            "counters": record.counters.model_copy(
                                update={
                                    "coherence_revision_rounds": record.counters.coherence_revision_rounds + 1,
                                }
                            ),
                        }
                    )
                )
                self._queue_unit(unit_id)
            return
        checks = {**check.checks, job.item_id: item}
        window_versions = dict(check.window_versions)
        if self._window_scope(window) == "unit":
            window_versions[job.item_id] = dict(manifest.revisions)
        _, _, missing = self._current_coherence_state(check.windows)
        terminal = any(issue.retry_action != "automatic" for issue in check.issues)
        if len(checks) == len(check.windows) and not missing:
            status = "valid"
        elif terminal:
            status = "needs_attention"
        elif missing:
            status = "blocked_dependency"
        else:
            status = "pending"
        self.checks[job.document_id] = self.store.write_document_status(
            check.model_copy(
                update={
                    "checks": checks,
                    "window_versions": window_versions,
                    "dependency_ids": missing,
                    "status": status,
                }
            )
        )

    def _apply_derived(self) -> None:
        from engine.item.inline import events_to_projection, parse_projection

        for document in self.documents.values():
            for binding in document.derived_bindings:
                unit_id = str(binding.get("unit_id", binding.get("target_unit_id", "")))
                source_id = str(binding.get("source_unit_id", ""))
                if not unit_id or not source_id or unit_id not in self.records:
                    continue
                source = self.records.get(source_id)
                record = self.records[unit_id]
                if any(entry.get("action") == "human_repair" for entry in record.history):
                    continue  # An explicit repair is independently reviewed, never silently overwritten by derivation.
                if source is None or not is_accepted(source):
                    if record.derived and record.derived.get("state") == "blocked_dependency":
                        continue
                    self._save(
                        record.model_copy(
                            update={"derived": {"state": "blocked_dependency", "source_unit_id": source_id}}
                        )
                    )
                    continue
                if (
                    record.derived
                    and record.derived.get("source_revision") == source.revision
                    and record.derived.get("state") == "valid"
                ):
                    continue
                source_events = parse_projection(self.units[unit_id].source_projection)
                text_events = [event for event in source_events if event.kind == "text" and event.value.strip()]
                if len(text_events) != 1 or any(
                    event.kind == "marker" and event.value.startswith("=") for event in source_events
                ):
                    raise CorruptRecord("derived navigation is not a single unambiguous text field")
                replacement = plain_text(source.candidate or "")
                target = events_to_projection(
                    [
                        event.model_copy(update={"value": replacement}) if event is text_events[0] else event
                        for event in source_events
                    ]
                )
                validate_projection(self.units[unit_id], target)
                self._save(
                    record.model_copy(
                        update={
                            "derived": {
                                "state": "valid",
                                "source_unit_id": source_id,
                                "source_revision": source.revision,
                                "source_target_hash": source.target_hash,
                                "target": target,
                                "target_hash": canonical_hash(target),
                            }
                        }
                    )
                )

    def _job_current(self, job: Job) -> bool:
        if job.stage == "coherence":
            check = self.checks.get(job.document_id)
            if check is None or job.item_id in check.checks:
                return False
            window = next(
                (window for window in check.windows if str(window["item_id"]) == job.item_id),
                None,
            )
            terminal = any(
                issue.item_id == job.item_id and issue.retry_action != "automatic" for issue in check.issues
            )
            return bool(window and not terminal and self._window_ready(window, check.windows))
        record = self.records.get(job.unit_id)
        return bool(
            record
            and record.plan_epoch == job.plan_epoch
            and record.revision == job.revision
            and job.item_id in record.items
            and not is_accepted(record)
            and not record.derived
        )

    def _report(self, *, outcome: str | None = None, execution_state: str = "running") -> dict[str, Any]:
        accepted = sum(is_accepted(record) for record in self.records.values())
        derived = sum(
            bool(record.derived and record.derived.get("state") == "valid") for record in self.records.values()
        )
        failures = []
        pending = review = waiting = 0
        for record in self.records.values():
            if record.derived and record.derived.get("state") == "blocked_dependency":
                waiting += 1
            if not record.derived:
                for item in record.items.values():
                    if item.target_projection is None:
                        pending += 1
                    elif item.status != ItemStatus.REVIEWED:
                        review += 1
            failures.extend(issue.model_dump(mode="json") for issue in record.unresolved_issues)
        for key, message in {**self.bad_documents, **self.bad_records}.items():
            failures.append({"scope": "record", "id": key, "code": "corrupt_or_missing", "message": message})
        for check in self.checks.values():
            if check.status == "blocked_dependency":
                waiting += 1
            failures.extend(issue.model_dump(mode="json") for issue in check.issues)
        usages = [
            attempt.usage for manifest in self.manifests.values() for attempt in manifest.attempts if attempt.usage
        ]
        return {
            "run_id": self.book.run_id,
            "execution_state": execution_state,
            "outcome": outcome,
            "work_dir": str(self.store.root),
            "required_units": self.book.required_unit_count,
            "accepted_units": accepted,
            "derived_units": derived,
            "completed_units": accepted + derived,
            "pending_items": pending,
            "review_pending_items": review,
            "blocked_dependencies": waiting,
            "local_failures": failures,
            "http_attempts": self.http_attempts,
            "preparation_issues": [
                *self.book.preparation_issues,
                *(issue for document in self.documents.values() for issue in document.preparation_issues),
            ],
            "known_input_tokens": sum(usage.input_tokens for usage in usages),
            "known_output_tokens": sum(usage.output_tokens for usage in usages),
            "unknown_usage_attempts": sum(
                attempt.usage is None for manifest in self.manifests.values() for attempt in manifest.attempts
            ),
            "stop_reason": self.stop_reason,
            "output_path": None,
        }

    def _checkpoint_report(self, **kwargs: Any) -> dict[str, Any]:
        report = self._report(**kwargs)
        self.store.write_report(report)
        if self.progress:
            self.progress(report)
        return report

    def _job_budget_error(self, job: Job) -> str | None:
        if job.stage == "coherence":
            check = self.checks[job.document_id]
            return "document coherence HTTP budget exhausted" if check.http_attempts >= check.http_limit else None
        record = self.records[job.unit_id]
        if record.counters.http_attempts >= record.counters.unit_http_limit:
            return f"unit HTTP budget exhausted: {job.unit_id}"
        item = record.items[job.item_id]
        allowed = 3 if job.stage == "translate" else 2
        spent = self._logical_attempts(item, job.stage)
        return f"{job.stage} logical attempt limit exhausted" if spent >= allowed else None

    def _jobs_fit(self, jobs: tuple[Job, ...]) -> bool:
        if len(jobs) == 1:
            return True
        if self.config.max_context_tokens is None or self.config.max_output_tokens is None:
            return False
        from engine.item.planner import PlannerConfig, PlanningError, batch_request

        stage = jobs[0].stage
        items = [
            self._payload_item(self.units[job.unit_id], self.records[job.unit_id], job.item_id, stage) for job in jobs
        ]
        planner = PlannerConfig(
            context_tokens=self.config.max_context_tokens,
            max_input_tokens=self.config.max_input_tokens,
            max_output_tokens=self.config.max_output_tokens,
            review_output_tokens=self.config.max_output_tokens,
            max_batch_items=self.batch_limits[stage],
        )
        try:
            planned = batch_request(items, planner, stage="translation" if stage == "translate" else "review")
            if not planned or len(planned[0]) != len(items):
                return False
            payload: dict[str, Any] = {
                "protocol": "epubox-text-1" if stage == "translate" else "epubox-review-1",
                "request_id": "r" + "0" * 32,
                "items": items,
            }
            if stage == "translate":
                payload["target_language"] = "zh-Hans"
            self._request_budget(stage, payload)
        except (PlanningError, RequestError):
            return False
        return True

    def _take_batch(self, running_units: set[str]) -> BatchJob | None:
        first: Job | None = None
        for _ in range(len(self.ready)):
            job = self.ready.popleft()
            if job.unit_id and job.unit_id in running_units:
                self.ready.append(job)
                continue
            if not self._job_current(job):
                self.queued.discard(job.key)
                continue
            if error := self._job_budget_error(job):
                self.queued.discard(job.key)
                self._fail(job, "budget_exhausted", error)
                continue
            first = job
            break
        if first is None:
            return None
        first_repair = bool(
            first.unit_id and self._has_protocol_repair(self.records[first.unit_id].items[first.item_id], first.stage)
        )
        if first.stage == "coherence" or self.batch_limits[first.stage] == 1 or first_repair:
            return BatchJob((first,))

        jobs = [first]
        for _ in range(len(self.ready)):
            candidate = self.ready.popleft()
            eligible = (
                candidate.stage == first.stage
                and candidate.unit_id not in running_units
                and candidate.unit_id not in {job.unit_id for job in jobs}
                and self._job_current(candidate)
                and not self._has_protocol_repair(
                    self.records[candidate.unit_id].items[candidate.item_id], candidate.stage
                )
            )
            if eligible and (error := self._job_budget_error(candidate)):
                self.queued.discard(candidate.key)
                self._fail(candidate, "budget_exhausted", error)
                continue
            if eligible and len(jobs) < self.batch_limits[first.stage] and self._jobs_fit((*jobs, candidate)):
                jobs.append(candidate)
            else:
                if not self._job_current(candidate):
                    self.queued.discard(candidate.key)
                else:
                    self.ready.append(candidate)
        return BatchJob(tuple(jobs))

    async def execute(self) -> dict[str, Any]:
        """Process every runnable task. Publishing is a separate, all-units gate."""
        running: dict[asyncio.Task, BatchJob] = {}
        with self.store.lock(blocking=False):
            self._load()
            self._apply_derived()
            for unit_id in self.book.unit_ids:
                self._queue_unit(unit_id)
            for document_id in self.documents:
                self._queue_coherence(document_id)
            self._checkpoint_report()
            try:
                while self.ready or running:
                    while self.ready and len(running) < self.config.max_concurrency and not self.stop_status:
                        running_units = {unit_id for batch in running.values() for unit_id in batch.unit_ids}
                        batch = self._take_batch(running_units)
                        if batch is None:
                            break
                        running[asyncio.create_task(self._invoke_batch(batch))] = batch
                    if not running:
                        break
                    done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        batch = running.pop(task)
                        for job in batch.jobs:
                            self.queued.discard(job.key)
                        try:
                            manifest, raw = task.result()
                            current = tuple(job for job in batch.jobs if self._job_current(job))
                            if current:
                                try:
                                    self._apply_batch(BatchJob(current), manifest, raw)
                                except ProtocolError as error:
                                    for job in current:
                                        self._protocol_failure(job, manifest, str(error))
                        except StaleWrite as error:
                            if any(self._job_current(job) for job in batch.jobs):
                                self.stop_status, self.stop_reason = "failed", str(error)
                        except RuntimePaused as error:
                            self.stop_status, self.stop_reason = "paused", str(error)
                        except UnitBudgetExhausted:
                            for job in batch.jobs:
                                if self._job_current(job):
                                    budget_error = self._job_budget_error(job)
                                    if budget_error:
                                        self._fail(job, "budget_exhausted", budget_error)
                                    else:
                                        self._enqueue(job)
                        except (ProjectionError, RequestError) as error:
                            oversized = isinstance(error, RequestError) and error.status_code == 413
                            if oversized and len(batch.jobs) > 1:
                                self.batch_limits[batch.stage] = max(1, len(batch.jobs) // 2)
                            for job in batch.jobs:
                                if not self._job_current(job):
                                    continue
                                replanned = (
                                    oversized
                                    and len(batch.jobs) == 1
                                    and bool(job.unit_id)
                                    and self.replan_unit(job.unit_id)
                                )
                                if not replanned:
                                    self._fail(
                                        job,
                                        "invalid_response",
                                        str(error),
                                        retry=job.stage == "translate" or (oversized and len(batch.jobs) > 1),
                                    )
                        except (OSError, StoreError, IdentityMismatch) as error:
                            self.stop_status, self.stop_reason = "failed", str(error)
                        except Exception as error:  # noqa: BLE001 -- unclassified faults must stop the whole run
                            # Unclassified errors are global; never silently continue with an untrusted plan.
                            self.stop_status, self.stop_reason = "failed", f"{type(error).__name__}: {error}"
                        if not self.stop_status:
                            for job in batch.jobs:
                                if not job.unit_id:
                                    continue
                                self._queue_unit(job.unit_id)
                            self._apply_derived()
                            for document_id in self.documents:
                                self._queue_coherence(document_id)
                        self._checkpoint_report(execution_state="draining" if self.stop_status else "running")
            except asyncio.CancelledError:
                self.stop_status, self.stop_reason = "paused", "user_cancelled"
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)
            finally:
                # No pending worker may escape the coordinator lifetime or its process lock.
                for task in running:
                    task.cancel()
                if running:
                    await asyncio.gather(*running, return_exceptions=True)
            completed_units = sum(
                is_accepted(record) or bool(record.derived and record.derived.get("state") == "valid")
                for record in self.records.values()
            )
            coherent = len(self.checks) == len(self.documents) and all(
                check.status == "valid" for check in self.checks.values()
            )
            ready_to_publish = (
                completed_units == self.book.required_unit_count
                and coherent
                and not self.bad_documents
                and not self.bad_records
                and not self.stop_status
            )
            outcome = self.stop_status or "needs_attention"
            report = self._checkpoint_report(outcome=outcome, execution_state="stopped")
            report["ready_to_publish"] = ready_to_publish
            self.store.write_report(report)
            return report

    def replan_unit(self, unit_id: str) -> bool:
        """Replace one whole Unit plan once; neither HTTP budgets nor old history reset."""
        from engine.item.planner import PlannerConfig, PlanningError, plan_unit

        record = self.records[unit_id]
        if record.counters.replan_attempts >= 1:
            return False
        capacity = self.config.max_context_tokens
        if capacity is None:
            return False
        output = max(64, (self.config.max_output_tokens or 2048) // 2)
        config = PlannerConfig(
            context_tokens=capacity,
            max_input_tokens=self.config.max_input_tokens,
            max_output_tokens=output,
            review_output_tokens=output,
            target_ratio=2.0,
        )
        try:
            cut_plan = plan_unit(self.units[unit_id], config, epoch=record.plan_epoch + 1)
        except PlanningError:
            return False
        history = (
            *record.history,
            {
                "revision": record.revision,
                "plan_epoch": record.plan_epoch,
                "candidate": record.candidate,
                "items": {k: v.model_dump(mode="json") for k, v in record.items.items()},
            },
        )
        updated = record.model_copy(
            update={
                "cut_plan": cut_plan,
                "plan_epoch": cut_plan.plan_epoch,
                "revision": record.revision + 1,
                "input_hash": compute_input_hash(record.logical_hash, cut_plan.plan_hash),
                "items": {
                    segment.item_id: ItemRecord(item_id=segment.item_id, segment_id=segment.segment_id)
                    for segment in cut_plan.segments
                },
                "candidate": None,
                "target_hash": None,
                "accepted_revision": None,
                "accepted_target_hash": None,
                "local_checks": {},
                "review": None,
                "unresolved_issues": (),
                "history": history,
                "counters": record.counters.model_copy(
                    update={"replan_attempts": record.counters.replan_attempts + 1}
                ),
            }
        )
        self._save(updated)
        return True

    def retry_units(self, unit_ids: list[str], *, add_unit_http: int = 0, add_run_http: int = 0) -> None:
        if min(add_unit_http, add_run_http) < 0:
            raise ValueError("budget increments must be nonnegative")
        with self.store.lock(blocking=False):
            self._load()
            for unit_id in unit_ids:
                if unit_id not in self.records:
                    raise ValueError(f"unit unavailable; repair its source/result record first: {unit_id}")
                record = self.records[unit_id]
                if record.cut_plan is None:
                    raise ValueError(
                        f"unit has no executable source plan; repair preparation or start a new run: {unit_id}"
                    )
                items = dict(record.items)
                for item_id, item in items.items():
                    if item.status == ItemStatus.NEEDS_ATTENTION or (
                        record.unresolved_issues and not is_accepted(record)
                    ):
                        items[item_id] = item.model_copy(
                            update={
                                "status": ItemStatus.LOCAL_VALID
                                if item.target_projection is not None
                                else ItemStatus.PENDING,
                                "attempts": {
                                    **item.attempts,
                                    "translate_cycle_start": item.attempts.get("translate", 0),
                                    "translate_protocol_repair_cycle_start": item.attempts.get(
                                        "translate_protocol_repair", 0
                                    ),
                                    "review_cycle_start": item.attempts.get("review", 0),
                                    "review_protocol_repair_cycle_start": item.attempts.get(
                                        "review_protocol_repair", 0
                                    ),
                                },
                                "failure": None,
                            }
                        )
                self._save(
                    record.model_copy(
                        update={
                            "items": items,
                            "unresolved_issues": (),
                            "history": (
                                *record.history,
                                {
                                    "action": "explicit_retry",
                                    "add_unit_http": add_unit_http,
                                    "revision": record.revision,
                                    "issues": [issue.model_dump(mode="json") for issue in record.unresolved_issues],
                                },
                            ),
                            "counters": record.counters.model_copy(
                                update={
                                    "unit_http_limit": record.counters.unit_http_limit + add_unit_http,
                                }
                            ),
                        }
                    )
                )
            if add_run_http:
                path = self.store.root / "checks" / "run-limits.json"
                previous = strict_json_loads(path.read_bytes()) if path.exists() else {}
                changes = previous.get("changes", []) if isinstance(previous, dict) else []
                if not isinstance(changes, list):
                    raise CorruptRecord("invalid run budget history")
                self.run_http_limit += add_run_http
                override: dict[str, Any] = {
                    "format": "epubox-run-limits-1",
                    "run_http_limit": self.run_http_limit,
                    "changes": [*changes, {"add_http": add_run_http, "unit_ids": unit_ids}],
                }
                self.store._atomic_write(path, override)

    def retry_checks(self, document_ids: list[str], *, add_http: int = 0) -> None:
        if add_http < 0:
            raise ValueError("coherence budget increase cannot be negative")
        with self.store.lock(blocking=False):
            self._load()
            for document_id in document_ids:
                if document_id not in self.documents:
                    raise ValueError(f"document unavailable: {document_id}")
                check = self.checks[document_id]
                windows = self._windows(self.documents[document_id])
                chapter_versions, independent_versions, missing = self._current_coherence_state(windows)
                stored_window_ids = {str(window["item_id"]) for window in check.windows}
                stored_chapter_ids = {
                    str(window["item_id"]) for window in check.windows if self._window_scope(window) == "chapter"
                }
                current_chapter_ids = {
                    str(window["item_id"]) for window in windows if self._window_scope(window) == "chapter"
                }
                passed = self._passed_coherence_checks(check.checks, windows)
                checks: dict[str, Any] = {}
                window_versions: dict[str, dict[str, int]] = {}
                for window in windows:
                    item_id = str(window["item_id"])
                    if self._window_scope(window) == "chapter":
                        preserve = (
                            stored_chapter_ids == current_chapter_ids and check.candidate_versions == chapter_versions
                        )
                    else:
                        current = independent_versions[item_id]
                        preserve = item_id in stored_window_ids and check.window_versions.get(item_id) == current
                        window_versions[item_id] = current
                    if preserve and item_id in passed:
                        checks[item_id] = passed[item_id]
                status = (
                    "valid"
                    if len(checks) == len(windows) and not missing
                    else "blocked_dependency"
                    if missing
                    else "pending"
                )
                updated = check.model_copy(
                    update={
                        "status": status,
                        "checks": checks,
                        "issues": (),
                        "candidate_versions": chapter_versions,
                        "window_versions": window_versions,
                        "dependency_ids": missing,
                        "windows": tuple(windows),
                        "summary_hash": canonical_hash(chapter_versions),
                        "extra_http_limit": check.extra_http_limit + add_http,
                        "http_limit": check.http_limit + add_http,
                        "retry_history": (
                            *check.retry_history,
                            {
                                "action": "explicit_retry",
                                "add_http": add_http,
                                "http_attempts": check.http_attempts,
                                "summary_hash": canonical_hash(chapter_versions),
                                "preserved_windows": sorted(checks),
                            },
                        ),
                    }
                )
                self.checks[document_id] = self.store.write_document_status(updated)

    def import_repairs(self, path: Path) -> None:
        from engine.item.planner import merge_segments

        raw = strict_json_loads(path.read_bytes())
        repairs = raw.get("items") if isinstance(raw, dict) and "items" in raw else [raw]
        if not isinstance(repairs, list):
            raise TypeError("repair file must contain a repair or an items list")
        with self.store.lock(blocking=False):
            self._load()
            for repair in repairs:
                if not isinstance(repair, dict):
                    raise TypeError("repair must be an object")
                unit_id = str(repair.get("unit_id", ""))
                if unit_id not in self.records:
                    raise ValueError(f"repair unit not available: {unit_id}")
                record = self.records[unit_id]
                if repair.get("base_revision") != record.revision or repair.get("plan_epoch") != record.plan_epoch:
                    raise StaleWrite(
                        f"repair is stale: {unit_id}; current revision={record.revision}, epoch={record.plan_epoch}"
                    )
                if record.cut_plan is None:
                    raise ValueError("unit has no valid source projection; repair the source plan first")
                target = repair.get("target")
                if len(record.cut_plan.segments) == 1 and isinstance(target, str):
                    targets = {record.cut_plan.segments[0].item_id: target}
                elif isinstance(target, dict) and all(isinstance(value, str) for value in target.values()):
                    targets = {key: str(value) for key, value in target.items()}
                else:
                    raise ValueError("segmented repair target must contain the complete item_id-to-target mapping")
                candidate = merge_segments(self.units[unit_id], record.cut_plan, targets)
                for segment in record.cut_plan.segments:
                    self._validate_candidate(self.units[unit_id], segment.source_projection, targets[segment.item_id])
                items: dict[str, ItemRecord] = {}
                for segment in record.cut_plan.segments:
                    item = record.items[segment.item_id]
                    projection = targets[segment.item_id]
                    items[segment.item_id] = item.model_copy(
                        update={
                            "target_projection": projection,
                            "target_hash": canonical_hash(projection),
                            "status": ItemStatus.LOCAL_VALID,
                            "checks": {},
                            "failure": None,
                            "attempts": {
                                **item.attempts,
                                "review_cycle_start": item.attempts.get("review", 0),
                                "review_protocol_repair_cycle_start": item.attempts.get("review_protocol_repair", 0),
                            },
                        }
                    )
                self._save(
                    record.model_copy(
                        update={
                            "revision": record.revision + 1,
                            "items": items,
                            "candidate": candidate,
                            "target_hash": canonical_hash(candidate),
                            "review": None,
                            "local_checks": {},
                            "unresolved_issues": (),
                            "derived": None,
                            "history": (
                                *record.history,
                                {"action": "human_repair", "revision": record.revision, "candidate": record.candidate},
                            ),
                        }
                    )
                )
                self._complete_candidate(unit_id)

    async def run(self, output_path: Path, checker: Any, *, overwrite: bool = False) -> RunResult:
        from engine.epub.publication import publish_book, recover_publication

        with self.store.lock(blocking=False):
            try:
                report = await self.execute()
                if not report["ready_to_publish"]:
                    return RunResult(
                        outcome=report["outcome"],
                        run_id=self.book.run_id,
                        work_dir=str(self.store.root),
                        report_path=str(self.store.root / "report.json"),
                        reader_check={"status": "not_run"},
                    )
                vector = {key: value.revision for key, value in self.records.items()}
                recovered = recover_publication(
                    self.store.root / "publish.json", plan_fingerprint=canonical_hash(self.book), version_vector=vector
                )
                if recovered and Path(str(recovered["target_path"])).resolve() == output_path.resolve():
                    result = {
                        "path": recovered["target_path"],
                        "sha256": recovered["target_hash"],
                        "verification": recovered["verification"],
                    }
                else:
                    targets = {
                        key: str(value.derived["target"]) if value.derived else value.candidate or ""
                        for key, value in self.records.items()
                    }
                    result = publish_book(self.store, targets, output_path, checker, overwrite=overwrite)
                report.update(
                    {
                        "outcome": "completed",
                        "output_path": str(result["path"]),
                        "output_sha256": result["sha256"],
                        "publication": result["verification"],
                    }
                )
                self.store.write_report(report)
                return RunResult(
                    outcome="completed",
                    run_id=self.book.run_id,
                    work_dir=str(self.store.root),
                    output_path=str(result["path"]),
                    output_hash=str(result["sha256"]),
                    report_path=str(self.store.root / "report.json"),
                    structural_check={"passed": True},
                    semantic_review={"passed": True},
                    coherence_check={"passed": True},
                    epubcheck={"passed": True},
                    reader_check={"status": "not_run"},
                )
            except (ValueError, OSError, StoreError) as error:
                failure = FailureRecord(
                    scope="run",
                    stage="publication",
                    code=type(error).__name__,
                    message=str(error),
                    plan_epoch=0,
                    revision=0,
                )
                try:
                    report = self._report(outcome="failed", execution_state="stopped")
                    report["local_failures"].append(failure.model_dump(mode="json"))
                    self.store.write_report(report)
                except OSError:
                    pass  # The caller still receives failure when the disk cannot accept its report.
                return RunResult(
                    outcome="failed",
                    run_id=self.book.run_id,
                    work_dir=str(self.store.root),
                    issues=(failure,),
                    reader_check={"status": "not_run"},
                )
