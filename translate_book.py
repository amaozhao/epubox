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

        try:
            # 翻译内容
            translated_contents = {}
            total_files = len(html_contents)

            print(f"\nStarting translation of {total_files} files...")
            for idx, (name, tasks) in enumerate(html_contents.items(), 1):
                print(f"\nTranslating file {idx}/{total_files}: {name}")
                try:
                    # 翻译每个任务
                    translated_tasks = []
                    total_tasks = len(tasks)
                    for task_idx, task in enumerate(tasks, 1):
                        print(f"  Translating task {task_idx}/{total_tasks}...")
                        translated_text = await translator.translate(
                            text=task["content"],
                            source_lang=source_lang,
                            target_lang=target_lang,
                        )
                        translated_tasks.append(translated_text)
                        print(
                            f"  Successfully translated task {task_idx}/{total_tasks}"
                        )

                        # 在任务之间添加延迟
                        if task_idx < total_tasks:
                            delay = 5  # 30秒延迟
                            print(f"  Waiting {delay} seconds before next task...")
                            await asyncio.sleep(delay)

                    translated_contents[name] = translated_tasks
                    print(f"Successfully translated file {idx}/{total_files}")

                    # 在文件之间添加延迟
                    if idx < total_files:
                        delay = 10  # 60秒延迟
                        print(f"Waiting {delay} seconds before next file...")
                        await asyncio.sleep(delay)

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
