"""Persisted provider-response proofs for body results."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from time import perf_counter_ns
from typing import Any

from engine.agents.protocol import review_applicability
from engine.agents.terms import validate_review_response
from engine.agents.workflow import _wire_items
from engine.item.members import pack_members
from engine.schemas.contracts import ItemRecord, ItemStatus, RequestManifest, canonical_hash
from engine.schemas.members import MemberBatch
from engine.services import frames
from engine.services.atomic import IdentityMismatch
from engine.services.custody import epoch, review_draft, review_feedback, text
from engine.services.working import BudgetCache

_TRANSLATION_MEMO_BYTES = 128 * 1024 * 1024


def validate_records(
    journal,
    records: Sequence[ItemRecord],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Validate a cold snapshot once per request while binding every item."""
    ordered = tuple(records)
    from engine.agents.workflow import _validate_saved_record

    translation_groups: dict[str, list[ItemRecord]] = defaultdict(list)
    review_groups: dict[str, list[ItemRecord]] = defaultdict(list)
    direct: list[ItemRecord] = []
    started = perf_counter_ns()
    for record in ordered:
        member = journal.session.index.members_by_id.get(record.item_id)
        if member is None or record.segment_id != record.item_id:
            raise IdentityMismatch("saved result is not owned by the ready member inventory")
        if record.status == ItemStatus.REVIEWED and any(
            key in record.checks for key in ("parts", "manual", "preserved_code")
        ):
            # Manual and composite proof functions already validate ownership,
            # source identity, result state and their complete evidence chain.
            # Running _validate_saved_record here would perform that work twice.
            direct.append(record)
            continue
        if record.status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}:
            if record.request_id is None:
                raise IdentityMismatch("saved draft lacks its durable translation request")
            translation_groups[record.request_id].append(record)
        elif record.status == ItemStatus.REVIEWED:
            review_id = record.checks.get("review_request_id")
            if not isinstance(review_id, str):
                raise IdentityMismatch("reviewed result lacks its durable review request")
            review_groups[review_id].append(record)
        else:
            _validate_saved_record(journal.session, record)
    metrics = journal._proof_metrics
    translation_memo: BudgetCache[str, Mapping[str, str]] = BudgetCache(_TRANSLATION_MEMO_BYTES)
    metrics["classification_ns"] += perf_counter_ns() - started

    total = len(ordered)
    completed = (
        total - sum(map(len, translation_groups.values())) - sum(map(len, review_groups.values())) - len(direct)
    )

    def report() -> None:
        if progress is not None:
            progress(
                {
                    "phase": "recovery",
                    "notice": f"恢复：已校验 {completed}/{total} 条正文断点。",
                    "completed_items": completed,
                    "required_items": total,
                }
            )

    def bound() -> None:
        nonlocal completed
        completed += 1
        if progress is not None and (completed % 250 == 0 or completed == total):
            report()

    if completed == total and total:
        report()

    started = perf_counter_ns()
    for request_id, grouped in translation_groups.items():
        request = journal._requests.get(request_id)
        if request is None or request.stage != "translate":
            raise IdentityMismatch("saved draft lacks its durable translation request")
        targets = _translation_targets(journal, request, translation_memo)
        for record in grouped:
            _bind_request_epoch(journal, request, record)
            _bind_translation(record, targets)
            metrics["item_bindings"] += 1
            bound()
    metrics["translation_ns"] += perf_counter_ns() - started

    started = perf_counter_ns()
    for request_id, grouped in review_groups.items():
        request = journal._requests.get(request_id)
        if request is None or request.stage != "review":
            raise IdentityMismatch("reviewed result lacks its durable review request")
        drafts, decisions = _review_evidence(journal, request, translation_memo)
        for record in grouped:
            _bind_review(record, drafts, decisions)
            metrics["item_bindings"] += 1
            bound()
    metrics["review_ns"] += perf_counter_ns() - started

    # Composite proofs recursively validate their children before accepting the
    # parent. Keeping them after provider/manual leaves establishes the proof
    # dependency order without persisting an unsigned "verified" authority.
    manual_ns = 0
    parts_ns = 0
    for record in direct:
        started = perf_counter_ns()
        journal._require_review_proof(record)
        elapsed = perf_counter_ns() - started
        if "manual" in record.checks:
            manual_ns += elapsed
        else:
            parts_ns += elapsed
        metrics["item_bindings"] += 1
        bound()
    metrics["manual_ns"] += manual_ns
    metrics["parts_ns"] += parts_ns


def _translation_targets(
    journal, request: RequestManifest, memo: MutableMapping[str, Mapping[str, str]]
) -> Mapping[str, str]:
    if request.stage != "translate":
        raise IdentityMismatch("translation proof names a non-translation request")
    cached = memo.get(request.request_id)
    if cached is not None:
        return cached
    # Even an immutable initial batch must rebind the succeeded attempt's
    # physical wire metadata to its canonical payload.
    journal._translation_batch(request)
    response = journal._response(request)
    if response is None:
        seeded = getattr(journal, "_seed_translation_targets", None)
        if seeded is None or (targets := seeded(request)) is None:
            raise IdentityMismatch("saved draft lacks its persisted translation response")
        memo[request.request_id] = targets
        journal._proof_metrics["translation_requests"] += 1
        return targets
    sources = journal._source_projections(request)
    parsed = journal._validate_translation_response(response.raw, request.request_id, sources)
    targets = {item_id: value["target"] for item_id, value in parsed.accepted.items()}
    journal._translations[request.request_id] = parsed.accepted
    memo[request.request_id] = targets
    journal._proof_metrics["translation_requests"] += 1
    return targets


def _review_evidence(
    journal,
    review: RequestManifest,
    translation_memo: MutableMapping[str, Mapping[str, str]],
) -> tuple[Mapping[str, str], Mapping[str, Mapping[str, Any]]]:
    drafts: dict[str, str] = {}
    started = perf_counter_ns()
    for item_id in review.item_ids:
        record = journal._records[item_id]
        translation = journal._requests.get(record.request_id or "")
        if translation is None or translation.stage != "translate" or item_id not in translation.item_ids:
            raise IdentityMismatch("reviewed result lacks its durable translation request")
        _bind_request_epoch(journal, translation, record)
        targets = _translation_targets(journal, translation, translation_memo)
        draft = targets.get(item_id)
        if draft is None:
            raise IdentityMismatch("saved draft was not accepted by its translation response")
        if review.target_hashes.get(item_id) != canonical_hash(draft):
            raise IdentityMismatch("review request does not prove the saved translation draft")
        drafts[item_id] = draft
    journal._proof_metrics["review_drafts_ns"] += perf_counter_ns() - started
    # Rebuilding a legacy review batch may need the frozen translation draft.
    # Seed the content-bound translation proof first so that path cannot parse
    # the same provider response a second time.
    started = perf_counter_ns()
    batch = journal._review_batch(review)
    journal._proof_metrics["review_batch_ns"] += perf_counter_ns() - started
    started = perf_counter_ns()
    wires = _wire_items(batch)
    expected = {
        item_id: {
            "base_revision": review.revisions[review.item_unit_ids[item_id][0]],
            "source_projection": journal.session.index.members_by_id[item_id].source_projection,
            **review_applicability(
                journal.session.index.members_by_id[item_id], journal.session.index, wires[item_id]
            ),
        }
        for item_id in review.item_ids
    }
    journal._proof_metrics["review_expected_ns"] += perf_counter_ns() - started
    response = journal._response(review)
    if response is None:
        raise IdentityMismatch("reviewed result lacks a succeeded persisted response")
    started = perf_counter_ns()
    parsed = validate_review_response(response.raw, review.request_id, expected)
    journal._proof_metrics["review_response_ns"] += perf_counter_ns() - started
    if any(value.get("term_suggestions") for value in parsed.accepted.values()):
        raise IdentityMismatch("review response changed its closed batch protocol")
    journal._proof_metrics["review_requests"] += 1
    return drafts, parsed.accepted


def _bind_request_epoch(journal, request: RequestManifest, record: ItemRecord) -> None:
    member = journal.session.index.members_by_id[record.item_id]
    if request.record_versions.get(member.unit_id) != epoch(record, "translation_epoch"):
        raise IdentityMismatch("saved result translation epoch differs from its request frame")


def _bind_translation(record: ItemRecord, targets: Mapping[str, str]) -> None:
    target = targets.get(record.item_id)
    if target is None:
        raise IdentityMismatch("saved draft was not accepted by its translation response")
    if record.target_projection != target or record.target_hash != canonical_hash(target):
        raise IdentityMismatch("saved draft differs from its persisted translation response")


def _bind_review(
    record: ItemRecord,
    drafts: Mapping[str, str],
    decisions: Mapping[str, Mapping[str, Any]],
) -> None:
    if record.stage != "reviewed" or record.failure is not None or record.next_action is not None:
        raise IdentityMismatch("reviewed result has an invalid terminal state")
    draft = drafts.get(record.item_id)
    decision = decisions.get(record.item_id)
    if draft is None or decision is None:
        raise IdentityMismatch("reviewed result is absent from its persisted responses")
    if (
        record.checks.get("decision") != decision["decision"]
        or record.checks.get("checks") != decision["checks"]
        or record.checks.get("issues") != decision["issues"]
    ):
        raise IdentityMismatch("reviewed result differs from its persisted review decision")
    expected_target = decision.get("target") if decision["decision"] == "replace" else draft
    if record.target_projection != expected_target or record.target_hash != canonical_hash(expected_target):
        raise IdentityMismatch("reviewed target differs from its persisted responses")


def rebuild_review(journal, request: RequestManifest) -> tuple[MemberBatch, dict[str, ItemRecord]]:
    frozen = request.model_copy(update={"attempts": ()})
    members = tuple(journal.session.index.members_by_id[item_id] for item_id in request.item_ids)
    targets = {
        item_id: journal._review_target(
            item_id,
            request.target_hashes[item_id],
            request.revisions[request.item_unit_ids[item_id][0]],
        )
        for item_id in request.item_ids
    }

    def pack(values: Mapping[str, ItemRecord]):
        restored = review_feedback(values, request.feedback_by_item)
        return (
            pack_members(
                "review",
                members,
                journal.session.prepared.glossary,
                journal.session.index,
                frames.limits(journal, request),
                targets=restored,
                revisions=request.revisions,
                record_versions=request.record_versions,
                plan_epochs=request.plan_epochs,
                feedback=request.feedback_by_item,
                tokenizer_model=text(journal.session.prepared.plan.translation_config, "model"),
                sparse=request.sparse,
            ),
            restored,
        )

    packed, restored = pack(targets)
    if len(packed.batches) == 1 and not packed.blocked and frozen != packed.batches[0].manifest:
        targets = {
            item_id: current
            if current.status in {ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}
            and current.target_hash == request.target_hashes[item_id]
            and epoch(current, "review_epoch") < request.revisions[request.item_unit_ids[item_id][0]]
            else review_draft(
                current,
                targets[item_id].target_projection,
                epoch(current, "review_epoch"),
            )
            if current.status == ItemStatus.REVIEWED
            and current.checks.get("review_request_id") == request.request_id
            and targets[item_id].target_projection is not None
            and epoch(current, "review_epoch") < request.revisions[request.item_unit_ids[item_id][0]]
            else targets[item_id]
            for item_id in request.item_ids
            for current in (journal._records[item_id],)
        }
        packed, restored = pack(targets)
    if len(packed.batches) != 1 or packed.blocked:
        raise IdentityMismatch(
            f"persisted review request is no longer a canonical fitting batch: {request.request_id}"
        )
    batch = packed.batches[0]
    if frozen != batch.manifest:
        raise IdentityMismatch(f"persisted review request differs from its saved target frame: {request.request_id}")
    return batch, restored


def translation_target(journal, record: ItemRecord) -> str:
    request = journal._requests.get(record.request_id or "")
    if request is None or request.stage != "translate" or record.item_id not in request.item_ids:
        raise IdentityMismatch("saved draft lacks its durable translation request")
    response = journal._response(request)
    if response is None:
        raise IdentityMismatch("saved draft lacks its persisted translation response")
    accepted = journal._translations.get(request.request_id)
    if accepted is None:
        accepted = journal._validate_translation_response(
            response.raw, request.request_id, journal._source_projections(request)
        ).accepted
        journal._translations[request.request_id] = accepted
    result = accepted.get(record.item_id)
    if result is None:
        raise IdentityMismatch("saved draft was not accepted by its translation response")
    return result["target"]


def translation_proof(journal, record: ItemRecord) -> None:
    target = journal._translation_target(record)
    if record.target_projection != target or record.target_hash != canonical_hash(target):
        raise IdentityMismatch("saved draft differs from its persisted translation response")


def review_proof(journal, record: ItemRecord) -> None:
    if "preserved_code" in record.checks:
        from engine.services.code import preserved_code_proof

        preserved_code_proof(journal.session, record)
        return
    if "parts" in record.checks:
        from engine.services.parts import verify_composite

        verify_composite(journal, record)
        return
    if "manual" in record.checks:
        from engine.services.manual import manual_proof

        manual_proof(journal.session, record)
        return
    if record.stage != "reviewed" or record.failure is not None or record.next_action is not None:
        raise IdentityMismatch("reviewed result has an invalid terminal state")
    translation = journal._requests.get(record.request_id or "")
    review_id = record.checks.get("review_request_id")
    review = journal._requests.get(review_id) if isinstance(review_id, str) else None
    if translation is None or translation.stage != "translate" or record.item_id not in translation.item_ids:
        raise IdentityMismatch("reviewed result lacks its durable translation request")
    if review is None or review.stage != "review" or record.item_id not in review.item_ids:
        raise IdentityMismatch("reviewed result lacks its durable review request")
    if journal._response(review) is None:
        raise IdentityMismatch("reviewed result lacks a succeeded persisted response")
    draft = journal._translation_target(record)
    if review.target_hashes.get(record.item_id) != canonical_hash(draft):
        raise IdentityMismatch("review request does not prove the saved translation draft")
    review_batch = journal._review_batch(review)
    wires = _wire_items(review_batch)
    expected = {
        item_id: {
            "base_revision": review.revisions[review.item_unit_ids[item_id][0]],
            "source_projection": journal.session.index.members_by_id[item_id].source_projection,
            **review_applicability(
                journal.session.index.members_by_id[item_id], journal.session.index, wires[item_id]
            ),
        }
        for item_id in review.item_ids
    }
    decisions = journal._reviews.get(review.request_id)
    if decisions is None:
        reviewed = journal._response(review)
        assert reviewed is not None
        parsed = validate_review_response(reviewed.raw, review.request_id, expected)
        if any(value.get("term_suggestions") for value in parsed.accepted.values()):
            raise IdentityMismatch("review response changed its closed batch protocol")
        decisions = parsed.accepted
        journal._reviews[review.request_id] = decisions
    decision = decisions.get(record.item_id)
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


__all__ = ["rebuild_review", "review_proof", "translation_proof", "translation_target", "validate_records"]
