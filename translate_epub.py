#!/usr/bin/env python3

import argparse
import asyncio
import os
from pathlib import Path

from app.core.config import settings
from app.epub.processor import EpubProcessor


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Translate EPUB files using various translation providers."
    )
    parser.add_argument("-i", "--input", required=True, help="Input EPUB file path")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="translated",
        help="Output directory for translated files (default: translated)",
    )
    parser.add_argument(
        "-p",
        "--provider",
        choices=["mistral", "google", "groq"],
        default="mistral",
        help="Translation provider to use (default: mistral)",
    )
    parser.add_argument(
        "--source-lang", default="en", help="Source language code (default: en)"
    )
    parser.add_argument(
        "--target-lang", default="zh", help="Target language code (default: zh)"
    )
    return parser.parse_args()


async def main():
    # 解析命令行参数
    args = parse_args()

    # 设置工作目录
    current_dir = Path(os.getcwd())
    input_file = current_dir / args.input
    output_dir = current_dir / args.output_dir

    # 确保输入文件存在
    if not input_file.exists():
        print(f"Error: Input file not found: {input_file}")
        return

    # 确保输出目录存在
    output_dir.mkdir(exist_ok=True)

    # 创建处理器实例
    processor = EpubProcessor(
        file_path=str(input_file),
        work_dir=str(output_dir),
        translator=args.provider,  # 使用命令行指定的翻译提供者
        source_lang=args.source_lang,
        target_lang=args.target_lang,
    )

    await processor.process()


if __name__ == "__main__":
    asyncio.run(main())
