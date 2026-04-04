
from typing import Dict


class TagRestorer:
    """将占位符恢复为原始标签"""

    def restore_tags(
        self,
        translated_text: str,
        tag_map: Dict[str, str],
    ) -> str:
        """将占位符恢复为原始标签"""
        result = translated_text

        # 按索引排序恢复（从大到小，避免替换冲突）
        sorted_items = sorted(
            tag_map.items(),
            key=lambda x: int(x[0][3:-1]),
            reverse=True
        )

        for placeholder, original in sorted_items:
            result = result.replace(placeholder, original)

        return result
