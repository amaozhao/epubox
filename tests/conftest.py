import pytest
import asyncio
from typing import AsyncGenerator
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from app.core.database import get_async_session, BaseModel
from app.main import app
from app.models.user import User
from app.models.storage import EpubFile


# This is a test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def test_app() -> FastAPI:
    """Create a test instance of the FastAPI application."""
    return app


@pytest.fixture(scope="session")
async def test_engine():
    """Create a test database engine."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=True,
        future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.drop_all)
        await conn.run_sync(BaseModel.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    session_maker = sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False
    )
    async with session_maker() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()


@pytest.fixture
async def client(test_app) -> AsyncGenerator[AsyncClient, None]:
    """Create a test client."""
    async with AsyncClient(app=test_app, base_url="http://test") as client:
        yield client


@pytest.fixture
async def test_user(db: AsyncSession) -> AsyncGenerator[User, None]:
    """Create a test user."""
    user = User(
        email="test@example.com",
        hashed_password="test_hashed_password",
        is_active=True
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    try:
        yield user
    finally:
        await db.delete(user)
        await db.commit()


@pytest.fixture
async def test_epub_file(db: AsyncSession) -> AsyncGenerator[EpubFile, None]:
    """Create a test epub file."""
    epub_file = EpubFile(
        filename="test.epub",
        status="pending"
    )
    db.add(epub_file)
    await db.commit()
    await db.refresh(epub_file)
    try:
        yield epub_file
    finally:
        await db.delete(epub_file)
        await db.commit()


@pytest.fixture
async def test_user_token(test_user: User) -> str:
    """Create a test verification token."""
    return "test_verification_token"


@pytest.fixture
async def authenticated_client(client: AsyncClient, test_user_token: str) -> AsyncGenerator[AsyncClient, None]:
    """Create an authenticated test client."""
    client.headers["Authorization"] = f"Bearer {test_user_token}"
    yield client
