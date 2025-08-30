import os
import zipfile


class Builder:
    """
    负责将一个目录中的所有文件打包成一个 EPUB 文件。
    """

    def __init__(self, dir: str, output: str):
        """
        初始化 Builder。

        Args:
            output: 包含所有解压文件的源目录路径。
            output: 生成的 EPUB 文件的保存路径。
        """
        self.dir = dir
        self.output = output

    def build(self) -> str:
        """
        将源目录下的所有文件打包成一个 EPUB 文件。

        Returns:
            生成的 EPUB 文件的路径。
        """
        if not os.path.exists(self.dir):
            raise FileNotFoundError(f"源目录不存在：{self.dir}")

        # 确保输出目录存在
        os.makedirs(os.path.dirname(self.output), exist_ok=True)

        with zipfile.ZipFile(self.output, "w", zipfile.ZIP_DEFLATED) as zf:
            # EPUB 规范要求 'mimetype' 文件必须是未压缩的，并且是第一个文件
            mimetype_path = os.path.join(self.dir, "mimetype")
            if os.path.exists(mimetype_path):
                # 写入未压缩的 mimetype 文件
                zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
            else:
                # 如果 mimetype 文件不存在，则创建一个，但请注意这可能不是一个有效的 EPUB
                zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

            # 遍历源目录中的所有文件和子目录
            for root, dirs, files in os.walk(self.dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    # 排除已经处理过的 mimetype 文件
                    if file == "mimetype" and root == self.dir:
                        continue

                    # 创建文件在 zip 中的相对路径
                    arcname = os.path.relpath(file_path, self.dir)

                    # 将文件写入 zipfile
                    zf.write(file_path, arcname)

        print(f"成功将目录 {self.dir} 打包为 EPUB 文件：{self.output}")
        return self.output


if __name__ == "__main__":
    builder = Builder(
        "/Users/amaozhao/workspace/epubox/temp/depth-leadership-unlocking-unconscious/",
        "/Users/amaozhao/workspace/epubox/depth-leadership-unlocking-unconscious-new.epub",
    )
    builder.build()
