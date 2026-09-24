from types import SimpleNamespace
from unittest.mock import MagicMock

from engine.services import glossary
from engine.services.glossary import GlossaryExtractor


def test_glossary_extractor_accepts_single_word_terms():
    extractor = GlossaryExtractor.__new__(GlossaryExtractor)
    extractor.forbidden_words = set()

    assert extractor._is_valid_term("memory")
    assert extractor._is_valid_term("architecture")
    assert extractor._is_valid_term("agent")


def test_missing_nltk_data_never_downloads_or_exits(monkeypatch):
    def missing(_resource):
        raise LookupError("missing")

    download = MagicMock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(glossary.nltk.data, "find", missing)
    monkeypatch.setattr(glossary.nltk, "download", download)

    extractor = GlossaryExtractor()

    assert extractor.available is False
    assert extractor.extract_from_epub("missing.epub") == {}
    download.assert_not_called()


def test_nltk_directory_resources_use_zip_compatible_paths(monkeypatch):
    resources = []

    def found(resource):
        resources.append(resource)
        return object()

    monkeypatch.setattr(glossary.nltk.data, "find", found)
    monkeypatch.setattr(glossary.nltk.corpus, "stopwords", SimpleNamespace(words=MagicMock(return_value=[])))

    extractor = GlossaryExtractor()

    assert extractor.available is True
    assert resources == [
        "tokenizers/punkt_tab/english/",
        "corpora/stopwords/",
        "taggers/averaged_perceptron_tagger_eng/",
    ]
