from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Integer,
    String,
    ForeignKey,
    DateTime,
    Boolean,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.user import User


class OAuthAccount(Base):
    """OAuth账号关联"""

    __tablename__ = "oauth_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 提供商：github, google, wechat
    provider_user_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # 提供商用户ID

    # 令牌相关
    access_token: Mapped[Optional[str]] = mapped_column(String(255))
    refresh_token: Mapped[Optional[str]] = mapped_column(String(255))
    token_type: Mapped[Optional[str]] = mapped_column(String(50))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    scopes: Mapped[Optional[List[str]]] = mapped_column(JSON)  # 存储授权范围列表

    # 提供商特定字段
    provider_data: Mapped[Optional[dict]] = mapped_column(
        JSON
    )  # 存储提供商返回的原始数据

    # 关联关系
    user: Mapped[User] = relationship("User", back_populates="oauth_accounts")

    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_user_id", name="uq_oauth_account_provider_uid"
        ),
    )

    class Config:
        orm_mode = True

    def is_token_expired(self) -> bool:
        """检查令牌是否过期"""
        if not self.expires_at:
            return True
        return datetime.utcnow() > self.expires_at
