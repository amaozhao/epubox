"""Test cases for the translation API endpoints."""

import os
from pathlib import Path
from typing import Dict

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.epub_file import EPUBFile
from app.models.user import User
from app.schemas.epub_file import EPUBFileCreate


@pytest.fixture
async def test_epub_file(
    db: AsyncSession,
    test_user_token: str,
    sample_epub_path: str,
) -> EPUBFile:
    """Create a test EPUB file in the database."""
    file_path = sample_epub_path
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)

    obj_in = EPUBFileCreate(
        filename=file_name,
        original_filename=file_name,
        file_size=file_size,
    )

    db_obj = EPUBFile(
        filename=obj_in.filename,
        original_filename=obj_in.original_filename,
        file_size=obj_in.file_size,
        file_path=file_path,
        user_id=1,  # Test user ID is always 1
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj


@pytest.fixture
def auth_headers(test_user_token: str) -> Dict[str, str]:
    """Get authentication headers for test user."""
    return {"Authorization": f"Bearer {test_user_token}"}


async def test_submit_translation(
    async_client: AsyncClient,
    test_epub_file: EPUBFile,
    auth_headers: Dict[str, str],
) -> None:
    """Test submitting a translation request."""
    response = await async_client.post(
        f"{settings.API_V1_STR}/translation/translate",
        headers=auth_headers,
        json={
            "file_id": test_epub_file.id,
            "source_lang": "en",
            "target_lang": "es",
            "provider": "google",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert "message" in data
    assert data["message"] == "Translation task submitted successfully"


async def test_submit_translation_invalid_file(
    async_client: AsyncClient,
    auth_headers: Dict[str, str],
) -> None:
    """Test submitting a translation request with invalid file ID."""
    response = await async_client.post(
        f"{settings.API_V1_STR}/translation/translate",
        headers=auth_headers,
        json={
            "file_id": 999999,
            "source_lang": "en",
            "target_lang": "es",
            "provider": "google",
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "File not found"


async def test_submit_translation_unauthorized(
    async_client: AsyncClient,
    test_epub_file: EPUBFile,
) -> None:
    """Test submitting a translation request without authentication."""
    response = await async_client.post(
        f"{settings.API_V1_STR}/translation/translate",
        json={
            "file_id": test_epub_file.id,
            "source_lang": "en",
            "target_lang": "es",
            "provider": "google",
        },
    )
    assert response.status_code == 401


async def test_submit_translation_not_owner(
    async_client: AsyncClient,
    test_epub_file: EPUBFile,
    db: AsyncSession,
    test_other_user: User,
    test_other_user_token: str,
) -> None:
    """Test submitting a translation request for a file owned by another user."""
    headers = {"Authorization": f"Bearer {test_other_user_token}"}

    response = await async_client.post(
        f"{settings.API_V1_STR}/translation/translate",
        headers=headers,
        json={
            "file_id": test_epub_file.id,
            "source_lang": "en",
            "target_lang": "es",
            "provider": "google",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Not enough permissions"


async def test_get_translation_status(
    async_client: AsyncClient,
    test_epub_file: EPUBFile,
    auth_headers: Dict[str, str],
) -> None:
    """Test getting translation status."""
    # First submit a translation
    submit_response = await async_client.post(
        f"{settings.API_V1_STR}/translation/translate",
        headers=auth_headers,
        json={
            "file_id": test_epub_file.id,
            "source_lang": "en",
            "target_lang": "es",
            "provider": "google",
        },
    )
    task_id = submit_response.json()["task_id"]

    # Then check its status
    response = await async_client.get(
        f"{settings.API_V1_STR}/translation/status/{task_id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == task_id
    assert data["status"] in [
        "queued",
        "processing",
        "completed",
        "failed",
        "cancelled",
    ]


async def test_get_translation_status_invalid_task(
    async_client: AsyncClient,
    auth_headers: Dict[str, str],
) -> None:
    """Test getting translation status with invalid task ID."""
    response = await async_client.get(
        f"{settings.API_V1_STR}/translation/status/invalid_task_id",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Translation task not found"


async def test_cancel_translation(
    async_client: AsyncClient,
    test_epub_file: EPUBFile,
    auth_headers: Dict[str, str],
) -> None:
    """Test cancelling a translation task."""
    # First submit a translation
    submit_response = await async_client.post(
        f"{settings.API_V1_STR}/translation/translate",
        headers=auth_headers,
        json={
            "file_id": test_epub_file.id,
            "source_lang": "en",
            "target_lang": "es",
            "provider": "mock",  # Use mock provider for predictable behavior
        },
    )
    task_id = submit_response.json()["task_id"]

    # Then cancel it
    response = await async_client.delete(
        f"{settings.API_V1_STR}/translation/cancel/{task_id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == task_id
    # Accept either "cancelled" or "already failed" message since task may complete quickly
    assert data["message"] in [
        "Translation task cancelled successfully",
        "Translation task already failed",
    ]


async def test_cancel_translation_invalid_task(
    async_client: AsyncClient,
    auth_headers: Dict[str, str],
) -> None:
    """Test cancelling a translation task with invalid task ID."""
    response = await async_client.delete(
        f"{settings.API_V1_STR}/translation/cancel/invalid_task_id",
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert (
        response.json()["detail"] == "Translation task not found or already completed"
    )


async def test_translation_supported_languages(
    async_client: AsyncClient,
    auth_headers: Dict[str, str],
) -> None:
    """Test getting supported languages for translation."""
    response = await async_client.get(
        f"{settings.API_V1_STR}/translation/languages",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "source_languages" in data
    assert "target_languages" in data
    assert len(data["source_languages"]) > 0
    assert len(data["target_languages"]) > 0
    assert all(isinstance(lang, dict) for lang in data["source_languages"])
    assert all(isinstance(lang, dict) for lang in data["target_languages"])
    assert all("code" in lang and "name" in lang for lang in data["source_languages"])
    assert all("code" in lang and "name" in lang for lang in data["target_languages"])
