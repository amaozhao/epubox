"""Metadata, stylesheet, semantic context, and navigation extraction helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import tinycss2
from lxml import etree  # type: ignore[attr-defined]

from engine.core.markup import element_path, qname_local_name
from engine.core.styles import ReorderPolicy, StyleIssue, StyleScan, scan_stylesheets
from engine.item.inline import Event, events_to_projection, parse_projection
from engine.item.policy import (
    _CONTEXT_LIMIT,
    _LATIN_RE,
    _TERM_MODES,
    _bounded,
    _resolve_local_resource,
    _stable_id,
    _unique_language_match,
    select_primary_title,
)
from engine.schemas.internal import SlotRange, Unit, canonical_hash

if TYPE_CHECKING:
    from engine.item.structure import _Extractor


def _style_policy(self: _Extractor, styles: Any) -> StyleScan:
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


def _style_locks(self: _Extractor, scan: StyleScan) -> tuple[set[etree._Element], set[etree._Element]]:
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
                locked_elements.update(descendant for descendant in element.iter() if isinstance(descendant.tag, str))
        else:
            locked_elements.update(candidates)
    return locked_elements, locked_parents


def _selector_candidates(self: _Extractor, selector: str) -> list[etree._Element]:
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


def _selector_parts(self: _Extractor, tokens: list[Any]) -> tuple[list[list[Any]], list[str]]:
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


def _matches_stable_compound(self: _Extractor, element: etree._Element, tokens: list[Any]) -> bool:
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


def _extract_attributes(self: _Extractor) -> None:
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
        unit_id = _stable_id("u", self.source_hash, self.resource_path, canonical_hash(region), self.extractor_version)
        slot.ranges.append(SlotRange(start=0, end=len(slot.source_value), owner_kind="unit", owner_unit_id=unit_id))
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


def _extract_opf(self: _Extractor, root: etree._Element) -> None:
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


def _finalize_units(self: _Extractor) -> None:
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


def _semantic_config(self: _Extractor) -> dict[str, Any]:
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
    frozen.update({"extractor_version": self.extractor_version, "adapter_version": self.adapter_version})
    return frozen


def _configured_terms(self: _Extractor) -> tuple[dict[str, Any], ...]:
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


def _term_applies(self: _Extractor, term: Mapping[str, Any], unit: Unit, section: str, source_text: str) -> bool:
    scope = str(term.get("scope", "global"))
    in_scope = scope in {"", "*", "global", "book", self.resource_path, unit.kind, section}
    return in_scope and str(term["source"]).casefold() in source_text.casefold()


def _unit_source_text(self: _Extractor, unit: Unit) -> str:
    parts: list[str] = []
    for event in parse_projection(unit.source_projection):
        if event.kind == "text":
            parts.append(event.value)
        elif event.value.startswith("=x"):
            parts.append(unit.registry[event.value[1:]].source_text)
    return "".join(parts)


def _section_for(self: _Extractor, unit: Unit) -> str:
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


def _table_context(self: _Extractor, unit: Unit) -> dict[str, str]:
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
            child for child in item_row if isinstance(child.tag, str) and qname_local_name(child.tag) in {"td", "th"}
        ]
        if scope == "row" and item_row is row or scope == "col" and item_cells.index(item) == column_index:
            headers.append("".join(item.itertext()).strip())
    context = {"table_position": f"row {row_index + 1}, column {column_index + 1}"}
    if headers:
        context["table_headers"] = _bounded(" | ".join(dict.fromkeys(headers)), _CONTEXT_LIMIT)
    return context


def _footnote_context(self: _Extractor, unit: Unit) -> str:
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


def _collect_derived_bindings(self: _Extractor) -> None:
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


def _unit_for_node(self: _Extractor, node_key: str) -> str:
    for unit in self.units:
        if unit.node_key == node_key or any(entry.source_node_key == node_key for entry in unit.registry.values()):
            return unit.unit_id
    return ""
