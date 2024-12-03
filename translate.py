"""EPUB翻译脚本 - 只翻译前两章"""

import asyncio
import logging
import os
from typing import List

import ebooklib
from ebooklib import epub

from src.services.epub_service import EPUBService
from src.services.processors.html import HTMLProcessor
from src.services.translation.google_translation_service import GoogleTranslationService


async def translate_first_two_chapters(epub_path: str):
    """只翻译EPUB的前两章"""
    # 设置日志
    logging.basicConfig(
        level=logging.DEBUG,  # 改为DEBUG级别以查看更多信息
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    # 设置翻译服务
    translation_service = GoogleTranslationService(
        max_retries=3, min_wait=5, max_wait=10, timeout=30
    )

    # 创建EPUB服务
    epub_service = EPUBService(
        translation_service=translation_service, text_extraction_service=HTMLProcessor()
    )

    try:
        # 读取原始EPUB
        book = epub.read_epub(epub_path)
        logging.info("成功打开EPUB文件")

        # 获取所有HTML文件
        html_items = []
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                html_items.append(item)

        if len(html_items) < 2:
            raise ValueError("EPUB文件中没有足够的章节")

        # 只翻译前两章
        chapters_to_translate = html_items[:2]
        logging.info(
            f"准备翻译前两章: {[item.get_name() for item in chapters_to_translate]}"
        )

        # 生成输出路径
        output_dir = os.path.join(os.path.dirname(epub_path), "translated")
        os.makedirs(output_dir, exist_ok=True)

        name, ext = os.path.splitext(os.path.basename(epub_path))
        output_path = os.path.join(output_dir, f"{name}.first_two_chapters.zh{ext}")

        # 复制原文件
        if os.path.exists(output_path):
            os.remove(output_path)
        epub.write_epub(output_path, book)
        logging.info(f"已创建输出文件: {output_path}")

        # 重新打开复制的文件进行翻译
        translated_book = epub.read_epub(output_path)

        # 找到要翻译的章节
        translated_items = []
        for item in translated_book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT and any(
                orig.get_name() == item.get_name() for orig in chapters_to_translate
            ):
                translated_items.append(item)

        # 翻译这些章节
        for i, item in enumerate(translated_items, 1):
            try:
                logging.info(f"开始翻译第 {i}/2 章: {item.get_name()}")

                # 获取翻译服务的最大文本长度限制
                max_text_length = getattr(translation_service, "max_text_length", 1000)

                # 提取文本块
                content = item.get_content().decode("utf-8")
                logging.debug(f"原始内容：\n{content}")

                # 提取文本并保存标签映射
                text, tag_map = (
                    epub_service.text_extraction_service.extract_text_blocks(content)
                )
                logging.debug(f"提取的文本：\n{text}")
                logging.debug(f"标签映射：\n{tag_map}")

                # 整体翻译文本
                if text.strip():
                    try:
                        translated_text = await epub_service._translate_with_rate_limit(
                            text, source_lang="en", target_lang="zh"
                        )
                        if translated_text and translated_text.strip():
                            logging.debug(f"翻译后的文本：\n{translated_text}")
                        else:
                            logging.warning(f"翻译结果为空，保留原文: {text[:100]}...")
                            translated_text = text
                    except Exception as e:
                        logging.error(f"翻译文本时出错: {e}")
                        continue
                else:
                    logging.warning("没有找到需要翻译的文本")
                    continue

                # 用原始标签替换回特殊标记
                new_content = epub_service.text_extraction_service.rebuild_html(
                    content, translated_text, tag_map
                )

                # 更新章节内容
                item.set_content(new_content.encode("utf-8"))

            except Exception as e:
                logging.error(f"翻译第 {i} 章时出错: {str(e)}")
                continue

        # 保存更新后的文件
        epub.write_epub(output_path, translated_book)
        logging.info("翻译完成！")
        return output_path

    except Exception as e:
        logging.error(f"翻译失败: {str(e)}")
        raise


if __name__ == "__main__":
    epub_path = "tests/test.epub"
    asyncio.run(translate_first_two_chapters(epub_path))
