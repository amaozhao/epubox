from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import engine.services.partition as partition_module
from engine.item.inline import plain_text
from engine.schemas.contracts import ItemRecord, canonical_hash, canonical_json_bytes
from engine.schemas.parts import PartPlan
from engine.services import state
from engine.services.atomic import IdentityMismatch
from engine.services.partition import (
    PartSession,
    build_plan,
    merge_targets,
    parent_id,
    plan_path,
    split_target,
    validate_plan,
)
from engine.services.preparation import prepare_translation
from engine.services.ready import ReadySession
from engine.services.store import RunStore
from tests.engine.epub.test_factory import make_epub
from tests.engine.epub.test_preparation import StubChecker
from tests.engine.services.test_preparing import atomic_config


def _case(tmp_path: Path):
    items = "".join(f"<li>Entry {number}: {'word ' * 110}</li>" for number in range(14))
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": f"<ul>{items}</ul>"})
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(max_source_tokens=3000, max_input_tokens=50_000, context_tokens=60_000),
            StubChecker(),
        )
    )
    assert result.status == "ready"
    session = ReadySession(RunStore(result.work_dir))
    parent = next(member for member in session.index.members if session.index.source_tokens[member.item_id] > 1500)
    return session, parent


def _adaptive_case(tmp_path: Path, markup: str, tag: str):
    source = make_epub(tmp_path / "source.epub", {"chapter.xhtml": markup})
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(max_source_tokens=3000, max_input_tokens=50_000, context_tokens=60_000),
            StubChecker(),
        )
    )
    assert result.status == "ready"
    session = ReadySession(RunStore(result.work_dir))
    parent = next(member for member in session.index.members if member.atomic_tag == tag)
    return session, parent


@pytest.mark.parametrize("tag", ["ul", "ol"])
def test_small_repeatedly_failing_list_can_split_without_discarding_its_draft(tmp_path, tag):
    base, parent = _adaptive_case(
        tmp_path, f"<{tag}><li>First <code>foo</code> step.</li><li>Second step.</li></{tag}>", tag
    )
    assert base.index.source_tokens[parent.item_id] < 1500
    target = parent.source_projection.replace("First", "第一").replace("Second", "第二").replace("step.", "步。")
    seed = ItemRecord(
        item_id=parent.item_id,
        segment_id=parent.item_id,
        terms_hash="terms",
        context_hash="context",
        stage="review",
        target_projection=target,
        target_hash=canonical_hash(target),
    )
    plan = build_plan(base, parent, seed, trigger_stage="review", trigger_count=10)
    assert plan is not None and plan.strategy == "list_items"
    assert len(plan.children) == 2 and plan.trigger_count == 10
    assert merge_targets(parent, plan, split_target(parent, plan.children, target, plan=plan)) == target
    validate_plan(base, plan)


def test_explicit_restart_can_split_a_list_before_the_automatic_failure_limit(tmp_path):
    base, parent = _adaptive_case(tmp_path, "<ol><li>First step.</li><li>Second step.</li></ol>", "ol")
    with pytest.raises(ValueError, match="ten durable"):
        build_plan(base, parent, trigger_stage="translate", trigger_count=1)
    plan = build_plan(base, parent, trigger_stage="translate", trigger_count=1, restart=True)
    assert plan is not None and plan.strategy == "list_items" and plan.trigger_count == 1
    validate_plan(base, plan)


def test_legacy_list_plan_is_deterministic_and_reconstructs_source_and_target(tmp_path: Path) -> None:
    base, parent = _case(tmp_path)
    seed = ItemRecord(
        item_id=parent.item_id,
        segment_id=parent.item_id,
        terms_hash="terms",
        context_hash="context",
    )

    plan = build_plan(base, parent, seed)

    assert plan is not None and plan.seed == seed
    assert plan == build_plan(base, parent, seed)
    assert len(plan.children) == 14
    assert "".join(child.source_projection for child in plan.children) == parent.source_projection
    assert plan.children[0].source_span.byte_start == parent.source_span.byte_start
    assert plan.children[-1].source_span.byte_end == parent.source_span.byte_end
    assert all(
        left.source_span.byte_end == right.source_span.byte_start
        for left, right in zip(plan.children, plan.children[1:])
    )
    assert all(child.parent_item_id == parent.item_id for child in plan.children)
    assert all(child.parent_hash == canonical_hash(parent) for child in plan.children)
    assert all(parent_id(child.item_id) == parent.item_id for child in plan.children)
    assert len({child.preflight_hash for child in plan.children}) == 1
    validate_plan(base, plan)

    target = parent.source_projection.replace("Entry", "条目").replace("word", "文字")
    translated_seed = seed.model_copy(update={"target_projection": target, "target_hash": canonical_hash(target)})
    translated = build_plan(base, parent, translated_seed)
    assert translated is not None and translated.children == plan.children
    validate_plan(base, translated)
    parts = split_target(parent, plan.children, target)
    assert tuple(parts) == tuple(child.item_id for child in plan.children)
    assert "".join(parts.values()) == target


def test_plan_rejects_child_or_seed_tampering(tmp_path: Path) -> None:
    base, parent = _case(tmp_path)
    plan = build_plan(base, parent)
    assert plan is not None
    child = plan.children[0]
    changed = child.model_copy(update={"source_projection": child.source_projection.replace("word", "changed", 1)})

    with pytest.raises(IdentityMismatch, match="children"):
        validate_plan(base, plan.model_copy(update={"children": (changed, *plan.children[1:])}))
    with pytest.raises(ValueError, match="legacy parent"):
        build_plan(
            base,
            parent,
            ItemRecord(item_id="other", segment_id="other", terms_hash="terms", context_hash="context"),
        )


def test_part_session_scopes_children_and_packs_under_hard_limit(tmp_path: Path, monkeypatch) -> None:
    base, parent = _case(tmp_path)
    plan = build_plan(base, parent)
    assert plan is not None
    path = plan_path(base.store.root, parent.item_id)
    state.write(path, canonical_json_bytes(plan))

    session = PartSession(base, plan)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unchanged part verification must not rederive children")

    monkeypatch.setattr(partition_module, "_derive_children", forbidden)

    assert session.prepared is base.prepared
    assert session.index.inventories is base.index.inventories
    assert session.index.documents is base.index.documents
    assert tuple(session.index.members_by_id) == tuple(child.item_id for child in plan.children)
    assert session.verify() == base.prepared
    assert all(session.owns(child) for child in plan.children)
    assert all(batch.manifest.sparse for batch in session._prepared_batches.values())
    assert all(batch.budget.source_tokens <= plan.source_limit for batch in session._prepared_batches.values())
    assert session.result_path(plan.children[0].item_id).parent == path.parent / "results"

    plan.children[0].registry.clear()
    assert not session.owns(plan.children[0])

    state.write(path, canonical_json_bytes(plan.model_copy(update={"source_limit": plan.source_limit - 1})))
    with pytest.raises(IdentityMismatch, match="saved part plan"):
        session.verify()


def test_plan_schema_roundtrips(tmp_path: Path) -> None:
    base, parent = _case(tmp_path)
    plan = build_plan(base, parent)
    assert plan is not None
    assert PartPlan.model_validate_json(canonical_json_bytes(plan)) == plan


def test_legacy_plan_hash_and_payload_remain_byte_contract_compatible(tmp_path: Path) -> None:
    base, parent = _case(tmp_path)
    plan = build_plan(base, parent)
    assert plan is not None and plan.strategy == "legacy_list"
    old_payload = {
        name: value
        for name, value in plan.model_dump(mode="json").items()
        if name not in {"strategy", "separators", "depth", "trigger_stage", "trigger_count"}
    }
    assert set(old_payload) == {
        "format",
        "source_hash",
        "parent_id",
        "parent_hash",
        "root_plan_hash",
        "source_limit",
        "children",
        "seed",
    }
    restored = PartPlan.model_validate(old_payload)
    assert canonical_hash(restored) == canonical_hash(old_payload)
    assert restored == plan


def test_parent_id_parses_whole_and_preflight_piece_parents() -> None:
    digest = "a" * 32
    assert parent_id(f"pc-u-parent-{digest}") == "u-parent"
    assert parent_id(f"pc-pc-original-piece-{digest}") == "pc-original-piece"
    assert parent_id("pc-u-parent-not-a-digest") is None
    assert parent_id("u-parent") is None


def test_store_reads_child_response_sources_only_through_an_authentic_parent(tmp_path: Path) -> None:
    base, parent = _case(tmp_path)
    plan = build_plan(base, parent)
    assert plan is not None
    state.write(plan_path(base.store.root, parent.item_id), canonical_json_bytes(plan))
    session = PartSession(base, plan)
    manifest = next(iter(session._prepared_batches.values())).manifest

    assert base.store.read_response_sources(manifest) == {
        child.item_id: child.source_projection for child in plan.children if child.item_id in manifest.item_ids
    }
    assert base.store.read_response_hints(manifest) == {
        child.item_id: {ref: dict(entry.hints) for ref, entry in child.registry.items()}
        for child in plan.children
        if child.item_id in manifest.item_ids
    }

    state.write(
        base.store._path("members", parent.item_id),
        canonical_json_bytes(parent.model_copy(update={"source_projection": parent.source_projection + "changed"})),
    )
    with pytest.raises(IdentityMismatch, match="parent identity"):
        base.store.read_response_sources(manifest)


def test_tenth_content_failure_freezes_sentence_tasks_and_reconstructs_parent(tmp_path: Path) -> None:
    base, parent = _adaptive_case(tmp_path, "<p>First sentence. Second sentence! Third sentence?</p>", "p")
    failed = ItemRecord(
        item_id=parent.item_id,
        segment_id=parent.item_id,
        terms_hash="terms",
        context_hash="context",
        stage="translate",
    )

    plan = build_plan(base, parent, failed, trigger_stage="translate", trigger_count=10)

    assert plan is not None and plan.strategy == "sentences" and plan.trigger_count == 10
    assert tuple(child.source_projection for child in plan.children) == (
        "First sentence. ",
        "Second sentence! ",
        "Third sentence?",
    )
    assert all(child.atomic_tag == "p" for child in plan.children)
    assert (
        merge_targets(
            parent,
            plan,
            {
                plan.children[0].item_id: "第一句。 ",
                plan.children[1].item_id: "第二句！ ",
                plan.children[2].item_id: "第三句？",
            },
        )
        == "第一句。 第二句！ 第三句？"
    )
    validate_plan(base, plan)


def test_adaptive_sentence_plan_requires_the_tenth_failure_and_a_safe_boundary(tmp_path: Path) -> None:
    base, parent = _adaptive_case(tmp_path, "<p>First sentence. Second sentence.</p>", "p")
    with pytest.raises(ValueError, match="ten durable"):
        build_plan(base, parent, trigger_stage="translate", trigger_count=9)

    single_base, single = _adaptive_case(tmp_path / "single", "<p>One indivisible sentence</p>", "p")
    assert build_plan(single_base, single, trigger_stage="translate", trigger_count=10) is None


def test_sentence_tasks_preserve_inline_emphasis_markers_in_the_parent(tmp_path: Path) -> None:
    base, parent = _adaptive_case(
        tmp_path,
        "<p>First <em>important sentence.</em> Second sentence.</p>",
        "p",
    )
    plan = build_plan(base, parent, trigger_stage="translate", trigger_count=10)

    assert plan is not None and plan.strategy == "sentences"
    assert len(plan.children) >= 2 and any("⟦" in separator for separator in plan.separators)
    targets = {
        child.item_id: child.source_projection.replace("First", "第一")
        .replace("important", "重要")
        .replace("Second", "第二")
        .replace("sentence", "句子")
        for child in plan.children
    }
    merged = merge_targets(parent, plan, targets)
    assert "重要 句子" in merged and "第二 句子" in merged
    validate_plan(base, plan)

    seed = ItemRecord(
        item_id=parent.item_id,
        segment_id=parent.item_id,
        terms_hash="terms",
        context_hash="context",
        target_projection=merged,
        target_hash=canonical_hash(merged),
    )
    review_plan = build_plan(base, parent, seed, trigger_stage="review", trigger_count=10)
    assert review_plan is not None
    assert (
        merge_targets(parent, review_plan, split_target(parent, review_plan.children, merged, plan=review_plan))
        == merged
    )


def test_table_cell_tasks_keep_outer_table_and_row_structure_frozen(tmp_path: Path) -> None:
    base, parent = _adaptive_case(
        tmp_path,
        "<table><tr><th>Head A</th><th>Head B</th></tr><tr><td>Cell A</td><td>Cell B</td></tr></table>",
        "table",
    )
    plan = build_plan(base, parent, trigger_stage="translate", trigger_count=10)

    assert plan is not None and plan.strategy == "table_cells" and len(plan.children) == 4
    translated = {
        child.item_id: child.source_projection.replace("Head", "标题")
        .replace("Cell", "单元格")
        .replace(" A", "甲")
        .replace(" B", "乙")
        for child in plan.children
    }
    merged = merge_targets(parent, plan, translated)
    assert "<table" not in merged and "标题甲" in merged and "单元格乙" in merged
    assert split_target(parent, plan.children, merged, plan=plan) == translated
    validate_plan(base, plan)


def test_repeatedly_failing_table_cell_can_refine_to_verified_sentence_slots(tmp_path: Path) -> None:
    base, parent = _adaptive_case(
        tmp_path,
        "<table><tr><td>First sentence. Second sentence.</td><td>Other cell.</td></tr></table>",
        "table",
    )
    cells = build_plan(base, parent, trigger_stage="translate", trigger_count=10)
    assert cells is not None and cells.strategy == "table_cells"
    state.write(plan_path(base.store.root, parent.item_id), canonical_json_bytes(cells))
    cell_session = PartSession(base, cells)
    stubborn = next(child for child in cells.children if "First sentence" in child.source_projection)

    sentences = build_plan(cell_session, stubborn, trigger_stage="translate", trigger_count=10)

    assert sentences is not None and sentences.strategy == "sentences" and sentences.depth == 2
    assert tuple(plain_text(child.source_projection) for child in sentences.children) == (
        "First sentence. ",
        "Second sentence.",
    )
    assert all(
        stubborn.source_span.byte_start
        <= child.source_span.byte_start
        < child.source_span.byte_end
        <= stubborn.source_span.byte_end
        for child in sentences.children
    )


def test_part_source_lookup_does_not_scan_other_document_inventories(tmp_path, monkeypatch):
    base, parent = _case(tmp_path)

    class NoScan:
        def __iter__(self):
            raise AssertionError("part lookup must use its owning document ID")

    monkeypatch.setattr(base.index, "inventories", NoScan())
    plan = build_plan(base, parent)
    assert plan is not None and plan.parent_id == parent.item_id


def test_resource_index_cache_evicts_by_bytes_and_reparses_deterministically(tmp_path, monkeypatch):
    base, parent = _case(tmp_path)
    monkeypatch.setattr(partition_module, "_RESOURCE_CACHE_BYTES", 0)
    partition_module._resource_indexes.pop(base, None)
    calls = 0
    parse = partition_module.parse_resource

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return parse(*args, **kwargs)

    monkeypatch.setattr(partition_module, "parse_resource", counted)
    first = build_plan(base, parent)
    second = build_plan(base, parent)

    assert first == second and first is not None
    assert calls == 2
    assert partition_module._resource_indexes[base].cached_bytes == 0
