import re

from engine.agents.verifier import verify_html_integrity
from engine.core.logger import engine_logger as logger
from engine.item import Merger, PreCodeExtractor
from engine.item.placeholder import PlaceholderManager
from engine.item.tag import TagRestorer
from engine.schemas import EpubItem


class Replacer:
    def _validate_nav_structure(self, content: str) -> bool:
        """验证 nav 文件结构完整性"""
        # NCX 格式（toc.ncx）使用这些标签
        ncx_required = ["<navMap>", "<navPoint>", "<navLabel>", "<content"]
        if all(tag in content for tag in ncx_required):
            return True
        # XHTML 格式（nav.xhtml）使用这些标签
        xhtml_required = ["<nav", "</nav>", "<ol", "</ol>", "<li>", "</li>"]
        if all(tag in content.lower() for tag in xhtml_required):
            return True
        return False

    def _merge_chunks(self, item: EpubItem) -> str:
        """将给定 EpubItem 的所有 Chunk 对象合并为一个字符串"""
        merger = Merger()
        if item.chunks:
            merged_content = merger.merge(item.chunks)
            return merged_content
        return ""

    def _restore_tags(self, item: EpubItem, merged_content: str) -> str:
        """使用占位符映射还原给定 EpubItem 的内容"""
        if not merged_content or not item.placeholder:
            return merged_content

        # 重建 PlaceholderManager
        placeholder_mgr = PlaceholderManager()
        placeholder_mgr.tag_map = item.placeholder

        # 使用 TagRestorer 恢复标签
        restorer = TagRestorer()
        restored_content = restorer.restore_tags(merged_content, placeholder_mgr)
        return restored_content

    def restore(self, item: EpubItem):
        """恢复 EpubItem 的内容"""
        # 1. 合并 chunks
        merged_content = self._merge_chunks(item)

        # 2. 恢复占位符为原始标签
        restored_content = self._restore_tags(item, merged_content)

        # 3. 恢复 pre/code 标签（二级占位符方案）
        if item.preserved_pre or item.preserved_code:
            pre_extractor = PreCodeExtractor()
            pre_extractor.preserved_pre = item.preserved_pre or []
            pre_extractor.preserved_code = item.preserved_code or []
            restored_content = pre_extractor.restore(restored_content)

        # 4. 验证 HTML 结构完整性
        is_valid, integrity_errors = verify_html_integrity(restored_content)
        if not is_valid:
            logger.error(f"HTML结构验证失败: {item.id}, 错误: {integrity_errors}")

        # 4.1 验证 nav 文件结构完整性（在占位符恢复之后）
        is_nav_file = "toc.ncx" in item.id.lower() or item.id.endswith("nav.xhtml")
        if is_nav_file and not self._validate_nav_structure(restored_content):
            logger.error(f"Nav 结构验证失败: {item.id}，但保留翻译结果")

        # 5. 检查是否有未恢复的占位符
        remaining = re.findall(r'\[id\d+\]', restored_content)
        if remaining:
            logger.error(f"还有未恢复的占位符: {remaining}")

        # 6. 检查是否有未恢复的 pre/code 占位符
        remaining_pre = re.findall(r'\[PRE:\d+\]', restored_content)
        remaining_code = re.findall(r'\[CODE:\d+\]', restored_content)
        if remaining_pre:
            logger.error(f"还有未恢复的PRE占位符: {remaining_pre}")
        if remaining_code:
            logger.error(f"还有未恢复的CODE占位符: {remaining_code}")

        # 7. 保存
        if restored_content:
            item.translated = restored_content
            with open(item.path, "w", encoding="utf-8") as f:
                f.write(item.translated)
