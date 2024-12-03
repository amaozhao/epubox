from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db.session import get_async_session
from app.core.auth import auth_backend, get_current_user
from app.schemas.auth import TokenResponse, RefreshTokenRequest
from app.core.config import settings
from app.models.user import User

router = APIRouter()
security = HTTPBearer()


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_async_session),
) -> TokenResponse:
    """刷新访问令牌"""
    try:
        # 验证刷新令牌并获取用户ID
        token_data = await auth_backend.get_strategy().read_token(
            request.refresh_token, refresh=True
        )
        if not token_data or not token_data.user_id:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        # 生成新的访问令牌和刷新令牌
        access_token = await auth_backend.get_strategy().write_token(token_data.user_id)
        refresh_token = await auth_backend.get_strategy().write_token(
            token_data.user_id, refresh=True
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_refresh_error", "description": str(e)},
        )


@router.post("/logout")
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: User = Depends(get_current_user),
    response: Response = None,
) -> dict:
    """用户登出

    - 清除客户端的认证令牌
    - 返回成功消息
    """
    # 设置响应头，通知客户端清除认证令牌
    if response:
        response.delete_cookie("Authorization")
        response.delete_cookie("RefreshToken")

    return {"message": "Successfully logged out"}
