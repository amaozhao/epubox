"""One bounded semantic check per source-evidenced terminology conflict."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import uuid4

from engine.agents.protocol import ProtocolError
from engine.agents.runtime import RequestError, RuntimePaused, wire_hash
from engine.agents.term_protocol import validate_resolution_response
from engine.core.tokens import count_tokens
from engine.schemas.contracts import JsonValue, RequestManifest
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
        self.pool = store.read_candidate_pool()
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
            and any(attempt.state != "reserved" for attempt in request.attempts)
            for request in self.term_runner._requests()
        )

    def _payload(self, group: dict[str, JsonValue], request_id: str) -> dict[str, Any]:
        candidate_ids = _ids(group, "candidate_ids")
        allowed = _ids(group, "allowed_unit_ids")
        candidates = {candidate.candidate_id: candidate for candidate in self.pool.candidates}
        return {
            "protocol": "epubox-term-resolution-1",
            "request_id": request_id,
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

    async def run(self) -> ResolutionResult:
        paused = False
        groups = sorted(self.pool.conflict_groups, key=lambda group: str(group.get("group_id")))
        for index, group in enumerate(groups):
            group_id = group.get("group_id")
            if not isinstance(group_id, str):
                raise TypeError("stored conflict group has no group_id")
            current = next(item for item in self.pool.conflict_groups if item.get("group_id") == group_id)
            if current.get("decision") in {"select", "defer"}:
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
            except (ProtocolError, RequestError, ValueError) as error:
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
