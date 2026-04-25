import json
import os
import shutil
from datetime import datetime

from tqdm import tqdm

from engine.agents.verifier import EnglishResidualDecision, classify_untranslated_english_texts
from engine.agents.workflow import get_translator_workflow
from engine.core.logger import engine_logger as logger
from engine.epub import Builder, DomReplacer, Parser
from engine.schemas import Chunk, TranslationStatus
from engine.services.glossary import GlossaryExtractor, GlossaryLoader


# 翻译结果统计
class TranslationStats:
    def __init__(self):
        self.total = 0
        self.translated = 0
        self.untranslated = 0
        self.pending = 0
        self.failed = 0

    def record(self, status: TranslationStatus | None):
        if status is None:
            return
        self.total += 1
        if status in (
            TranslationStatus.TRANSLATED,
            TranslationStatus.ACCEPTED_AS_IS,
            TranslationStatus.COMPLETED,
        ):
            self.translated += 1
        elif status == TranslationStatus.TRANSLATION_FAILED:
            self.untranslated += 1
        elif status == TranslationStatus.PENDING:
            self.pending += 1
        elif status == TranslationStatus.WRITEBACK_FAILED:
            self.failed += 1

    def record_failure(self):
        self.failed += 1

    def __str__(self):
        return (
            f"翻译统计: 总数={self.total}, "
            f"成功={self.translated}, "
            f"失败={self.untranslated}, "
            f"跳过={self.pending}, "
            f"错误={self.failed}"
        )


class Orchestrator:
    """
    一个协调不同工作流和代理的编排器。
    """

    def __init__(self, *args, **kwargs):
        """
        初始化编排器实例。
        """
        super().__init__(*args, **kwargs)
        self.replacer = DomReplacer()
        self.final_untranslated_review_findings: list[dict] = []

    def _save_manual_translation_report(
        self,
        manual_chunks: list,
        output_path: str,
        suspect_english_terms: list[dict] | None = None,
    ):
        """保存手动翻译报告到 JSON 文件"""
        suspect_english_terms = suspect_english_terms or []
        report = {
            "generated_at": datetime.now().isoformat(),
            "total": len(manual_chunks),
            "suspect_total": len(suspect_english_terms),
            "suspect_english_terms": suspect_english_terms,
            "chunks": [
                {
                    "file": chunk["file"],
                    "chunk_name": chunk["chunk_name"],
                    "original": chunk["original"],
                    "path": chunk["path"],
                    "placeholder": chunk.get("placeholder", {}),
                    "status": chunk["status"],
                }
                for chunk in manual_chunks
            ],
        }

        report_path = os.path.join(os.path.dirname(output_path), "manual_translation_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"手动翻译报告已保存: {report_path}")
        return report_path

    def _load_manual_translations(self, report_path: str) -> dict:
        """加载手动翻译报告，返回 {chunk_name: translated_text}"""
        if not os.path.exists(report_path):
            return {}

        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}

        return {
            item["chunk_name"]: item.get("translated", "")
            for item in report.get("chunks", [])
            if item.get("translated")
        }

    def _apply_manual_translations_to_book(self, book, report_path: str) -> int:
        manual_translations = self._load_manual_translations(report_path)
        if not manual_translations:
            return 0

        applied = 0
        for item in book.items:
            if not item.chunks:
                continue
            for chunk in item.chunks:
                translated = manual_translations.get(chunk.name)
                if not translated:
                    continue
                chunk.translated = translated
                chunk.status = TranslationStatus.TRANSLATED
                applied += 1

        if applied:
            logger.info(f"已从手动翻译报告回填 {applied} 个 chunk，将直接进入校对/回写流程。")
        return applied

    def _should_process_chunk(self, chunk) -> bool:
        """判断 chunk 是否需要处理"""
        if chunk.status == TranslationStatus.WRITEBACK_FAILED and chunk.translated:
            chunk.status = TranslationStatus.TRANSLATED
            return True  # 回写失败后保留翻译结果，重跑时直接恢复到校对流程

        if chunk.status in (
            TranslationStatus.ACCEPTED_AS_IS,
            TranslationStatus.COMPLETED,
            TranslationStatus.WRITEBACK_FAILED,
        ):
            return False  # 已有最终结果或缺少可恢复翻译结果，跳过

        if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
            return True  # 已翻译但未校对，需要继续校对流程

        if (
            chunk.status == TranslationStatus.TRANSLATION_FAILED
            and chunk.translated
            and chunk.translated != chunk.original
        ):
            # 手动翻译后的 chunk：用户编辑了 translated 字段
            chunk.status = TranslationStatus.TRANSLATED
            return True  # 进入校对流程

        if chunk.status == TranslationStatus.TRANSLATION_FAILED:
            return True  # 翻译失败后允许重跑重试

        if chunk.status == TranslationStatus.PENDING:
            return True  # 待翻译

        return False  # 未知状态，安全跳过

    def _should_translate_chunk(self, chunk: Chunk) -> bool:
        """
        判断一个分块是否需要翻译。

        判断逻辑：
        1. ACCEPTED_AS_IS / COMPLETED / WRITEBACK_FAILED 视为当前阶段无需再次翻译。
        2. 其他状态继续进入翻译或后续处理流程。
        """
        if chunk.status in (
            TranslationStatus.ACCEPTED_AS_IS,
            TranslationStatus.COMPLETED,
            TranslationStatus.WRITEBACK_FAILED,
        ):
            return False
        return True

    def _has_incomplete_output(self, book) -> bool:
        for item in book.items:
            if not item.chunks:
                continue
            for chunk in item.chunks:
                if chunk.status in (
                    TranslationStatus.TRANSLATION_FAILED,
                    TranslationStatus.WRITEBACK_FAILED,
                ):
                    return True
        return False

    def _get_output_path(self, book) -> str:
        suffix = "-cn-incomplete.epub" if self._has_incomplete_output(book) else "-cn.epub"
        return os.path.join(os.path.dirname(book.path), f"{book.name}{suffix}")

    def _apply_final_untranslated_gate(self, book) -> int:
        """Scan all final chunk translations and fail chunks with residual natural English."""
        failed_count = 0
        self.final_untranslated_review_findings = []
        for item in book.items:
            if not item.chunks:
                continue
            for chunk in item.chunks:
                if chunk.status in (TranslationStatus.TRANSLATION_FAILED, TranslationStatus.WRITEBACK_FAILED):
                    continue
                if not chunk.translated:
                    continue
                findings = classify_untranslated_english_texts(
                    chunk.translated,
                    split_nav_payloads=chunk.chunk_mode == "nav_text",
                )
                fail_findings = [finding for finding in findings if finding.decision == EnglishResidualDecision.FAIL]
                review_findings = [
                    finding for finding in findings if finding.decision == EnglishResidualDecision.REVIEW
                ]
                for finding in review_findings:
                    self.final_untranslated_review_findings.append(
                        {
                            "file": item.id,
                            "chunk_name": chunk.name,
                            "path": item.path,
                            "text": finding.text[:240],
                            "reason": finding.reason,
                        }
                    )
                    logger.info(
                        f"Chunk '{chunk.name}' 最终整书扫描发现需人工复核的英文片段，不阻断输出: {finding.text[:160]}"
                    )
                if not fail_findings:
                    continue
                chunk.status = TranslationStatus.TRANSLATION_FAILED
                failed_count += 1
                logger.warning(
                    f"Chunk '{chunk.name}' 最终整书扫描发现疑似残留未翻译英文，已标记为 TRANSLATION_FAILED: "
                    f"{fail_findings[0].text[:160]}"
                )
        return failed_count

    async def translate_epub(self, epub_path: str, limit: int = 3000, target_language: str = "Chinese") -> str | None:
        """
        翻译给定路径的 EPUB 文件。

        Args:
            epub_path: 输入 EPUB 文件的路径。
            target_language: 目标翻译语言代码（例如 'zh'、'en'）。

        Returns:
            None
        """
        # 解析 EPUB 文件
        parser = Parser(limit=limit, path=epub_path)
        book = parser.parse()
        report_path = os.path.join(os.path.dirname(book.path), "manual_translation_report.json")
        self._apply_manual_translations_to_book(book, report_path)

        # 加载或自动生成术语表
        loader = GlossaryLoader()
        glossary = loader.load(epub_path)
        if not glossary:
            logger.info("术语表为空，自动生成中...")
            extractor = GlossaryExtractor()
            glossary = extractor.extract_from_epub(epub_path)
            logger.info(f"术语表生成完成，共提取 {len(glossary)} 个术语")

        # 统计翻译结果
        stats = TranslationStats()

        # 使用 tqdm 显示外部循环进度（按文件）
        for item in tqdm(book.items, desc="翻译 EPUB", unit="文件"):
            if not item.content:
                continue
            if not item.chunks:
                continue

            for _, chunk in enumerate(item.chunks):
                original_status = chunk.status

                # 在开始工作流前，判断该分块是否需要处理
                if not self._should_process_chunk(chunk):
                    stats.record(chunk.status)
                    continue

                recovering_writeback_failure = (
                    original_status == TranslationStatus.WRITEBACK_FAILED
                    and chunk.status == TranslationStatus.TRANSLATED
                )

                workflow = get_translator_workflow()
                try:
                    response = await workflow.arun(
                        input=chunk, additional_data={"glossary": glossary, "tag_map": item.placeholder}
                    )
                    if isinstance(response.content, Chunk):
                        chunk_index = item.chunks.index(chunk)
                        item.chunks[chunk_index] = response.content
                        chunk = response.content
                        if chunk.status is not None:
                            stats.record(chunk.status)

                        # 每翻译一个 chunk 立即保存，支持断点续传
                        parser.save_json(book)
                    else:
                        if recovering_writeback_failure:
                            chunk.status = TranslationStatus.WRITEBACK_FAILED
                        logger.error(f"Invalid response.content type for chunk {chunk.name}: {type(response.content)}")
                        if not recovering_writeback_failure:
                            stats.record_failure()
                except Exception as e:
                    if recovering_writeback_failure:
                        chunk.status = TranslationStatus.WRITEBACK_FAILED
                    logger.error(f"Unexpected error for chunk {chunk.name}: {str(e)}")
                    if not recovering_writeback_failure:
                        stats.record_failure()

            # 每处理完一个 item，保存进度（断点续传）
            parser.save_json(book)

        # 将原始解压目录复制到输出目录（保持原始目录不变）
        output_extract_dir = book.extract_path + "_output"
        writeback_state_changed = False
        if os.path.exists(book.extract_path):
            if os.path.exists(output_extract_dir):
                shutil.rmtree(output_extract_dir)
            shutil.copytree(book.extract_path, output_extract_dir)

            # 将翻译结果写入输出目录（原始目录永不修改）
            dom_replacer = DomReplacer()
            for item in book.items:
                if not item.chunks:
                    continue
                original_statuses = [chunk.status for chunk in item.chunks]
                translated_content = dom_replacer.restore(item)
                if [chunk.status for chunk in item.chunks] != original_statuses:
                    writeback_state_changed = True
                if translated_content:
                    rel_path = os.path.relpath(item.path, book.extract_path)
                    output_item_path = os.path.join(output_extract_dir, rel_path)
                    with open(output_item_path, "w", encoding="utf-8") as f:
                        f.write(translated_content)
        else:
            logger.warning(f"原始解压目录不存在，跳过写入: {book.extract_path}")

        if writeback_state_changed:
            parser.save_json(book)

        final_gate_failed_count = self._apply_final_untranslated_gate(book)
        if final_gate_failed_count:
            logger.warning(f"最终整书扫描拦截 {final_gate_failed_count} 个疑似漏译 chunk。")
            parser.save_json(book)

        manual_chunks = [
            {
                "file": item.id,
                "chunk_name": chunk.name,
                "original": chunk.original,
                "path": item.path,
                "placeholder": item.placeholder,
                "status": chunk.status.value if chunk.status else None,
            }
            for item in book.items
            if item.chunks
            for chunk in item.chunks
            if chunk.status
            in (
                TranslationStatus.TRANSLATION_FAILED,
                TranslationStatus.WRITEBACK_FAILED,
            )
        ]
        if manual_chunks or self.final_untranslated_review_findings:
            self._save_manual_translation_report(
                manual_chunks,
                book.path,
                self.final_untranslated_review_findings,
            )

        final_failed_count = stats.failed
        stats = TranslationStats()
        for item in book.items:
            if not item.chunks:
                continue
            for chunk in item.chunks:
                stats.record(chunk.status)
        stats.failed += final_failed_count

        # 打印最终统计
        logger.info(str(stats))

        if self._has_incomplete_output(book):
            logger.warning("检测到未完成或回写失败的 chunk，跳过 EPUB 打包。")
            return None

        # 从输出目录构建 EPUB
        output_path = self._get_output_path(book)
        builder = Builder(output_extract_dir, output_path)
        builder.build()
        return output_path
