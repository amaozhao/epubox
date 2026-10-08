"""Package verification and crash-safe publication transactions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from engine.core.markup import parse_xml_bytes, qname_local_name
from engine.epub.diagnostics import compare, references, same_messages
from engine.epub.replace import replacement_fingerprint
from engine.epub.validation import (
    EpubCheckResult,
    EpubValidationError,
    PackageInventory,
    ValidationIssue,
    inspect_epub,
    validate_internal_references,
)
from engine.services import state

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


@dataclass(frozen=True)
class PackageVerification:
    output_hash: str
    epubcheck: EpubCheckResult
    checked_documents: tuple[str, ...]
    warnings: tuple[ValidationIssue, ...] = ()
    baseline: dict[str, object] | None = None
    epub_version: str | None = None

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "output_hash": self.output_hash,
            "epubcheck": self.epubcheck.to_dict(),
            "checked_documents": list(self.checked_documents),
            "warnings": [warning.__dict__ for warning in self.warnings],
        }
        if self.baseline is not None:
            value["baseline"] = self.baseline
        if self.epub_version is not None:
            value["epub_version"] = self.epub_version
        return value


def stage_epub(
    source_snapshot: Path,
    staged_path: Path,
    replacements: Mapping[str, bytes],
    *,
    source_directory: Path | None = None,
    allow_additions: bool = False,
) -> str:
    """Create a candidate EPUB while preserving every untouched ZIP member."""
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_snapshot) as source:
        names = set(source.namelist())
        unknown = set(replacements) - names
        if unknown and not allow_additions:
            raise EpubValidationError("unknown_replacement", f"Replacement is not in source EPUB: {min(unknown)}")
        if unknown:
            from engine.epub.validation import _validate_package_path

            for path in unknown:
                _validate_package_path(path)
        with zipfile.ZipFile(staged_path, "w") as target:
            mimetype = (
                replacements["mimetype"]
                if "mimetype" in replacements
                else _source_member(source, source.getinfo("mimetype"), source_directory)
            )
            if mimetype != b"application/epub+zip":
                raise EpubValidationError("invalid_mimetype", "mimetype cannot be changed")
            target.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
            for info in source.infolist():
                if info.filename == "mimetype":
                    continue
                data = (
                    replacements[info.filename]
                    if info.filename in replacements
                    else _source_member(source, info, source_directory)
                )
                copied = zipfile.ZipInfo(info.filename, info.date_time)
                copied.comment = info.comment
                copied.extra = info.extra
                copied.internal_attr = info.internal_attr
                copied.external_attr = info.external_attr
                copied.create_system = info.create_system
                copied.flag_bits = info.flag_bits & ~0x1
                target.writestr(copied, data, compress_type=info.compress_type)
            for path in sorted(unknown):
                target.writestr(path, replacements[path], compress_type=zipfile.ZIP_DEFLATED)
    return file_hash(staged_path)


def _source_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    directory: Path | None,
) -> bytes:
    if directory is None:
        return archive.read(info.filename)
    relative = PurePosixPath(info.filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise EpubValidationError("unsafe_path", f"Unsafe package path: {info.filename!r}")
    root = directory.resolve(strict=True)
    path = directory.joinpath(*relative.parts)
    parents = (path, *path.parents[: len(path.parts) - len(directory.parts)])
    linked = any(parent.is_symlink() for parent in parents)
    if info.is_dir():
        if not path.is_dir() or linked:
            raise EpubValidationError(
                "source_directory_changed", f"Extracted source directory is missing: {info.filename}"
            )
        return b""
    if linked or not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
        raise EpubValidationError("source_directory_changed", f"Extracted source resource is unsafe: {info.filename}")
    return path.read_bytes()


def verify_staged_epub(
    source_snapshot: Path,
    staged_path: Path,
    source_inventory: PackageInventory,
    expected_documents: Mapping[str, bytes],
    *,
    accepted_targets: Mapping[str, Mapping[str, str]],
    checker: object,
    expected_language: str | None = None,
    upgraded: bool = False,
) -> PackageVerification:
    output_hash = file_hash(staged_path)
    output_inventory = inspect_epub(staged_path, output_hash, checker=checker, for_output=True)
    if upgraded and output_inventory.epub_version != "3.0":
        raise EpubValidationError("epub_version_changed", "Upgraded output must be EPUB 3.0")
    if not upgraded and output_inventory.epub_version != source_inventory.epub_version:
        raise EpubValidationError("epub_version_changed", "Output EPUB version differs from source")
    added = set(output_inventory.entries) - set(source_inventory.entries)
    if (set(source_inventory.entries) - set(output_inventory.entries)) or (
        added and (not upgraded or added != {output_inventory.nav_path} or not added <= set(expected_documents))
    ):
        raise EpubValidationError("resource_inventory_changed", "Output resource inventory differs from source")
    if output_inventory.opf_path != source_inventory.opf_path:
        raise EpubValidationError("opf_changed", "Output package document path differs from source")
    if output_inventory.spine != source_inventory.spine:
        raise EpubValidationError("spine_changed", "Output reading order differs from source")
    if output_inventory.spine_linear != source_inventory.spine_linear:
        raise EpubValidationError("spine_changed", "Output linear reading order differs from source")
    output_items = {item.item_id: item for item in output_inventory.manifest}
    for item in source_inventory.manifest:
        current = output_items.get(item.item_id)
        if (
            current is None
            or current.path != item.path
            or current.media_type
            not in {item.media_type, "application/xhtml+xml" if item.media_type == "text/html" else item.media_type}
        ):
            raise EpubValidationError(
                "resource_inventory_changed", "Original manifest resource changed during upgrade"
            )
    if output_inventory.obfuscated_fonts != source_inventory.obfuscated_fonts:
        raise EpubValidationError("font_obfuscation_changed", "Font obfuscation resources changed")

    warnings = list(output_inventory.warnings)
    with zipfile.ZipFile(source_snapshot) as source, zipfile.ZipFile(staged_path) as output:
        for name in source.namelist():
            if name not in expected_documents and source.read(name) != output.read(name):
                raise EpubValidationError("unexpected_resource_change", f"Unexpected resource change: {name}")
        for path, expected in expected_documents.items():
            actual = output.read(path)
            _compare_document(path, expected, actual)
            if path not in accepted_targets:
                raise EpubValidationError("missing_target_evidence", f"No target evidence supplied for {path}")
        source_issues = validate_internal_references(source, source_inventory)
        output_issues = validate_internal_references(output, output_inventory)
        new_issues = references(source_issues, output_issues)
        if new_issues:
            raise EpubValidationError("invalid_references", new_issues[0].message, issues=new_issues)
        warnings.extend(output_issues)
        if expected_language:
            warnings.extend(_validate_metadata(output, output_inventory, accepted_targets, expected_language))
    result = output_inventory.epubcheck
    source_result = source_inventory.epubcheck
    if result is None or source_result is None:
        raise EpubValidationError("missing_epubcheck", "EPUBCheck evidence is missing")
    if upgraded and not result.passed:
        raise EpubValidationError(
            "epubcheck_failed",
            "Upgraded EPUB 3.0 still has EPUBCheck ERROR/FATAL: "
            + next(iter((*result.fatals, *result.errors)), "checker failed"),
        )
    baseline = (
        compare(source_snapshot, staged_path, source_result, result)
        if not upgraded and (not source_result.passed or not result.passed)
        else None
    )
    return PackageVerification(
        output_hash, result, tuple(sorted(expected_documents)), tuple(warnings), baseline, "3.0" if upgraded else None
    )


def publish_verified(
    staged_path: Path,
    target_path: Path,
    publish_path: Path,
    *,
    run_id: str,
    plan_fingerprint: str,
    version_vector: Mapping[str, int | str],
    verification: PackageVerification,
    forbidden_paths: Sequence[Path] = (),
    overwrite: bool = False,
) -> dict[str, object]:
    if file_hash(staged_path) != verification.output_hash:
        raise EpubValidationError("staged_hash_changed", "Staged EPUB changed after verification")
    _reject_alias(target_path, forbidden_paths)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    intent: dict[str, object] = {
        "format": "epubox-publish-1",
        "state": "intended",
        "run_id": run_id,
        "plan_fingerprint": _publication_fingerprint(plan_fingerprint),
        "version_vector": dict(sorted(version_vector.items())),
        "target_path": str(target_path.absolute()),
        "target_hash": verification.output_hash,
        "verification": verification.to_dict(),
        "created_at": utc_now(),
    }
    with _target_lock(target_path):
        _reject_alias(target_path, forbidden_paths)
        if target_path.exists() and not overwrite:
            raise FileExistsError(f"Output already exists: {target_path}")
        descriptor, name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, staged_path.open("rb") as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            if file_hash(temporary) != verification.output_hash:
                raise OSError("Target-filesystem copy hash mismatch")
            _atomic_json(publish_path, intent)
            if overwrite:
                os.replace(temporary, target_path)
            else:
                os.link(temporary, target_path)
                temporary.unlink()
            _sync(target_path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    intent["state"] = "completed"
    intent["completed_at"] = utc_now()
    _atomic_json(publish_path, intent)
    return intent


def verify_baseline(
    source_path: Path,
    output_path: Path,
    verification: Mapping[str, object],
    checker: object | None = None,
) -> None:
    """Recheck inherited EPUBCheck failures without trusting a persisted command."""
    epubcheck = verification.get("epubcheck")
    if not isinstance(epubcheck, dict) or type(epubcheck.get("passed")) is not bool:
        raise EpubValidationError("invalid_publish_intent", "Atomic publication evidence is incomplete")
    if verification.get("epub_version") == "3.0":
        if (
            epubcheck.get("passed") is not True
            or type(epubcheck.get("returncode")) is not int
            or epubcheck.get("returncode") != 0
            or epubcheck.get("errors") != []
            or epubcheck.get("fatals") != []
        ):
            raise EpubValidationError("invalid_publish_intent", "EPUB 3.0 publication evidence is inconsistent")
        with zipfile.ZipFile(output_path) as archive:
            from engine.epub.validation import _container_rootfiles

            rootfiles = _container_rootfiles(_parse_xml(archive.read("META-INF/container.xml"), "container.xml"))
            if len(rootfiles) != 1 or _parse_xml(archive.read(rootfiles[0]), rootfiles[0]).get("version") != "3.0":
                raise EpubValidationError("invalid_publish_intent", "Published package is not EPUB 3.0")
    baseline = verification.get("baseline")
    if baseline is None and epubcheck["passed"] is True:
        return
    output_hash = file_hash(output_path)
    source_hash = file_hash(source_path)
    keys = {"format", "source_hash", "output_hash", "source_epubcheck", "inherited_errors"}
    source_epubcheck = baseline.get("source_epubcheck") if isinstance(baseline, dict) else None
    command, returncode = epubcheck.get("command"), epubcheck.get("returncode")
    errors, fatals = epubcheck.get("errors"), epubcheck.get("fatals")
    source_errors = source_epubcheck.get("errors") if isinstance(source_epubcheck, dict) else None
    source_fatals = source_epubcheck.get("fatals") if isinstance(source_epubcheck, dict) else None
    inherited = baseline.get("inherited_errors") if isinstance(baseline, dict) else None
    if (
        not isinstance(baseline, dict)
        or set(baseline) != keys
        or baseline.get("format") != "epubox-diagnostics-1"
        or baseline.get("source_hash") != source_hash
        or baseline.get("output_hash") != output_hash
        or verification.get("output_hash") != output_hash
        or not isinstance(source_epubcheck, dict)
        or source_epubcheck.get("passed") is not False
        or not isinstance(source_errors, list)
        or not isinstance(source_fatals, list)
        or any(not isinstance(line, str) for line in (*source_errors, *source_fatals))
        or not isinstance(command, list)
        or any(not isinstance(part, str) for part in command)
        or type(returncode) is not int
        or epubcheck["passed"] is True
        and returncode != 0
        or not isinstance(errors, list)
        or not isinstance(fatals, list)
        or any(not isinstance(line, str) for line in (*errors, *fatals))
        or type(inherited) is not int
        or inherited < 0
        or inherited > len(source_errors) + len(source_fatals)
        or inherited != len(errors) + len(fatals)
        or (inherited == 0) != (epubcheck["passed"] is True)
    ):
        raise EpubValidationError("invalid_publish_intent", "Inherited EPUBCheck evidence is missing")
    if inherited == 0:
        return
    if checker is None:
        from engine.epub.checker import checker_command

        checker = checker_command()
    source_result = checker.check(source_path)  # type: ignore[attr-defined]
    output_result = checker.check(output_path)  # type: ignore[attr-defined]
    if not isinstance(source_result, EpubCheckResult) or not isinstance(output_result, EpubCheckResult):
        raise TypeError("checker.check() must return EpubCheckResult")
    if (
        compare(source_path, output_path, source_result, output_result) != baseline
        or command != list(output_result.command)
        or returncode != output_result.returncode
        or not same_messages(output_path, tuple(errors), output_result.errors)
        or not same_messages(output_path, tuple(fatals), output_result.fatals)
    ):
        raise EpubValidationError("invalid_publish_intent", "Inherited EPUBCheck evidence changed")


def recover_publication(
    publish_path: Path,
    *,
    plan_fingerprint: str,
    version_vector: Mapping[str, int | str],
) -> dict[str, object] | None:
    if not state.is_file(publish_path):
        return None
    data = json.loads(state.text(publish_path))
    if data.get("format") != "epubox-publish-1":
        raise EpubValidationError("invalid_publish_intent", "Unknown publish intent format")
    if data.get("plan_fingerprint") != _publication_fingerprint(plan_fingerprint) or data.get(
        "version_vector"
    ) != dict(sorted(version_vector.items())):
        return None
    target = Path(str(data.get("target_path", "")))
    target_hash = data.get("target_hash")
    if not target.is_file() or not isinstance(target_hash, str) or file_hash(target) != target_hash:
        return None
    if data.get("state") != "completed":
        data["state"] = "completed"
        data["completed_at"] = utc_now()
        _atomic_json(publish_path, data)
    return data


def _publication_fingerprint(plan_fingerprint: str) -> str:
    value = f"{plan_fingerprint}:{replacement_fingerprint()}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _validate_metadata(
    archive: zipfile.ZipFile,
    inventory: PackageInventory,
    accepted_targets: Mapping[str, Mapping[str, str]],
    expected_language: str,
) -> tuple[ValidationIssue, ...]:
    opf = parse_xml_bytes(archive.read(inventory.opf_path)).getroot()
    languages = [(element.text or "").strip() for element in opf.iter() if qname_local_name(element.tag) == "language"]
    if not languages or languages[0] != expected_language:
        raise EpubValidationError("target_language_missing", "OPF primary language was not finalized")
    if inventory.epub_version.startswith("3"):
        modified = [
            (element.text or "").strip()
            for element in opf.iter()
            if qname_local_name(element.tag) == "meta" and element.get("property") == "dcterms:modified"
        ]
        if len(modified) != 1 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", modified[0]):
            raise EpubValidationError("invalid_modified_time", "EPUB 3 requires one UTC dcterms:modified value")
    for path in inventory.documents:
        if path not in accepted_targets:
            continue
        root = parse_xml_bytes(archive.read(path)).getroot()
        if root.get("lang") != expected_language or root.get(_XML_LANG) != expected_language:
            raise EpubValidationError("document_language_missing", f"Target language missing from {path}")
    has_file_as = any(
        qname_local_name(name) == "file-as" or element.get("property") == "file-as"
        for element in opf.iter()
        for name in element.attrib
    )
    return (
        (ValidationIssue("metadata_file_as_preserved", "Ambiguous file-as metadata was preserved", "warning"),)
        if has_file_as
        else ()
    )


def _compare_document(path: str, expected: bytes, actual: bytes) -> None:
    if _tree_signature(_parse_xml(expected, path)) != _tree_signature(_parse_xml(actual, path)):
        raise EpubValidationError("document_mismatch", f"Actual document differs from verified plan: {path}")


def _tree_signature(element: ET.Element) -> tuple[object, ...]:
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        element.text,
        element.tail,
        tuple(_tree_signature(child) for child in element),
    )


def _parse_xml(data: bytes, path: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise EpubValidationError("invalid_output_xml", f"Invalid output XML in {path}: {error}") from error


def _reject_alias(target: Path, forbidden: Sequence[Path]) -> None:
    resolved = target.resolve(strict=False)
    for path in forbidden:
        if (
            resolved == path.resolve(strict=False)
            or target.exists()
            and path.exists()
            and os.path.samefile(target, path)
        ):
            raise EpubValidationError("unsafe_output_path", f"Output aliases protected input: {path}")


@contextmanager
def _target_lock(target: Path) -> Iterator[None]:
    path = target.with_name(f".{target.name}.lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    if state.artifact(path) != path:
        state.write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "PackageVerification",
    "file_hash",
    "publish_verified",
    "recover_publication",
    "stage_epub",
    "utc_now",
    "verify_baseline",
    "verify_staged_epub",
]
