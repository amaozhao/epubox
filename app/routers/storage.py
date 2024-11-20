from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.core.database import get_async_session
from app.service.storage import StorageService
from app.models.storage import EpubFile
from app.schemas.storage import EpubFileResponse
from pathlib import Path
import shutil
import os
from app.core.config import settings
from app.core.exceptions import FileAlreadyExistsError

router = APIRouter()


async def get_storage_service(db: AsyncSession = Depends(get_async_session)) -> StorageService:
    """Dependency to get StorageService instance."""
    return StorageService(db)


@router.post("/upload", response_model=EpubFileResponse)
async def upload_file(
    file: UploadFile = File(...),
    storage_service: StorageService = Depends(get_storage_service)
):
    """Upload an EPUB file."""
    if not file.filename.endswith('.epub'):
        raise HTTPException(
            status_code=400,
            detail="Only EPUB files are allowed"
        )

    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / file.filename

    try:
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save file: {str(e)}"
        )

    try:
        epub_file = await storage_service.create_file(file_path)
        return EpubFileResponse(
            id=epub_file.id,
            filename=epub_file.filename,
            status=epub_file.status
        )
    except FileAlreadyExistsError:
        os.unlink(file_path)
        raise HTTPException(
            status_code=400,
            detail="File with this name already exists"
        )
    except Exception as e:
        os.unlink(file_path)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create file record: {str(e)}"
        )


@router.get("/files", response_model=List[EpubFileResponse])
async def list_files(
    storage_service: StorageService = Depends(get_storage_service)
):
    """List all EPUB files."""
    files = await storage_service.list_files()
    return [
        EpubFileResponse(
            id=file.id,
            filename=file.filename,
            status=file.status
        )
        for file in files
    ]


@router.get("/files/{file_id}", response_model=EpubFileResponse)
async def get_file(
    file_id: int,
    storage_service: StorageService = Depends(get_storage_service)
):
    """Get a specific EPUB file by ID."""
    file = await storage_service.get_file(file_id)
    if not file:
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )
    return EpubFileResponse(
        id=file.id,
        filename=file.filename,
        status=file.status
    )


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: int,
    storage_service: StorageService = Depends(get_storage_service)
):
    """Delete a specific EPUB file by ID."""
    success = await storage_service.delete_file(file_id)
    if not success:
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )
    return {"message": "File deleted successfully"}


@router.put("/files/{file_id}/status")
async def update_file_status(
    file_id: int,
    status: str,
    storage_service: StorageService = Depends(get_storage_service)
):
    """Update the status of a specific EPUB file."""
    file = await storage_service.update_file_status(file_id, status)
    if not file:
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )
    return {"message": "File status updated successfully"}
