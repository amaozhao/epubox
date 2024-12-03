from typing import Optional, List
from pydantic import BaseModel, Field
from .base import (
    OAuthUserInfo,
    OAuthTokenResponse,
    OAuthLoginResponse,
    OAuthRefreshTokenRequest,
)


class GitHubUserInfo(OAuthUserInfo):
    """GitHub用户信息"""

    login: str = Field(..., description="GitHub用户名")
    name: Optional[str] = Field(None, description="姓名")
    company: Optional[str] = Field(None, description="公司")
    blog: Optional[str] = Field(None, description="博客")
    location: Optional[str] = Field(None, description="位置")
    bio: Optional[str] = Field(None, description="简介")
    public_repos: Optional[int] = Field(None, description="公开仓库数")
    followers: Optional[int] = Field(None, description="粉丝数")
    following: Optional[int] = Field(None, description="关注数")


class GitHubTokenResponse(OAuthTokenResponse):
    """GitHub令牌响应"""

    scope: Optional[List[str]] = Field(None, description="授权范围列表")


class GitHubLoginResponse(OAuthLoginResponse):
    """GitHub登录响应"""

    token: GitHubTokenResponse = Field(..., description="令牌信息")
    user_info: GitHubUserInfo = Field(..., description="用户信息")
