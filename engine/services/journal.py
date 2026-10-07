"""Crash-safe body result, request, and response persistence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast

from engine.agents.protocol import ProtocolError, review_applicability, validate_translation_response
from engine.agents.runtime import MAX_MODEL_INPUT_TOKENS, ModelRuntime, RuntimePaused, wire_hash
from engine.agents.terms import validate_review_response
from engine.agents.workflow import _failed, _frame, _target_error, _validate_saved_record, _wire_items
from engine.epub.assembly import derive_navigation_projection
from engine.item.budget import measure_budget
from engine.item.members import merge_member_targets, pack_members
from engine.schemas.contracts import (
    ItemRecord,
    ItemStatus,
    RequestManifest,
    Usage,
    canonical_hash,
    canonical_json_bytes,
)
from engine.schemas.members import MemberBatch
from engine.services import state
from engine.services.atomic import CorruptRecord, IdentityMismatch, StaleWrite, safe_id
from engine.services.coherence import load_budget_overrides
from engine.services.custody import bounded as _bounded
from engine.services.custody import epoch as _saved_epoch
from engine.services.custody import manifest as _manifest
from engine.services.custody import number as _number
from engine.services.custody import optional as _optional
from engine.services.custody import persisted_response as _persisted_response
from engine.services.custody import positive as _positive
from engine.services.custody import retry_checks as _retry_checks
from engine.services.custody import text as _text
from engine.services.ready import ReadySession, limits_from_config
from engine.services.store import ModelResponseStage, RunStore

_FROZEN_RESULT_FIELDS = tuple(
    "item_id segment_id selected_term_ids term_applicability terms_hash context_hash".split()  # noqa: SIM905
)


class BodyJournal:
    def __init__(self, store: RunStore, session: ReadySession | None = None):
        self.store = store
        self.session = session or ReadySession(store)
        if self.session.store.root != store.root:
            raise IdentityMismatch("body journal and ready session use different runs")
        self._requests = self._load_requests()
        self._records = self._load_records()
        self._responses: dict[tuple[str, str], Any] = {}
        self._translations: dict[str, Mapping[str, Any]] = {}
        self._reviews: dict[str, Mapping[str, Any]] = {}
        frozen_limit = _bounded(self.session.prepared.plan.translation_config, "run_http_limit", 0, 0, 2**31 - 1)
        addition = load_budget_overrides(store)["add_run_http"]
        if type(addition) is not int or addition < 0:
            raise CorruptRecord("body run limit addition must be a non-negative integer")
        self._run_limit = frozen_limit + addition if frozen_limit else 0
        for record in self._records.values():
            _validate_saved_record(self.session, record)
            if record.status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}:
                self._translation_proof(record)
            if record.status == ItemStatus.REVIEWED:
                self._require_review_proof(record)
        self._initialize_counts()

    def records(self, item_ids: Sequence[str] | None = None) -> dict[str, ItemRecord]:
        self.session.verify()
        requested = tuple(item_ids) if item_ids is not None else tuple(self.session.prepared.plan.member_hashes)
        if len(requested) != len(set(requested)) or not set(requested).issubset(self.session.index.members_by_id):
            raise IdentityMismatch("body result lookup names unknown or repeated members")
        for item_id in requested:
            if item_id not in self._records:
                raise CorruptRecord(f"missing body result: {item_id}")
        return {item_id: self._records[item_id] for item_id in requested}

    def save(self, record: ItemRecord) -> None:
        self.save_many((record,))

    def save_many(self, records: Sequence[ItemRecord]) -> None:
        self.session.verify()
        records = tuple(records)
        if len({record.item_id for record in records}) != len(records):
            raise IdentityMismatch("body result group contains duplicate members")
        changes: list[tuple[ItemRecord, ItemRecord, Path]] = []
        with self.store.lock(), state.batch(self.store.root):
            for record in records:
                if record.item_id != record.segment_id or record.item_id not in self.session.index.members_by_id:
                    raise IdentityMismatch("body result does not belong to the ready member inventory")
                _validate_saved_record(self.session, record)
                path = self._result_path(record.item_id)
                current = self._records[record.item_id]
                if current == record:
                    continue
                immutable = (
                    ("item_id", "segment_id") if current.status == ItemStatus.PENDING else _FROZEN_RESULT_FIELDS
                )
                if any(getattr(current, name) != getattr(record, name) for name in immutable):
                    raise IdentityMismatch("body result changed its frozen source, terms, or context identity")
                if current.status == ItemStatus.REVIEWED or current.status == ItemStatus.NEEDS_ATTENTION:
                    raise StaleWrite("terminal body result cannot be replaced implicitly")
                if (
                    current.target_projection is not None
                    and record.status != ItemStatus.REVIEWED
                    and record.target_projection != current.target_projection
                ):
                    raise StaleWrite("body draft can change only through a reviewed replacement")
                if record.status == ItemStatus.REVIEWED:
                    self._require_review_proof(record)
                elif record.status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}:
                    self._translation_proof(record)
                changes.append((current, record, path))
            for _, record, path in changes:
                self.store._base.atomic_write_bytes(path, canonical_json_bytes(record))
        for current, record, _ in changes:
            self._update_result(current, record)

    def recover_results(self) -> dict[str, ItemRecord]:
        for request in tuple(self._requests.values()):
            if (
                request.stage != "translate"
                or (response := _persisted_response(self.store, request, reconcile=True, finish=self._finish)) is None
            ):
                continue
            if not any(
                _saved_epoch(self._records[item_id], "translation_epoch")
                == request.record_versions[request.item_unit_ids[item_id][0]]
                for item_id in request.item_ids
            ):
                continue
            batch = self._translation_batch(request)
            wires = _wire_items(batch)
            batch_error = None
            try:
                if response.finish_reason in {"length", "max_tokens"}:
                    raise ProtocolError("translation response was truncated")
                parsed = validate_translation_response(response.raw, request.request_id, set(request.item_ids))
                if parsed.unknown:
                    raise ProtocolError("translation response contains unknown item IDs")
            except (KeyError, ProtocolError, TypeError, ValueError) as error:
                parsed, batch_error = None, str(error)
            recovered: list[ItemRecord] = []
            for member in batch.items:
                prior = self._records[member.item_id]
                if (
                    prior.status != ItemStatus.PENDING
                    or _saved_epoch(prior, "translation_epoch") != request.record_versions[member.unit_id]
                ):
                    continue
                accepted = parsed.accepted.get(member.item_id) if parsed is not None else None
                error = (
                    batch_error
                    if parsed is None
                    else parsed.errors.get(member.item_id, "translation item missing")
                    if accepted is None
                    else None
                )
                if accepted is not None:
                    error = _target_error(member, accepted["target"], wires[member.item_id])
                if error is not None:
                    recovered.append(_failed(member, prior, "translate", error, batch))
                    continue
                assert accepted is not None
                terms = tuple(term for term in wires[member.item_id]["terms"] if isinstance(term, Mapping))
                recovered.append(
                    ItemRecord(
                        item_id=member.item_id,
                        segment_id=member.item_id,
                        selected_term_ids=batch.manifest.term_ids_by_item[member.item_id],
                        term_applicability={str(term["term_id"]): term["role"] for term in terms},
                        terms_hash=batch.manifest.terms_hashes[member.item_id],
                        context_hash=batch.manifest.context_hashes[member.item_id],
                        stage="proofread",
                        status=ItemStatus.LOCAL_VALID,
                        target_projection=accepted["target"],
                        target_hash=canonical_hash(accepted["target"]),
                        checks={
                            "translation_frame": _frame(batch),
                            **(
                                {"translation_epoch": request.record_versions[member.unit_id]}
                                if request.record_versions[member.unit_id]
                                else {}
                            ),
                        },
                        request_id=batch.manifest.request_id,
                        next_action="review",
                    )
                )
            self.save_many(recovered)
        for request in tuple(self._requests.values()):
            if (
                request.stage != "review"
                or (response := _persisted_response(self.store, request, reconcile=True, finish=self._finish)) is None
            ):
                continue
            batch = self._review_batch(request)
            expected = {
                item.item_id: {
                    "base_revision": request.revisions[item.unit_id],
                    **review_applicability(item, self.session.index, _wire_items(batch)[item.item_id]),
                }
                for item in batch.items
            }
            batch_error = None
            try:
                if response.finish_reason in {"length", "max_tokens"}:
                    raise ProtocolError("review response was truncated")
                parsed = validate_review_response(response.raw, request.request_id, expected)
                if parsed.unknown or any(value.get("term_suggestions") for value in parsed.accepted.values()):
                    raise ProtocolError("review response changed its closed batch protocol")
            except (KeyError, ProtocolError, TypeError, ValueError) as error:
                parsed, batch_error = None, str(error)
            wires = _wire_items(batch)
            recovered = []
            for member in batch.items:
                prior = self._records[member.item_id]
                if (
                    prior.status not in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
                    or _saved_epoch(prior, "review_epoch") != request.revisions[member.unit_id]
                ):
                    continue
                decision = parsed.accepted.get(member.item_id) if parsed is not None else None
                error = (
                    batch_error
                    if parsed is None
                    else parsed.errors.get(member.item_id, "review item missing")
                    if decision is None
                    else None
                )
                if decision is not None and decision["decision"] == "replace":
                    error = _target_error(member, decision["target"], wires[member.item_id])
                if error is not None:
                    recovered.append(
                        prior.model_copy(
                            update={
                                "stage": "review",
                                "status": ItemStatus.NEEDS_ATTENTION,
                                "checks": dict(prior.checks) | {"accepted": False, "error": error},
                                "failure": {"stage": "review", "code": "review_failed", "message": error},
                                "next_action": None,
                            }
                        )
                    )
                    continue
                assert decision is not None
                if decision["decision"] == "needs_attention":
                    recovered.append(
                        prior.model_copy(
                            update={
                                "stage": "review",
                                "status": ItemStatus.NEEDS_ATTENTION,
                                "checks": dict(prior.checks)
                                | {"accepted": False, "error": str(decision["issues"] or "review needs attention")},
                                "failure": {
                                    "stage": "review",
                                    "code": "review_failed",
                                    "message": str(decision["issues"] or "review needs attention"),
                                },
                                "next_action": None,
                            }
                        )
                    )
                    continue
                target = decision["target"] if decision["decision"] == "replace" else prior.target_projection
                recovered.append(
                    prior.model_copy(
                        update={
                            "stage": "reviewed",
                            "status": ItemStatus.REVIEWED,
                            "target_projection": target,
                            "target_hash": canonical_hash(target),
                            "checks": {
                                "translation_frame": prior.checks["translation_frame"],
                                **(
                                    {"review_epoch": prior.checks["review_epoch"]}
                                    if "review_epoch" in prior.checks
                                    else {}
                                ),
                                **(
                                    {"translation_epoch": prior.checks["translation_epoch"]}
                                    if "translation_epoch" in prior.checks
                                    else {}
                                ),
                                "decision": decision["decision"],
                                "checks": decision["checks"],
                                "issues": decision["issues"],
                                "review_request_id": request.request_id,
                            },
                            "failure": None,
                            "next_action": None,
                        }
                    )
                )
            self.save_many(recovered)
        return self.records()

    def retry_units(self, unit_ids: Sequence[str], *, retry_unknown: bool = True) -> tuple[str, ...]:
        """Explicitly reopen failed review state while preserving its proven draft and counters."""
        selected = tuple(unit_ids)
        permitted = set(self.validate_retry_units(selected, retry_unknown=retry_unknown))
        reopened: list[str] = []
        for unit_id in selected:
            item_ids = self.session.prepared.plan.unit_members[unit_id]
            candidates = tuple(self._records[item_id] for item_id in item_ids if item_id in permitted)
            if not candidates:
                continue
            missing = tuple(record for record in candidates if record.target_projection is None)
            drafts = tuple(record for record in candidates if record.target_projection is not None)
            if missing:
                self._unlock_stage("translate", item_ids, retry_unknown=retry_unknown)
            if drafts:
                self._unlock_stage("review", item_ids, retry_unknown=retry_unknown)
            epoch = 1 + max(
                (_saved_epoch(record, "review_epoch") for record in (self._records[item_id] for item_id in item_ids)),
                default=0,
            )
            for record in candidates:
                if record.target_projection is None:
                    translation_epoch = 1 + max(
                        (_saved_epoch(self._records[item_id], "translation_epoch") for item_id in item_ids), default=0
                    )
                    reopened_record = self._initial_pending(record.item_id).model_copy(
                        update={"checks": {"translation_epoch": translation_epoch}}
                    )
                    self.store._base.atomic_write_bytes(
                        self._result_path(record.item_id), canonical_json_bytes(reopened_record)
                    )
                    self._update_result(record, reopened_record)
                    reopened.append(record.item_id)
                    continue
                checks = _retry_checks(record, epoch)
                reopened_record = record.model_copy(
                    update={
                        "stage": "proofread",
                        "status": ItemStatus.LOCAL_VALID,
                        "checks": checks,
                        "failure": None,
                        "next_action": "review",
                    }
                )
                _validate_saved_record(self.session, reopened_record)
                self.store._base.atomic_write_bytes(
                    self._result_path(record.item_id), canonical_json_bytes(reopened_record)
                )
                self._update_result(record, reopened_record)
                reopened.append(record.item_id)
        return tuple(reopened)

    def validate_retry_units(self, unit_ids: Sequence[str], *, retry_unknown: bool = True) -> tuple[str, ...]:
        selected = tuple(unit_ids)
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("explicit atomic retry requires unique Unit IDs")
        if not set(selected).issubset(self.session.prepared.plan.unit_members):
            raise IdentityMismatch("explicit atomic retry names an unknown Unit")
        authorized_items = {
            item_id for unit_id in selected for item_id in self.session.prepared.plan.unit_members[unit_id]
        }
        for request in self._requests.values():
            if (
                request.stage in {"translate", "review"}
                and self._ambiguous(request)
                and authorized_items.intersection(request.item_ids)
                and not set(request.item_ids).issubset(authorized_items)
            ):
                raise IdentityMismatch("explicit retry must include every Unit in an ambiguous shared review request")
        retryable: list[str] = []
        for unit_id in selected:
            item_ids = self.session.prepared.plan.unit_members[unit_id]
            unknown = any(
                self._ambiguous(request)
                for request in self._requests.values()
                if request.stage in {"translate", "review"} and set(item_ids).intersection(request.item_ids)
            )
            if unknown and not retry_unknown:
                raise RuntimePaused("explicit retry must authorize an unknown review outcome")
            candidates = tuple(
                self._records[item_id]
                for item_id in item_ids
                if self._records[item_id].status == ItemStatus.NEEDS_ATTENTION
                or unknown
                and self._records[item_id].status in {ItemStatus.PENDING, ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
            )
            for record in candidates:
                _saved_epoch(record, "translation_epoch")
                if record.target_projection is not None:
                    self._translation_proof(record)
                retryable.append(record.item_id)
        return tuple(retryable)

    def runtime(
        self,
        model: Any = None,
        transport: Any = None,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> ModelRuntime:
        config = self.session.prepared.plan.translation_config
        model_id = _text(config, "model")
        if model is not None and getattr(model, "id", None) != model_id:
            raise IdentityMismatch("body provider model differs from the frozen ready model")
        runtime_model = SimpleNamespace(id=model_id) if model is None and transport is not None else model
        self._progress = progress
        self._started: dict[str, float] = {}
        runtime = ModelRuntime(
            model=runtime_model,
            transport=transport,
            rpm=_optional(config, "rpm"),
            tpm=_optional(config, "tpm"),
            max_inflight=_positive(config, "concurrency", 2),
            reserve_attempt=self._reserve,
            finish_attempt=self._finish,
            persist_response=self._persist,
            prepare_request=self._prepare,
            replay_response=self._replay,
            max_transport_retries=_bounded(config, "max_transport_retries", 2, 0, 2),
            request_timeout_seconds=_number(config, "request_timeout_seconds", 120.0),
            model_max_output_tokens=_positive(config, "max_output_tokens", 4096),
            provider_output_token_field=(
                "max_tokens" if config.get("provider", "agnes") == "agnes" else "max_completion_tokens"
            ),
            prior_input_limit_breach=self._prior_input_breach,
            input_budget_version=2,
        )
        if runtime.model_id != model_id:
            raise IdentityMismatch("configured provider resolved to a different frozen model")
        return runtime

    def parent_targets(self, require_complete: bool = True) -> dict[str, str]:
        """Merge reviewed siblings and derive navigation labels from their title source."""
        self.session.verify()
        records = self._records
        members = self.session.index.members
        parents = {item.item_id: item for inventory in self.session.index.inventories for item in inventory.items}
        grouped: dict[str, list[Any]] = defaultdict(list)
        for member in members:
            grouped[member.parent_item_id].append(member)
        targets: dict[str, str] = {}
        missing: list[str] = []
        for parent_id, siblings in grouped.items():
            if siblings[0].unit_id in self.session.prepared.plan.derived_sources:
                continue
            selected = {member.item_id: records[member.item_id] for member in siblings}
            if any(record.status != ItemStatus.REVIEWED for record in selected.values()):
                missing.append(parent_id)
                continue
            targets[parent_id] = merge_member_targets(parents[parent_id], siblings, selected)
        by_unit: dict[str, list[str]] = defaultdict(list)
        for parent_id, siblings in grouped.items():
            by_unit[siblings[0].unit_id].append(parent_id)
        units = {
            unit.unit_id: unit for inventory in self.session.index.inventories for unit in inventory.document.units
        }
        for unit_id, source_unit_id in self.session.prepared.plan.derived_sources.items():
            derived_parents, source_parents = by_unit[unit_id], by_unit[source_unit_id]
            if len(derived_parents) != 1 or len(source_parents) != 1 or source_parents[0] not in targets:
                missing.extend(derived_parents)
                continue
            targets[derived_parents[0]] = derive_navigation_projection(units[unit_id], targets[source_parents[0]])
        if require_complete and missing:
            raise IdentityMismatch(f"body results are incomplete for parent items: {', '.join(sorted(set(missing)))}")
        return targets

    def parent_versions(self, require_complete: bool = True) -> dict[str, int]:
        return {parent_id: 1 for parent_id in self.parent_targets(require_complete=require_complete)}

    def progress_snapshot(self) -> dict[str, Any]:
        return {
            "required_items": len(self._records),
            "required_units": self.session.prepared.plan.required_unit_count,
            "translated_items": self._translated,
            "reviewed_items": self._reviewed,
            "accepted_units": len(self._accepted_units),
            "needs_attention_units": len(self._attention_units),
            "pending_items": sum(
                len(item_ids)
                for unit_id, item_ids in self.session.prepared.plan.unit_members.items()
                if unit_id not in self._accepted_units and unit_id not in self._attention_units
            ),
            "http_attempts": self._actual_attempts,
            "body_http_attempts": self._body_actual_attempts,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
        }

    def _translation_batch(self, request: RequestManifest):
        members = tuple(self.session.index.members_by_id[item_id] for item_id in request.item_ids)
        packed = pack_members(
            "translate",
            members,
            self.session.prepared.glossary,
            self.session.index,
            limits_from_config(self.session.prepared.plan.translation_config),
            record_versions=request.record_versions,
            plan_epochs=request.plan_epochs,
            tokenizer_model=_text(self.session.prepared.plan.translation_config, "model"),
        )
        if len(packed.batches) != 1 or packed.blocked:
            raise IdentityMismatch("persisted translation request is no longer a canonical fitting batch")
        batch = packed.batches[0]
        if request.model_copy(update={"attempts": ()}) != batch.manifest:
            raise IdentityMismatch("persisted translation request differs from its frozen source frame")
        return batch

    def _review_batch(self, request: RequestManifest):
        members = tuple(self.session.index.members_by_id[item_id] for item_id in request.item_ids)
        targets = {
            item_id: self._review_target(
                item_id,
                request.target_hashes[item_id],
                request.revisions[request.item_unit_ids[item_id][0]],
            )
            for item_id in request.item_ids
        }
        packed = pack_members(
            "review",
            members,
            self.session.prepared.glossary,
            self.session.index,
            limits_from_config(self.session.prepared.plan.translation_config),
            targets=targets,
            revisions=request.revisions,
            record_versions=request.record_versions,
            plan_epochs=request.plan_epochs,
            tokenizer_model=_text(self.session.prepared.plan.translation_config, "model"),
        )
        if len(packed.batches) != 1 or packed.blocked:
            raise IdentityMismatch("persisted review request is no longer a canonical fitting batch")
        batch = packed.batches[0]
        if request.model_copy(update={"attempts": ()}) != batch.manifest:
            raise IdentityMismatch("persisted review request differs from its saved target frame")
        return batch

    def _review_target(self, item_id: str, target_hash: str, review_epoch: int) -> ItemRecord:
        current = self._records[item_id]
        if (
            current.status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
            and current.target_hash == target_hash
            and _saved_epoch(current, "review_epoch") == review_epoch
        ):
            return current
        for request in self._requests.values():
            if request.stage != "translate" or item_id not in request.item_ids:
                continue
            response = self._response(request)
            if response is None:
                continue
            accepted = validate_translation_response(response.raw, request.request_id, set(request.item_ids)).accepted
            target = accepted.get(item_id, {}).get("target")
            if isinstance(target, str) and canonical_hash(target) == target_hash:
                return current.model_copy(
                    update={
                        "stage": "proofread",
                        "status": ItemStatus.LOCAL_VALID,
                        "target_projection": target,
                        "target_hash": target_hash,
                        "checks": {
                            "translation_frame": current.checks["translation_frame"],
                            **({"review_epoch": review_epoch} if review_epoch else {}),
                            **(
                                {"translation_epoch": current.checks["translation_epoch"]}
                                if "translation_epoch" in current.checks
                                else {}
                            ),
                        },
                        "failure": None,
                        "next_action": "review",
                    }
                )
        raise IdentityMismatch("persisted review request has no proven saved draft")

    def _initial_pending(self, item_id: str) -> ItemRecord:
        for request_id in self.session.prepared.plan.batch_hashes:
            batch = MemberBatch.model_validate_json(
                state.read(self.store.root / "batches" / f"{safe_id(request_id)}.json")
            )
            if item_id not in batch.manifest.item_ids:
                continue
            self.session.verify_batch(batch, initial=True)
            wire = _wire_items(batch)[item_id]
            terms = tuple(term for term in wire["terms"] if isinstance(term, Mapping))
            return ItemRecord(
                item_id=item_id,
                segment_id=item_id,
                selected_term_ids=batch.manifest.term_ids_by_item[item_id],
                term_applicability={str(term["term_id"]): term["role"] for term in terms},
                terms_hash=batch.manifest.terms_hashes[item_id],
                context_hash=batch.manifest.context_hashes[item_id],
            )
        raise IdentityMismatch("explicit retry member has no committed initial batch")

    def _prepare(self, stage: str, payload: dict[str, Any], context: Mapping[str, Any]) -> None:
        self.session.verify()
        manifest = _manifest(context)
        output = context.get("output_tokens")
        if stage != manifest.stage or wire_hash(cast(Any, stage), payload, cast(int, output)) != manifest.wire_hash:
            raise IdentityMismatch("body request wire differs from its frozen manifest")
        if not set(manifest.item_ids).issubset(self.session.index.members_by_id):
            raise IdentityMismatch("body request references a member outside the ready plan")
        path = self.store._path("requests", manifest.request_id)
        if state.exists(path):
            existing = self._requests.get(manifest.request_id) or self.store.read_request(manifest.request_id)
            if existing.model_copy(update={"attempts": ()}) != manifest:
                raise StaleWrite("body request identity changed during resume")
        else:
            self._requests[manifest.request_id] = self.store.write_request(manifest)
        measured = measure_budget(
            stage=cast(Any, stage),
            payload=payload,
            limits=limits_from_config(self.session.prepared.plan.translation_config),
            tokenizer_model=_text(self.session.prepared.plan.translation_config, "model"),
        )
        self._started[manifest.request_id] = monotonic()
        self._emit(
            {
                "event": "request",
                "stage": stage,
                "request_id": manifest.request_id,
                "item_ids": manifest.item_ids,
                "source_tokens": measured.source_tokens,
                "estimated_input_tokens": measured.input_tokens,
                "reserved_input_tokens": measured.input_reserve,
                "reserved_output_tokens": measured.output_tokens,
            }
        )

    def _replay(self, stage: str, _payload: dict[str, Any], context: Mapping[str, Any]) -> dict[str, Any] | None:
        manifest = self._requests[_manifest(context).request_id]
        for attempt in reversed(manifest.attempts):
            response = self.store.read_model_response(
                cast(ModelResponseStage, stage), manifest.request_id, attempt.attempt_id
            )
            if response is None:
                continue
            usage = (
                Usage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    known_cost=response.usage.known_cost,
                )
                if response.usage is not None
                else None
            )
            if attempt.state != "failed":
                self._finish(
                    manifest.request_id,
                    attempt.attempt_id,
                    state="succeeded",
                    usage=usage,
                    finished_at=attempt.finished_at or attempt.sent_at or attempt.created_at,
                    metadata=response.metadata,
                )
            self._emit_response(stage, manifest.request_id, response.model_dump(mode="python"), replayed=True)
            return response.model_dump(mode="python")
        if self._ambiguous(manifest):
            raise RuntimePaused("body request has an unknown provider outcome without a persisted response")
        return None

    def _persist(
        self,
        stage: ModelResponseStage,
        request_id: str,
        attempt_id: str,
        response: dict[str, Any],
    ) -> None:
        self.store.save_model_response(stage, request_id, attempt_id, response)
        request = self._requests[request_id]
        attempt = next(value for value in request.attempts if value.attempt_id == attempt_id)
        self._account_usage(request_id, attempt_id, self._usage(request, attempt))
        self._emit_response(stage, request_id, response, replayed=False)

    def _emit_response(self, stage: str, request_id: str, response: Mapping[str, Any], *, replayed: bool) -> None:
        usage = response.get("usage")
        self._emit(
            {
                "event": "response",
                "stage": stage,
                "request_id": request_id,
                "replayed": replayed,
                "elapsed_seconds": max(0.0, monotonic() - self._started.get(request_id, monotonic())),
                "actual_input_tokens": usage.get("input_tokens") if isinstance(usage, Mapping) else None,
                "actual_output_tokens": usage.get("output_tokens") if isinstance(usage, Mapping) else None,
                "finish_reason": response.get("finish_reason"),
            }
        )

    def _emit(self, event: dict[str, Any]) -> None:
        callback = getattr(self, "_progress", None)
        if callback is not None:
            callback(event)

    def _reserve(self, request_id: str, attempt) -> None:
        self.session.verify()
        if self._ambiguous(self._requests[request_id]):
            raise RuntimePaused("body request has an unknown provider outcome without a persisted response")
        if self._run_limit and self._run_attempts >= self._run_limit:
            raise RuntimePaused("frozen body request limit is exhausted")
        self._requests[request_id] = self.store.reserve_attempt(request_id, attempt)
        self._run_attempts += 1
        self._body_attempts += 1

    def _ambiguous(self, request: RequestManifest) -> bool:
        return any(
            (
                attempt.state in {"sent", "unknown"}
                or attempt.state == "reserved"
                and any(
                    value is not None for value in (attempt.sent_at, attempt.finished_at, attempt.usage, attempt.error)
                )
            )
            and self.store.read_model_response(request.stage, request.request_id, attempt.attempt_id) is None
            for attempt in request.attempts
        )

    def _finish(
        self,
        request_id: str,
        attempt_id: str,
        *,
        state: str,
        usage: Usage | None = None,
        error: str | None = None,
        sent_at: str | None = None,
        finished_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        request = self._requests[request_id]
        prior = next(attempt for attempt in request.attempts if attempt.attempt_id == attempt_id)
        updated = self.store.finish_attempt(
            request_id,
            attempt_id,
            state=cast(Any, state),
            usage=usage,
            error=error,
            sent_at=sent_at,
            finished_at=finished_at,
            metadata=metadata,
        )
        current = next(attempt for attempt in updated.attempts if attempt.attempt_id == attempt_id)
        if prior.state == "reserved" and current.state != "reserved":
            self._actual_attempts += 1
            if updated.stage in {"translate", "review"}:
                self._body_actual_attempts += 1
        self._requests[request_id] = updated
        self._account_usage(request_id, attempt_id, self._usage(updated, current))

    def _account_usage(self, request_id: str, attempt_id: str, usage: Usage | None) -> None:
        key = (request_id, attempt_id)
        if usage is None or key in self._accounted_usage:
            return
        self._accounted_usage.add(key)
        self._input_tokens += usage.input_tokens
        self._output_tokens += usage.output_tokens

    def _load_records(self) -> dict[str, ItemRecord]:
        result: dict[str, ItemRecord] = {}
        for item_id in self.session.prepared.plan.member_hashes:
            path = self._result_path(item_id)
            try:
                record = ItemRecord.model_validate_json(state.read(path))
            except Exception as error:
                raise CorruptRecord(f"invalid body result {path}: {error}") from error
            if record.item_id != item_id or record.segment_id != item_id:
                raise IdentityMismatch(f"body result ownership changed: {item_id}")
            result[item_id] = record
        return result

    def _load_requests(self) -> dict[str, RequestManifest]:
        result: dict[str, RequestManifest] = {}
        for path in sorted(state.glob(self.store.root / "requests", "*.json")):
            request = self.store.read_request(path.stem)
            if request.request_id != path.stem or request.request_id in result:
                raise IdentityMismatch("request filename or request ID inventory changed")
            result[request.request_id] = request
        return result

    def _initialize_counts(self) -> None:
        attempts = tuple(attempt for request in self._requests.values() for attempt in request.attempts)
        body = tuple(
            attempt
            for request in self._requests.values()
            if request.stage in {"translate", "review"}
            for attempt in request.attempts
        )
        usages = tuple(
            (request.request_id, attempt.attempt_id, usage)
            for request in self._requests.values()
            for attempt in request.attempts
            for usage in (self._usage(request, attempt),)
            if usage is not None
        )
        self._actual_attempts = sum(attempt.state != "reserved" for attempt in attempts)
        self._body_actual_attempts = sum(attempt.state != "reserved" for attempt in body)
        self._run_attempts = len(attempts)
        self._body_attempts = len(body)
        self._accounted_usage = {(request_id, attempt_id) for request_id, attempt_id, _ in usages}
        self._input_tokens = sum(usage.input_tokens for _, _, usage in usages)
        self._output_tokens = sum(usage.output_tokens for _, _, usage in usages)
        self._prior_input_breach = max(
            (usage.input_tokens for _, _, usage in usages if usage.input_tokens > MAX_MODEL_INPUT_TOKENS),
            default=None,
        )
        self._translated = sum(record.target_projection is not None for record in self._records.values())
        self._reviewed = sum(record.status == ItemStatus.REVIEWED for record in self._records.values())
        self._attention_units = {
            unit_id
            for unit_id, item_ids in self.session.prepared.plan.unit_members.items()
            if any(self._records[item_id].status == ItemStatus.NEEDS_ATTENTION for item_id in item_ids)
        }
        self._accepted_units = {
            unit_id
            for unit_id, item_ids in self.session.prepared.plan.unit_members.items()
            if unit_id not in self.session.prepared.plan.derived_sources
            and all(self._records[item_id].status == ItemStatus.REVIEWED for item_id in item_ids)
        }
        self._propagate_derived()

    def _update_result(self, prior: ItemRecord, current: ItemRecord) -> None:
        self._records[current.item_id] = current
        self._translated += int(current.target_projection is not None) - int(prior.target_projection is not None)
        self._reviewed += int(current.status == ItemStatus.REVIEWED) - int(prior.status == ItemStatus.REVIEWED)
        unit_id = self.session.index.members_by_id[current.item_id].unit_id
        item_ids = self.session.prepared.plan.unit_members[unit_id]
        values = tuple(self._records[item_id] for item_id in item_ids)
        if any(value.status == ItemStatus.NEEDS_ATTENTION for value in values):
            self._attention_units.add(unit_id)
        else:
            self._attention_units.discard(unit_id)
        if all(value.status == ItemStatus.REVIEWED for value in values):
            self._accepted_units.add(unit_id)
            self._propagate_derived()
        elif unit_id in self._accepted_units:
            self._accepted_units.discard(unit_id)
            while True:
                stale = {
                    derived
                    for derived, source in self.session.prepared.plan.derived_sources.items()
                    if derived in self._accepted_units and source not in self._accepted_units
                }
                if not stale:
                    break
                self._accepted_units.difference_update(stale)

    def _propagate_derived(self) -> None:
        remaining = {
            unit_id: source_id
            for unit_id, source_id in self.session.prepared.plan.derived_sources.items()
            if unit_id not in self._accepted_units
        }
        while remaining:
            ready = {unit_id for unit_id, source_id in remaining.items() if source_id in self._accepted_units}
            if not ready:
                return
            self._accepted_units.update(ready)
            for unit_id in ready:
                del remaining[unit_id]

    def _usage(self, request: RequestManifest, attempt) -> Usage | None:
        if attempt.usage is not None:
            return attempt.usage
        response = self.store.read_model_response(request.stage, request.request_id, attempt.attempt_id)
        if response is None or response.usage is None:
            return None
        return Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            known_cost=response.usage.known_cost,
        )

    def _translation_proof(self, record: ItemRecord) -> None:
        request = self._requests.get(record.request_id or "")
        if request is None or request.stage != "translate" or record.item_id not in request.item_ids:
            raise IdentityMismatch("saved draft lacks its durable translation request")
        response = self._response(request)
        if response is None:
            raise IdentityMismatch("saved draft lacks its persisted translation response")
        parsed = validate_translation_response(response.raw, request.request_id, set(request.item_ids))
        if parsed.unknown:
            raise IdentityMismatch("translation response contains unknown item IDs")
        result = parsed.accepted.get(record.item_id)
        if (
            result is None
            or record.target_projection != result["target"]
            or record.target_hash != canonical_hash(result["target"])
        ):
            raise IdentityMismatch("saved draft differs from its persisted translation response")

    def _unlock_stage(self, stage: str, item_ids: Sequence[str], *, retry_unknown: bool) -> bool:
        selected = set(item_ids)
        unlocked = False
        for request in tuple(self._requests.values()):
            if request.stage != stage or not selected.intersection(request.item_ids):
                continue
            for attempt in request.attempts:
                if not (
                    attempt.state in {"sent", "unknown"}
                    or attempt.state == "reserved"
                    and any(
                        value is not None
                        for value in (attempt.sent_at, attempt.finished_at, attempt.usage, attempt.error)
                    )
                ):
                    continue
                if self.store.read_model_response(request.stage, request.request_id, attempt.attempt_id) is not None:
                    continue
                if not retry_unknown:
                    raise RuntimePaused(f"explicit retry must authorize an unknown {stage} outcome")
                self._finish(
                    request.request_id,
                    attempt.attempt_id,
                    state="failed",
                    error=f"explicit retry authorized after unknown {stage} outcome",
                    finished_at=attempt.finished_at or datetime.now(UTC).isoformat(),
                )
                unlocked = True
        return unlocked

    def _review_proof(self, record: ItemRecord) -> None:
        if record.stage != "reviewed" or record.failure is not None or record.next_action is not None:
            raise IdentityMismatch("reviewed result has an invalid terminal state")
        translation = self._requests.get(record.request_id or "")
        review_id = record.checks.get("review_request_id")
        review = self._requests.get(review_id) if isinstance(review_id, str) else None
        if translation is None or translation.stage != "translate" or record.item_id not in translation.item_ids:
            raise IdentityMismatch("reviewed result lacks its durable translation request")
        if review is None or review.stage != "review" or record.item_id not in review.item_ids:
            raise IdentityMismatch("reviewed result lacks its durable review request")
        translated = self._response(translation)
        reviewed = self._response(review)
        if translated is None or reviewed is None:
            raise IdentityMismatch("reviewed result lacks a succeeded persisted response")
        if translation.request_id not in self._translations:
            parsed_translation = validate_translation_response(
                translated.raw, translation.request_id, set(translation.item_ids)
            )
            if parsed_translation.unknown:
                raise IdentityMismatch("translation response contains unknown item IDs")
            self._translations[translation.request_id] = parsed_translation.accepted
        translation_result = self._translations[translation.request_id].get(record.item_id)
        if translation_result is None:
            raise IdentityMismatch("reviewed result was not accepted by its translation response")
        draft = translation_result["target"]
        if review.target_hashes.get(record.item_id) != canonical_hash(draft):
            raise IdentityMismatch("review request does not prove the saved translation draft")
        review_batch = self._review_batch(review)
        wires = _wire_items(review_batch)
        expected = {
            item_id: {
                "base_revision": review.revisions[review.item_unit_ids[item_id][0]],
                **review_applicability(self.session.index.members_by_id[item_id], self.session.index, wires[item_id]),
            }
            for item_id in review.item_ids
        }
        if review.request_id not in self._reviews:
            parsed_review = validate_review_response(reviewed.raw, review.request_id, expected)
            if parsed_review.unknown or any(
                value.get("term_suggestions") for value in parsed_review.accepted.values()
            ):
                raise IdentityMismatch("review response changed its closed batch protocol")
            self._reviews[review.request_id] = parsed_review.accepted
        decision = self._reviews[review.request_id].get(record.item_id)
        if (
            decision is None
            or record.checks.get("decision") != decision["decision"]
            or record.checks.get("checks") != decision["checks"]
            or record.checks.get("issues") != decision["issues"]
        ):
            raise IdentityMismatch("reviewed result differs from its persisted review decision")
        expected_target = decision.get("target") if decision["decision"] == "replace" else draft
        if record.target_projection != expected_target or record.target_hash != canonical_hash(expected_target):
            raise IdentityMismatch("reviewed target differs from its persisted responses")

    def _require_review_proof(self, record: ItemRecord) -> None:
        try:
            self._review_proof(record)
        except IdentityMismatch:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise IdentityMismatch(f"reviewed result proof is invalid: {error}") from error

    def _response(self, request: RequestManifest):
        key = (request.stage, request.request_id)
        if key in self._responses:
            return self._responses[key]
        response = _persisted_response(self.store, request)
        if response is not None and response.finish_reason not in {"length", "max_tokens"}:
            self._responses[key] = response
            return response
        return None

    def _result_path(self, item_id: str) -> Path:
        return self.store.root / "results" / f"{safe_id(item_id)}.json"


__all__ = ["BodyJournal"]
