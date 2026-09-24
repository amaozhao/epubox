from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit
from xml.parsers import expat

import tinycss2

from engine.core.logger import engine_logger as logger

FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}
TEXT_RESOURCE_EXTENSIONS = {".css", ".xhtml", ".html", ".htm", ".svg", ".xml", ".ncx"}
TEXT_MEDIA_TYPES = {
    "text/css",
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/xml",
    "text/xml",
    "application/x-dtbncx+xml",
}
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
MONOSPACE_TAGS = {"code", "pre", "kbd", "samp"}


class Builder:
    """
    负责将一个目录中的所有文件打包成一个 EPUB 文件，并设置全局语言。
    """

    def __init__(self, dir: str, output: str, language: str = "zh-CN", optimize_fonts: bool = True):
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
        self.optimize_fonts = optimize_fonts

    def _find_content_opf(self) -> str | None:
        root = Path(self.dir).resolve()
        container_path = root / "META-INF" / "container.xml"
        if container_path.exists():
            try:
                tree = self._parse_xml(container_path)
                for element in tree.getroot().iter():
                    if self._local_name(element.tag) != "rootfile":
                        continue
                    full_path = self._resource_path(element.attrib.get("full-path", ""))
                    candidate = (root / full_path).resolve()
                    if candidate.is_relative_to(root) and candidate.is_file():
                        return str(candidate)
            except (ET.ParseError, OSError, ValueError) as error:
                logger.warning(f"无法解析 container.xml，将回退搜索 OPF: {error}")

        for path in root.rglob("*.opf"):
            if path.is_file():
                return str(path)
        return None

    @staticmethod
    def _parse_xml(path: Path) -> ET.ElementTree[ET.Element[str]]:
        namespaces: dict[str, str] = {}
        for _, (prefix, uri) in ET.iterparse(path, events=("start-ns",)):
            namespaces.setdefault(prefix, uri)
        for prefix, uri in namespaces.items():
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                pass
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        return cast("ET.ElementTree[ET.Element[str]]", ET.parse(path, parser=parser))

    def _write_document_tree(self, tree: ET.ElementTree[ET.Element[str]], path: Path) -> None:
        original = path.read_bytes()
        parser = expat.ParserCreate()
        depth = 0
        root_start: int | None = None
        root_end: int | None = None

        def start_element(_name, _attributes) -> None:
            nonlocal depth, root_start
            if depth == 0:
                root_start = parser.CurrentByteIndex
            depth += 1

        def end_element(_name) -> None:
            nonlocal depth, root_end
            depth -= 1
            if depth == 0:
                closing_start = parser.CurrentByteIndex
                root_end = original.find(b">", closing_start) + 1

        parser.StartElementHandler = start_element
        parser.EndElementHandler = end_element
        parser.Parse(original, True)
        if root_start is None or root_end is None or root_end <= root_start:
            raise ValueError("无法定位 XHTML 根元素")

        declaration = re.search(rb"<\?xml[^>]*encoding=[\"']([^\"']+)", original[:root_start], re.IGNORECASE)
        encoding = declaration.group(1).decode("ascii") if declaration else "utf-8"
        serialized = ET.tostring(tree.getroot(), encoding=encoding)
        path.write_bytes(original[:root_start] + serialized + original[root_end:])

    @staticmethod
    def _local_name(tag: object) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    @staticmethod
    def _resource_path(value: str) -> str:
        return unquote(urlsplit(value.strip()).path)

    @staticmethod
    def _supports_document_encoding(path: Path) -> bool:
        data = path.read_bytes()[:256]
        if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
            return False
        if data.startswith((b"<\x00", b"\x00<")):
            return False
        declaration = re.search(rb"encoding=[\"']([^\"']+)", data, re.IGNORECASE)
        if declaration is None:
            return True
        encoding = declaration.group(1).decode("ascii", errors="ignore").lower().replace("_", "-")
        return encoding in {"utf-8", "utf8", "us-ascii", "ascii"}

    @classmethod
    def _contains_device_font_risk(cls, text: str) -> bool:
        tokens = tinycss2.parse_component_value_list(text, skip_comments=False)

        def contains_pua(nodes) -> bool:
            for node in nodes or []:
                if node.type == "error":
                    return True
                value = getattr(node, "value", None)
                if isinstance(value, str) and cls._contains_pua(value):
                    return True
                for attribute in ("prelude", "content", "arguments"):
                    if contains_pua(getattr(node, attribute, None)):
                        return True
            return False

        return bool(
            contains_pua(tokens)
            or re.search(r"writing-mode\s*:", text, re.IGNORECASE)
            or re.search(r"(?:fontawesome|material[- ]icons|icon[-_ ]font|glyph)", text, re.IGNORECASE)
        )

    @staticmethod
    def _contains_pua(text: str | None) -> bool:
        return bool(text and re.search(r"[\ue000-\uf8ff]|[\U000f0000-\U0010ffff]", text))

    @staticmethod
    def _contains_cjk(text: str | None) -> bool:
        return bool(
            text
            and re.search(
                r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U0003134f]",
                text,
            )
        )

    def _has_inline_script(self, opf_path: Path, items: list[ET.Element]) -> bool:
        publication_root = Path(self.dir).resolve()
        try:
            for item in items:
                if item.attrib.get("media-type", "") not in {"application/xhtml+xml", "text/html"}:
                    continue
                href = self._resource_path(item.attrib.get("href", ""))
                path = (opf_path.parent / href).resolve()
                if not path.is_relative_to(publication_root):
                    return True
                if not self._supports_document_encoding(path):
                    return True
                tree = self._parse_xml(path)
                if any(self._local_name(element.tag) == "script" for element in tree.getroot().iter()):
                    return True
        except (ET.ParseError, OSError, ValueError):
            return True
        return False

    def _mark_font_elements(self, root: ET.Element) -> None:
        parents = {child: parent for parent in root.iter() for child in parent}

        def is_special(target: ET.Element) -> bool:
            current: ET.Element | None = target
            while current is not None:
                if self._local_name(current.tag) in {"svg", "math"}:
                    return True
                current = parents.get(current)
            return False

        states: dict[ET.Element, list[bool]] = {}
        for element in root.iter():
            candidates = [(element, element.text), (parents.get(element), element.tail)]
            for target, text in candidates:
                if target is None:
                    continue
                state = states.setdefault(target, [False, False])
                state[0] = state[0] or self._contains_cjk(text)
                state[1] = (
                    state[1]
                    or self._contains_pua(text)
                    or any(self._contains_pua(value) for value in target.attrib.values())
                )

        unsafe_ancestors: set[ET.Element] = set()
        special_elements = {
            element
            for element in root.iter()
            if self._local_name(element.tag) in {"svg", "math"} or states.get(element, [False, False])[1]
        }
        for element in special_elements:
            current: ET.Element | None = element
            while current is not None:
                unsafe_ancestors.add(current)
                current = parents.get(current)

        for target, (has_cjk, has_pua) in states.items():
            classes = target.attrib.get("class", "").split()
            if has_pua:
                classes = [name for name in classes if name != "epubox-device-font"]
                if "epubox-preserve-font" not in classes:
                    classes.append("epubox-preserve-font")
                target.set("class", " ".join(classes))
                continue
            if not has_cjk or is_special(target) or target in unsafe_ancestors:
                continue

            declarations = tinycss2.parse_declaration_list(
                target.attrib.get("style", ""), skip_comments=False, skip_whitespace=False
            )
            if any(declaration.type == "error" for declaration in declarations):
                raise ValueError("行内 CSS 解析失败")
            declarations = [
                declaration
                for declaration in declarations
                if not (declaration.type == "declaration" and declaration.lower_name == "font-family")
            ]
            style = tinycss2.serialize(declarations).strip()
            if style and not style.endswith(";"):
                style += ";"
            family = "monospace" if self._local_name(target.tag) in MONOSPACE_TAGS else "serif"
            target.set("style", f"{style} font-family: {family} !important;".lstrip())
            if "epubox-device-font" not in classes:
                classes.append("epubox-device-font")
            target.set("class", " ".join(classes))

    def _is_fixed_layout(self, opf_root: ET.Element) -> bool:
        for element in opf_root.iter():
            if (
                self._local_name(element.tag) == "itemref"
                and "rendition:layout-pre-paginated" in element.attrib.get("properties", "").split()
            ):
                return True
            if self._local_name(element.tag) != "meta":
                continue
            value = (element.text or element.attrib.get("content", "")).strip().lower()
            if element.attrib.get("property") == "rendition:layout" and value == "pre-paginated":
                return True
            if element.attrib.get("name", "").lower() == "fixed-layout" and value in {"true", "yes"}:
                return True
        return False

    @classmethod
    def _css_urls(cls, css: str) -> set[str]:
        rules = tinycss2.parse_stylesheet(css, skip_comments=False, skip_whitespace=False)
        if any(rule.type == "error" for rule in rules):
            raise ValueError("CSS 解析失败")

        urls: set[str] = set()

        def walk(nodes) -> None:
            for node in nodes or []:
                if node.type == "error":
                    raise ValueError("CSS 解析失败")
                if node.type == "url":
                    urls.add(node.value)
                elif node.type == "function" and node.lower_name == "url":
                    value = tinycss2.serialize(node.arguments).strip().strip("\"'")
                    if value:
                        urls.add(value)
                for attribute in ("prelude", "content", "arguments"):
                    nested = getattr(node, attribute, None)
                    if nested:
                        walk(nested)

        walk(rules)
        return urls

    @staticmethod
    def _resolve_resource_url(root: Path, source: Path, value: str) -> Path | None:
        parsed = urlsplit(value.strip())
        if parsed.scheme or parsed.netloc or not parsed.path:
            return None
        path = unquote(parsed.path)
        if path.startswith("/"):
            return (root / path.lstrip("/")).resolve()
        return (source.parent / path).resolve()

    def _collect_resource_references(
        self,
        root: Path,
        opf_path: Path,
        manifest_resources: dict[Path, str],
    ) -> set[Path] | None:
        references: set[Path] = set()
        try:
            resources = {
                path.resolve(): "text/css" if path.suffix.lower() == ".css" else "application/xml"
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in TEXT_RESOURCE_EXTENSIONS
            }
            resources.update(manifest_resources)
            for path, media_type in resources.items():
                if not path.is_file() or path.resolve() in {
                    opf_path.resolve(),
                    (root / "META-INF/encryption.xml").resolve(),
                }:
                    continue
                if media_type == "text/css":
                    values = self._css_urls(path.read_text(encoding="utf-8"))
                else:
                    tree = self._parse_xml(path)
                    values: set[str] = set()
                    for element in tree.getroot().iter():
                        for attribute, value in element.attrib.items():
                            if self._local_name(attribute) in {"href", "src"}:
                                values.add(value)
                            elif self._local_name(attribute) == "style":
                                values.update(self._css_urls(f"x {{{value}}}"))
                        if self._local_name(element.tag) == "style" and element.text:
                            values.update(self._css_urls(element.text))
                for value in values:
                    resolved = self._resolve_resource_url(root, path, value)
                    if resolved is not None and resolved.is_relative_to(root):
                        references.add(resolved)
        except (ET.ParseError, OSError, UnicodeError, ValueError) as error:
            logger.warning(f"无法完整解析 EPUB 资源引用，跳过字体剪枝: {error}")
            return None
        return references

    def _encrypted_paths(self) -> set[Path] | None:
        encryption_path = Path(self.dir) / "META-INF" / "encryption.xml"
        if not encryption_path.exists():
            return set()
        try:
            tree = self._parse_xml(encryption_path)
        except (ET.ParseError, OSError, ValueError) as error:
            logger.warning(f"无法解析 encryption.xml，跳过字体剪枝: {error}")
            return None

        encrypted: set[Path] = set()
        for element in tree.getroot().iter():
            if self._local_name(element.tag) != "CipherReference":
                continue
            uri = self._resource_path(element.attrib.get("URI", ""))
            if uri:
                encrypted.add((Path(self.dir) / uri).resolve())
        return encrypted

    def _apply_device_font_mode(
        self,
        opf_path: Path,
        opf_root: ET.Element,
        items: list[ET.Element],
    ) -> bool:
        if self._is_fixed_layout(opf_root):
            logger.info("检测到 fixed-layout EPUB，保留原字体配置")
            return False

        document_paths: list[Path] = []
        css_paths: list[Path] = []
        publication_root = Path(self.dir).resolve()
        try:
            for item in items:
                href = self._resource_path(item.attrib.get("href", ""))
                path = (opf_path.parent / href).resolve()
                if not path.is_relative_to(publication_root):
                    logger.warning(f"manifest 路径越界，保留原字体配置: {href}")
                    return False
                media_type = item.attrib.get("media-type", "")
                if media_type in {"text/javascript", "application/javascript"} or path.suffix.lower() == ".js":
                    logger.info("检测到脚本资源，无法证明动态字体引用，保留全部字体")
                    return False
                if media_type in {"application/xhtml+xml", "text/html"}:
                    if not self._supports_document_encoding(path):
                        logger.info(f"检测到非 UTF-8 XHTML，保留原字体配置: {path.name}")
                        return False
                    document_paths.append(path)
                elif media_type == "text/css":
                    css_paths.append(path)

            raw_styles = {path: path.read_text(encoding="utf-8") for path in css_paths}
        except (OSError, UnicodeError) as error:
            logger.warning(f"无法读取 EPUB 样式资源，保留原字体配置: {error}")
            return False

        if any(self._contains_device_font_risk(text) for text in raw_styles.values()):
            logger.info("检测到 CSS icon/PUA 或特殊排版，保留原字体配置")
            return False

        try:
            document_trees = {path: self._parse_xml(path) for path in document_paths}
            for tree in document_trees.values():
                if any(self._local_name(element.tag) == "script" for element in tree.getroot().iter()):
                    raise ValueError("XHTML 含脚本")
                for element in tree.getroot().iter():
                    inline_style = element.attrib.get("style", "")
                    if inline_style and self._contains_device_font_risk(inline_style):
                        raise ValueError("行内 CSS 含特殊排版")
                    if (
                        self._local_name(element.tag) == "style"
                        and element.text
                        and self._contains_device_font_risk(element.text)
                    ):
                        raise ValueError("内联 CSS 含 icon/PUA 或特殊排版")
                self._mark_font_elements(tree.getroot())
        except (ET.ParseError, OSError, ValueError) as error:
            logger.warning(f"无法安全处理 EPUB 样式，保留原字体配置: {error}")
            return False

        for path, tree in document_trees.items():
            self._write_document_tree(tree, path)
        logger.info("可重排 EPUB 的中文元素已切换设备字体；特殊内容保留原字体")
        return True

    def _prune_unreferenced_fonts(
        self,
        opf_path: Path,
        manifest: ET.Element,
        items: list[ET.Element],
        encrypted_paths: set[Path],
    ) -> tuple[int, int]:
        root = Path(self.dir).resolve()
        font_files = {
            path.resolve() for path in root.rglob("*") if path.is_file() and path.suffix.lower() in FONT_EXTENSIONS
        }
        font_items: dict[Path, list[ET.Element]] = {}
        manifest_resources: dict[Path, str] = {}
        for item in items:
            href = self._resource_path(item.attrib.get("href", ""))
            path = (opf_path.parent / href).resolve()
            media_type = item.attrib.get("media-type", "")
            if media_type in TEXT_MEDIA_TYPES and path.is_relative_to(root):
                manifest_resources[path] = media_type
            if path.suffix.lower() in FONT_EXTENSIONS or item.attrib.get("media-type", "").startswith("font/"):
                font_files.add(path)
                font_items.setdefault(path, []).append(item)

        references = self._collect_resource_references(root, opf_path, manifest_resources)
        if references is None:
            return 0, 0
        missing_references = [
            path for path in references if path.suffix.lower() in FONT_EXTENSIONS and not path.exists()
        ]
        if missing_references:
            logger.warning(f"检测到缺失字体引用，跳过字体剪枝: {missing_references[0]}")
            return 0, 0

        removed_count = 0
        removed_bytes = 0
        for path in font_files:
            if path in encrypted_paths or not path.exists() or not path.is_relative_to(root):
                continue
            if path in references:
                continue
            size = path.stat().st_size
            path.unlink()
            for item in font_items.get(path, []):
                manifest.remove(item)
            removed_count += 1
            removed_bytes += size
            logger.info(f"删除未引用字体: {path.relative_to(root)} ({size} bytes)")
        return removed_count, removed_bytes

    def _prepare_fonts(self, content_opf_path: str) -> None:
        opf_path = Path(content_opf_path)
        encrypted_paths = self._encrypted_paths()
        if encrypted_paths is None:
            return
        try:
            tree = self._parse_xml(opf_path)
            root = tree.getroot()
            manifest = next(element for element in root.iter() if self._local_name(element.tag) == "manifest")
        except (ET.ParseError, OSError, StopIteration, ValueError) as error:
            logger.warning(f"无法解析 OPF，跳过字体处理: {error}")
            return

        items = [element for element in manifest if self._local_name(element.tag) == "item"]
        publication_root = Path(self.dir).resolve()
        for item in items:
            href = self._resource_path(item.attrib.get("href", ""))
            if href and not (opf_path.parent / href).resolve().is_relative_to(publication_root):
                logger.warning(f"manifest 路径越界，跳过设备字体与字体剪枝: {href}")
                return
        if any(
            item.attrib.get("media-type", "") in {"text/javascript", "application/javascript"}
            or self._resource_path(item.attrib.get("href", "")).lower().endswith(".js")
            or "scripted" in item.attrib.get("properties", "").split()
            for item in items
        ):
            logger.info("检测到脚本资源，跳过设备字体与字体剪枝")
            return
        if self._has_inline_script(opf_path, items):
            logger.info("检测到 inline script 或无法验证的 XHTML，跳过设备字体与字体剪枝")
            return
        if not encrypted_paths:
            self._apply_device_font_mode(opf_path, root, items)
        items = [element for element in manifest if self._local_name(element.tag) == "item"]
        removed_count, removed_bytes = self._prune_unreferenced_fonts(
            opf_path,
            manifest,
            items,
            encrypted_paths,
        )
        tree.write(opf_path, encoding="utf-8", xml_declaration=True)
        logger.info(f"字体剪枝完成: 删除 {removed_count} 个文件，节省 {removed_bytes} bytes")

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

    def _set_document_languages(self, content_opf_path: str) -> None:
        opf_path = Path(content_opf_path)
        publication_root = Path(self.dir).resolve()
        try:
            opf = self._parse_xml(opf_path)
            manifest = next(element for element in opf.getroot().iter() if self._local_name(element.tag) == "manifest")
            for item in manifest:
                if item.attrib.get("media-type", "") not in {"application/xhtml+xml", "text/html"}:
                    continue
                href = self._resource_path(item.attrib.get("href", ""))
                path = (opf_path.parent / href).resolve()
                if not path.is_relative_to(publication_root):
                    raise ValueError(f"manifest 路径越界: {href}")
                if not self._supports_document_encoding(path):
                    logger.info(f"跳过非 UTF-8 XHTML 的语言改写: {path.name}")
                    continue
                tree = self._parse_xml(path)
                root = tree.getroot()
                root.set("lang", self.language)
                root.set(f"{{{XML_NAMESPACE}}}lang", self.language)
                self._write_document_tree(tree, path)
        except (ET.ParseError, OSError, StopIteration, ValueError) as error:
            logger.warning(f"无法设置 XHTML 语言，保留原文档: {error}")

    def build(self) -> str:
        """
        将源目录下的所有文件打包成一个 EPUB 文件，并设置语言。

        Returns:
            生成的 EPUB 文件的路径。
        """
        if not os.path.exists(self.dir):
            logger.warning(f"源目录不存在：{self.dir}")
            return self.output

        content_opf_path = self._find_content_opf()

        if not content_opf_path:
            logger.warning("未找到任何 .opf 文件，跳过语言设置")
        else:
            # 修改 .opf 文件以设置语言
            self._modify_content_opf(content_opf_path)
            self._set_document_languages(content_opf_path)
            if self.optimize_fonts:
                self._prepare_fonts(content_opf_path)

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
