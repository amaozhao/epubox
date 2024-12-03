from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class OAuthTokenResponse(BaseModel):
    """OAuth令牌响应基类"""

    access_token: str = Field(..., description="访问令牌")
    token_type: str = Field("Bearer", description="令牌类型")
    refresh_token: Optional[str] = Field(None, description="刷新令牌")
    expires_in: Optional[int] = Field(None, description="过期时间（秒）")
    scope: Optional[str] = Field(None, description="授权范围")


class OAuthUserInfo(BaseModel):
    """OAuth用户信息基类"""

    provider: str = Field(..., description="提供商")
    provider_user_id: str = Field(..., description="提供商用户ID")
    email: Optional[str] = Field(None, description="邮箱")
    username: Optional[str] = Field(None, description="用户名")
    name: Optional[str] = Field(None, description="姓名")
    avatar_url: Optional[str] = Field(None, description="头像URL")
    raw_data: Optional[Dict[str, Any]] = Field(None, description="原始数据")


class OAuthLoginResponse(BaseModel):
    """OAuth登录响应基类"""

    token: OAuthTokenResponse = Field(..., description="令牌信息")
    user_info: OAuthUserInfo = Field(..., description="用户信息")


class OAuthRefreshTokenRequest(BaseModel):
    """刷新令牌请求基类"""

    refresh_token: str = Field(..., description="刷新令牌")
