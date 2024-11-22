from datetime import datetime
import hashlib
from typing import List, Optional
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.file import FileMetadata
from app.models.user import User

class FileMetadataService:
    def __init__(self):
        self.chunk_size = 8192  # 8KB chunks for file hashing

    async def create_file_metadata(
        self,
        db: AsyncSession,
        user: User,
        filename: str,
        original_filename: str,
        file_path: str,
        file_size: int,
        mime_type: str,
        file_hash: str,
        status: str = "pending"
    ) -> FileMetadata:
        """Create a new file metadata record."""
        file_metadata = FileMetadata(
            filename=filename,
            original_filename=original_filename,
            file_path=file_path,
            file_size=file_size,
            mime_type=mime_type,
            file_hash=file_hash,
            status=status,
            user_id=user.id
        )
        db.add(file_metadata)
        await db.commit()
        await db.refresh(file_metadata)
        return file_metadata

    async def get_file_metadata(
        self,
        db: AsyncSession,
        file_id: int
    ) -> Optional[FileMetadata]:
        """Get file metadata by ID."""
        result = await db.execute(
            select(FileMetadata).where(FileMetadata.id == file_id)
        )
        return result.scalar_one_or_none()

    async def get_user_files(
        self,
        db: AsyncSession,
        user_id: int,
        include_deleted: bool = False
    ) -> List[FileMetadata]:
        """Get all files for a user."""
        query = select(FileMetadata).where(FileMetadata.user_id == user_id)
        if not include_deleted:
            query = query.where(FileMetadata.deleted_at.is_(None))
        result = await db.execute(query)
        return result.scalars().all()

    async def update_file_status(
        self,
        db: AsyncSession,
        file_id: int,
        status: str
    ) -> Optional[FileMetadata]:
        """Update file status."""
        file_metadata = await self.get_file_metadata(db, file_id)
        if file_metadata:
            file_metadata.status = status
            file_metadata.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(file_metadata)
        return file_metadata

    async def soft_delete_file(
        self,
        db: AsyncSession,
        file_id: int
    ) -> bool:
        """Soft delete a file by setting deleted_at timestamp."""
        file_metadata = await self.get_file_metadata(db, file_id)
        if file_metadata:
            file_metadata.deleted_at = datetime.utcnow()
            await db.commit()
            return True
        return False

    async def get_file_by_hash(
        self,
        db: AsyncSession,
        file_hash: str
    ) -> Optional[FileMetadata]:
        """Get file metadata by hash."""
        result = await db.execute(
            select(FileMetadata).where(
                and_(
                    FileMetadata.file_hash == file_hash,
                    FileMetadata.deleted_at.is_(None)
                )
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def calculate_file_hash(file) -> str:
        """Calculate SHA-256 hash of file contents."""
        sha256_hash = hashlib.sha256()
        await file.seek(0)
        while chunk := await file.read(8192):
            sha256_hash.update(chunk)
        await file.seek(0)
        return sha256_hash.hexdigest()
