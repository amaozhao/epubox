from __future__ import annotations

import asyncio
import json
import zipfile
from functools import partial

import pytest

from engine.agents.workflow import _failed
from engine.execution.atomic import run_atomic
from engine.item.atoms import extract_resource
from engine.schemas.contracts import ItemStatus, canonical_hash, canonical_json_bytes
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.code import _source_code_version
from engine.services.journal import BodyJournal
from engine.services.manual import import_codex_translation
from engine.services.partition import build_plan, plan_path
from engine.services.parts import PartJournal
from engine.services.resume import plan_resume
from tests.engine.agents import test_workflow as fixtures
from tests.engine.core.test_code import SHELL, rendered_listing
from tests.engine.execution.test_atomic import answer

_ATOMIC4 = "epubox-atomic-4"
_RUST = "⟦=x1⟧⟦+g1⟧fn⟦-g1⟧ main() {\n        ⟦+g2⟧let⟦-g2⟧ index = values[index / 2];\n    }"


def test_frozen_calibre_rust_lines_resume_without_http(tmp_path, monkeypatch):
    from engine.services import preparation

    version = "epubox-atomic-7"
    monkeypatch.setattr(preparation, "ATOMIC_EXTRACTOR_VERSION", version)
    monkeypatch.setattr(fixtures, "EXTRACTOR_VERSION", version)
    loop = "for (index, value) in samples.iter().enumerate() {"
    field = "pub index: u32,"
    body = "<p>Keep data safe.</p>" + rendered_listing(
        "listing", "fn main() {", loop, "}", "pub struct GpuMetrics {", field, "}"
    )
    raw = ('<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + "</body></html>").encode()
    extracted = extract_resource(raw, "OEBPS/chapter.xhtml", "source", extractor_version=version)
    case = fixtures.prepare_case(tmp_path, body, tuple(item.source_projection for item in extracted.items))
    journal = BodyJournal(case.session.store, case.session)
    prose, *code = case.batch.items
    import_codex_translation(journal, prose, journal.records()[prose.item_id], "确保数据安全。", user_authorized=True)
    prose_before = state.read(journal._result_path(prose.item_id))
    for member in code:
        if loop in member.source_projection:
            journal.save(
                _failed(
                    member,
                    journal.records()[member.item_id],
                    "translate",
                    f"untranslated English remains: {loop}",
                    case.batch,
                )
            )
        if field in member.source_projection:
            assert journal.records()[member.item_id].status == ItemStatus.PENDING

    code_ids = {member.item_id for member in code}

    async def reject_code_http(stage, payload):
        assert code_ids.isdisjoint(item["item_id"] for item in payload["items"])
        return answer(stage, payload)

    result = asyncio.run(run_atomic(journal.store.root, transport=reject_code_http, ready_session=case.session))

    assert result.status == "translated", result.reason
    assert state.read(journal._result_path(prose.item_id)) == prose_before
    records = BodyJournal(journal.store).records()
    for member in code:
        assert records[member.item_id].status == ItemStatus.REVIEWED
        assert records[member.item_id].target_projection == member.source_projection
        assert records[member.item_id].checks["preserved_code"] == {
            "version": 5,
            "member_hash": canonical_hash(member),
        }
    assert plan_resume(journal.store.root).phase == "publication"


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

    assert _source_code_version(case.session, member) is None


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

    assert _source_code_version(case.session, changed) is None


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
        _source_code_version(case.session, member)


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


@pytest.mark.parametrize(
    ("block_class", "line_class", "text_class"),
    (("class-0-1", "class-0-2", "bookSpan"), ("class48", "class39", "bookspan")),
)
@pytest.mark.parametrize(
    "error",
    (
        None,
        "untranslated English remains: def render(row): return row[0]",
        "response root must contain exactly protocol, request_id, and items",
    ),
)
def test_rendered_python_in_atomic6_resumes_from_source(
    tmp_path, monkeypatch, error, block_class, line_class, text_class
):
    import engine.epub.preparation as epub_preparation
    from engine.services import preparation

    version = "epubox-atomic-6"
    monkeypatch.setattr(preparation, "ATOMIC_EXTRACTOR_VERSION", version)
    monkeypatch.setattr(epub_preparation, "ATOMIC_EXTRACTOR_VERSION", version)
    monkeypatch.setattr(
        epub_preparation, "extract_resource", partial(epub_preparation.extract_resource, extractor_version=version)
    )
    monkeypatch.setattr(fixtures, "EXTRACTOR_VERSION", version)
    case = fixtures.prepare_case(
        tmp_path,
        f'<div class="{block_class}">'
        f'<span class="{line_class}"><span class="{text_class}">def render(row):</span></span><br/>'
        f'<span class="{line_class}"><span class="{text_class}">&#160;&#160;&#160;&#160;return row[0]</span></span>'
        "</div>",
        (
            (
                "⟦+b1⟧⟦+g1⟧⟦+g2⟧def render(row):⟦-g2⟧⟦-g1⟧⟦-b1⟧⟦=x1⟧"
                "⟦+b2⟧⟦+g3⟧⟦+g4⟧\xa0\xa0\xa0\xa0return row[0]⟦-g4⟧⟦-g3⟧⟦-b2⟧"
            ),
        ),
    )
    journal = BodyJournal(case.session.store, case.session)
    (member,) = case.batch.items
    if error is not None:
        journal.save(_failed(member, journal.records()[member.item_id], "translate", error, case.batch))

    record = journal.recover_results()[member.item_id]

    assert record.status == ItemStatus.REVIEWED
    assert record.target_projection == member.source_projection
    assert record.checks["preserved_code"] == {"version": 3, "member_hash": canonical_hash(member)}
    assert BodyJournal(case.session.store).records()[member.item_id] == record
    assert all(not request.attempts for request in journal._requests.values())


@pytest.mark.parametrize("previously_reviewed", (False, True))
def test_frozen_shell_with_existing_sentence_parts_resumes_without_http(tmp_path, monkeypatch, previously_reviewed):
    import engine.epub.preparation as epub_preparation
    from engine.services import preparation

    version = "epubox-atomic-6"
    monkeypatch.setattr(preparation, "ATOMIC_EXTRACTOR_VERSION", version)
    monkeypatch.setattr(epub_preparation, "ATOMIC_EXTRACTOR_VERSION", version)
    monkeypatch.setattr(
        epub_preparation, "extract_resource", partial(epub_preparation.extract_resource, extractor_version=version)
    )
    monkeypatch.setattr(fixtures, "EXTRACTOR_VERSION", version)
    raw = ('<html xmlns="http://www.w3.org/1999/xhtml"><body>' + SHELL + "</body></html>").encode()
    extracted = extract_resource(raw, "OEBPS/chapter.xhtml", "source", extractor_version=version)
    case = fixtures.prepare_case(tmp_path, SHELL, tuple(item.source_projection for item in extracted.items))
    journal = BodyJournal(case.session.store, case.session)
    (member,) = case.batch.items
    failed = _failed(member, journal.records()[member.item_id], "translate", "invalid response root", case.batch)
    journal.save(failed)
    plan = build_plan(case.session, member, failed, trigger_stage="translate", trigger_count=10)
    assert plan is not None and len(plan.children) > 4
    journal.store._atomic_write(plan_path(journal.store.root, member.item_id), plan)
    PartJournal.initialize(journal, plan)
    assert _source_code_version(case.session, member) == 4, member
    if previously_reviewed:
        response = answer("translate", case.batch.payload)
        target = json.loads(response["raw"])["items"][0]["target"]
        import_codex_translation(journal, member, failed, target, user_authorized=True)

    async def unexpected_http(stage, payload):
        ids = {item["item_id"] for item in payload["items"]}
        assert not ids.intersection({member.item_id, *(child.item_id for child in plan.children)})
        return answer(stage, payload)

    result = asyncio.run(run_atomic(journal.store.root, transport=unexpected_http, ready_session=case.session))
    assert result.status == "translated", result.reason
    record = BodyJournal(case.session.store).records()[member.item_id]
    assert record.status == ItemStatus.REVIEWED
    assert record.target_projection == member.source_projection
    assert record.checks["preserved_code"] == {"version": 4, "member_hash": canonical_hash(member)}
    assert state.exists(plan_path(journal.store.root, member.item_id))
    assert plan_resume(journal.store.root).phase == "publication"
