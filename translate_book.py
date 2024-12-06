"""
Script to translate an EPUB book using Mistral translation service.
"""

import asyncio
import json
import os
from pathlib import Path

from app.core.config import settings
from app.epub.processor import EpubProcessor
from app.translation.factory import ProviderFactory
from app.translation.models import LimitType, TranslationProvider
from app.translation.providers.mistral import MistralProvider


async def translate_book(
    epub_path: str,
    source_lang: str = "en",
    target_lang: str = "zh",
    work_dir: str = "work_dir",
):
    """
    Translate an EPUB book using Mistral service.

    Args:
        epub_path: Path to the EPUB file
        source_lang: Source language code
        target_lang: Target language code
        work_dir: Working directory for processing
    """
    # 初始化EPUB处理器
    processor = EpubProcessor(epub_path, work_dir)

    try:
        # 准备文件
        print("\nPreparing EPUB file...")
        if not await processor.prepare():
            print("Failed to prepare EPUB file")
            return

        # 提取HTML内容
        print("\nExtracting HTML content...")
        html_contents = await processor.extract_content()
        if not html_contents:
            print("No HTML content found in EPUB file")
            return

        # 创建翻译提供者模型
        provider_model = TranslationProvider(
            name="mistral",
            provider_type="mistral",
            config=json.dumps({"api_key": settings.MISTRAL_API_KEY}),
            enabled=True,
            is_default=True,
            rate_limit=2,  # 降低速率限制
            retry_count=3,
            retry_delay=60,  # 增加重试延迟到60秒
            limit_type=LimitType.TOKENS,  # Mistral 需要使用基于token的限制
            limit_value=6000,  # 每次请求的token限制
        )

        # 初始化翻译提供者
        factory = ProviderFactory()
        translator = factory.create_provider(provider_model)
        await translator.initialize()

        async def translate_html_content(
            translator,
            content: str,
            source_lang: str,
            target_lang: str,
            max_tokens: int = 3000,
        ) -> str:
            """
            翻译 HTML 内容，如果内容过长会自动分割。

            Args:
                translator: 翻译器实例
                content: HTML 内容
                source_lang: 源语言
                target_lang: 目标语言
                max_tokens: 每个块的最大 token 数

            Returns:
                翻译后的 HTML 内容
            """
            import tiktoken
            from bs4 import BeautifulSoup

            # 初始化 tokenizer
            tokenizer = tiktoken.get_encoding("cl100k_base")

            # 解析 HTML
            soup = BeautifulSoup(content, "html.parser")
            translated_blocks = []
            current_block = []
            current_tokens = 0

            # 遍历所有段落级元素
            for element in soup.find_all(
                ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6"]
            ):
                # 获取元素的 HTML 字符串
                element_html = str(element)
                element_tokens = len(tokenizer.encode(element_html))

                # 如果当前块加上新元素会超过 token 限制
                if current_tokens + element_tokens > max_tokens and current_block:
                    # 翻译当前块
                    block_html = "".join(current_block)
                    translated_block = await translator.translate(
                        text=block_html,
                        source_lang=source_lang,
                        target_lang=target_lang,
                    )
                    translated_blocks.append(translated_block)

                    # 重置当前块
                    current_block = []
                    current_tokens = 0

                # 添加元素到当前块
                current_block.append(element_html)
                current_tokens += element_tokens

            # 翻译最后一个块
            if current_block:
                block_html = "".join(current_block)
                translated_block = await translator.translate(
                    text=block_html,
                    source_lang=source_lang,
                    target_lang=target_lang,
                )
                translated_blocks.append(translated_block)

            # 合并所有翻译后的块
            return "".join(translated_blocks)

        try:
            # 翻译内容
            translated_contents = {}
            total_files = len(html_contents)

            print(f"\nStarting translation of {total_files} files...")
            for idx, (name, content) in enumerate(html_contents.items(), 1):
                print(f"\nTranslating file {idx}/{total_files}: {name}")
                try:
                    # 分块翻译内容
                    print(f"  Translating content...")
                    translated_text = await translate_html_content(
                        translator=translator,
                        content=content,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        max_tokens=4000,  # 设置较小的块大小，为 HTML 标签预留空间
                    )
                    translated_contents[name] = translated_text
                    print(f"  Successfully translated {name}")
                except Exception as e:
                    print(f"Error translating file {name}: {str(e)}")
                    raise

            # 更新EPUB内容
            print("\nUpdating EPUB content...")
            if not await processor.update_content(translated_contents):
                print("Failed to update EPUB content")
                return

            print(
                f"\nTranslation completed. Output file: {processor.get_work_file_path()}"
            )

        finally:
            # 清理翻译器资源
            await translator.cleanup()

    except Exception as e:
        print(f"\nError: {str(e)}")
        raise

    finally:
        # 清理处理器资源
        await processor.cleanup()


if __name__ == "__main__":
    # 设置源文件路径
    current_dir = Path(__file__).parent
    epub_file = current_dir / "AI-Future-Blueprint.epub"

    if not epub_file.exists():
        raise FileNotFoundError(f"EPUB file not found: {epub_file}")

    # 创建工作目录
    work_dir = current_dir / "work_dir"

    # 运行翻译
    asyncio.run(
        translate_book(
            epub_path=str(epub_file),
            source_lang="en",
            target_lang="zh",
            work_dir=str(work_dir),
        )
    )
