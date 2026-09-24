import os
import re
import zipfile

from engine.core.logger import engine_logger as logger


class Builder:
    """
    负责将一个目录中的所有文件打包成一个 EPUB 文件，并设置全局语言。
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
        修改 .opf 文件，设置或更新语言标签。
        会修改 <dc:language> 和 <meta property="dcterms:language"> 两个标签。

        Args:
            content_opf_path: .opf 文件的路径。

        Returns:
            bool: 修改是否成功。
        """
        if not os.path.exists(content_opf_path):
            logger.warning(f"未找到 .opf 文件：{content_opf_path}")
            return False

        # 读取 .opf 文件内容
        try:
            with open(content_opf_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"读取 .opf 文件失败：{content_opf_path}, 错误：{e}")
            return False

        modified = False

        # 1. 修改 <dc:language> 标签并保留原有属性
        dc_lang_pattern = r"(<dc:language\b[^>]*>)[^<]*(</dc:language>)"
        if re.search(dc_lang_pattern, content):
            content = re.sub(dc_lang_pattern, rf"\g<1>{self.language}\g<2>", content)
            modified = True

        # 2. 修改 <meta id="meta-language" property="dcterms:language">xxx</meta> 标签
        meta_lang_pattern = r'<meta\s+id="meta-language"\s+property="dcterms:language"[^>]*>[^<]*</meta>'
        if re.search(meta_lang_pattern, content):
            content = re.sub(
                meta_lang_pattern,
                f'<meta id="meta-language" property="dcterms:language">{self.language}</meta>',
                content,
            )
            modified = True

        if not modified:
            logger.warning(f"未找到需要修改的语言标签，跳过语言设置：{content_opf_path}")

        # 写回修改后的 .opf 文件
        try:
            with open(content_opf_path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception as e:
            logger.warning(f"写入 .opf 文件失败：{content_opf_path}, 错误：{e}")
            return False

    def build(self) -> str:
        """
        将源目录下的所有文件打包成一个 EPUB 文件，并设置语言。

        Returns:
            生成的 EPUB 文件的路径。
        """
        if not os.path.exists(self.dir):
            logger.warning(f"源目录不存在：{self.dir}")
            return self.output

        # 查找扩展名为 .opf 的文件
        content_opf_path = None
        for root, _, files in os.walk(self.dir):
            for file in files:
                if file.lower().endswith(".opf"):
                    content_opf_path = os.path.join(root, file)
                    break  # 只处理第一个找到的 .opf 文件
            if content_opf_path:
                break  # 如果找到，退出外层循环

        if not content_opf_path:
            logger.warning("未找到任何 .opf 文件，跳过语言设置")
        else:
            # 修改 .opf 文件以设置语言
            self._modify_content_opf(content_opf_path)

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
