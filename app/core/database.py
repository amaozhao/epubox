from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from sqlalchemy import MetaData, DateTime
import datetime
from fastapi import Depends


class BaseModel(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_`%(constraint_name)s`",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )

    type_annotation_map = {
        datetime.datetime: DateTime(timezone=True),
    }


# class User(SQLAlchemyBaseUserTableUUID, BaseModel):
#     pass


# 创建异步数据库引擎
engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URL, echo=True)

# 创建异步Session类
async_session = async_sessionmaker(
    bind=engine,  # 使用异步引擎
    autoflush=False,
    expire_on_commit=False,  # 不要在提交时过期session数据
)


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.create_all)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


async def get_user_session(session: AsyncSession = Depends(get_async_session)):
    from app.models.user import User
    yield SQLAlchemyUserDatabase(session, User)
