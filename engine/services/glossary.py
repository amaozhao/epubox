import json
import logging
import os
import re
import sys
from typing import Any, Dict, cast

import ebooklib
import nltk
from bs4 import BeautifulSoup
from ebooklib import epub
from nltk import pos_tag, word_tokenize
from nltk.chunk import RegexpParser
from sklearn.feature_extraction.text import TfidfVectorizer

from .utils import CODE_KEYWORDS, GENERIC_BLACKLIST, INVALID_CHARS


class GlossaryExtractor:
    """
    [终极版] 一个用于从EPUB文件中提取技术术语并生成术语表草案的类。
    采用【NLTK名词短语分块 + TF-IDF权重排序 + 终极过滤】的方案。
    """

    # --- 过滤规则配置 (终极版) ---
    MIN_WORDS = 2
    MAX_WORDS = 5

    # 1. 任何包含这些字符的术语都将被视为代码或垃圾
    INVALID_CHARS = INVALID_CHARS

    # 2. 任何包含这些编程关键词的术语，都将被视为代码
    CODE_KEYWORDS = CODE_KEYWORDS

    # 3. 最终、最全面的通用/示例词黑名单
    GENERIC_BLACKLIST = GENERIC_BLACKLIST

    def _ensure_nltk_data(self):
        """确保运行所需的所有NLTK数据包都已下载。"""
        required_packages = ["punkt", "stopwords", "averaged_perceptron_tagger"]
        try:
            for package in required_packages:
                nltk.download(package, quiet=True, raise_on_error=True)
        except Exception as e:
            logging.error(f"❌ 下载NLTK核心数据包时失败: {e}", exc_info=False)
            logging.error("   请检查您的网络连接。程序无法继续。")
            sys.exit(1)

    def __init__(self):
        logging.info("正在初始化 GlossaryExtractor (方案: 终极版)...")
        self._ensure_nltk_data()
        stop_words_set = set(nltk.corpus.stopwords.words("english"))
        self.forbidden_words = stop_words_set.union(self.GENERIC_BLACKLIST)
        self.grammar = r"NP: {<JJ.*>*<NN.*>+}"
        self.chunker = RegexpParser(self.grammar)
        logging.info("✅ Extractor 初始化成功。")

    def _is_valid_term(self, term: str) -> bool:
        """对单个候选术语进行多层强力规则校验。"""
        words = term.split()
        if not (self.MIN_WORDS <= len(words) <= self.MAX_WORDS):
            return False
        if any(char in term for char in self.INVALID_CHARS):
            return False
        term_words_set = set(words)
        if not term_words_set.isdisjoint(self.CODE_KEYWORDS):
            return False
        if not re.search(r"[a-zA-Z]", term) or not term[0].isalnum() or not term[-1].isalnum():
            return False
        if term_words_set.issubset(self.forbidden_words):
            return False
        return True

    def _extract_text_from_epub(self, epub_path: str) -> list[str] | None:
        """从EPUB中提取并净化文本内容。"""
        logging.info("📖 [阶段1/3] 正在从EPUB中解析和净化HTML内容...")
        try:
            book = epub.read_epub(epub_path)
        except Exception as e:
            logging.error(f"❌ 读取EPUB文件 '{epub_path}' 失败: {e}")
            return None
        documents, tags_to_ignore = (
            [],
            ["pre", "code", "figure", "figcaption", "table", "script", "style", "a", "header", "footer", "nav"],
        )
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            for bad_tag in soup(tags_to_ignore):
                bad_tag.decompose()
            text_content = re.sub(r"\s+", " ", soup.get_text(separator=" "))
            if text_content and len(text_content.strip()) > 100:
                documents.append(text_content.strip())
        if not documents:
            logging.warning("⚠️ 未能从EPUB中提取任何有效的文本内容。")
            return None
        logging.info(f"   成功解析并清理了 {len(documents)} 个文档（章节）。")
        return documents

    def _get_all_unique_terms(self, documents: list[str], top_n: int = 200) -> list[str]:
        """结合名词短语提取、TF-IDF评分和强力规则过滤。"""
        logging.info("🔍 [阶段2/3] 正在提取候选短语并进行强力过滤...")
        full_text = " ".join(documents)
        sentences = nltk.sent_tokenize(full_text)
        if not sentences:
            return []
        candidate_phrases = set()
        for sentence in sentences:
            try:
                tokens = word_tokenize(sentence)
                pos_tags = pos_tag(tokens)
                chunked = cast(Any, self.chunker.parse(pos_tags))
                for subtree in chunked.subtrees(filter=lambda t: t.label() == "NP"):
                    phrase = " ".join(word for word, tag in subtree.leaves()).lower()
                    if self._is_valid_term(phrase):
                        candidate_phrases.add(phrase)
            except Exception:
                continue
        if not candidate_phrases:
            logging.warning("   ⚠️ 强力过滤后未能找到任何有效的候选术语。")
            return []
        logging.info(f"   过滤后剩下 {len(candidate_phrases)} 个高质量候选。")
        logging.info("🔍 [阶段3/3] 正在为高质量候选计算TF-IDF权重并排序...")
        vectorizer = TfidfVectorizer(vocabulary=list(candidate_phrases), stop_words="english")
        try:
            tfidf_matrix = cast(Any, vectorizer.fit_transform(sentences))
        except ValueError:
            return []
        scores = cast(Any, tfidf_matrix.mean(axis=0)).A1
        scored_phrases = {phrase: score for phrase, score in zip(vectorizer.get_feature_names_out(), scores)}
        sorted_phrases = sorted(scored_phrases.items(), key=lambda x: x[1], reverse=True)
        final_terms = [term.title() for term, score in sorted_phrases[:top_n]]
        logging.info(f"   最终结果: 筛选出 Top {len(final_terms)} 个高质量术语。")
        return sorted(final_terms)

    def run(self, epub_path: str, output_path: str | None = None):
        """执行完整的术语提取流程。"""
        logging.info(f"🚀 开始处理EPUB文件: {os.path.basename(epub_path)}")
        if output_path is None:
            glossary_dir = "glossary"
            os.makedirs(glossary_dir, exist_ok=True)
            base_name = os.path.splitext(os.path.basename(epub_path))[0]
            output_path = os.path.join(glossary_dir, f"{base_name}.json")
            logging.info(f"ℹ️ 未指定输出路径，将自动使用: '{output_path}'")
        else:
            output_dir = os.path.dirname(str(output_path))
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
        documents = self._extract_text_from_epub(epub_path)
        if not documents:
            return
        all_terms = self._get_all_unique_terms(documents)
        if not all_terms:
            logging.warning("⚠️ 未能生成候选术语列表。流程终止。")
            return
        existing_glossary = {}
        if os.path.exists(output_path):
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    existing_glossary = json.load(f)
                logging.info(f"🔄 检测到已存在的术语表，共 {len(existing_glossary)} 条。")
            except (json.JSONDecodeError, IOError) as e:
                logging.error(f"❌ 加载现有术语表 '{output_path}' 失败: {e}。")
        final_glossary = {term: "" for term in all_terms}
        restored_count = 0
        for term, translation in existing_glossary.items():
            if term.title() in final_glossary and translation:
                final_glossary[term.title()] = translation
                restored_count += 1
        if restored_count > 0:
            logging.info(f"   恢复了 {restored_count} 条已有的翻译。")
        existing_keys = {k.title() for k in existing_glossary.keys()}
        final_keys = set(final_glossary.keys())
        added_count = len(final_keys - existing_keys)
        removed_count = len(existing_keys - final_keys)
        if added_count == 0 and removed_count == 0 and restored_count == len(final_glossary):
            logging.info("✅ 术语表内容与书中提取结果一致，无需更新。")
        else:
            if added_count > 0:
                logging.info(f"➕ 新增了 {added_count} 个全新的术语。")
            if removed_count > 0:
                logging.info(f"🗑️ 术语表已清理，移除了 {removed_count} 个过时的术语。")
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(dict(sorted(final_glossary.items())), f, indent=4, ensure_ascii=False)
            logging.info(f"\n✅ 术语表已成功更新并保存到: '{output_path}'")
        except IOError as e:
            logging.error(f"❌ 无法写入文件 '{output_path}': {e}")

    def extract_from_epub(self, epub_path: str) -> Dict[str, str]:
        """从EPUB提取术语并返回字典（不保存文件）"""
        documents = self._extract_text_from_epub(epub_path)
        if not documents:
            return {}
        all_terms = self._get_all_unique_terms(documents)
        if not all_terms:
            return {}
        return {term: "" for term in all_terms}


class GlossaryLoader:
    def __init__(self, glossary_dir: str = "glossary"):
        self.glossary_dir = glossary_dir
        if not os.path.isdir(glossary_dir):
            logging.warning(f"⚠️ 术语表目录 '{glossary_dir}' 不存在。")

    def load(self, epub_path: str) -> Dict[str, str]:
        base_name = os.path.splitext(os.path.basename(epub_path))[0]
        glossary_path = os.path.join(self.glossary_dir, f"{base_name}.json")
        logging.info(f"📂 正在尝试从 '{glossary_path}' 加载术语表...")
        if not os.path.exists(glossary_path):
            logging.warning("   术语表文件不存在。将使用空术语表。")
            return {}
        try:
            with open(glossary_path, "r", encoding="utf-8") as f:
                glossary_data = json.load(f)
            translated_glossary = {k: v for k, v in glossary_data.items() if v}
            logging.info(f"   成功加载并过滤了 {len(translated_glossary)} 条已翻译的术语。")
            return translated_glossary
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"❌ 加载或解析术语表 '{glossary_path}' 失败: {e}")
            return {}
