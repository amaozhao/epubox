from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..infrastructure.database import Base


class User(Base):
    """用户模型"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    username: Mapped[str] = mapped_column(String(50), unique=True)  # 用户名
    email: Mapped[str] = mapped_column(String(255), unique=True)  # 电子邮件
    password_hash: Mapped[str] = mapped_column(String(255))  # 密码哈希

    # 用户状态
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # 关联存储
    storages: Mapped[List["Storage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.id})>"
