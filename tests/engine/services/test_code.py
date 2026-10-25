from __future__ import annotations

import zipfile
from functools import partial

import pytest

from engine.agents.workflow import _failed
from engine.schemas.contracts import ItemStatus, canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.code import _is_source_code
from engine.services.journal import BodyJournal
from engine.services.manual import import_codex_translation
from tests.engine.agents import test_workflow as fixtures

_ATOMIC4 = "epubox-atomic-4"
_RUST = "⟦=x1⟧⟦+g1⟧fn⟦-g1⟧ main() {\n        ⟦+g2⟧let⟦-g2⟧ index = values[index / 2];\n    }"


def _atomic4_rust_case(tmp_path, monkeypatch, *, body=None, projections=None):
    import engine.epub.preparation as epub_preparation
    from engine.services import preparation

    monkeypatch.setattr(preparation, "ATOMIC_EXTRACTOR_VERSION", _ATOMIC4)
    monkeypatch.setattr(epub_preparation, "ATOMIC_EXTRACTOR_VERSION", _ATOMIC4)
    monkeypatch.setattr(
        epub_preparation,
        "extract_resource",
        partial(epub_preparation.extract_resource, extractor_version=_ATOMIC4),
    )
    monkeypatch.setattr(fixtures, "EXTRACTOR_VERSION", _ATOMIC4)
    source = body or (
        '<p>Keep data safe.</p><p style="white-space: pre-wrap; '
        'font-family: Menlo, Consolas, monospace">    '
        '<span class="syntax-keyword">fn</span> main() {\n        '
        '<span class="syntax-keyword">let</span> index = values[index / 2];\n    }</p>'
    )
    return fixtures.prepare_case(tmp_path, source, projections or ("Keep data safe.", _RUST))


@pytest.mark.parametrize("failure_kind", ("translate-english", "review-english", "request-id-mismatch"))
def test_atomic4_styled_rust_code_is_recovered_from_frozen_source_without_http_or_fake_review(
    tmp_path, monkeypatch, failure_kind
):
    case = _atomic4_rust_case(tmp_path, monkeypatch)
    journal = BodyJournal(case.session.store, case.session)
    prose, code = case.batch.items
    completed = import_codex_translation(
        journal, prose, journal.records()[prose.item_id], "确保数据安全。", user_authorized=True
    )
    prose_before = state.read(journal._result_path(prose.item_id))
    prior = journal.records()[code.item_id]
    if failure_kind == "review-english":
        prior = prior.model_copy(
            update={
                "stage": "review",
                "status": ItemStatus.NEEDS_ATTENTION,
                "target_projection": "错误的审核替换。",
                "target_hash": canonical_hash("错误的审核替换。"),
                "checks": {
                    "translation_frame": {
                        "request_id": case.batch.manifest.request_id,
                        "member_ids": list(case.batch.manifest.item_ids),
                        "batch_hash": canonical_hash(case.batch),
                    },
                    "accepted": False,
                    "error": "untranslated English remains: fn main",
                    "review_request_id": "tx-old-review",
                },
                "request_id": case.batch.manifest.request_id,
                "failure": {
                    "stage": "review",
                    "code": "review_failed",
                    "message": "untranslated English remains: fn main",
                },
            }
        )
    else:
        error = (
            f"translate:{case.batch.manifest.request_id}: request_id mismatch"
            if failure_kind == "request-id-mismatch"
            else "untranslated English remains: fn main"
        )
        prior = _failed(code, prior, "translate", error, case.batch)
    journal.save(prior)
    state.write(
        journal.store.root / "failures" / f"{code.item_id}.json",
        canonical_json_bytes(
            {
                "format": "epubox-content-failures-1",
                "item_id": code.item_id,
                "entries": [
                    {
                        "item_id": code.item_id,
                        "stage": "translate",
                        "generation": canonical_hash("legacy-code-generation"),
                        "source_hash": canonical_hash(code),
                        "events": [canonical_hash("legacy-code-failure")],
                    }
                ],
            }
        ),
    )
    failure_before = tuple(
        (path.name, state.read(path)) for path in state.glob(journal.store.root / "failures", "*.json")
    )

    recovered = journal.recover_results()

    assert recovered[prose.item_id] == completed
    assert state.read(journal._result_path(prose.item_id)) == prose_before
    assert {document.extractor_version for document in case.session.index.documents.values()} == {_ATOMIC4}
    record = recovered[code.item_id]
    assert record.status == ItemStatus.REVIEWED and record.stage == "reviewed"
    assert record.target_projection == code.source_projection == _RUST
    assert record.checks["preserved_code"] == {
        "version": 2,
        "member_hash": canonical_hash(code),
    }
    assert "decision" not in record.checks and "review_request_id" not in record.checks
    assert (
        tuple((path.name, state.read(path)) for path in state.glob(journal.store.root / "failures", "*.json"))
        == failure_before
    )
    assert all(not request.attempts for request in journal._requests.values())
    assert BodyJournal(case.session.store).recover_results() == recovered


@pytest.mark.parametrize(
    ("body", "projection"),
    (
        ("<p>const index = values[index / 2];</p>", "const index = values[index / 2];"),
        (
            (
                '<p>    <span class="syntax-keyword">fn</span> main() {\n        '
                '<span class="syntax-keyword">let</span> index = values[index / 2];\n    }</p>'
            ),
            _RUST,
        ),
        (
            (
                '<p style="font-family: Menlo, Consolas, monospace">    '
                '<span class="syntax-keyword">fn</span> main() {\n        '
                '<span class="syntax-keyword">let</span> index = values[index / 2];\n    }</p>'
            ),
            _RUST,
        ),
        (
            (
                '<p style="white-space: pre-wrap">    '
                '<span class="syntax-keyword">fn</span> main() {\n        '
                '<span class="syntax-keyword">let</span> index = values[index / 2];\n    }</p>'
            ),
            _RUST,
        ),
        (
            (
                '<p>Natural text <span style="white-space: pre-wrap; font-family: monospace">'
                "index /= 2</span> continues.</p>"
            ),
            "Natural text ⟦+g1⟧index /= 2⟦-g1⟧ continues.",
        ),
        ('<p title="index /= 2">Body.</p>', "index /= 2"),
    ),
    ids=("ordinary-prose", "no-style", "monospace-only", "pre-wrap-only", "inner-style", "attribute"),
)
def test_atomic4_code_shape_requires_whole_prewrapped_generic_monospace_source(
    tmp_path, monkeypatch, body, projection
):
    case = _atomic4_rust_case(tmp_path, monkeypatch, body=body, projections=(projection,))
    member = case.batch.items[0]

    assert not _is_source_code(case.session, member)


@pytest.mark.parametrize("damage", ("split", "projection", "registry"))
def test_atomic4_code_shape_rejects_nonwhole_or_identity_changed_member(tmp_path, monkeypatch, damage):
    case = _atomic4_rust_case(tmp_path, monkeypatch)
    member = case.batch.items[1]
    if damage == "split":
        changed = member.model_copy(
            update={
                "item_id": "pc-forged-code-piece",
                "piece_count": 2,
                "piece_index": 0,
            }
        )
    elif damage == "projection":
        changed = member.model_copy(update={"source_projection": member.source_projection + "\n// changed"})
    else:
        changed = member.model_copy(update={"registry": {}})

    assert not _is_source_code(case.session, changed)


def test_atomic4_code_shape_rejects_changed_source_resource_hash(tmp_path, monkeypatch):
    import engine.services.code as code_module
    from engine.epub.verification import stage_epub

    case = _atomic4_rust_case(tmp_path, monkeypatch)
    member = case.batch.items[1]
    snapshot = state.snapshot(case.session.store.root)
    tampered = tmp_path / "tampered-source.epub"
    with zipfile.ZipFile(snapshot) as archive:
        chapter = archive.read("OEBPS/chapter.xhtml").replace(b"values[index", b"changed[index")
    stage_epub(snapshot, tampered, {"OEBPS/chapter.xhtml": chapter})
    monkeypatch.setattr(code_module.state, "snapshot", lambda _root: tampered)

    with pytest.raises(IdentityMismatch, match="source resource changed"):
        _is_source_code(case.session, member)


def test_atomic4_code_recovery_does_not_bypass_unrelated_protocol_failure(tmp_path, monkeypatch):
    case = _atomic4_rust_case(tmp_path, monkeypatch)
    journal = BodyJournal(case.session.store, case.session)
    code = case.batch.items[1]
    failed = _failed(
        code,
        journal.records()[code.item_id],
        "translate",
        "translation item missing from otherwise valid response",
        case.batch,
    )
    journal.save(failed)
    before = state.read(journal._result_path(code.item_id))

    recovered = journal.recover_results()

    assert recovered[code.item_id] == failed
    assert state.read(journal._result_path(code.item_id)) == before


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
