from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import pytest

from engine.item.atoms import extract_resource
from engine.item.request import SourceIndex, build_payload, select_terms
from engine.schemas.bridge import AtomicDocument
from engine.schemas.contracts import FrozenTerm, GlossarySnapshot, ItemRecord, TermScope, canonical_hash

XHTML = "http://www.w3.org/1999/xhtml"


def test_repeated_term_selection_reuses_matches_and_keeps_context_and_glossary_local(monkeypatch):
    import engine.item.request as requests

    doc = inventory("<p>Memory allocation.</p><p>RAM model.</p>")
    previous, current = doc.items
    index = SourceIndex((doc,))
    terms = (
        term("memory", "Memory", "内存", TermScope(kind="book"), aliases=("RAM",)),
        term("allocation", "allocation", "分配", TermScope(kind="book")),
    )
    contexts = ((previous, index.text(previous)),)
    calls = []
    occurs = requests._occurs

    def counted(*args):
        calls.append(args)
        return occurs(*args)

    monkeypatch.setattr(requests, "_occurs", counted)
    selected = select_terms(current, terms, index, contexts)
    assert {value["term_id"]: value["role"] for value in selected} == {
        "memory": "target",
        "allocation": "context",
    }
    count = len(calls)
    selected[0]["target"] = "corrupted return value"
    repeated = select_terms(current, terms, index, contexts)
    assert len(calls) == count
    assert all(value["target"] != "corrupted return value" for value in repeated)
    without_context = select_terms(current, terms, index, ((previous, "Other text."),))
    assert [value["term_id"] for value in without_context] == ["memory"]
    changed = (terms[0].model_copy(update={"target": "新译文"}),)
    assert select_terms(current, changed, index, ())[0]["target"] == "新译文"


@pytest.mark.parametrize(
    ("spelling", "text", "match_policy", "expected"),
    (
        ("RAM", "RAMBO", "exact", False),
        ("RAM", "ram", "casefold", True),
        ("C++", "C++ model", "exact", True),
        ("memory", "memory_cache", "exact", False),
    ),
)
def test_cached_term_matching_keeps_literal_word_boundaries(spelling, text, match_policy, expected):
    import engine.item.request as requests

    value = term("term", spelling, "译文", TermScope(kind="book"), match_policy=match_policy)
    assert requests._occurs(value, text) is expected
    assert requests._occurs(value, text) is expected


def source(body: str) -> bytes:
    return f'<html xmlns="{XHTML}"><head/><body>{body}</body></html>'.encode()


def inventory(body: str, *, path: str = "OPS/chapter.xhtml", source_hash: str = "book") -> AtomicDocument:
    return extract_resource(source(body), path, source_hash)


def glossary(*terms: FrozenTerm, source_hash: str = "book") -> GlossarySnapshot:
    return GlossarySnapshot(
        source_hash=source_hash,
        freeze_id="freeze",
        extraction_config_hash="config",
        user_terms_hash="user",
        extraction_status="closed",
        warnings=() if terms else ("no terms",),
        terms=terms,
    )


def term(
    term_id: str,
    source_text: str,
    target: str,
    scope: TermScope,
    *,
    aliases: tuple[str, ...] = (),
    mode: Literal["required", "preferred", "keep_source"] = "preferred",
    match_policy: Literal["exact", "casefold"] = "exact",
    note: str = "",
) -> FrozenTerm:
    return FrozenTerm(
        term_id=term_id,
        source=source_text,
        target=target,
        aliases=aliases,
        scope=scope,
        mode=mode,
        match_policy=match_policy,
        note=note,
        origin="user",
    )


def record(item, target: str | None = None) -> ItemRecord:
    target = item.source_projection if target is None else target
    return ItemRecord(
        item_id=item.item_id,
        segment_id=item.item_id,
        terms_hash="terms",
        context_hash="context",
        target_projection=target,
        target_hash=canonical_hash(target),
    )


def keys(value) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | set().union(*(keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(child) for child in value))
    return set()


def test_context_is_shared_bounded_and_excludes_every_batch_member() -> None:
    doc = inventory(f"<p>first</p><p>{'a' * 410}second</p><p>{'b' * 410}third</p><p>fourth</p>")
    index = SourceIndex((doc,))
    first, second, third, fourth = doc.items

    single = build_payload("translate", (fourth,), glossary(), index, request_id="r1")
    assert len(single["context"]) == 2
    assert single["context"][0].endswith("second")
    assert single["context"][1].endswith("third")
    assert all(len(value) <= 400 for value in single["context"])
    assert all("context" not in item for item in single["items"])

    batch = build_payload("translate", (third, fourth), glossary(), index, request_id="r2")
    assert batch["context"] == ["first", ("a" * 394) + "second"]
    assert "third" not in "".join(batch["context"])
    assert batch["target_language"] == "zh-Hans"
    assert [item["item_id"] for item in batch["items"]] == [third.item_id, fourth.item_id]
    assert first.item_id in index.items_by_id and second.document_id in index.documents


def test_context_uses_one_joint_source_view_fragment_per_atomic_item() -> None:
    doc = inventory(
        "<table><tr><th>Heading</th><th>Value</th></tr><tr><td>Memory</td><td>Fast</td></tr></table>"
        "<p>Current paragraph.</p>"
    )
    table, paragraph = doc.items
    assert len(table.source_view_ids) > 1
    payload = build_payload("translate", (paragraph,), glossary(), SourceIndex((doc,)), request_id="r")
    assert payload["context"] == ["Heading\nValue\nMemory\nFast"]


def test_terms_preserve_semantics_but_keep_scope_local_to_selection() -> None:
    doc = inventory("<p>Memory model.</p><p>RAM MODEL.</p>")
    previous, current = doc.items
    frozen = glossary(
        term(
            "target",
            "memory",
            "内存",
            TermScope(kind="units", unit_ids=(current.unit_id,)),
            aliases=("RAM",),
            mode="required",
            match_policy="casefold",
            note="Use the hardware sense; retain this full note.",
        ),
        term(
            "context",
            "Memory",
            "存储器",
            TermScope(kind="units", unit_ids=(previous.unit_id,)),
            mode="required",
            note="Reference only.",
        ),
        term("absent", "Other", "其他", TermScope(kind="book")),
    )
    payload = build_payload("translate", (current,), frozen, SourceIndex((doc,)), request_id="r")
    terms = {value["term_id"]: value for value in payload["items"][0]["terms"]}

    assert set(terms) == {"target", "context"}
    assert terms["target"] == {
        "term_id": "target",
        "source": "memory",
        "target": "内存",
        "aliases": ["RAM"],
        "mode": "required",
        "match_policy": "casefold",
        "note": "Use the hardware sense; retain this full note.",
        "role": "target",
    }
    assert terms["context"]["role"] == "context"
    assert terms["context"]["mode"] == "required"
    assert "scope" not in terms["target"] and "scope" not in terms["context"]


def test_context_terms_are_selected_only_from_the_visible_suffix() -> None:
    doc = inventory(f"<p>RemovedTerm {'x' * 500}</p><p>Current.</p>")
    payload = build_payload(
        "translate",
        (doc.items[1],),
        glossary(term("trimmed", "RemovedTerm", "已移除", TermScope(kind="book"))),
        SourceIndex((doc,)),
        request_id="r",
    )
    assert payload["context"] == ["x" * 400]
    assert payload["items"][0]["terms"] == []


def test_review_requires_current_hash_bound_valid_targets_and_revisions() -> None:
    doc = inventory("<p>Use <em>memory</em>.</p>")
    item = doc.items[0]
    index = SourceIndex((doc,))
    frozen = glossary()

    with pytest.raises(ValueError, match="current saved targets"):
        build_payload("review", (item,), frozen, index, request_id="r")
    with pytest.raises(ValueError, match="exactly cover"):
        build_payload(
            "review",
            (item,),
            frozen,
            index,
            request_id="r",
            targets={item.item_id: record(item)},
            revisions={},
        )
    broken = item.source_projection.replace("⟦-g1⟧", "")
    with pytest.raises(ValueError):
        build_payload(
            "review",
            (item,),
            frozen,
            index,
            request_id="r",
            targets={item.item_id: record(item, broken)},
            revisions={item.unit_id: 3},
        )

    payload = build_payload(
        "review",
        (item,),
        frozen,
        index,
        request_id="r",
        targets={item.item_id: record(item)},
        revisions={item.unit_id: 3},
    )
    wire = payload["items"][0]
    assert payload["protocol"] == "epubox-review-2"
    assert "target_language" not in payload
    assert wire["target"] == item.source_projection
    assert wire["base_revision"] == 3
    assert wire["applicability"] == {"terminology": False, "bindings": True}
    assert all(set(binding) == {"ref", "source", "target"} for binding in wire["bindings"])


def test_marker_projection_exposes_only_local_semantics_and_bounded_excerpts() -> None:
    doc = inventory("<p>Before <em>movable</em> <code>" + ("x" * 450) + "</code> literal ⟦=x8⟧.</p>")
    item = doc.items[0]
    payload = build_payload("translate", (item,), glossary(), SourceIndex((doc,)), request_id="r")
    wire = payload["items"][0]

    assert set(wire["constraints"]) == set(item.registry)
    assert all(value["parent"] == "root" or value["parent"] in item.registry for value in wire["constraints"].values())
    assert max(len(value.get("excerpt", "")) for value in wire["hints"].values()) <= 400
    assert any(value.get("class") == "code" and value.get("excerpt") == "x" * 400 for value in wire["hints"].values())
    assert all(
        "excerpt" not in wire["hints"].get(ref, {}) for ref, entry in item.registry.items() if entry.kind == "g"
    )
    literal = next(value for value in wire["hints"].values() if value.get("class") == "literal_marker")
    assert literal["literal"] == "⟦=x8⟧"
    assert not ({"scope", "source_hash", "source_node_key", "node_key", "slot_id", "start", "end"} & keys(payload))
    assert "<code>" not in str(payload)


def test_unknown_required_marker_metadata_is_rejected_instead_of_omitted() -> None:
    saved = inventory("<p>Use <em>memory</em>.</p>")
    item = saved.items[0]
    ref_id, entry = next(iter(item.registry.items()))
    changed = entry.model_copy(update={"hints": entry.hints | {"future_required": "yes"}})
    forged = item.model_copy(update={"registry": item.registry | {ref_id: changed}})
    units = tuple(forged if unit.unit_id == forged.unit_id else unit for unit in saved.document.units)
    document = saved.document.model_copy(update={"units": units})
    inventory_data = AtomicDocument.model_validate(
        saved.model_dump(mode="python")
        | {"document": document.model_dump(mode="python"), "items": (forged.model_dump(),)}
    )

    with pytest.raises(ValueError, match="unsupported required marker hints"):
        build_payload(
            "translate", (inventory_data.items[0],), glossary(), SourceIndex((inventory_data,)), request_id="r"
        )

    changed_item = saved.items[0].model_copy(update={"checks": (*saved.items[0].checks, "future_check")})
    changed_units = tuple(
        changed_item if unit.unit_id == changed_item.unit_id else unit for unit in saved.document.units
    )
    changed_document = saved.document.model_copy(update={"units": changed_units})
    changed_inventory = saved.model_copy(update={"document": changed_document, "items": (changed_item,)})
    with pytest.raises(ValueError, match="unsupported required atomic checks"):
        SourceIndex((changed_inventory,))


def test_source_index_rejects_foreign_reordered_and_mixed_channel_items() -> None:
    doc = inventory('<p>First.</p><p>Second.<img alt="Label"/></p>')
    first, second, attribute = doc.items
    index = SourceIndex((doc,))
    with pytest.raises(ValueError, match="reading order"):
        index.validate_items((second, first))
    with pytest.raises(ValueError, match="content channel"):
        index.validate_items((second, attribute))
    foreign = inventory("<p>Foreign.</p>", path="OPS/other.xhtml").items[0]
    with pytest.raises(ValueError, match="not owned"):
        index.validate_items((foreign,))

    bypassed = doc.model_copy(
        update={"items": (first.model_copy(update={"source_projection": "Changed"}), second, attribute)}
    )
    with pytest.raises(ValueError, match="authoritative source"):
        SourceIndex((bypassed,))
