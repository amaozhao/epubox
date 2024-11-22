"""File service for handling file operations."""
import os
import shutil
import aiofiles
import mimetypes
from typing import List, Tuple, Optional, Dict
from fastapi import HTTPException, status, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from app.models.file import FileMetadata, FileStatus
from app.services.file_metadata_service import FileMetadataService

class FileService:
    def __init__(self):
        self.temp_dir = settings.TEMP_UPLOAD_DIR
        self.processed_dir = settings.PROCESSED_FILES_DIR
        self.file_metadata_service = FileMetadataService()

        # Create directories if they don't exist
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.processed_dir, exist_ok=True)

    def validate_file_type(self, filename: str) -> bool:
        """Validate file extension."""
        # Allow any file type
        return True

    def validate_file_size(self, size: int) -> bool:
        """Validate file size."""
        return size <= settings.MAX_UPLOAD_SIZE

    async def save_upload_file(
        self,
        file: UploadFile,
        db: AsyncSession,
        user: User
    ) -> Tuple[str, str, FileMetadata]:
        """Save uploaded file and create metadata."""
        # Generate unique filename
        ext = os.path.splitext(file.filename)[1].lower()
        filename = f"{user.id}_{os.urandom(16).hex()}{ext}"
        temp_path = os.path.join(self.temp_dir, filename)
        final_path = os.path.join(self.processed_dir, filename)

        # Calculate file hash
        file_hash = await self.file_metadata_service.calculate_file_hash(file)

        # Check for duplicate file
        existing_file = await self.file_metadata_service.get_file_by_hash(db, file_hash)
        if existing_file:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="File already exists"
            )

        # Save file to temp directory
        content = await file.read()
        async with aiofiles.open(temp_path, 'wb') as f:
            await f.write(content)

        # Create file metadata
        file_metadata = await self.file_metadata_service.create_file_metadata(
            db=db,
            user=user,
            filename=filename,
            original_filename=file.filename,
            file_path=final_path,
            file_size=os.path.getsize(temp_path),
            mime_type=file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream",
            file_hash=file_hash,
            status=FileStatus.UPLOADED
        )

        return temp_path, final_path, file_metadata

    async def move_to_processed(self, temp_path: str, final_path: str) -> str:
        """Move file from temp to processed directory."""
        shutil.move(temp_path, final_path)
        return final_path

    async def get_file_info(
        self,
        db: AsyncSession,
        file_id: int,
        user: User
    ) -> Dict:
        """Get file information."""
        file_metadata = await self.file_metadata_service.get_file_metadata(db, file_id)
        if not file_metadata or file_metadata.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )
        
        return {
            "id": file_metadata.id,
            "filename": file_metadata.original_filename,
            "size": file_metadata.file_size,
            "mime_type": file_metadata.mime_type,
            "status": file_metadata.status,
            "created_at": file_metadata.created_at,
            "updated_at": file_metadata.updated_at
        }

    async def list_user_files(
        self,
        db: AsyncSession,
        user_id: int
    ) -> List[Dict]:
        """List all files for a user."""
        files = await self.file_metadata_service.get_user_files(db, user_id)
        return [
            {
                "id": f.id,
                "filename": f.original_filename,
                "size": f.file_size,
                "mime_type": f.mime_type,
                "status": f.status,
                "created_at": f.created_at,
                "updated_at": f.updated_at
            }
            for f in files
        ]

    async def delete_file(
        self,
        db: AsyncSession,
        file_id: int,
        user: User
    ) -> bool:
        """Delete file and its metadata."""
        file_metadata = await self.file_metadata_service.get_file_metadata(db, file_id)
        if not file_metadata or file_metadata.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )

        # Soft delete metadata
        await self.file_metadata_service.soft_delete_file(db, file_id)

        # Delete physical file
        try:
            if os.path.exists(file_metadata.file_path):
                os.remove(file_metadata.file_path)
            return True
        except OSError:
            return False
