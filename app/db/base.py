"""Database initialization."""

from app.core.logging import get_logger
from app.db.base_class import Base
from app.db.session import engine

# Create logger
logger = get_logger(__name__)

from app.models.epub_file import EPUBFile  # noqa

# Import all models here to ensure they are registered with SQLAlchemy
from app.models.user import User  # noqa


async def init_db():
    """Initialize the database by creating all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
