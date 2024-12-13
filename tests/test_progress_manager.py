"""Test progress manager functionality."""

from datetime import datetime
from typing import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import TranslationProgress, TranslationStatus
from app.progress.manager import ProgressManager

# 使用内存数据库进行测试
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def engine():
    """Create a test database engine."""
    engine = create_async_engine(TEST_DATABASE_URL)

    # 创建所有表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        # 清理数据库
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
async def session_maker(engine):
    """Create a test session maker."""
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    return async_session


@pytest.fixture
async def test_session(session_maker):
    """Create a test session."""
    async with session_maker() as session:
        yield session


@pytest.fixture
def test_book_id():
    return "test-book-123"


@pytest.fixture
def test_chapters():
    return {
        "1": {"id": "1", "type": "chapter", "name": "chapter1.xhtml"},
        "2": {"id": "2", "type": "chapter", "name": "chapter2.xhtml"},
    }


@pytest.fixture
def manager(monkeypatch, session_maker):
    """Create a test progress manager with mocked session."""

    async def mock_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_maker() as session:
            yield session

    # 替换 get_async_session
    import app.progress.manager

    monkeypatch.setattr(app.progress.manager, "get_async_session", mock_get_session)

    return ProgressManager()


async def test_create_progress(manager, test_book_id, test_chapters):
    """Test creating a new progress record."""
    progress = await manager.create_progress(test_book_id, test_chapters)

    assert progress is not None
    assert progress.book_id == test_book_id
    assert progress.total_chapters == test_chapters
    assert progress.completed_chapters == {}
    assert progress.status == TranslationStatus.PENDING
    assert progress.started_at is not None
    assert progress.completed_at is None


async def test_get_progress_nonexistent(manager, test_book_id):
    """Test getting progress for a nonexistent book."""
    progress = await manager.get_progress(test_book_id)
    assert progress is None


async def test_get_progress_existing(manager, test_book_id, test_chapters):
    """Test getting progress for an existing book."""
    created_progress = await manager.create_progress(test_book_id, test_chapters)

    fetched_progress = await manager.get_progress(test_book_id)
    assert fetched_progress is not None
    assert fetched_progress.book_id == created_progress.book_id
    assert fetched_progress.total_chapters == created_progress.total_chapters


async def test_update_chapter(manager, test_book_id, test_chapters):
    """Test updating chapter status."""
    await manager.create_progress(test_book_id, test_chapters)

    # Update first chapter
    await manager.update_chapter(test_book_id, "1")

    progress = await manager.get_progress(test_book_id)
    assert progress is not None
    assert "1" in progress.completed_chapters
    assert len(progress.completed_chapters) == 1
    assert progress.status == TranslationStatus.PROCESSING


async def test_complete_translation_flow(manager, test_book_id, test_chapters):
    """Test complete translation flow."""
    await manager.create_progress(test_book_id, test_chapters)

    # Start translation
    await manager.start_translation(test_book_id)
    progress = await manager.get_progress(test_book_id)
    assert progress.status == TranslationStatus.PROCESSING

    # Complete chapters
    for chapter_id in test_chapters.keys():
        await manager.update_chapter(test_book_id, chapter_id)

    # Mark as completed
    await manager.complete_translation(test_book_id)

    final_progress = await manager.get_progress(test_book_id)
    assert final_progress.status == TranslationStatus.COMPLETED
    assert final_progress.completed_at is not None
    assert len(final_progress.completed_chapters) == len(test_chapters)


async def test_progress_percentage(manager, test_book_id, test_chapters):
    """Test progress percentage calculation."""
    progress = await manager.create_progress(test_book_id, test_chapters)

    assert progress.get_progress_percentage() == 0

    await manager.update_chapter(test_book_id, "1")
    progress = await manager.get_progress(test_book_id)
    assert progress.get_progress_percentage() == 50

    await manager.update_chapter(test_book_id, "2")
    progress = await manager.get_progress(test_book_id)
    assert progress.get_progress_percentage() == 100
