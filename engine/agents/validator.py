import re
from typing import Dict, List, Tuple


class ValidationError(Exception):
    """占位符验证错误"""

    def __init__(self, message: str, missing_ids: List[int] | None = None, extra_ids: List[int] | None = None):
        super().__init__(message)
        self.message = message
        self.missing_ids = missing_ids or []
        self.extra_ids = extra_ids or []


def validate_placeholders(text: str, tag_map: Dict[str, str]) -> Tuple[bool, str]:
    """严格验证占位符：顺序和存在性。返回 (是否有效, 错误信息)"""
    if not tag_map:
        return True, ""

    # 从 text 中按顺序提取所有占位符
    found_placeholders = re.findall(r"\[id\d+\]", text)

    # 从 tag_map.keys() 按顺序获取所有占位符
    expected_placeholders = list(tag_map.keys())

    # 先检查长度
    if len(found_placeholders) != len(expected_placeholders):
        # 计算缺少和多出的占位符
        found_set = set(found_placeholders)
        expected_set = set(expected_placeholders)
        missing = sorted(expected_set - found_set, key=lambda x: int(x[3:-1]))
        extra = sorted(found_set - expected_set, key=lambda x: int(x[3:-1]))
        parts = []
        if missing:
            parts.append(f"缺少 {missing}")
        if extra:
            parts.append(f"多余 {extra}")
        return False, ", ".join(parts)

    # 逐个比较
    for i, (found, expected) in enumerate(zip(found_placeholders, expected_placeholders)):
        if found != expected:
            return False, f"占位符不匹配: 位置 {i} 期望 [{expected}], 实际 [{found}]"

    return True, ""
