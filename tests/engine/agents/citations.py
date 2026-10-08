from __future__ import annotations

import pytest

from engine.agents.workflow import _target_error
from engine.schemas.bridge import ByteSpan
from engine.schemas.contracts import RegistryEntry
from engine.schemas.members import RequestMember

TITLE = "ReAct: Synergizing Reasoning and Acting in Language Models"
ENGLISH_SENTENCE = "This replacement remains entirely untranslated and contains a complete English sentence."


def member(*, lead: str = "This comes from the paper ", title: str = TITLE, element: str = "em") -> RequestMember:
    return RequestMember(
        item_id="u1",
        parent_item_id="u1",
        parent_hash="0" * 64,
        preflight_hash="1" * 64,
        unit_id="unit1",
        document_id="doc1",
        kind="paragraph",
        channel="body",
        ordinal=0,
        piece_index=0,
        piece_count=1,
        root_node_key="n1",
        source_span=ByteSpan(byte_start=0, byte_end=1),
        source_projection=f"{lead}⟦+g1⟧{title}⟦-g1⟧ (Yao et al., 2022).",
        registry={
            "g1": RegistryEntry(
                ref_id="g1",
                kind="g",
                source_node_key="n2",
                parent_ref="n1",
                movement="same_parent",
                reorder_allowed=True,
                source_text=title,
                hints={"element": element},
            )
        },
        atomic_tag="p",
    )


def error(value: RequestMember, target: str) -> str | None:
    return _target_error(value, target, {"terms": []})


def actual_member() -> RequestMember:
    source = (
        "⟦+b1⟧This ReAct loop⟦-b1⟧⟦=x1⟧⟦+b2⟧ is what initiated the broader agent paradigm."
        "⟦+g1⟧⟦-g1⟧ The majority of modern agent architectures trace back to this algorithmic structure."
        f"⟦+g2⟧⟦-g2⟧ The approach was introduced in the paper ⟦+g3⟧{TITLE}⟦-g3⟧ "
        "(⟦+g4⟧⟦+g5⟧https://arxiv.org/abs/2210.03629⟦-g5⟧⟦-g4⟧).⟦-b2⟧"
    )
    refs = {
        "b1": RegistryEntry(
            ref_id="b1",
            kind="b",
            source_node_key="n2",
            parent_ref="n1",
            movement="fixed",
            boundary_type="hard_interval",
        ),
        "b2": RegistryEntry(
            ref_id="b2",
            kind="b",
            source_node_key="n2",
            parent_ref="n1",
            movement="fixed",
            boundary_type="hard_interval",
        ),
        "x1": RegistryEntry(
            ref_id="x1",
            kind="x",
            source_node_key="n2",
            parent_ref="n1",
            movement="fixed",
            boundary_type="anchor",
        ),
    }
    for ref, element, text in (
        ("g1", "span", ""),
        ("g2", "span", ""),
        ("g3", "em", TITLE),
        ("g4", "a", "https://arxiv.org/abs/2210.03629"),
        ("g5", "span", "https://arxiv.org/abs/2210.03629"),
    ):
        refs[ref] = RegistryEntry(
            ref_id=ref,
            kind="g",
            source_node_key=f"n-{ref}",
            parent_ref="n1",
            movement="locked",
            source_text=text,
            hints={"element": element},
        )
    return member().model_copy(update={"source_projection": source, "registry": refs})


def caption_member(captions: tuple[str, ...]) -> RequestMember:
    refs = {
        f"g{index}": RegistryEntry(
            ref_id=f"g{index}",
            kind="g",
            source_node_key=f"n{index}",
            parent_ref="n1",
            movement="locked",
            source_text=caption,
            hints={"element": "li", "source_view_boundary": "paragraph"},
        )
        for index, caption in enumerate(captions, 1)
    }
    source = "".join(f"⟦+g{index}⟧{caption}⟦-g{index}⟧" for index, caption in enumerate(captions, 1))
    return member().model_copy(update={"source_projection": source, "registry": refs})


@pytest.mark.parametrize("element", ("em", "i", "cite"))
def test_exact_cited_paper_title_may_remain_english(element: str) -> None:
    target = f"这一方法源自论文⟦+g1⟧{TITLE}⟦-g1⟧（Yao 等，2022）。"

    assert error(member(element=element), target) is None


def test_exact_cited_title_may_remain_in_actual_nested_source_shape() -> None:
    target = (
        "⟦+b1⟧该 ReAct 循环⟦-b1⟧⟦=x1⟧⟦+b2⟧引发了更广泛的智能体范式。⟦+g1⟧⟦-g1⟧"
        "大多数现代智能体架构都源于这种算法结构。⟦+g2⟧⟦-g2⟧该方法由论文 "
        f"⟦+g3⟧{TITLE}⟦-g3⟧（⟦+g4⟧⟦+g5⟧https://arxiv.org/abs/2210.03629⟦-g5⟧⟦-g4⟧）提出。⟦-b2⟧"
    )

    assert error(actual_member(), target) is None


@pytest.mark.parametrize(
    ("value", "target"),
    (
        (
            member(),
            f"{ENGLISH_SENTENCE} ⟦+g1⟧{TITLE}⟦-g1⟧。",
        ),
        (member(lead="This emphasizes "), f"这里强调⟦+g1⟧{TITLE}⟦-g1⟧。"),
        (member(element="strong"), f"这里引用论文⟦+g1⟧{TITLE}⟦-g1⟧。"),
        (
            member(),
            "这里引用⟦+g1⟧ReAct: Synergizing Reasoning and Acting with Language Models⟦-g1⟧。",
        ),
        (member(), f"这里引用⟦+g1⟧反应式智能体⟦-g1⟧。{TITLE}"),
    ),
)
def test_other_english_residue_still_fails(value: RequestMember, target: str) -> None:
    result = error(value, target)

    assert result is not None
    assert result.startswith("untranslated English remains:")


def test_proper_names_in_separate_captions_do_not_pool_english_stopwords() -> None:
    value = caption_member(("The Zebra report.", "Women in Tech SEO survey.", "The Athletic article."))
    target = "⟦+g1⟧The Zebra 报告。⟦-g1⟧⟦+g2⟧Women in Tech SEO 调查。⟦-g2⟧⟦+g3⟧The Athletic 文章。⟦-g3⟧"

    assert error(value, target) is None


def test_inline_ranges_do_not_hide_an_untranslated_english_sentence() -> None:
    sentence = "This replacement remains entirely untranslated and contains a complete English sentence."
    value = caption_member((sentence,))
    value = value.model_copy(
        update={
            "source_projection": (
                "⟦+g1⟧This replacement remains ⟦+g2⟧entirely untranslated⟦-g2⟧ "
                "and contains a complete English sentence.⟦-g1⟧"
            ),
            "registry": value.registry
            | {
                "g2": RegistryEntry(
                    ref_id="g2",
                    kind="g",
                    source_node_key="n-inline",
                    parent_ref="g1",
                    movement="same_parent",
                    reorder_allowed=True,
                    source_text="entirely untranslated",
                    hints={"element": "em"},
                )
            },
        }
    )

    result = error(value, value.source_projection)
    assert result is not None and result.startswith("untranslated English remains:")
