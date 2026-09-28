"""Shared translation quality checks independent of either translation workflow."""

import re
import zlib
from dataclasses import dataclass
from enum import Enum

from bs4 import BeautifulSoup, Tag
from bs4.element import Comment, NavigableString

from engine.core.markup import get_markup_parser


def _looks_like_technical_ascii_noop(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not stripped.isascii():
        return False

    command_starters = {
        "bash",
        "curl",
        "docker",
        "git",
        "kubectl",
        "make",
        "node",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "pytest",
        "python",
        "python3",
        "sh",
        "uv",
        "wget",
        "yarn",
    }
    score = 0
    if re.search(r"https?://\S+", stripped):
        score += 2
    if re.search(r"(?:^|\s)--?[A-Za-z0-9][A-Za-z0-9_-]*\b", stripped):
        score += 1
    if re.search(r"\b[\w./-]+/[\w./-]+\b", stripped):
        score += 1
    if re.search(r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|yaml|yml|toml|ini|cfg|md|txt|html|xml|epub|sh)\b", stripped):
        score += 1
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b", stripped):
        score += 1
    if re.search(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b", stripped):
        score += 1
    if re.search(r"\b[A-Za-z0-9_.-]+::[A-Za-z0-9_.-]+\b", stripped):
        score += 1

    tokens = stripped.split()
    if (
        tokens
        and tokens[0] in command_starters
        and re.fullmatch(r"[A-Za-z0-9_./:=@+-]+(?:\s+[A-Za-z0-9_./:=@+-]+)*", stripped)
    ):
        score += 2
    if re.fullmatch(r"[A-Za-z0-9_.:/+-]+", stripped):
        if stripped in command_starters:
            score += 2
        elif re.search(r"[._:/+-]|\d", stripped):
            score += 1
    return score >= 2


def _looks_like_bibliographic_reference(text: str) -> bool:
    stripped = re.sub(r"\s+", " ", text).strip()
    if len(stripped) < 40:
        return False

    def has_publisher_hint(value: str) -> bool:
        return bool(
            re.search(
                r"\b(?:Books?|Press|Publishing|Publishers?|Publications|University\s+Press)\b",
                value,
                re.IGNORECASE,
            )
        )

    def english_word_count(value: str) -> int:
        return len(re.findall(r"[A-Za-z][A-Za-z'’.-]*", value))

    year_match = re.search(r"(?:\((?:18|19|20)\d{2}[a-z]?\)|\b(?:18|19|20)\d{2}[a-z]?\.)", stripped)
    if year_match and year_match.start() <= 220:
        author_segment = stripped[: year_match.start()]
        if not author_segment:
            return False
        initials = re.findall(r"\b[A-Z]\.", author_segment)
        has_author_joiner = bool(re.search(r"(?:&|\band\b|\bet\s+al\.)", author_segment, re.IGNORECASE))
        has_surname_initial = bool(re.search(r"(?<!\w)[^\W\d_][\w'’.-]*,\s+[A-Z]\.", author_segment))
        if has_surname_initial and (initials or has_author_joiner or author_segment.count(",") >= 2):
            return True

    author_title_match = re.match(
        r"^[A-Z][A-Za-z'’.-]{1,60},\s+[A-Z](?:[A-Za-z'’.-]{1,60}|\.)(?:\s+[A-Z][A-Za-z'’.-]{1,60})?\s*[:：]\s+(.+)$",
        stripped,
    )
    if author_title_match:
        tail = author_title_match.group(1)
        if english_word_count(tail) >= 6 and has_publisher_hint(tail):
            return True

    if any("\u4e00" <= char <= "\u9fff" for char in stripped):
        translated_head = re.match(r"^[^。！？.!?]{2,120}[:：]\s*《", stripped)
        has_original_title = bool(re.search(r"[（(][^（）()]*[A-Za-z][^（）()]*[）)]", stripped))
        if (
            translated_head
            and english_word_count(stripped) >= 6
            and (has_original_title or has_publisher_hint(stripped))
        ):
            return True
    return False


UNTRANSLATED_SKIP_TAGS = {"pre", "code", "math", "script", "style"}
UNTRANSLATED_CODE_CLASS_MARKERS = ("Code", "pre", "mono", "TheSansMono", "NSAnnotations")
UNTRANSLATED_NAV_MARKER_PATTERN = re.compile(r"\[NAVTXT:\d+\]")
UNTRANSLATED_ALLOWED_WORDS = {
    "alb",
    "api",
    "arn",
    "aws",
    "azure",
    "bucket",
    "cargo",
    "cli",
    "cloudformation",
    "codeartifact",
    "codebuild",
    "codedeploy",
    "codepipeline",
    "container",
    "devops",
    "docker",
    "ebs",
    "ec2",
    "ecs",
    "elb",
    "eks",
    "github",
    "gitlab",
    "google",
    "grafana",
    "helm",
    "http",
    "https",
    "iam",
    "json",
    "kibana",
    "kubernetes",
    "linux",
    "mfa",
    "minikube",
    "multi",
    "mysql",
    "netconf",
    "node",
    "npm",
    "postgresql",
    "python",
    "rds",
    "rust",
    "s3",
    "sast",
    "saas",
    "scp",
    "snyk",
    "sonarqube",
    "terraform",
    "typescript",
    "ubuntu",
    "vpc",
    "yaml",
}
UNTRANSLATED_ENGLISH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "when",
    "while",
    "with",
    "you",
    "your",
}
UNTRANSLATED_HEADING_WORDS = {"appendix", "chapter", "index", "part", "preface", "section"}
UNTRANSLATED_SENTENCE_VERBS = {
    "are",
    "be",
    "been",
    "being",
    "can",
    "could",
    "did",
    "do",
    "does",
    "explain",
    "explains",
    "fail",
    "fails",
    "has",
    "have",
    "is",
    "may",
    "might",
    "must",
    "provide",
    "provides",
    "receive",
    "receives",
    "remain",
    "remains",
    "retrieve",
    "retrieves",
    "send",
    "sends",
    "shall",
    "should",
    "was",
    "were",
    "will",
    "would",
}
UNTRANSLATED_ALLOWED_PHRASE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bTCP\s+Fast[-\s]+Open(?:\s+Cookie)?\b",
        r"\bFast[-\s]+Open(?:\s+Cookie)?\b",
        r"\bClaude\s+Code\b",
        r"\bClaude\s+CLI\b",
        r"\bClaude\s+GitHub\s+App\b",
        r"\bPlaywright\s+MCP\b",
        r"\bNext\.js\b",
        r"\bGitHub(?:\s+Actions|\s+App|\s+Workflow)?\b",
        r"\bHookHub\b",
    )
)
UNTRANSLATED_CODEISH_TEXT_PATTERN = re.compile(
    r"`[^`]+`|https?://\S+|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
    r"</?[A-Za-z][A-Za-z0-9_-]*>|\b[\w./-]+/[\w./-]+\b|"
    r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|yaml|yml|toml|ini|cfg|md|txt|html|xml|epub|sh|css|tf)\b",
    re.IGNORECASE,
)
UNTRANSLATED_PARENTHETICAL_NAMED_ENTITY_PATTERN = re.compile(
    r"[（(][^（）()]*[A-Za-z][^（）()]*\b(?:Center|Centre|College|Department|Hospital|Institute|"
    r"Laboratory|Ltd|Press|School|Technology|University|Vidyapeeth)\b[^（）()]*[）)]",
    re.IGNORECASE,
)
UNTRANSLATED_CITATION_PATTERN = re.compile(r"\[[^\[\]]*\b(?:18|19|20)\d{2}\b[^\[\]]*\]")
UNTRANSLATED_ENGLISH_RUN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9'.+-]*(?:\s+[A-Za-z][A-Za-z0-9'.+-]*)*")
QUALITY_MARKER_PATTERN = re.compile(r"\[(?:PRE|CODE|STYLE|TAG|TEXT|NAVTXT):\d+\]")
DEGENERATE_MIN_CHARS = 80
DEGENERATE_MAX_COMPRESSION_RATIO = 0.22
DEGENERATE_MIN_EXPANSION_RATIO = 1.15
_NLTK_TREEBANK_TOKENIZER = None


class EnglishResidualDecision(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    FAIL = "fail"


@dataclass(frozen=True)
class UntranslatedEnglishAnalysis:
    decision: EnglishResidualDecision
    reason: str
    words: tuple[str, ...]
    stopword_count: int
    max_run_word_count: int
    max_run_stopword_count: int
    cjk_count: int
    latin_count: int

    @property
    def is_suspicious(self) -> bool:
        return self.decision == EnglishResidualDecision.FAIL


@dataclass(frozen=True)
class EnglishResidualFinding:
    text: str
    decision: EnglishResidualDecision
    reason: str
    words: tuple[str, ...]


def _ancestor_classes(node: NavigableString) -> str:
    values: list[str] = []
    parent = node.parent
    while isinstance(parent, Tag):
        classes = parent.get("class")
        if isinstance(classes, str):
            values.append(classes)
        elif classes is not None:
            values.extend(str(item) for item in classes)
        parent = parent.parent
    return " ".join(values)


def _should_skip_untranslated_scan(node: NavigableString) -> bool:
    if isinstance(node, Comment):
        return True
    parent = node.parent
    while isinstance(parent, Tag):
        if str(parent.name).lower() in UNTRANSLATED_SKIP_TAGS:
            return True
        parent = parent.parent
    return any(marker in _ancestor_classes(node) for marker in UNTRANSLATED_CODE_CLASS_MARKERS)


def _has_cjk_parent_context(node: NavigableString, text: str) -> bool:
    parent = node.parent
    while isinstance(parent, Tag):
        parent_text = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip()
        if parent_text and parent_text != text and any("\u4e00" <= ch <= "\u9fff" for ch in parent_text):
            return True
        parent = parent.parent
    return False


def _is_allowed_english_term(word: str) -> bool:
    normalized = word.strip("'").lower()
    return (
        not normalized
        or normalized in UNTRANSLATED_ALLOWED_WORDS
        or (len(word) <= 5 and word.isupper())
        or any(ch.isdigit() for ch in word)
        or bool(re.search(r"[a-z][A-Z]", word))
    )


def _get_nltk_treebank_tokenizer():
    global _NLTK_TREEBANK_TOKENIZER
    if _NLTK_TREEBANK_TOKENIZER is not None:
        return _NLTK_TREEBANK_TOKENIZER
    try:
        from nltk.tokenize import TreebankWordTokenizer
    except ImportError:
        return None
    _NLTK_TREEBANK_TOKENIZER = TreebankWordTokenizer()
    return _NLTK_TREEBANK_TOKENIZER


def _strip_low_risk_english_fragments(text: str) -> str:
    text = re.sub(r"\[(?:PRE|CODE|STYLE|TEXT|NAVTXT):\d+\]", " ", text)
    text = UNTRANSLATED_CITATION_PATTERN.sub(" ", text)
    text = UNTRANSLATED_CODEISH_TEXT_PATTERN.sub(" ", text)
    text = UNTRANSLATED_PARENTHETICAL_NAMED_ENTITY_PATTERN.sub(" ", text)
    for phrase_pattern in UNTRANSLATED_ALLOWED_PHRASE_PATTERNS:
        text = phrase_pattern.sub(" ", text)
    return text


def _tokenize_english_words(text: str) -> list[str]:
    tokenizer = _get_nltk_treebank_tokenizer()
    tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", text) if tokenizer is None else tokenizer.tokenize(text)
    words: list[str] = []
    for token in tokens:
        stripped = token.strip("\"'“”‘’()[]{}<>.,;:!?，。！？；：、")
        if re.fullmatch(r"[A-Za-z][A-Za-z'-]*", stripped):
            words.append(stripped)
    return words


def _english_words_for_untranslated_scan(text: str) -> list[str]:
    return [
        word
        for word in _tokenize_english_words(_strip_low_risk_english_fragments(text))
        if not _is_allowed_english_term(word)
    ]


def _english_runs_for_untranslated_scan(text: str) -> list[tuple[list[str], int]]:
    cleaned = _strip_low_risk_english_fragments(text)
    runs: list[tuple[list[str], int]] = []
    for match in UNTRANSLATED_ENGLISH_RUN_PATTERN.finditer(cleaned):
        words = [word for word in _tokenize_english_words(match.group(0)) if not _is_allowed_english_term(word)]
        if words:
            runs.append((words, sum(word.lower().strip("'") in UNTRANSLATED_ENGLISH_STOPWORDS for word in words)))
    return runs


def _analyze_untranslated_english_text(text: str, *, has_cjk_context: bool = False) -> UntranslatedEnglishAnalysis:
    words = _english_words_for_untranslated_scan(text)
    latin_count = sum("a" <= ch.lower() <= "z" for ch in text)
    cjk_count = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
    effective_cjk_count = cjk_count or (1 if has_cjk_context else 0)
    stopword_count = sum(word.lower().strip("'") in UNTRANSLATED_ENGLISH_STOPWORDS for word in words)
    runs = _english_runs_for_untranslated_scan(text)
    max_run_word_count = max((len(run_words) for run_words, _ in runs), default=0)
    max_run_stopword_count = max((run_stopwords for _, run_stopwords in runs), default=0)

    decision = EnglishResidualDecision.ALLOW
    reason = ""
    if words:
        unique_words = {word.lower().strip("'") for word in words}
        has_sentence_verb = any(word.lower().strip("'") in UNTRANSLATED_SENTENCE_VERBS for word in words)
        sentence_like = (
            (max_run_word_count >= 6 and max_run_stopword_count >= 2)
            or (max_run_word_count >= 6 and max_run_stopword_count >= 1 and has_sentence_verb)
            or (len(words) >= 6 and stopword_count >= 3)
        )
        if effective_cjk_count == 0 and any(word.lower().strip("'") in UNTRANSLATED_HEADING_WORDS for word in words):
            decision, reason = EnglishResidualDecision.FAIL, "english_heading"
        elif sentence_like:
            decision, reason = EnglishResidualDecision.FAIL, "mixed_text_english_run"
        elif effective_cjk_count == 0 and latin_count >= 8 and len(words) >= 2:
            decision, reason = EnglishResidualDecision.REVIEW, "english_phrase_review"
        elif effective_cjk_count > 0 and len(words) >= 5 and len(unique_words) >= 4:
            decision, reason = EnglishResidualDecision.REVIEW, "mixed_text_long_english_phrase_review"

    return UntranslatedEnglishAnalysis(
        decision,
        reason,
        tuple(words),
        stopword_count,
        max_run_word_count,
        max_run_stopword_count,
        effective_cjk_count,
        latin_count,
    )


def _extract_nav_payloads(text: str) -> list[str]:
    matches = list(UNTRANSLATED_NAV_MARKER_PATTERN.finditer(text))
    payloads: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if payload := text[match.end() : end].strip():
            payloads.append(payload)
    return payloads


def classify_untranslated_english_texts(
    html: str, *, split_nav_payloads: bool = False
) -> list[EnglishResidualFinding]:
    """Classify visible English residuals as review-only or hard failures."""
    if split_nav_payloads and (payloads := _extract_nav_payloads(html or "")):
        return [finding for payload in payloads for finding in classify_untranslated_english_texts(payload)]

    soup = BeautifulSoup(html or "", get_markup_parser(html or ""))
    findings: list[EnglishResidualFinding] = []
    for node in soup.find_all(string=True):
        if not isinstance(node, NavigableString) or _should_skip_untranslated_scan(node):
            continue
        text = re.sub(r"\s+", " ", str(node)).strip()
        if len(text) < 4 or _looks_like_technical_ascii_noop(text) or _looks_like_bibliographic_reference(text):
            continue
        analysis = _analyze_untranslated_english_text(text, has_cjk_context=_has_cjk_parent_context(node, text))
        if analysis.decision != EnglishResidualDecision.ALLOW:
            findings.append(EnglishResidualFinding(text, analysis.decision, analysis.reason, analysis.words))
    return findings


def find_untranslated_english_texts(html: str, *, split_nav_payloads: bool = False) -> list[str]:
    return [
        finding.text
        for finding in classify_untranslated_english_texts(html, split_nav_payloads=split_nav_payloads)
        if finding.decision == EnglishResidualDecision.FAIL
    ]


def _visible_prose_text(value: str) -> str:
    soup = BeautifulSoup(value, get_markup_parser(value))
    parts = [
        str(node)
        for node in soup.find_all(string=True)
        if isinstance(node, NavigableString) and not _should_skip_untranslated_scan(node)
    ]
    return re.sub(r"\s+", " ", QUALITY_MARKER_PATTERN.sub("", " ".join(parts))).strip()


def _compression_ratio(text: str) -> float:
    data = text.encode("utf-8")
    return len(zlib.compress(data, level=9)) / len(data) if data else 1.0


def _max_character_run(text: str) -> int:
    return max((len(match.group(0)) for match in re.finditer(r"([^\W\d_])\1+", text)), default=1)


def find_degenerate_translation(original: str, translated: str) -> str | None:
    original_text = re.sub(r"\s+", "", _visible_prose_text(original))
    translated_text = re.sub(r"\s+", "", _visible_prose_text(translated))
    if len(translated_text) < DEGENERATE_MIN_CHARS:
        return None
    source_run = _max_character_run(original_text)
    translated_run = _max_character_run(translated_text)
    if translated_run >= 12 and translated_run >= source_run * 3:
        return f"连续字符重复 {translated_run} 次"
    if not original_text or len(translated_text) < len(original_text) * DEGENERATE_MIN_EXPANSION_RATIO:
        return None
    source_ratio = _compression_ratio(original_text)
    translated_ratio = _compression_ratio(translated_text)
    if translated_ratio < DEGENERATE_MAX_COMPRESSION_RATIO and translated_ratio < source_ratio * 0.55:
        return (
            f"异常重复压缩率 {translated_ratio:.3f}（原文 {source_ratio:.3f}），"
            f"长度 {len(translated_text)}/{len(original_text)}"
        )
    return None
