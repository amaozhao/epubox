import re
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Any


BOUNDARIES = ' \t\n\r.,;:!?，。；：？！'


def _replace_pre_placeholders(text: str, preserved: List[str], marker_prefix: str = "__PRE") -> Tuple[str, List[Tuple[str, str]]]:
    """
    替换 [PRE:n]/[CODE:n]/[STYLE:n] 为临时标记

    Returns:
        (替换后的文本, [(临时标记, 原始内容), ...])
    """
    markers = []
    result = text
    for i, pre in enumerate(preserved):
        marker = f"{marker_prefix}{i}__"
        result = result.replace(pre, marker)
        markers.append((marker, pre))
    return result, markers


def _restore_pre_placeholders(text: str, markers: List[Tuple[str, str]]) -> str:
    """恢复 [PRE:n]/[CODE:n]/[STYLE:n]"""
    for marker, original in markers:
        text = text.replace(marker, original)
    return text


async def token_alignment_fallback(
    original: str,
    local_tag_map: Dict[str, str],
    preserved_pre: List[str] = None,
    preserved_code: List[str] = None,
    preserved_style: List[str] = None,
    translate_func=None
) -> Optional[str]:
    """
    Phase 2: Token Alignment Fallback

    1. 移除 [PRE:n]/[CODE:n]/[STYLE:n]，记录位置
    2. 移除 [idN] 得到纯净文本
    3. 翻译纯净文本
    4. 词级对齐重新插入 [idN]
    5. 恢复 [PRE:n]/[CODE:n]/[STYLE:n]
    """
    if preserved_pre is None:
        preserved_pre = []
    if preserved_code is None:
        preserved_code = []
    if preserved_style is None:
        preserved_style = []

    # 合并所有 pre/code/style
    all_preserved = preserved_pre + preserved_code + preserved_style

    # 1. 移除 [PRE:n]/[CODE:n]/[STYLE:n]
    temp = original
    pre_markers = []

    if all_preserved:
        temp, pre_markers = _replace_pre_placeholders(temp, all_preserved)

    # 2. 移除 [idN]
    placeholders = list(local_tag_map.keys())
    clean = temp
    for ph in placeholders:
        clean = clean.replace(ph, "")

    if not clean.strip():
        return None

    # 3. 翻译
    if translate_func is None:
        # 默认翻译函数（需要根据实际情况调整）
        from engine.agents.translator import get_translator
        translator = get_translator()
        response = await translator.arun(clean)
        if hasattr(response, 'content') and hasattr(response.content, 'translation'):
            translated = response.content.translation
        else:
            translated = str(response.content) if response.content else ""
    else:
        translated = await translate_func(clean)

    if not translated:
        return None

    # 4. 对齐重新插入 [idN]
    try:
        aligned = _align(temp, translated, placeholders)
    except Exception:
        return None

    # 5. 恢复 [PRE:n]/[CODE:n]/[STYLE:n]
    if pre_markers:
        aligned = _restore_pre_placeholders(aligned, pre_markers)

    return aligned


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
