"""Base service class."""

from typing import Generic, TypeVar
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.repositories.base import BaseRepository

ModelType = TypeVar("ModelType", bound=Base)
RepoType = TypeVar("RepoType", bound=BaseRepository)

class BaseService(Generic[ModelType, RepoType]):
    """Base service implementing common business logic.
    
    Args:
        repository: Repository instance for database operations
    """
    
    def __init__(self, repository: RepoType):
        self.repository = repository

    async def get(self, id: int):
        """Get a single record by id."""
        return await self.repository.get(id)

    async def list(self, *, skip: int = 0, limit: int = 100):
        """Get a list of records."""
        return await self.repository.list(skip=skip, limit=limit)

    async def create(self, **kwargs):
        """Create a new record."""
        return await self.repository.create(**kwargs)

    async def update(self, id: int, **kwargs):
        """Update a record."""
        return await self.repository.update(id, **kwargs)

    async def delete(self, id: int):
        """Delete a record."""
        return await self.repository.delete(id)
