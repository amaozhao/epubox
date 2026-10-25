from __future__ import annotations

import re

import pytest

from engine.agents.workflow import _target_error
from engine.schemas.bridge import ByteSpan
from engine.schemas.contracts import RegistryEntry
from engine.schemas.members import RequestMember

TITLE = "ReAct: Synergizing Reasoning and Acting in Language Models"
ENGLISH_SENTENCE = "This replacement remains entirely untranslated and contains a complete English sentence."
TOKENIZER_SENTENCE = (
    "Take this 22-word sentence that I am writing right now, containing common words, selcouth ones, "
    "and ones that I have missspleelled."
)
OVERLONG_TOKENIZER_SENTENCE = (
    "We can see how each word is split as it goes from text to token parts in this long line and we must not keep "
    "all of this plain English when the test has more than forty words in it right now"
)


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


def linked_member(source: str) -> RequestMember:
    refs = {
        ref: RegistryEntry(
            ref_id=ref,
            kind="g",
            source_node_key=f"n-{ref}",
            parent_ref="n1",
            movement="locked",
            source_text=text,
            hints={"element": "a"},
        )
        for ref, text in re.findall(r"⟦\+(g\d+)⟧([^⟦]*)⟦-\1⟧", source)
    }
    return member().model_copy(update={"source_projection": source, "registry": refs})


def attribute_member(source: str) -> RequestMember:
    return member().model_copy(
        update={"source_projection": source, "registry": {}, "kind": "attribute", "channel": "attribute"}
    )


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


def test_chinese_parent_with_repeated_technical_citations_is_not_untranslated() -> None:
    value = member().model_copy(update={"source_projection": "English source paragraph.", "registry": {}})
    target = (
        "参见 [Trail of Bits, 行跳跃]，并采用 Trail of Bits 的 mcp-context-protector，"
        "详见 [Trail of Bits, 上下文保护器]。另见 [Ferrag et al., 协议利用]。"
    )

    assert error(value, target) is None


def test_source_grounded_search_query_may_remain_english() -> None:
    source = (
        "[68] Google Search, results for query “attention is not all you need,” Sep. 13, 2025. "
        "https://www.google.com/search?q=attention+is+not+all+you+need. "
        "A Google search for “attention is not all you need” yields 583,000 results."
    )
    target = (
        "[68] 谷歌搜索，查询“attention is not all you need”，2025 年 9 月 13 日。"
        "https://www.google.com/search?q=attention+is+not+all+you+need。"
        "在谷歌搜索“attention is not all you need”可得到 583,000 个结果。"
    )
    value = member().model_copy(update={"source_projection": source, "registry": {}})

    assert error(value, target) is None


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (
            (
                "Let’s break down TF-IDF using the sample sentence “The sky is blue and beautiful” "
                "and focusing on the word ⟦+g1⟧blue⟦-g1⟧."
            ),
            "让我们使用示例句子“The sky is blue and beautiful”拆解 TF-IDF，并聚焦于单词⟦+g1⟧blue⟦-g1⟧。",
        ),
        (
            (
                "Calculate the exact TF-IDF value for the word ⟦+g1⟧blue⟦-g1⟧ in the sentence "
                "“The sky is blue and beautiful”:"
            ),
            "计算单词⟦+g1⟧blue⟦-g1⟧在句子“The sky is blue and beautiful”中的精确 TF-IDF 值：",
        ),
    ),
)
def test_source_grounded_example_sentence_may_remain_english(source: str, target: str) -> None:
    value = member(title="blue").model_copy(update={"source_projection": source})

    assert error(value, target) is None


def test_tokenizer_diagram_attribute_may_retain_one_bounded_long_example_sentence() -> None:
    source = (
        f"Diagram of the sentence “{TOKENIZER_SENTENCE}” with each token highlighted in alternating light and dark "
        "gray rectangles around individual words, spaces, and subword segments, illustrating how a tokenizer is "
        "breaking the sentence into thirty-three tokens."
    )
    target = (
        f"句子“{TOKENIZER_SENTENCE}”的示意图，其中每个词元以交替的浅灰色和深灰色矩形突出显示，"
        "矩形分别框住单个单词、空格和子词片段，说明分词器如何将该句子拆分成三十三个词元。"
    )

    assert error(attribute_member(source), target) is None


@pytest.mark.parametrize(
    ("source", "target"),
    (
        (
            f"The quotation “{TOKENIZER_SENTENCE}” is presented on a slide.",
            f"幻灯片上展示了引文“{TOKENIZER_SENTENCE}”。",
        ),
        (
            f"Tokenizer diagram of the example sentence “{TOKENIZER_SENTENCE}” split into subword tokens.",
            f"分词器示意图展示了被拆成子词词元的示例句子“{TOKENIZER_SENTENCE.replace('right now', 'today')}”。",
        ),
        (
            f"Tokenization diagram of the sentence “{TOKENIZER_SENTENCE}” split into word and subword tokens.",
            (
                f"分词示意图展示了被拆成单词和子词词元的句子“{TOKENIZER_SENTENCE}”。"
                f"图下注释再次写道：“{TOKENIZER_SENTENCE}”"
            ),
        ),
        (
            f"Tokenization diagram of the sentence “{TOKENIZER_SENTENCE}” split into word and subword tokens.",
            (
                f"分词示意图展示了被拆成单词和子词词元的句子“{TOKENIZER_SENTENCE}”。"
                "This new tail remains entirely untranslated and is not part of the quoted example."
            ),
        ),
        (
            (
                "Illustration of the sentence “This tokenizer diagram shows each token and subword as a word unit "
                "while another tokenization example remains ordinary English prose for readers today.” in boxes."
            ),
            (
                "方框中的句子示意图：“This tokenizer diagram shows each token and subword as a word unit while "
                "another tokenization example remains ordinary English prose for readers today.”"
            ),
        ),
        (
            f"Tokenizer diagram of the sentence “{OVERLONG_TOKENIZER_SENTENCE}” split into subword tokens.",
            f"分词器示意图展示了被拆成子词词元的句子“{OVERLONG_TOKENIZER_SENTENCE}”。",
        ),
        (
            f"Tokenizer diagram of the sentence “{TOKENIZER_SENTENCE}” split into subword tokens.",
            f"“{TOKENIZER_SENTENCE}”",
        ),
        (
            (
                f"Diagram of the sentence “{TOKENIZER_SENTENCE}”. "
                "A tokenizer is discussed in a later, unrelated source sentence."
            ),
            f"句子“{TOKENIZER_SENTENCE}”的示意图。后文另一句讨论了分词器。",
        ),
    ),
    ids=(
        "ordinary-long-quote",
        "rewritten-quote",
        "duplicated-quote",
        "new-untranslated-tail",
        "tokenizer-terms-only-inside-quote",
        "over-forty-words",
        "target-has-no-chinese-context",
        "tokenizer-cue-in-later-source-sentence",
    ),
)
def test_tokenizer_long_quote_exemption_does_not_hide_other_english(source: str, target: str) -> None:
    result = error(attribute_member(source), target)

    assert result is not None and result.startswith("untranslated English remains:")


def test_source_grounded_example_does_not_hide_unquoted_english_prose() -> None:
    source = "Use the sample sentence “The sky is blue and beautiful”."
    target = (
        "使用示例句子“The sky is blue and beautiful”。"
        "This replacement remains entirely untranslated and contains a complete English sentence."
    )
    value = member().model_copy(update={"source_projection": source, "registry": {}})

    result = error(value, target)
    assert result is not None and result.startswith("untranslated English remains:")


def test_generic_example_cue_does_not_exempt_long_quoted_prose_without_a_focused_term() -> None:
    sentence = "This replacement remains entirely untranslated and contains a complete English sentence."
    source = f"For example, “{sentence}”"
    target = f"例如：“{sentence}”"
    value = member().model_copy(update={"source_projection": source, "registry": {}})

    result = error(value, target)
    assert result is not None and result.startswith("untranslated English remains:")


def test_generic_quote_cue_does_not_exempt_long_prose_without_a_focused_term() -> None:
    sentence = "This replacement remains entirely untranslated and contains a complete English sentence."
    source = f"The quote “{sentence}” appears here."
    target = f"这里出现了这段引文：“{sentence}”"
    value = member().model_copy(update={"source_projection": source, "registry": {}})

    result = error(value, target)
    assert result is not None and result.startswith("untranslated English remains:")


def test_untranslated_sentence_in_quotes_without_source_query_cue_still_fails() -> None:
    sentence = "This replacement remains entirely untranslated and contains a complete English sentence."
    source = f"The user wrote “{sentence}”"
    target = f"用户写道：“{sentence}”"
    value = member().model_copy(update={"source_projection": source, "registry": {}})

    result = error(value, target)
    assert result is not None and result.startswith("untranslated English remains:")


def test_source_grounded_query_exemption_is_limited_by_occurrence_count() -> None:
    phrase = "attention is not all you need"
    source = f"Search for “{phrase}”. The user then wrote “{phrase}”."
    target = f"搜索“{phrase}”。用户随后原样写道：“{phrase}”。"
    value = member().model_copy(update={"source_projection": source, "registry": {}})

    result = error(value, target)
    assert result is not None and result.startswith("untranslated English remains:")


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


def test_source_citations_with_year_or_undated_suffix_may_remain_english() -> None:
    source = (
        "Posthumanism breaks human anthropocentrism (⟦+g1⟧Al-Omari & Al-Omari, 2025⟦-g1⟧). "
        "AI will blur human and machine collaboration (⟦+g2⟧Oodles Blockchain 2024⟦-g2⟧). "
        "Health apps followed (⟦+g3⟧Smartphone History: The Timeline of a Modern Marvel, n.d⟦-g3⟧.). "
        "Communities may connect (⟦+g4⟧Global Council for the Protion of International Trade, 2025⟦-g4⟧)."
    )
    target = (
        "后人类主义打破了人类中心主义（⟦+g1⟧Al-Omari & Al-Omari, 2025⟦-g1⟧）。"
        "人工智能将模糊人机协作边界（⟦+g2⟧Oodles Blockchain 2024⟦-g2⟧）。"
        "随后出现了健康应用（⟦+g3⟧Smartphone History: The Timeline of a Modern Marvel, n.d⟦-g3⟧）。"
        "数字社区可能由此连接（⟦+g4⟧Global Council for the Protection of International Trade, 2025⟦-g4⟧）。"
    )

    assert error(linked_member(source), target) is None


def test_source_research_system_names_may_remain_english() -> None:
    source = (
        "Examples include Reactable (⟦+g1⟧Jorda, 2008⟦-g1⟧), the Slot Machine "
        "(⟦+g2⟧McNerney, 2004⟦-g2⟧), Passive Props (⟦+g3⟧Hinckley et al., 1994⟦-g3⟧), "
        "Environment Audio (⟦+g4⟧Oba, 1990⟦-g4⟧), Tangible Remote Controllers "
        "(⟦+g5⟧Jansen et al., 2012⟦-g5⟧), Morphology Extension Kit "
        "(⟦+g6⟧Leigh et al., 2018⟦-g6⟧), and Mediating the Sacred "
        "(⟦+g7⟧Markum et al., 2024⟦-g7⟧)."
    )
    target = (
        "示例包括 Reactable（⟦+g1⟧Jorda, 2008⟦-g1⟧）、Slot Machine（⟦+g2⟧McNerney, 2004⟦-g2⟧）、"
        "Passive Props（⟦+g3⟧Hinckley et al., 1994⟦-g3⟧）、Environment Audio（⟦+g4⟧Oba, 1990⟦-g4⟧）、"
        "Tangible Remote Controllers（⟦+g5⟧Jansen et al., 2012⟦-g5⟧）、Morphology Extension Kit"
        "（⟦+g6⟧Leigh et al., 2018⟦-g6⟧）以及 Mediating the Sacred（⟦+g7⟧Markum et al., 2024⟦-g7⟧）。"
    )

    assert error(linked_member(source), target) is None


def test_source_math_symbols_may_remain_english() -> None:
    source = (
        "Here, the constants ⟦+g1⟧a⟦-g1⟧ and ⟦+g2⟧b⟦-g2⟧ fit the average observed "
        "⟦+g3⟧MT⟦-g3⟧ for each combination of ⟦+g4⟧A⟦-g4⟧ and ⟦+g5⟧W⟦-g5⟧."
    )
    target = (
        "在此，常数 ⟦+g1⟧a⟦-g1⟧ 和 ⟦+g2⟧b⟦-g2⟧ 是拟合所有观测 "
        "⟦+g3⟧MT⟦-g3⟧ 平均值的系数，适用于每种 ⟦+g4⟧A⟦-g4⟧ 和 ⟦+g5⟧W⟦-g5⟧ 组合。"
    )

    assert error(linked_member(source), target) is None


def test_source_article_titles_may_remain_english() -> None:
    source = (
        "Rows list article examples such as Elham Collee, Good to See You Again, Fernando, "
        "School’s Out and Other Hits, I Am the Future, and Eddie Van Halen."
    )
    target = (
        "各行列出了诸如 Elham Collee、Good to See You Again、Fernando、"
        "School’s Out and Other Hits、I Am the Future 以及 Eddie Van Halen 等文章示例。"
    )

    assert error(member().model_copy(update={"source_projection": source, "registry": {}}), target) is None


def test_source_identical_english_prose_still_fails() -> None:
    sentence = "The client will automatically retrieve the new URL."
    value = member().model_copy(update={"source_projection": sentence, "registry": {}})

    result = error(value, f"说明如下：{sentence}")

    assert result is not None and result.startswith("untranslated English remains:")


def test_source_marker_ending_in_year_does_not_hide_english_prose() -> None:
    sentence = "This replacement remains entirely untranslated in 2025"
    source = f"⟦+g1⟧{sentence}⟦-g1⟧"

    result = error(linked_member(source), source)

    assert result is not None and result.startswith("untranslated English remains:")


def test_unrelated_target_citation_does_not_get_source_exemption() -> None:
    source = "⟦+g1⟧Global Council for Trade, 2025⟦-g1⟧"
    target = "⟦+g1⟧This Organization Will Replace the Source Citation, 2025⟦-g1⟧"

    result = error(linked_member(source), target)

    assert result is not None and result.startswith("untranslated English remains:")


def test_one_shared_author_does_not_exempt_replacement_citation_prose() -> None:
    source = "⟦+g1⟧Smith and Jones, 2025⟦-g1⟧"
    target = "⟦+g1⟧Smith Will Replace Every Important Source Citation With Untranslated Prose, 2025⟦-g1⟧"

    result = error(linked_member(source), target)

    assert result is not None and result.startswith("untranslated English remains:")


def test_source_identical_title_case_prose_still_fails_in_chinese_context() -> None:
    sentence = "The Client Will Automatically Retrieve the New URL"
    value = member().model_copy(update={"source_projection": sentence, "registry": {}})

    result = error(value, f"说明如下：{sentence}。")

    assert result is not None and result.startswith("untranslated English remains:")
