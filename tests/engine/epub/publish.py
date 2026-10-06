from __future__ import annotations

import asyncio
import codecs
import json
import os
import zipfile
from pathlib import Path

import pytest
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.agents.workflow import run_workflow
from engine.core.markup import parse_xml_bytes
from engine.epub.publish import _language, publish_atomic, recover_atomic, validate_atomic_output
from engine.epub.validation import EpubValidationError
from engine.item.inline import events_to_projection, parse_projection
from engine.schemas.contracts import canonical_json_bytes
from engine.schemas.internal import Event
from engine.schemas.members import MemberBatch
from engine.services.atomic import AtomicStore
from engine.services.journal import BodyJournal
from tests.engine.agents.workflow import ReadyCase, prepare_case, review_item
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
