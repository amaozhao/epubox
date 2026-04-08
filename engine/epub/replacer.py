from engine.core.logger import engine_logger as logger
from engine.item import Merger, PreCodeExtractor
from engine.schemas import EpubItem, TranslationStatus


class Replacer:
    def _merge_chunks(self, item: EpubItem) -> str:
        """将给定 EpubItem 的所有 Chunk 对象合并为一个字符串"""
        merger = Merger()
        if item.chunks:
            merged_content = merger.merge(item.chunks, original_content=item.content)
            return merged_content
        return ""

    def _restore_tags(self, item: EpubItem, merged_content: str) -> str:
        """仅返回合并后的内容。

        Note: With the new architecture, HTML tags are preserved directly in translation.
        This method is kept for interface compatibility.
        """
        return merged_content

    def restore(self, item: EpubItem):
        """恢复 EpubItem 的内容"""
        # 1. 合并 chunks
        merged_content = self._merge_chunks(item)

        # 2. 恢复 pre/code/style 标签占位符为原始标签
        # 注意：[idN] 标签占位符在 orchestrator 中每个 chunk 翻译完成后已恢复
        restored_content = merged_content
        if item.preserved_pre or item.preserved_code or item.preserved_style:
            pre_extractor = PreCodeExtractor()
            pre_extractor.preserved_pre = item.preserved_pre or []
            pre_extractor.preserved_code = item.preserved_code or []
            pre_extractor.preserved_style = item.preserved_style or []
            restored_content = pre_extractor.restore(restored_content)

        # 3. 检查是否有未翻译的 chunk（每个 chunk 已在 merger 中验证过结构）
        untranslated_chunks = [c.name for c in item.chunks if c.status != TranslationStatus.COMPLETED]
        if untranslated_chunks:
            logger.warning(f"以下 chunk 未完成翻译: {untranslated_chunks}, 文件: {item.id}")

        # 7. 保存
        if restored_content:
            item.translated = restored_content
            with open(item.path, "w", encoding="utf-8") as f:
                f.write(item.translated)
