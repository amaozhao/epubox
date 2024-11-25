import os
from pathlib import Path
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
import zipfile

from app.core.config import settings
from app.models.epub_file import EPUBFile


@pytest.fixture
def test_epub_path() -> Path:
    """Get the test EPUB file path."""
    return Path("tests/test.epub")


async def test_upload_epub(
    async_client: AsyncClient,
    app_fixture: FastAPI,
    db: AsyncSession,
    test_user_token: str,
    test_epub_path: Path,
):
    """Test uploading an EPUB file."""
    headers = {"Authorization": f"Bearer {test_user_token}"}

    with open(test_epub_path, "rb") as f:
        files = {"file": ("test.epub", f, "application/epub+zip")}
        response = await async_client.post(
            f"{settings.API_V1_STR}/epub-files/upload/",
            headers=headers,
            files=files,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["filename"]
    assert data["original_filename"] == "test.epub"
    assert data["file_size"] > 0

    # Check file was saved
    assert os.path.exists(data["file_path"])


async def test_list_epub_files(
    async_client: AsyncClient,
    app_fixture: FastAPI,
    db: AsyncSession,
    test_user_token: str,
):
    """Test listing EPUB files."""
    headers = {"Authorization": f"Bearer {test_user_token}"}
    response = await async_client.get(
        f"{settings.API_V1_STR}/epub-files/",
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


async def test_get_epub_file(
    async_client: AsyncClient,
    app_fixture: FastAPI,
    db: AsyncSession,
    test_user_token: str,
    test_epub_path: Path,
):
    """Test getting a specific EPUB file."""
    # First upload a file
    headers = {"Authorization": f"Bearer {test_user_token}"}
    with open(test_epub_path, "rb") as f:
        files = {"file": ("test.epub", f, "application/epub+zip")}
        response = await async_client.post(
            f"{settings.API_V1_STR}/epub-files/upload/",
            headers=headers,
            files=files,
        )
    assert response.status_code == 200
    file_id = response.json()["id"]

    # Then get the file
    response = await async_client.get(
        f"{settings.API_V1_STR}/epub-files/{file_id}",
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == file_id


async def test_delete_epub_file(
    async_client: AsyncClient,
    app_fixture: FastAPI,
    db: AsyncSession,
    test_user_token: str,
    test_epub_path: Path,
):
    """Test deleting an EPUB file."""
    # First upload a file
    headers = {"Authorization": f"Bearer {test_user_token}"}
    with open(test_epub_path, "rb") as f:
        files = {"file": ("test.epub", f, "application/epub+zip")}
        response = await async_client.post(
            f"{settings.API_V1_STR}/epub-files/upload/",
            headers=headers,
            files=files,
        )
    assert response.status_code == 200
    file_id = response.json()["id"]
    file_path = response.json()["file_path"]

    # Then delete the file
    response = await async_client.delete(
        f"{settings.API_V1_STR}/epub-files/{file_id}",
        headers=headers,
    )
    assert response.status_code == 200

    # Verify file is deleted from disk
    assert not os.path.exists(file_path)

    # Verify file is deleted from database
    response = await async_client.get(
        f"{settings.API_V1_STR}/epub-files/{file_id}",
        headers=headers,
    )
    assert response.status_code == 404


async def test_unauthorized_access(
    async_client: AsyncClient,
    app_fixture: FastAPI,
    db: AsyncSession,
    test_epub_path: Path,
):
    """Test unauthorized access to EPUB endpoints."""
    # Try to list files without token
    response = await async_client.get(f"{settings.API_V1_STR}/epub-files/")
    assert response.status_code == 401

    # Try to upload without token
    with open(test_epub_path, "rb") as f:
        files = {"file": ("test.epub", f, "application/epub+zip")}
        response = await async_client.post(
            f"{settings.API_V1_STR}/epub-files/upload/",
            files=files,
        )
    assert response.status_code == 401


async def test_invalid_file_type(
    async_client: AsyncClient,
    app_fixture: FastAPI,
    db: AsyncSession,
    test_user_token: str,
):
    """Test uploading invalid file type."""
    headers = {"Authorization": f"Bearer {test_user_token}"}

    # Create a test text file
    test_file = Path("tests/data/test.txt")
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("This is not an EPUB file")

    try:
        with open(test_file, "rb") as f:
            files = {"file": ("test.txt", f, "text/plain")}
            response = await async_client.post(
                f"{settings.API_V1_STR}/epub-files/upload/",
                headers=headers,
                files=files,
            )

        assert response.status_code == 400
        assert "Invalid file type" in response.json()["detail"]

    finally:
        if test_file.exists():
            test_file.unlink()
