from typing import Optional

from fastapi_users.db import SQLAlchemyBaseUserTable
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base


class User(SQLAlchemyBaseUserTable[int], Base):
    __tablename__ = "user"

    username: Mapped[str] = mapped_column(
        String(length=50), unique=True, index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(
        String(length=320), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(length=1024), nullable=False)
    actived: Mapped[bool] = mapped_column(default=True, nullable=False)
    superuser: Mapped[bool] = mapped_column(default=False, nullable=False)
    verified: Mapped[bool] = mapped_column(default=False, nullable=False)

    # 可选的用户信息字段
    full_name: Mapped[Optional[str]] = mapped_column(String(length=100), nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(
        String(length=1024), nullable=True
    )

    # OAuth关联
    oauth_accounts: Mapped[list["OAuthAccount"]] = relationship(
        "OAuthAccount",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",  # 使用 selectin 策略来避免 N+1 问题
    )
