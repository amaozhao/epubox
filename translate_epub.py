#!/usr/bin/env python3

import asyncio
import os
from pathlib import Path

from app.core.config import settings
from app.epub.processor import EpubProcessor


async def main():
    # 设置工作目录
    current_dir = Path(os.getcwd())
    input_file = current_dir / "test.epub"
    output_dir = current_dir / "translated"

    # 确保输出目录存在
    output_dir.mkdir(exist_ok=True)

    # 创建处理器实例
    processor = EpubProcessor(
        file_path=str(input_file),
        work_dir=str(output_dir),
        translator="mistral",  # 使用 Mistral 作为翻译提供者
        source_lang="en",  # 源语言为英语
        target_lang="zh",  # 目标语言为中文
    )

    await processor.process()


if __name__ == "__main__":
    asyncio.run(main())
