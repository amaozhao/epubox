from enum import Enum
from typing import Optional

from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base
from .user import User


class OAuthProvider(str, Enum):
    GOOGLE = "google"
    GITHUB = "github"


class OAuthAccount(Base):
    __tablename__ = "oauth_account"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[OAuthProvider] = mapped_column(
        SQLAlchemyEnum(OAuthProvider), nullable=False
    )
    provider_user_id: Mapped[str] = mapped_column(String(length=320), nullable=False)
    provider_user_login: Mapped[Optional[str]] = mapped_column(
        String(length=320), nullable=True
    )  # GitHub username or Google email
    provider_user_email: Mapped[Optional[str]] = mapped_column(
        String(length=320), nullable=True
    )

    access_token: Mapped[str] = mapped_column(String(length=1024), nullable=False)
    expires_at: Mapped[Optional[int]] = mapped_column(nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(
        String(length=1024), nullable=True
    )
    token_type: Mapped[str] = mapped_column(
        String(length=50), default="bearer", nullable=False
    )
    scopes: Mapped[Optional[str]] = mapped_column(
        String(length=1024), nullable=True
    )  # 空格分隔的scope列表

    user: Mapped[User] = relationship("User", back_populates="oauth_accounts")

    __table_args__ = (
        # 确保每个用户在每个OAuth提供商只有一个账号
        UniqueConstraint("provider", "provider_user_id"),
    )
