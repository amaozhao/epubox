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
