"""Keep oversized frozen members out of new model requests."""

from engine.agents.workflow import _failed, _translation_epoch
from engine.item.budget import request_source_tokens
from engine.item.members import pack_members
from engine.schemas.contracts import ItemStatus
from engine.services import frames
from engine.services.ready import limits_from_config, request_source_limit


def mark_oversized(journal) -> None:
    session = journal.session
    model = str(session.prepared.plan.translation_config["model"])
    ceiling = limits_from_config(
        session.prepared.plan.translation_config,
        source_hard_limit=request_source_limit(session.prepared.plan.translation_config),
    ).source_ceiling
    changes = []
    for item_id, record in journal.records().items():
        if record.status not in {ItemStatus.PENDING, ItemStatus.LOCAL_VALID, ItemStatus.CANDIDATE}:
            continue
        member = session.index.members_by_id[item_id]
        if member.unit_id in session.prepared.plan.derived_sources:
            continue
        if session.index.source_tokens[item_id] <= ceiling:
            continue
        measured = request_source_tokens({"items": [{"item_id": item_id, "source": member.source_projection}]}, model)
        if measured <= ceiling:
            continue
        batch = None
        if record.status == ItemStatus.PENDING:
            # Preserve the old source frame as local evidence; it must never be dispatched.
            packed = pack_members(
                "translate",
                (member,),
                session.prepared.glossary,
                session.index,
                limits_from_config(
                    session.prepared.plan.translation_config,
                    context_unlimited=True,
                    output_unlimited=True,
                    source_hard_limit=None,
                ),
                record_versions={member.unit_id: _translation_epoch(record)},
                tokenizer_model=model,
                sparse=True,
                whole=True,
            )
            if packed.blocked or len(packed.batches) != 1:
                raise ValueError(f"cannot preserve the source identity for oversized member {item_id}")
            batch = packed.batches[0]
            if batch.manifest.request_id not in journal._requests:
                journal._requests[batch.manifest.request_id] = journal.store.write_request(batch.manifest)
            frames.remember(journal, batch)
        stage = "review" if record.target_projection is not None else "translate"
        changes.append(
            _failed(
                member,
                record,
                stage,
                f"source budget {measured} exceeds hard limit {ceiling}; saved member requires attention",
                batch,
            )
        )
    if changes:
        journal.save_many(changes)
