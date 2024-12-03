"""测试 Google 翻译服务"""

import asyncio
import os

import pytest

from src.services.translation.google_translation_service import GoogleTranslationService


@pytest.fixture
def translation_service():
    """创建翻译服务实例"""
    return GoogleTranslationService(
        max_retries=3, min_wait=1, max_wait=10, timeout=30, is_testing=True
    )


@pytest.mark.asyncio
async def test_translate_text(translation_service):
    """测试文本翻译"""
    # 准备测试数据
    text = "Hello, world!"
    source_lang = "en"
    target_lang = "zh"

    # 执行翻译
    translated = await translation_service.translate(text, source_lang, target_lang)

    # 验证翻译结果
    assert translated, "翻译结果不能为空"
    assert isinstance(translated, str), "翻译结果必须是字符串"
    assert translated != text, "翻译结果不能与原文相同"
    print(f"\n翻译结果: {translated}")


@pytest.mark.asyncio
async def test_translate_long_text(translation_service):
    """测试长文本翻译"""
    # 准备测试数据
    text = "Hello, world! " * 100  # 创建一个较长的文本
    source_lang = "en"
    target_lang = "zh"

    # 执行翻译
    translated = await translation_service.translate(text, source_lang, target_lang)

    # 验证翻译结果
    assert translated, "翻译结果不能为空"
    assert isinstance(translated, str), "翻译结果必须是字符串"
    assert translated != text, "翻译结果不能与原文相同"
    assert len(translated) > 100, "长文本翻译结果长度应该大于100"
    print(f"\n翻译结果: {translated[:100]}...")
