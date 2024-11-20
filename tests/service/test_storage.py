import pytest
from pathlib import Path
from sqlalchemy import select

from app.core.exceptions import FileAlreadyExistsError
from app.models.storage import EpubFile
from app.service.storage import StorageService


# Mark all tests in this module as async
pytestmark = pytest.mark.asyncio


@pytest.fixture
def test_file_path(tmp_path) -> Path:
    """Create a temporary file for testing."""
    test_dir = tmp_path / "test_create_file"
    test_dir.mkdir()
    test_file = test_dir / "test.epub"
    test_file.touch()
    return test_file


@pytest.fixture
async def storage_service(db):
    """Create a storage service instance for testing."""
    async with db as session:
        yield StorageService(session)


async def test_create_file(storage_service: StorageService, test_file_path: Path):
    """Test creating a new file record."""
    file = await storage_service.create_file(test_file_path)
    assert file.filename == test_file_path.name
    assert file.status == "pending"


async def test_create_duplicate_file(storage_service: StorageService, test_file_path: Path):
    """Test creating a duplicate file record."""
    await storage_service.create_file(test_file_path)
    with pytest.raises(FileAlreadyExistsError):
        await storage_service.create_file(test_file_path)


async def test_get_file(storage_service: StorageService, test_epub_file: EpubFile):
    """Test retrieving a file record."""
    file = await storage_service.get_file(test_epub_file.id)
    assert file is not None
    assert file.filename == test_epub_file.filename


async def test_get_nonexistent_file(storage_service: StorageService):
    """Test retrieving a nonexistent file record."""
    file = await storage_service.get_file(999)
    assert file is None


async def test_update_file_status(storage_service: StorageService, test_epub_file: EpubFile):
    """Test updating a file's status."""
    updated_file = await storage_service.update_file_status(test_epub_file.id, "processed")
    assert updated_file is not None
    assert updated_file.status == "processed"


async def test_update_nonexistent_file_status(storage_service: StorageService):
    """Test updating a nonexistent file's status."""
    updated_file = await storage_service.update_file_status(999, "processed")
    assert updated_file is None


async def test_list_files(storage_service: StorageService, test_epub_file: EpubFile):
    """Test listing all files."""
    files = await storage_service.list_files()
    assert len(files) > 0
    assert any(f.id == test_epub_file.id for f in files)


async def test_delete_file(storage_service: StorageService, test_epub_file: EpubFile):
    """Test deleting a file record."""
    success = await storage_service.delete_file(test_epub_file.id)
    assert success is True
    deleted_file = await storage_service.get_file(test_epub_file.id)
    assert deleted_file is None


async def test_delete_nonexistent_file(storage_service: StorageService):
    """Test deleting a nonexistent file record."""
    success = await storage_service.delete_file(999)
    assert success is False
