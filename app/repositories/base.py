"""Base repository pattern implementation."""

from typing import Generic, TypeVar, Type, Optional, List, Any
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.db.base import Base

ModelType = TypeVar("ModelType", bound=Base)

class BaseRepository(Generic[ModelType]):
    """Base repository implementing common database operations.
    
    Args:
        model_cls: SQLAlchemy model class
        session: AsyncSession for database operations
    """
    
    def __init__(self, model_cls: Type[ModelType], session: AsyncSession):
        self.model_cls = model_cls
        self.session = session

    async def get(self, id: Any) -> Optional[ModelType]:
        """Get a single record by id."""
        query = select(self.model_cls).where(self.model_cls.id == id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_attribute(self, attr: str, value: Any) -> Optional[ModelType]:
        """Get a single record by attribute."""
        query = select(self.model_cls).where(getattr(self.model_cls, attr) == value)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def list(self, *, skip: int = 0, limit: int = 100) -> List[ModelType]:
        """Get a list of records with pagination."""
        query = select(self.model_cls).offset(skip).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def create(self, **kwargs) -> ModelType:
        """Create a new record."""
        db_obj = self.model_cls(**kwargs)
        self.session.add(db_obj)
        await self.session.commit()
        await self.session.refresh(db_obj)
        return db_obj

    async def update(self, id: Any, **kwargs) -> Optional[ModelType]:
        """Update a record by id."""
        query = (
            update(self.model_cls)
            .where(self.model_cls.id == id)
            .values(**kwargs)
            .returning(self.model_cls)
        )
        result = await self.session.execute(query)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def delete(self, id: Any) -> bool:
        """Delete a record by id."""
        query = delete(self.model_cls).where(self.model_cls.id == id)
        result = await self.session.execute(query)
        await self.session.commit()
        return result.rowcount > 0

    def filter(self, *criterion) -> Select:
        """Create a filtered query."""
        return select(self.model_cls).filter(*criterion)
