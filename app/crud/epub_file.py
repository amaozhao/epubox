from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.base import CRUDBase
from app.models.epub_file import EPUBFile
from app.schemas.epub_file import EPUBFileCreate, EPUBFileUpdate


class CRUDEPUBFile(CRUDBase[EPUBFile, EPUBFileCreate, EPUBFileUpdate]):
    async def create_with_owner(
        self,
        db: AsyncSession,
        *,
        obj_in: EPUBFileCreate,
        owner_id: int,
        file_path: str,
    ) -> EPUBFile:
        db_obj = EPUBFile(
            filename=obj_in.filename,
            original_filename=obj_in.original_filename,
            file_size=obj_in.file_size,
            file_path=file_path,
            user_id=owner_id,
        )
        db.add(db_obj)
        await db.commit()
        await db.refresh(db_obj)
        return db_obj

    async def get_multi_by_owner(
        self,
        db: AsyncSession,
        *,
        owner_id: int,
        skip: int = 0,
        limit: int = 100,
    ) -> List[EPUBFile]:
        query = (
            select(EPUBFile)
            .filter(EPUBFile.user_id == owner_id)
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(query)
        return result.scalars().all()

    def is_owner(self, obj: EPUBFile, user_id: int) -> bool:
        """Check if the user is the owner of the file.

        Args:
            obj: The EPUB file object to check.
            user_id: The ID of the user to check ownership for.

        Returns:
            bool: True if the user is the owner, False otherwise.
        """
        return obj.user_id == user_id


epub_file = CRUDEPUBFile(EPUBFile)
