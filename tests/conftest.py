import asyncio
import logging
import os
import shutil
import tempfile
from typing import AsyncGenerator, Generator, Optional

import pytest
from fastapi import Depends, FastAPI
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.auth import (
    UserManager,
    get_async_session,
    get_jwt_strategy,
    get_user_db,
    get_user_manager,
)
from app.core.config import settings
from app.core.logging import test_logger as logger
from app.db.base import Base
from app.main import app
from app.models.user import User
from app.schemas.user import UserCreate

# Test database URL
DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
)

async_session_maker = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_test_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Get test database session."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_test_user_db(
    session: AsyncSession = Depends(get_test_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    """Get test user database."""
    try:
        yield SQLAlchemyUserDatabase(session, User)
    except Exception as e:
        raise


async def get_test_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_test_user_db),
) -> AsyncGenerator[UserManager, None]:
    """Get test user manager."""
    try:
        yield UserManager(user_db)
    except Exception as e:
        raise


@pytest.fixture(autouse=True)
def setup_logging():
    """Configure logging for tests to only show warnings and above."""
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.WARNING)


@pytest.fixture
def sample_epub_path():
    """Create a temporary copy of test.epub for testing."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    source_epub = os.path.join(current_dir, "test.epub")

    # Create a temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix=".epub")
    os.close(temp_fd)

    # Copy the test file to the temporary location
    shutil.copy2(source_epub, temp_path)

    yield temp_path

    # Cleanup the temporary file after the test
    try:
        os.unlink(temp_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
async def create_test_database():
    """Create test database tables."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    except Exception as e:
        raise


@pytest.fixture
def app_fixture() -> FastAPI:
    """Get FastAPI application fixture."""
    return app


@pytest.fixture
async def async_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Get async HTTP client."""
    app.dependency_overrides[get_async_session] = get_test_async_session
    app.dependency_overrides[get_user_db] = get_test_user_db
    app.dependency_overrides[get_user_manager] = get_test_user_manager

    async with AsyncClient(app=app_fixture, base_url="http://test") as client:
        try:
            yield client
        except Exception as e:
            raise

    app.dependency_overrides = {}


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Database session fixture."""
    async for session in get_test_async_session():
        yield session


@pytest.fixture
async def test_user_token(db: AsyncSession) -> str:
    """Create a test user and return JWT token."""
    user_db = SQLAlchemyUserDatabase(db, User)
    user_manager = UserManager(user_db)

    user = await user_manager.create(
        UserCreate(
            email="test@example.com",
            username="testuser",
            password="testpassword123",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
    )

    # Generate JWT token
    strategy = get_jwt_strategy()
    token = await strategy.write_token(user)
    return token


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create event loop for tests."""
    try:
        loop = asyncio.get_event_loop_policy().new_event_loop()
        yield loop
        loop.close()
    except Exception as e:
        raise
