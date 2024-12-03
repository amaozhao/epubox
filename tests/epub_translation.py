"""测试EPUB翻译完整流程"""

import asyncio
import os
import shutil
from pathlib import Path

import ebooklib
import pytest
from ebooklib import epub

from src.infrastructure.config import settings
from src.services.epub_service import EPUBService
from src.services.processors.html import HTMLProcessor
from src.services.translation.mistral_translation_service import (
    MistralTranslationService,
)


@pytest.fixture
def epub_path():
    """测试用EPUB文件路径"""
    return os.path.join(os.path.dirname(__file__), "test.epub")


@pytest.fixture
def translation_service():
    """创建翻译服务实例"""
    return MistralTranslationService(
        model="mistral-tiny",
        max_retries=3,
        min_wait=1,
        max_wait=10,
        timeout=30,
        is_testing=False,  # 使用真实的翻译服务
    )


@pytest.fixture
def html_processor():
    """创建HTML处理服务实例"""
    return HTMLProcessor()  # 使用翻译服务的默认块大小限制


@pytest.fixture
def epub_service(translation_service, html_processor):
    """创建EPUB服务实例"""
    return EPUBService(
        translation_service=translation_service,
        html_processor=html_processor,
        max_concurrent_translations=1,  # 使用单个并发
    )


@pytest.mark.asyncio
async def test_translate_epub_default_output(epub_path, epub_service):
    """测试使用默认输出目录的EPUB翻译"""
    # 确保测试文件存在
    assert os.path.exists(epub_path), f"测试文件不存在: {epub_path}"

    # 执行翻译（使用默认输出目录）
    output_path = await epub_service.translate_epub(
        input_path=epub_path, source_lang="en", target_lang="zh"
    )

    try:
        # 验证输出文件存在
        assert os.path.exists(output_path), "翻译后的文件未生成"

        # 验证输出文件在默认目录中
        assert "translated" in output_path, "输出文件不在默认的translated目录中"

        # 验证输出文件名格式
        expected_name = f"{os.path.splitext(os.path.basename(epub_path))[0]}.zh.epub"
        assert (
            os.path.basename(output_path) == expected_name
        ), f"输出文件名格式错误: {os.path.basename(output_path)}"

        # 验证文件内容
        await _verify_translation(epub_path, output_path)

    finally:
        # 清理输出文件
        if os.path.exists(output_path):
            os.remove(output_path)


@pytest.mark.asyncio
async def test_translate_epub_custom_output(epub_path, epub_service, tmp_path):
    """测试使用自定义输出目录的EPUB翻译"""
    # 确保测试文件存在
    assert os.path.exists(epub_path), f"测试文件不存在: {epub_path}"

    # 执行翻译（使用临时目录作为自定义输出目录）
    output_path = await epub_service.translate_epub(
        input_path=epub_path,
        source_lang="en",
        target_lang="zh",
        output_dir=str(tmp_path),  # pytest 提供的临时目录
    )

    # 验证输出文件存在
    assert os.path.exists(output_path), "翻译后的文件未生成"

    # 验证输出文件在指定目录中
    assert str(tmp_path) in output_path, "输出文件不在指定目录中"

    # 验证输出文件名格式
    expected_name = f"{os.path.splitext(os.path.basename(epub_path))[0]}.zh.epub"
    assert (
        os.path.basename(output_path) == expected_name
    ), f"输出文件名格式错误: {os.path.basename(output_path)}"

    # 验证文件内容
    await _verify_translation(epub_path, output_path)


@pytest.mark.asyncio
async def test_translate_epub_invalid_input(epub_service):
    """测试无效输入文件"""
    with pytest.raises(FileNotFoundError):
        await epub_service.translate_epub(
            input_path="non_existent.epub", source_lang="en", target_lang="zh"
        )


@pytest.mark.asyncio
async def test_translate_epub_empty_file(epub_service, tmp_path):
    """测试空文件"""
    # 创建空的EPUB文件
    empty_epub_path = os.path.join(str(tmp_path), "empty.epub")
    book = epub.EpubBook()
    epub.write_epub(empty_epub_path, book)

    try:
        output_path = await epub_service.translate_epub(
            input_path=empty_epub_path, source_lang="en", target_lang="zh"
        )

        # 验证输出文件存在
        assert os.path.exists(output_path), "翻译后的文件未生成"

        # 验证是空的EPUB文件
        translated_book = epub.read_epub(output_path)
        assert len(list(translated_book.get_items())) == 0, "空文件不应该包含任何内容"

    finally:
        # 清理测试文件
        if os.path.exists(empty_epub_path):
            os.remove(empty_epub_path)


async def _verify_translation(input_path: str, output_path: str):
    """验证翻译结果"""
    # 验证输出文件大小不为0
    assert os.path.getsize(output_path) > 0, "翻译后的文件为空"

    # 验证文件内容
    original_book = epub.read_epub(input_path)
    translated_book = epub.read_epub(output_path)

    # 验证文件结构完整性
    assert len(list(original_book.get_items())) == len(
        list(translated_book.get_items())
    ), "翻译前后的文件结构不一致"

    # 验证HTML文件已被翻译（至少内容有变化）
    original_content = []
    translated_content = []

    for item in original_book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            original_content.append(item.get_content())

    for item in translated_book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            translated_content.append(item.get_content())

    # 确保至少有一个HTML文件的内容发生了变化（被翻译）
    assert any(
        o != t for o, t in zip(original_content, translated_content)
    ), "没有检测到任何文件被翻译"
