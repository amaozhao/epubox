from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.user.oauth.wechat import WeChatOAuth, WeChatAuthType, OAuthError
from app.services.user.oauth.base import OAuthState
from app.services.user.oauth.service import OAuthService
from app.schemas.oauth.wechat import (
    WeChatMiniLoginRequest,
    WeChatLoginResponse,
    WeChatTokenResponse,
    WeChatUserInfo,
)
from app.db.session import get_db
from app.core.auth import create_access_token, create_refresh_token

router = APIRouter()

# 状态管理（实际项目中应该使用Redis）
oauth_states = {}


@router.get("/wechat/qr/authorize")
async def wechat_qr_authorize():
    """微信扫码登录授权"""
    oauth = WeChatOAuth(auth_type=WeChatAuthType.QR_CODE)
    state = OAuthState()
    oauth_states[state.state] = state

    auth_url = await oauth.get_authorization_url(state)
    return {"auth_url": auth_url}


@router.get("/wechat/mp/authorize")
async def wechat_mp_authorize():
    """微信公众号网页授权"""
    oauth = WeChatOAuth(auth_type=WeChatAuthType.OFFICIAL)
    state = OAuthState()
    oauth_states[state.state] = state

    auth_url = await oauth.get_authorization_url(state)
    return {"auth_url": auth_url}


@router.post("/wechat/mini/login")
async def wechat_mini_login(
    request: WeChatMiniLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> WeChatLoginResponse:
    """微信小程序登录"""
    try:
        oauth = WeChatOAuth(auth_type=WeChatAuthType.MINI_PROGRAM)
        result = await oauth.verify_and_process(
            code=request.code, encrypted_data=request.encrypted_data, iv=request.iv
        )

        # 获取或创建用户
        oauth_service = OAuthService(db)
        user, created = await oauth_service.get_or_create_user(result["user_info"])

        # 生成系统访问令牌
        access_token = create_access_token(user.id)
        refresh_token = create_refresh_token(user.id)

        # 构造响应
        token_info = WeChatTokenResponse(
            access_token=access_token,  # 使用系统的访问令牌
            refresh_token=refresh_token,  # 使用系统的刷新令牌
            openid=result["user_info"].openid,
            unionid=result["user_info"].unionid,
        )

        user_info = WeChatUserInfo(
            openid=result["user_info"].openid,
            nickname=result["user_info"].username,
            avatar_url=result["user_info"].avatar_url,
            unionid=result["user_info"].unionid,
        )

        return WeChatLoginResponse(
            token=token_info,
            user_info=user_info,
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


@router.get("/wechat/callback")
async def wechat_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> WeChatLoginResponse:
    """微信授权回调"""
    # 验证state
    if state not in oauth_states:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_state", "description": "Invalid state parameter"},
        )

    try:
        # 根据state判断授权类型
        oauth_state = oauth_states[state]
        auth_type = WeChatAuthType.QR_CODE  # 默认扫码登录

        # 处理授权
        oauth = WeChatOAuth(auth_type=auth_type)
        result = await oauth.verify_and_process(code=code, state=state)

        # 获取或创建用户
        oauth_service = OAuthService(db)
        user, created = await oauth_service.get_or_create_user(result["user_info"])

        # 生成系统访问令牌
        access_token = create_access_token(user.id)
        refresh_token = create_refresh_token(user.id)

        # 构造响应
        token_info = WeChatTokenResponse(
            access_token=access_token,  # 使用系统的访问令牌
            refresh_token=refresh_token,  # 使用系统的刷新令牌
            openid=result["user_info"].openid,
            unionid=result["user_info"].unionid,
        )

        user_info = WeChatUserInfo(
            openid=result["user_info"].openid,
            nickname=result["user_info"].username,
            avatar_url=result["user_info"].avatar_url,
            unionid=result["user_info"].unionid,
            # 其他可选字段
            sex=result["user_info"].raw_data.get("sex"),
            province=result["user_info"].raw_data.get("province"),
            city=result["user_info"].raw_data.get("city"),
            country=result["user_info"].raw_data.get("country"),
            language=result["user_info"].raw_data.get("language"),
        )

        return WeChatLoginResponse(
            token=token_info,
            user_info=user_info,
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
