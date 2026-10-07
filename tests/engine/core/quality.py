import warnings
from html import escape

import pytest
from bs4 import MarkupResemblesLocatorWarning

from engine.core import quality
from engine.core.quality import (
    EnglishResidualDecision,
    classify_untranslated_english_texts,
    find_degenerate_translation,
)


def test_shared_quality_rejects_untranslated_english_prose() -> None:
    findings = classify_untranslated_english_texts(
        "<p>这是说明。The client will automatically retrieve the new URL.</p>"
    )

    assert [finding.decision for finding in findings] == [EnglishResidualDecision.FAIL]


def test_shared_quality_allows_technical_identifiers() -> None:
    assert (
        classify_untranslated_english_texts("<p>使用 AWS CodePipeline、GitHub Actions、Docker 和 Python API。</p>")
        == []
    )


def test_shared_quality_detects_degenerate_repetition() -> None:
    assert find_degenerate_translation(
        "<p>This source paragraph contains enough ordinary prose for comparison.</p>",
        f"<p>{'重复' * 80}</p>",
    )


@pytest.mark.parametrize(
    "text", ("https://example.com/docs?a=1&b=2", "/tmp/chapter.xhtml", "List<ReviewIssue> &copy;")
)
def test_plain_text_quality_preserves_literals_without_constructing_a_markup_parser(monkeypatch, text):
    monkeypatch.setattr(quality, "BeautifulSoup", lambda *_args: pytest.fail("plain text was parsed as markup"))
    assert quality._visible_prose_text(text, markup=False) == text
    quality.classify_untranslated_english_texts(text, markup=False)
    assert quality.find_degenerate_translation(text, text, markup=False) is None


def test_markup_text_fragment_does_not_treat_a_url_as_a_locator():
    with warnings.catch_warnings():
        warnings.simplefilter("error", MarkupResemblesLocatorWarning)
        assert quality.find_untranslated_english_texts("https://example.com/docs") == []
        assert quality.find_degenerate_translation("chapter.xhtml", "chapter.xhtml") is None


def test_plain_text_prose_is_checked_without_decoding_literal_entities():
    text = "The client will automatically retrieve the new URL."
    assert quality.find_untranslated_english_texts(text, markup=False) == [text]
    assert quality._visible_prose_text("文本 &amp; 文字", markup=False) == "文本 &amp; 文字"
    assert quality._visible_prose_text("<p>文本 &amp; 文字</p>") == "文本 & 文字"


def test_translation_workflow_checks_url_projection_as_text(tmp_path, monkeypatch):
    from engine.agents.workflow import _target_error
    from tests.engine.agents.workflow import prepare_case

    url = "https://example.com/docs?a=1&b=2"
    case = prepare_case(tmp_path, f"<p>{escape(url)}</p>", (url,))
    monkeypatch.setattr(quality, "BeautifulSoup", lambda *_args: pytest.fail("workflow text reached markup parser"))
    assert _target_error(case.batch.items[0], url, {"terms": []}) is None


@pytest.mark.parametrize("root", ("div", "container"))
def test_markup_quality_keeps_protected_tags_and_chinese_parent_context(root):
    text = "The client will automatically retrieve the new URL."
    markup = f"<{root}><pre>{text}</pre><script>{text}</script><style>{text}</style><code>{text}</code>"
    markup += f"<p>中文说明。<span>{text}</span></p></{root}>"
    assert quality.find_untranslated_english_texts(markup) == [text]
    assert quality._visible_prose_text(markup) == "中文说明。 " + text
