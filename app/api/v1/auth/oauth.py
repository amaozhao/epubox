from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_session
from app.services.user.oauth.base import OAuthState
from app.services.user.oauth.github import GitHubOAuth
from app.core.config import settings
from app.services.user.oauth.service import OAuthService
from app.services.user.auth import auth_backend


router = APIRouter(prefix="/oauth")

# OAuth提供商实例
oauth_providers = {
    "github": GitHubOAuth(),
}


@router.get("/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    request: Request,
    redirect_uri: str = Query(..., description="授权成功后的跳转地址"),
) -> Dict[str, str]:
    """获取OAuth授权URL"""
    if provider not in oauth_providers:
        raise HTTPException(status_code=400, detail="不支持的OAuth提供商")

    # 创建OAuth状态
    state = OAuthState(redirect_url=redirect_uri)

    # TODO: 保存state到Redis

    # 获取授权URL
    oauth_provider = oauth_providers[provider]
    auth_url = await oauth_provider.get_authorization_url(state)

    return {"auth_url": auth_url}


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_async_session),
) -> RedirectResponse:
    """处理OAuth回调"""
    if provider not in oauth_providers:
        raise HTTPException(status_code=400, detail="不支持的OAuth提供商")

    # TODO: 从Redis获取并验证state

    oauth_provider = oauth_providers[provider]
    try:
        # 验证并获取用户信息
        result = await oauth_provider.verify_and_process(code, state)

        # 关联或创建用户
        oauth_service = OAuthService(db)
        user, created = await oauth_service.get_or_create_user(result["user_info"])

        # 生成token
        token = await auth_backend.get_strategy().write_token(user)

        # 重定向到前端，带上token
        redirect_url = f"{settings.FRONTEND_URL}/oauth/callback?token={token}"
        return RedirectResponse(url=redirect_url)

    except Exception as e:
        error_redirect = f"{settings.FRONTEND_URL}/oauth/error?error={str(e)}"
        return RedirectResponse(url=error_redirect)
