import os
import re
import zipfile

from engine.core.logger import engine_logger as logger

# 全局字体样式常量
GLOBAL_FONT_STYLE = """
/* 全局字体设置 */
body, p, h1, h2, h3, h4, h5, h6, div, span, .figure-caption, .callout-heading, .callout, .index-head {
    font-family: STYuanti, serif;
}
"""

# 代码和数学标签字体样式常量
CODE_FONT_STYLE = """
/* 代码和数学标签使用等宽字体 */
code, pre, math, kbd, samp, .source-code, .source-inline, .screen-inline, .sc-highlight, .console, .highlight {
    font-family: "Courier New", monospace;
}
"""


class Builder:
    """
    负责将一个目录中的所有文件打包成一个 EPUB 文件，设置全局语言并替换字体。
    """

    def __init__(self, dir: str, output: str, language: str = "zh"):
        """
        初始化 Builder。

        Args:
            dir: 包含所有解压文件的源目录路径。
            output: 生成的 EPUB 文件的保存路径。
            language: 要设置的 EPUB 全局语言代码（默认 'zh'）。
        """
        self.dir = dir
        self.output = output
        self.language = language

    def _modify_content_opf(self, content_opf_path: str) -> bool:
        """
        修改 content.opf 文件，设置或更新 <dc:language> 或 <dc: language> 标签。

        Args:
            content_opf_path: content.opf 文件的路径。

        Returns:
            bool: 修改是否成功。
        """
        if not os.path.exists(content_opf_path):
            logger.warning(f"未找到 content.opf 文件：{content_opf_path}")
            return False

        # 读取 content.opf 文件内容
        try:
            with open(content_opf_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"读取 content.opf 文件失败：{e}")
            return False

        # 设置语言：检查是否已存在 <dc:language> 或 <dc: language> 标签
        language_pattern = r"<dc:\s*language>[^<]*</dc:\s*language>"
        if re.search(language_pattern, content):
            content = re.sub(language_pattern, f"<dc:language>{self.language}</dc:language>", content)
        else:
            metadata_end_pattern = r"</metadata>"
            if re.search(metadata_end_pattern, content):
                content = re.sub(
                    metadata_end_pattern, f"  <dc:language>{self.language}</dc:language>\n</metadata>", content
                )
            else:
                logger.warning("content.opf 文件中未找到 </metadata> 标签，无法添加语言")
                return False

        # 写回修改后的 content.opf 文件
        try:
            with open(content_opf_path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            logger.warning(f"写入 content.opf 文件失败：{e}")
            return False

    def _find_css_files(self, content_opf_path: str) -> list:
        """
        从 content.opf 文件中查找所有 CSS 文件的路径。

        Args:
            content_opf_path: content.opf 文件的路径。

        Returns:
            包含 CSS 文件绝对路径的列表。
        """
        css_files = []
        try:
            with open(content_opf_path, "r", encoding="utf-8") as f:
                content = f.read()
            # 查找 <item> 标签中 media-type="text/css" 的 href 属性
            pattern = r'<item[^>]*href=["\'](.*?\.css)["\'][^>]*media-type=["\']text/css["\'][^>]*>'
            matches = re.findall(pattern, content)
            for css_href in matches:
                # 将相对路径转换为绝对路径
                css_path = os.path.join(os.path.dirname(content_opf_path), css_href)
                if os.path.exists(css_path):
                    css_files.append(css_path)
            if not css_files:
                logger.warning("未找到任何 CSS 文件，请确保 content.opf 中声明了 CSS 文件")
        except Exception as e:
            logger.warning(f"读取 content.opf 文件以查找 CSS 文件失败：{e}")
        return css_files

    def _modify_css_file(self, css_path: str) -> bool:
        """
        修改 CSS 文件，替换全局字体为 STYuanti，代码和数学标签为等宽字体。

        Args:
            css_path: CSS 文件的路径。

        Returns:
            bool: 修改是否成功。
        """
        # 读取现有 CSS 文件内容
        try:
            with open(css_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"读取 CSS 文件失败：{css_path}, 错误：{e}")
            return False

        # 定义目标字体
        global_font = "STYuanti, serif"
        code_font = '"Courier New", monospace'

        # 匹配任何选择器的 font-family 声明，支持多行值
        font_family_pattern = r"([^{]*)\s*\{([^}]*?font-family\s*:[^}]*?;)"
        code_selectors = r"\b(code|pre|math|kbd|samp|\.source-code|\.source-inline|\.screen-inline|\.sc-highlight|\.console|\.highlight)\b"

        def replace_font(match):
            selector = match.group(1).strip()
            # 检查选择器是否包含代码/数学相关关键字
            if re.search(code_selectors, selector, re.IGNORECASE):
                return re.sub(
                    r"font-family\s*:[^;]*;", f"font-family: {code_font};", match.group(0), flags=re.IGNORECASE
                )
            else:
                return re.sub(
                    r"font-family\s*:[^;]*;", f"font-family: {global_font};", match.group(0), flags=re.IGNORECASE
                )

        # 替换所有 font-family 声明
        content = re.sub(font_family_pattern, replace_font, content, flags=re.IGNORECASE | re.DOTALL)

        # 如果没有任何 font-family 声明，添加常量定义的样式
        if not re.search(r"font-family\s*:[^;]*;", content, re.IGNORECASE):
            content += "\n" + GLOBAL_FONT_STYLE + "\n" + CODE_FONT_STYLE

        # 写回修改后的 CSS 文件
        try:
            with open(css_path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            logger.warning(f"写入 CSS 文件失败：{css_path}, 错误：{e}")
            return False

    def build(self) -> str:
        """
        将源目录下的所有文件打包成一个 EPUB 文件，设置语言并替换字体。

        Returns:
            生成的 EPUB 文件的路径。
        """
        if not os.path.exists(self.dir):
            logger.warning(f"源目录不存在：{self.dir}")
            return self.output

        # 查找 content.opf 文件
        content_opf_path = None
        for root, _, files in os.walk(self.dir):
            if "content.opf" in files:
                content_opf_path = os.path.join(root, "content.opf")
                break

        if not content_opf_path:
            logger.warning("未找到 content.opf 文件，跳过语言和字体设置")
        else:
            # 修改 content.opf 文件以设置语言
            self._modify_content_opf(content_opf_path)

            # 查找并修改所有 CSS 文件
            css_files = self._find_css_files(content_opf_path)
            for css_path in css_files:
                self._modify_css_file(css_path)

        # 确保输出目录存在
        try:
            os.makedirs(os.path.dirname(self.output), exist_ok=True)
        except Exception as e:
            logger.warning(f"创建输出目录失败：{os.path.dirname(self.output)}, 错误：{e}")

        # 打包 EPUB 文件
        try:
            with zipfile.ZipFile(self.output, "w", zipfile.ZIP_DEFLATED) as zf:
                # EPUB 规范要求 'mimetype' 文件必须是未压缩的，并且是第一个文件
                mimetype_path = os.path.join(self.dir, "mimetype")
                if os.path.exists(mimetype_path):
                    zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
                else:
                    zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

                # 遍历源目录中的所有文件和子目录
                for root, dirs, files in os.walk(self.dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if file == "mimetype" and root == self.dir:
                            continue
                        arcname = os.path.relpath(file_path, self.dir)
                        try:
                            zf.write(file_path, arcname)
                        except Exception as e:
                            logger.warning(f"打包文件失败：{file_path}, 错误：{e}")

            logger.info(f"成功将目录 {self.dir} 打包为 EPUB 文件：{self.output}")
        except Exception as e:
            logger.warning(f"打包 EPUB 文件失败：{self.output}, 错误：{e}")

        return self.output


if __name__ == "__main__":
    builder = Builder(
        "/Users/amaozhao/workspace/epubox/temp/depth-leadership-unlocking-unconscious/",
        "/Users/amaozhao/workspace/epubox/depth-leadership-unlocking-unconscious-new.epub",
        language="zh",
    )
    builder.build()
