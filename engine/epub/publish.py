"""Publish the verified atomic workflow without normalizing source XML."""

from __future__ import annotations

import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.epub.cleanup import CLEANUP_POLICY, cleanup_resources
from engine.epub.fill import fill_resource
from engine.epub.parsing import OPF_NAMESPACE, XHTML_NAMESPACE, parse_resource
from engine.epub.preservation import (
    CODE_POLICY,
    apply_code_plan,
    effective_code_plan,
)
from engine.epub.ranges import RawSpan, SlotSpan, index_resource
from engine.epub.replace import replace_resources
from engine.epub.styles import FONT_POLICY, use_reader_fonts
from engine.epub.upgrade import upgrade_package
from engine.epub.validation import EpubValidationError, inspect_epub
from engine.epub.verification import (
    file_hash,
    publish_verified,
    recover_publication,
    stage_epub,
    verify_baseline,
    verify_staged_epub,
)
from engine.schemas.contracts import PreparationPlan, canonical_hash, strict_json_loads
from engine.services import state
from engine.services.atomic import safe_id
from engine.services.ready import ReadySession
from engine.services.store import RunStore

if TYPE_CHECKING:
    from engine.services.journal import BodyJournal

_DC = "http://purl.org/dc/elements/1.1/"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_LANGUAGE = "zh-Hans"


def repack_completed(
    source: Path, output_path: Path, checker: object, *, overwrite: bool = False
) -> dict[str, object]:
    """Repackage a finished EPUB using only its bytes, without translation state or models."""
    source, output_path = Path(source).resolve(strict=True), Path(output_path)
    if output_path.resolve() == source or output_path.exists() and source.samefile(output_path):
        raise EpubValidationError("unsafe_output_path", "Repack output must not overwrite its input EPUB")
    source_hash = file_hash(source)
    inventory = inspect_epub(source, source_hash, checker=checker)
    media_types = {item.path: item.media_type for item in inventory.manifest}
    replacements, cleanup = cleanup_resources({}, media_types=media_types, source=source)
    staged = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.candidate.epub"
    try:
        stage_epub(source, staged, replacements)
        verification = verify_staged_epub(
            source,
            staged,
            inventory,
            replacements,
            accepted_targets={path: {} for path in replacements},
            checker=checker,
            upgraded=inventory.epub_version == "3.0",
            cleanup=cleanup,
            cleanup_only=True,
        )
        if file_hash(source) != source_hash:
            raise EpubValidationError("source_changed", "Completed EPUB changed while it was being repackaged")
        intent = publish_verified(
            staged,
            output_path,
            output_path.with_suffix(output_path.suffix + ".publish.json"),
            run_id="repack-" + uuid.uuid4().hex,
            plan_fingerprint=source_hash,
            version_vector={"source": source_hash},
            verification=verification,
            forbidden_paths=(source,),
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


def publish_atomic(
    store: RunStore,
    output_path: Path,
    checker: object,
    *,
    overwrite: bool = False,
    session: ReadySession | None = None,
    journal: BodyJournal | None = None,
) -> dict[str, object]:
    """Render only complete reviewed parents, verify, then atomically publish."""
    if not isinstance(store, RunStore):
        raise TypeError("atomic publication requires RunStore")
    output_path = Path(output_path)
    with store.lock():
        validate_atomic_output(store, output_path)
        code_plan = _code_plan(store)
        session, targets = _targets(store, session=session, journal=journal)
        recovered = _recover(store, output_path, session.prepared.plan, targets, checker=checker)
        if recovered is not None:
            proof = recovered.get("verification")
            if (
                isinstance(proof, dict)
                and proof.get("epub_version") == "3.0"
                and proof.get("font_policy") == FONT_POLICY
                and proof.get("cleanup_policy") == CLEANUP_POLICY
                and code_policy_current(store, proof)
            ):
                return recovered
            overwrite = True
        result_fingerprint = _result_fingerprint(store, session)
        snapshot = state.snapshot(store.root)
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
        replacements = upgrade_package(snapshot, inventory, replacements)
        media_types = {item.path: item.media_type for item in inventory.manifest}
        media_types[inventory.opf_path] = "application/oebps-package+xml"
        replacements = replace_resources(replacements, media_types=media_types, source=snapshot)
        replacements, removed_paths = use_reader_fonts(snapshot, inventory, replacements)
        if code_plan is not None:
            replacements, _code_stats = apply_code_plan(snapshot, replacements, code_plan)
        code_node_maps: dict[str, dict[tuple[int, ...], tuple[int, ...]]] = {}
        replacements, cleanup = cleanup_resources(
            replacements, media_types=media_types, source=snapshot, node_maps=code_node_maps
        )
        for path in replacements:
            accepted.setdefault(path, {})
        staged = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.candidate.epub"
        try:
            stage_epub(
                snapshot,
                staged,
                replacements,
                source_directory=store.root / "source" if state.compact(store.root) else None,
                allow_additions=True,
                removed_paths=removed_paths,
            )
            verification = verify_staged_epub(
                snapshot,
                staged,
                inventory,
                replacements,
                accepted_targets=accepted,
                checker=checker,
                expected_language=_LANGUAGE,
                upgraded=True,
                removed_paths=removed_paths,
                reader_fonts=True,
                cleanup=cleanup,
                code_plan=code_plan,
                code_node_maps=code_node_maps,
                source_code=True,
            )
            session.verify()
            if _result_fingerprint(store, session) != result_fingerprint:
                raise EpubValidationError("atomic_results_changed", "Reviewed targets changed during publication")
            if _code_plan(store) != code_plan:
                raise EpubValidationError(
                    "code_plan_changed", "Source code preservation plan changed during publication"
                )
            if file_hash(snapshot) != session.prepared.plan.source_hash:
                raise EpubValidationError("source_changed", "Source snapshot changed during publication")
            intent = publish_verified(
                staged,
                output_path,
                store.root / "publish.json",
                run_id=session.prepared.plan.run_id,
                plan_fingerprint=canonical_hash(session.prepared.plan),
                version_vector=_versions(targets, code_plan),
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


def recover_atomic(store: RunStore, output_path: Path, checker: object | None = None) -> dict[str, object] | None:
    """Recover one already-verified atomic publication for the current results."""
    if not isinstance(store, RunStore):
        raise TypeError("atomic publication requires RunStore")
    output_path = Path(output_path)
    with store.lock():
        validate_atomic_output(store, output_path)
        session, targets = _targets(store)
        return _recover(store, output_path, session.prepared.plan, targets, checker=checker)


def validate_atomic_output(store: RunStore, output_path: Path) -> None:
    """Reject work records and every recorded source alias without loading workflow state."""
    if not isinstance(store, RunStore):
        raise TypeError("atomic publication requires RunStore")
    _reject_output(Path(output_path), store.root, store.read_preparation())


def _recover(store, output_path, plan, targets, *, checker: object | None = None) -> dict[str, object] | None:
    code_plan = _code_plan(store)
    intent = recover_publication(
        store.root / "publish.json",
        plan_fingerprint=canonical_hash(plan),
        version_vector=_versions(targets, code_plan),
        allow_policy_upgrade=True,
    )
    if intent is None and code_plan is not None:
        # A newly approved code plan upgrades an existing verified publication
        # without reopening translation or discarding its accepted target proofs.
        intent = recover_publication(
            store.root / "publish.json",
            plan_fingerprint=canonical_hash(plan),
            version_vector=_versions(targets),
            allow_policy_upgrade=True,
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
    ):
        raise EpubValidationError("invalid_publish_intent", "Atomic publication evidence is incomplete")
    snapshot = state.snapshot(store.root)
    if file_hash(snapshot) != plan.source_hash:
        raise EpubValidationError("source_changed", "Source snapshot changed after publication")
    verify_baseline(snapshot, output_path, verification, checker, code_plan=code_plan)
    return {"path": str(output_path), "sha256": target_hash, "verification": verification, "publish": intent}


def _targets(
    store: RunStore,
    *,
    session: ReadySession | None = None,
    journal: BodyJournal | None = None,
):
    from engine.services.journal import BodyJournal

    if journal is not None:
        if journal.store.root != store.root or (session is not None and session.prepared != journal.session.prepared):
            raise EpubValidationError("invalid_verified_context", "Publication journal belongs to another run")
        journal.verify_persisted_proofs()
        session = journal.session
    session = session or ReadySession(store)
    targets = (journal or BodyJournal(store, session=session)).parent_targets(require_complete=True)
    expected = {member.parent_item_id for member in session.index.members}
    if set(targets) != expected:
        raise EpubValidationError("incomplete_atomic_results", "Reviewed parents do not cover the source inventory")
    return session, targets


def _versions(targets: Mapping[str, str], code_plan: Mapping[str, object] | None = None) -> dict[str, str]:
    versions = {item_id: canonical_hash(target) for item_id, target in sorted(targets.items())}
    if code_plan is not None:
        versions["@source-code"] = canonical_hash(code_plan)
    return versions


def _code_plan(store: RunStore) -> Mapping[str, object] | None:
    path = store.root / "checks" / "code.json"
    value = strict_json_loads(state.read(path), max_bytes=None) if state.is_file(path) else None
    if value is not None and not isinstance(value, dict):
        raise EpubValidationError("invalid_code_plan", "Source code preservation plan must be an object")
    return effective_code_plan(state.snapshot(store.root), value)


def code_policy_current(store: RunStore, verification: Mapping[str, object]) -> bool:
    """Determine whether publication includes the currently approved source code plan."""
    code_plan = _code_plan(store)
    return verification.get("code_policy") == CODE_POLICY and verification.get("code_plan_hash") == canonical_hash(
        code_plan
    )


def _result_fingerprint(store: RunStore, session: ReadySession) -> tuple[tuple[str, str], ...]:
    if state.compact(store.root):
        return (("state.json", file_hash(store.root / "state.json")),)
    return tuple(
        (item_id, file_hash(store.root / "results" / f"{safe_id(item_id)}.json"))
        for item_id in session.prepared.plan.member_hashes
    )


def _reject_output(output: Path, root: Path, preparation: PreparationPlan) -> None:
    resolved, workspace = output.resolve(strict=False), root.resolve(strict=True)
    if resolved == workspace or workspace in resolved.parents:
        raise EpubValidationError("unsafe_output_path", "Output must stay outside the translation work directory")
    sources = (_source_hint(root, preparation), *_source_aliases(root))
    for original, stored_inode in sources:
        if resolved == original:
            raise EpubValidationError("unsafe_output_path", "Output aliases the recorded original EPUB")
        if output.exists():
            output_inode = _inode(output)
            current_inode = _inode(original) if original.exists() else None
            if output_inode in {stored_inode, current_inode}:
                raise EpubValidationError("unsafe_output_path", "Output aliases the recorded original EPUB inode")
    snapshot = state.snapshot(root)
    if output.exists() and output.is_file() and file_hash(output) == file_hash(snapshot):
        raise EpubValidationError("unsafe_output_path", "Output is the source EPUB or an identical source copy")


def _source_hint(root: Path, preparation: PreparationPlan) -> tuple[Path, tuple[int, int]]:
    path = root / "source.json"
    try:
        value = strict_json_loads(state.read(path))
    except Exception as error:
        raise EpubValidationError(
            "invalid_source_hint", f"Original source record is missing or invalid: {error}"
        ) from error
    keys = {"format", "run_id", "source_hash", "original_path", "st_dev", "st_ino"}
    if not isinstance(value, dict) or set(value) not in (keys, keys | {"aliases"}):
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


def _source_aliases(root: Path) -> tuple[tuple[Path, tuple[int, int]], ...]:
    value = strict_json_loads(state.read(root / "source.json"))
    if not isinstance(value, dict):
        raise EpubValidationError("invalid_source_hint", "Original source record must be an object")
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list):
        raise EpubValidationError("invalid_source_hint", "Original source aliases must be an array")
    result = []
    for alias in aliases:
        keys = {"original_path", "source_hash", "st_dev", "st_ino"}
        if not isinstance(alias, dict) or set(alias) != keys:
            raise EpubValidationError("invalid_source_hint", "Original source alias fields are invalid")
        name, digest = alias["original_path"], alias["source_hash"]
        device, inode = alias["st_dev"], alias["st_ino"]
        if (
            not isinstance(name, str)
            or not name
            or not Path(name).is_absolute()
            or Path(name).resolve(strict=False) != Path(name)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or type(device) is not int
            or type(inode) is not int
            or device < 0
            or inode < 0
        ):
            raise EpubValidationError("invalid_source_hint", "Original source alias identity is invalid")
        result.append((Path(name), (device, inode)))
    return tuple(result)


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


__all__ = ["code_policy_current", "publish_atomic", "recover_atomic", "repack_completed", "validate_atomic_output"]
