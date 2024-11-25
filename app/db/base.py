from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

# Create logger
logger = get_logger(__name__)

# Create the declarative base
Base = declarative_base()

# Import all models here to ensure they are registered with SQLAlchemy
from app.models.user import User  # noqa
from app.models.epub_file import EPUBFile  # noqa

# Create the async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=getattr(settings, "DEBUG_MODE", False),  # Safely get debug mode setting
    connect_args={"check_same_thread": False},  # Required for SQLite
)

# Async session factory
async_session_factory = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session."""
    async with async_session_factory() as session:
        logger.debug("database_session_started")
        try:
            yield session
            await session.commit()
            logger.debug("database_session_committed")
        except Exception as e:
            await session.rollback()
            logger.error("database_session_rollback", error=str(e))
            raise
        finally:
            await session.close()
            logger.debug("database_session_closed")


# Create all tables
async def init_db():
    """Initialize the database by creating all tables."""
    try:
        logger.info("database_initialization_started")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database_initialization_completed")
    except Exception as e:
        logger.error("database_initialization_failed", error=str(e))
        raise
