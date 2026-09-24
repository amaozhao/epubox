from engine.services.glossary import GlossaryExtractor


def test_glossary_extractor_accepts_single_word_terms():
    extractor = GlossaryExtractor.__new__(GlossaryExtractor)
    extractor.forbidden_words = set()

    assert extractor._is_valid_term("memory")
    assert extractor._is_valid_term("architecture")
    assert extractor._is_valid_term("agent")
