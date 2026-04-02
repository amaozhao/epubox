from typing import List

from engine.item.placeholder import PlaceholderManager


class TagRestorer:
    """将占位符恢复为原始标签"""

    def restore_tags(
        self,
        translated_text: str,
        placeholder_mgr: PlaceholderManager,
    ) -> str:
        """将占位符恢复为原始标签"""
        result = translated_text

        # 按索引排序恢复（从大到小，避免替换冲突）
        sorted_items = sorted(
            placeholder_mgr.tag_map.items(),
            key=lambda x: int(x[0][3:-1]),
            reverse=True
        )

        for placeholder, original in sorted_items:
            result = result.replace(placeholder, original)

        return result
