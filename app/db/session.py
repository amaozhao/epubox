"""Database session management."""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.core.config import settings
from app.db.base import Base

# Create async engine
engine = create_async_engine(
    settings.SQLALCHEMY_DATABASE_URL,
    echo=settings.DB_ECHO,
    connect_args={"check_same_thread": False}  # SQLite specific
)

# Create async session factory
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get async database session.
    
    Yields:
        AsyncSession: Database session for async operations
        
    Example:
        async with get_session() as session:
            result = await session.execute(query)
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db() -> None:
    """Initialize database schema.
    
    Creates all tables defined in models if they don't exist.
    Should be called when application starts.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)