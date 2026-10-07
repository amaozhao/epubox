"""Locate an existing book session and reopen unfinished atomic work."""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from engine.epub.preparation import PreparationConfig, _frozen_extraction_config, _frozen_translation_config
from engine.item.atoms import EXTRACTOR_VERSION as ATOMIC_EXTRACTOR_VERSION
from engine.schemas.contracts import ItemStatus, canonical_json_bytes, strict_json_loads
from engine.services import state
from engine.services.atomic import AtomicStore, IdentityMismatch
from engine.services.store import RunStore
from engine.services.terms.inputs import load_atomic_terms, load_user_terms

_SOURCE_FIELDS = {"format", "run_id", "source_hash", "original_path", "st_dev", "st_ino"}
_ALIAS_FIELDS = {"original_path", "source_hash", "st_dev", "st_ino"}


def fingerprint(path: Path) -> tuple[str, Any]:
    """Hash one stable file identity without trusting a stale caller digest."""
    path = path.resolve(strict=True)
    before = state.stat(path)
    digest = hashlib.sha256(state.read(path)).hexdigest()
    after = state.stat(path)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise IdentityMismatch("source changed while its session identity was read")
    return digest, after


def source_record(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    if path.is_symlink():
        raise IdentityMismatch("original source record must not be a symbolic link")
    value = strict_json_loads(state.read(path))
    if not isinstance(value, dict):
        raise IdentityMismatch("original source record has an invalid schema")
    keys = set(value)
    if keys != _SOURCE_FIELDS and keys != _SOURCE_FIELDS | {"aliases"}:
        raise IdentityMismatch("original source record has an invalid schema")
    if any(value.get(name) != wanted for name, wanted in expected.items()):
        raise IdentityMismatch("original source record differs from the frozen run")
    _source_entry(value, _SOURCE_FIELDS)
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list):
        raise IdentityMismatch("original source aliases must be a list")
    paths: set[str] = set()
    primary = value["original_path"]
    for alias in aliases:
        if not isinstance(alias, dict):
            raise IdentityMismatch("original source alias must be an object")
        _source_entry(alias, _ALIAS_FIELDS)
        original_path = alias["original_path"]
        if not isinstance(original_path, str):
            raise IdentityMismatch("original source alias path must be a string")
        if original_path == primary or original_path in paths:
            raise IdentityMismatch("original source aliases contain a duplicate path")
        paths.add(original_path)
    return value


def _source_entry(value: Mapping[str, Any], fields: set[str]) -> None:
    path = value.get("original_path")
    if (
        not fields.issubset(value)
        or not isinstance(path, str)
        or not Path(path).is_absolute()
        or Path(path).resolve(strict=False) != Path(path)
    ):
        raise IdentityMismatch("original source identity is incomplete")
    digest = value["source_hash"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise IdentityMismatch("original source hash is invalid")
    if any(type(value[name]) is not int or value[name] < 0 for name in ("st_dev", "st_ino")):
        raise IdentityMismatch("original source inode is invalid")


def remember(source: Path, work_dir: Path) -> None:
    root = source.with_name(source.stem).resolve()
    if work_dir.is_symlink():
        raise IdentityMismatch("book session must not be a symbolic link")
    work_dir = work_dir.resolve(strict=True)
    if state.compact(root):
        current_hash, current_stat = fingerprint(source)
        identity = state.header(root)
        original = identity.get("source")
        if (
            work_dir != root
            or not isinstance(original, dict)
            or original.get("path") != str(source.resolve(strict=True))
            or original.get("hash") != current_hash
            or original.get("st_dev") != current_stat.st_dev
            or original.get("st_ino") != current_stat.st_ino
        ):
            raise IdentityMismatch("book session identity differs from the compact checkpoint")
        if state.exists(root / "preparation.json"):
            preparation = RunStore(root).read_preparation()
            if (preparation.source_hash, preparation.run_id) != (
                original.get("hash"),
                identity.get("run_id"),
            ):
                raise IdentityMismatch("book session preparation differs from the compact checkpoint")
        return
    if not work_dir.is_relative_to(root) or work_dir.is_symlink():
        raise IdentityMismatch("book session must remain inside the source book directory")
    if not state.is_dir(work_dir):
        raise IdentityMismatch("book session directory is missing")
    preparation_path = work_dir / "preparation.json"
    if preparation_path.is_symlink():
        raise IdentityMismatch("book session preparation must not be a symbolic link")
    preparation = RunStore(work_dir).read_preparation()
    if work_dir.name != preparation.run_id or work_dir.parent.name != preparation.source_hash:
        raise IdentityMismatch("book session identity differs from its directory")
    snapshot = state.snapshot(work_dir)
    if snapshot.is_symlink() or fingerprint(snapshot)[0] != preparation.source_hash:
        raise IdentityMismatch("book session source snapshot changed")
    original_hash, original_stat = fingerprint(source)
    hint_path = work_dir / "source.json"
    if state.exists(hint_path) or hint_path.is_symlink():
        hint = source_record(
            hint_path,
            {"format": "epubox-source-1", "run_id": preparation.run_id, "source_hash": preparation.source_hash},
        )
        original_path = str(source.resolve(strict=True))
        if original_path == hint["original_path"]:
            if original_hash != hint["source_hash"]:
                raise IdentityMismatch("original source changed before session registration")
        else:
            alias = {
                "original_path": original_path,
                "source_hash": original_hash,
                "st_dev": original_stat.st_dev,
                "st_ino": original_stat.st_ino,
            }
            aliases = {value["original_path"]: value for value in hint.get("aliases", [])}
            if original_path in aliases and aliases[original_path] != alias:
                raise IdentityMismatch("original source alias changed")
            aliases[original_path] = alias
            AtomicStore.atomic_write_bytes(hint_path, canonical_json_bytes(hint | {"aliases": list(aliases.values())}))
    elif original_hash != preparation.source_hash:
        raise IdentityMismatch("repaired snapshot requires its primary original source record")
    active = root / "active.json"
    if active.is_symlink():
        raise IdentityMismatch("book session index must not be a symbolic link")
    AtomicStore.atomic_write_bytes(
        active,
        canonical_json_bytes(
            {
                "format": "epubox-session-1",
                "original_hash": original_hash,
                "snapshot_hash": preparation.source_hash,
                "work_dir": str(work_dir.relative_to(root)),
            }
        ),
    )


def find(source: Path, source_hash: str | None = None) -> Path | None:
    root = source.with_name(source.stem)
    if state.compact(root):
        current_hash, current_stat = fingerprint(source)
        if source_hash is not None and source_hash != current_hash:
            raise IdentityMismatch("original source changed while locating its active session")
        identity = state.header(root)
        original = identity.get("source")
        if (
            not isinstance(original, dict)
            or original.get("path") != str(source.resolve(strict=True))
            or original.get("hash") != current_hash
            or original.get("st_dev") != current_stat.st_dev
            or original.get("st_ino") != current_stat.st_ino
            or not isinstance(identity.get("run_id"), str)
        ):
            raise IdentityMismatch("compact book session does not match the original source")
        if state.exists(root / "preparation.json"):
            preparation = RunStore(root).read_preparation()
            if (preparation.source_hash, preparation.run_id) != (current_hash, identity["run_id"]):
                raise IdentityMismatch("compact preparation identity changed")
        return root
    path = root / "active.json"
    if path.is_symlink():
        raise IdentityMismatch("book session index must not be a symbolic link")
    if not state.exists(path):
        return None
    current_hash, current_stat = fingerprint(source)
    if source_hash is not None and source_hash != current_hash:
        raise IdentityMismatch("original source changed while locating its active session")
    value = strict_json_loads(state.read(path))
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "original_hash", "snapshot_hash", "work_dir"}
        or value["format"] != "epubox-session-1"
        or value["original_hash"] != current_hash
        or not isinstance(value["work_dir"], str)
    ):
        raise IdentityMismatch("book session index does not match the original source")
    relative = Path(value["work_dir"])
    if relative.is_absolute() or ".." in relative.parts:
        raise IdentityMismatch("book session index escapes the book directory")
    work_dir = root / relative
    if any(part.is_symlink() for part in (root, work_dir, *work_dir.parents) if part.is_relative_to(root)):
        raise IdentityMismatch("book session path must not contain symbolic links")
    preparation_path = work_dir / "preparation.json"
    if preparation_path.is_symlink():
        raise IdentityMismatch("book session preparation must not be a symbolic link")
    if not state.is_dir(work_dir) or not state.is_file(preparation_path):
        raise IdentityMismatch("book session checkpoint is missing")
    preparation = RunStore(work_dir).read_preparation()
    snapshot = state.snapshot(work_dir)
    if snapshot.is_symlink():
        raise IdentityMismatch("book session source snapshot must not be a symbolic link")
    if (
        preparation.source_hash != value["snapshot_hash"]
        or work_dir.name != preparation.run_id
        or work_dir.parent.name != preparation.source_hash
        or fingerprint(snapshot)[0] != preparation.source_hash
    ):
        raise IdentityMismatch("book session snapshot or run identity changed")
    if value["original_hash"] != value["snapshot_hash"]:
        hint_path = work_dir / "source.json"
        if hint_path.is_symlink() or not state.is_file(hint_path):
            raise IdentityMismatch("mixed-source book session is missing its protected source record")
        hint = source_record(
            hint_path,
            {"format": "epubox-source-1", "run_id": preparation.run_id, "source_hash": preparation.source_hash},
        )
        original_path = str(source.resolve(strict=True))
        protected = any(
            alias.get("original_path") == original_path
            and alias.get("source_hash") == current_hash
            and alias.get("st_dev") == current_stat.st_dev
            and alias.get("st_ino") == current_stat.st_ino
            for alias in hint.get("aliases", [])
        )
        if not protected:
            raise IdentityMismatch("book session does not protect the logical original source")
    return work_dir


def infer(options: Mapping[str, Any]) -> frozenset[str]:
    defaults = {
        "glossary": None,
        "auto_extract": True,
        "provider": "agnes",
        "context_tokens": 32768,
        "max_input_tokens": 50000,
        "max_output_tokens": 8192,
        "limit": None,
        "http_limit": 0,
        "concurrency": 2,
        "repair_terms": False,
    }
    return frozenset(name for name, default in defaults.items() if options.get(name) != default)


def validate_options(work_dir: Path, config: PreparationConfig, explicit: frozenset[str]) -> None:
    """Reject only explicitly requested values that differ from the frozen active run."""
    if not explicit:
        return
    if "repair_terms" in explicit:
        raise ValueError("--repair-terms cannot replace an active book session")
    store = RunStore(work_dir)
    preparation, documents = store._trusted_preparation_documents()
    expected_extraction = _frozen_extraction_config(config)
    expected_translation = _frozen_translation_config(config)
    extraction_fields = {
        "auto_extract": ("auto_extract",),
        "provider": ("provider", "model"),
        "max_output_tokens": ("max_output_tokens",),
        "http_limit": ("run_http_limit",),
        "concurrency": ("concurrency",),
    }
    translation_fields = {
        "provider": ("provider", "model"),
        "context_tokens": ("context_tokens",),
        "max_input_tokens": ("max_input_tokens",),
        "max_output_tokens": ("max_output_tokens",),
        "limit": ("max_source_tokens",),
        "http_limit": ("run_http_limit",),
        "concurrency": ("concurrency",),
    }
    for option in explicit:
        for field in extraction_fields.get(option, ()):
            if preparation.extraction_config.get(field) != expected_extraction.get(field):
                raise ValueError(f"active session conflicts with explicit --{option.replace('_', '-')}")
        for field in translation_fields.get(option, ()):
            if preparation.translation_config.get(field) != expected_translation.get(field):
                raise ValueError(f"active session conflicts with explicit --{option.replace('_', '-')}")
    if "glossary" in explicit:
        atomic = {document.extractor_version for document in documents.values()} == {ATOMIC_EXTRACTOR_VERSION}
        if atomic:
            terms, digest = load_atomic_terms(config.user_terms_path, tuple(documents.values()))
        else:
            terms, digest = load_user_terms(
                config.user_terms_path,
                document_ids=preparation.document_hashes,
                unit_ids=preparation.unit_documents,
            )
        if terms != preparation.user_terms or digest != preparation.user_terms_hash:
            raise ValueError("active session conflicts with explicit --glossary")


def reopen(work_dir: Path) -> tuple[str, ...]:
    if not state.is_file(work_dir / "prepared.json"):
        return ()
    from engine.services.journal import BodyJournal

    store = RunStore(work_dir)
    with store.lock(blocking=False):
        journal = BodyJournal(store)
        if journal.session.prepared.plan.translation_config.get("output_budget_version", 2) not in {3, 4, 5, 6}:
            return ()
        records = journal.recover_results()
        units = {
            unit_id
            for unit_id, item_ids in journal.session.prepared.plan.unit_members.items()
            if any(records[item_id].status == ItemStatus.NEEDS_ATTENTION for item_id in item_ids)
        }
        for request in journal._requests.values():
            if request.stage in {"translate", "review"} and journal._ambiguous(request):
                units.update(unit_id for ids in request.item_unit_ids.values() for unit_id in ids)
        return journal.retry_units(tuple(sorted(units))) if units else ()
