"""Minimal atomic translate, proofread, and correction workflow."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from engine.agents.protocol import ProtocolError, validate_translation_response
from engine.agents.runtime import ATOMIC_PROMPT_VERSION, ModelRuntime, RequestError
from engine.agents.terms import validate_review_response
from engine.core.quality import find_degenerate_translation, find_untranslated_english_texts
from engine.item.inline import plain_text
from engine.item.members import MemberIndex, pack_members, validate_member_target
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_hash
from engine.schemas.members import MemberBatch, RequestMember
from engine.schemas.ready import AtomicPreparedInput
from engine.services.ready import ReadySession, limits_for

type SaveCallback = Callable[[ItemRecord], None | Awaitable[None]]


@dataclass(frozen=True)
class WorkflowResult:
    results: Mapping[str, ItemRecord]
    status: Literal["completed", "needs_attention"]
    issues: tuple[str, ...] = ()


async def run_workflow(
    prepared: AtomicPreparedInput,
    batch: MemberBatch,
    index: MemberIndex,
    runtime: ModelRuntime,
    *,
    session: ReadySession,
    save: SaveCallback,
    records: Mapping[str, ItemRecord] | None = None,
) -> WorkflowResult:
    """Run one ready translation batch through the historical three steps."""
    if session.verify() != prepared or session.index is not index:
        raise ValueError("workflow requires the physically verified ready session values")
    session.verify_batch(batch, initial=True)
    _guard_ready(prepared, batch, index, runtime)
    if records is not None and not set(records).issubset(batch.manifest.item_ids):
        raise ValueError("saved results contain members outside the supplied translation batch")
    current = dict(records or {})
    completed = {item.item_id for item in batch.items if _valid_saved(item, current.get(item.item_id), session, batch)}
    issues: list[str] = []
    translation_batches = ((batch, True),)
    if completed or any(_translation_epoch(record) for record in current.values()):
        replanned = pack_members(
            "translate",
            batch.items,
            prepared.glossary,
            index,
            _limits(prepared),
            record_versions={
                item.unit_id: max(
                    _translation_epoch(current.get(candidate.item_id))
                    for candidate in batch.items
                    if candidate.unit_id == item.unit_id
                )
                for item in batch.items
                if item.item_id not in completed
            },
            completed=completed,
            tokenizer_model=str(prepared.plan.translation_config["model"]),
        )
        if replanned.blocked:
            raise ValueError("saved translation checkpoint cannot replan the remaining ready members")
        translation_batches = tuple((value, False) for value in replanned.batches)
    for translation_batch, initial in translation_batches:
        _guard_common(prepared, translation_batch, index, runtime)
        current, translated_issues = await _translate_step(
            translation_batch,
            runtime,
            session=session,
            initial=initial,
            save=save,
            records=current,
        )
        issues.extend(translated_issues)
    pending = tuple(item for item in batch.items if current[item.item_id].status != ItemStatus.REVIEWED)
    reviewable = tuple(item for item in pending if _reviewable(item, current.get(item.item_id)))
    if reviewable:
        current, review_issues = await _proofread_step(
            prepared,
            reviewable,
            index,
            runtime,
            session=session,
            save=save,
            records=current,
            revisions={
                item.unit_id: max(
                    batch.manifest.revisions[item.unit_id],
                    _review_epoch(current[item.item_id]),
                )
                for item in reviewable
            },
        )
        issues.extend(review_issues)
    status = (
        "completed"
        if all(
            current.get(item.item_id) is not None and current[item.item_id].status == ItemStatus.REVIEWED
            for item in batch.items
        )
        else "needs_attention"
    )
    return WorkflowResult(dict(current), status, tuple(issues))


async def _translate_step(
    batch: MemberBatch,
    runtime: ModelRuntime,
    *,
    session: ReadySession,
    initial: bool = True,
    save: SaveCallback,
    records: Mapping[str, ItemRecord] | None = None,
) -> tuple[dict[str, ItemRecord], list[str]]:
    """Translate only members without an identity-valid saved candidate."""
    prepared = session.verify()
    _guard_common(prepared, batch, session.index, runtime)
    session.verify_batch(batch, initial=initial)
    current = dict(records or {})
    issues: list[str] = []
    requested = tuple(
        item
        for item in batch.items
        if current.get(item.item_id) is None
        or current[item.item_id].status == ItemStatus.PENDING
        or not _valid_saved(item, current.get(item.item_id), session, batch)
    )
    if not requested:
        return current, issues
    if requested != batch.items:
        raise ValueError("translation step requires a batch containing only pending members")
    try:
        response = await runtime.invoke(
            "translate",
            batch.payload,
            _manifest(batch),
            dispatch_guard=lambda: session.verify_batch(batch, initial=initial),
        )
        if response.get("finish_reason") in {"length", "max_tokens"}:
            raise ProtocolError("translation response was truncated")
        parsed = validate_translation_response(
            response["raw"], batch.manifest.request_id, {item.item_id for item in batch.items}
        )
        if parsed.unknown:
            raise ProtocolError(f"translation response contains unknown item IDs: {', '.join(parsed.unknown)}")
    except (KeyError, ProtocolError, RequestError, TypeError, ValueError) as error:
        parsed = None
        issues.append(f"translate:{batch.manifest.request_id}: {error}")

    wires = _wire_items(batch)
    for member in batch.items:
        error = None
        target = None
        if parsed is None:
            error = issues[-1]
        elif member.item_id in parsed.accepted:
            target = parsed.accepted[member.item_id]["target"]
            error = _target_error(member, target, wires[member.item_id])
        else:
            error = parsed.errors.get(member.item_id, "translation item missing")
        if error is not None:
            issues.append(f"translate:{member.item_id}: {error}")
            current[member.item_id] = _failed(member, current.get(member.item_id), "translate", error, batch)
            await _save(save, current[member.item_id])
            continue
        assert target is not None
        current[member.item_id] = ItemRecord(
            item_id=member.item_id,
            segment_id=member.item_id,
            selected_term_ids=batch.manifest.term_ids_by_item[member.item_id],
            term_applicability={
                str(term["term_id"]): term["role"]
                for term in wires[member.item_id]["terms"]
                if isinstance(term, Mapping)
            },
            terms_hash=batch.manifest.terms_hashes[member.item_id],
            context_hash=batch.manifest.context_hashes[member.item_id],
            stage="proofread",
            status=ItemStatus.LOCAL_VALID,
            target_projection=target,
            target_hash=canonical_hash(target),
            checks={
                "translation_frame": _frame(batch),
                **(
                    {"translation_epoch": _translation_epoch(current.get(member.item_id))}
                    if _translation_epoch(current.get(member.item_id))
                    else {}
                ),
            },
            request_id=batch.manifest.request_id,
            next_action="review",
        )
        await _save(save, current[member.item_id])
    return current, issues


async def _proofread_step(
    prepared: AtomicPreparedInput,
    members: Sequence[RequestMember],
    index: MemberIndex,
    runtime: ModelRuntime,
    *,
    session: ReadySession,
    save: SaveCallback,
    records: Mapping[str, ItemRecord],
    revisions: Mapping[str, int],
) -> tuple[dict[str, ItemRecord], list[str]]:
    """Repack review against the actual saved targets, then apply each decision."""
    current = dict(records)
    limits = _limits(prepared)
    result = pack_members(
        "review",
        members,
        prepared.glossary,
        index,
        limits,
        targets={item.item_id: current[item.item_id] for item in members},
        revisions={item.unit_id: revisions[item.unit_id] for item in members},
        tokenizer_model=str(prepared.plan.translation_config["model"]),
    )
    issues: list[str] = []
    for blocked in result.blocked:
        item_id, budget = blocked.item_id, blocked.budget
        issue = f"review:{item_id}: {'; '.join(budget.failures)}"
        issues.append(issue)
        member = index.members_by_id[item_id]
        current[item_id] = _failed(member, current[item_id], "review", issue, None)
        await _save(save, current[item_id])
    for review_batch in result.batches:
        session.verify_batch(review_batch)
        _guard_review(prepared, review_batch, index, runtime)
        try:
            response = await runtime.invoke(
                "review",
                review_batch.payload,
                _manifest(review_batch),
                dispatch_guard=lambda value=review_batch: session.verify_batch(value),
            )
            if response.get("finish_reason") in {"length", "max_tokens"}:
                raise ProtocolError("review response was truncated")
            expected = {
                item.item_id: {
                    "base_revision": review_batch.manifest.revisions[item.unit_id],
                    "terminology_applicable": any(
                        term.get("role") == "target" for term in _wire_items(review_batch)[item.item_id]["terms"]
                    ),
                    "bindings_applicable": bool(item.registry),
                }
                for item in review_batch.items
            }
            parsed = validate_review_response(response["raw"], review_batch.manifest.request_id, expected)
            if parsed.unknown:
                raise ProtocolError(f"review response contains unknown item IDs: {', '.join(parsed.unknown)}")
            if any(value.get("term_suggestions") for value in parsed.accepted.values()):
                raise ProtocolError("atomic review response cannot add terminology suggestions")
        except (KeyError, ProtocolError, RequestError, TypeError, ValueError) as error:
            parsed = None
            issues.append(f"review:{review_batch.manifest.request_id}: {error}")
        current, applied = await _apply_corrections_step(
            review_batch, parsed, current, save=save, batch_error=issues[-1] if parsed is None else None
        )
        issues.extend(applied)
    return current, issues


async def _apply_corrections_step(
    batch: MemberBatch,
    parsed: Any,
    records: Mapping[str, ItemRecord],
    *,
    save: SaveCallback,
    batch_error: str | None = None,
) -> tuple[dict[str, ItemRecord], list[str]]:
    """Apply one complete valid replacement, or retain the saved candidate as failed."""
    current = dict(records)
    issues: list[str] = []
    wires = _wire_items(batch)
    for member in batch.items:
        prior = current[member.item_id]
        if (
            prior.item_id != member.item_id
            or prior.segment_id != member.item_id
            or prior.status not in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
            or prior.target_hash != batch.manifest.target_hashes.get(member.item_id)
        ):
            raise ValueError("review result does not match the current saved target")
        result: dict[str, Any] | None = parsed.accepted.get(member.item_id) if parsed is not None else None
        error = batch_error
        if result is None and error is None:
            assert parsed is not None
            error = parsed.errors.get(member.item_id, "review item missing")
        if result is not None and result["decision"] == "needs_attention":
            error = str(result["issues"] or "review needs attention")
        if result is not None and result["decision"] == "replace":
            error = _target_error(member, result["target"], wires[member.item_id])
        if error is not None:
            issues.append(f"review:{member.item_id}: {error}")
            current[member.item_id] = _failed(member, prior, "review", error, None)
        else:
            assert result is not None
            target = result["target"] if result["decision"] == "replace" else prior.target_projection
            assert isinstance(target, str)
            current[member.item_id] = prior.model_copy(
                update={
                    "stage": "reviewed",
                    "status": ItemStatus.REVIEWED,
                    "target_projection": target,
                    "target_hash": canonical_hash(target),
                    "checks": {
                        **(
                            {"translation_frame": prior.checks["translation_frame"]}
                            if "translation_frame" in prior.checks
                            else {}
                        ),
                        **({"review_epoch": prior.checks["review_epoch"]} if "review_epoch" in prior.checks else {}),
                        **(
                            {"translation_epoch": prior.checks["translation_epoch"]}
                            if "translation_epoch" in prior.checks
                            else {}
                        ),
                        "decision": result["decision"],
                        "checks": result["checks"],
                        "issues": result["issues"],
                        "review_request_id": batch.manifest.request_id,
                    },
                    "failure": None,
                    "next_action": None,
                }
            )
        await _save(save, current[member.item_id])
    return current, issues


def _guard_ready(prepared: AtomicPreparedInput, batch: MemberBatch, index: MemberIndex, runtime: ModelRuntime) -> None:
    if batch.manifest.stage != "translate":
        raise ValueError("workflow entry requires a translation batch")
    _guard_common(prepared, batch, index, runtime)
    if prepared.plan.batch_hashes.get(batch.manifest.request_id) != canonical_hash(batch):
        raise ValueError("translation batch differs from the committed ready plan")


def _guard_review(
    prepared: AtomicPreparedInput, batch: MemberBatch, index: MemberIndex, runtime: ModelRuntime
) -> None:
    if batch.manifest.stage != "review":
        raise ValueError("proofread requires a review batch")
    _guard_common(prepared, batch, index, runtime)


def _guard_common(
    prepared: AtomicPreparedInput, batch: MemberBatch, index: MemberIndex, runtime: ModelRuntime
) -> None:
    plan = prepared.plan
    config = plan.translation_config
    if config.get("prompt_version") != ATOMIC_PROMPT_VERSION or config.get("input_budget_version") != 2:
        raise ValueError("ready plan does not use the atomic prompt and token budget")
    if runtime.input_budget_version != 2:
        raise ValueError("atomic workflow requires runtime input budget version 2")
    if runtime.model_id != config.get("model"):
        raise ValueError("runtime model differs from the frozen ready plan")
    if runtime.model_max_output_tokens is None or runtime.model_max_output_tokens < _integer(
        config, "max_output_tokens"
    ):
        raise ValueError("runtime output capacity is below the frozen ready plan")
    if index.source_hash != plan.source_hash or prepared.glossary.freeze_id != plan.freeze_id:
        raise ValueError("workflow source or glossary differs from the ready plan")
    if batch.manifest.freeze_id != plan.freeze_id or batch.manifest.glossary_file_sha256 != canonical_hash(
        prepared.glossary
    ):
        raise ValueError("request glossary differs from the ready plan")
    limits = _limits(prepared)
    identity = batch.budget.identity
    if (
        identity.version != limits.output_version
        or identity.source_limit != limits.source_tokens
        or identity.input_limit != min(limits.input_tokens, 50_000)
        or identity.output_limit != limits.output_tokens
        or identity.context_limit != limits.context_tokens
        or identity.safety_tokens != limits.safety_tokens
        or identity.target_ratio != limits.target_ratio
    ):
        raise ValueError("request budget differs from the frozen ready plan")
    for member in batch.items:
        if index.members_by_id.get(member.item_id) != member:
            raise ValueError("request member is not owned by the verified member index")
        if plan.member_hashes.get(member.item_id) != canonical_hash(member):
            raise ValueError("request member differs from the committed ready plan")
        if member.item_id not in plan.unit_members.get(member.unit_id, ()):
            raise ValueError("request member owner differs from the committed ready plan")


def _limits(prepared: AtomicPreparedInput):
    return limits_for(prepared.preparation)


def _manifest(batch: MemberBatch) -> dict[str, Any]:
    return batch.manifest.model_dump(mode="python") | {"output_tokens": batch.budget.output_tokens}


def _wire_items(batch: MemberBatch) -> dict[str, dict[str, Any]]:
    values = batch.payload.get("items")
    if not isinstance(values, list):
        raise TypeError("member payload items are missing")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("item_id"), str):
            raise TypeError("member payload item is invalid")
        item_id = cast(str, value["item_id"])
        result[item_id] = cast(dict[str, Any], value)
    return result


def _integer(config: Mapping[str, Any], name: str, default: int | None = None) -> int:
    value = config.get(name, default)
    if type(value) is not int or value < 0:
        raise ValueError(f"frozen {name} must be a non-negative integer")
    return value


def _review_epoch(record: ItemRecord) -> int:
    value = record.checks.get("review_epoch", 0)
    if type(value) is not int or value < 0:
        raise ValueError("saved review epoch must be a non-negative integer")
    return value


def _translation_epoch(record: ItemRecord | None) -> int:
    value = record.checks.get("translation_epoch", 0) if record is not None else 0
    if type(value) is not int or value < 0:
        raise ValueError("saved translation epoch must be a non-negative integer")
    return value


def _valid_saved(
    member: RequestMember,
    record: ItemRecord | None,
    session: ReadySession,
    pending_batch: MemberBatch,
) -> bool:
    if record is None:
        return False
    if record.item_id != member.item_id or record.segment_id != member.item_id:
        raise ValueError("saved result identity differs from its request member")
    if record.status == ItemStatus.PENDING and record.target_projection is None:
        _validate_record_frame(member, record, pending_batch)
        return False
    if record.status == ItemStatus.NEEDS_ATTENTION:
        batch = _saved_batch(session, member, record)
        if record.target_projection is not None:
            if record.target_hash != canonical_hash(record.target_projection):
                raise ValueError("saved attention result is not hash-bound")
            validate_member_target(member, record.target_projection)
            if error := _target_error(member, record.target_projection, _wire_items(batch)[member.item_id]):
                raise ValueError(f"saved attention result no longer passes local validation: {error}")
        return True
    if record.status not in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE, ItemStatus.REVIEWED}:
        raise ValueError(f"saved result has no reusable workflow state: {member.item_id}/{record.status}")
    batch = _saved_batch(session, member, record)
    target = record.target_projection
    if target is None or record.target_hash != canonical_hash(target):
        raise ValueError("saved result is not hash-bound")
    validate_member_target(member, target)
    wire = _wire_items(batch)[member.item_id]
    if error := _target_error(member, target, wire):
        raise ValueError(f"saved result no longer passes local validation: {error}")
    return True


def _saved_batch(session: ReadySession, member: RequestMember, record: ItemRecord) -> MemberBatch:
    frame = record.checks.get("translation_frame")
    if not isinstance(frame, dict) or set(frame) != {"request_id", "member_ids", "batch_hash"}:
        raise ValueError("saved result lacks a canonical translation frame")
    member_ids = frame.get("member_ids")
    if not isinstance(member_ids, list) or any(not isinstance(value, str) for value in member_ids):
        raise ValueError("saved translation frame has invalid members")
    member_ids = cast(list[str], member_ids)
    if member.item_id not in member_ids or len(member_ids) != len(set(member_ids)):
        raise ValueError("saved translation frame does not own its member")
    try:
        members = tuple(session.index.members_by_id[value] for value in member_ids)
    except KeyError as error:
        raise ValueError("saved translation frame references an unknown member") from error
    request_id = frame.get("request_id")
    if not isinstance(request_id, str):
        raise TypeError("saved translation frame has an invalid request ID")
    request_path = session.store.root / "requests" / f"{request_id}.json"
    request = session.store.read_request(request_id) if request_path.exists() else None
    if request is not None and request.record_versions.get(member.unit_id) != _translation_epoch(record):
        raise ValueError("saved result translation epoch differs from its request frame")
    versions: dict[str, int] = {}
    for value, saved_member in zip(member_ids, members, strict=True):
        saved = (
            record
            if value == record.item_id
            else ItemRecord.model_validate_json((session.store.root / "results" / f"{value}.json").read_bytes())
        )
        versions[saved_member.unit_id] = max(versions.get(saved_member.unit_id, 0), _translation_epoch(saved))
    packed = pack_members(
        "translate",
        members,
        session.prepared.glossary,
        session.index,
        _limits(session.prepared),
        record_versions=request.record_versions if request is not None else versions,
        plan_epochs=request.plan_epochs if request is not None else {member.unit_id: 0 for member in members},
        tokenizer_model=str(session.prepared.plan.translation_config["model"]),
    )
    if len(packed.batches) != 1 or packed.blocked:
        raise ValueError("saved translation frame is not a canonical fitting batch")
    batch = packed.batches[0]
    if (
        tuple(member_ids) != batch.manifest.item_ids
        or frame.get("request_id") != batch.manifest.request_id
        or frame.get("batch_hash") != canonical_hash(batch)
        or record.request_id != batch.manifest.request_id
        or request is not None
        and request.model_copy(update={"attempts": ()}) != batch.manifest
    ):
        raise ValueError("saved translation frame changed")
    session.verify_batch(batch)
    _validate_record_frame(member, record, batch)
    return batch


def _frame(batch: MemberBatch) -> dict[str, Any]:
    return {
        "request_id": batch.manifest.request_id,
        "member_ids": list(batch.manifest.item_ids),
        "batch_hash": canonical_hash(batch),
    }


def _validate_record_frame(member: RequestMember, record: ItemRecord, batch: MemberBatch) -> None:
    wire = _wire_items(batch)[member.item_id]
    expected_roles = {str(term["term_id"]): term["role"] for term in wire["terms"] if isinstance(term, Mapping)}
    if (
        record.selected_term_ids != batch.manifest.term_ids_by_item[member.item_id]
        or record.term_applicability != expected_roles
        or record.terms_hash != batch.manifest.terms_hashes[member.item_id]
        or record.context_hash != batch.manifest.context_hashes[member.item_id]
    ):
        raise ValueError("saved result terminology or context identity changed")


def _reviewable(member: RequestMember, record: ItemRecord | None) -> bool:
    if record is None or record.status not in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}:
        return False
    target = record.target_projection
    if target is None or record.target_hash != canonical_hash(target):
        return False
    try:
        validate_member_target(member, target)
    except ValueError:
        return False
    return True


def _target_error(member: RequestMember, target: str, wire: Mapping[str, Any]) -> str | None:
    try:
        validate_member_target(member, target)
    except ValueError as error:
        return str(error)
    source_text, target_text = plain_text(member.source_projection), plain_text(target)
    if degeneration := find_degenerate_translation(source_text, target_text):
        return degeneration
    script_text = target_text
    for term in wire.get("terms", ()):
        if isinstance(term, Mapping) and term.get("role") == "target":
            spelling = _source_variant(term, source_text) if term.get("mode") == "keep_source" else term.get("target")
            if isinstance(spelling, str):
                flags = re.IGNORECASE if term.get("match_policy") == "casefold" else 0
                script_text = re.sub(re.escape(spelling), "", script_text, flags=flags)
    if residual := find_untranslated_english_texts(script_text):
        return f"untranslated English remains: {residual[0]}"
    for term in wire.get("terms", ()):
        if not isinstance(term, Mapping) or term.get("role") != "target":
            continue
        required = _required_term(term, source_text)
        if required is not None and not _contains(target_text, required, term.get("match_policy") == "casefold"):
            return f"required term is missing: {required}"
    return None


def _required_term(term: Mapping[str, Any], source: str) -> str | None:
    mode = term.get("mode")
    if mode == "required":
        target = term.get("target")
        return target if _source_variant(term, source) is not None and isinstance(target, str) else None
    if mode != "keep_source":
        return None

    return _source_variant(term, source)


def _source_variant(term: Mapping[str, Any], source: str) -> str | None:
    values = (term.get("source"), *(term.get("aliases") or ()))
    folded = term.get("match_policy") == "casefold"
    for value in values:
        if not isinstance(value, str):
            continue
        haystack, needle = (source.casefold(), value.casefold()) if folded else (source, value)
        if (position := haystack.find(needle)) >= 0:
            return source[position : position + len(value)]
    return None


def _contains(value: str, part: str, folded: bool) -> bool:
    return part.casefold() in value.casefold() if folded else part in value


def _failed(
    member: RequestMember,
    prior: ItemRecord | None,
    stage: str,
    error: str,
    batch: MemberBatch | None,
) -> ItemRecord:
    translation_epoch = _translation_epoch(prior)
    if prior is None or prior.status == ItemStatus.PENDING:
        if batch is None:
            raise ValueError("new failed records require their request identity")
        wire = _wire_items(batch)[member.item_id]
        terms = tuple(term for term in wire["terms"] if isinstance(term, Mapping))
        prior = ItemRecord(
            item_id=member.item_id,
            segment_id=member.item_id,
            selected_term_ids=batch.manifest.term_ids_by_item[member.item_id],
            term_applicability={str(term["term_id"]): term["role"] for term in terms},
            terms_hash=batch.manifest.terms_hashes[member.item_id],
            context_hash=batch.manifest.context_hashes[member.item_id],
            checks={
                "translation_frame": _frame(batch),
                **({"translation_epoch": translation_epoch} if translation_epoch else {}),
            },
            request_id=batch.manifest.request_id,
        )
    base = prior
    return base.model_copy(
        update={
            "stage": stage,
            "status": ItemStatus.NEEDS_ATTENTION,
            "checks": dict(base.checks) | {"accepted": False, "error": error},
            "failure": {"stage": stage, "code": f"{stage}_failed", "message": error},
            "next_action": None,
        }
    )


async def _save(callback: SaveCallback, record: ItemRecord) -> None:
    result = callback(record)
    if inspect.isawaitable(result):
        await result


def _validate_saved_record(session: ReadySession, record: ItemRecord) -> None:
    """Validate the canonical source frame carried by one durable body result."""
    member = session.index.members_by_id.get(record.item_id)
    if member is None or record.segment_id != record.item_id:
        raise ValueError("saved result is not owned by the ready member inventory")
    if record.status != ItemStatus.PENDING:
        _saved_batch(session, member, record)


__all__ = [
    "WorkflowResult",
    "run_workflow",
]
