from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user.oauth.google import GoogleOAuth, OAuthError
from app.services.user.oauth.base import OAuthState
from app.services.user.oauth.service import OAuthService
from app.schemas.oauth.google import (
    GoogleLoginResponse,
    GoogleTokenResponse,
    GoogleUserInfo,
)
from app.db.session import get_db

router = APIRouter()

# 状态管理（实际项目中应该使用Redis）
oauth_states = {}


@router.get("/google/authorize")
async def google_authorize():
    """Google OAuth授权"""
    oauth = GoogleOAuth()
    state = OAuthState()
    oauth_states[state.state] = state

    auth_url = await oauth.get_authorization_url(state)
    return {"auth_url": auth_url}


@router.get("/google/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> GoogleLoginResponse:
    """Google OAuth回调"""
    # 验证state
    if state not in oauth_states:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_state", "description": "Invalid state parameter"},
        )

    try:
        # 处理授权
        oauth = GoogleOAuth()
        result = await oauth.verify_and_process(code=code, state=state)

        # 获取或创建用户
        oauth_service = OAuthService(db)
        user, created = await oauth_service.get_or_create_user(result["user_info"])

        # 构造令牌响应
        token_info = GoogleTokenResponse(
            access_token=result["access_token"],
            token_type="Bearer",
            refresh_token=result.get("refresh_token"),
            expires_in=result.get("expires_in"),
            scope=result.get("scope", ""),
            id_token=result.get("id_token"),
        )

        # 构造用户信息响应
        user_info = result["user_info"]
        google_user = GoogleUserInfo(
            provider=user_info.provider,
            provider_user_id=user_info.provider_user_id,
            email=user_info.email,
            username=user_info.username,
            avatar_url=user_info.avatar_url,
            raw_data=user_info.raw_data,
            # Google特有字段
            email_verified=user_info.raw_data.get("email_verified", False),
            given_name=user_info.raw_data.get("given_name"),
            family_name=user_info.raw_data.get("family_name"),
            locale=user_info.raw_data.get("locale"),
            hd=user_info.raw_data.get("hd"),
            picture=user_info.raw_data.get("picture"),
        )

        return GoogleLoginResponse(
            token=token_info,
            user_info=google_user,
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
