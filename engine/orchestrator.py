import os
import re

from tqdm import tqdm

from engine.agents.workflow import TranslatorWorkflow
from engine.epub import Builder, Parser, Replacer
from engine.schemas import TranslationStatus


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

    def _should_translate_chunk(self, chunk) -> bool:
        """
        判断一个分块是否需要翻译。

        判断逻辑：
        1. 如果 chunk.status 属性为 COMPLETED，则认为它已经翻译过，返回 False。
        2. 如果 chunk.translated 属性不为空，并且包含任何中文字符，也认为它已经翻译过，返回 False。
        3. 否则，返回 True，表示需要进行翻译。

        Args:
            chunk: 待判断的 Chunk 对象。

        Returns:
            如果需要翻译则返回 True，否则返回 False。
        """
        # 首先检查状态，这是最可靠的判断
        if chunk.status == TranslationStatus.COMPLETED:
            return False

        # 中文字符的 unicode 范围
        chinese_pattern = re.compile(r"[\u4e00-\u9fff]")

        # 检查 translated 属性是否为非空字符串，并包含中文字符
        if chunk.translated and isinstance(chunk.translated, str) and chinese_pattern.search(chunk.translated):
            return False

        return True

    async def translate_epub(self, epub_path: str, limit: int = 3000, target_language: str = "Chinese") -> None:
        """
        翻译给定路径的 EPUB 文件，并返回翻译后 EPUB 文件的路径。

        Args:
            epub_path: 输入 EPUB 文件的路径。
            target_language: 目标翻译语言代码（例如 'zh'、'en'）。

        Returns:
            翻译后 EPUB 文件的路径。
        """
        # 解析 EPUB 文件
        parser = Parser(limit=limit, path=epub_path)
        book = parser.parse()

        # 使用 tqdm 显示外部循环进度（按文件）
        for item in tqdm(book.items, desc="翻译 EPUB", unit="文件"):
            if not item.content:
                continue
            if not item.chunks:
                continue
            for chunk in item.chunks:
                # 在开始工作流前，判断该分块是否需要翻译
                if not self._should_translate_chunk(chunk):
                    # 如果不需要，则跳过当前分块，继续下一个
                    continue

                workflow = TranslatorWorkflow()
                await workflow.arun(chunk=chunk)

            # This call should be inside the loop as it restores content for each item.
            self.replacer.restore(item)
            # These calls should be outside the loop as they operate on the entire book.
            parser.save_json(book)
        output_path = os.path.join(os.path.dirname(book.path), f"{book.name}-cn.epub")
        builder = Builder(book.extract_path, output_path)
        builder.build()
