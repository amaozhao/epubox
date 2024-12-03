"""Test HTML processor."""

import pytest
from bs4 import BeautifulSoup

from src.services.processors.html import HTMLProcessingError, HTMLProcessor
from src.services.translation.google_translation_service import GoogleTranslationService
from src.services.translation.mistral_translation_service import (
    MistralTranslationService,
)


@pytest.fixture
def google_translation_service():
    """Create Google translation service for testing."""
    return GoogleTranslationService(
        max_retries=3, min_wait=1, max_wait=10, timeout=30, is_testing=True
    )


@pytest.fixture
def mistral_translation_service():
    """Create Mistral translation service for testing."""
    return MistralTranslationService(
        model="mistral-tiny",
        max_retries=3,
        min_wait=1,
        max_wait=10,
        timeout=30,
        is_testing=True,
    )


@pytest.fixture
def google_html_processor(google_translation_service):
    """Create HTML processor with Google translation for testing."""
    return HTMLProcessor(translation_service=google_translation_service)


@pytest.fixture
def mistral_html_processor(mistral_translation_service):
    """Create HTML processor with Mistral translation for testing."""
    return HTMLProcessor(translation_service=mistral_translation_service)


def create_long_html(text_length: int) -> str:
    """创建指定长度的HTML内容"""
    return f"""
    <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Praise for Linux Pocket Guide</title><link href="epub.css" rel="stylesheet" type="text/css"/>
<meta content="urn:uuid:e48a1cb1-4de3-4395-8a0a-9dee0497b5a9" name="Adept.expected.resource"/></head><body data-type="book"><section class="praise" data-pdf-bookmark="Praise for Linux Pocket Guide" data-type="dedication" epub:type="dedication"><div class="dedication" id="id251">
<h1>Praise for <em>Linux Pocket Guide</em></h1>

<blockquote>
<p><em>Linux Pocket Guide</em> is a must-have book on every Linux user’s desk, even in this digital age. It’s like a collection of my favorite bookmarked manual pages that I keep revisiting for reference, but simpler to understand <span class="keep-together">and easier to follow</span>.</p>

<p data-type="attribution">Abhishek Prakash, cofounder of It’s FOSS</p>
</blockquote>

<blockquote>
<p>One of the beloved features of Linux environments is the assortment of small utilities that combine in wonderful ways to solve problems. This book distills that experience into an accessible reference. Even experienced readers will rediscover forgotten facets and incredible options on <span class="keep-together">their favorite tools</span>.</p>

<p data-type="attribution">Jess Males, DevOps engineer, TriumphPay</p>
</blockquote>

<blockquote>
<p>This is such a handy reference! It somehow manages to be both thorough and concise.</p>

<p data-type="attribution">Jerod Santo, changelog.com</p>
</blockquote>

</div></section></body></html>
    """


class TestHTMLProcessor:
    """Test HTML processor."""

    async def test_google_translation_segmentation(
        self, google_html_processor, google_translation_service
    ):
        """测试 Google 翻译的分段和翻译功能"""
        # 创建一个较长的HTML内容
        html_content = create_long_html(1000)  # 创建1000字符的内容

        # 翻译内容
        translated_html = await google_html_processor.translate_html(
            html_content,
            "zh",  # 源语言
            "en",  # 目标语言
            translator=google_translation_service,
        )

        # 验证翻译结果
        assert translated_html is not None
        assert len(translated_html) > 0

        # 解析HTML并验证结构
        original_soup = BeautifulSoup(html_content, "html.parser")
        translated_soup = BeautifulSoup(translated_html, "html.parser")

        # 验证 body 中的 HTML 结构完整性
        original_body = original_soup.find("body")
        translated_body = translated_soup.find("body")
        assert len(original_body.find_all(True)) == len(translated_body.find_all(True))

        # 验证所有内容元素都被翻译了
        original_text_elements = [
            elem for elem in original_body.find_all(text=True) if elem.strip()
        ]
        translated_text_elements = [
            elem for elem in translated_body.find_all(text=True) if elem.strip()
        ]
        assert len(original_text_elements) == len(translated_text_elements)

    async def test_mistral_translation_segmentation(
        self, mistral_html_processor, mistral_translation_service
    ):
        """测试 Mistral 翻译的分段和翻译功能"""
        # 创建一个较长的HTML内容
        html_content = create_long_html(1000)  # 创建1000字符的内容

        # 翻译内容
        translated_html = await mistral_html_processor.translate_html(
            html_content,
            "zh",  # 源语言
            "en",  # 目标语言
            translator=mistral_translation_service,
        )

        # 验证翻译结果
        assert translated_html is not None
        assert len(translated_html) > 0

        # 解析HTML并验证结构
        original_soup = BeautifulSoup(html_content, "html.parser")
        translated_soup = BeautifulSoup(translated_html, "html.parser")

        # 验证 body 中的 HTML 结构完整性
        original_body = original_soup.find("body")
        translated_body = translated_soup.find("body")
        assert len(original_body.find_all(True)) == len(translated_body.find_all(True))

        # 验证所有内容元素都被翻译了
        original_text_elements = [
            elem for elem in original_body.find_all(text=True) if elem.strip()
        ]
        translated_text_elements = [
            elem for elem in translated_body.find_all(text=True) if elem.strip()
        ]
        assert len(original_text_elements) == len(translated_text_elements)

    async def test_content_integrity(
        self, google_html_processor, google_translation_service
    ):
        """测试翻译后内容的完整性"""
        # 创建包含多个HTML元素的内容
        html_content = """
            <html>
                <head></head>
                <body>
                    <h1>标题1</h1>
                    <p>段落1</p>
                    <h2>标题2</h2>
                    <p>段落2</p>
                    <ul>
                        <li>列表项1</li>
                        <li>列表项2</li>
                    </ul>
                </body>
            </html>
        """

        # 直接调用 translate_html 方法
        translated_html = await google_html_processor.translate_html(
            html_content,
            "zh",  # 源语言
            "en",  # 目标语言
            translator=google_translation_service,
        )

        # 解析原始和翻译后的HTML
        original_soup = BeautifulSoup(html_content, "html.parser")
        translated_soup = BeautifulSoup(translated_html, "html.parser")

        # 验证 body 中的 HTML 结构完整性
        original_body = original_soup.find("body")
        translated_body = translated_soup.find("body")
        assert len(original_body.find_all(True)) == len(translated_body.find_all(True))

        # 验证所有内容元素都被翻译了
        original_text_elements = [
            elem for elem in original_body.find_all(text=True) if elem.strip()
        ]
        translated_text_elements = [
            elem for elem in translated_body.find_all(text=True) if elem.strip()
        ]
        assert len(original_text_elements) == len(translated_text_elements)
