"""File upload and management endpoints."""

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.db.session import get_session
from app.models.user import User
from app.services.file_service import FileService
from app.docs.schemas import FileUploadResponse, FileListResponse
from app.api.v1.users import get_current_active_user
from app.core.config import settings
from app.middleware.rate_limit import rate_limit
import logging

router = APIRouter()
file_service = FileService()
logger = logging.getLogger(__name__)

class FileResponse(FileResponse):
    """Custom FileResponse with additional headers for better download handling."""
    def __init__(self, path: str, filename: str, **kwargs):
        super().__init__(
            path,
            filename=filename,
            media_type="application/octet-stream",
            **kwargs
        )
        self.headers["Content-Disposition"] = f'attachment; filename="{filename}"'

@router.post("/upload", status_code=status.HTTP_201_CREATED)
@rate_limit(action="upload", check_file_size=True)
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_session)
):
    """Upload a file with validation and rate limiting."""
    try:
        logger.info(f"Attempting to upload file: {file.filename} by user: {current_user.email}")
        
        # Save file and create metadata
        logger.debug("Saving file to temporary storage")
        temp_path, final_path, file_metadata = await file_service.save_upload_file(file, db, current_user)
        logger.info(f"File saved successfully at: {final_path}")
        
        # Move to processed directory
        processed_path = await file_service.move_to_processed(temp_path, final_path)
        
        # Get file info
        file_info = await file_service.get_file_info(db, file_metadata.id, current_user)
        
        return {
            "message": "File uploaded successfully",
            "file_info": file_info
        }
        
    except Exception as e:
        logger.error(f"Error uploading file: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error uploading file: {str(e)}"
        )

@router.get("/list")
@rate_limit(action="list", check_file_size=False)
async def list_files(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_session)
) -> List[dict]:
    """List all files for the current user."""
    try:
        logger.info(f"Attempting to list files for user: {current_user.email}")
        return await file_service.list_user_files(db, current_user.id)
    except Exception as e:
        logger.error(f"Error listing files: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing files: {str(e)}"
        )

@router.get("/info/{file_id}")
@rate_limit(action="info", check_file_size=False)
async def get_file_info(
    file_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_session)
):
    """Get information about a specific file."""
    try:
        logger.info(f"Attempting to get file info for file ID: {file_id} by user: {current_user.email}")
        return await file_service.get_file_by_id(db, file_id, current_user.id)
    except Exception as e:
        logger.error(f"Error getting file info: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting file info: {str(e)}"
        )

@router.delete("/{file_id}")
@rate_limit(action="delete", check_file_size=False)
async def delete_file(
    file_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_session)
):
    """Delete a specific file."""
    try:
        logger.info(f"Attempting to delete file ID: {file_id} by user: {current_user.email}")
        await file_service.delete_file(db, file_id, current_user.id)
        return {"message": "File deleted successfully"}
    except Exception as e:
        logger.error(f"Error deleting file: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting file: {str(e)}"
        )

@router.get("/download/{file_id}")
@rate_limit(action="download", check_file_size=False)
async def download_file(
    file_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_session)
):
    """Download a specific file."""
    try:
        logger.info(f"Attempting to download file ID: {file_id} by user: {current_user.email}")
        file_path, filename = await file_service.get_file_path(db, file_id, current_user.id)
        return FileResponse(file_path, filename=filename)
    except Exception as e:
        logger.error(f"Error downloading file: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error downloading file: {str(e)}"
        )
