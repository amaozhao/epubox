"""Publish the verified atomic workflow without normalizing source XML."""

from __future__ import annotations

import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.epub.fill import fill_resource
from engine.epub.parsing import OPF_NAMESPACE, XHTML_NAMESPACE, parse_resource
from engine.epub.ranges import RawSpan, SlotSpan, index_resource
from engine.epub.validation import EpubValidationError, inspect_epub
from engine.epub.verification import (
    file_hash,
    publish_verified,
    recover_publication,
    stage_epub,
    verify_staged_epub,
)
from engine.schemas.contracts import PreparationPlan, canonical_hash, strict_json_loads
from engine.services.ready import ReadySession
from engine.services.store import RunStore

_DC = "http://purl.org/dc/elements/1.1/"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_LANGUAGE = "zh-Hans"


def publish_atomic(
    store: RunStore,
    output_path: Path,
    checker: object,
    *,
    overwrite: bool = False,
) -> dict[str, object]:
    """Render only complete reviewed parents, verify, then atomically publish."""
    if not isinstance(store, RunStore):
        raise TypeError("atomic publication requires RunStore")
    output_path = Path(output_path)
    with store.lock():
        validate_atomic_output(store, output_path)
        session, targets = _targets(store)
        recovered = _recover(store, output_path, session.prepared.plan, targets)
        if recovered is not None:
            return recovered
        snapshot = store.root / "source.epub"
        inventory = inspect_epub(snapshot, session.prepared.plan.source_hash, checker=checker)
        replacements: dict[str, bytes] = {}
        accepted: dict[str, dict[str, str]] = {}
        with zipfile.ZipFile(snapshot) as archive:
            for document in session.index.inventories:
                path = document.document.resource.path
                raw = archive.read(path)
                owned = {item.item_id: targets[item.item_id] for item in document.items}
                rendered = fill_resource(raw, document, owned)
                if path in inventory.documents or path == inventory.opf_path:
                    rendered = _language(
                        rendered, document.document.resource.media_type, opf=path == inventory.opf_path
                    )
                replacements[path] = rendered
                accepted[path] = owned
        staged = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.candidate.epub"
        try:
            stage_epub(snapshot, staged, replacements)
            verification = verify_staged_epub(
                snapshot,
                staged,
                inventory,
                replacements,
                accepted_targets=accepted,
                checker=checker,
                expected_language=_LANGUAGE,
            )
            current_session, current_targets = _targets(store)
            if current_session.prepared != session.prepared or current_targets != targets:
                raise EpubValidationError("atomic_results_changed", "Reviewed targets changed during publication")
            if file_hash(snapshot) != session.prepared.plan.source_hash:
                raise EpubValidationError("source_changed", "Source snapshot changed during publication")
            intent = publish_verified(
                staged,
                output_path,
                store.root / "publish.json",
                run_id=session.prepared.plan.run_id,
                plan_fingerprint=canonical_hash(session.prepared.plan),
                version_vector=_versions(targets),
                verification=verification,
                forbidden_paths=(snapshot, store.root / session.prepared.preparation.source_path),
                overwrite=overwrite,
            )
        finally:
            staged.unlink(missing_ok=True)
    return {
        "path": str(output_path),
        "sha256": verification.output_hash,
        "verification": verification.to_dict(),
        "publish": intent,
    }


def recover_atomic(store: RunStore, output_path: Path) -> dict[str, object] | None:
    """Recover one already-verified atomic publication for the current results."""
    if not isinstance(store, RunStore):
        raise TypeError("atomic publication requires RunStore")
    output_path = Path(output_path)
    with store.lock():
        validate_atomic_output(store, output_path)
        session, targets = _targets(store)
        return _recover(store, output_path, session.prepared.plan, targets)


def validate_atomic_output(store: RunStore, output_path: Path) -> None:
    """Reject work records and every recorded source alias without loading workflow state."""
    if not isinstance(store, RunStore):
        raise TypeError("atomic publication requires RunStore")
    _reject_output(Path(output_path), store.root, store.read_preparation())


def _recover(store, output_path, plan, targets) -> dict[str, object] | None:
    intent = recover_publication(
        store.root / "publish.json",
        plan_fingerprint=canonical_hash(plan),
        version_vector=_versions(targets),
    )
    if intent is None or Path(str(intent.get("target_path", ""))).resolve() != output_path.resolve():
        return None
    verification = intent.get("verification")
    target_hash = intent.get("target_hash")
    epubcheck = verification.get("epubcheck") if isinstance(verification, dict) else None
    if (
        not isinstance(target_hash, str)
        or not isinstance(verification, dict)
        or verification.get("output_hash") != target_hash
        or not isinstance(epubcheck, dict)
        or epubcheck.get("passed") is not True
    ):
        raise EpubValidationError("invalid_publish_intent", "Atomic publication evidence is incomplete")
    return {"path": str(output_path), "sha256": target_hash, "verification": verification, "publish": intent}


def _targets(store: RunStore):
    from engine.services.journal import BodyJournal

    session = ReadySession(store)
    targets = BodyJournal(store, session=session).parent_targets(require_complete=True)
    expected = {item.item_id for inventory in session.index.inventories for item in inventory.items}
    if set(targets) != expected:
        raise EpubValidationError("incomplete_atomic_results", "Reviewed parents do not cover the source inventory")
    return session, targets


def _versions(targets: Mapping[str, str]) -> dict[str, str]:
    return {item_id: canonical_hash(target) for item_id, target in sorted(targets.items())}


def _reject_output(output: Path, root: Path, preparation: PreparationPlan) -> None:
    resolved, workspace = output.resolve(strict=False), root.resolve(strict=True)
    if resolved == workspace or workspace in resolved.parents:
        raise EpubValidationError("unsafe_output_path", "Output must stay outside the translation work directory")
    original, stored_inode = _source_hint(root, preparation)
    if resolved == original:
        raise EpubValidationError("unsafe_output_path", "Output aliases the recorded original EPUB")
    if output.exists():
        output_inode = _inode(output)
        current_inode = _inode(original) if original.exists() else None
        if output_inode in {stored_inode, current_inode}:
            raise EpubValidationError("unsafe_output_path", "Output aliases the recorded original EPUB inode")
    snapshot = root / "source.epub"
    if output.exists() and output.is_file() and file_hash(output) == file_hash(snapshot):
        raise EpubValidationError("unsafe_output_path", "Output is the source EPUB or an identical source copy")


def _source_hint(root: Path, preparation: PreparationPlan) -> tuple[Path, tuple[int, int]]:
    path = root / "source.json"
    try:
        value = strict_json_loads(path.read_bytes())
    except Exception as error:
        raise EpubValidationError(
            "invalid_source_hint", f"Original source record is missing or invalid: {error}"
        ) from error
    keys = {"format", "run_id", "source_hash", "original_path", "st_dev", "st_ino"}
    if not isinstance(value, dict) or set(value) != keys:
        raise EpubValidationError("invalid_source_hint", "Original source record fields are invalid")
    original = value["original_path"]
    device, inode = value["st_dev"], value["st_ino"]
    if (
        value["format"] != "epubox-source-1"
        or value["run_id"] != preparation.run_id
        or value["source_hash"] != preparation.source_hash
        or not isinstance(original, str)
        or not original
        or not Path(original).is_absolute()
        or Path(original).resolve(strict=False) != Path(original)
        or type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode < 0
    ):
        raise EpubValidationError("invalid_source_hint", "Original source record identity is invalid")
    return Path(original), (device, inode)


def _inode(path: Path) -> tuple[int, int]:
    status = path.stat()
    return status.st_dev, status.st_ino


def _language(raw: bytes, media_type: str, *, opf: bool) -> bytes:
    parsed = parse_resource(raw, media_type)
    if parsed.tree is None or parsed.diagnostics:
        raise EpubValidationError("language_mapping_failed", "Target language has no verified XML byte mapping")
    index = index_resource(parsed)
    root = parsed.tree.getroot()
    patches: list[tuple[RawSpan, bytes]] = []
    if etree.QName(root).namespace == XHTML_NAMESPACE:
        _root_languages(index, patches)
    if opf:
        if etree.QName(root).namespace != OPF_NAMESPACE:
            raise EpubValidationError("language_mapping_failed", "Package document has the wrong namespace")
        languages = root.findall(f"{{{OPF_NAMESPACE}}}metadata/{{{_DC}}}language")
        if not languages:
            raise EpubValidationError("missing_language", "Source OPF has no dc:language")
        path = _path(languages[0])
        slots = [
            slot for slot in index.slots if slot.path == path and slot.field == "text" and slot.special_index is None
        ]
        if len(slots) != 1 or not slots[0].text:
            raise EpubValidationError("language_mapping_failed", "Primary dc:language has no exact text range")
        patches.append((_full(slots[0]), _LANGUAGE.encode(index.encoding)))
    return _splice(raw, patches)


def _root_languages(index, patches: list[tuple[RawSpan, bytes]]) -> None:
    attributes = {
        slot.attribute_name: slot
        for slot in index.slots
        if slot.path == () and slot.field == "attribute" and slot.special_index is None
    }
    missing: list[str] = []
    for name, lexical in (("lang", "lang"), (_XML_LANG, "xml:lang")):
        slot = attributes.get(name)
        if slot is None:
            missing.append(lexical)
        elif not slot.text:
            raise EpubValidationError("language_mapping_failed", f"Root {lexical} has no exact value range")
        else:
            patches.append((_full(slot), _LANGUAGE.encode(index.encoding)))
    if missing:
        node = index.nodes[()]
        suffix = "/>" if node.self_closing else ">"
        point = node.starttag.end - len(suffix.encode(index.encoding))
        value = "".join(f' {name}="{_LANGUAGE}"' for name in missing).encode(index.encoding)
        patches.append((RawSpan(point, point), value))


def _full(slot: SlotSpan) -> RawSpan:
    span = slot.source_span(0, len(slot.text))
    return RawSpan(span.byte_start, span.byte_end)


def _path(node: etree._Element) -> tuple[int, ...]:
    result: list[int] = []
    while node.getparent() is not None:
        parent = node.getparent()
        children = [child for child in parent if isinstance(child.tag, str)]
        result.append(children.index(node))
        node = parent
    return tuple(reversed(result))


def _splice(raw: bytes, patches: list[tuple[RawSpan, bytes]]) -> bytes:
    result = raw
    end = len(raw)
    for span, value in sorted(patches, key=lambda patch: patch[0].start, reverse=True):
        if span.end > end:
            raise EpubValidationError("language_mapping_failed", "Language byte ranges overlap")
        result = result[: span.start] + value + result[span.end :]
        end = span.start
    return result


__all__ = ["publish_atomic", "recover_atomic", "validate_atomic_output"]
