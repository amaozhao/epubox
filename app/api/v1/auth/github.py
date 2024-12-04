from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user.oauth.github import GitHubOAuth, OAuthError
from app.services.user.oauth.base import OAuthState
from app.services.user.oauth.service import OAuthService
from app.schemas.oauth.github import (
    GitHubLoginResponse,
    GitHubTokenResponse,
    GitHubUserInfo,
)
from app.db.session import get_async_session

router = APIRouter()

# 状态管理（实际项目中应该使用Redis）
oauth_states = {}


@router.get("/github/authorize")
async def github_authorize():
    """GitHub OAuth授权"""
    oauth = GitHubOAuth()
    state = OAuthState()
    oauth_states[state.state] = state

    auth_url = await oauth.get_authorization_url(state)
    return {"auth_url": auth_url}


@router.get("/github/callback")
async def github_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_async_session),
) -> GitHubLoginResponse:
    """GitHub OAuth回调"""
    # 验证state
    if state not in oauth_states:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_state", "description": "Invalid state parameter"},
        )

    try:
        # 处理授权
        oauth = GitHubOAuth()
        result = await oauth.verify_and_process(code=code, state=state)

        # 获取或创建用户
        oauth_service = OAuthService(db)
        user, created = await oauth_service.get_or_create_user(result["user_info"])

        # 构造令牌响应
        token_info = GitHubTokenResponse(
            access_token=result["access_token"],
            token_type="Bearer",
            scope=result.get("scope", "").split(","),
        )

        # 构造用户信息响应
        user_info = result["user_info"]
        github_user = GitHubUserInfo(
            provider=user_info.provider,
            provider_user_id=user_info.provider_user_id,
            email=user_info.email,
            username=user_info.username,
            avatar_url=user_info.avatar_url,
            raw_data=user_info.raw_data,
            # GitHub特有字段
            login=user_info.raw_data["login"],
            name=user_info.raw_data.get("name"),
            company=user_info.raw_data.get("company"),
            blog=user_info.raw_data.get("blog"),
            location=user_info.raw_data.get("location"),
            bio=user_info.raw_data.get("bio"),
            public_repos=user_info.raw_data.get("public_repos"),
            followers=user_info.raw_data.get("followers"),
            following=user_info.raw_data.get("following"),
        )

        return GitHubLoginResponse(
            token=token_info,
            user_info=github_user,
        )

    except OAuthError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": e.error, "description": e.description},
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "description": str(e)},
        )
    finally:
        # 清理state
        oauth_states.pop(state, None)
