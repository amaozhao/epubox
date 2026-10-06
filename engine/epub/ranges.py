from __future__ import annotations

import hashlib
import html.entities
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Any
from xml.parsers import expat

from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import _TRUSTED_XHTML_DTDS, _xhtml_entities_dtd
from engine.epub.parsing import ParsedResource
from engine.schemas.bridge import ByteSpan, SourceLocation, SourceMap
from engine.schemas.source import DocumentPlan, SourceRef, SourceSlot


class RangeError(ValueError):
    """Raised when lexical XML positions cannot be proven against the semantic tree."""


@dataclass(frozen=True, order=True)
class RawSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise RangeError("raw byte span must be ordered and non-negative")


@dataclass(frozen=True)
class CharSpan:
    text: str
    raw: RawSpan


@dataclass(frozen=True)
class NodeSpan:
    path: tuple[int, ...]
    qname: str
    full: RawSpan
    starttag: RawSpan
    content: RawSpan
    endtag: RawSpan
    self_closing: bool


@dataclass(frozen=True)
class SlotSpan:
    path: tuple[int, ...]
    field: str
    text: str
    chars: tuple[CharSpan, ...]
    attribute_name: str | None = None
    special_index: int | None = None

    def source_span(self, start: int, end: int) -> ByteSpan:
        if start < 0 or end > len(self.text) or start >= end:
            raise RangeError("decoded slot range is empty or out of bounds")
        selected = self.chars[start:end]
        if "".join(part.text for part in selected) != self.text[start:end]:
            raise RangeError("decoded character map is inconsistent")
        if start and self.chars[start - 1].raw == selected[0].raw:
            raise RangeError("range starts inside one lexical entity")
        if end < len(self.chars) and selected[-1].raw == self.chars[end].raw:
            raise RangeError("range ends inside one lexical entity")
        unique = [part.raw for index, part in enumerate(selected) if not index or part.raw != selected[index - 1].raw]
        if any(left.end != right.start for left, right in pairwise(unique)):
            raise RangeError("range crosses non-text XML syntax")
        return ByteSpan(byte_start=unique[0].start, byte_end=unique[-1].end)


@dataclass(frozen=True)
class ResourceIndex:
    raw: bytes
    text: str
    encoding: str
    nodes: dict[tuple[int, ...], NodeSpan]
    slots: tuple[SlotSpan, ...]
    specials: dict[tuple[tuple[int, ...], int, str], RawSpan] = field(default_factory=dict)

    def replay(self) -> bytes:
        return self.raw

    def bind_document(self, document: DocumentPlan) -> SourceMap:
        if document.source_markup != self.text:
            raise RangeError("DocumentPlan text differs from the indexed source bytes")
        expected_nodes = {record.element_path: record for record in document.nodes.values()}
        if set(expected_nodes) != set(self.nodes):
            raise RangeError("DocumentPlan element paths differ from the indexed XML")
        for path, record in expected_nodes.items():
            if record.qname != self.nodes[path].qname:
                raise RangeError(f"QName differs at element path {path}")

        special: dict[str, tuple[str, int]] = {}
        for boundary in document.boundaries:
            if boundary.get("kind") != "non_element_tail":
                continue
            slot_id, parent, index = (
                boundary.get("slot_id"),
                boundary.get("parent_node_key"),
                boundary.get("child_index"),
            )
            if not isinstance(slot_id, str) or not isinstance(parent, str) or type(index) is not int:
                raise RangeError("invalid non-element tail boundary")
            special[slot_id] = (parent, index)
        lexical_slots: dict[tuple[tuple[int, ...], str, str | None, int | None], SlotSpan] = {}
        for item in self.slots:
            key = (item.path, item.field, item.attribute_name, item.special_index)
            if key in lexical_slots:
                raise RangeError(f"duplicate lexical source at element path {item.path}")
            lexical_slots[key] = item
        by_path = {record.node_key: record.element_path for record in document.nodes.values()}
        locations: list[SourceLocation] = []
        for slot in document.source_slots.values():
            path, special_index = self._slot_owner(slot, special, by_path)
            lexical = self._find_slot(lexical_slots, path, slot, special_index)
            if lexical.text != slot.source_value:
                raise RangeError(f"decoded source differs for slot {slot.slot_id}")
            for owned in slot.ranges:
                if owned.owner_kind != "unit" or owned.start == owned.end:
                    continue
                locations.append(
                    SourceLocation(
                        node_key=slot.node_key,
                        source_ref=SourceRef(slot_id=slot.slot_id, start=owned.start, end=owned.end),
                        byte_span=lexical.source_span(owned.start, owned.end),
                        field=slot.field,
                        attribute_name=slot.attribute_name,
                    )
                )

        locations.sort(key=lambda item: item.byte_span.byte_start)
        protected = _complement(len(self.raw), [item.byte_span for item in locations])
        return SourceMap(
            document_id=document.document_id,
            source_hash=document.source_hash,
            document_hash=hashlib.sha256(self.raw).hexdigest(),
            encoding=self.encoding,
            source_size=len(self.raw),
            node_spans={
                record.node_key: ByteSpan(byte_start=self.nodes[path].full.start, byte_end=self.nodes[path].full.end)
                for path, record in expected_nodes.items()
            },
            locations=tuple(locations),
            protected_spans=protected,
        )

    @staticmethod
    def _slot_owner(
        slot: SourceSlot,
        special: dict[str, tuple[str, int]],
        by_key: dict[str, tuple[int, ...]],
    ) -> tuple[tuple[int, ...], int | None]:
        if slot.slot_id in special:
            parent, index = special[slot.slot_id]
            if parent != slot.node_key:
                raise RangeError(f"special tail owner differs for slot {slot.slot_id}")
            return by_key[parent], index
        try:
            return by_key[slot.node_key], None
        except KeyError as exc:
            raise RangeError(f"unknown slot node {slot.node_key}") from exc

    @staticmethod
    def _find_slot(
        slots: dict[tuple[tuple[int, ...], str, str | None, int | None], SlotSpan],
        path: tuple[int, ...],
        slot: SourceSlot,
        special_index: int | None,
    ) -> SlotSpan:
        try:
            return slots[(path, slot.field, slot.attribute_name, special_index)]
        except KeyError as exc:
            raise RangeError(f"no lexical source for slot {slot.slot_id}") from exc


@dataclass
class _Frame:
    path: tuple[int, ...]
    qname: str
    starttag: RawSpan
    self_closing: bool
    next_element: int = 0
    next_raw_child: int = 0
    last_child: tuple[str, tuple[int, ...], int] | None = None
    parent_raw_index: int | None = None


@dataclass
class _Builder:
    parsed: ParsedResource
    offsets: tuple[int, ...]
    byte_to_char: dict[int, int]
    nodes: dict[tuple[int, ...], NodeSpan] = field(default_factory=dict)
    specials: dict[tuple[tuple[int, ...], int, str], RawSpan] = field(default_factory=dict)
    parts: dict[tuple[tuple[int, ...], str, str | None, int | None], list[CharSpan]] = field(default_factory=dict)
    stack: list[_Frame] = field(default_factory=list)
    cdata: bool = False

    def start(self, parser: Any, name: str, attrs: list[str] | dict[str, str]) -> None:
        if not isinstance(attrs, list):
            raise RangeError("Expat ordered attributes are unavailable")
        byte_start = parser.CurrentByteIndex
        char_start = self._char(byte_start)
        char_end = _tag_end(self.parsed.text, char_start)
        starttag = RawSpan(byte_start, self.offsets[char_end])
        self_closing = self.parsed.text[char_start:char_end].rstrip().endswith("/>")
        if self.stack:
            parent = self.stack[-1]
            path = (*parent.path, parent.next_element)
            raw_index = parent.next_raw_child
            parent.next_element += 1
            parent.next_raw_child += 1
            parent.last_child = None
        else:
            path, raw_index = (), -1
        qname = _qname(name)
        frame = _Frame(path, qname, starttag, self_closing, parent_raw_index=None if raw_index < 0 else raw_index)
        self.stack.append(frame)
        self._attributes(path, char_start, char_end, attrs)

    def end(self, parser: Any, name: str) -> None:
        if not self.stack:
            raise RangeError("unexpected XML end event")
        frame = self.stack.pop()
        if frame.qname != _qname(name):
            raise RangeError(f"XML end QName differs at {frame.path}")
        if frame.self_closing:
            endtag = RawSpan(frame.starttag.end, frame.starttag.end)
            content = endtag
            full = frame.starttag
        else:
            end_start = parser.CurrentByteIndex
            end_char = _tag_end(self.parsed.text, self._char(end_start))
            endtag = RawSpan(end_start, self.offsets[end_char])
            content = RawSpan(frame.starttag.end, endtag.start)
            full = RawSpan(frame.starttag.start, endtag.end)
        self.nodes[frame.path] = NodeSpan(
            frame.path, frame.qname, full, frame.starttag, content, endtag, frame.self_closing
        )
        if self.stack:
            parent = self.stack[-1]
            if frame.parent_raw_index is None:
                raise RangeError("child element has no lexical sibling index")
            parent.last_child = ("element", frame.path, frame.parent_raw_index)

    def comment(self, parser: Any) -> None:
        self._special_child(parser, "comment", "-->")

    def pi(self, parser: Any) -> None:
        self._special_child(parser, "pi", "?>")

    def _special_child(self, parser: Any, kind: str, terminator: str) -> None:
        if not self.stack:
            return
        parent = self.stack[-1]
        raw_index = parent.next_raw_child
        parent.next_raw_child += 1
        parent.last_child = (kind, parent.path, raw_index)
        start = parser.CurrentByteIndex
        char_start = self._char(start)
        char_end = self.parsed.text.find(terminator, char_start)
        if char_end < 0:
            raise RangeError(f"unterminated XML {kind}")
        self.specials[(parent.path, raw_index, kind)] = RawSpan(start, self.offsets[char_end + len(terminator)])

    def text_event(self, parser: Any, value: str) -> None:
        if not value or not self.stack:
            return
        key = self._content_key()
        if key is None:
            return
        chars = self._text_chars(parser.CurrentByteIndex, value, attribute=False)
        self.parts.setdefault(key, []).extend(chars)

    def _content_key(self) -> tuple[tuple[int, ...], str, str | None, int | None] | None:
        if not self.stack:
            return None
        frame = self.stack[-1]
        if frame.last_child is None:
            return (frame.path, "text", None, None)
        elif frame.last_child[0] == "element":
            return (frame.last_child[1], "tail", None, None)
        return (frame.path, "tail", None, frame.last_child[2])

    def _attributes(self, path: tuple[int, ...], start: int, end: int, attrs: list[str]) -> None:
        lexical = _attributes(self.parsed.text, start, end)
        ordinary = [item for item in lexical if item[0] != "xmlns" and not item[0].startswith("xmlns:")]
        ordered = list(zip(attrs[::2], attrs[1::2], strict=True))
        if len(ordinary) != len(ordered):
            raise RangeError(f"attribute inventory differs at element path {path}")
        for (_, value_start, value_end), (name, expected) in zip(ordinary, ordered, strict=True):
            chars = self._value_chars(value_start, value_end, attribute=True)
            actual = "".join(part.text for part in chars)
            if actual != expected:
                raise RangeError(f"attribute decoding differs at element path {path}: {name}")
            self.parts[(path, "attribute", _qname(name), None)] = chars

    def _text_chars(self, byte_start: int, expected: str, *, attribute: bool) -> list[CharSpan]:
        start = self._char(byte_start)
        return self._consume(start, None, expected, attribute=attribute, cdata=self.cdata)

    def _value_chars(self, start: int, end: int, *, attribute: bool) -> list[CharSpan]:
        return self._consume(start, end, None, attribute=attribute, cdata=False)

    def _consume(
        self,
        start: int,
        end: int | None,
        expected: str | None,
        *,
        attribute: bool,
        cdata: bool,
    ) -> list[CharSpan]:
        result: list[CharSpan] = []
        position = start
        target = expected or ""
        decoded_length = 0
        while (end is None or position < end) and (expected is None or decoded_length < len(target)):
            if not cdata and self.parsed.text[position] == "&":
                close = self.parsed.text.find(";", position + 1, end)
                if close < 0:
                    raise RangeError("unterminated XML entity")
                decoded = _entity(self.parsed.text[position + 1 : close])
                raw = RawSpan(self.offsets[position], self.offsets[close + 1])
                result.extend(CharSpan(char, raw) for char in decoded)
                decoded_length += len(decoded)
                position = close + 1
                continue
            char = self.parsed.text[position]
            next_position = position + 1
            decoded = char
            if char == "\r":
                if next_position < len(self.parsed.text) and self.parsed.text[next_position] == "\n":
                    next_position += 1
                decoded = " " if attribute else "\n"
            elif attribute and char in "\n\t":
                decoded = " "
            raw = RawSpan(self.offsets[position], self.offsets[next_position])
            result.append(CharSpan(decoded, raw))
            decoded_length += len(decoded)
            position = next_position
        actual = "".join(part.text for part in result)
        if expected is not None and actual != expected:
            raise RangeError(f"Expat text event differs from lexical XML: {expected!r} != {actual!r}")
        if end is not None and position != end:
            raise RangeError("attribute value did not consume its lexical source")
        return result

    def _char(self, byte_offset: int) -> int:
        try:
            return self.byte_to_char[byte_offset]
        except KeyError as exc:
            raise RangeError(f"Expat byte index {byte_offset} is not a decoded character boundary") from exc

    def set_cdata(self, value: bool) -> None:
        self.cdata = value

    def start_cdata(self) -> None:
        key = self._content_key()
        if key is not None:
            self.parts.setdefault(key, [])
        self.cdata = True


def index_resource(parsed: ParsedResource) -> ResourceIndex:
    if parsed.kind == "html" or parsed.tree is None:
        raise RangeError("genuine HTML has no verified raw-byte mapping")
    offsets = _offsets(parsed)
    builder = _Builder(parsed, offsets, {value: index for index, value in enumerate(offsets)})
    parser = expat.ParserCreate(_expat_encoding(parsed.encoding), "}")
    parser.buffer_text = False
    parser.ordered_attributes = True
    parser.specified_attributes = True
    parser.StartElementHandler = lambda name, attrs: builder.start(parser, name, attrs)
    parser.EndElementHandler = lambda name: builder.end(parser, name)
    parser.CharacterDataHandler = lambda value: builder.text_event(parser, value)
    parser.CommentHandler = lambda _value: builder.comment(parser)
    parser.ProcessingInstructionHandler = lambda _target, _value: builder.pi(parser)
    parser.StartCdataSectionHandler = builder.start_cdata
    parser.EndCdataSectionHandler = lambda: builder.set_cdata(False)
    parser.ExternalEntityRefHandler = lambda context, base, system, public: _external(parser, context, system, public)
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_ALWAYS)
    try:
        parser.Parse(parsed.raw, True)
    except (expat.ExpatError, OSError, RangeError, ValueError) as exc:
        raise RangeError(f"cannot build verified XML byte ranges: {exc}") from exc
    if builder.stack:
        raise RangeError("XML range parser ended with open elements")
    _validate_tree(parsed.tree, builder.nodes)
    slots = tuple(
        SlotSpan(path, field_name, "".join(char.text for char in chars), tuple(chars), attribute, special)
        for (path, field_name, attribute, special), chars in builder.parts.items()
    )
    return ResourceIndex(parsed.raw, parsed.text, parsed.encoding, builder.nodes, slots, builder.specials)


def _offsets(parsed: ParsedResource) -> tuple[int, ...]:
    bom = len(parsed.raw) - len(parsed.text.encode(parsed.encoding))
    if bom < 0 or parsed.raw[:bom] + parsed.text.encode(parsed.encoding) != parsed.raw:
        raise RangeError(f"{parsed.encoding} cannot provide stable source byte boundaries")
    result = [bom]
    position = bom
    for char in parsed.text:
        position += len(char.encode(parsed.encoding))
        result.append(position)
    return tuple(result)


def _expat_encoding(encoding: str) -> str:
    return {"utf-8": "UTF-8", "utf-16-le": "UTF-16LE", "utf-16-be": "UTF-16BE"}[encoding]


def _tag_end(text: str, start: int) -> int:
    quote: str | None = None
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ">":
            return index + 1
    raise RangeError("unterminated XML tag")


def _attributes(text: str, start: int, end: int) -> list[tuple[str, int, int]]:
    position = start + 1
    while position < end and not text[position].isspace() and text[position] not in "/>":
        position += 1
    result: list[tuple[str, int, int]] = []
    while position < end:
        while position < end and text[position].isspace():
            position += 1
        if position >= end or text[position] in "/>":
            break
        name_start = position
        while position < end and not text[position].isspace() and text[position] not in "=/>":
            position += 1
        name = text[name_start:position]
        while position < end and text[position].isspace():
            position += 1
        if position >= end or text[position] != "=":
            raise RangeError(f"attribute {name!r} has no value")
        position += 1
        while position < end and text[position].isspace():
            position += 1
        if position >= end or text[position] not in "\"'":
            raise RangeError(f"attribute {name!r} is not quoted")
        quote = text[position]
        value_start = position + 1
        value_end = text.find(quote, value_start, end)
        if value_end < 0:
            raise RangeError(f"attribute {name!r} is unterminated")
        result.append((name, value_start, value_end))
        position = value_end + 1
    return result


def _entity(value: str) -> str:
    if value.startswith("#x"):
        return chr(int("0x" + value[2:], 0))
    if value.startswith("#"):
        return chr(int(value[1:]))
    predefined = {"amp": "&", "apos": "'", "gt": ">", "lt": "<", "quot": '"'}
    if value in predefined:
        return predefined[value]
    decoded = html.entities.html5.get(value + ";")
    if decoded is None:
        raise RangeError(f"unsupported XML entity: &{value};")
    return decoded


def _qname(name: str) -> str:
    return "{" + name if "}" in name else name


def _external(
    parser: Any,
    context: str | None,
    system: str | None,
    public: str | None,
) -> int:
    filename = PurePosixPath(system or "").name.lower()
    if filename not in _TRUSTED_XHTML_DTDS:
        raise RangeError(f"external entity is not allowed: {system or public}")
    child = parser.ExternalEntityParserCreate(context)
    child.Parse(_xhtml_entities_dtd().encode(), True)
    return 1


def _validate_tree(tree: etree._ElementTree, nodes: dict[tuple[int, ...], NodeSpan]) -> None:
    expected = {_tree_path(node): str(node.tag) for node in tree.getroot().iter() if isinstance(node.tag, str)}
    actual = {path: span.qname for path, span in nodes.items()}
    if expected != actual:
        raise RangeError("Expat lexical nodes do not match the strict lxml tree")


def _tree_path(node: etree._Element) -> tuple[int, ...]:
    path: list[int] = []
    while node.getparent() is not None:
        parent = node.getparent()
        elements = [child for child in parent if isinstance(child.tag, str)]
        path.append(elements.index(node))
        node = parent
    return tuple(reversed(path))


def _complement(size: int, editable: list[ByteSpan]) -> tuple[ByteSpan, ...]:
    result: list[ByteSpan] = []
    position = 0
    for span in editable:
        if span.byte_start < position:
            raise RangeError("editable raw byte spans overlap")
        if position < span.byte_start:
            result.append(ByteSpan(byte_start=position, byte_end=span.byte_start))
        position = span.byte_end
    if position < size:
        result.append(ByteSpan(byte_start=position, byte_end=size))
    return tuple(result)


__all__ = [
    "CharSpan",
    "NodeSpan",
    "RangeError",
    "RawSpan",
    "ResourceIndex",
    "SlotSpan",
    "index_resource",
]
