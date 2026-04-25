import re
from typing import Dict, List, Tuple


def validate_placeholders(text: str, tag_map: Dict[str, str]) -> Tuple[bool, str]:
    """严格验证占位符：数量、顺序、完整性。返回 (是否有效, 错误信息)"""
    if not tag_map:
        return True, ""

    first = next(iter(tag_map.keys()))
    prefix, suffix = first[:3], first[-1]

    # 1. 检查数量和具体差异
    pattern = re.escape(prefix) + r"(\d+)" + re.escape(suffix)
    found = re.findall(pattern, text)
    found_indices = sorted([int(x) for x in found])
    expected_indices = sorted([int(x) for k in tag_map.keys() for x in re.findall(r"\d+", k)])

    missing = set(expected_indices) - set(found_indices)
    extra = set(found_indices) - set(expected_indices)

    if missing or extra:
        error_parts = []
        if missing:
            error_parts.append(f"缺少:{[f'[id{i}]' for i in sorted(missing)]}")
        if extra:
            error_parts.append(f"多余:{[f'[id{i}]' for i in sorted(extra)]}")
        return False, ", ".join(error_parts)

    # 2. 检查顺序 - 不排序，直接比较实际出现顺序
    original_order = [int(x) for x in re.findall(pattern, text)]
    if original_order != expected_indices:
        return (
            False,
            f"顺序错误: 期望{[f'[id{i}]' for i in expected_indices]}, 实际{[f'[id{i}]' for i in original_order]}",
        )

    # 3. 检查完整性
    for ph in tag_map:
        if ph not in text:
            return False, f"缺失: {ph}"

    return True, ""


def validate_placeholder_positions(
    original: str, translated: str, local_tag_map: Dict[str, str]
) -> Tuple[bool, str, List[str]]:
    """
    验证占位符位置

    Returns:
        (is_valid, corrected_text, errors)
    """

    original_indices = extract_placeholder_indices(original)
    translated_indices = extract_placeholder_indices(translated)

    errors = []

    # 1. 数量验证
    if len(original_indices) != len(translated_indices):
        errors.append(f"占位符数量不匹配: 原文{len(original_indices)}, 译文{len(translated_indices)}")
        return False, translated, errors

    # 2. 顺序验证
    for i, (orig_idx, trans_idx) in enumerate(zip(original_indices, translated_indices)):
        if orig_idx != trans_idx:
            errors.append(f"位置{i}占位符索引不匹配: 原文[{orig_idx}], 译文[{trans_idx}]")

    if errors:
        return False, translated, errors

    # 3. 验证通过
    return True, translated, []


def extract_placeholder_indices(text: str) -> List[int]:
    """提取占位符的索引值"""
    matches = re.findall(r"\[id(\d+)\]", text)
    return [int(m) for m in matches]
