from engine.item import Merger
from engine.item import Replacer as ItemReplacer
from engine.schemas import EpubItem


class Replacer:
    def _merge_chunks(self, item: EpubItem) -> str:
        """
        将给定 EpubItem 的所有 Chunk 对象合并为一个字符串。

        Args:
            item: 包含多个 Chunk 对象的 EpubItem。

        Returns:
            合并后的内容字符串。
        """
        merger = Merger()
        if item.chunks:
            merged_content = merger.merge(item.chunks)
            return merged_content
        return ""

    def _restore_replacer(self, item: EpubItem, merged_content: str) -> str:
        """
        使用占位符映射还原给定 EpubItem 的内容。

        Args:
            item: 包含占位符映射的 EpubItem 对象。
            merged_content: 合并后的内容字符串。

        Returns:
            还原后的内容字符串。
        """
        replacer = ItemReplacer()
        if merged_content and item.placeholder:
            restored_content = replacer.restore(merged_content, item.placeholder)
            return restored_content
        return merged_content

    def restore(self, item: EpubItem):
        """
        使用占位符映射还原给定 EpubItem 的内容。

        Args:
            item: 包含占位符映射的 EpubItem 对象.
        """
        merged_content = self._merge_chunks(item)
        final_content = self._restore_replacer(item, merged_content)

        if final_content:
            item.translated = final_content
            with open(item.path, "w", encoding="utf-8") as f:
                f.write(item.translated)
