
from engine.item.xpath import XPATH_PATTERN, is_valid_xpath, parse_xpath


class TestXPATHPattern:
    def test_valid_text_xpath(self):
        assert XPATH_PATTERN.match("html/body/div[0]:text:0")

    def test_valid_attr_xpath(self):
        assert XPATH_PATTERN.match("html/body/div[0]:attr:5")

    def test_valid_deep_xpath(self):
        assert XPATH_PATTERN.match("html/body/div/span/p[1]:text:0")

    def test_invalid_no_colon(self):
        assert not XPATH_PATTERN.match("html/body/div[0]text0")

    def test_invalid_no_type(self):
        assert not XPATH_PATTERN.match("html/body/div[0]:0")

    def test_invalid_bad_type(self):
        assert not XPATH_PATTERN.match("html/body/div[0]:xyz:0")


class TestIsValidXPath:
    def test_valid(self):
        assert is_valid_xpath("html/body/div[0]:text:0") is True

    def test_valid_attr(self):
        assert is_valid_xpath("html/body/p[1]:attr:3") is True

    def test_empty_string(self):
        assert is_valid_xpath("") is False

    def test_invalid_format(self):
        assert is_valid_xpath("html/body/div[0]text0") is False


class TestParseXPath:
    def test_parse_text_xpath(self):
        path_prefix, xpath_type, index = parse_xpath("html/body/div[0]:text:0")
        assert path_prefix == "html/body/div[0]"
        assert xpath_type == "text"
        assert index == 0

    def test_parse_attr_xpath(self):
        path_prefix, xpath_type, index = parse_xpath("html/body/p[1]:attr:5")
        assert path_prefix == "html/body/p[1]"
        assert xpath_type == "attr"
        assert index == 5

    def test_parse_deep_xpath(self):
        path_prefix, xpath_type, index = parse_xpath("html/body/div/span/p[1]:text:2")
        assert path_prefix == "html/body/div/span/p[1]"
        assert xpath_type == "text"
        assert index == 2
