"""Source-grounded citation-title handling for translation script checks."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher

from engine.item.inline import parse_projection
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
_NAME_WORD = r"[A-Z][A-Za-z]*(?:[’'-][A-Za-z]+)*"
_SOURCE_NAME = re.compile(
    rf"(?<![A-Za-z]){_NAME_WORD}(?:\s+(?:(?:and|for|in|of|on|the|to)\s+)*{_NAME_WORD}){{1,7}}(?![A-Za-z])"
)
_SOURCE_QUOTED_CUE = re.compile(
    r"\b(?P<cue>called|citation|entitled|example|named|phrase|query|quotation|quote|"
    r"(?:example|sample)\s+(?:phrase|sentence|text)|search(?:es|ed|ing)?(?:\s+(?:for|of))?|"
    r"sentence|term|title|titled|word)\b"
    r"[^.!?。！？\"“”‘’]{0,40}(?:\"(?P<straight>[^\"]{2,200})\"|"
    r"“(?P<curly>[^”]{2,200})”|‘(?P<single>[^’]{2,200})’)",
    re.IGNORECASE,
)
_TARGET_QUOTED_TEXT = re.compile(r"\"([^\"]{2,200})\"|“([^”]{2,200})”|‘([^’]{2,200})’")
_FOCUSED_QUOTED_CUE = re.compile(
    r"^(?:example|phrase|quotation|quote|sentence|term|word|(?:example|sample)\s+)", re.IGNORECASE
)
_TOKENIZATION_CUE = re.compile(r"\btokeni[sz](?:er|ation|e[ds]?|ing)\b", re.IGNORECASE)
_EXAMPLE_SENTENCE_CUE = re.compile(r"^(?:(?:example|sample)\s+)?sentence$", re.IGNORECASE)


def script_paragraphs_without_retained_titles(
    source_projection: str,
    target_projection: str,
    registry: Mapping[str, RegistryEntry],
) -> tuple[str, ...]:
    """Return target paragraphs without bounded English text retained from the source."""
    source_events = parse_projection(source_projection)
    target_events = parse_projection(target_projection)
    source_ranges = _range_texts(source_events)
    target_ranges = _range_texts(target_events)
    source_text = _unmarked_text(source_events, registry)
    grounded_quotes = _source_grounded_quoted_phrases(
        source_text,
        tuple(source_ranges.values()),
        tokenization_examples=any("\u4e00" <= char <= "\u9fff" for char in _unmarked_text(target_events, registry)),
    )
    retained: set[str] = set()

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
            paragraphs.append(text)
        parts.clear()

    for event in target_events:
        if event.kind == "text":
            if not retained.intersection(stack):
                nested = any(
                    ref.startswith("g") and registry[ref].hints.get("source_view_boundary") != "paragraph"
                    for ref in stack
                )
                text = event.value if nested else _without_source_names(source_text, event.value)
                parts.append(_without_source_quoted_phrases(grounded_quotes, text))
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
    source: str, focused: Sequence[str], *, tokenization_examples: bool = False
) -> Counter[str]:
    result: Counter[str] = Counter()
    terms = tuple(term for value in focused if (term := _focused_term_key(value)) is not None)
    for match in _SOURCE_QUOTED_CUE.finditer(source):
        value = next(match.group(name) for name in ("straight", "curly", "single") if match.group(name) is not None)
        tokenization = tokenization_examples and _tokenization_example(source, match)
        key = _quoted_phrase_key(value, max_words=40 if tokenization else 20)
        if key is None:
            continue
        cue = match.group("cue")
        if (
            not tokenization
            and _FOCUSED_QUOTED_CUE.match(cue)
            and not any(_contains_focused_term(key, term) for term in terms)
        ):
            continue
        result[key] += 1
    return result


def _tokenization_example(source: str, match: re.Match[str]) -> bool:
    if not _EXAMPLE_SENTENCE_CUE.fullmatch(match.group("cue")):
        return False
    # The technical cue must be outside the quote, in its surrounding sentence.
    # A quoted paragraph about tokenizers is still ordinary translatable prose.
    before = re.split(r"[.!?。！？\n]", source[: match.start()])[-1][-120:]
    after = re.split(r"[.!?。！？\n]", source[match.end() :], maxsplit=1)[0][:400]
    return _TOKENIZATION_CUE.search(before + " " + after) is not None


def _without_source_quoted_phrases(grounded: Counter[str], target: str) -> str:
    if not grounded:
        return target

    def remove(match: re.Match[str]) -> str:
        value = next(value for value in match.groups() if value is not None)
        # Longer keys can only enter the counter through source-proven examples.
        key = _quoted_phrase_key(value, max_words=40)
        if key is None or grounded[key] <= 0:
            return match.group(0)
        grounded[key] -= 1
        return ""

    return _TARGET_QUOTED_TEXT.sub(remove, target)


def _quoted_phrase_key(value: str, *, max_words: int = 20) -> str | None:
    normalized = re.sub(r"\s+", " ", value).strip(" \t,.;:!?，。；：！？、")
    words = re.findall(r"[A-Za-z][A-Za-z’'-]*", normalized)
    if not 2 <= len(words) <= max_words or any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return None
    return normalized.casefold()


def _focused_term_key(value: str) -> str | None:
    normalized = re.sub(r"\s+", " ", value).strip(" \t,.;:!?，。；：！？、").casefold()
    words = re.findall(r"[a-z][a-z’'-]*", normalized)
    if not 1 <= len(words) <= 6 or any("\u4e00" <= char <= "\u9fff" for char in normalized):
        return None
    return normalized


def _contains_focused_term(phrase: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", phrase, re.IGNORECASE) is not None


def _has_name_context(source: str, name: str) -> bool:
    for match in re.finditer(re.escape(name), source):
        if re.match(r"\s*\(", source[match.end() :]):
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
