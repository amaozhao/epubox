"""The single v2.5 translation and review executor."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from engine.agents.protocol import ProtocolError, validate_coherence_response
from engine.agents.runtime import (
    MAX_MODEL_INPUT_TOKENS,
    InputBudgetError,
    MalformedEnvelopeError,
    RequestError,
    RuntimePaused,
    model_input_budget,
    request_messages,
    wire_hash,
)
from engine.core.tokens import count_tokens
from engine.execution.state import State, TranslationPaused, _DocumentCoherenceBudgetExhausted
from engine.execution.utility import (
    _positive_number,
)
from engine.item.context import plan_unit
from engine.schemas.contracts import (
    ItemStatus,
    JsonValue,
    RequestManifest,
    UnitRecord,
    canonical_hash,
)
from engine.services.coherence import (
    pending_windows,
    save_document_check,
    save_window_result,
    window_payload,
)


class Coherence(State):
    if TYPE_CHECKING:

        def _prepare_checks(self) -> None: ...

        def _unit_complete(self, record: UnitRecord) -> bool: ...

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
            if response.finish_reason in {"length", "max_tokens"}:
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
                    output_tokens = self._coherence_min_output_tokens(pending_batch)
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
                        wire_hash=wire_hash("coherence", payload, None),
                        output_unlimited=True,
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
                                "output_tokens": None,
                                "estimated_output_tokens": output_tokens,
                            },
                        )
                        if response.get("finish_reason") in {"length", "max_tokens"}:
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
            if current and tokens > self._coherence_input_limit():
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
        return min(MAX_MODEL_INPUT_TOKENS, self.runtime.tpm)

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
