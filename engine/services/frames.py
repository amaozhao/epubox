"""Compact restart cache for verified body request frames."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import Field

from engine.schemas.budget import BudgetResult
from engine.schemas.contracts import (
    FrozenModel,
    ItemStatus,
    JsonValue,
    RequestManifest,
    canonical_hash,
    canonical_json_bytes,
)
from engine.schemas.members import MemberBatch
from engine.services import state
from engine.services.atomic import CorruptRecord, IdentityMismatch, safe_id

if TYPE_CHECKING:
    from engine.services.journal import BodyJournal

FORMAT = "epubox-frame-1"


class Frame(FrozenModel):
    format: Literal["epubox-frame-1"] = FORMAT
    request_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, JsonValue]
    budget: BudgetResult


def restore(journal: BodyJournal, request: RequestManifest) -> MemberBatch | None:
    if not state.compact(journal.store.root):
        return None
    path = _path(journal, request.request_id)
    if not state.exists(path):
        return None
    try:
        frame = Frame.model_validate_json(state.read(path))
        frozen = request.model_copy(update={"attempts": ()})
        if frame.request_id != request.request_id or frame.manifest_hash != canonical_hash(frozen):
            raise IdentityMismatch("saved request frame differs from its frozen manifest")
        batch = MemberBatch(
            manifest=frozen,
            items=tuple(journal.session.index.members_by_id[item_id] for item_id in request.item_ids),
            context=_context(frame.payload),
            payload=frame.payload,
            budget=frame.budget,
        )
        if canonical_hash(batch) != frame.batch_hash:
            raise IdentityMismatch("saved request frame changed")
        journal.session.verify_batch(batch)
        return batch
    except (IdentityMismatch, KeyError):
        raise
    except Exception as error:
        raise CorruptRecord(f"invalid saved request frame {request.request_id}: {error}") from error


def remember(journal: BodyJournal, batch: MemberBatch) -> None:
    if not state.compact(journal.store.root) or batch.manifest.request_id in journal.session._prepared_batches:
        return
    frozen = batch.manifest.model_copy(update={"attempts": ()})
    journal._pending_frames[batch.manifest.request_id] = Frame(
        request_id=batch.manifest.request_id,
        manifest_hash=canonical_hash(frozen),
        batch_hash=canonical_hash(batch.model_copy(update={"manifest": frozen})),
        payload=batch.payload,
        budget=batch.budget,
    )


def capture(
    journal: BodyJournal,
    manifest: RequestManifest,
    payload: dict[str, JsonValue],
    budget: BudgetResult,
) -> None:
    batch = MemberBatch(
        manifest=manifest,
        items=tuple(journal.session.index.members_by_id[item_id] for item_id in manifest.item_ids),
        context=_context(payload),
        payload=payload,
        budget=budget,
    )
    remember(journal, batch)


def warm(journal: BodyJournal, progress: Callable[[dict[str, Any]], None] | None = None) -> None:
    seen: set[str] = set()
    rebuilt = 0
    for record in journal._records.values():
        frame = record.checks.get("translation_frame")
        if not isinstance(frame, dict):
            continue
        request_id = frame.get("request_id")
        member_ids = frame.get("member_ids")
        batch_hash = frame.get("batch_hash")
        if (
            not isinstance(request_id, str)
            or request_id in seen
            or not isinstance(member_ids, list)
            or any(not isinstance(item_id, str) for item_id in member_ids)
            or not isinstance(batch_hash, str)
        ):
            continue
        request = journal._requests.get(request_id)
        if request is None or request.stage != "translate":
            continue
        pending = len(journal._pending_frames)
        batch = journal._translation_batch(request)
        if tuple(member_ids) != batch.manifest.item_ids or batch_hash != canonical_hash(batch):
            raise IdentityMismatch("saved translation frame changed")
        stamp = state.stat(journal.store._path("requests", request_id)).st_mtime_ns
        journal.session._saved_batches[(request_id, batch_hash, tuple(member_ids), stamp)] = batch
        seen.add(request_id)
        if len(journal._pending_frames) > pending:
            rebuilt += 1
            if progress is not None and rebuilt % 25 == 0:
                progress({"phase": "recovery", "notice": f"恢复：已迁移 {rebuilt} 个旧正文请求断点。"})
    if progress is not None and rebuilt % 25:
        progress({"phase": "recovery", "notice": f"恢复：已迁移 {rebuilt} 个旧正文请求断点。"})
    active: set[str] = set()
    for request in journal._requests.values():
        if request.stage == "translate" and _active_translation(journal, request):
            journal._translation_batch(request)
            active.add(request.request_id)
        elif request.stage == "review" and _active_review(journal, request):
            journal._review_batch(request)
            active.add(request.request_id)
    journal._active_frames = active  # type: ignore[attr-defined]


def flush(journal: BodyJournal, *, include_active: bool = True) -> None:
    if not journal._pending_frames:
        return
    active = getattr(journal, "_active_frames", set())
    pending = tuple(
        frame for request_id, frame in journal._pending_frames.items() if include_active or request_id not in active
    )
    if not pending:
        return
    with journal.store.lock(), state.batch(journal.store.root):
        for frame in pending:
            path = _path(journal, frame.request_id)
            if state.exists(path):
                if Frame.model_validate_json(state.read(path)) != frame:
                    raise IdentityMismatch("saved request frame changed")
                continue
            state.write(path, canonical_json_bytes(frame))
    for frame in pending:
        journal._pending_frames.pop(frame.request_id)
    if include_active:
        active.clear()


def _path(journal: BodyJournal, request_id: str):
    return journal.store.root / "frames" / f"{safe_id(request_id)}.json"


def _context(payload: dict[str, JsonValue]) -> tuple[str, ...]:
    value = payload.get("context")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CorruptRecord("saved request frame has invalid context")
    return cast(tuple[str, ...], tuple(value))


def _active_translation(journal: BodyJournal, request: RequestManifest) -> bool:
    return any(
        journal._records[item_id].status == ItemStatus.PENDING
        and journal._records[item_id].checks.get("translation_epoch", 0)
        == request.record_versions[request.item_unit_ids[item_id][0]]
        for item_id in request.item_ids
    )


def _active_review(journal: BodyJournal, request: RequestManifest) -> bool:
    return any(
        journal._records[item_id].status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
        and journal._records[item_id].checks.get("review_epoch", 0)
        == request.revisions[request.item_unit_ids[item_id][0]]
        and journal._records[item_id].target_hash == request.target_hashes[item_id]
        for item_id in request.item_ids
    )


def limits(journal: BodyJournal, request: RequestManifest):
    from engine.services.ready import limits_from_config

    return limits_from_config(
        journal.session.prepared.plan.translation_config,
        context_unlimited=request.context_unlimited,
        output_unlimited=request.output_unlimited,
        source_hard_limit=request.source_hard_limit,
    )


__all__ = ["capture", "flush", "limits", "remember", "restore", "warm"]
