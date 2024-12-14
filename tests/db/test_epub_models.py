"""Test cases for EPUB database models."""

from datetime import datetime

import pytest

from app.db.models import TranslationProgress, TranslationStatus


class TestTranslationProgress:
    """Test cases for TranslationProgress model."""

    @pytest.mark.asyncio
    async def test_translation_progress_creation(self):
        """Test creating a translation progress record."""
        # 准备测试数据
        book_id = "test-book-1"
        total_chapters = {
            "chapter1.xhtml": {
                "id": "chapter1.xhtml",
                "type": "html",
                "name": "Chapter 1",
            },
            "chapter2.xhtml": {
                "id": "chapter2.xhtml",
                "type": "html",
                "name": "Chapter 2",
            },
        }

        # 创建记录
        progress = TranslationProgress(
            book_id=book_id,
            total_chapters=total_chapters,
            status=TranslationStatus.PENDING,
        )

        # 验证基本属性
        assert progress.book_id == book_id
        assert len(progress.total_chapters) == 2
        assert progress.status == TranslationStatus.PENDING
        assert len(progress.completed_chapters) == 0

        # 更新章节状态
        progress.update_chapter_status(
            chapter_id="chapter1.xhtml", status="completed", completed_at=datetime.now()
        )

        # 验证更新
        assert len(progress.completed_chapters) == 1
        assert progress.completed_chapters["chapter1.xhtml"]["status"] == "completed"
        assert progress.completed_chapters["chapter1.xhtml"]["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_translation_progress_update(self):
        """Test updating translation progress."""
        # 创建初始记录
        progress = TranslationProgress(
            book_id="test-book-2",
            total_chapters={
                "chapter1.xhtml": {
                    "id": "chapter1.xhtml",
                    "type": "html",
                    "name": "Chapter 1",
                }
            },
            status=TranslationStatus.PROCESSING,
        )

        # 更新章节状态
        progress.update_chapter_status(
            chapter_id="chapter1.xhtml", status="completed", completed_at=datetime.now()
        )

        # 验证更新
        assert len(progress.completed_chapters) == 1
        assert progress.completed_chapters["chapter1.xhtml"]["status"] == "completed"
        assert progress.completed_chapters["chapter1.xhtml"]["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_translation_progress_completion(self):
        """Test translation progress completion logic."""
        # 创建带多个章节的记录
        progress = TranslationProgress(
            book_id="test-book-3",
            total_chapters={
                "chapter1.xhtml": {
                    "id": "chapter1.xhtml",
                    "type": "html",
                    "name": "Chapter 1",
                },
                "chapter2.xhtml": {
                    "id": "chapter2.xhtml",
                    "type": "html",
                    "name": "Chapter 2",
                },
            },
            status=TranslationStatus.PROCESSING,
        )

        # 更新章节状态
        progress.update_chapter_status(
            chapter_id="chapter1.xhtml", status="completed", completed_at=datetime.now()
        )
        progress.update_chapter_status(
            chapter_id="chapter2.xhtml", status="completed", completed_at=datetime.now()
        )

        # 检查是否所有章节都完成
        all_completed = len(progress.completed_chapters) == len(progress.total_chapters)

        # 如果所有章节都完成，更新整体状态
        if all_completed:
            progress.status = TranslationStatus.COMPLETED

        # 验证完成状态
        assert progress.status == TranslationStatus.COMPLETED
        assert len(progress.completed_chapters) == 2

    @pytest.mark.asyncio
    async def test_translation_progress_validation(self):
        """Test validation of translation progress data."""
        # 测试无效的状态值
        with pytest.raises(ValueError):
            TranslationProgress(
                book_id="test-book-4",
                total_chapters={
                    "chapter1.xhtml": {
                        "id": "chapter1.xhtml",
                        "type": "html",
                        "name": "Chapter 1",
                    }
                },
                status="invalid_status",  # 无效的状态
            )

        # 测试无效的章节数据结构
        with pytest.raises(ValueError):
            TranslationProgress(
                book_id="test-book-5",
                total_chapters={
                    "chapter1.xhtml": {"id": "chapter1.xhtml", "type": "html"}
                },
                status=TranslationStatus.PENDING,
            )
            # 缺少必要的字段

    @pytest.mark.asyncio
    async def test_translation_progress_calculation(self):
        """Test progress calculation functionality."""
        # 创建带有混合状态章节的记录
        progress = TranslationProgress(
            book_id="test-book-6",
            total_chapters={
                "chapter1.xhtml": {
                    "id": "chapter1.xhtml",
                    "type": "html",
                    "name": "Chapter 1",
                },
                "chapter2.xhtml": {
                    "id": "chapter2.xhtml",
                    "type": "html",
                    "name": "Chapter 2",
                },
                "chapter3.xhtml": {
                    "id": "chapter3.xhtml",
                    "type": "html",
                    "name": "Chapter 3",
                },
            },
            status=TranslationStatus.PROCESSING,
        )

        # 更新章节状态
        progress.update_chapter_status(
            chapter_id="chapter1.xhtml", status="completed", completed_at=datetime.now()
        )
        progress.update_chapter_status(
            chapter_id="chapter2.xhtml", status="processing", completed_at=None
        )
        progress.update_chapter_status(
            chapter_id="chapter3.xhtml", status="pending", completed_at=None
        )

        # 计算进度
        total_chapters = len(progress.total_chapters)
        completed_chapters = len(progress.completed_chapters)
        completion_percentage = (completed_chapters / total_chapters) * 100

        # 验证计算结果
        assert total_chapters == 3
        assert completed_chapters == 1
        assert completion_percentage == pytest.approx(33.33, rel=0.01)

    @pytest.mark.asyncio
    async def test_translation_progress_status_transition(self):
        """Test status transition validation."""
        # 创建初始记录
        progress = TranslationProgress(
            book_id="test-book-7",
            total_chapters={
                "chapter1.xhtml": {
                    "id": "chapter1.xhtml",
                    "type": "html",
                    "name": "Chapter 1",
                }
            },
            status=TranslationStatus.PENDING,
        )

        # 测试有效的状态转换
        progress.status = TranslationStatus.PROCESSING
        assert progress.status == TranslationStatus.PROCESSING

        progress.status = TranslationStatus.COMPLETED
        assert progress.status == TranslationStatus.COMPLETED

        # 测试无效的状态转换
        with pytest.raises(ValueError):
            progress.status = "invalid"
