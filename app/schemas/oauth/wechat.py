from typing import Optional
from pydantic import BaseModel, Field
from .base import (
    OAuthUserInfo,
    OAuthTokenResponse,
    OAuthLoginResponse,
    OAuthRefreshTokenRequest,
)


class WeChatMiniLoginRequest(BaseModel):
    """小程序登录请求"""

    code: str = Field(..., description="小程序登录code")
    encrypted_data: str = Field(..., description="加密的用户信息")
    iv: str = Field(..., description="加密算法的初始向量")


class WeChatTokenResponse(OAuthTokenResponse):
    """微信令牌响应"""

    openid: str = Field(..., description="用户openid")
    unionid: Optional[str] = Field(None, description="用户统一标识")
    session_key: Optional[str] = Field(None, description="会话密钥（小程序）")


class WeChatUserInfo(OAuthUserInfo):
    """微信用户信息"""

    openid: str = Field(..., description="用户openid")
    unionid: Optional[str] = Field(None, description="用户统一标识")
    nickname: Optional[str] = Field(None, description="用户昵称")
    sex: Optional[int] = Field(None, description="性别，1为男性，2为女性，0为未知")
    province: Optional[str] = Field(None, description="省份")
    city: Optional[str] = Field(None, description="城市")
    country: Optional[str] = Field(None, description="国家")
    language: Optional[str] = Field(None, description="语言")


class WeChatLoginResponse(OAuthLoginResponse):
    """微信登录响应"""

    token: WeChatTokenResponse = Field(..., description="令牌信息")
    user_info: WeChatUserInfo = Field(..., description="用户信息")


class WeChatRefreshTokenRequest(OAuthRefreshTokenRequest):
    """刷新令牌请求，继承基类即可"""

    pass
