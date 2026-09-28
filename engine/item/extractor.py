"""Lossless EPUB XML extraction into the v2.3 document contract."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import regex
import tinycss2
from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import element_path, parse_xml_safely, qname_local_name
from engine.core.styles import ReorderPolicy, StyleIssue, StyleScan, scan_inline_style, scan_stylesheets
from engine.item.inline import Event, events_to_projection, parse_projection
from engine.schemas.v23 import (
    DocumentPlan,
    NodeRecord,
    RegistryEntry,
    ResourceRecord,
    SlotRange,
    SourceSlot,
    Unit,
    canonical_hash,
)

EXTRACTOR_VERSION = "epubox-extractor-2"
ADAPTER_VERSION = "epubox-xml-1"

_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_TRANSLATABLE_ATTRS = {"alt", "title", "aria-label", "aria-description"}
_HARD_TAGS = {"code", "pre", "script", "style", "svg", "math", "kbd", "samp", "var", "tt"}
_MEDIA_TAGS = {"img", "audio", "video", "source", "canvas", "iframe", "object", "embed"}
_EMPTY_BOUNDARIES = {"br", "wbr"}
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_SEMANTIC_TAGS = {
    "blockquote",
    "caption",
    "dd",
    "dt",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "td",
    "th",
    "title",
}
_CONTAINER_TAGS = {
    "article",
    "aside",
    "body",
    "div",
    "footer",
    "header",
    "main",
    "nav",
    "section",
}
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN_RE = re.compile(r"[A-Za-z]")
_MONOSPACE_CLASS_RE = re.compile(
    r"mono(?:space|cd)?|(?:^|[_-])(?:code|terminal|console|syntax)(?:[_-]|$)", re.IGNORECASE
)
_IDENTIFIER_RE = re.compile(
    r"(?:@?[A-Za-z_][A-Za-z0-9_]*\([^\n)]*\)|\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+\b|"
    r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b|\b[A-Za-z_]\w*_[A-Za-z0-9_]+\b)"
)
_RUST_COMMAND_RE = re.compile(
    r"^(?:cargo(?:\s+(?:check|build|run|test|new|doc|install|update)(?:\s+[-\w=./]+)*)?|"
    r"rustup(?:\s+(?:update|doc|self\s+uninstall)(?:\s+[-\w=./]+)*)?|"
    r"rustc(?:\s+[-\w=./]+)*)$",
    re.IGNORECASE,
)
_CONTEXT_LIMIT = 400
_HINT_LIMIT = 160
_TERM_MODES = {"preferred", "required", "keep_source"}

type SlotField = Literal["text", "tail", "attribute"]
type OwnerKind = Literal["unit", "protected", "whitespace", "out_of_scope"]


def _effective_language(node: etree._Element) -> str:
    for item in [node, *node.iterancestors()]:
        if language := (item.get(_XML_LANG) or item.get("lang") or "").strip():
            return language.casefold()
    return ""


def _primary_language(root: etree._Element) -> str:
    languages = {
        (node.text or "").strip().casefold()
        for node in root.iter()
        if isinstance(node.tag, str) and qname_local_name(node.tag) == "language" and (node.text or "").strip()
    }
    return next(iter(languages)) if len(languages) == 1 else ""


def _unique_language_match(root: etree._Element, elements: list[etree._Element]) -> etree._Element | None:
    language = _primary_language(root)
    if not language:
        return None
    matches = [element for element in elements if _effective_language(element) == language]
    return matches[0] if len(matches) == 1 else None


def select_primary_title(root: etree._Element | etree._ElementTree) -> etree._Element | None:
    """Select only an unambiguous OPF primary title; preserve all others."""

    root_element = root.getroot() if isinstance(root, etree._ElementTree) else root
    titles = [
        node for node in root_element.iter() if isinstance(node.tag, str) and qname_local_name(node.tag) == "title"
    ]
    if not titles:
        return None
    by_id = {node.get("id"): node for node in titles if node.get("id")}
    main_refs = {
        (node.get("refines") or "").removeprefix("#")
        for node in root_element.iter()
        if isinstance(node.tag, str)
        and qname_local_name(node.tag) == "meta"
        and (node.get("property") or "").strip().casefold() == "title-type"
        and (node.text or "").strip().casefold() == "main"
    }
    if main_refs:
        candidates = {by_id[ref] for ref in main_refs if ref in by_id}
        return next(iter(candidates)) if len(candidates) == 1 else None
    if len(titles) == 1:
        return titles[0]
    return _unique_language_match(root_element, titles)


@dataclass
class _Slot:
    slot_id: str
    node_key: str
    field: SlotField
    source_value: str
    attribute_name: str | None = None
    ranges: list[SlotRange] = dataclass_field(default_factory=list)


class _Extractor:
    def __init__(
        self,
        source_markup: str,
        resource_path: str,
        source_hash: str,
        media_type: str,
        config: Mapping[str, Any],
        styles: Any,
    ) -> None:
        self.source_markup = source_markup
        self.resource_path = resource_path
        self.source_hash = source_hash
        self.media_type = media_type
        self.config = config
        self.tree = parse_xml_safely(source_markup)
        self.elements = list(self.tree.getroot().iter())
        self.elements = [node for node in self.elements if isinstance(node.tag, str)]
        self.node_keys = {node: self._node_key(node) for node in self.elements}
        self.nodes = {
            key: NodeRecord(node_key=key, element_path=element_path(node), qname=str(node.tag))
            for node, key in self.node_keys.items()
        }
        self.document_id = _stable_id("d", source_hash, resource_path, EXTRACTOR_VERSION)
        self.slots: dict[str, _Slot] = {}
        self.units: list[Unit] = []
        self.issues: list[dict[str, Any]] = []
        self.boundaries: list[dict[str, Any]] = []
        self.derived_bindings: list[dict[str, Any]] = []
        self.non_element_tail_slots: dict[etree._Element, _Slot] = {}
        self.translate_overrides = self._compile_translate_exceptions()
        self._collect_slots()
        self.style_scan = self._style_policy(styles)
        self.style_document_fallback = self.style_scan.policy == ReorderPolicy.UNKNOWN
        self.style_locked_elements, self.style_locked_parents = self._style_locks(self.style_scan)

    def extract(self) -> DocumentPlan:
        root = self.tree.getroot()
        root_name = qname_local_name(root.tag)
        if root_name == "package":
            self._extract_opf(root)
        elif root_name == "ncx":
            for node in self.elements:
                parent = node.getparent()
                if (
                    qname_local_name(node.tag) == "text"
                    and parent is not None
                    and isinstance(parent.tag, str)
                    and qname_local_name(parent.tag) in {"navlabel", "doctitle"}
                ):
                    self._make_whole_content_unit(node, True, "navigation")
        else:
            title = next((node for node in self.elements if qname_local_name(node.tag) == "title"), None)
            if title is not None:
                self._make_whole_content_unit(title, True, "head_title")
            body = next((node for node in self.elements if qname_local_name(node.tag) == "body"), root)
            self._walk(body, inherited_translate=True)
        self._extract_attributes()
        self._finalize_unowned_slots()
        self._finalize_units()
        self._collect_derived_bindings()
        source_slots = {slot_id: self._slot_model(slot) for slot_id, slot in self.slots.items()}
        return DocumentPlan(
            document_id=self.document_id,
            source_hash=self.source_hash,
            resource=ResourceRecord(
                path=self.resource_path,
                media_type=self.media_type,
                source_sha256=hashlib.sha256(self.source_markup.encode("utf-8")).hexdigest(),
            ),
            adapter_version=ADAPTER_VERSION,
            extractor_version=EXTRACTOR_VERSION,
            source_markup=self.source_markup,
            nodes=self.nodes,
            source_slots=source_slots,
            units=tuple(self.units),
            boundaries=tuple(self.boundaries),
            derived_bindings=tuple(self.derived_bindings),
            preparation_issues=tuple(self.issues),
        )

    def _node_key(self, node: etree._Element) -> str:
        path = element_path(node)
        return "n-root" if not path else "n-" + "-".join(map(str, path))

    def _collect_slots(self) -> None:
        for node in self.elements:
            key = self.node_keys[node]
            if node.text is not None:
                self._add_slot(key, "text", node.text)
            if node.getparent() is not None and node.tail is not None:
                self._add_slot(key, "tail", node.tail)
            for name, value in node.attrib.items():
                if qname_local_name(name) in _TRANSLATABLE_ATTRS:
                    self._add_slot(key, "attribute", value, name)
            for index, child in enumerate(node):
                if not isinstance(child.tag, str) and child.tail is not None:
                    slot = self._add_slot(key, "tail", child.tail, token=f"special-{index}")
                    self.non_element_tail_slots[child] = slot
                    self.boundaries.append(
                        {
                            "kind": "non_element_tail",
                            "slot_id": slot.slot_id,
                            "parent_node_key": key,
                            "child_index": index,
                        }
                    )

    def _add_slot(
        self,
        node_key: str,
        field_name: SlotField,
        value: str,
        attribute_name: str | None = None,
        *,
        token: str | None = None,
    ) -> _Slot:
        suffix = token or attribute_name or field_name
        slot_id = f"s-{node_key[2:]}-{hashlib.sha256(suffix.encode()).hexdigest()[:8]}"
        slot = _Slot(slot_id, node_key, field_name, value, attribute_name)
        self.slots[slot_id] = slot
        return slot

    def _compile_translate_exceptions(self) -> dict[etree._Element, bool]:
        raw = self.config.get("translate_exceptions", ())
        if not raw:
            return {}
        if isinstance(raw, Mapping):
            rules = [{"selector": selector, "action": action} for selector, action in raw.items()]
        elif isinstance(raw, (list, tuple)):
            rules = list(raw)
        else:
            raise TypeError("translate_exceptions must be a mapping or list")

        overrides: dict[etree._Element, bool] = {}
        for rule in rules:
            if not isinstance(rule, Mapping):
                raise TypeError("each translate exception must be an object")
            resource = str(rule.get("resource", rule.get("resource_path", self.resource_path)))
            if resource != self.resource_path:
                continue
            selector = str(rule.get("selector", ""))
            element_id = str(rule.get("element_id", selector[1:] if selector.startswith("#") else ""))
            if not element_id or selector and selector != f"#{element_id}":
                raise ValueError("translate exceptions support only exact #id selectors")
            action = rule.get("action", rule.get("translate"))
            if isinstance(action, bool):
                translated = action
            elif str(action).lower() in {"translate", "yes", "true"}:
                translated = True
            elif str(action).lower() in {"keep", "no", "false"}:
                translated = False
            else:
                raise ValueError(f"unknown translate exception action: {action}")
            matches = [node for node in self.elements if node.get("id") == element_id]
            if not matches:
                self.issues.append(
                    {
                        "scope": "document",
                        "code": "unmatched_translate_exception",
                        "element_id": element_id,
                    }
                )
                continue
            for node in matches:
                overrides[node] = translated
        return overrides

    def _style_policy(self, styles: Any) -> StyleScan:
        sheets: dict[str, str] = {}
        roots: list[str] = []
        for index, node in enumerate(self.elements):
            if qname_local_name(node.tag) == "style" and node.text:
                name = f"{self.resource_path}#style-{index}"
                sheets[name] = node.text
                roots.append(name)
        if isinstance(styles, Mapping):
            sheets.update({str(key): str(value) for key, value in styles.items()})
        for node in self.elements:
            if qname_local_name(node.tag) != "link":
                continue
            rel = {token.casefold() for token in (node.get("rel") or "").split()}
            if "stylesheet" not in rel:
                continue
            resolved = _resolve_local_resource(self.resource_path, node.get("href") or "")
            if resolved is None:
                return StyleScan(
                    ReorderPolicy.UNKNOWN,
                    issues=(StyleIssue("external_stylesheet", self.resource_path, node.get("href") or ""),),
                )
            roots.append(resolved)
        if not roots:
            return StyleScan(ReorderPolicy.REORDER_ALLOWED)

        def load_import(current: str, href: str) -> tuple[str, str] | None:
            resolved = _resolve_local_resource(current.split("#", 1)[0], href)
            if resolved is None or resolved not in sheets:
                return None
            return resolved, sheets[resolved]

        return scan_stylesheets(sheets, roots=tuple(dict.fromkeys(roots)), loader=load_import)

    def _style_locks(self, scan: StyleScan) -> tuple[set[etree._Element], set[etree._Element]]:
        locked_elements: set[etree._Element] = set()
        locked_parents: set[etree._Element] = set()
        if scan.policy == ReorderPolicy.UNKNOWN:
            return locked_elements, locked_parents
        for constraint in scan.constraints:
            candidates = self._selector_candidates(constraint.selector)
            if constraint.mode == "group":
                for element in candidates:
                    current = element
                    while isinstance(current.tag, str):
                        parent = current.getparent()
                        if parent is None:
                            break
                        locked_parents.add(parent)
                        current = parent
            elif constraint.mode == "descendants":
                for element in candidates:
                    locked_elements.update(
                        descendant for descendant in element.iter() if isinstance(descendant.tag, str)
                    )
            else:
                locked_elements.update(candidates)
        return locked_elements, locked_parents

    def _selector_candidates(self, selector: str) -> list[etree._Element]:
        tokens = tinycss2.parse_component_value_list(selector, skip_comments=True)
        compounds, combinators = self._selector_parts(tokens)
        if not compounds:
            return list(self.elements)
        candidates = [element for element in self.elements if self._matches_stable_compound(element, compounds[-1])]
        for index in range(len(compounds) - 2, -1, -1):
            combinator = combinators[index]
            if combinator in {"+", "~"}:
                break  # Ignoring sibling requirements is a conservative superset.
            required = compounds[index]
            filtered: list[etree._Element] = []
            for element in candidates:
                if combinator == ">":
                    parent = element.getparent()
                    if parent is not None and self._matches_stable_compound(parent, required):
                        filtered.append(element)
                elif any(self._matches_stable_compound(ancestor, required) for ancestor in element.iterancestors()):
                    filtered.append(element)
            candidates = filtered
        return candidates

    def _selector_parts(self, tokens: list[Any]) -> tuple[list[list[Any]], list[str]]:
        compounds: list[list[Any]] = []
        combinators: list[str] = []
        current: list[Any] = []
        pending_space = False
        for token in tokens:
            if token.type == "whitespace":
                pending_space = bool(current)
                continue
            value = getattr(token, "value", None)
            if token.type == "literal" and value in {">", "+", "~"}:
                if current:
                    compounds.append(current)
                    current = []
                combinators.append(str(value))
                pending_space = False
                continue
            if pending_space:
                compounds.append(current)
                combinators.append(" ")
                current = []
                pending_space = False
            current.append(token)
        if current:
            compounds.append(current)
        if len(combinators) != max(0, len(compounds) - 1):
            return [], []
        return compounds, combinators

    def _matches_stable_compound(self, element: etree._Element, tokens: list[Any]) -> bool:
        tag: str | None = None
        element_id: str | None = None
        classes: set[str] = set()
        pseudo = False
        index = 0
        while index < len(tokens):
            token = tokens[index]
            token_type = token.type
            value = getattr(token, "value", None)
            if token_type == "literal" and value == ":":
                pseudo = True
            elif token_type == "hash" and not pseudo:
                element_id = str(value)
            elif token_type == "ident" and not pseudo:
                previous = getattr(tokens[index - 1], "value", None) if index else None
                if previous == ".":
                    classes.add(str(value))
                elif tag is None:
                    tag = str(value).casefold()
            elif token_type == "literal" and value not in {".", "*"}:
                return True  # Widen an already classified selector rather than miss a candidate.
            if token_type not in {"ident", "function"} and not (token_type == "literal" and value == ":"):
                pseudo = False
            index += 1
        if tag is not None and qname_local_name(element.tag) != tag:
            return False
        if element_id is not None and element.get("id") != element_id:
            return False
        return not classes or classes.issubset(set((element.get("class") or "").split()))

    def _walk(self, node: etree._Element, inherited_translate: bool, metadata_mode: bool = False) -> None:
        if not isinstance(node.tag, str):
            return
        name = qname_local_name(node.tag)
        translated = self._translate_state(node, inherited_translate)
        if self._is_hard(node) or not translated and not self._has_translate_yes(node):
            self._mark_subtree(node, "protected")
            return
        if metadata_mode and name == "text" and qname_local_name(node.getparent().tag) in {"navlabel", "doctitle"}:
            self._make_whole_content_unit(node, translated, "navigation")
            return
        if name in _SEMANTIC_TAGS:
            direct_blocks = [
                child for child in node if isinstance(child.tag, str) and qname_local_name(child.tag) in _BLOCK_TAGS
            ]
            if not direct_blocks:
                self._make_whole_content_unit(node, translated, self._kind(node))
                return
        if name in _CONTAINER_TAGS or name in _SEMANTIC_TAGS:
            self._extract_direct_regions(node, translated)
        for child in node:
            if isinstance(child.tag, str) and qname_local_name(child.tag) in _BLOCK_TAGS:
                self._walk(child, translated, metadata_mode)

    def _extract_direct_regions(self, parent: etree._Element, translated: bool) -> None:
        blocks = [
            child for child in parent if isinstance(child.tag, str) and qname_local_name(child.tag) in _BLOCK_TAGS
        ]
        before: etree._Element | None = None
        for after in [*blocks, None]:
            self._make_region_unit(parent, before, after, translated, self._kind(parent, virtual=True))
            before = after

    def _make_whole_content_unit(self, node: etree._Element, translated: bool, kind: str) -> None:
        self._make_region_unit(node, None, None, translated, kind)

    def _make_region_unit(
        self,
        parent: etree._Element,
        after_node: etree._Element | None,
        before_node: etree._Element | None,
        translated: bool,
        kind: str,
    ) -> None:
        members = self._region_members(parent, after_node, before_node)
        lead_slot = self._leading_slot(parent, after_node)
        has_latin = bool(lead_slot and _LATIN_RE.search(lead_slot.source_value)) or self._members_have_latin(members)
        if (not translated and not self._has_translate_yes(parent)) or not has_latin:
            return
        region = {
            "type": "content",
            "parent_node_key": self.node_keys[parent],
            "after_node_key": self.node_keys.get(after_node) if after_node is not None else None,
            "before_node_key": self.node_keys.get(before_node) if before_node is not None else None,
        }
        unit_id = _stable_id("u", self.source_hash, self.resource_path, canonical_hash(region), EXTRACTOR_VERSION)
        registry: dict[str, RegistryEntry] = {}
        events: list[Event] = []
        slot_ids: list[str] = []
        counter = {"g": 0, "x": 0, "b": 0}

        if lead_slot is not None:
            self._emit_slot(
                lead_slot,
                unit_id,
                events,
                registry,
                counter,
                slot_ids,
                self.node_keys[parent],
                force_protected=not translated,
            )
        for member in members:
            if member is after_node or member is before_node:
                continue
            self._emit_child(member, unit_id, events, registry, counter, slot_ids, self.node_keys[parent], translated)
            tail = self._tail_slot(member)
            if tail is not None:
                self._emit_slot(
                    tail,
                    unit_id,
                    events,
                    registry,
                    counter,
                    slot_ids,
                    self.node_keys[parent],
                    force_protected=not translated,
                )
        events = list(self._constrain_hard_boundaries(tuple(events), registry, counter))
        if not any(event.kind == "text" and _LATIN_RE.search(event.value) for event in events):
            return
        projection = events_to_projection(events)
        self.units.append(
            Unit(
                unit_id=unit_id,
                document_id=self.document_id,
                kind=kind,
                source_projection=projection,
                node_key=self.node_keys[parent],
                slot_ids=tuple(dict.fromkeys(slot_ids)),
                registry=registry,
                checks=("projection", "source_target", "format_binding"),
                region=region,
                logical_hash="pending",
            )
        )

    def _region_members(
        self, parent: etree._Element, after_node: etree._Element | None, before_node: etree._Element | None
    ) -> list[etree._Element]:
        children = list(parent)
        start = children.index(after_node) + 1 if after_node is not None else 0
        end = children.index(before_node) if before_node is not None else len(children)
        return children[start:end]

    def _members_have_latin(self, members: list[etree._Element]) -> bool:
        for member in members:
            if (
                isinstance(member.tag, str)
                and not self._is_hard(member)
                and _LATIN_RE.search("".join(member.itertext()))
            ):
                return True
            if member.tail and _LATIN_RE.search(member.tail):
                return True
        return False

    def _leading_slot(self, parent: etree._Element, after_node: etree._Element | None) -> _Slot | None:
        return (
            self._slot_for(self.node_keys[parent], "text")
            if after_node is None
            else self._slot_for(self.node_keys[after_node], "tail")
        )

    def _emit_child(
        self,
        node: etree._Element,
        unit_id: str,
        events: list[Event],
        registry: dict[str, RegistryEntry],
        counter: dict[str, int],
        slot_ids: list[str],
        parent_ref: str,
        inherited_translate: bool,
    ) -> None:
        if not isinstance(node.tag, str):
            self._add_atom(
                node, "comment" if isinstance(node, etree._Comment) else "pi", parent_ref, events, registry, counter
            )
            return
        translated = self._translate_state(node, inherited_translate)
        if self._is_atom(node) or not translated and not self._has_translate_yes(node):
            self._mark_subtree(node, "protected", preserve_attributes=self._is_atom(node) and not self._is_hard(node))
            self._add_atom(node, self._boundary_type(node), parent_ref, events, registry, counter)
            return
        counter["g"] += 1
        ref = f"g{counter['g']}"
        reorder = self._style_reorder_allowed(node) and self._inline_reorder_allowed(node)
        registry[ref] = RegistryEntry(
            ref_id=ref,
            kind="g",
            source_node_key=self.node_keys[node],
            parent_ref=parent_ref,
            movement="same_parent" if reorder else "locked",
            reorder_allowed=reorder,
            source_text="".join(node.itertext()),
            hints={"element": qname_local_name(node.tag)},
        )
        events.append(Event(kind="marker", value=f"+{ref}"))
        text_slot = self._slot_for(self.node_keys[node], "text")
        if text_slot is not None:
            self._emit_slot(
                text_slot,
                unit_id,
                events,
                registry,
                counter,
                slot_ids,
                self.node_keys[node],
                force_protected=not translated,
            )
        for child in node:
            self._emit_child(child, unit_id, events, registry, counter, slot_ids, self.node_keys[node], translated)
            tail = self._tail_slot(child)
            if tail is not None:
                self._emit_slot(
                    tail,
                    unit_id,
                    events,
                    registry,
                    counter,
                    slot_ids,
                    self.node_keys[node],
                    force_protected=not translated,
                )
        events.append(Event(kind="marker", value=f"-{ref}"))

    def _emit_slot(
        self,
        slot: _Slot,
        unit_id: str,
        events: list[Event],
        registry: dict[str, RegistryEntry],
        counter: dict[str, int],
        slot_ids: list[str],
        parent_ref: str,
        *,
        force_protected: bool = False,
    ) -> None:
        if slot.ranges:
            return
        value = slot.source_value
        if not value:
            return
        if force_protected or not value.strip():
            self._emit_protected_text(
                slot,
                0,
                len(value),
                events,
                registry,
                counter,
                parent_ref,
                "protected_text" if force_protected else "whitespace",
                "protected" if force_protected else "whitespace",
            )
            return
        cursor = 0
        for match in _CJK_RE.finditer(value):
            if match.start() > cursor:
                self._emit_text_range(slot, cursor, match.start(), unit_id, events)
            self._emit_protected_text(
                slot,
                match.start(),
                match.end(),
                events,
                registry,
                counter,
                parent_ref,
                "existing_chinese",
                "out_of_scope",
            )
            cursor = match.end()
        if cursor < len(value):
            self._emit_text_range(slot, cursor, len(value), unit_id, events)
        if any(item.owner_unit_id == unit_id for item in slot.ranges):
            slot_ids.append(slot.slot_id)

    def _emit_text_range(self, slot: _Slot, start: int, end: int, unit_id: str, events: list[Event]) -> None:
        value = slot.source_value[start:end]
        owner_kind: OwnerKind = "unit" if value.strip() else "whitespace"
        owner_unit_id = unit_id if owner_kind == "unit" else None
        slot.ranges.append(SlotRange(start=start, end=end, owner_kind=owner_kind, owner_unit_id=owner_unit_id))
        events.append(Event(kind="text", value=value))

    def _emit_protected_text(
        self,
        slot: _Slot,
        start: int,
        end: int,
        events: list[Event],
        registry: dict[str, RegistryEntry],
        counter: dict[str, int],
        parent_ref: str,
        boundary_type: str,
        owner_kind: OwnerKind,
    ) -> None:
        counter["x"] += 1
        ref = f"x{counter['x']}"
        text = slot.source_value[start:end]
        slot.ranges.append(SlotRange(start=start, end=end, owner_kind=owner_kind))
        registry[ref] = RegistryEntry(
            ref_id=ref,
            kind="x",
            source_node_key=slot.node_key,
            parent_ref=parent_ref,
            movement="locked",
            source_text=text,
            hints={"slot_id": slot.slot_id, "start": str(start), "end": str(end)},
            boundary_type=boundary_type,
        )
        events.append(Event(kind="marker", value=f"={ref}"))

    def _add_atom(
        self,
        node: etree._Element,
        boundary_type: str,
        parent_ref: str,
        events: list[Event],
        registry: dict[str, RegistryEntry],
        counter: dict[str, int],
    ) -> None:
        counter["x"] += 1
        ref = f"x{counter['x']}"
        if isinstance(node.tag, str):
            node_key = self.node_keys[node]
            hints = {"element": qname_local_name(node.tag)}
            source_text = "".join(node.itertext())
            if boundary_type == "code" and source_text:
                hints["readonly"] = _bounded(source_text, _HINT_LIMIT)
        else:
            parent = node.getparent()
            node_key = self.node_keys[parent]
            hints = {"child_index": str(list(parent).index(node)), "node_kind": boundary_type}
            source_text = node.text or ""
        hard = boundary_type in {"code", "math", "media", "footnote", "br", "page", "anchor"}
        registry[ref] = RegistryEntry(
            ref_id=ref,
            kind="x",
            source_node_key=node_key,
            parent_ref=parent_ref,
            movement="fixed" if hard else "locked",
            reorder_allowed=False,
            source_text=source_text,
            hints=hints,
            boundary_type=boundary_type,
        )
        events.append(Event(kind="marker", value=f"={ref}"))

    def _constrain_hard_boundaries(
        self,
        events: tuple[Event, ...],
        registry: dict[str, RegistryEntry],
        counter: dict[str, int],
    ) -> tuple[Event, ...]:
        hard_types = {"code", "math", "media", "footnote", "br", "page", "anchor"}

        def visit(index: int, closing_ref: str | None = None) -> tuple[list[Event], int, bool]:
            items: list[tuple[list[Event], str | None]] = []
            contains_hard = False
            while index < len(events):
                event = events[index]
                if event.kind == "text":
                    items.append(([event], None))
                    index += 1
                    continue
                edge, ref = event.value[0], event.value[1:]
                if edge == "-":
                    if ref != closing_ref:
                        raise ValueError(f"unexpected source close marker: {event.value}")
                    break
                if edge == "+" and ref.startswith("g"):
                    inner, index, child_hard = visit(index + 1, ref)
                    if child_hard:
                        registry[ref] = registry[ref].model_copy(
                            update={"movement": "fixed", "reorder_allowed": False}
                        )
                    items.append(([event, *inner, events[index]], None))
                    contains_hard |= child_hard
                    index += 1
                    continue
                entry = registry[ref]
                direct_hard = ref if edge == "=" and entry.boundary_type in hard_types else None
                items.append(([event], direct_hard))
                contains_hard |= direct_hard is not None
                index += 1

            direct = [ref for _, ref in items if ref is not None]
            if not direct:
                return [event for item, _ in items for event in item], index, contains_hard

            parent_ref = registry[direct[0]].parent_ref
            source_node_key = registry[direct[0]].source_node_key
            constrained: list[Event] = []
            fixed_order: list[str] = []
            run: list[Event] = []

            def flush_run() -> None:
                counter["b"] += 1
                boundary = f"b{counter['b']}"
                fixed_order.append(boundary)
                registry[boundary] = RegistryEntry(
                    ref_id=boundary,
                    kind="b",
                    source_node_key=source_node_key,
                    parent_ref=parent_ref,
                    movement="fixed",
                    reorder_allowed=False,
                    boundary_type="hard_interval",
                )
                constrained.extend((Event(kind="marker", value=f"+{boundary}"), *run))
                constrained.append(Event(kind="marker", value=f"-{boundary}"))
                run.clear()

            for item, hard_ref in items:
                if hard_ref is None:
                    run.extend(item)
                    continue
                flush_run()
                constrained.extend(item)
                fixed_order.append(hard_ref)
            flush_run()
            order = tuple(fixed_order)
            for ref in order:
                registry[ref] = registry[ref].model_copy(update={"fixed_order": order})
            return constrained, index, True

        constrained, index, _ = visit(0)
        if index != len(events):
            raise ValueError("unexpected trailing source marker")
        return tuple(constrained)

    def _extract_attributes(self) -> None:
        existing_unit_slots = {slot_id for unit in self.units for slot_id in unit.slot_ids}
        for slot in self.slots.values():
            if slot.field != "attribute" or slot.slot_id in existing_unit_slots or slot.ranges:
                continue
            node = self._element_for_key(slot.node_key)
            if self._inside_hard(node) or not self._translate_state_chain(node):
                slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind="protected"))
                continue
            if not _LATIN_RE.search(slot.source_value):
                kind = "whitespace" if not slot.source_value.strip() else "out_of_scope"
                slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind=kind))
                continue
            region = {"type": "attribute", "node_key": slot.node_key, "attribute_name": slot.attribute_name}
            unit_id = _stable_id("u", self.source_hash, self.resource_path, canonical_hash(region), EXTRACTOR_VERSION)
            slot.ranges.append(
                SlotRange(start=0, end=len(slot.source_value), owner_kind="unit", owner_unit_id=unit_id)
            )
            projection = events_to_projection((Event(kind="text", value=slot.source_value),))
            self.units.append(
                Unit(
                    unit_id=unit_id,
                    document_id=self.document_id,
                    kind="attribute",
                    source_projection=projection,
                    node_key=slot.node_key,
                    slot_ids=(slot.slot_id,),
                    checks=("plain_text", "source_target"),
                    region=region,
                    logical_hash="pending",
                )
            )

    def _extract_opf(self, root: etree._Element) -> None:
        titles = [node for node in self.elements if qname_local_name(node.tag) == "title"]
        descriptions = [node for node in self.elements if qname_local_name(node.tag) == "description"]
        primary_title = select_primary_title(root)
        if primary_title is not None:
            self._make_whole_content_unit(primary_title, True, "metadata_title")
        elif titles:
            self.issues.append(
                {
                    "scope": "document",
                    "severity": "warning",
                    "code": "ambiguous_primary_title",
                    "count": len(titles),
                }
            )
        for title in titles:
            if title is not primary_title:
                self._mark_subtree(title, "out_of_scope")

        textual_descriptions = [node for node in descriptions if len(node) == 0 and (node.text or "").strip()]
        if len(textual_descriptions) != len(descriptions):
            self.issues.append(
                {
                    "scope": "document",
                    "severity": "warning",
                    "code": "unsupported_description_markup",
                    "count": len(descriptions) - len(textual_descriptions),
                }
            )
        if len(textual_descriptions) == 1:
            primary_description = textual_descriptions[0]
        elif len(textual_descriptions) > 1:
            primary_description = _unique_language_match(root, textual_descriptions)
        else:
            primary_description = None
        if primary_description is not None:
            self._make_whole_content_unit(primary_description, True, "metadata_description")
        elif descriptions:
            self.issues.append(
                {
                    "scope": "document",
                    "severity": "warning",
                    "code": "ambiguous_text_description",
                    "count": len(descriptions),
                }
            )
        for description in descriptions:
            if description is not primary_description:
                self._mark_subtree(description, "out_of_scope")
        for node in self.elements:
            if node not in titles and node not in descriptions:
                name = qname_local_name(node.tag)
                if name in {"creator", "publisher", "identifier", "date"}:
                    self._mark_subtree(node, "out_of_scope")

    def _finalize_units(self) -> None:
        self.units.sort(key=lambda unit: (self.nodes[unit.node_key].element_path, unit.kind == "attribute"))
        source_texts = [self._unit_source_text(unit) for unit in self.units]
        document_title = next(
            (
                _bounded("".join(node.itertext()).strip(), _CONTEXT_LIMIT)
                for node in self.elements
                if qname_local_name(node.tag) == "title" and "".join(node.itertext()).strip()
            ),
            "",
        )
        book_title = _bounded(str(self.config.get("book_title", document_title)), _CONTEXT_LIMIT)
        raw_terms = self._configured_terms()
        semantic_config = self._semantic_config()
        updated: list[Unit] = []

        for index, unit in enumerate(self.units):
            section = self._section_for(unit)
            context = {
                "book_title": book_title,
                "title": document_title,
                "section": section,
                "previous": _bounded(source_texts[index - 1], _CONTEXT_LIMIT, tail=True) if index else "",
                "next": _bounded(source_texts[index + 1], _CONTEXT_LIMIT) if index + 1 < len(self.units) else "",
            }
            context.update(self._table_context(unit))
            footnote = self._footnote_context(unit)
            if footnote:
                context["footnote"] = footnote
            terms = tuple(term for term in raw_terms if self._term_applies(term, unit, section, source_texts[index]))
            logical_hash = canonical_hash(
                {
                    "source_hash": self.source_hash,
                    "document_id": self.document_id,
                    "kind": unit.kind,
                    "projection": unit.source_projection,
                    "registry": {key: value.model_dump(mode="json") for key, value in unit.registry.items()},
                    "context": context,
                    "terms": terms,
                    "semantic_config": semantic_config,
                }
            )
            updated.append(unit.model_copy(update={"context": context, "terms": terms, "logical_hash": logical_hash}))
        self.units = updated

    def _semantic_config(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "target_language": "zh-Hans",
            "model": "unspecified",
            "provider": "unspecified",
            "prompt_version": "unspecified",
            "protocol_version": "epubox-text-1",
            "generation": {},
            "translate_exceptions": (),
        }
        frozen = {key: self.config.get(key, value) for key, value in defaults.items()}
        frozen.update({"extractor_version": EXTRACTOR_VERSION, "adapter_version": ADAPTER_VERSION})
        return frozen

    def _configured_terms(self) -> tuple[dict[str, Any], ...]:
        raw = self.config.get("terms", self.config.get("glossary", ()))
        if raw in (None, (), [], {}):
            return ()
        if isinstance(raw, Mapping):
            entries = [{"source": source, "target": target} for source, target in raw.items()]
        elif isinstance(raw, (list, tuple)):
            entries = list(raw)
        else:
            raise TypeError("terms must be a mapping or list")

        terms: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise TypeError("each term must be an object")
            source = str(entry.get("source", "")).strip()
            mode = str(entry.get("mode", "preferred"))
            target = str(entry.get("target", source if mode == "keep_source" else "")).strip()
            if not source or not target:
                raise ValueError("term source and target must not be empty")
            if mode not in _TERM_MODES:
                raise ValueError(f"unknown term mode: {mode}")
            terms.append(
                {
                    "source": source,
                    "target": target,
                    "scope": str(entry.get("scope", "global")),
                    "mode": mode,
                    "note": str(entry.get("note", "")),
                }
            )
        return tuple(terms)

    def _term_applies(self, term: Mapping[str, Any], unit: Unit, section: str, source_text: str) -> bool:
        scope = str(term.get("scope", "global"))
        in_scope = scope in {"", "*", "global", "book", self.resource_path, unit.kind, section}
        return in_scope and str(term["source"]).casefold() in source_text.casefold()

    def _unit_source_text(self, unit: Unit) -> str:
        parts: list[str] = []
        for event in parse_projection(unit.source_projection):
            if event.kind == "text":
                parts.append(event.value)
            elif event.value.startswith("=x"):
                parts.append(unit.registry[event.value[1:]].source_text)
        return "".join(parts)

    def _section_for(self, unit: Unit) -> str:
        unit_path = self.nodes[unit.node_key].element_path
        headings = [
            node
            for node in self.elements
            if qname_local_name(node.tag) in {"h1", "h2", "h3", "h4", "h5", "h6"} and element_path(node) <= unit_path
        ]
        if unit.kind == "heading":
            node = self._element_for_key(unit.node_key)
            if qname_local_name(node.tag).startswith("h"):
                return _bounded("".join(node.itertext()).strip(), _CONTEXT_LIMIT)
        return _bounded("".join(headings[-1].itertext()).strip(), _CONTEXT_LIMIT) if headings else ""

    def _table_context(self, unit: Unit) -> dict[str, str]:
        node = self._element_for_key(unit.node_key)
        cell = next(
            (item for item in [node, *node.iterancestors()] if qname_local_name(item.tag) in {"td", "th"}),
            None,
        )
        if cell is None:
            return {}
        row = next((item for item in cell.iterancestors() if qname_local_name(item.tag) == "tr"), None)
        table = next((item for item in cell.iterancestors() if qname_local_name(item.tag) == "table"), None)
        if row is None or table is None:
            return {}
        rows = [item for item in table.iter() if isinstance(item.tag, str) and qname_local_name(item.tag) == "tr"]
        cells = [item for item in row if isinstance(item.tag, str) and qname_local_name(item.tag) in {"td", "th"}]
        row_index, column_index = rows.index(row), cells.index(cell)
        headers: list[str] = []
        header_ids = (cell.get("headers") or "").split()
        if header_ids:
            headers.extend("".join(item.itertext()).strip() for item in self.elements if item.get("id") in header_ids)
        for item in table.iter():
            if not isinstance(item.tag, str) or qname_local_name(item.tag) != "th":
                continue
            scope = (item.get("scope") or "").lower()
            item_row = next((parent for parent in item.iterancestors() if qname_local_name(parent.tag) == "tr"), None)
            if item_row is None:
                continue
            item_cells = [
                child
                for child in item_row
                if isinstance(child.tag, str) and qname_local_name(child.tag) in {"td", "th"}
            ]
            if scope == "row" and item_row is row or scope == "col" and item_cells.index(item) == column_index:
                headers.append("".join(item.itertext()).strip())
        context = {"table_position": f"row {row_index + 1}, column {column_index + 1}"}
        if headers:
            context["table_headers"] = _bounded(" | ".join(dict.fromkeys(headers)), _CONTEXT_LIMIT)
        return context

    def _footnote_context(self, unit: Unit) -> str:
        notes: list[str] = []
        for entry in unit.registry.values():
            if entry.kind != "x" or entry.boundary_type != "footnote":
                continue
            href = self._element_for_key(entry.source_node_key).get("href") or ""
            if not href.startswith("#"):
                continue
            target = next((node for node in self.elements if node.get("id") == href[1:]), None)
            if target is not None:
                notes.append("".join(target.itertext()).strip())
        return _bounded(" | ".join(notes), _CONTEXT_LIMIT)

    def _collect_derived_bindings(self) -> None:
        for node in self.elements:
            name = qname_local_name(node.tag)
            node_key = self.node_keys[node]
            source_text = _bounded("".join(node.itertext()).strip(), _CONTEXT_LIMIT)
            unit_id = self._unit_for_node(node_key)
            href = node.get("href") if name in {"a", "area"} else None
            if href:
                self.derived_bindings.append(
                    {
                        "kind": "href_candidate",
                        "source_resource": self.resource_path,
                        "source_node_key": node_key,
                        "source_unit_id": unit_id,
                        "href": href,
                        "source_text": source_text,
                    }
                )
            if name in {"h1", "h2", "h3", "h4", "h5", "h6", "title"} and source_text:
                self.derived_bindings.append(
                    {
                        "kind": "title_candidate",
                        "source_resource": self.resource_path,
                        "source_node_key": node_key,
                        "source_unit_id": unit_id,
                        "fragment": node.get("id", ""),
                        "source_text": source_text,
                    }
                )

        if qname_local_name(self.tree.getroot().tag) == "ncx":
            for nav_point in self.elements:
                if qname_local_name(nav_point.tag) != "navpoint":
                    continue
                label = next(
                    (
                        item
                        for item in nav_point.iter()
                        if isinstance(item.tag, str)
                        and qname_local_name(item.tag) == "text"
                        and item.getparent() is not None
                        and qname_local_name(item.getparent().tag) == "navlabel"
                    ),
                    None,
                )
                content = next(
                    (
                        item
                        for item in nav_point.iter()
                        if isinstance(item.tag, str) and qname_local_name(item.tag) == "content" and item.get("src")
                    ),
                    None,
                )
                if label is None or content is None:
                    continue
                label_key = self.node_keys[label]
                self.derived_bindings.append(
                    {
                        "kind": "href_candidate",
                        "source_resource": self.resource_path,
                        "source_node_key": label_key,
                        "source_unit_id": self._unit_for_node(label_key),
                        "href": content.get("src", ""),
                        "source_text": _bounded("".join(label.itertext()).strip(), _CONTEXT_LIMIT),
                    }
                )

    def _unit_for_node(self, node_key: str) -> str:
        for unit in self.units:
            if unit.node_key == node_key or any(entry.source_node_key == node_key for entry in unit.registry.values()):
                return unit.unit_id
        return ""

    def _finalize_unowned_slots(self) -> None:
        for slot in self.slots.values():
            if slot.ranges or not slot.source_value:
                continue
            node = self._element_for_key(slot.node_key)
            if self._inside_hard(node) or not self._translate_state_chain(node):
                kind = "protected"
            elif not slot.source_value.strip():
                kind = "whitespace"
            else:
                kind = "out_of_scope"
            slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind=kind))

    def _slot_model(self, slot: _Slot) -> SourceSlot:
        owner_kind = slot.ranges[0].owner_kind if len(slot.ranges) == 1 else None
        owner_unit_id = slot.ranges[0].owner_unit_id if len(slot.ranges) == 1 else None
        return SourceSlot(
            slot_id=slot.slot_id,
            node_key=slot.node_key,
            field=slot.field,
            source_value=slot.source_value,
            ranges=tuple(slot.ranges),
            owner_kind=owner_kind,
            owner_unit_id=owner_unit_id,
            attribute_name=slot.attribute_name,
        )

    def _slot_for(self, node_key: str, field_name: SlotField) -> _Slot | None:
        special_ids = {slot.slot_id for slot in self.non_element_tail_slots.values()}
        return next(
            (
                slot
                for slot in self.slots.values()
                if slot.node_key == node_key and slot.field == field_name and slot.slot_id not in special_ids
            ),
            None,
        )

    def _tail_slot(self, node: etree._Element) -> _Slot | None:
        if isinstance(node.tag, str):
            return self._slot_for(self.node_keys[node], "tail")
        return self.non_element_tail_slots.get(node)

    def _element_for_key(self, node_key: str) -> etree._Element:
        return next(node for node, key in self.node_keys.items() if key == node_key)

    def _mark_subtree(self, node: etree._Element, owner_kind: OwnerKind, *, preserve_attributes: bool = False) -> None:
        keys = {self.node_keys[item] for item in node.iter() if isinstance(item.tag, str)}
        for slot in self.slots.values():
            is_root_tail = slot.node_key == self.node_keys[node] and slot.field == "tail"
            is_separate_attribute = preserve_attributes and slot.field == "attribute"
            if slot.node_key in keys and not slot.ranges and not is_root_tail and not is_separate_attribute:
                slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind=owner_kind))

    def _translate_state(self, node: etree._Element, inherited: bool) -> bool:
        if node in self.translate_overrides:
            return self.translate_overrides[node]
        value = (node.get("translate") or "").strip().lower()
        if value in {"no", "false", "0"}:
            return False
        if value in {"yes", "true", "1"}:
            return True
        return inherited

    def _translate_state_chain(self, node: etree._Element) -> bool:
        chain = list(node.iterancestors())[::-1] + [node]
        translated = True
        for item in chain:
            if self._is_hard(item):
                return False
            translated = self._translate_state(item, translated)
        return translated

    def _has_translate_yes(self, node: etree._Element) -> bool:
        return any(
            self.translate_overrides.get(item) is True
            or (item.get("translate") or "").strip().lower() in {"yes", "true", "1"}
            for item in node.iterdescendants()
            if isinstance(item.tag, str)
        )

    def _is_hard(self, node: etree._Element) -> bool:
        return isinstance(node.tag, str) and (
            qname_local_name(node.tag) in _HARD_TAGS or self._is_short_inline_code(node)
        )

    def _is_short_inline_code(self, node: etree._Element) -> bool:
        if qname_local_name(node.tag) != "span" or len(node):
            return False
        classes = " ".join((node.get("class") or "").split())
        if not _MONOSPACE_CLASS_RE.search(classes):
            return False
        text = "".join(node.itertext()).strip()
        if not text or len(text) > 80 or "\n" in text or len(text.split()) > 8:
            return False
        if _RUST_COMMAND_RE.fullmatch(text) or _IDENTIFIER_RE.search(text):
            return True
        if re.search(r"(?:^|\s)(?:--?[A-Za-z][\w-]*|[~./\\][^\s]+)(?:\s|$)", text):
            return True
        if text.startswith(("$ ", "> ", ">>> ", "# ")):
            return True
        return bool(re.fullmatch(r"(?:println!|[(){};]|%[A-Za-z_][A-Za-z0-9_]*%)", text))

    def _inside_hard(self, node: etree._Element) -> bool:
        return any(self._is_hard(item) for item in [node, *node.iterancestors()])

    def _is_atom(self, node: etree._Element) -> bool:
        name = qname_local_name(node.tag)
        return self._is_hard(node) or name in _MEDIA_TAGS | _EMPTY_BOUNDARIES or self._is_footnote_ref(node)

    def _is_footnote_ref(self, node: etree._Element) -> bool:
        epub_type = (node.get(_EPUB_TYPE) or node.get("epub:type") or "").lower()
        role = (node.get("role") or "").lower()
        return "noteref" in epub_type.split() or role == "doc-noteref"

    def _boundary_type(self, node: etree._Element) -> str:
        name = qname_local_name(node.tag)
        if name in {"code", "pre", "kbd", "samp", "var", "tt"} or self._is_short_inline_code(node):
            return "code"
        if name in {"math", "svg"}:
            return "math"
        if name in _MEDIA_TAGS:
            return "media"
        if self._is_footnote_ref(node):
            return "footnote"
        return name if name in _EMPTY_BOUNDARIES else "protected"

    def _inline_reorder_allowed(self, node: etree._Element) -> bool:
        if qname_local_name(node.tag) in {"ruby", "rb", "rt", "rp", "bdo", "bdi"}:
            return False
        style = node.get("style")
        if style and scan_inline_style(style).policy != ReorderPolicy.REORDER_ALLOWED:
            return False
        for ancestor in node.iterancestors():
            inherited_style = ancestor.get("style")
            if not inherited_style:
                continue
            scan = scan_inline_style(inherited_style)
            if scan.policy == ReorderPolicy.UNKNOWN or any(
                constraint.mode == "descendants" for constraint in scan.constraints
            ):
                return False
        return True

    def _style_reorder_allowed(self, node: etree._Element) -> bool:
        return (
            not self.style_document_fallback
            and node not in self.style_locked_elements
            and node.getparent() not in self.style_locked_parents
        )

    def _kind(self, node: etree._Element, virtual: bool = False) -> str:
        name = qname_local_name(node.tag)
        if virtual:
            return f"{name}_text_region"
        if name in {"h1", "h2", "h3", "h4", "h5", "h6", "title"}:
            return "heading"
        if name in {"td", "th", "caption"}:
            return "table_cell"
        if name == "figcaption":
            return "figure_caption"
        if name in {"li", "dt", "dd"}:
            return "list_item"
        return "paragraph"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _resolve_local_resource(base_resource: str, href: str) -> str | None:
    parsed = urlsplit(href)
    if not parsed.path or parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
        return None
    path = PurePosixPath(base_resource).parent / PurePosixPath(unquote(parsed.path))
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts) if parts else None


def _bounded(value: str, limit: int, *, tail: bool = False) -> str:
    clusters = regex.findall(r"\X", value)
    if len(value) <= limit:
        return value
    selected: list[str] = []
    size = 0
    iterable = reversed(clusters) if tail else iter(clusters)
    for cluster in iterable:
        if size + len(cluster) > limit:
            break
        selected.append(cluster)
        size += len(cluster)
    if tail:
        selected.reverse()
    return "".join(selected)


def extract_document(
    source_markup: str,
    resource_path: str,
    source_hash: str,
    media_type: str = "application/xhtml+xml",
    config: Mapping[str, Any] | None = None,
    styles: Any = None,
) -> DocumentPlan:
    """Extract one UTF-8 EPUB text resource without normalizing its source string."""

    if not isinstance(source_markup, str):
        raise TypeError("source_markup must be decoded UTF-8 text")
    return _Extractor(source_markup, resource_path, source_hash, media_type, config or {}, styles).extract()


__all__ = ["ADAPTER_VERSION", "EXTRACTOR_VERSION", "extract_document", "select_primary_title"]
