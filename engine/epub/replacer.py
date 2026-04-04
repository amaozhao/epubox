import xml.etree.ElementTree as ET

from engine.agents.verifier import verify_html_integrity
from engine.core.logger import engine_logger as logger
from engine.item import Merger, PreCodeExtractor
from engine.schemas import EpubItem, TranslationStatus


class Replacer:
    def _validate_nav_structure(self, content: str) -> bool:
        """验证 nav 文件结构完整性（使用 XML 解析器）"""
        try:
            root = ET.fromstring(content)
            ncx_ns = "{http://www.daisy.org/z3986/2005/ncx/}"
            xhtml_ns = "{http://www.w3.org/1999/xhtml}"

            # NCX 格式：根元素是 ncx，包含 navMap 且 navMap 下有 navPoint
            if root.tag.endswith("ncx") or root.tag == f"{ncx_ns}ncx":
                navmap = root.find(f".//{ncx_ns}navMap")
                if navmap is None:
                    navmap = root.find(".//navMap")
                if navmap is not None:
                    navpoint = navmap.find(f"./{ncx_ns}navPoint")
                    if navpoint is None:
                        navpoint = navmap.find("./navPoint")
                    return navpoint is not None
                return False

            # XHTML 格式：文档包含 nav 元素，nav 下有 ol 且 ol 下有 li
            # nav 可能是根元素（standalone nav.xhtml）或 html>body 的后代
            if root.tag.endswith("nav") or root.tag == f"{xhtml_ns}nav":
                # nav 是根元素
                ol_elem = root.find(f"./{xhtml_ns}ol")
                if ol_elem is None:
                    ol_elem = root.find("./ol")
                if ol_elem is not None:
                    li_elem = ol_elem.find(f"./{xhtml_ns}li")
                    if li_elem is None:
                        li_elem = ol_elem.find("./li")
                    return li_elem is not None
                return False
            else:
                # nav 是后代（html>body>nav 结构）
                nav_elem = root.find(f".//{xhtml_ns}nav")
                if nav_elem is None:
                    nav_elem = root.find(".//nav")
                if nav_elem is not None:
                    ol_elem = nav_elem.find(f"./{xhtml_ns}ol")
                    if ol_elem is None:
                        ol_elem = nav_elem.find("./ol")
                    if ol_elem is not None:
                        li_elem = ol_elem.find(f"./{xhtml_ns}li")
                        if li_elem is None:
                            li_elem = ol_elem.find("./li")
                        return li_elem is not None
                return False

            return False
        except ET.ParseError:
            return False

    def _merge_chunks(self, item: EpubItem) -> str:
        """将给定 EpubItem 的所有 Chunk 对象合并为一个字符串"""
        merger = Merger()
        if item.chunks:
            merged_content = merger.merge(item.chunks)
            return merged_content
        return ""

    def _restore_tags(self, item: EpubItem, merged_content: str) -> str:
        """仅返回合并后的内容，不做标签恢复

        注意：[idN] 标签占位符在 orchestrator 中每个 chunk 翻译完成后已恢复。
        pre/code/style 标签在 restore() 方法中通过 PreCodeExtractor.restore 恢复。
        此方法仅用于接口兼容性。
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

        # 3. 验证 HTML 结构完整性
        is_valid, integrity_errors = verify_html_integrity(restored_content)
        if not is_valid:
            logger.error(f"HTML结构验证失败: {item.id}, 错误: {integrity_errors}")

        # 4.1 验证 nav 文件结构完整性（在占位符恢复之后）
        is_nav_file = "toc.ncx" in item.id.lower() or item.id.endswith("nav.xhtml")
        if is_nav_file and not self._validate_nav_structure(restored_content):
            logger.error(f"Nav 结构验证失败: {item.id}，但保留翻译结果")

        # 5. 检查是否有未翻译的 chunk
        untranslated_chunks = [c.name for c in item.chunks if c.status != TranslationStatus.COMPLETED]
        if untranslated_chunks:
            logger.error(f"以下 chunk 未完成翻译: {untranslated_chunks}, 文件: {item.id}")

        # 7. 保存
        if restored_content:
            item.translated = restored_content
            with open(item.path, "w", encoding="utf-8") as f:
                f.write(item.translated)
