from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import tinycss2

from engine.core.markup import UnsafeMarkupError, parse_xml_safely

XHTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}
XML_MEDIA_TYPES = XHTML_MEDIA_TYPES | {
    "application/oebps-package+xml",
    "application/x-dtbncx+xml",
    "application/smil+xml",
    "application/xml",
    "image/svg+xml",
    "text/xml",
}
FONT_MEDIA_PREFIXES = ("font/", "application/font", "application/vnd.ms-opentype")
FONT_SUFFIXES = {".otf", ".ttf", ".ttc", ".otc", ".woff", ".woff2"}
FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "error"
    resource: str | None = None


class EpubValidationError(ValueError):
    def __init__(self, code: str, message: str, *, issues: Iterable[ValidationIssue] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.issues = tuple(issues)


class EpubCheckUnavailable(EpubValidationError):
    pass


@dataclass(frozen=True)
class EpubCheckResult:
    command: tuple[str, ...]
    returncode: int
    errors: tuple[str, ...] = ()
    fatals: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    output: str = ""

    @property
    def passed(self) -> bool:
        return not self.errors and not self.fatals and self.returncode == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "errors": list(self.errors),
            "fatals": list(self.fatals),
            "warnings": list(self.warnings),
            "passed": self.passed,
        }


class EpubChecker:
    """Small configurable wrapper around a real EPUBCheck command."""

    def __init__(self, command: Sequence[str] = ("epubcheck",), *, timeout: float = 120.0) -> None:
        if not command:
            raise ValueError("EPUBCheck command cannot be empty")
        self.command = tuple(command)
        self.timeout = timeout

    def check(self, epub_path: Path) -> EpubCheckResult:
        executable = self.command[0]
        if os.sep not in executable and shutil.which(executable) is None:
            raise EpubCheckUnavailable("epubcheck_unavailable", f"EPUBCheck executable not found: {executable}")
        if os.sep in executable and not Path(executable).is_file():
            raise EpubCheckUnavailable("epubcheck_unavailable", f"EPUBCheck executable not found: {executable}")
        if "-jar" in self.command:
            jar_index = self.command.index("-jar") + 1
            if jar_index >= len(self.command) or not Path(self.command[jar_index]).is_file():
                raise EpubCheckUnavailable("epubcheck_unavailable", "Configured EPUBCheck JAR was not found")

        command = (*self.command, str(epub_path))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as error:
            raise EpubCheckUnavailable("epubcheck_unavailable", str(error)) from error
        except subprocess.TimeoutExpired as error:
            raise EpubValidationError("epubcheck_timeout", f"EPUBCheck timed out after {self.timeout}s") from error

        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        fatals, errors, warnings = _classify_epubcheck_output(output)
        if completed.returncode and not (fatals or errors):
            errors = (f"EPUBCheck exited with status {completed.returncode}",)
        return EpubCheckResult(self.command, completed.returncode, errors, fatals, warnings, output)


@dataclass(frozen=True)
class ManifestItem:
    item_id: str
    path: str
    media_type: str
    properties: tuple[str, ...] = ()
    media_overlay: str | None = None


@dataclass(frozen=True)
class PackageInventory:
    source_hash: str
    epub_version: str
    opf_path: str
    entries: Mapping[str, int]
    manifest: tuple[ManifestItem, ...]
    spine: tuple[str, ...]
    spine_linear: Mapping[str, bool]
    documents: tuple[str, ...]
    nav_path: str | None
    ncx_path: str | None
    obfuscated_fonts: tuple[str, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    epubcheck: EpubCheckResult | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_hash": self.source_hash,
            "epub_version": self.epub_version,
            "opf_path": self.opf_path,
            "entries": dict(self.entries),
            "manifest": [item.__dict__ for item in self.manifest],
            "spine": list(self.spine),
            "spine_linear": dict(self.spine_linear),
            "documents": list(self.documents),
            "nav_path": self.nav_path,
            "ncx_path": self.ncx_path,
            "obfuscated_fonts": list(self.obfuscated_fonts),
            "warnings": [issue.__dict__ for issue in self.warnings],
            "epubcheck": self.epubcheck.to_dict() if self.epubcheck else None,
        }


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int = 20_000
    max_entry_size: int = 256 * 1024 * 1024
    max_total_size: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: int = 1_000


DEFAULT_ZIP_LIMITS = ZipLimits()


def inspect_epub(
    source: Path,
    source_hash: str,
    *,
    checker: EpubChecker | object,
    limits: ZipLimits = DEFAULT_ZIP_LIMITS,
    for_output: bool = False,
) -> PackageInventory:
    """Validate an immutable source snapshot and return its complete package inventory."""
    with zipfile.ZipFile(source) as archive:
        entries = _validate_zip(archive, limits)
        rootfiles = _container_rootfiles(_read_xml(archive, "META-INF/container.xml"))
        if len(rootfiles) != 1:
            raise EpubValidationError(
                "multiple_renditions",
                f"Exactly one rootfile is supported, found {len(rootfiles)}",
            )
        opf_path = rootfiles[0]
        if opf_path not in entries:
            raise EpubValidationError("missing_opf", f"Container references missing OPF: {opf_path}")

        opf = _parse_xml_bytes(_read_bytes(archive, opf_path), opf_path)
        epub_version = (opf.attrib.get("version") or "").strip()
        if not re.fullmatch(r"[23](?:\.\d+){0,2}", epub_version):
            raise EpubValidationError("unsupported_epub_version", f"Unsupported EPUB version: {epub_version!r}")

        manifest = _manifest_items(opf, opf_path, entries)
        manifest_by_id = {item.item_id: item for item in manifest}
        spine, spine_linear = _spine_items(opf, manifest_by_id)
        documents = tuple(dict.fromkeys(item.path for item in manifest if item.media_type in XHTML_MEDIA_TYPES))
        nav_path = next((item.path for item in manifest if "nav" in item.properties), None)
        toc_id = next(
            (element.attrib.get("toc") for element in opf.iter() if _local_name(element.tag) == "spine"), None
        )
        ncx_path = (
            manifest_by_id[toc_id].path
            if toc_id in manifest_by_id
            else next((item.path for item in manifest if item.media_type == "application/x-dtbncx+xml"), None)
        )

        blockers = _support_blockers(archive, opf, opf_path, manifest, spine, entries, for_output=for_output)
        if blockers:
            raise EpubValidationError("unsupported_source", blockers[0].message, issues=blockers)
        obfuscated = _obfuscated_fonts(archive, manifest, entries)

        for item in manifest:
            if item.media_type in XML_MEDIA_TYPES:
                _parse_xml_bytes(_read_bytes(archive, item.path), item.path)

    check_result = checker.check(source)  # type: ignore[attr-defined]
    if not isinstance(check_result, EpubCheckResult):
        raise TypeError("checker.check() must return EpubCheckResult")
    from engine.epub.diagnostics import validate

    validate(source, check_result)
    warnings = tuple(
        ValidationIssue(code, line, "warning")
        for code, lines in (
            ("source_epubcheck_fatal", check_result.fatals),
            ("source_epubcheck_error", check_result.errors),
            ("source_epubcheck_warning", check_result.warnings),
        )
        for line in lines
    )
    return PackageInventory(
        source_hash=source_hash,
        epub_version=epub_version,
        opf_path=opf_path,
        entries=entries,
        manifest=manifest,
        spine=spine,
        spine_linear=spine_linear,
        documents=documents,
        nav_path=nav_path,
        ncx_path=ncx_path,
        obfuscated_fonts=obfuscated,
        warnings=warnings,
        epubcheck=check_result,
    )


def validate_internal_references(archive: zipfile.ZipFile, inventory: PackageInventory) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for item in inventory.manifest:
        if item.media_type == "text/css":
            try:
                values = _css_urls(_read_bytes(archive, item.path).decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                issues.append(ValidationIssue("invalid_css", str(error), resource=item.path))
                continue
            _validate_reference_values(item.path, values, archive, inventory, issues)
            continue
        if item.media_type not in XML_MEDIA_TYPES:
            continue
        root = _parse_xml_bytes(_read_bytes(archive, item.path), item.path)
        ids: set[str] = set()
        for element in root.iter():
            element_id = element.attrib.get("id")
            if element_id:
                if element_id in ids:
                    issues.append(ValidationIssue("duplicate_id", f"Duplicate id {element_id!r}", resource=item.path))
                ids.add(element_id)
            values = [value for name, value in element.attrib.items() if _local_name(name) in {"href", "src"}]
            style = element.attrib.get("style")
            if style:
                values.extend(_css_urls(f"x {{{style}}}"))
            if _local_name(element.tag) == "style" and element.text:
                values.extend(_css_urls(element.text))
            _validate_reference_values(item.path, values, archive, inventory, issues)
    return tuple(issues)


def _validate_reference_values(
    source_path: str,
    values: Iterable[str],
    archive: zipfile.ZipFile,
    inventory: PackageInventory,
    issues: list[ValidationIssue],
) -> None:
    for value in values:
        parsed = urlsplit(value.strip())
        if parsed.scheme or parsed.netloc or (not parsed.path and not parsed.fragment):
            continue
        target = _resolve_package_path(source_path, parsed.path) if parsed.path else source_path
        if target not in inventory.entries:
            issues.append(ValidationIssue("missing_reference", f"Missing resource: {value}", resource=source_path))
            continue
        if parsed.fragment and target in inventory.documents:
            target_root = _parse_xml_bytes(_read_bytes(archive, target), target)
            target_ids = {element.attrib["id"] for element in target_root.iter() if "id" in element.attrib}
            if unquote(parsed.fragment) not in target_ids:
                issues.append(ValidationIssue("missing_fragment", f"Missing fragment: {value}", resource=source_path))


def _css_urls(css: str) -> list[str]:
    rules = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
    if any(rule.type == "error" for rule in rules):
        raise ValueError("CSS parse failed")
    urls: list[str] = []

    def walk(nodes: Iterable[object]) -> None:
        for node in nodes:
            if getattr(node, "type", None) == "error":
                raise ValueError("CSS parse failed")
            if getattr(node, "type", None) == "url":
                urls.append(str(node.value))  # type: ignore[attr-defined]
            elif getattr(node, "type", None) == "function" and getattr(node, "lower_name", None) == "url":
                value = tinycss2.serialize(node.arguments).strip().strip("\"'")  # type: ignore[attr-defined]
                if value:
                    urls.append(value)
            for attribute in ("prelude", "content", "arguments"):
                children = getattr(node, attribute, None)
                if children:
                    walk(children)

    walk(rules)
    return urls


def _classify_epubcheck_output(output: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    buckets: dict[str, list[str]] = {"FATAL": [], "ERROR": [], "WARNING": []}
    for line in output.splitlines():
        match = re.match(r"\s*(FATAL|ERROR|WARNING)(?=\(|:|\s)", line, re.IGNORECASE)
        if match:
            buckets[match.group(1).upper()].append(line.strip())
    return tuple(buckets["FATAL"]), tuple(buckets["ERROR"]), tuple(buckets["WARNING"])


def _validate_zip(archive: zipfile.ZipFile, limits: ZipLimits) -> dict[str, int]:
    infos = archive.infolist()
    if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
        raise EpubValidationError("invalid_mimetype_layout", "mimetype must be the first uncompressed ZIP entry")
    if len(infos) > limits.max_entries:
        raise EpubValidationError("too_many_entries", f"EPUB contains {len(infos)} ZIP entries")
    entries: dict[str, int] = {}
    total = 0
    for info in infos:
        name = info.filename
        _validate_package_path(name)
        if name in entries:
            raise EpubValidationError("duplicate_zip_entry", f"Duplicate ZIP entry: {name}")
        mode = info.external_attr >> 16
        if (mode & 0o170000) == 0o120000:
            raise EpubValidationError("zip_symlink", f"Symbolic links are not allowed: {name}")
        if info.flag_bits & 0x1:
            raise EpubValidationError("zip_encrypted", f"ZIP-level encryption is not supported: {name}")
        if info.file_size > limits.max_entry_size:
            raise EpubValidationError("entry_too_large", f"ZIP entry is too large: {name}")
        total += info.file_size
        if total > limits.max_total_size:
            raise EpubValidationError("archive_too_large", "Uncompressed EPUB exceeds configured limit")
        if info.file_size and not info.compress_size:
            raise EpubValidationError("suspicious_compression", f"Invalid compressed size: {name}")
        if info.compress_size and info.file_size / info.compress_size > limits.max_compression_ratio:
            raise EpubValidationError("suspicious_compression", f"Suspicious compression ratio: {name}")
        entries[name] = info.file_size
    if "mimetype" not in entries or archive.read("mimetype") != b"application/epub+zip":
        raise EpubValidationError("invalid_mimetype", "EPUB mimetype entry is missing or invalid")
    return entries


def _validate_package_path(path: str) -> str:
    if not path or "\x00" in path or "\\" in path or "//" in path:
        raise EpubValidationError("unsafe_path", f"Unsafe package path: {path!r}")
    decoded = unquote(path)
    normalized = decoded.removesuffix("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise EpubValidationError("unsafe_path", f"Unsafe package path: {path!r}")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise EpubValidationError("unsafe_path", f"Unsafe package path: {path!r}")
    return candidate.as_posix()


def _read_bytes(archive: zipfile.ZipFile, path: str) -> bytes:
    try:
        return archive.read(path)
    except KeyError as error:
        raise EpubValidationError("missing_resource", f"Missing resource: {path}") from error


def _read_xml(archive: zipfile.ZipFile, path: str) -> ET.Element:
    return _parse_xml_bytes(_read_bytes(archive, path), path)


def _parse_xml_bytes(data: bytes, resource: str) -> ET.Element:
    try:
        return parse_xml_safely(data.decode("utf-8")).getroot()  # type: ignore[return-value]
    except (UnicodeDecodeError, UnsafeMarkupError) as error:
        raise EpubValidationError("invalid_xml", f"Invalid XML in {resource}: {error}") from error


def _container_rootfiles(root: ET.Element) -> tuple[str, ...]:
    paths = []
    for element in root.iter():
        if _local_name(element.tag) != "rootfile":
            continue
        path = _validate_package_path(element.attrib.get("full-path", ""))
        paths.append(path)
    if not paths:
        raise EpubValidationError("missing_rootfile", "container.xml has no rootfile")
    return tuple(paths)


def _manifest_items(root: ET.Element, opf_path: str, entries: Mapping[str, int]) -> tuple[ManifestItem, ...]:
    items: list[ManifestItem] = []
    ids: set[str] = set()
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = element.attrib.get("id", "")
        if not item_id or item_id in ids:
            raise EpubValidationError("invalid_manifest_id", f"Missing or duplicate manifest id: {item_id!r}")
        ids.add(item_id)
        href = element.attrib.get("href", "")
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc or not parsed.path:
            raise EpubValidationError("external_manifest_resource", f"Manifest resource must be local: {href}")
        path = _resolve_package_path(opf_path, parsed.path)
        if path not in entries:
            raise EpubValidationError("missing_manifest_resource", f"Manifest resource is missing: {path}")
        items.append(
            ManifestItem(
                item_id,
                path,
                element.attrib.get("media-type", ""),
                tuple(element.attrib.get("properties", "").split()),
                element.attrib.get("media-overlay"),
            )
        )
    return tuple(items)


def _spine_items(root: ET.Element, manifest: Mapping[str, ManifestItem]) -> tuple[tuple[str, ...], dict[str, bool]]:
    result: list[str] = []
    linear: dict[str, bool] = {}
    for element in root.iter():
        if _local_name(element.tag) != "itemref":
            continue
        item_id = element.attrib.get("idref", "")
        if item_id not in manifest:
            raise EpubValidationError("missing_spine_resource", f"Spine references missing manifest id: {item_id}")
        result.append(item_id)
        linear[item_id] = element.attrib.get("linear", "yes").lower() != "no"
    if not result:
        raise EpubValidationError("empty_spine", "EPUB spine is empty")
    return tuple(result), linear


def _support_blockers(
    archive: zipfile.ZipFile,
    opf: ET.Element,
    opf_path: str,
    manifest: tuple[ManifestItem, ...],
    spine: tuple[str, ...],
    entries: Mapping[str, int],
    *,
    for_output: bool = False,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    manifest_by_id = {item.item_id: item for item in manifest}
    for element in opf.iter():
        name = _local_name(element.tag)
        value = (element.text or element.attrib.get("content", "")).strip().lower()
        if name == "meta" and (
            (element.attrib.get("property") == "rendition:layout" and value == "pre-paginated")
            or (element.attrib.get("name", "").lower() == "fixed-layout" and value in {"true", "yes"})
        ):
            issues.append(ValidationIssue("fixed_layout", "Fixed-layout EPUB is not supported"))
        if name == "itemref" and "rendition:layout-pre-paginated" in element.attrib.get("properties", "").split():
            issues.append(ValidationIssue("fixed_layout", "Fixed-layout spine item is not supported"))

    if "META-INF/signatures.xml" in entries:
        issues.append(ValidationIssue("signature", "Signed EPUB relationships are not supported"))
    if any(item.media_overlay and item.item_id in spine for item in manifest):
        issues.append(ValidationIssue("media_overlay", "Synchronized media overlays are not supported"))
    if not for_output and any("scripted" in item.properties for item in manifest):
        issues.append(ValidationIssue("scripted_content", "Script-generated reading content is not supported"))

    for item_id in spine:
        item = manifest_by_id[item_id]
        if item.media_type not in XHTML_MEDIA_TYPES:
            continue
        root = _parse_xml_bytes(_read_bytes(archive, item.path), item.path)
        if any(_local_name(element.tag) == "script" for element in root.iter()):
            issues.append(ValidationIssue("scripted_content", "Script in reading-order document", resource=item.path))

    encryption_path = "META-INF/encryption.xml"
    if encryption_path in entries:
        encryption = _read_xml(archive, encryption_path)
        item_by_path = {item.path: item for item in manifest}
        for encrypted_data in (
            element for element in encryption.iter() if _local_name(element.tag) == "EncryptedData"
        ):
            algorithm = next(
                (
                    child.attrib.get("Algorithm", "")
                    for child in encrypted_data.iter()
                    if _local_name(child.tag) == "EncryptionMethod"
                ),
                "",
            )
            uri = next(
                (
                    child.attrib.get("URI", "")
                    for child in encrypted_data.iter()
                    if _local_name(child.tag) == "CipherReference"
                ),
                "",
            )
            if not uri:
                issues.append(ValidationIssue("unsupported_encryption", "EncryptedData has no CipherReference"))
                continue
            resource = _encryption_resource_path(uri)
            item = item_by_path.get(resource)
            is_font = bool(
                item
                and (item.media_type.startswith(FONT_MEDIA_PREFIXES) or Path(resource).suffix.lower() in FONT_SUFFIXES)
            )
            if algorithm not in FONT_OBFUSCATION_ALGORITHMS or not is_font:
                issues.append(
                    ValidationIssue("encrypted_content", "Encrypted body/resource is not supported", resource=resource)
                )
    return tuple(issues)


def _obfuscated_fonts(
    archive: zipfile.ZipFile,
    manifest: tuple[ManifestItem, ...],
    entries: Mapping[str, int],
) -> tuple[str, ...]:
    encryption_path = "META-INF/encryption.xml"
    if encryption_path not in entries:
        return ()
    item_by_path = {item.path: item for item in manifest}
    encryption = _read_xml(archive, encryption_path)
    paths: list[str] = []
    for encrypted_data in (element for element in encryption.iter() if _local_name(element.tag) == "EncryptedData"):
        algorithm = next(
            (
                child.attrib.get("Algorithm", "")
                for child in encrypted_data.iter()
                if _local_name(child.tag) == "EncryptionMethod"
            ),
            "",
        )
        uri = next(
            (
                child.attrib.get("URI", "")
                for child in encrypted_data.iter()
                if _local_name(child.tag) == "CipherReference"
            ),
            "",
        )
        if uri and algorithm in FONT_OBFUSCATION_ALGORITHMS:
            resource = _encryption_resource_path(uri)
            if resource in item_by_path:
                paths.append(resource)
    return tuple(paths)


def _resolve_package_path(base_resource: str, href: str) -> str:
    decoded = unquote(href)
    path = PurePosixPath(base_resource).parent.joinpath(decoded)
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise EpubValidationError("unsafe_reference", f"Reference escapes package: {href}")
            parts.pop()
        else:
            parts.append(part)
    return _validate_package_path("/".join(parts))


def _encryption_resource_path(uri: str) -> str:
    decoded = unquote(urlsplit(uri).path).lstrip("/")
    return _validate_package_path(decoded)


def _local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
