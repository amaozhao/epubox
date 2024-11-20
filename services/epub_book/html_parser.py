from bs4 import BeautifulSoup, Tag

class HtmlParser:
    def __init__(self, tags=None):
        self.tags = tags or ['title', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li']
        self.soup = None

    def parse(self, html_content):
        """
        解析 HTML 内容，初始化 BeautifulSoup
        """
        self.soup = BeautifulSoup(html_content, 'html.parser')

    def extract_contents(self):
        """
        提取指定标签的内容
        """
        contents = []
        elements = []
        for tag in self.tags:
            for element in self.soup.find_all(tag):
                content = ''.join(str(child) for child in element.contents)
                contents.append(content)
                elements.append(element)
        return contents, elements

    def update_contents(self, elements, translations):
        """
        用翻译后的内容替换 HTML 中的原内容
        """
        for element, translated_content in zip(elements, translations):
            element.clear()
            element.append(BeautifulSoup(translated_content, 'html.parser'))

    def rebuild_html(self):
        """
        返回修改后的 HTML 内容
        """
        return str(self.soup)
