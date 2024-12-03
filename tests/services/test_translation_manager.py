"""Test translation manager."""

import logging
import os
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.config import Settings
from src.infrastructure.database import Base
from src.models.storage import Storage, StorageStatus
from src.models.users import User
from src.services.processors import epub
from src.services.translation.translation_manager import (
    TranslationManager,
    TranslationManagerError,
)
from src.services.translation.translator import TranslationError, TranslationProvider


@pytest.fixture
async def engine():
    """Create test database engine."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(engine):
    """Create database session."""
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


@pytest.fixture
async def test_user(db_session):
    """Create test user."""
    user = User(
        id=str(uuid.uuid4()),
        username="test_user",
        email="test@example.com",
        password_hash="test_password_hash",  # 添加必需的密码哈希
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def mock_storage(test_user, db_session):
    """Create mock storage."""
    storage = Storage(
        id=str(uuid.uuid4()),
        original_filename="test.epub",
        file_size=1024,
        mime_type="application/epub+zip",
        status=StorageStatus.UPLOADING,
        upload_path="test.epub",  # Changed to relative path
        user_id=test_user.id,
    )
    db_session.add(storage)
    await db_session.commit()
    return storage


@pytest.fixture
def test_dirs():
    """Create temporary test directories."""
    base_dir = tempfile.mkdtemp()
    storage_dir = os.path.join(base_dir, "data", "storage")
    temp_dir = os.path.join(base_dir, "data", "temp")
    translation_dir = os.path.join(base_dir, "data", "translations")
    log_dir = os.path.join(base_dir, "logs")

    # Create all required directories
    for path in [storage_dir, temp_dir, translation_dir, log_dir]:
        os.makedirs(path, exist_ok=True)

    dirs = {
        "base_dir": base_dir,
        "storage_dir": storage_dir,
        "temp_dir": temp_dir,
        "translation_dir": translation_dir,
        "log_dir": log_dir,
    }

    yield dirs

    # Cleanup
    shutil.rmtree(base_dir)


@pytest.fixture
def mock_settings(test_dirs):
    """Create mock settings."""
    settings = Mock(spec=Settings)
    settings.STORAGE_PATH = Path(test_dirs["storage_dir"])
    settings.TEMP_PATH = Path(test_dirs["temp_dir"])
    settings.TRANSLATION_DIR = Path(test_dirs["translation_dir"])
    settings.LOG_DIR = Path(test_dirs["log_dir"])
    settings.OPENAI_API_KEY = "test-openai-key"
    settings.GOOGLE_API_KEY = "test-google-key"
    settings.MISTRAL_API_KEY = "test-mistral-key"
    settings.DEEPL_API_KEY = "test-deepl-key"
    return settings


@pytest.fixture
def mock_session(db_session):
    """Create mock session."""
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def translation_manager(mock_settings, db_session):
    """Create translation manager."""
    return TranslationManager(
        settings=mock_settings,
        session=db_session,
        provider=TranslationProvider.OPENAI,
    )


@pytest.mark.asyncio
async def test_process_translation_success(
    translation_manager, mock_storage, test_dirs
):
    """Test successful translation processing."""
    # Create test file
    test_file = Path(test_dirs["storage_dir"]) / mock_storage.upload_path
    test_file.write_text("Test EPUB content")

    # Create async mock for EPUBProcessor
    mock_epub_processor = AsyncMock()
    mock_epub_processor.extract_content.return_value = [
        {
            "id": "chapter1",
            "file_name": "chapter1.xhtml",
            "media_type": "application/xhtml+xml",
            "content": "Test content",
        }
    ]
    mock_epub_processor.save_translated_content.return_value = None
    mock_epub_processor.cleanup.return_value = None

    with (
        patch(
            "src.services.processors.epub.EPUBProcessor",
            return_value=mock_epub_processor,
        ),
        patch.object(
            translation_manager.translator,
            "translate_batch",
            return_value=["translated content"],
        ),
    ):

        await translation_manager.process_translation(mock_storage, "en", "zh")

        await translation_manager.session.refresh(mock_storage)
        assert mock_storage.status == StorageStatus.COMPLETED
        assert mock_storage.error_message is None


@pytest.mark.asyncio
async def test_process_translation_error(translation_manager, mock_storage, test_dirs):
    """Test translation processing with error."""
    # Create test file
    test_file = Path(test_dirs["storage_dir"]) / mock_storage.upload_path
    test_file.write_text("Test EPUB content")

    with patch("src.services.processors.epub.EPUBProcessor") as mock_processor:
        mock_processor.return_value.extract_content.side_effect = (
            epub.EPUBProcessorError("Test error")
        )

        await translation_manager.process_translation(mock_storage, "en", "zh")

        await translation_manager.session.refresh(mock_storage)
        assert mock_storage.status == StorageStatus.FAILED
        assert (
            mock_storage.error_message == "Failed to extract EPUB content: Test error"
        )


@pytest.mark.asyncio
async def test_process_translation_empty_content(
    translation_manager, mock_storage, test_dirs
):
    """Test translation processing with empty content."""
    # Create test file
    test_file = Path(test_dirs["storage_dir"]) / mock_storage.upload_path
    test_file.write_text("Test EPUB content")

    # Create async mock for EPUBProcessor
    mock_epub_processor = AsyncMock()
    mock_epub_processor.extract_content.return_value = []
    mock_epub_processor.cleanup.return_value = None

    with patch(
        "src.services.processors.epub.EPUBProcessor", return_value=mock_epub_processor
    ):
        await translation_manager.process_translation(mock_storage, "en", "zh")

        await translation_manager.session.refresh(mock_storage)
        assert mock_storage.status == StorageStatus.FAILED
        assert "No content found" in mock_storage.error_message


@pytest.mark.asyncio
async def test_process_translation_file_not_found(
    translation_manager, mock_storage, test_dirs
):
    """Test translation processing with missing file."""
    mock_storage.upload_path = "nonexistent.epub"  # Changed file_path to upload_path
    await translation_manager.session.commit()

    await translation_manager.process_translation(mock_storage, "en", "zh")

    await translation_manager.session.refresh(mock_storage)
    assert mock_storage.status == StorageStatus.FAILED
    assert (
        "Upload file not found" in mock_storage.error_message
    )  # Updated error message check


@pytest.mark.asyncio
async def test_process_translation_translation_error(
    translation_manager, mock_storage, test_dirs
):
    """Test translation processing with translation error."""
    # Create test file
    test_file = Path(test_dirs["storage_dir"]) / mock_storage.upload_path
    test_file.write_text("Test EPUB content")

    # Create async mock for EPUBProcessor
    mock_epub_processor = AsyncMock()
    mock_epub_processor.extract_content.return_value = [
        {
            "id": "chapter1",
            "file_name": "chapter1.xhtml",
            "media_type": "application/xhtml+xml",
            "content": "Test content",
        }
    ]
    mock_epub_processor.cleanup.return_value = None

    with (
        patch(
            "src.services.processors.epub.EPUBProcessor",
            return_value=mock_epub_processor,
        ),
        patch.object(
            translation_manager.translator,
            "translate_batch",
            side_effect=TranslationError("Translation failed"),
        ),
    ):

        await translation_manager.process_translation(mock_storage, "en", "zh")

        await translation_manager.session.refresh(mock_storage)
        assert mock_storage.status == StorageStatus.FAILED
        assert (
            "Translation failed for chapter 1: Translation failed"
            in mock_storage.error_message
        )


@pytest.mark.asyncio
async def test_context_manager(translation_manager):
    """Test async context manager."""
    async with translation_manager as manager:
        assert isinstance(manager, TranslationManager)


@pytest.mark.asyncio
async def test_cleanup(translation_manager):
    """Test cleanup."""
    await translation_manager.cleanup()


@pytest.mark.asyncio
async def test_process_translation_success_with_context_manager(
    mock_settings, db_session, mock_storage, test_dirs
):
    """Test successful translation processing with context manager."""
    async with TranslationManager(
        settings=mock_settings,
        session=db_session,
        provider=TranslationProvider.OPENAI,
    ) as manager:
        # Create test file
        test_file = Path(test_dirs["storage_dir"]) / mock_storage.upload_path
        test_file.write_text("Test EPUB content")

        mock_processor = AsyncMock()
        mock_processor.extract_content.return_value = [
            {
                "id": "chapter1",
                "file_name": "chapter1.xhtml",
                "media_type": "application/xhtml+xml",
                "content": "Test content",
            }
        ]
        mock_processor.save_translated_content.return_value = None
        mock_processor.cleanup.return_value = None

        with (
            patch(
                "src.services.processors.epub.EPUBProcessor",
                return_value=mock_processor,
            ),
            patch.object(
                manager.translator,
                "translate_batch",
                return_value=["translated content"],
            ),
        ):

            await manager.process_translation(mock_storage, "en", "zh")

            await manager.session.refresh(mock_storage)
            assert mock_storage.status == StorageStatus.COMPLETED
            assert mock_storage.error_message is None


@pytest.mark.asyncio
async def test_process_translation_error_with_context_manager(
    mock_settings, db_session, mock_storage, test_dirs
):
    """Test translation processing with error using context manager."""
    async with TranslationManager(
        settings=mock_settings,
        session=db_session,
        provider=TranslationProvider.OPENAI,
    ) as manager:
        # Create test file
        test_file = Path(test_dirs["storage_dir"]) / mock_storage.upload_path
        test_file.write_text("Test EPUB content")

        # Create async mock for EPUBProcessor
        mock_processor = AsyncMock()
        mock_processor.extract_content.return_value = [
            {
                "id": "chapter1",
                "file_name": "chapter1.xhtml",
                "media_type": "application/xhtml+xml",
                "content": "Test content",
            }
        ]
        mock_processor.cleanup.return_value = None

        with (
            patch(
                "src.services.processors.epub.EPUBProcessor",
                return_value=mock_processor,
            ),
            patch.object(
                manager.translator,
                "translate_batch",
                side_effect=TranslationError("Translation failed"),
            ),
        ):

            await manager.process_translation(mock_storage, "en", "zh")

            await manager.session.refresh(mock_storage)
            assert mock_storage.status == StorageStatus.FAILED
            assert (
                "Translation failed for chapter 1: Translation failed"
                in mock_storage.error_message
            )


@pytest.mark.asyncio
async def test_cleanup_with_context_manager(mock_settings, db_session):
    """Test cleanup with context manager."""
    manager = None
    async with TranslationManager(
        settings=mock_settings,
        session=db_session,
        provider=TranslationProvider.OPENAI,
    ) as manager:
        pass
    assert manager is not None
