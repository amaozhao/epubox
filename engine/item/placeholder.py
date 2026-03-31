from typing import Dict, List

from engine.core.logger import engine_logger as logger


class PlaceholderManager:
    """
    管理占位符的创建、索引转换和恢复

    三层索引架构：
    1. 文档级全局索引：[id0], [id1], [id2]... (在整个文档中唯一)
    2. Chunk级局部索引：[id0], [id1]... (在每个chunk中从0开始)
    3. 全局索引恢复：将局部索引转换回全局索引
    """

    def __init__(self):
        self.tag_map: Dict[str, str] = {}  # 全局占位符→原始标签
        self.counter: int = 0  # 全局计数器

    def create_placeholder(self, tag_content: str) -> str:
        """创建一个新的占位符"""
        placeholder = f"[id{self.counter}]"
        self.tag_map[placeholder] = tag_content
        self.counter += 1
        return placeholder

    def get_local_tag_map(self, global_indices: List[int]) -> Dict[str, str]:
        """
        获取指定全局索引对应的局部tag_map

        用于chunk翻译时，告诉LLM这个chunk使用了哪些占位符
        """
        local_map = {}
        for local_idx, global_idx in enumerate(global_indices):
            placeholder = f"[id{global_idx}]"
            if placeholder in self.tag_map:
                local_map[f"[id{local_idx}]"] = self.tag_map[placeholder]
        return local_map

    def restore_to_global(
        self,
        translated_with_local: str,
        global_indices: List[int],
    ) -> str:
        """
        将翻译后的文本（使用局部索引）转换回全局索引

        支持多种占位符格式检测：
        - [id0], [id1] (标准格式)
        - [[0]], [[1]] (双括号格式)
        - {{0}}, {{1}} (双花括号格式)
        """
        result = translated_with_local

        # 1. 检测占位符格式
        if "[id" in result:
            prefix, suffix = "[id", "]"
        elif "[[" in result:
            prefix, suffix = "[[", "]]"
        elif "{{" in result:
            prefix, suffix = "{{", "}}"
        else:
            # 未知格式，使用默认值
            prefix, suffix = "[id", "]"
            logger.warning(f"未知占位符格式，尝试使用默认格式: {prefix}")

        # 2. 用临时标记替换局部占位符，避免冲突
        for local_idx in range(len(global_indices)):
            local_ph = f"{prefix}{local_idx}{suffix}"
            if local_ph in result:
                result = result.replace(local_ph, f"__RESTORE_{local_idx}__")

        # 3. 用全局索引替换临时标记
        for local_idx, global_idx in enumerate(global_indices):
            result = result.replace(f"__RESTORE_{local_idx}__", f"[id{global_idx}]")

        return result

    def remove_all_placeholders(self, text: str) -> str:
        """移除所有占位符，返回纯净文本"""
        result = text
        for placeholder in self.tag_map:
            result = result.replace(placeholder, "")
        return result
