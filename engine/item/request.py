"""Minimal model-visible projection for whole atomic translation items."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from types import MappingProxyType
from typing import Any

import regex

from engine.item.inline import parse_projection, validate_item_target
from engine.item.views import validate_source_views
from engine.schemas.bridge import AtomicDocument, AtomicItem
from engine.schemas.budget import BudgetStage
from engine.schemas.contracts import DocumentPlan, FrozenTerm, GlossarySnapshot, ItemRecord, canonical_hash

_HINTS = frozenset(
    {"element", "source_view_boundary", "readonly", "node_kind", "slot_id", "start", "end", "child_index"}
)
_CHECKS = frozenset({"format_binding", "plain_text", "projection", "source_target"})
_REGISTRY_FIELDS = frozenset(
    {
        "boundary_type",
        "fixed_order",
        "hints",
        "kind",
        "movement",
        "parent_ref",
        "ref_id",
        "reorder_allowed",
        "source_node_key",
        "source_text",
    }
)
_BOUNDARIES = frozenset(
    {
        "anchor",
        "br",
        "code",
        "comment",
        "existing_chinese",
        "footnote",
        "hard_interval",
        "literal_marker",
        "math",
        "media",
        "page",
        "pi",
        "protected",
        "protected_text",
        "wbr",
        "whitespace",
    }
)
_REGION_FIELDS = frozenset(
    {
        "after_node_key",
        "attribute_name",
        "before_node_key",
        "member_node_keys",
        "node_key",
        "parent_node_key",
        "patch_owner_id",
        "safe_boundaries",
        "type",
    }
)


class SourceIndex:
    """Verified lookup and reading order for persisted atomic inventories."""

    def __init__(self, inventories: Sequence[AtomicDocument]):
        saved = tuple(AtomicDocument.model_validate(value.model_dump(mode="python")) for value in inventories)
        if not saved:
            raise ValueError("source index requires at least one atomic inventory")
        documents = {inventory.document.document_id: inventory.document for inventory in saved}
        if len(documents) != len(saved):
            raise ValueError("source index document IDs must be unique")
        source_hashes = {document.source_hash for document in documents.values()}
        if len(source_hashes) != 1:
            raise ValueError("source index cannot mix source identities")
        items = {item.item_id: item for inventory in saved for item in inventory.items}
        if len(items) != sum(len(inventory.items) for inventory in saved):
            raise ValueError("atomic item IDs must be unique across the source index")
        for inventory in saved:
            document = inventory.document
            validate_source_views(document)
            for item in inventory.items:
                unknown = set(item.region) - _REGION_FIELDS
                if unknown:
                    raise ValueError(f"unsupported required atomic region fields: {sorted(unknown)}")
                unknown = set(item.checks) - _CHECKS
                if unknown:
                    raise ValueError(f"unsupported required atomic checks: {sorted(unknown)}")
                if item.context_view_ids:
                    raise ValueError("atomic request context must be selected by SourceIndex")

        self.inventories = saved
        self.documents: Mapping[str, DocumentPlan] = MappingProxyType(documents)
        self.items_by_id: Mapping[str, AtomicItem] = MappingProxyType(items)
        self.source_hash = next(iter(source_hashes))
        self.document_order = tuple(inventory.document.document_id for inventory in saved)
        self._texts = MappingProxyType(
            {
                item.item_id: "\n".join(
                    inventory.document.source_views[view_id].text for view_id in item.source_view_ids
                )
                for inventory in saved
                for item in inventory.items
            }
        )

        self._previous: dict[str, tuple[tuple[AtomicItem, str], ...]] = {}
        for inventory in saved:
            channels: dict[str, tuple[tuple[AtomicItem, str], ...]] = {}
            for item in inventory.items:
                prior = channels.get(item.channel, ())
                self._previous[item.item_id] = prior
                if text := self._texts[item.item_id]:
                    channels[item.channel] = (*prior, (item, text))[-2:]

    def validate_items(self, items: tuple[AtomicItem, ...]) -> None:
        """Require source-owned items in canonical order from one document/channel."""
        if not items:
            raise ValueError("request candidates cannot be empty")
        if len({item.item_id for item in items}) != len(items):
            raise ValueError("request candidates cannot repeat an atomic item")
        for item in items:
            if self.items_by_id.get(item.item_id) != item:
                raise ValueError(f"request candidate is not owned by this source index: {item.item_id}")
        if len({item.document_id for item in items}) != 1:
            raise ValueError("request candidates must belong to one source document")
        if len({item.channel for item in items}) != 1:
            raise ValueError("request candidates must belong to one content channel")
        if tuple(item.ordinal for item in items) != tuple(sorted(item.ordinal for item in items)):
            raise ValueError("request candidates must follow source reading order")

    def text(self, item: AtomicItem) -> str:
        if self.items_by_id.get(item.item_id) != item:
            raise ValueError(f"atomic item is not owned by this source index: {item.item_id}")
        return self._texts[item.item_id]

    def preceding(self, items: tuple[AtomicItem, ...], count: int) -> tuple[tuple[AtomicItem, str], ...]:
        self.validate_items(items)
        if type(count) is not int or not 0 <= count <= 2:
            raise ValueError("context_count must be 0, 1, or 2")
        if not count:
            return ()
        return self._previous[items[0].item_id][-count:]


def build_payload(
    stage: BudgetStage,
    items: tuple[AtomicItem, ...],
    glossary: GlossarySnapshot,
    index: SourceIndex,
    *,
    request_id: str,
    targets: Mapping[str, ItemRecord] | None = None,
    revisions: Mapping[str, int] | None = None,
    context_count: int = 2,
) -> dict[str, Any]:
    """Build one complete candidate payload without execution or persisted batch state."""
    if stage not in {"translate", "review"}:
        raise ValueError(f"unsupported request stage: {stage}")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id must be a non-empty string")
    glossary = GlossarySnapshot.model_validate(glossary.model_dump(mode="python"))
    if glossary.source_hash != index.source_hash:
        raise ValueError("glossary and source index identities differ")
    index.validate_items(items)
    contexts = tuple((item, context_suffix(text, 400)) for item, text in index.preceding(items, context_count))
    review = _review_inputs(stage, items, targets, revisions)

    wire_items: list[dict[str, Any]] = []
    for item in items:
        validate_item_target(item, item.source_projection)
        hints, constraints = marker_fields(item)
        terms = select_terms(item, glossary.terms, index, contexts)
        wire: dict[str, Any] = {
            "item_id": item.item_id,
            "source": item.source_projection,
            "terms": terms,
            "hints": hints,
            "constraints": constraints,
        }
        if stage == "review":
            record = review[item.item_id]
            target = record.target_projection
            assert target is not None
            wire.update(
                target=target,
                base_revision=revisions[item.unit_id],  # type: ignore[index]
                applicability={
                    "terminology": any(term["role"] == "target" for term in terms),
                    "bindings": bool(item.registry),
                },
                bindings=target_bindings(item, target),
            )
        wire_items.append(wire)

    payload: dict[str, Any] = {
        "protocol": "epubox-text-1" if stage == "translate" else "epubox-review-2",
        "request_id": request_id,
        "items": wire_items,
        "context": [text for _, text in contexts],
    }
    if stage == "translate":
        payload["target_language"] = "zh-Hans"
    return payload


def _review_inputs(
    stage: BudgetStage,
    items: tuple[AtomicItem, ...],
    targets: Mapping[str, ItemRecord] | None,
    revisions: Mapping[str, int] | None,
) -> Mapping[str, ItemRecord]:
    if stage == "translate":
        if targets is not None or revisions is not None:
            raise ValueError("translation payloads cannot include saved review targets")
        return {}
    if targets is None or revisions is None:
        raise ValueError("review payloads require current saved targets and revisions")
    item_ids = {item.item_id for item in items}
    unit_ids = {item.unit_id for item in items}
    if set(targets) != item_ids or set(revisions) != unit_ids:
        raise ValueError("review targets and revisions must exactly cover the candidate items")
    for item in items:
        record = targets[item.item_id]
        if not isinstance(record, ItemRecord) or record.item_id != item.item_id or record.segment_id != item.item_id:
            raise ValueError(f"review target identity differs from its atomic item: {item.item_id}")
        target = record.target_projection
        if target is None or record.target_hash != canonical_hash(target):
            raise ValueError(f"review requires a current hash-bound target: {item.item_id}")
        validate_item_target(item, target)
        revision = revisions[item.unit_id]
        if type(revision) is not int or revision < 0:
            raise ValueError(f"review revision must be a non-negative integer: {item.unit_id}")
    return targets


def select_terms(
    item: Any,
    terms: tuple[FrozenTerm, ...],
    index: Any,
    contexts: tuple[tuple[Any, str], ...],
) -> list[dict[str, Any]]:
    cached = getattr(index, "_term_matches", None)
    if cached is None or cached[0] is not terms:
        cached = (terms, tuple(sorted(terms, key=lambda value: value.term_id)), {})
        index._term_matches = cached
    ordered, matches = cached[1:]

    def matching(member: Any, text: str) -> tuple[FrozenTerm, ...]:
        key = (member.document_id, member.unit_id, text)
        if key not in matches:
            matches[key] = tuple(term for term in ordered if _applies(term, member) and _occurs(term, text))
        return matches[key]

    target = {term.term_id: term for term in matching(item, index.text(item))}
    context = {term.term_id: term for prior, text in contexts for term in matching(prior, text)}
    selected: list[dict[str, Any]] = []
    for term_id in sorted(target.keys() | context.keys()):
        term = target.get(term_id) or context[term_id]
        selected.append(
            {
                "term_id": term.term_id,
                "source": term.source,
                "target": term.target,
                "aliases": list(term.aliases),
                "mode": term.mode,
                "match_policy": term.match_policy,
                "note": term.note,
                "role": "target" if term_id in target else "context",
            }
        )
    return selected


def _applies(term: FrozenTerm, item: AtomicItem) -> bool:
    if term.scope.kind == "book":
        return True
    if term.scope.kind == "documents":
        return item.document_id in term.scope.document_ids
    return item.unit_id in term.scope.unit_ids


def _occurs(term: FrozenTerm, text: str) -> bool:
    flags = re.IGNORECASE if term.match_policy == "casefold" else 0
    for spelling in (term.source, *term.aliases):
        if _spelling_pattern(spelling, flags).search(text):
            return True
    return False


@lru_cache(maxsize=8192)
def _spelling_pattern(spelling: str, flags: int) -> re.Pattern[str]:
    left = r"(?<!\w)" if spelling[0].isalnum() or spelling[0] == "_" else ""
    right = r"(?!\w)" if spelling[-1].isalnum() or spelling[-1] == "_" else ""
    return re.compile(left + re.escape(spelling) + right, flags)


def marker_fields(item: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    node_refs = {entry.source_node_key: ref_id for ref_id, entry in item.registry.items() if entry.kind == "g"}
    hints: dict[str, Any] = {}
    constraints: dict[str, Any] = {}
    for ref_id, entry in sorted(item.registry.items()):
        if set(entry.__class__.model_fields) != _REGISTRY_FIELDS:
            raise ValueError("registry contract contains unclassified fields")
        unknown = set(entry.hints) - _HINTS
        if unknown:
            raise ValueError(f"unsupported required marker hints for {ref_id}: {sorted(unknown)}")
        boundary = entry.boundary_type
        if boundary is not None and boundary not in _BOUNDARIES:
            raise ValueError(f"unsupported required marker boundary for {ref_id}: {boundary}")
        if (
            entry.kind == "g"
            and boundary is not None
            or entry.kind == "x"
            and boundary is None
            or entry.kind == "b"
            and boundary != "hard_interval"
        ):
            raise ValueError(f"marker boundary does not match its kind: {ref_id}")
        parent = (
            "root"
            if entry.parent_ref == item.node_key
            else entry.parent_ref
            if entry.parent_ref in item.registry
            else node_refs.get(entry.parent_ref)
        )
        if parent is None:
            raise ValueError(f"marker parent cannot be localized inside its item: {ref_id}")
        if any(value not in item.registry for value in entry.fixed_order):
            raise ValueError(f"marker fixed order references an unknown local marker: {ref_id}")
        constraints[ref_id] = {
            "kind": entry.kind,
            "parent": parent,
            "movement": entry.movement,
            "reorder_allowed": entry.reorder_allowed,
            "fixed_order": list(entry.fixed_order),
        }
        visible: dict[str, Any] = {}
        for key in ("element", "source_view_boundary", "node_kind"):
            if value := entry.hints.get(key):
                visible[key] = value
        if value := entry.hints.get("readonly"):
            visible["readonly"] = _prefix(value, 400)
        if boundary is not None:
            visible["class"] = boundary
        if boundary == "literal_marker":
            visible["literal"] = _prefix(entry.source_text, 400)
        elif boundary in {"code", "footnote", "protected", "protected_text"}:
            visible["excerpt"] = _prefix(entry.source_text, 400)
        if visible:
            hints[ref_id] = visible
    return hints, constraints


def target_bindings(item: Any, target: str) -> list[dict[str, str]]:
    source_ranges, target_ranges = _ranges(item.source_projection), _ranges(target)
    bindings = [
        {"ref": ref_id, "source": text, "target": target_ranges[ref_id]} for ref_id, text in source_ranges.items()
    ]
    bindings.extend(
        {"ref": ref_id, "source": _prefix(entry.source_text, 400), "target_context": ""}
        for ref_id, entry in item.registry.items()
        if entry.boundary_type == "footnote" and f"⟦={ref_id}⟧" in item.source_projection
    )
    return bindings


def _ranges(projection: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    stack: list[str] = []
    for event in parse_projection(projection):
        if event.kind == "text":
            for ref_id in stack:
                result[ref_id].append(event.value)
        elif event.value.startswith("+g"):
            stack.append(event.value[1:])
            result.setdefault(event.value[1:], [])
        elif event.value.startswith("-g"):
            stack.pop()
    return {ref_id: "".join(parts) for ref_id, parts in result.items()}


def _prefix(value: str, limit: int) -> str:
    selected: list[str] = []
    size = 0
    for match in regex.finditer(r"\X", value):
        cluster = match.group()
        if size + len(cluster) > limit:
            break
        selected.append(cluster)
        size += len(cluster)
    return "".join(selected)


def context_suffix(value: str, limit: int) -> str:
    matches = list(regex.finditer(r"\X", value))
    selected: list[str] = []
    size = 0
    for match in reversed(matches):
        cluster = match.group()
        if size + len(cluster) > limit:
            break
        selected.append(cluster)
        size += len(cluster)
    return "".join(reversed(selected))
