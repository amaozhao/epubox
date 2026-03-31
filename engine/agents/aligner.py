import re
from collections import defaultdict
from typing import Dict, List, Tuple


BOUNDARIES = ' \t\n\r.,;:!?，。；：？！'


def _find_positions(text: str, placeholders: List[str]) -> List[Tuple[int, int, str]]:
    positions = []
    remaining, offset = text, 0
    for ph in placeholders:
        idx = remaining.find(ph)
        if idx != -1:
            positions.append((offset + idx, offset + idx + len(ph), ph))
            remaining = remaining[idx + len(ph):]
            offset = offset + idx + len(ph)
    return positions


def _remove_placeholders(text: str, placeholders: List[str]) -> str:
    for ph in placeholders:
        text = text.replace(ph, "")
    return text


def _adjust_boundary(text: str, pos: int) -> int:
    if pos <= 0:
        return 0
    if pos >= len(text):
        return len(text)
    if text[pos] in BOUNDARIES:
        return pos
    left = pos
    while left > 0 and text[left] not in BOUNDARIES:
        left -= 1
    right = pos
    while right < len(text) and text[right] not in BOUNDARIES:
        right += 1
    return left if pos - left <= right - pos else right


def _insert(text: str, insertions: List[Tuple[int, str]]) -> str:
    groups = defaultdict(list)
    for pos, ph in insertions:
        groups[pos].append(ph)
    for pos in groups:
        groups[pos].sort(key=lambda ph: int(re.search(r'\d+', ph).group()))
    result = text
    for pos in sorted(groups.keys(), reverse=True):
        result = result[:pos] + ''.join(groups[pos]) + result[pos:]
    return result


def _align(original: str, translated: str, placeholders: List[str]) -> str:
    """词级对齐重新插入占位符"""
    positions = _find_positions(original, placeholders)
    clean = _remove_placeholders(original, placeholders)

    # 相对位置 → 译文绝对位置
    rel = [(s / len(clean), ph) for s, _, ph in positions]
    mapped = [(int(r * len(translated)), ph) for r, ph in rel]

    # 校正到词边界，逆序插入
    adjusted = [(_adjust_boundary(translated, p), ph) for p, ph in mapped]
    return _insert(translated, adjusted)
