"""Validate and commit atomic body retries as one checkpoint update."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from engine.agents.runtime import RuntimePaused
from engine.agents.workflow import _validate_saved_record
from engine.schemas.contracts import ItemRecord, ItemStatus, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.custody import epoch as saved_epoch
from engine.services.custody import retry_checks

if TYPE_CHECKING:
    from engine.services.journal import BodyJournal


def validate_units(journal: BodyJournal, unit_ids: Sequence[str], *, retry_unknown: bool = True) -> tuple[str, ...]:
    selected = tuple(unit_ids)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("explicit atomic retry requires unique Unit IDs")
    plan = journal.session.prepared.plan
    if not set(selected).issubset(plan.unit_members):
        raise IdentityMismatch("explicit atomic retry names an unknown Unit")
    authorized = {item for unit in selected for item in plan.unit_members[unit]}
    ambiguous = tuple(
        request
        for request in journal._requests.values()
        if request.stage in {"translate", "review"} and journal._ambiguous(request)
    )
    for request in ambiguous:
        if authorized.intersection(request.item_ids) and not set(request.item_ids).issubset(authorized):
            raise IdentityMismatch("explicit retry must include every Unit in an ambiguous shared review request")
    unknown_items = {item for request in ambiguous for item in request.item_ids}
    retryable: list[str] = []
    for unit_id in selected:
        item_ids = plan.unit_members[unit_id]
        unknown = bool(set(item_ids).intersection(unknown_items))
        if unknown and not retry_unknown:
            raise RuntimePaused("explicit retry must authorize an unknown review outcome")
        candidates = (
            journal._records[item_id]
            for item_id in item_ids
            if journal._records[item_id].status == ItemStatus.NEEDS_ATTENTION
            or unknown
            and journal._records[item_id].status in {ItemStatus.PENDING, ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
        )
        for record in candidates:
            saved_epoch(record, "translation_epoch")
            if record.target_projection is not None:
                journal._translation_proof(record)
            retryable.append(record.item_id)
    return tuple(retryable)


def retry_units(journal: BodyJournal, unit_ids: Sequence[str], *, retry_unknown: bool = True) -> tuple[str, ...]:
    selected = tuple(unit_ids)
    permitted = set(validate_units(journal, selected, retry_unknown=retry_unknown))
    changes: list[tuple[ItemRecord, ItemRecord]] = []
    rollback = _snapshot(journal)
    try:
        with journal.store.lock(), state.batch(journal.store.root):
            for unit_id in selected:
                item_ids = journal.session.prepared.plan.unit_members[unit_id]
                candidates = tuple(journal._records[item] for item in item_ids if item in permitted)
                if not candidates:
                    continue
                if any(record.target_projection is None for record in candidates):
                    journal._unlock_stage("translate", item_ids, retry_unknown=retry_unknown)
                if any(record.target_projection is not None for record in candidates):
                    journal._unlock_stage("review", item_ids, retry_unknown=retry_unknown)
                review_epoch = 1 + max(saved_epoch(journal._records[item], "review_epoch") for item in item_ids)
                translation_epoch = 1 + max(
                    saved_epoch(journal._records[item], "translation_epoch") for item in item_ids
                )
                for record in candidates:
                    reopened = (
                        _initial(journal, record.item_id).model_copy(
                            update={
                                "checks": {
                                    "translation_epoch": translation_epoch,
                                    **(
                                        {"retry_feedback": str(record.failure["message"])[:1200]}
                                        if record.failure and record.failure.get("message")
                                        else {}
                                    ),
                                }
                            }
                        )
                        if record.target_projection is None
                        else record.model_copy(
                            update={
                                "stage": "proofread",
                                "status": ItemStatus.LOCAL_VALID,
                                "checks": retry_checks(record, review_epoch),
                                "failure": None,
                                "next_action": "review",
                            }
                        )
                    )
                    _validate_saved_record(journal.session, reopened)
                    journal.store._base.atomic_write_bytes(
                        journal._result_path(record.item_id), canonical_json_bytes(reopened)
                    )
                    changes.append((record, reopened))
    except BaseException:
        _restore(journal, rollback)
        raise
    for prior, current in changes:
        journal._update_result(prior, current)
    return tuple(current.item_id for _, current in changes)


def _initial(journal: BodyJournal, item_id: str) -> ItemRecord:
    if journal._initial_records is None:
        records: dict[str, ItemRecord] = {}
        for request_id in journal.session.prepared.plan.batch_hashes:
            batch = journal.session._prepared_batches[request_id]
            items = batch.payload.get("items")
            if not isinstance(items, list):
                raise IdentityMismatch("initial batch has an invalid item payload")
            for value in items:
                if not isinstance(value, Mapping) or not isinstance(value.get("item_id"), str):
                    raise IdentityMismatch("initial batch has an invalid item payload")
                wire = cast(Mapping[str, Any], value)
                member_id = cast(str, wire["item_id"])
                raw_terms = wire.get("terms")
                terms = (
                    tuple(term for term in raw_terms if isinstance(term, Mapping))
                    if isinstance(raw_terms, list)
                    else ()
                )
                records[member_id] = ItemRecord(
                    item_id=member_id,
                    segment_id=member_id,
                    selected_term_ids=batch.manifest.term_ids_by_item[member_id],
                    term_applicability={str(term["term_id"]): term["role"] for term in terms},
                    terms_hash=batch.manifest.terms_hashes[member_id],
                    context_hash=batch.manifest.context_hashes[member_id],
                )
        journal._initial_records = records
    try:
        return journal._initial_records[item_id]
    except KeyError as error:
        raise IdentityMismatch("explicit retry member has no committed initial batch") from error


def _snapshot(journal: BodyJournal) -> tuple[Any, ...]:
    return (
        dict(journal._requests),
        journal._actual_attempts,
        journal._body_actual_attempts,
        journal._run_attempts,
        journal._body_attempts,
        set(journal._accounted_usage),
        journal._input_tokens,
        journal._output_tokens,
        dict(journal._ambiguous_cache),
    )


def _restore(journal: BodyJournal, saved: tuple[Any, ...]) -> None:
    (
        journal._requests,
        journal._actual_attempts,
        journal._body_actual_attempts,
        journal._run_attempts,
        journal._body_attempts,
        journal._accounted_usage,
        journal._input_tokens,
        journal._output_tokens,
        journal._ambiguous_cache,
    ) = saved
