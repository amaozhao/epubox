from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from uuid import uuid4

from pydantic import BaseModel, Field

from app.models.user import User
from app.core.config import settings


class OAuthError(Exception):
    """OAuth错误"""

    def __init__(self, provider: str, error_code: str, description: str):
        self.provider = provider
        self.error_code = error_code
        self.error_description = description
        super().__init__(f"[{provider}] {error_code}: {description}")


class OAuthUserInfo(BaseModel):
    """OAuth用户信息"""

    provider: str
    provider_user_id: str
    username: str
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[str] = None
    expires_in: Optional[int] = None
    scopes: Optional[List[str]] = None
    raw_data: Dict[str, Any] = Field(default_factory=dict)

    async def to_user(self) -> User:
        """转换为系统用户"""
        # TODO: 实现用户转换逻辑
        # 1. 检查是否存在相同provider和provider_user_id的用户
        # 2. 如果存在，更新用户信息
        # 3. 如果不存在，创建新用户
        raise NotImplementedError()


class OAuthConfig(BaseModel):
    """OAuth配置"""

    client_id: str
    client_secret: str
    redirect_uri: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)

    def validate(self):
        """验证配置"""
        if not self.client_id or not self.client_secret:
            raise ValueError("client_id and client_secret are required")
        if not self.redirect_uri:
            raise ValueError("redirect_uri is required")
        if not self.scopes:
            raise ValueError("scopes is required")


class OAuthState(BaseModel):
    """OAuth状态"""

    state: str = Field(default_factory=lambda: secrets.token_urlsafe(16))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime = Field(
        default_factory=lambda: datetime.utcnow() + timedelta(minutes=10)
    )
    redirect_url: Optional[str] = None

    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at


class OAuthProvider(ABC):
    """OAuth提供商基类"""

    name: str

    def __init__(self, config: OAuthConfig):
        self.config = config
        self.config.validate()

    @abstractmethod
    async def get_authorization_url(self, state: OAuthState) -> str:
        """获取授权URL"""
        pass

    @abstractmethod
    async def get_access_token(self, code: str) -> Dict[str, Any]:
        """获取访问令牌"""
        pass

    @abstractmethod
    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        """获取用户信息"""
        pass

    @abstractmethod
    async def verify_and_process(self, code: str, state: str) -> User:
        """验证并处理OAuth回调"""
        pass
