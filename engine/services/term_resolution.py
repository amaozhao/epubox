"""One bounded semantic check per source-evidenced terminology conflict."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from engine.agents.protocol import ProtocolError
from engine.agents.runtime import (
    MAX_MODEL_INPUT_TOKENS,
    RESOLUTION_PROTOCOL_VERSION,
    RequestError,
    RuntimePaused,
    model_input_budget,
    wire_hash,
)
from engine.agents.term_protocol import validate_resolution_batch_response, validate_resolution_response
from engine.core.tokens import count_tokens
from engine.schemas.contracts import JsonValue, RequestManifest, Usage
from engine.services.store import RunStore
from engine.services.term_freeze import ResolutionDecision
from engine.services.term_runner import TermBudgetPaused, TermRunner


@dataclass(frozen=True)
class ResolutionResult:
    status: Literal["closed", "paused"]
    selected: int
    deferred: int
    pending: int
    http_attempts: int


class TermResolutionRunner:
    def __init__(self, store: RunStore, *, model: Any = None, transport: Any = None):
        self.term_runner = TermRunner(store, model=model, transport=transport)
        self.store = store
        self.preparation = store.read_preparation()
        self.pool = store.read_candidate_pool()
        self._replayed_splits: list[tuple[dict[str, JsonValue], ...]] = []
        if self.pool.extraction_status != "open":
            raise ValueError("conflict resolution requires an open candidate pool")

    def _save_group(self, group_id: str, fields: dict[str, JsonValue], response_id: str | None = None) -> None:
        groups = tuple(
            group | fields if group.get("group_id") == group_id else group for group in self.pool.conflict_groups
        )
        consumed = self.pool.consumed_response_ids
        if response_id is not None:
            consumed = tuple(sorted({*consumed, response_id}))
        updated = self.pool.model_copy(
            update={
                "conflict_groups": groups,
                "consumed_response_ids": consumed,
                "record_version": self.pool.record_version + 1,
            }
        )
        self.pool = self.store.save_candidate_pool(updated, expected_record_version=self.pool.record_version)

    def _previous_sent(self, group_id: str) -> bool:
        return any(
            request.stage == "resolution"
            and group_id in request.item_ids
            and any(attempt.state in {"sent", "unknown", "succeeded"} for attempt in request.attempts)
            for request in self.term_runner._requests()
        )

    def _replay_v1(self, group: dict[str, JsonValue]) -> bool:
        group_id = str(group["group_id"])
        for request in reversed(self.term_runner._requests()):
            if request.stage != "resolution" or request.item_ids != (group_id,):
                continue
            for attempt in reversed(request.attempts):
                response = self.store.read_model_response("resolution", request.request_id, attempt.attempt_id)
                if response is None:
                    continue
                self._finish_replayed_attempt(request.request_id, attempt, response)
                try:
                    if response.finish_reason == "length":
                        raise ProtocolError("resolution response was truncated")
                    result = validate_resolution_response(
                        response.raw,
                        request.request_id,
                        group_id,
                        set(_ids(group, "candidate_ids")),
                        set(_ids(group, "allowed_unit_ids")),
                    )
                except ProtocolError as error:
                    self._save_group(
                        group_id,
                        {
                            "decision": "defer",
                            "status": "deferred_conflict",
                            "reason": f"resolution_failed:{error}",
                        },
                        request.request_id,
                    )
                else:
                    self._save_group(
                        group_id,
                        {
                            "decision": result["decision"],
                            "status": "resolved" if result["decision"] == "select" else "deferred_conflict",
                            "selected_candidate_ids": result["selected_candidate_ids"],
                            "restricted_unit_ids": result.get("restricted_unit_ids", []),
                            "reason": result["reason"],
                        },
                        request.request_id,
                    )
                return True
        return False

    def _payload_group(self, group: dict[str, JsonValue]) -> dict[str, Any]:
        candidate_ids = _ids(group, "candidate_ids")
        allowed = _ids(group, "allowed_unit_ids")
        candidates = {candidate.candidate_id: candidate for candidate in self.pool.candidates}
        return {
            "group_id": group["group_id"],
            "group_input_hash": group["group_input_hash"],
            "allowed_unit_ids": list(allowed),
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "source": candidates[candidate_id].source,
                    "target": candidates[candidate_id].target,
                    "evidence": [
                        {"view_id": evidence.view_id, "source_quote": evidence.source_quote}
                        for evidence in candidates[candidate_id].evidence
                        if evidence.evidence_check == "source_matched"
                    ],
                }
                for candidate_id in candidate_ids
            ],
        }

    def _payload(self, group: dict[str, JsonValue], request_id: str) -> dict[str, Any]:
        return {"protocol": "epubox-term-resolution-1", "request_id": request_id, **self._payload_group(group)}

    def _batch_payload(self, groups: tuple[dict[str, JsonValue], ...], request_id: str) -> dict[str, Any]:
        return {
            "protocol": RESOLUTION_PROTOCOL_VERSION,
            "request_id": request_id,
            "items": [self._payload_group(group) for group in groups],
        }

    async def run(self) -> ResolutionResult:
        protocol = self.preparation.extraction_config.get("resolution_protocol_version", "epubox-term-resolution-1")
        if protocol == "epubox-term-resolution-1":
            return await self._run_v1()
        if protocol != RESOLUTION_PROTOCOL_VERSION:
            raise ValueError(f"unsupported terminology resolution protocol: {protocol!r}")
        return await self._run_v2()

    async def _run_v1(self) -> ResolutionResult:
        paused = False
        groups = sorted(self.pool.conflict_groups, key=lambda group: str(group.get("group_id")))
        for index, group in enumerate(groups):
            group_id = group.get("group_id")
            if not isinstance(group_id, str):
                raise TypeError("stored conflict group has no group_id")
            current = next(item for item in self.pool.conflict_groups if item.get("group_id") == group_id)
            if current.get("decision") in {"select", "defer"}:
                continue
            if self._replay_v1(current):
                continue
            if index >= self.term_runner.plan.resolution_group_limit or self._previous_sent(group_id):
                self._save_group(
                    group_id,
                    {
                        "decision": "defer",
                        "status": "deferred_conflict",
                        "reason": "resolution_limit_or_unknown_result",
                    },
                )
                continue
            request_id = f"rr-{uuid4().hex}"
            payload = self._payload(current, request_id)
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            estimated_tokens = count_tokens(encoded) + self.term_runner.output_tokens
            estimated_input = model_input_budget("resolution", payload)["estimated_input_tokens"]
            if estimated_input > self.term_runner._input_limit():
                self._save_group(
                    group_id,
                    {
                        "decision": "defer",
                        "status": "deferred_conflict",
                        "reason": "unplannable_input_budget",
                    },
                )
                continue
            input_hash = current.get("group_input_hash")
            if not isinstance(input_hash, str):
                raise TypeError("stored conflict group has no input hash")
            self.store.write_request(
                RequestManifest(
                    request_id=request_id,
                    stage="resolution",
                    owner_kind="resolution_group",
                    owner_id=group_id,
                    item_ids=(group_id,),
                    input_hashes={group_id: input_hash},
                    wire_hash=wire_hash("resolution", payload, self.term_runner.output_tokens),
                )
            )
            try:
                response = await self.term_runner.runtime.invoke(
                    "resolution",
                    payload,
                    {
                        "request_id": request_id,
                        "item_ids": (group_id,),
                        "estimated_tokens": estimated_tokens,
                        "output_tokens": self.term_runner.output_tokens,
                    },
                )
                if response.get("finish_reason") == "length":
                    raise ProtocolError("resolution response was truncated")
                result = validate_resolution_response(
                    response["raw"],
                    request_id,
                    group_id,
                    set(_ids(current, "candidate_ids")),
                    set(_ids(current, "allowed_unit_ids")),
                )
                self._save_group(
                    group_id,
                    {
                        "decision": result["decision"],
                        "status": "resolved" if result["decision"] == "select" else "deferred_conflict",
                        "selected_candidate_ids": result["selected_candidate_ids"],
                        "restricted_unit_ids": result.get("restricted_unit_ids", []),
                        "reason": result["reason"],
                    },
                    request_id,
                )
            except (RuntimePaused, TermBudgetPaused):
                paused = True
                break
            except (ProtocolError, RequestError) as error:
                if isinstance(error, RequestError) and error.attempts == 0:
                    reason = "unplannable"
                else:
                    reason = f"resolution_failed:{error}"
                self._save_group(
                    group_id,
                    {"decision": "defer", "status": "deferred_conflict", "reason": reason},
                    request_id,
                )
        selected = sum(group.get("decision") == "select" for group in self.pool.conflict_groups)
        deferred = sum(group.get("decision") == "defer" for group in self.pool.conflict_groups)
        pending = len(self.pool.conflict_groups) - selected - deferred
        return ResolutionResult(
            "paused" if paused or pending else "closed",
            selected,
            deferred,
            pending,
            self.term_runner._spent(actual=True),
        )

    def _eligible_v2_groups(self) -> tuple[dict[str, JsonValue], ...]:
        groups = sorted(self.pool.conflict_groups, key=lambda group: str(group.get("group_id")))
        for group in groups[self.term_runner.plan.resolution_group_limit :]:
            group_id = group.get("group_id")
            if isinstance(group_id, str) and group.get("decision") not in {"select", "defer"}:
                self._save_group(
                    group_id,
                    {"decision": "defer", "status": "deferred_conflict", "reason": "resolution_limit"},
                )
        return tuple(groups[: self.term_runner.plan.resolution_group_limit])

    def _budget_ok(self, groups: tuple[dict[str, JsonValue], ...]) -> bool:
        payload = self._batch_payload(groups, "rr-" + "0" * 32)
        estimated_input = model_input_budget("resolution", payload)["estimated_input_tokens"]
        return (
            estimated_input <= min(MAX_MODEL_INPUT_TOKENS, self.term_runner._input_limit())
            and self._minimal_response_tokens(groups) <= self.term_runner.output_tokens
        )

    @staticmethod
    def _minimal_response_tokens(groups: tuple[dict[str, JsonValue], ...]) -> int:
        envelope = {
            "protocol": RESOLUTION_PROTOCOL_VERSION,
            "request_id": "rr-" + "0" * 32,
            "items": [
                {
                    "group_id": group["group_id"],
                    "decision": "defer",
                    "selected_candidate_ids": [],
                    "reason": "x",
                }
                for group in groups
            ],
        }
        return count_tokens(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))

    def _batches(self, groups: tuple[dict[str, JsonValue], ...]) -> tuple[tuple[dict[str, JsonValue], ...], ...]:
        batches: list[tuple[dict[str, JsonValue], ...]] = []
        current: tuple[dict[str, JsonValue], ...] = ()
        for group in groups:
            proposed = (*current, group)
            if len(proposed) <= 256 and self._budget_ok(proposed):
                current = proposed
                continue
            if current:
                batches.append(current)
                current = (group,)
            else:
                batches.append((group,))
        if current:
            batches.append(current)
        return tuple(batches)

    def _group_map(self, groups: tuple[dict[str, JsonValue], ...]) -> dict[str, dict[str, JsonValue]]:
        result: dict[str, dict[str, JsonValue]] = {}
        for group in groups:
            group_id = group.get("group_id")
            if not isinstance(group_id, str):
                raise TypeError("stored conflict group has no group_id")
            result[group_id] = group
        return result

    def _expected(self, groups: tuple[dict[str, JsonValue], ...]) -> dict[str, tuple[set[str], set[str]]]:
        return {
            str(group["group_id"]): (set(_ids(group, "candidate_ids")), set(_ids(group, "allowed_unit_ids")))
            for group in groups
        }

    def _apply_batch_response(self, groups: tuple[dict[str, JsonValue], ...], request_id: str, raw: str) -> set[str]:
        parsed = validate_resolution_batch_response(raw, request_id, self._expected(groups))
        for group_id, result in parsed.accepted.items():
            current = next(group for group in self.pool.conflict_groups if group.get("group_id") == group_id)
            if current.get("decision") in {"select", "defer"}:
                continue
            self._save_group(
                group_id,
                {
                    "decision": result["decision"],
                    "status": "resolved" if result["decision"] == "select" else "deferred_conflict",
                    "selected_candidate_ids": result["selected_candidate_ids"],
                    "restricted_unit_ids": result.get("restricted_unit_ids", []),
                    "reason": result["reason"],
                },
                request_id,
            )
        return set(parsed.errors) | set(parsed.missing)

    def _finish_replayed_attempt(self, request_id: str, attempt: Any, response: Any) -> None:
        if attempt.state in {"succeeded", "failed"}:
            return
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

    def _replay_v2(self, eligible: dict[str, dict[str, JsonValue]]) -> None:
        for request in self.term_runner._requests():
            if request.stage != "resolution" or not set(request.item_ids).issubset(eligible):
                continue
            groups = tuple(eligible[group_id] for group_id in request.item_ids)
            for attempt in reversed(request.attempts):
                response = self.store.read_model_response("resolution", request.request_id, attempt.attempt_id)
                if response is None:
                    continue
                self._finish_replayed_attempt(request.request_id, attempt, response)
                if response.finish_reason == "length":
                    if len(groups) > 1:
                        middle = len(groups) // 2
                        self._replayed_splits.extend((groups[:middle], groups[middle:]))
                    else:
                        only = next(iter(groups))
                        group_id = str(only["group_id"])
                        current = next(
                            group for group in self.pool.conflict_groups if group.get("group_id") == group_id
                        )
                        if current.get("decision") not in {"select", "defer"}:
                            self._save_group(
                                group_id,
                                {
                                    "decision": "defer",
                                    "status": "deferred_conflict",
                                    "reason": "resolution_truncated",
                                },
                                request.request_id,
                            )
                else:
                    try:
                        self._apply_batch_response(groups, request.request_id, response.raw)
                    except ProtocolError:
                        pass
                break

    def _unknown_result(self, group_id: str) -> bool:
        for request in self.term_runner._requests():
            if request.stage != "resolution" or group_id not in request.item_ids:
                continue
            for attempt in request.attempts:
                if attempt.state not in {"sent", "unknown", "succeeded"}:
                    continue
                if self.store.read_model_response("resolution", request.request_id, attempt.attempt_id) is None:
                    return True
        return False

    def _write_batch_request(
        self, groups: tuple[dict[str, JsonValue], ...], request_id: str, payload: dict[str, Any]
    ) -> None:
        ids = tuple(str(group["group_id"]) for group in groups)
        hashes = {str(group["group_id"]): str(group["group_input_hash"]) for group in groups}
        self.store.write_request(
            RequestManifest(
                request_id=request_id,
                stage="resolution",
                owner_kind="resolution_group",
                owner_id=max(ids, key=self.term_runner._spent),
                item_ids=ids,
                input_hashes=hashes,
                wire_hash=wire_hash("resolution", payload, self.term_runner.output_tokens),
            )
        )

    async def _dispatch_v2(self, groups: tuple[dict[str, JsonValue], ...]) -> bool:
        groups = tuple(
            group
            for group in groups
            if self.term_runner._spent(str(group["group_id"])) < 3
            and next(
                current for current in self.pool.conflict_groups if current.get("group_id") == group["group_id"]
            ).get("decision")
            not in {"select", "defer"}
        )
        if not groups:
            return False
        if not self._budget_ok(groups):
            if len(groups) > 1:
                middle = len(groups) // 2
                left = await self._dispatch_v2(groups[:middle])
                right = await self._dispatch_v2(groups[middle:])
                return left or right
            only = next(iter(groups))
            reason = (
                "unplannable_output_budget"
                if self._minimal_response_tokens(groups) > self.term_runner.output_tokens
                else "unplannable_input_budget"
            )
            self._save_group(
                str(only["group_id"]),
                {"decision": "defer", "status": "deferred_conflict", "reason": reason},
            )
            return True
        request_id = f"rr-{uuid4().hex}"
        payload = self._batch_payload(groups, request_id)
        self._write_batch_request(groups, request_id, payload)
        try:
            response = await self.term_runner.runtime.invoke(
                "resolution",
                payload,
                {
                    "request_id": request_id,
                    "item_ids": tuple(str(group["group_id"]) for group in groups),
                    "estimated_tokens": count_tokens(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                    + self.term_runner.output_tokens,
                    "output_tokens": self.term_runner.output_tokens,
                },
            )
        except (RuntimePaused, TermBudgetPaused):
            raise
        except RequestError as error:
            if error.attempts == 0:
                for group in groups:
                    self._save_group(
                        str(group["group_id"]),
                        {
                            "decision": "defer",
                            "status": "deferred_conflict",
                            "reason": f"resolution_unplannable:{error}",
                        },
                    )
            return True
        if response.get("finish_reason") == "length":
            if len(groups) > 1:
                middle = len(groups) // 2
                await self._dispatch_v2(groups[:middle])
                await self._dispatch_v2(groups[middle:])
            else:
                only = next(iter(groups))
                self._save_group(
                    str(only["group_id"]),
                    {"decision": "defer", "status": "deferred_conflict", "reason": "resolution_truncated"},
                    request_id,
                )
            return True
        try:
            self._apply_batch_response(groups, request_id, response["raw"])
        except ProtocolError:
            pass
        return True

    async def _run_v2(self) -> ResolutionResult:
        eligible_groups = self._eligible_v2_groups()
        eligible = self._group_map(eligible_groups)
        self._replay_v2(eligible)
        paused = False
        try:
            for batch in self._replayed_splits:
                await self._dispatch_v2(batch)
        except (RuntimePaused, TermBudgetPaused):
            paused = True
        while True:
            pending: list[dict[str, JsonValue]] = []
            for group_id, original in eligible.items():
                current = next(group for group in self.pool.conflict_groups if group.get("group_id") == group_id)
                if current.get("decision") in {"select", "defer"}:
                    continue
                if self._unknown_result(group_id):
                    self._save_group(
                        group_id,
                        {
                            "decision": "defer",
                            "status": "deferred_conflict",
                            "reason": "resolution_unknown_result",
                        },
                    )
                elif self.term_runner._spent(group_id) >= 3:
                    self._save_group(
                        group_id,
                        {"decision": "defer", "status": "deferred_conflict", "reason": "resolution_exhausted"},
                    )
                else:
                    pending.append(original)
            if not pending:
                break
            if paused:
                break
            try:
                for batch in self._batches(tuple(pending)):
                    await self._dispatch_v2(batch)
            except (RuntimePaused, TermBudgetPaused):
                paused = True
                break
        selected = sum(group.get("decision") == "select" for group in self.pool.conflict_groups)
        deferred = sum(group.get("decision") == "defer" for group in self.pool.conflict_groups)
        pending_count = len(self.pool.conflict_groups) - selected - deferred
        return ResolutionResult(
            "paused" if paused or pending_count else "closed",
            selected,
            deferred,
            pending_count,
            self.term_runner._spent(actual=True),
        )

    def decisions(self) -> tuple[ResolutionDecision, ...]:
        if any(group.get("decision") not in {"select", "defer"} for group in self.pool.conflict_groups):
            raise ValueError("conflict resolution is not terminal")
        return tuple(
            ResolutionDecision(
                group_id=str(group["group_id"]),
                decision=cast(Literal["select", "defer"], group["decision"]),
                selected_candidate_ids=_ids(group, "selected_candidate_ids", missing_ok=True),
                restricted_unit_ids=_ids(group, "restricted_unit_ids", missing_ok=True),
                reason=str(group.get("reason", "")),
            )
            for group in self.pool.conflict_groups
        )


def _ids(group: dict[str, JsonValue], key: str, *, missing_ok: bool = False) -> tuple[str, ...]:
    value = group.get(key, [] if missing_ok else None)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"stored conflict group {key} must be a string array")
    return tuple(item for item in value if isinstance(item, str))
