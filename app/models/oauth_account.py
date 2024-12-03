from datetime import datetime
from typing import Optional
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class OAuthAccount(Base):
    """OAuth账号模型"""

    __tablename__ = "oauth_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)

    # OAuth提供商信息
    provider: Mapped[str] = mapped_column(
        index=True, comment="OAuth提供商（github, google, wechat）"
    )
    provider_user_id: Mapped[str] = mapped_column(index=True, comment="提供商用户ID")
    provider_account_id: Mapped[Optional[str]] = mapped_column(
        nullable=True, comment="提供商账号ID（如微信的unionid）"
    )

    # OAuth令牌
    access_token: Mapped[str] = mapped_column(comment="访问令牌")
    refresh_token: Mapped[Optional[str]] = mapped_column(
        nullable=True, comment="刷新令牌"
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True, comment="令牌过期时间"
    )

    # 用户信息
    email: Mapped[Optional[str]] = mapped_column(
        nullable=True, index=True, comment="邮箱"
    )
    username: Mapped[Optional[str]] = mapped_column(nullable=True, comment="用户名")
    avatar_url: Mapped[Optional[str]] = mapped_column(nullable=True, comment="头像URL")

    # 关联的用户
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), comment="关联的用户ID"
    )
    user = relationship("User", back_populates="oauth_accounts")

    # 元数据
    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow, comment="更新时间"
    )

    class Config:
        orm_mode = True
