"""Source-grounded titles and linguistic examples for translation script checks."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher

from engine.item.inline import ProjectionError, parse_projection, plain_text
from engine.schemas.contracts import RegistryEntry
from engine.schemas.internal import Event

_TITLE_ELEMENTS = frozenset({"cite", "em", "i"})
_CITATION_CUE = re.compile(
    r"\b(?:paper|article|study|book|report)\s+(?:(?:called|entitled|named|titled)\s+)?$",
    re.IGNORECASE,
)
_CITATION = re.compile(
    r"(?:,\s*(?:(?:18|19|20)\d{2}[a-z]?|n\.d(?:\.-?[a-z])?\.?)|"
    r"\s+\((?:(?:18|19|20)\d{2}[a-z]?|n\.d(?:\.-?[a-z])?\.?)\))$",
    re.IGNORECASE,
)
_BARE_CITATION = re.compile(r"^(?:[A-Z][A-Za-z’'-]*\s+){1,7}(?:18|19|20)\d{2}[a-z]?$")
_SYMBOL = re.compile(r"(?:[A-Za-z]|[A-Z]{2,5})")
_IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*"
_FUNCTION_SIGNATURE = re.compile(rf"{_IDENTIFIER}\((?:{_IDENTIFIER}(?:\s*,\s*{_IDENTIFIER})*)?\)")
_NAME_WORD = r"[A-Z][A-Za-z]*(?:[’'-][A-Za-z]+)*"
_SOURCE_NAME = re.compile(
    rf"(?<![A-Za-z]){_NAME_WORD}(?:\s+(?:(?:and|for|in|of|on|the|to|as\s+an?)\s+)*{_NAME_WORD}){{1,7}}(?![A-Za-z])"
)
_SOURCE_EXAMPLE_LIST = re.compile(r"\(\s*(?:e\.g\.|for example)\s*,?\s*([^()]{1,240})\)", re.IGNORECASE)
_SOURCE_DEFINED_NAME_CUE = re.compile(
    r"\b(?:as|called|entitled|named|titled)\s*[\"'“‘]\s*$",
    re.IGNORECASE,
)
_SOURCE_QUOTED_CUE = re.compile(
    r"\b(?P<cue>called|citation|entitled|example|named|phrase|query|quotation|quote|"
    r"(?:example|sample)\s+(?:phrase|sentence|text)|search(?:es|ed|ing)?(?:\s+(?:for|of))?|"
    r"sentence|term|title|titled|translation|word)\b"
    r"[^.!?。！？\"“”‘’]{0,40}(?:\"(?P<straight>[^\"]{2,200})\"|"
    r"“(?P<curly>[^”]{2,200})”|‘(?P<single>[^’]{2,200})’)",
    re.IGNORECASE,
)
_QUOTED_TEXT = re.compile(r"\"([^\"]{2,200})\"|“([^”]{2,200})”|‘([^’]{2,200})’")
_NUMBERED_REFERENCE_TITLE = re.compile(
    r"(?:^|\n)\s*\[\d+\]\s+[^\n]{2,180}?"
    r"(?:\"(?P<straight>[^\"]{2,200})\"|“(?P<curly>[^”]{2,200})”)"
    r"\s*,?\s*(?:18|19|20)\d{2}[a-z]?\b",
    re.IGNORECASE,
)
_FOCUSED_QUOTED_CUE = re.compile(
    r"^(?:example|phrase|quotation|quote|sentence|term|translation|word|(?:example|sample)\s+)", re.IGNORECASE
)
_TOKENIZATION_CUE = re.compile(r"\btokeni[sz](?:er|ation|e[ds]?|ing)\b", re.IGNORECASE)
_EXAMPLE_SENTENCE_CUE = re.compile(r"^(?:(?:example|sample)\s+)?sentence$", re.IGNORECASE)
_EXPLICIT_EXAMPLE_CUE = re.compile(r"\b(?:sentence|translation)\s*:\s*$", re.IGNORECASE)
_CANDIDATE_TRANSLATION_CUE = re.compile(
    r"\bcandidate translation(?:\s+(?:that\s+)?is(?:\s+just)?)?\s*:?\s*$", re.IGNORECASE
)
_REFERENCE_TRANSLATIONS_CUE = re.compile(
    r"\b(?:its references are|reference (?:translations?|sentences?) (?:is|are))\s*:?\s*$", re.IGNORECASE
)
_REFERENCE_TRANSLATIONS_JOIN = re.compile(r"\s*(?:,\s*(?:and\s+)?|and\s+)\s*", re.IGNORECASE)


def script_paragraphs_without_retained_titles(
    source_projection: str,
    target_projection: str,
    registry: Mapping[str, RegistryEntry],
    *,
    table: bool = False,
) -> tuple[str, ...]:
    """Return target prose after validating and excluding source-grounded literals."""
    source_events = parse_projection(source_projection)
    target_events = parse_projection(target_projection)
    source_ranges = _range_texts(source_events)
    target_ranges = _range_texts(target_events)
    source_text = _unmarked_text(source_events, registry)
    grounded_quotes = _source_grounded_quoted_phrases(
        plain_text(source_events),
        tuple(source_ranges.values()),
        translated_context=any("\u4e00" <= char <= "\u9fff" for char in _unmarked_text(target_events, registry)),
        scoring_context=any("\u4e00" <= char <= "\u9fff" for char in plain_text(target_events)),
    )
    retained: set[str] = set()
    if table:
        for ref, text in source_ranges.items():
            signature = text.strip()
            if registry[ref].hints.get("element") not in {"td", "th"} or not _FUNCTION_SIGNATURE.fullmatch(signature):
                continue
            if target_ranges[ref].strip() != signature:
                raise ProjectionError(f"function signature changed: {signature}")
            retained.add(ref)

    for index, event in enumerate(source_events):
        if event.kind != "marker" or not event.value.startswith("+g"):
            continue
        ref = event.value[1:]
        entry = registry.get(ref)
        previous = source_events[index - 1] if index else None
        text = source_ranges.get(ref)
        target_text = target_ranges.get(ref)
        grounded = entry is not None and bool(text) and text == entry.source_text
        exact = grounded and text == target_text
        title = (
            exact
            and entry is not None
            and entry.hints.get("element", "").lower() in _TITLE_ELEMENTS
            and previous is not None
            and previous.kind == "text"
            and _CITATION_CUE.search(previous.value)
        )
        citation = grounded and _related_citation(text, target_text)
        symbol = exact and text is not None and bool(_SYMBOL.fullmatch(text))
        if title or citation or symbol:
            retained.add(ref)

    paragraphs: list[str] = []
    parts: list[str] = []
    stack: list[str] = []

    def flush() -> None:
        if text := "".join(parts):
            paragraphs.append(_without_source_quoted_phrases(grounded_quotes, text))
        parts.clear()

    for event in target_events:
        if event.kind == "text":
            if not retained.intersection(stack):
                nested = any(
                    ref.startswith("g") and registry[ref].hints.get("source_view_boundary") != "paragraph"
                    for ref in stack
                )
                text = event.value if nested else _without_source_names(source_text, event.value)
                parts.append(text)
        elif event.value.startswith("+"):
            ref = event.value[1:]
            if registry[ref].hints.get("source_view_boundary") == "paragraph":
                flush()
            stack.append(ref)
        elif event.value.startswith("-"):
            ref = event.value[1:]
            if registry[ref].hints.get("source_view_boundary") == "paragraph":
                flush()
            stack.pop()
    flush()
    return tuple(paragraphs)


def _without_source_names(source: str, target: str) -> str:
    if not any("\u4e00" <= char <= "\u9fff" for char in target):
        return target
    return _SOURCE_NAME.sub(lambda match: "" if _has_name_context(source, match.group(0)) else match.group(0), target)


def _source_grounded_quoted_phrases(
    source: str, focused: Sequence[str], *, translated_context: bool, scoring_context: bool
) -> tuple[Counter[str], Counter[str]]:
    scoring = _source_scoring_quotes(source) if scoring_context else {}
    result: Counter[str] = Counter()
    terms = tuple(term for value in focused if (term := _focused_term_key(value)) is not None)
    for match in _SOURCE_QUOTED_CUE.finditer(source):
        value = next(match.group(name) for name in ("straight", "curly", "single") if match.group(name) is not None)
        tokenization = translated_context and _tokenization_example(source, match)
        key = _quoted_phrase_key(value, max_words=40 if tokenization else 20)
        if key is None:
            continue
        cue = match.group("cue")
        quote_start = next(
            match.start(name) - 1 for name in ("straight", "curly", "single") if match.group(name) is not None
        )
        if quote_start in scoring:
            continue
        explicit_example = translated_context and _EXPLICIT_EXAMPLE_CUE.search(
            source[max(0, quote_start - 120) : quote_start]
        )
        if (
            not tokenization
            and not explicit_example
            and _FOCUSED_QUOTED_CUE.match(cue)
            and not any(_contains_focused_term(key, term) for term in terms)
        ):
            continue
        result[key] += 1
    for match in _NUMBERED_REFERENCE_TITLE.finditer(source):
        group = "straight" if match.group("straight") is not None else "curly"
        if match.start(group) - 1 not in scoring and (key := _quoted_phrase_key(match.group(group))):
            result[key] += 1
    return result, Counter(scoring.values())


def _source_scoring_quotes(source: str) -> dict[int, str]:
    """Find source strings used as candidate or reference translations in a worked example."""
    examples: dict[int, str] = {}
    previous_end = 0
    in_references = False
    for match in _QUOTED_TEXT.finditer(source):
        gap = source[previous_end : match.start()]
        in_references = bool(
            _REFERENCE_TRANSLATIONS_CUE.search(gap) or in_references and _REFERENCE_TRANSLATIONS_JOIN.fullmatch(gap)
        )
        if in_references or _CANDIDATE_TRANSLATION_CUE.search(gap):
            value = next(value for value in match.groups() if value is not None)
            if key := _quoted_phrase_key(value, case_sensitive=True):
                examples[match.start()] = key
        previous_end = match.end()
    return examples


def _tokenization_example(source: str, match: re.Match[str]) -> bool:
    if not _EXAMPLE_SENTENCE_CUE.fullmatch(match.group("cue")):
        return False
    # The technical cue must be outside the quote, in its surrounding sentence.
    # A quoted paragraph about tokenizers is still ordinary translatable prose.
    before = re.split(r"[.!?。！？\n]", source[: match.start()])[-1][-120:]
    after = re.split(r"[.!?。！？\n]", source[match.end() :], maxsplit=1)[0][:400]
    return _TOKENIZATION_CUE.search(before + " " + after) is not None


def _without_source_quoted_phrases(grounded: tuple[Counter[str], Counter[str]], target: str) -> str:
    ordinary, scoring = grounded
    if not ordinary and not scoring:
        return target

    def remove(match: re.Match[str]) -> str:
        value = next(value for value in match.groups() if value is not None)
        # Longer keys can only enter the counter through source-proven examples.
        key = _quoted_phrase_key(value, max_words=40, case_sensitive=True)
        if key is not None:
            for counts, spelling in ((scoring, key), (ordinary, key.casefold())):
                if counts[spelling] > 0:
                    counts[spelling] -= 1
                    return ""
        return match.group(0)

    return _QUOTED_TEXT.sub(remove, target)


def _quoted_phrase_key(value: str, *, max_words: int = 20, case_sensitive: bool = False) -> str | None:
    normalized = re.sub(r"\s+", " ", value).strip(" \t,.;:!?，。；：！？、")
    words = re.findall(r"[A-Za-z][A-Za-z’'-]*", normalized)
    if not 2 <= len(words) <= max_words or any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return None
    return normalized if case_sensitive else normalized.casefold()


def _focused_term_key(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", value).strip(" \t,.;:!?，。；：！？、").casefold()
    words = re.findall(r"[a-z][a-z’'-]*", normalized)
    if not 1 <= len(words) <= 6 or any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return None
    return normalized


def _contains_focused_term(phrase: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", phrase, re.IGNORECASE) is not None


def _has_name_context(source: str, name: str) -> bool:
    if any(
        name == item.strip() for examples in _SOURCE_EXAMPLE_LIST.finditer(source) for item in examples[1].split(",")
    ):
        return True
    for match in re.finditer(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", source):
        if re.match(r"\s*\(", source[match.end() :]):
            return True
        if _SOURCE_DEFINED_NAME_CUE.search(source[max(0, match.start() - 120) : match.start()]):
            return True
        sentence_start = max(source.rfind(mark, 0, match.start()) for mark in ".!?") + 1
        if re.search(
            r"\b(?:article\s+)?examples?\s+(?:include|such\s+as)\b",
            source[sentence_start : match.start()],
            re.IGNORECASE,
        ):
            return True
    return False


def _is_citation(text: str | None) -> bool:
    return bool(
        text
        and len(text) <= 200
        and len(text.split()) <= 20
        and (_CITATION.search(text) or _BARE_CITATION.fullmatch(text))
    )


def _related_citation(source: str | None, target: str | None) -> bool:
    if not (_is_citation(source) and _is_citation(target)) or source is None or target is None:
        return False
    dates = re.compile(r"(?:18|19|20)\d{2}[a-z]?|n\.d(?:\.-?[a-z])?", re.IGNORECASE)
    if [value.casefold() for value in dates.findall(source)] != [value.casefold() for value in dates.findall(target)]:
        return False
    words = lambda value: [word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z’'-]*", value)]
    source_words, target_words = words(source), words(target)
    if source_words == target_words:
        return True
    differences = [(left, right) for left, right in zip(source_words, target_words) if left != right]
    return (
        len(source_words) == len(target_words)
        and len(differences) == 1
        and min(map(len, differences[0])) >= 5
        and SequenceMatcher(None, *differences[0]).ratio() >= 0.75
    )


def _unmarked_text(events: Sequence[Event], registry: Mapping[str, RegistryEntry]) -> str:
    parts: list[str] = []
    stack: list[str] = []
    for event in events:
        if event.kind == "text":
            if not any(
                ref.startswith("g") and registry[ref].hints.get("source_view_boundary") != "paragraph" for ref in stack
            ):
                parts.append(event.value)
        elif event.value.startswith("+"):
            stack.append(event.value[1:])
        elif event.value.startswith("-"):
            stack.pop()
    return "".join(parts)


def _range_texts(events: Sequence[Event]) -> dict[str, str]:
    parts: dict[str, list[str]] = {}
    stack: list[str] = []
    for event in events:
        if event.kind == "text":
            for ref in stack:
                parts[ref].append(event.value)
        elif event.value.startswith("+"):
            ref = event.value[1:]
            stack.append(ref)
            parts[ref] = []
        elif event.value.startswith("-"):
            stack.pop()
    return {ref: "".join(values) for ref, values in parts.items()}
