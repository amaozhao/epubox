from __future__ import annotations

import asyncio
import codecs
import json
import os
import zipfile
from pathlib import Path

import pytest
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

import engine.epub.publish as publish_module
from engine.agents.workflow import run_workflow
from engine.core.markup import parse_xml_bytes
from engine.epub.diagnostics import compare
from engine.epub.publish import _language, publish_atomic, recover_atomic, validate_atomic_output
from engine.epub.validation import EpubCheckResult, EpubValidationError
from engine.epub.verification import file_hash, verify_baseline
from engine.item.inline import events_to_projection, parse_projection
from engine.schemas.contracts import canonical_json_bytes
from engine.schemas.internal import Event
from engine.schemas.members import MemberBatch
from engine.services.atomic import AtomicStore
from engine.services.journal import BodyJournal
from tests.engine.agents.workflow import ReadyCase, prepare_case, review_item
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker

XHTML = "http://www.w3.org/1999/xhtml"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _case(root: Path, body: str, projections: tuple[str, ...]) -> ReadyCase:
    case = prepare_case(root, body, projections)
    original = (root / "source.epub").resolve(strict=True)
    status = original.stat()
    AtomicStore.atomic_write_bytes(
        case.session.store.root / "source.json",
        canonical_json_bytes(
            {
                "format": "epubox-source-1",
                "run_id": case.prepared.plan.run_id,
                "source_hash": case.prepared.plan.source_hash,
                "original_path": str(original),
                "st_dev": status.st_dev,
                "st_ino": status.st_ino,
            }
        ),
    )
    return case


def test_language_patch_preserves_utf8_protected_bytes_and_line_endings() -> None:
    raw = (
        b'<?xml version="1.0" encoding="UTF-8"?>\r\n'
        b'<html xmlns="http://www.w3.org/1999/xhtml" lang="en">\r\n'
        b"<head><style>a>b{color:red}</style><script>if (a > b) x();</script></head>\r\n"
        b"<body><p>Text</p></body></html>"
    )

    result = _language(raw, "application/xhtml+xml", opf=False)

    root = parse_xml_bytes(result).getroot()
    assert root.get("lang") == "zh-Hans" and root.get(XML_LANG) == "zh-Hans"
    assert b"<style>a>b{color:red}</style><script>if (a > b) x();</script>" in result
    assert result.count(b"\r\n") == raw.count(b"\r\n")


def test_language_patch_preserves_utf16_bom_and_protected_text() -> None:
    text = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        f'<html xmlns="{XHTML}"><head><script>const value = "原样";</script></head>'
        "<body><p>Text</p></body></html>"
    )
    raw = codecs.BOM_UTF16_LE + text.encode("utf-16-le")

    result = _language(raw, "application/xhtml+xml", opf=False)

    root = etree.fromstring(result)
    assert result.startswith(codecs.BOM_UTF16_LE)
    assert root.get("lang") == "zh-Hans" and root.get(XML_LANG) == "zh-Hans"
    assert 'const value = "原样";' in result[len(codecs.BOM_UTF16_LE) :].decode("utf-16-le")


def test_package_language_patch_changes_only_primary_dc_language_text() -> None:
    raw = (
        b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        b'xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata>'
        b"<dc:title>Title</dc:title><dc:language>en</dc:language>"
        b'<meta property="dcterms:modified">2026-09-28T00:00:00Z</meta>'
        b"</metadata><manifest/><spine/></package>"
    )

    result = _language(raw, "application/oebps-package+xml", opf=True)

    assert b"<dc:language>zh-Hans</dc:language>" in result
    assert b"<dc:title>Title</dc:title>" in result
    assert b"2026-09-28T00:00:00Z" in result


def test_atomic_publish_uses_reviewed_results_preserves_resources_and_recovers(tmp_path: Path) -> None:
    case = _case(
        tmp_path,
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
        "<body><p>Hello world.</p></body></html>",
        ("Hello world.",),
    )
    journal = BodyJournal(case.session.store, case.session)

    async def transport(kind, payload):
        if kind == "translate":
            items = [
                {"item_id": item["item_id"], "target": _translated(str(item["source"]))} for item in payload["items"]
            ]
            protocol = "epubox-text-1"
        else:
            items = [review_item(item, decision="no_change") for item in payload["items"]]
            protocol = "epubox-review-2"
        return {"raw": json.dumps({"protocol": protocol, "request_id": payload["request_id"], "items": items})}

    for path in sorted((case.session.store.root / "batches").glob("*.json")):
        batch = MemberBatch.model_validate_json(path.read_bytes())
        result = asyncio.run(
            run_workflow(
                case.prepared,
                batch,
                case.index,
                journal.runtime(transport=transport),
                session=case.session,
                save=journal.save,
                records=journal.records(batch.manifest.item_ids),
            )
        )
        assert result.status == "completed"

    output = tmp_path / "source-cn.epub"
    published = publish_atomic(case.session.store, output, StubChecker())

    assert published["path"] == str(output) and published["sha256"]
    verification = published["verification"]
    assert isinstance(verification, dict) and "baseline" not in verification
    assert isinstance(published["publish"], dict) and published["publish"]["state"] == "completed"
    with zipfile.ZipFile(case.session.store.root / "source.epub") as source, zipfile.ZipFile(output) as target:
        assert source.read("META-INF/container.xml") == target.read("META-INF/container.xml")
        assert "译文" in target.read("OEBPS/chapter.xhtml").decode()
    assert recover_atomic(case.session.store, output) == published


def test_atomic_publish_rejects_snapshot_alias_and_keeps_existing_product_on_failed_check(tmp_path: Path) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }

    def complete(self, require_complete=True):
        del self
        assert require_complete
        return targets

    original = BodyJournal.parent_targets
    BodyJournal.parent_targets = complete
    try:
        with pytest.raises(EpubValidationError, match="outside the translation work directory"):
            publish_atomic(case.session.store, case.session.store.root / "source.epub", StubChecker())
        output = tmp_path / "existing.epub"
        output.write_bytes(b"old product")

        class FailedChecker:
            calls = 0

            def check(self, _path):
                from engine.epub.validation import EpubCheckResult

                self.calls += 1
                return (
                    EpubCheckResult(("stub",), 0)
                    if self.calls == 1
                    else EpubCheckResult(("failed",), 1, errors=("invalid",))
                )

        with pytest.raises(EpubValidationError):
            publish_atomic(case.session.store, output, FailedChecker(), overwrite=True)
        assert output.read_bytes() == b"old product"
        assert not list(tmp_path.glob(".*.candidate.epub"))
    finally:
        BodyJournal.parent_targets = original


def test_atomic_publish_reuses_one_verified_target_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    original = publish_module._targets
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(publish_module, "_targets", counted)
    publish_atomic(case.session.store, tmp_path / "single-snapshot-cn.epub", StubChecker(), session=case.session)

    assert calls == 1


def test_atomic_publish_detects_result_mutation_without_rebuilding_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    verify = publish_module.verify_staged_epub

    def mutate(*args, **kwargs):
        result = verify(*args, **kwargs)
        path = next((case.session.store.root / "results").glob("*.json"))
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(publish_module, "verify_staged_epub", mutate)
    with pytest.raises(EpubValidationError, match="changed during publication"):
        publish_atomic(case.session.store, tmp_path / "mutated-cn.epub", StubChecker(), session=case.session)


def test_atomic_publish_rejects_hardlink_to_snapshot(tmp_path: Path) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    output = tmp_path / "alias.epub"
    os.link(case.session.store.root / "source.epub", output)
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    original = BodyJournal.parent_targets
    BodyJournal.parent_targets = lambda self, require_complete=True: targets
    try:
        with pytest.raises(EpubValidationError, match="source EPUB"):
            publish_atomic(case.session.store, output, StubChecker(), overwrite=True)
    finally:
        BodyJournal.parent_targets = original


def test_atomic_publish_rejects_an_identical_source_copy(tmp_path: Path) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    output = tmp_path / "original.epub"
    output.write_bytes((case.session.store.root / "source.epub").read_bytes())

    with pytest.raises(EpubValidationError, match="identical source copy"):
        publish_atomic(case.session.store, output, StubChecker(), overwrite=True)


@pytest.mark.parametrize("name", ["preparation.json", "publish.json"])
def test_atomic_publish_never_overwrites_work_checkpoints(tmp_path: Path, name: str) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    output = case.session.store.root / name
    if not output.exists():
        output.write_bytes(b"checkpoint sentinel")
    before = output.read_bytes()

    with pytest.raises(EpubValidationError, match="outside the translation work directory"):
        publish_atomic(case.session.store, output, StubChecker(), overwrite=True)

    assert output.read_bytes() == before


def test_atomic_publish_rejects_symlink_into_work_directory(tmp_path: Path) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    checkpoint = case.session.store.root / "preparation.json"
    output = tmp_path / "linked.epub"
    output.symlink_to(checkpoint)
    before = checkpoint.read_bytes()

    with pytest.raises(EpubValidationError, match="outside the translation work directory"):
        recover_atomic(case.session.store, output)

    assert checkpoint.read_bytes() == before


@pytest.mark.parametrize("alias", [False, True])
def test_atomic_publish_never_overwrites_recorded_original_after_it_changes(tmp_path: Path, alias: bool) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    original = tmp_path / "source.epub"
    output = tmp_path / "moved-original.epub" if alias else original
    if alias:
        os.link(original, output)
    original.write_bytes(b"original changed after its immutable snapshot")
    before = output.read_bytes()

    with pytest.raises(EpubValidationError, match="recorded original EPUB"):
        publish_atomic(case.session.store, output, StubChecker(), overwrite=True)

    assert output.read_bytes() == before


def test_output_guard_rejects_changed_original_without_loading_workflow(tmp_path: Path, monkeypatch) -> None:
    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    original = tmp_path / "source.epub"
    original.write_bytes(b"changed original must remain protected")
    before = original.read_bytes()
    monkeypatch.setattr(
        "engine.epub.publish._targets",
        lambda *_args: (_ for _ in ()).throw(AssertionError("output guard must not load ready or body results")),
    )

    with pytest.raises(EpubValidationError, match="recorded original EPUB"):
        validate_atomic_output(case.session.store, original)

    assert original.read_bytes() == before


def _translated(source: str) -> str:
    return events_to_projection(
        Event(kind="text", value="译文。" if event.value.strip() else event.value) if event.kind == "text" else event
        for event in parse_projection(source)
    )


def _epub_error(path: Path) -> EpubCheckResult:
    resource = "OEBPS/chapter.xhtml"
    with zipfile.ZipFile(path) as archive:
        text = archive.read(resource).decode()
    offset = text.index("<p") + 2
    prefix = text[:offset]
    row = prefix.count("\n") + 1
    column = len(prefix.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2
    diagnostic = f'ERROR(RSC-005): {path}/{resource}({row},{column}): attribute "data-test" not allowed'
    return EpubCheckResult(("stub",), 1, errors=(diagnostic,))


class BaselineChecker:
    def __init__(self) -> None:
        self.calls = 0

    def check(self, path: Path) -> EpubCheckResult:
        self.calls += 1
        return _epub_error(path)


def _legacy_publish(monkeypatch: pytest.MonkeyPatch, case: ReadyCase, output: Path, checker: object):
    original = publish_module.verify_staged_epub

    def verify(*args, **kwargs):
        kwargs.pop("upgraded", None)
        return original(*args, **kwargs, upgraded=False)

    with monkeypatch.context() as patch:
        patch.setattr(publish_module, "verify_staged_epub", verify)
        return publish_atomic(case.session.store, output, checker)


def _resolved_proof(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    source = make_epub(tmp_path / "broken.epub", {"chapter.xhtml": '<p data-test="1">English.</p>'})
    output = make_epub(tmp_path / "resolved.epub", {"chapter.xhtml": "<p>中文。</p>"})
    passed = EpubCheckResult(("stub",), 0)
    baseline = compare(source, output, _epub_error(source), passed)
    return source, output, {"output_hash": file_hash(output), "epubcheck": passed.to_dict(), "baseline": baseline}


def test_resolved_epubcheck_baseline_uses_a_validated_zero_error_fast_path(tmp_path: Path) -> None:
    source, output, verification = _resolved_proof(tmp_path)

    class UnexpectedChecker:
        def check(self, _path: Path) -> EpubCheckResult:
            raise AssertionError("resolved baseline must not run EPUBCheck again")

    verify_baseline(source, output, verification, UnexpectedChecker())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "unknown"),
        ("source_hash", "0" * 64),
        ("output_hash", "0" * 64),
        ("inherited_errors", False),
        ("inherited_errors", -1),
    ],
)
def test_resolved_epubcheck_baseline_rejects_invalid_identity_and_count(
    tmp_path: Path, field: str, value: object
) -> None:
    source, output, verification = _resolved_proof(tmp_path)
    baseline = verification["baseline"]
    assert isinstance(baseline, dict)
    baseline[field] = value

    with pytest.raises(EpubValidationError, match="evidence"):
        verify_baseline(source, output, verification)


def test_atomic_publish_rejects_inherited_epubcheck_errors_without_an_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, '<p data-test="1">Hello world.</p>', ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    checker = BaselineChecker()
    output = tmp_path / "inherited-cn.epub"

    with pytest.raises(EpubValidationError, match="Upgraded EPUB 3.0 still has EPUBCheck ERROR/FATAL"):
        publish_atomic(case.session.store, output, checker)

    assert checker.calls == 2
    assert not output.exists()
    assert not list(tmp_path.glob(".*.candidate.epub"))


@pytest.mark.parametrize("tamper", ["removed", "forged", "epubcheck", "passed", "message", "command", "returncode"])
def test_atomic_recovery_rejects_missing_or_forged_inherited_error_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    case = _case(tmp_path, '<p data-test="1">Hello world.</p>', ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)
    output = tmp_path / "tampered-cn.epub"
    _legacy_publish(monkeypatch, case, output, BaselineChecker())
    path = case.session.store.root / "publish.json"
    intent = json.loads(path.read_text())
    baseline = intent["verification"]["baseline"]
    if tamper == "removed":
        del intent["verification"]["baseline"]
    elif tamper == "epubcheck":
        intent["verification"]["epubcheck"]["errors"] = []
    elif tamper == "passed":
        intent["verification"]["epubcheck"]["passed"] = True
    elif tamper == "message":
        errors = intent["verification"]["epubcheck"]["errors"]
        errors[0] = errors[0].replace("data-test", "xxxx-test")
    elif tamper == "command":
        intent["verification"]["epubcheck"]["command"] = ["forged"]
    elif tamper == "returncode":
        intent["verification"]["epubcheck"]["returncode"] = 7
    else:
        baseline["inherited_errors"] += 1
    path.write_text(json.dumps(intent))

    with pytest.raises(EpubValidationError, match="evidence"):
        recover_atomic(case.session.store, output, BaselineChecker())


def test_atomic_publish_repacks_a_verified_historical_artifact_as_clean_epub3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, '<p data-test="1">Hello world.</p>', ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)

    class ResolvedChecker:
        def __init__(self) -> None:
            self.calls = 0

        def check(self, path: Path) -> EpubCheckResult:
            self.calls += 1
            return _epub_error(path) if self.calls == 1 else EpubCheckResult(("stub",), 0)

    output = tmp_path / "historical-cn.epub"
    historical = _legacy_publish(monkeypatch, case, output, ResolvedChecker())
    with zipfile.ZipFile(output) as archive:
        old_contents = {path: archive.read(path) for path in archive.namelist()}
    old_verification = historical["verification"]
    assert isinstance(old_verification, dict) and "epub_version" not in old_verification
    assert old_verification["baseline"]["source_epubcheck"]["passed"] is False

    published = publish_atomic(case.session.store, output, StubChecker())

    verification = published["verification"]
    assert isinstance(verification, dict)
    assert verification["epub_version"] == "3.0"
    assert verification["epubcheck"]["passed"] is True
    assert "baseline" not in verification
    with zipfile.ZipFile(output) as archive:
        assert {path: archive.read(path) for path in archive.namelist()} == old_contents


def test_atomic_recovery_checks_source_hash_for_a_resolved_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(tmp_path, '<p data-test="1">Hello world.</p>', ("Hello world.",))
    targets = {
        item.item_id: item.source_projection for inventory in case.index.inventories for item in inventory.items
    }
    monkeypatch.setattr(BodyJournal, "parent_targets", lambda self, require_complete=True: targets)

    class ResolvedChecker:
        def __init__(self) -> None:
            self.calls = 0

        def check(self, path: Path) -> EpubCheckResult:
            self.calls += 1
            return _epub_error(path) if self.calls == 1 else EpubCheckResult(("stub",), 0)

    checker = ResolvedChecker()
    output = tmp_path / "resolved-cn.epub"
    published = publish_atomic(case.session.store, output, checker)
    verification = published["verification"]
    assert isinstance(verification, dict)
    assert verification["epubcheck"]["passed"] is True
    assert verification["epub_version"] == "3.0"
    snapshot = case.session.store.root / "source.epub"
    snapshot.chmod(0o644)
    snapshot.write_bytes(snapshot.read_bytes() + b"changed")
    monkeypatch.setattr("engine.epub.publish._targets", lambda _store: (case.session, targets))

    with pytest.raises(EpubValidationError, match="Source snapshot changed"):
        recover_atomic(case.session.store, output, checker)
    assert checker.calls == 2


@pytest.mark.parametrize("hardlink", [False, True])
def test_registered_original_alias_is_protected_even_after_original_changes(tmp_path, hardlink):
    import hashlib

    case = _case(tmp_path, "<p>Hello world.</p>", ("Hello world.",))
    original = tmp_path / "original.epub"
    original.write_bytes(b"original before its repaired snapshot")
    status = original.stat()
    path = case.session.store.root / "source.json"
    hint = json.loads(path.read_text())
    hint["aliases"] = [
        {
            "original_path": str(original.resolve()),
            "source_hash": hashlib.sha256(original.read_bytes()).hexdigest(),
            "st_dev": status.st_dev,
            "st_ino": status.st_ino,
        }
    ]
    path.write_text(json.dumps(hint))
    original.write_bytes(b"changed original remains protected")
    output = tmp_path / "alias.epub" if hardlink else original
    if hardlink:
        os.link(original, output)
    before = output.read_bytes()
    with pytest.raises(EpubValidationError, match="recorded original EPUB"):
        validate_atomic_output(case.session.store, output)
    with pytest.raises(EpubValidationError, match="recorded original EPUB"):
        publish_atomic(case.session.store, output, StubChecker(), overwrite=True)
    assert output.read_bytes() == before
