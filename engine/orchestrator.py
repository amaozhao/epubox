import json
import os
from datetime import datetime

from tqdm import tqdm

from engine.agents.workflow import get_translator_workflow
from engine.core.logger import engine_logger as logger
from engine.epub import Builder, Parser, DomReplacer
from engine.schemas import Chunk, TranslationStatus
from engine.services.glossary import GlossaryExtractor, GlossaryLoader


# 翻译结果统计
class TranslationStats:
    def __init__(self):
        self.total = 0
        self.translated = 0
        self.untranslated = 0
        self.pending = 0
        self.failed = 0

    def record(self, status: TranslationStatus):
        self.total += 1
        if status == TranslationStatus.TRANSLATED:
            self.translated += 1
        elif status == TranslationStatus.UNTRANSLATED:
            self.untranslated += 1
        elif status == TranslationStatus.PENDING:
            self.pending += 1

    def record_failure(self):
        self.failed += 1

    def __str__(self):
        return (
            f"翻译统计: 总数={self.total}, "
            f"成功={self.translated}, "
            f"失败={self.untranslated}, "
            f"跳过={self.pending}, "
            f"错误={self.failed}"
        )


class Orchestrator:
    """
    一个协调不同工作流和代理的编排器。
    """

    def __init__(self, *args, **kwargs):
        """
        初始化编排器实例。
        """
        super().__init__(*args, **kwargs)
        self.replacer = DomReplacer()

    def _save_manual_translation_report(self, manual_chunks: list, output_path: str):
        """保存手动翻译报告到 JSON 文件"""
        report = {
            "generated_at": datetime.now().isoformat(),
            "total": len(manual_chunks),
            "chunks": [
                {
                    "file": chunk["file"],
                    "chunk_name": chunk["chunk_name"],
                    "original": chunk["original"],
                    "path": chunk["path"],
                    "placeholder": chunk.get("placeholder", {}),
                    "status": chunk["status"],
                }
                for chunk in manual_chunks
            ],
        }

        report_path = os.path.join(os.path.dirname(output_path), "manual_translation_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"手动翻译报告已保存: {report_path}")
        return report_path

    def _load_manual_translations(self, report_path: str) -> dict:
        """加载手动翻译报告，返回 {chunk_name: translated_text}"""
        if not os.path.exists(report_path):
            return {}

        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)

        return {
            item["chunk_name"]: item.get("translated", "")
            for item in report.get("chunks", [])
            if item.get("translated")
        }

    def _should_process_chunk(self, chunk) -> bool:
        """判断 chunk 是否需要处理"""
        if chunk.status == TranslationStatus.COMPLETED:
            return False  # 已完成，跳过

        if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
            return True  # 已翻译但未校对，需要继续校对流程

        if chunk.status == TranslationStatus.UNTRANSLATED and chunk.translated and chunk.translated != chunk.original:
            # 手动翻译后的 chunk：用户编辑了 translated 字段
            chunk.status = TranslationStatus.TRANSLATED
            return True  # 进入校对流程

        if chunk.status == TranslationStatus.UNTRANSLATED:
            # 翻译失败且未手动编辑 → 跳过，避免重复失败
            return False

        if chunk.status == TranslationStatus.PENDING:
            return True  # 待翻译

        return False  # 未知状态，安全跳过

    def _should_translate_chunk(self, chunk: Chunk) -> bool:
        """
        判断一个分块是否需要翻译。

        判断逻辑：
        1. 如果 chunk.status 属性为 COMPLETED，则认为它已经翻译过，返回 False。
        2. 否则，返回 True，表示需要进行翻译。

        Args:
            chunk: 待判断的 Chunk 对象。

        Returns:
            如果需要翻译则返回 True，否则返回 False。
        """
        if chunk.status == TranslationStatus.COMPLETED:
            return False
        return True

    async def translate_epub(self, epub_path: str, limit: int = 3000, target_language: str = "Chinese") -> None:
        """
        翻译给定路径的 EPUB 文件。

        Args:
            epub_path: 输入 EPUB 文件的路径。
            target_language: 目标翻译语言代码（例如 'zh'、'en'）。

        Returns:
            None
        """
        # 解析 EPUB 文件
        parser = Parser(limit=limit, path=epub_path)
        book = parser.parse()

        # 加载或自动生成术语表
        loader = GlossaryLoader()
        glossary = loader.load(epub_path)
        if not glossary:
            logger.info("术语表为空，自动生成中...")
            extractor = GlossaryExtractor()
            glossary = extractor.extract_from_epub(epub_path)
            logger.info(f"术语表生成完成，共提取 {len(glossary)} 个术语")

        # 统计翻译结果
        stats = TranslationStats()
        manual_chunks = []

        # 使用 tqdm 显示外部循环进度（按文件）
        for item in tqdm(book.items, desc="翻译 EPUB", unit="文件"):
            if not item.content:
                continue
            if not item.chunks:
                continue

            for _, chunk in enumerate(item.chunks):
                # 在开始工作流前，判断该分块是否需要处理
                if not self._should_process_chunk(chunk):
                    stats.record(chunk.status)
                    continue

                workflow = get_translator_workflow()
                try:
                    response = await workflow.arun(
                        input=chunk, additional_data={"glossary": glossary, "tag_map": item.placeholder}
                    )
                    if isinstance(response.content, Chunk):
                        chunk_index = item.chunks.index(chunk)
                        item.chunks[chunk_index] = response.content
                        chunk = response.content
                        stats.record(chunk.status)

                        # 每翻译一个 chunk 立即保存，支持断点续传
                        parser.save_json(book)

                        # 记录需要手动翻译的 chunk
                        if chunk.status == TranslationStatus.UNTRANSLATED:
                            manual_chunks.append(
                                {
                                    "file": item.id,
                                    "chunk_name": chunk.name,
                                    "original": chunk.original,
                                    "path": item.path,
                                    "placeholder": item.placeholder,
                                    "status": chunk.status.value,
                                }
                            )
                    else:
                        logger.error(f"Invalid response.content type for chunk {chunk.name}: {type(response.content)}")
                        stats.record_failure()
                except Exception as e:
                    logger.error(f"Unexpected error for chunk {chunk.name}: {str(e)}")
                    stats.record_failure()

            # 恢复 item 内容
            dom_replacer = DomReplacer()
            dom_replacer.restore(item)
            # 将翻译后的内容写回 temp 目录（Builder 打包的是 temp 目录）
            if item.translated:
                with open(item.path, "w", encoding="utf-8") as f:
                    f.write(item.translated)
            # 保存当前 item 的翻译结果
            parser.save_json(book)

        # 打印最终统计
        logger.info(str(stats))

        # 生成手动翻译报告
        if manual_chunks:
            self._save_manual_translation_report(manual_chunks, book.path)

        # 构建翻译后的 EPUB 文件
        output_path = os.path.join(os.path.dirname(book.path), f"{book.name}-cn.epub")
        builder = Builder(book.extract_path, output_path)
        builder.build()
