from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.storage import EpubFile
from app.core.exceptions import FileAlreadyExistsError


class StorageService:
    """Service for managing file storage."""

    def __init__(self, db: AsyncSession):
        """Initialize the storage service.

        Args:
            db: Database session
        """
        self.db = db

    async def create_file(self, file_path: Path) -> EpubFile:
        """Create a new file record.

        Args:
            file_path: Path to the uploaded file

        Returns:
            EpubFile: Created file record

        Raises:
            FileAlreadyExistsError: If file with same name already exists
        """
        # Check if file with same name exists
        query = select(EpubFile).filter(EpubFile.filename == file_path.name)
        result = await self.db.execute(query)
        if result.scalar_one_or_none() is not None:
            raise FileAlreadyExistsError(f"File {file_path.name} already exists")

        # Create new file record
        file = EpubFile(filename=file_path.name, status="pending")
        self.db.add(file)
        await self.db.commit()
        await self.db.refresh(file)
        return file

    async def get_file(self, file_id: int) -> EpubFile | None:
        """Get a file record by ID.

        Args:
            file_id: ID of the file to retrieve

        Returns:
            EpubFile or None: Retrieved file record or None if not found
        """
        query = select(EpubFile).filter(EpubFile.id == file_id)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def list_files(self) -> list[EpubFile]:
        """List all file records.

        Returns:
            List[EpubFile]: List of all file records
        """
        query = select(EpubFile)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def update_file_status(self, file_id: int, status: str) -> EpubFile | None:
        """Update a file's status.

        Args:
            file_id: ID of the file to update
            status: New status

        Returns:
            EpubFile or None: Updated file record or None if not found
        """
        file = await self.get_file(file_id)
        if file is None:
            return None

        file.status = status
        await self.db.commit()
        await self.db.refresh(file)
        return file

    async def delete_file(self, file_id: int) -> bool:
        """Delete a file record.

        Args:
            file_id: ID of the file to delete

        Returns:
            bool: True if file was deleted, False if not found
        """
        file = await self.get_file(file_id)
        if file is None:
            return False

        await self.db.delete(file)
        await self.db.commit()
        return True
