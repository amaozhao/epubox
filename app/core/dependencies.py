"""FastAPI dependencies."""

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.user import UserRepository
from app.services.user import UserService

async def get_user_repository(
    session: AsyncSession = Depends(get_session)
) -> AsyncGenerator[UserRepository, None]:
    """Dependency for user repository."""
    yield UserRepository(session)

async def get_user_service(
    repo: UserRepository = Depends(get_user_repository)
) -> AsyncGenerator[UserService, None]:
    """Dependency for user service."""
    yield UserService(repo)

# Type aliases for dependencies
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]
DbSessionDep = Annotated[AsyncSession, Depends(get_session)]
