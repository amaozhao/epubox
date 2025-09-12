import re
import string
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from engine.constant import ID_LENGTH, PLACEHOLDER_DELIMITER, PLACEHOLDER_PATTERN
from engine.item.replacer import Placeholder, Replacer


class TestPlaceholder:
    def test_generate_unique_placeholder(self):
        placeholder = Placeholder()
        generated = placeholder.generate()
        assert len(generated) == ID_LENGTH
        assert all(c in Placeholder.characters for c in generated)

    def test_generate_no_duplicates(self):
        placeholder = Placeholder()
        placeholders = {placeholder.generate() for _ in range(100)}
        assert len(placeholders) == 100  # Ensure all generated placeholders are unique

    def test_placeholder_stores_original(self):
        placeholder = Placeholder()
        original = "<script>alert('test')</script>"
        holder = placeholder.placeholder(original)
        assert holder.startswith(PLACEHOLDER_DELIMITER) and holder.endswith(PLACEHOLDER_DELIMITER)
        assert placeholder.placer_map[holder] == original

    def test_placeholder_unique_holders(self):
        placeholder = Placeholder()
        holder1 = placeholder.placeholder("test1")
        holder2 = placeholder.placeholder("test2")
        assert holder1 != holder2
        assert holder1 in placeholder.placer_map
        assert holder2 in placeholder.placer_map


class TestReplacer:
    @pytest.fixture
    def replacer(self):
        return Replacer(parser="html.parser")

    def test_replace_ignore_tags(self, replacer):
        content = "<div><script>alert('test')</script><style>.class {}</style></div>"
        replaced = replacer.replace(content)
        soup = BeautifulSoup(replaced, "html.parser")
        for tag in ["script", "style"]:
            assert not soup.find(tag), f"{tag} should be replaced"
        placeholders = re.findall(PLACEHOLDER_PATTERN, replaced)
        assert len(placeholders) == 2  # One for script, one for style

    def test_replace_table_with_processedcode(self, replacer):
        content = '<table class="processedcode"><tr><td>Code</td></tr></table>'
        replaced = replacer.replace(content)
        soup = BeautifulSoup(replaced, "html.parser")
        assert not soup.find("table", class_="processedcode"), "Table with processedcode should be replaced"
        placeholders = re.findall(PLACEHOLDER_PATTERN, replaced)
        assert len(placeholders) == 1  # One for table.processedcode

    def test_preserve_non_ignored_tags(self, replacer):
        content = "<div><p>Hello, world!</p><span>Test</span></div>"
        replaced = replacer.replace(content)
        soup = BeautifulSoup(replaced, "html.parser")
        p_tag = soup.find("p")
        span_tag = soup.find("span")
        assert p_tag is not None, "P tag should be present"
        assert p_tag.text == "Hello, world!", "P tag text should match"
        assert span_tag is not None, "Span tag should be present"
        assert span_tag.text == "Test", "Span tag text should match"
        assert not re.findall(PLACEHOLDER_PATTERN, replaced), "No placeholders for non-ignored tags"

    def test_nested_tags(self, replacer):
        content = '<div><p>Nested <script>alert("test")</script> text</p></div>'
        replaced = replacer.replace(content)
        soup = BeautifulSoup(replaced, "html.parser")
        assert soup.find("p"), "P tag should be preserved"
        assert not soup.find("script"), "Script tag should be replaced"
        placeholders = re.findall(PLACEHOLDER_PATTERN, replaced)
        assert len(placeholders) == 1  # One for script

    def test_restore_content(self, replacer):
        content = '<table class="processedcode"><tr><td>Code</td></tr></table><p>Test</p>'
        replaced = replacer.replace(content)
        restored = replacer.restore(replaced)
        assert restored == content, "Restored content should match original"
        assert not re.findall(PLACEHOLDER_PATTERN, restored), "No placeholders should remain"

    def test_restore_with_custom_placeholders(self, replacer):
        content = '<script>alert("test")</script>'
        replaced = replacer.replace(content)
        custom_placeholders = replacer.placeholder.placer_map.copy()
        restored = replacer.restore(replaced, custom_placeholders)
        assert restored == content, "Restored content with custom placeholders should match original"

    def test_empty_input(self, replacer):
        content = ""
        replaced = replacer.replace(content)
        assert replaced == "", "Empty input should return empty string"
        restored = replacer.restore(replaced)
        assert restored == "", "Restored empty input should remain empty"

    def test_malformed_html(self, replacer):
        content = "<div><p>Unclosed tag<script>alert('test')</script>"
        replaced = replacer.replace(content)
        soup = BeautifulSoup(replaced, "html.parser")
        assert soup.find("p"), "P tag should be preserved"
        assert not soup.find("script"), "Script tag should be replaced"
        placeholders = re.findall(PLACEHOLDER_PATTERN, replaced)
        assert len(placeholders) == 1, "One placeholder for script"

    @patch("engine.item.replacer.engine_logger")
    def test_restore_unmatched_placeholders(self, mock_logger, replacer):
        content = "<p>Test</p>"
        # Construct an unmatched placeholder using constants to match the pattern
        unmatched_id = "".join(
            c for i, c in enumerate(string.ascii_letters + string.digits) if i < ID_LENGTH
        )  # Fixed 4 chars: 'abcd'
        invalid_placeholder = f"{PLACEHOLDER_DELIMITER}{unmatched_id}{PLACEHOLDER_DELIMITER}"
        invalid_content = content + invalid_placeholder
        restored = replacer.restore(invalid_content)
        remaining_placeholders = re.findall(PLACEHOLDER_PATTERN, restored)
        assert remaining_placeholders == [invalid_placeholder], (
            f"Expected unmatched placeholder, got {remaining_placeholders}, PLACEHOLDER_PATTERN={PLACEHOLDER_PATTERN}"
        )
        assert mock_logger.warning.called, (
            f"Warning should be logged for unmatched placeholders: {remaining_placeholders}, "
            f"PLACEHOLDER_PATTERN={PLACEHOLDER_PATTERN}"
        )
        assert invalid_placeholder in restored, "Unmatched placeholder should remain"
