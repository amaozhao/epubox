"""Bounded-memory indexing for the record objects in compact JSON shards."""

from __future__ import annotations

import codecs
import hashlib
import json
import mmap
import os
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


class StreamJSONError(ValueError):
    pass


class StaleRecordIndex(StreamJSONError):
    """The backing file no longer matches a previously indexed snapshot."""


def _signature(stat) -> tuple[int, int, int, int]:
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass
class FileSnapshot:
    path: Path
    signature: tuple[int, int, int, int]
    digest: str

    def check(self, handle) -> None:
        current = _signature(os.fstat(handle.fileno()))
        if current == self.signature:
            return
        if current[:3] != self.signature[:3]:
            raise StaleRecordIndex(f"{self.path.name} changed after its record index was built")
        # macOS can update extended attributes without changing any JSON bytes.
        # Keep ctime as a change detector, but prove content equality before
        # accepting a metadata-only change (including restored-mtime edits).
        position = handle.tell()
        try:
            handle.seek(0)
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        finally:
            handle.seek(position)
        after = _signature(os.fstat(handle.fileno()))
        if after != current or digest != self.digest:
            raise StaleRecordIndex(f"{self.path.name} changed after its record index was built")
        self.signature = after

    def check_path(self) -> tuple[str, int, int, int, int]:
        with self.path.open("rb") as handle:
            self.check(handle)
        return (self.path.name, *self.signature)


@dataclass(frozen=True)
class RecordRef:
    path: Path
    start: int
    end: int
    data_start: int
    data_end: int
    stamp: int
    data_size: int
    signature: tuple[int, int, int, int]
    snapshot: FileSnapshot | None = None

    def _check_handle(self, handle) -> None:
        if self.snapshot is not None:
            self.snapshot.check(handle)
            return
        stat = os.fstat(handle.fileno())
        if (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != self.signature:
            raise StaleRecordIndex(f"{self.path.name} changed after its record index was built")

    def read(self, object_hook) -> dict[str, Any]:
        with self.path.open("rb") as handle:
            self._check_handle(handle)
            handle.seek(self.start)
            raw = handle.read(self.end - self.start)
            self._check_handle(handle)
        try:
            value = json.loads(raw, object_pairs_hook=object_hook)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StreamJSONError(f"invalid record in {self.path.name}: {error}") from error
        if not isinstance(value, dict):
            raise StreamJSONError(f"invalid record in {self.path.name}")
        return value

    def copy_to(self, handle) -> None:
        remaining = self.end - self.start
        with self.path.open("rb") as source:
            self._check_handle(source)
            source.seek(self.start)
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise StreamJSONError(f"truncated record in {self.path.name}")
                handle.write(block)
                remaining -= len(block)
            self._check_handle(source)

    def fingerprint(self) -> tuple[int, str]:
        with self.path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            self._check_handle(source)
            return self.stamp, _json_string_digest(raw, self.data_start, self.data_end)


class IndexedRecords(Mapping[str, dict[str, Any]]):
    """A record mapping that decodes payloads only when the caller asks for them."""

    def __init__(self, refs: Mapping[str, RecordRef], object_hook, *, snapshots=None) -> None:
        self._refs = dict(refs)
        self._object_hook = object_hook
        self.snapshots: dict[Path, FileSnapshot] = dict(snapshots or {})

    def __getitem__(self, key: str) -> dict[str, Any]:
        try:
            ref = self._refs[key]
        except KeyError:
            raise KeyError(key) from None
        return ref.read(self._object_hook)

    def __iter__(self) -> Iterator[str]:
        return iter(self._refs)

    def __len__(self) -> int:
        return len(self._refs)

    def __contains__(self, key: object) -> bool:
        return key in self._refs

    def raw(self, key: str) -> RecordRef | None:
        return self._refs.get(key)

    def fingerprint(self, key: str) -> tuple[int, str]:
        return self._refs[key].fingerprint()

    def fingerprints(self, keys: Iterator[str]) -> dict[str, tuple[int, str]]:
        by_path: dict[Path, list[tuple[str, RecordRef]]] = defaultdict(list)
        for key in keys:
            ref = self._refs[key]
            by_path[ref.path].append((key, ref))
        values: dict[str, tuple[int, str]] = {}
        for path, entries in by_path.items():
            with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as raw:
                for _key, ref in entries:
                    ref._check_handle(source)
                for key, ref in entries:
                    try:
                        data = json.loads(raw[ref.data_start : ref.data_end])
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise StreamJSONError(f"invalid record in {path.name}: {error}") from error
                    values[key] = (ref.stamp, hashlib.sha256(data.encode("utf-8")).hexdigest())
        return values

    def metadata(self, key: str) -> tuple[int, int]:
        ref = self._refs[key]
        if ref.data_size >= 0:
            return ref.stamp, ref.data_size
        record = ref.read(self._object_hook)
        return ref.stamp, len(record["data"].encode("utf-8"))

    def refs(self) -> dict[str, RecordRef]:
        return dict(self._refs)


def indexed_object(path: Path, object_hook) -> tuple[dict[str, Any], IndexedRecords]:
    """Parse a shard root while retaining only byte ranges for record values."""
    if path.is_symlink() or not path.is_file():
        raise StreamJSONError(f"{path.name} must be a regular file")
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        signature = (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if before.st_size == 0:
            raise StreamJSONError(f"invalid {path.name}: empty document")
        decoder = codecs.getincrementaldecoder("utf-8")()
        digest = hashlib.sha256()
        while block := handle.read(1024 * 1024):
            decoder.decode(block)
            digest.update(block)
        decoder.decode(b"", final=True)
        handle.seek(0)
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            fields, refs = _indexed_root(raw, path, object_hook, signature)
            values: dict[str, Any] = {}
            for key, (start, end) in fields.items():
                if key == "records":
                    continue
                try:
                    values[key] = json.loads(raw[start:end], object_pairs_hook=object_hook)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise StreamJSONError(f"invalid {path.name}: {error}") from error
        snapshot = FileSnapshot(path, signature, digest.hexdigest())
        try:
            snapshot.check(handle)
            snapshot.check_path()
        except StaleRecordIndex as error:
            raise StaleRecordIndex(f"{path.name} changed while its record index was built") from error
    records = IndexedRecords(
        {key: replace(ref, snapshot=snapshot) for key, ref in refs.items()},
        object_hook,
        snapshots={path: snapshot},
    )
    values["records"] = records
    return values, records


def _indexed_root(
    raw, path: Path, object_hook, signature: tuple[int, int, int, int]
) -> tuple[dict[str, tuple[int, int]], dict[str, RecordRef]]:
    end = len(raw)
    cursor = _space(raw, 0, end)
    if cursor >= end or raw[cursor] != ord("{"):
        raise StreamJSONError(f"{path.name} must contain an object")
    cursor += 1
    fields: dict[str, tuple[int, int]] = {}
    refs: dict[str, RecordRef] = {}
    while True:
        cursor = _space(raw, cursor, end)
        if cursor < end and raw[cursor] == ord("}"):
            cursor = _space(raw, cursor + 1, end)
            if cursor != end:
                raise StreamJSONError(f"invalid {path.name}: trailing data")
            return fields, refs
        key_start = cursor
        key_end = _string_end(raw, cursor, end)
        try:
            key = json.loads(raw[key_start:key_end])
            key.encode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StreamJSONError(f"invalid {path.name}: {error}") from error
        except UnicodeEncodeError as error:
            raise StreamJSONError(f"invalid UTF-8 key in {path.name}") from error
        if key in fields:
            raise StreamJSONError(f"duplicate JSON key: {key!r}")
        cursor = _space(raw, key_end, end)
        if cursor >= end or raw[cursor] != ord(":"):
            raise StreamJSONError(f"invalid {path.name}: missing colon")
        value_start = _space(raw, cursor + 1, end)
        if key == "records":
            value_end, refs = _canonical_records(raw, path, value_start, end, object_hook, signature)
        else:
            value_end = _value_end(raw, value_start, end)
        fields[key] = (value_start, value_end)
        cursor = _space(raw, value_end, end)
        if cursor >= end:
            raise StreamJSONError(f"invalid {path.name}: truncated object")
        if raw[cursor] == ord("}"):
            cursor = _space(raw, cursor + 1, end)
            if cursor != end:
                raise StreamJSONError(f"invalid {path.name}: trailing data")
            return fields, refs
        if raw[cursor] != ord(","):
            raise StreamJSONError(f"invalid {path.name}: missing comma")
        cursor += 1


def _canonical_records(
    raw, path: Path, start: int, end: int, object_hook, signature: tuple[int, int, int, int]
) -> tuple[int, dict[str, RecordRef]]:
    if start >= end or raw[start] != ord("{"):
        raise StreamJSONError(f"{path.name} records must contain an object")
    cursor = start + 1
    refs: dict[str, RecordRef] = {}
    marker = b'","stamp":'
    while True:
        cursor = _space(raw, cursor, end)
        if cursor < end and raw[cursor] == ord("}"):
            return cursor + 1, refs
        key_start = cursor
        key_end = _string_end(raw, key_start, end)
        try:
            key = json.loads(raw[key_start:key_end])
            key.encode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as error:
            raise StreamJSONError(f"invalid record key in {path.name}") from error
        if key in refs:
            raise StreamJSONError(f"duplicate JSON key: {key!r}")
        cursor = key_end
        if raw[cursor : cursor + 9] != b':{"data":':
            raise StreamJSONError(f"non-canonical record in {path.name}: {key!r}")
        record_start = cursor + 1
        data_start = cursor + 9
        if data_start >= end or raw[data_start] != ord('"'):
            raise StreamJSONError(f"invalid record payload in {path.name}: {key!r}")
        stamp_marker = raw.find(marker, data_start + 1, end)
        if stamp_marker < 0:
            raise StreamJSONError(f"invalid record in {path.name}: {key!r}")
        data_end = stamp_marker + 1
        try:
            decoded = json.loads(raw[data_start:data_end])
            if not isinstance(decoded, str):
                raise StreamJSONError(f"invalid record payload in {path.name}: {key!r}")
            data_size = len(decoded.encode("utf-8"))
        except (UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as error:
            raise StreamJSONError(f"invalid record payload in {path.name}: {key!r}") from error
        stamp_start = stamp_marker + len(marker)
        record_end = raw.find(b"}", stamp_start, end)
        if record_end < 0:
            raise StreamJSONError(f"invalid record in {path.name}: {key!r}")
        try:
            stamp = json.loads(raw[stamp_start:record_end])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StreamJSONError(f"invalid record stamp in {path.name}: {key!r}") from error
        if type(stamp) is not int or stamp < 0:
            raise StreamJSONError(f"invalid record stamp in {path.name}: {key!r}")
        record_end += 1
        refs[key] = RecordRef(path, record_start, record_end, data_start, data_end, stamp, data_size, signature)
        cursor = _space(raw, record_end, end)
        if cursor >= end:
            raise StreamJSONError(f"invalid {path.name}: truncated records")
        if raw[cursor] == ord("}"):
            return cursor + 1, refs
        if raw[cursor] != ord(","):
            raise StreamJSONError(f"invalid {path.name}: missing record comma")
        cursor += 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _object_fields(raw, start: int, end: int, name: str) -> dict[str, tuple[int, int]]:
    cursor = _space(raw, start, end)
    if cursor >= end or raw[cursor] != ord("{"):
        raise StreamJSONError(f"{name} must contain an object")
    cursor += 1
    fields: dict[str, tuple[int, int]] = {}
    cursor = _space(raw, cursor, end)
    if cursor < end and raw[cursor] == ord("}"):
        cursor = _space(raw, cursor + 1, end)
        if cursor != end:
            raise StreamJSONError(f"invalid {name}: trailing data")
        return fields
    while cursor < end:
        key_start = cursor
        key_end = _string_end(raw, cursor, end)
        try:
            key = json.loads(raw[key_start:key_end])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StreamJSONError(f"invalid {name}: {error}") from error
        if not isinstance(key, str):
            raise StreamJSONError(f"invalid {name}: object key is not text")
        if key in fields:
            raise StreamJSONError(f"duplicate JSON key: {key!r}")
        cursor = _space(raw, key_end, end)
        if cursor >= end or raw[cursor] != ord(":"):
            raise StreamJSONError(f"invalid {name}: missing colon")
        value_start = _space(raw, cursor + 1, end)
        value_end = _value_end(raw, value_start, end)
        fields[key] = (value_start, value_end)
        cursor = _space(raw, value_end, end)
        if cursor >= end:
            raise StreamJSONError(f"invalid {name}: truncated object")
        if raw[cursor] == ord("}"):
            cursor = _space(raw, cursor + 1, end)
            if cursor != end:
                raise StreamJSONError(f"invalid {name}: trailing data")
            return fields
        if raw[cursor] != ord(","):
            raise StreamJSONError(f"invalid {name}: missing comma")
        cursor = _space(raw, cursor + 1, end)
    raise StreamJSONError(f"invalid {name}: truncated object")


def _validate_record(raw, span: tuple[int, int], name: str, key: str) -> tuple[tuple[int, int], int, int]:
    fields = _object_fields(raw, span[0], span[1], f"{name} record {key!r}")
    if set(fields) != {"data", "stamp"}:
        raise StreamJSONError(f"invalid record in {name}: {key!r}")
    data_start, data_end = fields["data"]
    if raw[data_start] != ord('"') or _string_end(raw, data_start, data_end) != data_end:
        raise StreamJSONError(f"invalid record payload in {name}: {key!r}")
    stamp_start, stamp_end = fields["stamp"]
    try:
        stamp = json.loads(raw[stamp_start:stamp_end])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StreamJSONError(f"invalid record stamp in {name}: {key!r}") from error
    if type(stamp) is not int or stamp < 0:
        raise StreamJSONError(f"invalid record stamp in {name}: {key!r}")
    # Exact payload size is resolved lazily for stat(); calculating it here
    # would walk every escape in every embedded JSON payload during startup.
    return (data_start, data_end), stamp, -1


def _value_end(raw, start: int, end: int) -> int:
    if start >= end:
        raise StreamJSONError("truncated JSON value")
    marker = raw[start]
    if marker == ord('"'):
        return _string_end(raw, start, end)
    if marker in (ord("{"), ord("[")):
        stack = [marker]
        cursor = start + 1
        while cursor < end:
            byte = raw[cursor]
            if byte == ord('"'):
                cursor = _string_end(raw, cursor, end)
                continue
            if byte in (ord("{"), ord("[")):
                stack.append(byte)
            elif byte in (ord("}"), ord("]")):
                expected = ord("}") if stack[-1] == ord("{") else ord("]")
                if byte != expected:
                    raise StreamJSONError("mismatched JSON container")
                stack.pop()
                if not stack:
                    return cursor + 1
            cursor += 1
        raise StreamJSONError("truncated JSON container")
    cursor = start
    while cursor < end and raw[cursor] not in b",}] \t\r\n":
        cursor += 1
    if cursor == start:
        raise StreamJSONError("invalid JSON value")
    return cursor


def _string_end(raw, start: int, end: int) -> int:
    if start >= end or raw[start] != ord('"'):
        raise StreamJSONError("JSON object key must be a string")
    cursor = raw.find(b'"', start + 1, end)
    while cursor >= 0:
        slash = cursor - 1
        while slash >= start and raw[slash] == ord("\\"):
            slash -= 1
        if (cursor - slash - 1) % 2 == 0:
            string_end = cursor + 1
            break
        cursor = raw.find(b'"', cursor + 1, end)
    else:
        raise StreamJSONError("unterminated JSON string")
    # Validate small strings immediately. Large record payloads were emitted by
    # our canonical encoder and are decoded/validated on first access; avoiding
    # a Python-level escape walk here is the main startup CPU saving.
    if string_end - start <= 1_000_000:
        try:
            json.loads(raw[start:string_end])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StreamJSONError(f"invalid JSON string: {error}") from error
    return string_end


def _escape_end(raw, slash: int, end: int) -> int:
    if slash + 1 >= end:
        raise StreamJSONError("unterminated JSON escape")
    marker = raw[slash + 1]
    if marker in b'"\\/bfnrt':
        return slash + 2
    if marker != ord("u") or slash + 6 > end:
        raise StreamJSONError("invalid JSON escape")
    digits = raw[slash + 2 : slash + 6]
    if any(value not in b"0123456789abcdefABCDEF" for value in digits):
        raise StreamJSONError("invalid JSON unicode escape")
    return slash + 6


def _json_string_utf8_size(raw, start: int, end: int) -> int:
    size = 0
    cursor = start + 1
    limit = end - 1
    while cursor < limit:
        slash = raw.find(b"\\", cursor, limit)
        if slash < 0:
            return size + (limit - cursor)
        size += slash - cursor
        marker = raw[slash + 1]
        if marker != ord("u"):
            size += 1
            cursor = slash + 2
            continue
        codepoint = int(raw[slash + 2 : slash + 6], 16)
        cursor = slash + 6
        if 0xD800 <= codepoint <= 0xDBFF:
            if cursor + 6 > limit or raw[cursor : cursor + 2] != b"\\u":
                raise StreamJSONError("unpaired JSON surrogate")
            low = int(raw[cursor + 2 : cursor + 6], 16)
            if not 0xDC00 <= low <= 0xDFFF:
                raise StreamJSONError("unpaired JSON surrogate")
            codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
            cursor += 6
        elif 0xDC00 <= codepoint <= 0xDFFF:
            raise StreamJSONError("unpaired JSON surrogate")
        size += len(chr(codepoint).encode("utf-8"))
    return size


def _json_string_digest(raw, start: int, end: int) -> str:
    digest = hashlib.sha256()
    view = memoryview(raw)
    cursor = start + 1
    limit = end - 1
    escaped = {
        ord('"'): b'"',
        ord("\\"): b"\\",
        ord("/"): b"/",
        ord("b"): b"\b",
        ord("f"): b"\f",
        ord("n"): b"\n",
        ord("r"): b"\r",
        ord("t"): b"\t",
    }
    while cursor < limit:
        slash = raw.find(b"\\", cursor, limit)
        if slash < 0:
            digest.update(view[cursor:limit])
            break
        digest.update(view[cursor:slash])
        marker = raw[slash + 1]
        if marker != ord("u"):
            digest.update(escaped[marker])
            cursor = slash + 2
            continue
        codepoint = int(raw[slash + 2 : slash + 6], 16)
        cursor = slash + 6
        if 0xD800 <= codepoint <= 0xDBFF:
            low = int(raw[cursor + 2 : cursor + 6], 16)
            codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
            cursor += 6
        digest.update(chr(codepoint).encode("utf-8"))
    view.release()
    return digest.hexdigest()


def _space(raw, cursor: int, end: int) -> int:
    while cursor < end and raw[cursor] in b" \t\r\n":
        cursor += 1
    return cursor


__all__ = ["IndexedRecords", "RecordRef", "StreamJSONError", "indexed_object", "sha256_file"]
