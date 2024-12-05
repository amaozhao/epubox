import asyncio
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base

# 使用内存数据库进行测试
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# 创建异步引擎
engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=True,
    future=True,
    poolclass=StaticPool,  # 使用静态连接池以避免连接问题
    connect_args={
        "check_same_thread": False,  # SQLite 特定设置
    },
)

# 创建异步会话工厂
TestingSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@pytest.fixture(scope="session")
def event_loop():
    """创建一个事件循环"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def setup_database():
    """设置测试数据库"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # 启用外键约束
        await conn.execute(text("PRAGMA foreign_keys=ON"))
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session(setup_database) -> AsyncGenerator[AsyncSession, None]:
    """提供异步数据库会话"""
    async with TestingSessionLocal() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()


@pytest.fixture(scope="session")
def test_data_dir() -> Path:
    """获取测试数据目录."""
    return Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def sample_epub_path(test_data_dir) -> Path:
    """获取示例EPUB文件路径."""
    epub_path = test_data_dir / "sample.epub"
    if not epub_path.exists():
        pytest.skip("Sample EPUB file not found")
    return epub_path


@pytest.fixture(scope="function")
def temp_work_dir(tmp_path) -> Path:
    """创建临时工作目录."""
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    return work_dir
