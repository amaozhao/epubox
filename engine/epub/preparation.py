"""Deterministic source preparation for the translation pipeline."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from engine.agents.runtime import PROMPT_VERSION, RESOLUTION_PROTOCOL_VERSION, TERM_PROMPT_VERSION
from engine.core.config import settings
from engine.core.markup import parse_xml_safely
from engine.epub.bindings import resolve_derived_navigation
from engine.epub.validation import EpubChecker, PackageInventory, ZipLimits, inspect_epub
from engine.item.atoms import ADAPTER_VERSION as ATOMIC_ADAPTER_VERSION
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.item.atoms import extract_resource
from engine.item.extractor import ADAPTER_VERSION, EXTRACTOR_VERSION, extract_document
from engine.item.structure import select_primary_title
from engine.schemas.contracts import JsonValue, PreparationPlan, canonical_json_bytes
from engine.services import state
from engine.services.atomic import AtomicStore, IdentityMismatch
from engine.services.store import RunStore
from engine.services.terms.inputs import load_atomic_terms, load_user_terms
from engine.services.terms.planning import ATOMIC_TERM_PLANNER_VERSION, TERM_PLANNER_VERSION

_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class PreparationConfig:
    run_id: str | None = None
    expected_source_hash: str | None = None
    user_terms_path: Path | None = None
    auto_extract: bool = True
    extraction_config: dict[str, JsonValue] = field(default_factory=dict)
    translation_config: dict[str, JsonValue] = field(default_factory=dict)
    adapter_version: str = ADAPTER_VERSION
    extractor_version: str = EXTRACTOR_VERSION
    zip_limits: ZipLimits = field(default_factory=ZipLimits)


@dataclass(frozen=True)
class PreparedBook:
    work_dir: Path
    source_snapshot: Path
    inventory: PackageInventory
    preparation: PreparationPlan
    preparation_hash: str


def prepare_book(
    source: Path,
    work_root: Path,
    config: PreparationConfig,
    checker: EpubChecker | object,
) -> PreparedBook:
    """Create the immutable source gate; no model or translation plan is touched."""

    source = source.resolve(strict=True)
    work_root.mkdir(parents=True, exist_ok=True)
    compact = state.compact(work_root)
    temporary_snapshot: Path | None = None
    if compact:
        source_hash = _stable_hash(source)
    else:
        temporary_snapshot, source_hash = _stable_snapshot(source, work_root)
    if config.expected_source_hash is not None and source_hash != config.expected_source_hash:
        if temporary_snapshot is not None:
            temporary_snapshot.unlink(missing_ok=True)
        raise IdentityMismatch("source EPUB identity changed before the immutable snapshot was created")
    run_id = config.run_id or uuid.uuid4().hex
    if run_id in {".", ".."} or not _SAFE_RUN_ID.fullmatch(run_id):
        if temporary_snapshot is not None:
            temporary_snapshot.unlink(missing_ok=True)
        raise ValueError(f"unsafe run id: {run_id!r}")
    if compact:
        header = state.header(work_root)
        recorded = header.get("source")
        if (
            header.get("run_id") != run_id
            or not isinstance(recorded, dict)
            or recorded.get("path") != str(source)
            or recorded.get("hash") != source_hash
        ):
            raise IdentityMismatch("compact state belongs to a different source or run")

    work_dir = work_root if compact else work_root / source_hash / run_id
    snapshot = source if compact else work_dir / "source.epub"
    if not compact:
        assert temporary_snapshot is not None
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            if snapshot.exists():
                if not snapshot.is_file() or _sha256_file(snapshot) != source_hash:
                    raise OSError("Existing preparation snapshot does not match the source")
                temporary_snapshot.unlink()
            else:
                os.replace(temporary_snapshot, snapshot)
                AtomicStore.sync_directory(work_dir)
                snapshot.chmod(0o444)
        except BaseException:
            temporary_snapshot.unlink(missing_ok=True)
            raise

    store = RunStore(work_dir)
    with store.lock(blocking=False):
        inventory = inspect_epub(snapshot, source_hash, checker=checker, limits=config.zip_limits)
        if compact:
            _extract_source(snapshot, work_dir / "source", inventory)
        if inventory.epubcheck is not None and (not inventory.epubcheck.passed or inventory.epubcheck.warnings):
            store._base.atomic_write_bytes(
                work_dir / "report.json",
                canonical_json_bytes(
                    {"source_validation": {"source_hash": source_hash, **inventory.epubcheck.to_dict()}}
                ),
            )
        documents = []
        atomic = _atomic_config(config)
        with zipfile.ZipFile(snapshot) as archive:
            manifest = {item.path: item for item in inventory.manifest}
            styles = {
                item.path: archive.read(item.path).decode("utf-8")
                for item in inventory.manifest
                if item.media_type == "text/css"
            }
            book_title = _book_title(archive.read(inventory.opf_path).decode("utf-8"))
            paths = tuple(dict.fromkeys((*inventory.documents, inventory.ncx_path, inventory.opf_path)))
            for resource_path in (path for path in paths if path):
                item = manifest.get(resource_path)
                media_type = item.media_type if item else _container_media_type(resource_path)
                raw = archive.read(resource_path)
                if atomic:
                    document = extract_resource(
                        raw,
                        resource_path,
                        source_hash,
                        media_type=media_type,
                        config={"book_title": book_title},
                    ).document
                else:
                    document = extract_document(
                        raw.decode("utf-8"),
                        resource_path,
                        source_hash,
                        media_type=media_type,
                        config={"book_title": book_title},
                        styles=styles,
                    )
                if document.adapter_version != config.adapter_version:
                    raise ValueError(
                        f"Adapter version mismatch: {document.adapter_version} != {config.adapter_version}"
                    )
                if document.extractor_version != config.extractor_version:
                    raise ValueError(
                        f"Extractor version mismatch: {document.extractor_version} != {config.extractor_version}"
                    )
                documents.append(document)

        if compact and _sha256_file(snapshot) != source_hash:
            raise IdentityMismatch("source EPUB changed while it was being prepared")

        if not atomic:
            documents = list(resolve_derived_navigation(documents))
        document_hashes = {document.document_id: store.write_document(document) for document in documents}

        by_resource = {document.resource.path: document for document in documents}
        reading_order = tuple(
            by_resource[_manifest_path(inventory, item_id)].document_id for item_id in inventory.spine
        )
        unit_documents = {unit.unit_id: document.document_id for document in documents for unit in document.units}
        if atomic:
            terms, terms_hash = load_atomic_terms(config.user_terms_path, documents)
        else:
            terms, terms_hash = load_user_terms(
                config.user_terms_path,
                document_ids=document_hashes,
                unit_ids=unit_documents,
            )
        store.write_user_terms(terms)
        extraction_config = _frozen_extraction_config(config)
        preparation = PreparationPlan(
            source_hash=source_hash,
            source_path="source.epub",
            source_epub_version=inventory.epub_version,
            run_id=run_id,
            document_hashes=document_hashes,
            reading_order=reading_order,
            unit_documents=unit_documents,
            user_terms=terms,
            user_terms_hash=terms_hash,
            extraction_config=extraction_config,
            translation_config=_frozen_translation_config(config),
        )
        preparation_hash = store.write_preparation(preparation)
        if store.read_preparation() != preparation:
            raise OSError("PreparationPlan round-trip mismatch")
        return PreparedBook(work_dir, snapshot, inventory, preparation, preparation_hash)


def _stable_snapshot(source: Path, work_root: Path) -> tuple[Path, str]:
    temporary = work_root / f".source-{uuid.uuid4().hex}.tmp"
    try:
        for _ in range(3):
            temporary.unlink(missing_ok=True)
            before = _sha256_file(source)
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
            after = _sha256_file(source)
            if before == after == _sha256_file(temporary):
                return temporary, before
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink(missing_ok=True)
    raise OSError("Source EPUB kept changing while creating immutable snapshot")


def _stable_hash(source: Path) -> str:
    for _ in range(3):
        before = source.stat()
        digest = _sha256_file(source)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return digest
    raise OSError("Source EPUB kept changing while its identity was checked")


def _extract_source(source: Path, directory: Path, inventory: PackageInventory) -> None:
    if directory.is_symlink():
        raise IdentityMismatch("extracted source directory cannot be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True)
    expected = {name for name in inventory.entries if not name.endswith("/")}
    explicit_directories = {name.removesuffix("/") for name in inventory.entries if name.endswith("/")}
    allowed_directories = set(explicit_directories)
    for name in inventory.entries:
        relative = PurePosixPath(name.removesuffix("/"))
        allowed_directories.update(parent.as_posix() for parent in relative.parents if parent.as_posix() != ".")
    paths = tuple(directory.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise IdentityMismatch("extracted source cannot contain symbolic links")
    existing = {path.relative_to(directory).as_posix() for path in paths if path.is_file()}
    if existing - expected:
        raise IdentityMismatch("extracted source contains files absent from the original EPUB")
    existing_directories = {path.relative_to(directory).as_posix() for path in paths if path.is_dir()}
    if existing_directories - allowed_directories:
        raise IdentityMismatch("extracted source contains directories absent from the original EPUB")
    for name in sorted(explicit_directories, key=lambda value: (value.count("/"), value)):
        target = directory.joinpath(*name.split("/"))
        if target.exists() and not target.is_dir():
            raise IdentityMismatch(f"extracted source directory collides with a file: {name}")
        target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        for name in sorted(expected):
            target = directory.joinpath(*name.split("/"))
            parents = (target, *target.parents[: len(target.parts) - len(directory.parts)])
            if any(parent.is_symlink() for parent in parents):
                raise IdentityMismatch(f"extracted source path is a symbolic link: {name}")
            data = archive.read(name)
            if target.exists():
                if not target.is_file() or target.read_bytes() != data:
                    raise IdentityMismatch(f"extracted source differs from the original EPUB: {name}")
                continue
            AtomicStore.atomic_write_bytes(target, data)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(inventory: PackageInventory, item_id: str) -> str:
    return next(item.path for item in inventory.manifest if item.item_id == item_id)


def _container_media_type(path: str) -> str:
    if path.endswith(".opf"):
        return "application/oebps-package+xml"
    if path.endswith(".ncx"):
        return "application/x-dtbncx+xml"
    if path.endswith(".xml"):
        return "application/xml"
    return "application/octet-stream"


def _book_title(opf_markup: str) -> str:
    root = parse_xml_safely(opf_markup).getroot()
    title = select_primary_title(root)
    return "" if title is None else "".join(title.itertext()).strip()


def _frozen_extraction_config(config: PreparationConfig) -> dict[str, JsonValue]:
    extraction = dict(config.extraction_config)
    if "auto_extract" in extraction and extraction["auto_extract"] is not config.auto_extract:
        raise ValueError("extraction_config auto_extract conflicts with PreparationConfig")
    extraction["auto_extract"] = config.auto_extract

    expected_strategy = ATOMIC_TERM_PLANNER_VERSION if _atomic_config(config) else TERM_PLANNER_VERSION
    strategy = extraction.get("strategy", expected_strategy)
    if strategy != expected_strategy:
        raise ValueError(f"unsupported terminology extraction strategy: {strategy!r}")
    extraction["strategy"] = strategy

    prompt_version = extraction.get("prompt_version", TERM_PROMPT_VERSION)
    if prompt_version != TERM_PROMPT_VERSION:
        raise ValueError(f"unsupported terminology prompt version: {prompt_version!r}")
    provider = extraction.get("provider", config.translation_config.get("provider", "agnes"))
    if provider not in {"agnes", "cr_proxy"}:
        raise ValueError(f"unsupported terminology provider: {provider!r}")
    extraction["provider"] = provider
    resolution_protocol = extraction.get("resolution_protocol_version", RESOLUTION_PROTOCOL_VERSION)
    if resolution_protocol != RESOLUTION_PROTOCOL_VERSION:
        raise ValueError(f"unsupported terminology resolution protocol: {resolution_protocol!r}")
    extraction["resolution_protocol_version"] = resolution_protocol
    default_model = settings.AGNES_MODEL if provider == "agnes" else settings.CR_PROXY_MODEL
    model = extraction.get("model", config.translation_config.get("model", default_model))
    translation_language = config.translation_config.get("target_language", "zh-Hans")
    target_language = extraction.get("target_language", translation_language)
    if target_language != "zh-Hans" or (
        "target_language" in config.translation_config
        and config.translation_config["target_language"] != target_language
    ):
        raise ValueError(f"unsupported or conflicting target language: {target_language!r}")
    for name, value in {
        "prompt_version": prompt_version,
        "model": model,
        "target_language": target_language,
    }.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"extraction_config {name} must be a non-empty string")
        extraction[name] = value
    for name in (
        "max_output_tokens",
        "run_http_limit",
        "rpm",
        "tpm",
        "concurrency",
        "request_timeout_seconds",
    ):
        if name not in extraction and name in config.translation_config:
            extraction[name] = config.translation_config[name]
    return extraction


def _frozen_translation_config(config: PreparationConfig) -> dict[str, JsonValue]:
    translation = dict(config.translation_config)
    if _atomic_config(config):
        provider = translation.get("provider", "agnes")
        if provider not in {"agnes", "cr_proxy"}:
            raise ValueError(f"unsupported translation provider: {provider!r}")
        default_model = settings.AGNES_MODEL if provider == "agnes" else settings.CR_PROXY_MODEL
        defaults: dict[str, JsonValue] = {
            "provider": provider,
            "model": translation.get("model", default_model),
            "target_language": translation.get("target_language", "zh-Hans"),
            "max_source_tokens": settings.EPUB_CHUNK_MAX_TOKENS,
            "context_tokens": 32_768,
            "max_output_tokens": 4_096,
            "prompt_version": "epubox-members-1",
            "planner_version": "epubox-member-planner-2",
            "input_budget_version": 2,
        }
        defaults["max_input_tokens"] = translation.get("context_tokens", defaults["context_tokens"])
        for name, value in defaults.items():
            translation.setdefault(name, value)
        if translation["planner_version"] == "epubox-member-planner-2":
            translation.setdefault("output_budget_version", 5)
            translation.setdefault("minimum_source_tokens", settings.EPUB_CHUNK_MIN_TOKENS)
        if translation["target_language"] != "zh-Hans":
            raise ValueError(f"unsupported target language: {translation['target_language']!r}")
        for name, expected in {
            "prompt_version": "epubox-members-1",
            "input_budget_version": 2,
        }.items():
            if translation[name] != expected:
                raise ValueError(f"unsupported atomic translation {name}: {translation[name]!r}")
        if translation["planner_version"] not in {"epubox-member-planner-1", "epubox-member-planner-2"}:
            raise ValueError(f"unsupported atomic translation planner_version: {translation['planner_version']!r}")
        if translation["planner_version"] == "epubox-member-planner-2":
            minimum = translation["minimum_source_tokens"]
            if type(minimum) is not int or minimum < 1:
                raise ValueError("translation_config minimum_source_tokens must be a positive integer")
        for name in ("max_source_tokens", "context_tokens", "max_input_tokens", "max_output_tokens"):
            value = translation[name]
            if type(value) is not int or value < 1:
                raise ValueError(f"translation_config {name} must be a positive integer")
        for name in ("provider", "model", "target_language"):
            if not isinstance(translation[name], str) or not translation[name]:
                raise ValueError(f"translation_config {name} must be a non-empty string")
        return translation
    prompt_version = translation.get("prompt_version", PROMPT_VERSION)
    if prompt_version != PROMPT_VERSION:
        raise ValueError(f"unsupported translation prompt version: {prompt_version!r}")
    translation["prompt_version"] = PROMPT_VERSION
    return translation


def _atomic_config(config: PreparationConfig) -> bool:
    atomic_adapter = config.adapter_version == ATOMIC_ADAPTER_VERSION
    atomic_extractor = config.extractor_version == ATOMIC_EXTRACTOR_VERSION
    if atomic_adapter != atomic_extractor:
        raise ValueError("atomic adapter and extractor versions must be selected together")
    return atomic_adapter


__all__ = ["PreparationConfig", "PreparedBook", "prepare_book"]
