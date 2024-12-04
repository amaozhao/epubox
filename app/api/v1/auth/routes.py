from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from app.db.session import get_async_session
from app.services.user.oauth.base import OAuthState
from app.services.user.oauth.github import GitHubOAuth
from app.services.user.oauth.service import OAuthService
from app.services.user.auth import auth_backend, get_current_user
from app.core.config import settings
from app.schemas.user import User, UserCreate, Token, UserLogin
from app.services.user.auth import AuthService
# from app.models.user import User


router = APIRouter()

# OAuth提供商实例
oauth_providers = {
    "github": GitHubOAuth(),
}


@router.post("/register", response_model=User)
async def register(
    user_in: UserCreate,
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """注册新用户"""
    auth_service = AuthService(db)
    user = await auth_service.register_new_user(user_in)
    return user


@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_async_session),
) -> Token:
    """用户登录"""
    auth_service = AuthService(db)
    user = await auth_service.authenticate_user(
        form_data.username,
        form_data.password,
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 创建访问令牌
    access_token = auth_backend.create_access_token(
        data={"sub": user.username}
    )
    refresh_token = auth_backend.create_refresh_token(
        data={"sub": user.username}
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        refresh_token=refresh_token,
    )


@router.post("/refresh", response_model=Token)
async def refresh_token(
    refresh_token: str,
    db: AsyncSession = Depends(get_async_session),
) -> Token:
    """刷新访问令牌"""
    auth_service = AuthService(db)
    token = await auth_service.refresh_token(refresh_token)
    return token


@router.get("/me", response_model=User)
async def read_users_me(
    current_user: User = Depends(get_current_user),
) -> User:
    """获取当前用户信息"""
    return current_user


# 认证相关路由
auth_router = APIRouter()


@auth_router.post("/register", response_model=Token)
async def register(
    user_in: UserCreate,
    db: AsyncSession = Depends(get_async_session),
) -> Any:
    """注册新用户"""
    auth_service = AuthService(db)
    user = await auth_service.register_new_user(user_in)
    return await auth_service.create_token(user)


@auth_router.post("/token", response_model=Token)
async def login(
    user_in: UserLogin,
    db: AsyncSession = Depends(get_async_session),
) -> Any:
    """用户登录"""
    auth_service = AuthService(db)
    user = await auth_service.authenticate_user(
        user_in.username, user_in.password
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await auth_service.create_token(user)


@auth_router.post("/token/refresh", response_model=Token)
async def refresh_token(
    refresh_token: str,
    db: AsyncSession = Depends(get_async_session),
) -> Any:
    """刷新访问令牌"""
    auth_service = AuthService(db)
    return await auth_service.refresh_token(refresh_token)


@auth_router.get("/me", response_model=User)
async def read_current_user(
    current_user: User = Depends(get_current_user),
) -> Any:
    """获取当前用户信息"""
    return current_user


# OAuth 相关路由
@router.get("/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    request: Request,
    redirect_uri: str = Query(..., description="授权成功后的跳转地址"),
) -> dict:
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


@router.get("/oauth/{provider}/callback")
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

    # 获取OAuth用户信息
    oauth_provider = oauth_providers[provider]
    oauth_service = OAuthService(db)
    
    # 获取用户信息
    oauth_user = await oauth_provider.get_user_info(code)
    
    # 获取或创建用户
    user = await oauth_service.get_or_create_user(oauth_user)
    
    # 创建访问令牌
    access_token = auth_backend.create_access_token(
        data={"sub": user.username}
    )
    
    # 重定向到前端，带上访问令牌
    redirect_uri = "TODO: 从state中获取"  # TODO: 从Redis中获取state
    return RedirectResponse(
        f"{redirect_uri}?access_token={access_token}"
    )
