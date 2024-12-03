"""Test fixtures."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.infrastructure.database import Base
from tests.infrastructure.models import Storage, StorageStatus, User

SQLALCHEMY_TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="function")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def engine():
    """Create test database engine."""
    test_engine = create_async_engine(
        SQLALCHEMY_TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield test_engine

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await test_engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db(engine):
    """Get a test database session."""
    TestingSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    async with TestingSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
def setup_logging():
    """禁用测试时的日志输出"""
    # 设置根日志级别
    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 确保所有logger都设置为ERROR级别
    logging.getLogger().setLevel(logging.ERROR)
    for logger_name in logging.root.manager.loggerDict:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


@pytest_asyncio.fixture(scope="function")
async def test_user(db: AsyncSession):
    """Create a test user."""
    user = User(
        id="test-user-id",
        username="testuser",
        email="test@example.com",
        password_hash="dummy_hash",
        is_active=True,
        is_verified=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture(scope="function")
async def test_storage(db: AsyncSession, test_user: User):
    """Create a test storage record."""
    storage = Storage(
        id=str(uuid.uuid4()),
        original_filename="test.epub",
        file_size=1000,
        mime_type="application/epub+zip",
        status=StorageStatus.UPLOADED,
        upload_path="/path/to/test.epub",
        user_id=test_user.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(storage)
    await db.commit()
    await db.refresh(storage)
    return storage
