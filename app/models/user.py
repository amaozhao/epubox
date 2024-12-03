from typing import Optional, List
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class User(Base):
    """用户模型"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), index=True, nullable=True)  # 邮箱可选且不要求唯一
    username = Column(
        String(255), unique=True, index=True, nullable=False
    )  # 用户名必须唯一
    avatar_url = Column(String(255), nullable=True)  # 头像URL可选

    # 状态标志
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)  # 邮箱是否验证
    is_superuser = Column(Boolean, default=False)

    # 关联关系
    oauth_accounts = relationship(
        "OAuthAccount", back_populates="user", cascade="all, delete-orphan"
    )

    class Config:
        orm_mode = True

    @property
    def is_authenticated(self) -> bool:
        """用户是否已认证"""
        return True  # OAuth用户总是已认证的

    @property
    def display_name(self) -> str:
        """显示名称"""
        if self.username:
            return self.username
        if self.email:
            return self.email.split("@")[0]
        return f"user_{self.id}"  # 如果都没有，返回 user_id
