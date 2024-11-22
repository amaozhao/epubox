"""Test configuration and fixtures."""
import sys
from unittest.mock import Mock
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from contextlib import asynccontextmanager
from httpx import AsyncClient

# Import and apply mocks before importing app
from .mocks import mock_aioredis, mock_httpx
sys.modules['aioredis'] = mock_aioredis
sys.modules['httpx'] = mock_httpx

# Now we can safely import app
from app.main import app
from app.db.base import Base
from app.db.session import get_session

# Create test database
SQLALCHEMY_TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

@pytest_asyncio.fixture(scope="session")
async def db_engine():
    """Create test database engine."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture
async def db():
    """Get a test database session."""
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    async with TestingSessionLocal() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()

async def get_test_session():
    """Override the get_session dependency."""
    async with TestingSessionLocal() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()

@pytest_asyncio.fixture
async def client(db):
    """Get a test client with overridden session dependency."""
    app.dependency_overrides[get_session] = get_test_session
    async with AsyncClient(app=app, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()

@pytest_asyncio.fixture
async def test_user():
    """Get test user data."""
    return {
        "email": "test@example.com",
        "password": "password123"
    }
