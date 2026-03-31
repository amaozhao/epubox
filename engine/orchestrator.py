import os

from agno.run import RunStatus
from tqdm import tqdm

from engine.agents.workflow import get_translator_workflow
from engine.core.logger import engine_logger as logger
from engine.epub import Builder, Parser, Replacer
from engine.item.placeholder import PlaceholderManager
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
        self.replacer = Replacer()

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

        # 使用 tqdm 显示外部循环进度（按文件）
        for item in tqdm(book.items, desc="翻译 EPUB", unit="文件"):
            if not item.content:
                continue
            if not item.chunks:
                continue

            # 从 item.placeholder 重建 PlaceholderManager
            placeholder_mgr = PlaceholderManager()
            if item.placeholder:
                placeholder_mgr.tag_map = item.placeholder
                placeholder_mgr.counter = len(item.placeholder)

            for _, chunk in enumerate(item.chunks):
                # 在开始工作流前，判断该分块是否需要翻译
                if not self._should_translate_chunk(chunk):
                    stats.record(chunk.status)
                    continue

                workflow = get_translator_workflow()
                max_attempts = 3
                for attempt in range(max_attempts):
                    try:
                        response = await workflow.arun(
                            input=chunk,
                            additional_data={"glossary": glossary, "placeholder_mgr": placeholder_mgr, "tag_map": item.placeholder}
                        )
                        if response.status == RunStatus.completed:
                            if isinstance(response.content, Chunk):
                                chunk_index = item.chunks.index(chunk)
                                item.chunks[chunk_index] = response.content
                                chunk = response.content
                                stats.record(chunk.status)
                            else:
                                logger.error(
                                    f"Invalid response.content type for chunk {chunk.name}: {type(response.content)}"
                                )
                                stats.record_failure()
                            parser.save_json(book)
                            break
                        else:
                            logger.error(
                                f"Translation failed for chunk {chunk.name} (attempt {attempt + 1}/{max_attempts}): status={response.status}"
                            )
                            if attempt == max_attempts - 1:
                                stats.record_failure()
                    except Exception as e:
                        logger.error(
                            f"Unexpected error for chunk {chunk.name} (attempt {attempt + 1}/{max_attempts}): {str(e)}"
                        )
                        if attempt == max_attempts - 1:
                            stats.record_failure()

            # 恢复 item 内容
            self.replacer.restore(item)
            # 保存当前 item 的翻译结果
            parser.save_json(book)

        # 打印最终统计
        logger.info(str(stats))

        # 构建翻译后的 EPUB 文件
        output_path = os.path.join(os.path.dirname(book.path), f"{book.name}-cn.epub")
        builder = Builder(book.extract_path, output_path)
        builder.build()
