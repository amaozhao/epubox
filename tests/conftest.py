import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.database import BaseModel
from fastapi.testclient import TestClient
from app.main import app

# 创建一个内存数据库引擎，用于测试
SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

# 创建异步引擎和会话
engine = create_async_engine(SQLALCHEMY_DATABASE_URL, echo=True)
TestingSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


# 创建一个测试客户端
@pytest.fixture(scope="module")
async def client():
    # 在测试前创建数据库表
    async with engine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.create_all)

    # 创建一个测试客户端
    with TestClient(app) as client:
        yield client

    # 在测试后关闭数据库连接
    async with engine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.drop_all)


# 创建数据库依赖注入的测试数据库会话
@pytest.fixture(scope="module")
async def db():
    async with TestingSessionLocal() as session:
        yield session
