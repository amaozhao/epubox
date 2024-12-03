from typing import Optional, List
from pydantic import BaseModel, Field, EmailStr
from .base import (
    OAuthUserInfo,
    OAuthTokenResponse,
    OAuthLoginResponse,
    OAuthRefreshTokenRequest,
)


class GoogleUserInfo(OAuthUserInfo):
    """Google用户信息"""

    email: EmailStr = Field(..., description="邮箱")
    email_verified: bool = Field(False, description="邮箱是否验证")
    given_name: Optional[str] = Field(None, description="名")
    family_name: Optional[str] = Field(None, description="姓")
    locale: Optional[str] = Field(None, description="语言区域")
    hd: Optional[str] = Field(None, description="Hosted domain")
    picture: Optional[str] = Field(None, description="头像URL")


class GoogleTokenResponse(OAuthTokenResponse):
    """Google令牌响应"""

    id_token: Optional[str] = Field(None, description="ID令牌")
    scope: str = Field(..., description="授权范围")


class GoogleLoginResponse(OAuthLoginResponse):
    """Google登录响应"""

    token: GoogleTokenResponse = Field(..., description="令牌信息")
    user_info: GoogleUserInfo = Field(..., description="用户信息")
