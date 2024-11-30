from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

# 创建异步引擎
engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG, future=True)

# 创建异步会话工厂
async_session_factory = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

# 创建基类
Base = declarative_base()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """异步会话上下文管理器"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """初始化数据库"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """关闭数据库连接"""
    await engine.dispose()
