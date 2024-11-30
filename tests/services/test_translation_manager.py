"""Test translation manager."""

import logging
import os
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.infrastructure.config import Settings
from src.services.epub_processor import EPUBProcessorError
from src.services.translation_manager import TranslationManager
from src.services.translator import TranslationError, TranslationProvider
from tests.infrastructure.models import Storage, StorageStatus, User


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
    settings = Settings()
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
def mock_user():
    """Create mock user."""
    return User(
        id="test-user-id",
        username="testuser",
        email="test@example.com",
        password_hash="dummy_hash",
    )


@pytest.fixture
def mock_session():
    """Create mock database session."""
    session = AsyncMock()
    session.flush = AsyncMock()
    return session


class MockEPUBProcessor:
    """Mock EPUB processor for testing."""

    def __init__(self, temp_dir: str = None):
        self.temp_dir = temp_dir
        self.extract_content = AsyncMock()
        self.save_translated_content = AsyncMock()
        self.cleanup = AsyncMock()


class MockTranslator:
    """Mock translator for testing."""

    def __init__(self):
        self.translate_batch = AsyncMock()


@pytest.fixture
def translation_manager(mock_settings, mock_session, monkeypatch):
    """Create translation manager instance with mocked components."""

    def mock_epub_processor(*args, **kwargs):
        return MockEPUBProcessor(*args, **kwargs)

    def mock_translator(*args, **kwargs):
        return MockTranslator()

    # Patch the creation of EPUBProcessor and translator
    monkeypatch.setattr(
        "src.services.translation_manager.EPUBProcessor", mock_epub_processor
    )
    monkeypatch.setattr(
        "src.services.translation_manager.create_translator", mock_translator
    )

    return TranslationManager(
        settings=mock_settings,
        session=mock_session,
        provider=TranslationProvider.OPENAI,
    )


@pytest.mark.asyncio
async def test_process_translation_success(translation_manager, test_dirs):
    """Test successful translation processing."""
    # Create test file
    test_file = Path(test_dirs["storage_dir"]) / "test.epub"
    test_file.write_text("Test EPUB content")

    # Create storage instance with session
    storage = Storage(
        id=str(uuid.uuid4()),
        upload_path="test.epub",
        original_filename="test.epub",
        status=StorageStatus.UPLOADED,
        user_id="test-user-id",
        file_size=1000,
        mime_type="application/epub+zip",
    )
    await translation_manager.session.add(storage)
    await translation_manager.session.flush()

    # Set up mock return values
    translation_manager.epub_processor.extract_content.return_value = [
        {
            "id": "chapter1",
            "file_name": "chapter1.xhtml",
            "media_type": "application/xhtml+xml",
            "content": "Test content",
        }
    ]
    translation_manager.epub_processor.save_translated_content.return_value = None
    translation_manager.translator.translate_batch.return_value = ["Translated content"]

    # Process translation
    result = await translation_manager.process_translation(
        storage=storage, source_lang="en", target_lang="fr"
    )

    await translation_manager.session.refresh(result)
    # Verify result
    assert str(result.status.value) == str(StorageStatus.COMPLETED.value)
    assert result.id == storage.id

    translation_manager.epub_processor.extract_content.assert_called_once()
    translation_manager.epub_processor.save_translated_content.assert_called_once()


@pytest.mark.asyncio
async def test_process_translation_error(translation_manager, test_dirs):
    """Test translation processing with error."""
    # Create test file first
    test_file = Path(test_dirs["storage_dir"]) / "test.epub"
    test_file.write_text("Test content")

    # Create storage instance with session
    storage = Storage(
        id=str(uuid.uuid4()),
        upload_path="test.epub",
        original_filename="test.epub",
        status=StorageStatus.UPLOADED,
        user_id="test-user-id",
        file_size=1000,
        mime_type="application/epub+zip",
    )
    await translation_manager.session.add(storage)
    await translation_manager.session.flush()

    # Mock extract_content to raise an error
    translation_manager.epub_processor.extract_content.side_effect = EPUBProcessorError(
        "Failed to extract content"
    )

    # Process translation
    result = await translation_manager.process_translation(
        storage=storage, source_lang="en", target_lang="fr"
    )

    await translation_manager.session.refresh(result)
    # Verify error handling
    assert str(result.status.value) == str(StorageStatus.FAILED.value)
    assert result.id == storage.id


@pytest.mark.asyncio
async def test_context_manager():
    """Test translation manager as context manager."""
    settings = Settings()
    settings.TEMP_PATH = Path(tempfile.mkdtemp())
    session = AsyncMock()

    async with TranslationManager(
        settings=settings, session=session, provider=TranslationProvider.OPENAI
    ) as manager:
        assert isinstance(manager, TranslationManager)
        assert manager.epub_processor is not None
        assert manager.translator is not None

    # Verify cleanup was called
    assert not settings.TEMP_PATH.exists()


@pytest.mark.asyncio
async def test_cleanup():
    """Test cleanup process."""
    settings = Settings()
    temp_dir = Path(tempfile.mkdtemp())
    settings.TEMP_PATH = temp_dir
    session = AsyncMock()

    manager = TranslationManager(
        settings=settings, session=session, provider=TranslationProvider.OPENAI
    )

    # Create test files
    test_files = []
    for i in range(3):
        test_file = temp_dir / f"test{i}.epub"
        test_file.write_text(f"Test content {i}")
        test_files.append(test_file)

    # Verify files exist
    for file in test_files:
        assert file.exists()

    # Run cleanup
    await manager.cleanup()

    # Verify directory is deleted
    assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_process_translation_success_with_context_manager(
    mock_settings, mock_session, test_dirs, monkeypatch
):
    """Test successful translation processing with context manager."""
    # Create test file
    test_file = Path(test_dirs["storage_dir"]) / "test.epub"
    test_file.write_text("Test EPUB content")

    def mock_epub_processor(*args, **kwargs):
        processor = MockEPUBProcessor(*args, **kwargs)
        processor.extract_content.return_value = [
            {
                "id": "chapter1",
                "file_name": "chapter1.xhtml",
                "media_type": "application/xhtml+xml",
                "content": "Test content",
            }
        ]
        return processor

    def mock_translator(*args, **kwargs):
        translator = MockTranslator()
        translator.translate_batch.return_value = ["Translated content"]
        return translator

    # Patch the creation of EPUBProcessor and translator
    monkeypatch.setattr(
        "src.services.translation_manager.EPUBProcessor", mock_epub_processor
    )
    monkeypatch.setattr(
        "src.services.translation_manager.create_translator", mock_translator
    )

    async with TranslationManager(
        settings=mock_settings,
        session=mock_session,
        provider=TranslationProvider.OPENAI,
    ) as manager:
        storage = Storage(
            id=str(uuid.uuid4()),
            upload_path="test.epub",
            original_filename="test.epub",
            status=StorageStatus.UPLOADED,
            user_id="test-user-id",
            file_size=1000,
            mime_type="application/epub+zip",
        )

        await mock_session.add(storage)
        await mock_session.flush()
        result = await manager.process_translation(storage, "en", "fr")
        await mock_session.refresh(result)
        assert str(result.status.value) == str(StorageStatus.COMPLETED.value)
        assert result.id == storage.id
        assert result.translation_path is not None
        assert result.error_message is None


@pytest.mark.asyncio
async def test_process_translation_empty_content(translation_manager, test_dirs):
    """Test translation processing with empty EPUB content."""
    # Create empty test file
    test_file = Path(test_dirs["storage_dir"]) / "empty.epub"
    test_file.write_text("")

    storage = Storage(
        id=str(uuid.uuid4()),
        upload_path="empty.epub",
        original_filename="empty.epub",
        status=StorageStatus.UPLOADED,
        user_id="test-user-id",
        file_size=0,
        mime_type="application/epub+zip",
    )
    await translation_manager.session.add(storage)
    await translation_manager.session.flush()

    # Configure mock to return empty content
    translation_manager.epub_processor.extract_content.return_value = []

    result = await translation_manager.process_translation(
        storage=storage, source_lang="en", target_lang="fr"
    )

    await translation_manager.session.refresh(result)
    assert str(result.status.value) == str(StorageStatus.FAILED.value)
    assert result.id == storage.id
    assert "No content found in EPUB file" in result.error_message


@pytest.mark.asyncio
async def test_process_translation_error_with_context_manager(
    mock_settings, mock_session, test_dirs, monkeypatch
):
    """Test error handling in translation processing with context manager."""
    # Create test file first
    test_file = Path(test_dirs["storage_dir"]) / "test.epub"
    test_file.write_text("Test content")

    def mock_epub_processor(*args, **kwargs):
        processor = MockEPUBProcessor(*args, **kwargs)
        processor.extract_content.side_effect = EPUBProcessorError(
            "Failed to extract content"
        )
        return processor

    def mock_translator(*args, **kwargs):
        return MockTranslator()

    # Patch the creation of EPUBProcessor and translator
    monkeypatch.setattr(
        "src.services.translation_manager.EPUBProcessor", mock_epub_processor
    )
    monkeypatch.setattr(
        "src.services.translation_manager.create_translator", mock_translator
    )

    async with TranslationManager(
        settings=mock_settings,
        session=mock_session,
        provider=TranslationProvider.OPENAI,
    ) as manager:
        storage = Storage(
            id=str(uuid.uuid4()),
            upload_path="test.epub",
            original_filename="test.epub",
            status=StorageStatus.UPLOADED,
            user_id="test-user-id",
            file_size=1000,
            mime_type="application/epub+zip",
        )

        await mock_session.add(storage)
        await mock_session.flush()
        result = await manager.process_translation(storage, "en", "fr")
        await mock_session.refresh(result)
        assert str(result.status.value) == str(StorageStatus.FAILED.value)
        assert result.id == storage.id
        assert result.error_message is not None
        assert "Failed to extract content" in result.error_message


@pytest.mark.asyncio
async def test_cleanup_with_context_manager(mock_settings, mock_session):
    """Test cleanup functionality with context manager."""
    session = mock_session
    settings = mock_settings
    async with TranslationManager(
        settings=settings, session=session, provider=TranslationProvider.OPENAI
    ) as manager:
        temp_dir = Path(manager.epub_processor.temp_dir)
        assert temp_dir.exists()

        # Create test files
        test_files = [temp_dir / f"test{i}.txt" for i in range(3)]
        for file in test_files:
            file.write_text("test content")
            assert file.exists()

    # Verify cleanup after context manager exit
    for file in test_files:
        assert not file.exists()
    assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_process_translation_file_not_found(translation_manager, test_dirs):
    """Test translation processing with missing file."""
    storage = Storage(
        id=str(uuid.uuid4()),
        upload_path="nonexistent.epub",
        original_filename="nonexistent.epub",
        status=StorageStatus.UPLOADED,
        user_id="test-user-id",
        file_size=1000,
        mime_type="application/epub+zip",
    )
    await translation_manager.session.add(storage)
    await translation_manager.session.flush()

    result = await translation_manager.process_translation(
        storage=storage, source_lang="en", target_lang="fr"
    )

    await translation_manager.session.refresh(result)
    assert str(result.status.value) == str(StorageStatus.FAILED.value)
    assert result.id == storage.id
    assert "not found" in result.error_message.lower()


@pytest.mark.asyncio
async def test_process_translation_translation_error(translation_manager, test_dirs):
    """Test translation processing with translation error."""
    # Create test file
    test_file = Path(test_dirs["storage_dir"]) / "test.epub"
    test_file.write_text("Test content")

    storage = Storage(
        id=str(uuid.uuid4()),
        upload_path="test.epub",
        original_filename="test.epub",
        status=StorageStatus.UPLOADED,
        user_id="test-user-id",
        file_size=1000,
        mime_type="application/epub+zip",
    )
    await translation_manager.session.add(storage)
    await translation_manager.session.flush()

    # Mock content extraction
    translation_manager.epub_processor.extract_content.return_value = [
        {
            "id": "chapter1",
            "file_name": "chapter1.xhtml",
            "media_type": "application/xhtml+xml",
            "content": "Test content",
        }
    ]

    # Mock translation error
    translation_manager.translator.translate_batch.side_effect = TranslationError(
        "Translation failed"
    )

    result = await translation_manager.process_translation(
        storage=storage, source_lang="en", target_lang="fr"
    )

    await translation_manager.session.refresh(result)
    assert str(result.status.value) == str(StorageStatus.FAILED.value)
    assert result.id == storage.id
    assert "translation failed" in result.error_message.lower()
