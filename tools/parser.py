from bs4 import BeautifulSoup
import re
from tools.manager import TranslationManager
from translators import GoogleTranslator
import asyncio


class HTMLParser:
    def __init__(self, exclude_tags=None, max_chunk_size=2000):
        self.exclude_tags = exclude_tags or set()
        self.max_chunk_size = max_chunk_size

    def parse_html(self, html_content):
        """
        提取头部信息（xml声明、doctype声明）和主体内容。
        :param html_content: 原始HTML内容
        :return: 返回 xml_declaration, doctype 和 解析后的主体内容（body_content）
        """
        xml_declaration, doctype = self._extract_header(html_content)
        body_content = html_content[len(xml_declaration) + len(doctype) :]
        return xml_declaration, doctype, body_content

    def _extract_header(self, html_content):
        """
        提取XML声明和DOCTYPE声明
        :param html_content: 原始HTML内容
        :return: 返回xml声明和doctype声明
        """
        xml_declaration = ""
        doctype = ""

        # 匹配并保留头部声明
        xml_match = re.match(r"^\s*(<?xml[^>]*?>)", html_content)
        doctype_match = re.match(r"^\s*(<!DOCTYPE[^>]*?>)", html_content)

        if xml_match:
            xml_declaration = xml_match.group(0)  # 原样保留，避免strip丢失内容

        if doctype_match:
            doctype = doctype_match.group(0)  # 原样保留，避免strip丢失内容

        return xml_declaration, doctype

    def process_html(self, html_content, manager):
        """
        处理HTML，提取头部信息并翻译主体内容。
        :param html_content: 原始HTML内容
        :param manager: 翻译管理器实例（TranslationManager）
        :return: 翻译后的HTML内容
        """
        # 使用BeautifulSoup解析主体内容
        soup = BeautifulSoup(html_content, "html.parser")

        # 提取所有文本节点，排除排除的标签
        texts = [
            element
            for element in soup.find_all(text=True)
            if element.parent.name not in self.exclude_tags
        ]

        # 过滤掉空白文本节点（例如纯空格、换行等）
        non_empty_texts = [text for text in texts if text.strip()]

        # 使用翻译管理器进行批量翻译
        translated_texts = asyncio.run(manager.translate_batch(non_empty_texts))

        # 替换原文本为翻译后的文本
        for original, translated in zip(non_empty_texts, translated_texts):
            original.replace_with(translated)

        # 返回处理后的HTML内容
        return soup.prettify()


class HTMLTranslator:
    def __init__(
        self,
        exclude_tags=None,
        max_chunk_size=2000,
        target_language="en",
        max_concurrent_tasks=1,
    ):
        self.parser = HTMLParser(exclude_tags, max_chunk_size)
        self.translator = GoogleTranslator(language=target_language)
        self.manager = TranslationManager(self.translator, max_concurrent_tasks)

    def translate_html(self, html_content):
        """
        翻译HTML内容
        :param html_content: 原始HTML内容
        :return: 翻译后的HTML内容
        """
        # 提取xml声明、doctype声明和主体内容
        xml_declaration, doctype, body_content = self.parser.parse_html(html_content)

        # 使用翻译器翻译主体内容
        translated_body_content = self.parser.process_html(body_content, self.manager)

        # 返回组合后的HTML，确保前导内容不被修改
        return f"{xml_declaration}\n{doctype}\n{translated_body_content}"


if __name__ == "__main__":
    # 使用示例
    html_content = """<?xml version="1.0" encoding="utf-8" ?>
<!DOCTYPE html>
    <html lang="en-US">
      <head><title>Example</title></head>
      <body>
        <p>Hello, this is an example paragraph.</p>
        <pre><code>Some code example</code></pre>
        <p>Another paragraph that needs translation.</p>
      </body>
    </html>
    """

    # 创建翻译器
    html_translator = HTMLTranslator(exclude_tags={"code", "pre"}, target_language="en")

    # 翻译HTML
    translated_html = html_translator.translate_html(html_content.strip())

    print(translated_html)
