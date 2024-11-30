"""Test configuration and fixtures."""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.test_infrastructure.database import Base
from tests.test_infrastructure.models import Storage, StorageStatus, User

# Create test database
SQLALCHEMY_TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
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


@pytest_asyncio.fixture
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
        # 开始事务
        async with session.begin():
            # 清理表数据
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            yield session


@pytest_asyncio.fixture
async def test_user(db: AsyncSession) -> User:
    """创建测试用户"""
    user = User(
        id=str(uuid.uuid4()),
        username="testuser",
        email="test@example.com",
        password_hash="hashed_password",
        is_active=True,
        is_verified=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_storage(db: AsyncSession, test_user: User) -> Storage:
    """创建测试存储记录"""
    storage = Storage(
        id=str(uuid.uuid4()),
        user_id=test_user.id,
        original_filename="test.epub",
        file_size=1024,
        mime_type="application/epub+zip",
        upload_path="/tmp/test.epub",
        status=StorageStatus.UPLOADED,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(storage)
    await db.flush()
    await db.refresh(storage)
    return storage
