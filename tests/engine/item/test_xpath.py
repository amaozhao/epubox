from bs4 import BeautifulSoup

from engine.item.xpath import find_by_xpath, get_xpath


class TestGetXpath:
    def test_simple_path(self):
        soup = BeautifulSoup("<html><body><p>Text</p></body></html>", "html.parser")
        p = soup.find("p")
        assert get_xpath(p) == "/html/body/p"

    def test_indexed_siblings(self):
        soup = BeautifulSoup("<html><body><p>A</p><p>B</p></body></html>", "html.parser")
        ps = soup.find_all("p")
        assert get_xpath(ps[0]) == "/html/body/p[1]"
        assert get_xpath(ps[1]) == "/html/body/p[2]"


class TestFindByXpath:
    def test_simple_find(self):
        soup = BeautifulSoup("<html><body><p>Text</p></body></html>", "html.parser")
        elem = find_by_xpath(soup, "/html/body/p")
        assert elem is not None
        assert elem.string == "Text"

    def test_indexed_find(self):
        soup = BeautifulSoup("<html><body><p>A</p><p>B</p></body></html>", "html.parser")
        elem = find_by_xpath(soup, "/html/body/p[2]")
        assert elem is not None
        assert elem.string == "B"

    def test_invalid_xpath_format(self):
        """测试无效 xpath 格式返回 None（覆盖 line 52）"""
        soup = BeautifulSoup("<html><body><p>Text</p></body></html>", "html.parser")
        assert find_by_xpath(soup, "/html/body/!!!invalid") is None

    def test_index_out_of_range(self):
        """测试索引越界返回 None（覆盖 line 59）"""
        soup = BeautifulSoup("<html><body><p>Only one</p></body></html>", "html.parser")
        assert find_by_xpath(soup, "/html/body/p[5]") is None
