from __future__ import annotations

import pytest

from engine.agents.workflow import _failed
from engine.schemas.contracts import ItemStatus, canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal
from engine.services.manual import import_codex_translation
from tests.engine.agents import test_workflow as fixtures


def test_legacy_code_recovery_preserves_completed_prose_and_survives_restart(tmp_path, monkeypatch):
    import engine.epub.preparation as epub_preparation
    from engine.services import preparation

    monkeypatch.setattr(preparation, "ATOMIC_EXTRACTOR_VERSION", "epubox-atomic-3")
    monkeypatch.setattr(epub_preparation, "ATOMIC_EXTRACTOR_VERSION", "epubox-atomic-3")
    from functools import partial

    monkeypatch.setattr(
        epub_preparation,
        "extract_resource",
        partial(epub_preparation.extract_resource, extractor_version="epubox-atomic-3"),
    )
    monkeypatch.setattr(fixtures, "EXTRACTOR_VERSION", "epubox-atomic-3")
    lines = ('const index = pc.index("document-search");', "const index = pc.index('document-search');")
    case = fixtures.prepare_case(
        tmp_path,
        '<p>Keep data safe.</p><div class="ProgramCode"><div class="LineGroup">'
        + "".join(f'<div class="FixedLine">{line}</div>' for line in lines)
        + "</div></div>",
        ("Keep data safe.", *lines),
    )
    journal = BodyJournal(case.session.store, case.session)
    prose, *code = case.batch.items
    done = import_codex_translation(
        journal, prose, journal.records()[prose.item_id], "确保数据安全。", user_authorized=True
    )
    before = state.read(journal._result_path(prose.item_id))
    for member in code:
        journal.save(
            _failed(
                member,
                journal.records()[member.item_id],
                "translate",
                f"untranslated English remains: {member.source_projection}",
                case.batch,
            )
        )

    recovered = journal.recover_results()
    assert recovered[prose.item_id] == done
    assert state.read(journal._result_path(prose.item_id)) == before
    for member in code:
        record = recovered[member.item_id]
        assert record.status == ItemStatus.REVIEWED
        assert record.target_projection == member.source_projection
        assert "preserved_code" in record.checks
    assert all(not request.attempts for request in journal._requests.values())
    assert BodyJournal(case.session.store).recover_results() == recovered

    member = code[0]
    changed = recovered[member.item_id].model_copy(
        update={
            "target_projection": "const index = pc.index('changed');",
            "target_hash": canonical_hash("const index = pc.index('changed');"),
        }
    )
    state.write(journal._result_path(member.item_id), canonical_json_bytes(changed))
    with pytest.raises(IdentityMismatch, match="preserved code"):
        BodyJournal(case.session.store)


@pytest.mark.parametrize(
    "body",
    (
        "<p>Index of documents</p>",
        '<div class="ProgramCode"><div class="Caption">Index of documents</div></div>',
    ),
)
def test_plain_prose_cannot_claim_code_preservation(tmp_path, body):
    case = fixtures.prepare_case(tmp_path, body, ("Index of documents",))
    journal = BodyJournal(case.session.store, case.session)
    member = case.batch.items[0]
    failed = _failed(
        member,
        journal.records()[member.item_id],
        "translate",
        "untranslated English remains: Index of documents",
        case.batch,
    )
    journal.save(failed)
    assert journal.recover_results()[member.item_id] == failed

    forged = failed.model_copy(
        update={
            "stage": "reviewed",
            "status": ItemStatus.REVIEWED,
            "target_projection": member.source_projection,
            "target_hash": canonical_hash(member.source_projection),
            "failure": None,
            "next_action": None,
            "checks": {
                "translation_frame": failed.checks["translation_frame"],
                "preserved_code": {"version": 1, "member_hash": canonical_hash(member)},
            },
        }
    )
    state.write(journal._result_path(member.item_id), canonical_json_bytes(forged))
    with pytest.raises(IdentityMismatch):
        BodyJournal(case.session.store)
