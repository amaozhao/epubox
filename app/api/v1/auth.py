from fastapi import APIRouter

from app.auth.backend import auth_backend, github_oauth_client, google_oauth_client
from app.auth.users import fastapi_users
from app.core.config import settings
from app.schemas.user import UserCreate, UserRead, UserUpdate

# 创建路由器
router = APIRouter()

# 添加认证相关路由
router.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/jwt",
    tags=["auth"],
)

# 添加注册相关路由
router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    tags=["auth"],
)

# 添加重置密码相关路由
router.include_router(
    fastapi_users.get_reset_password_router(),
    tags=["auth"],
)

# 添加验证相关路由
router.include_router(
    fastapi_users.get_verify_router(UserRead),
    tags=["auth"],
)

# 添加用户相关路由
router.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/users",
    tags=["users"],
)

# 如果配置了 OAuth 客户端，添加 OAuth 路由
if google_oauth_client:
    router.include_router(
        fastapi_users.get_oauth_router(
            google_oauth_client, auth_backend, settings.SECRET_KEY
        ),
        prefix="/google",
        tags=["auth"],
    )

if github_oauth_client:
    router.include_router(
        fastapi_users.get_oauth_router(
            github_oauth_client, auth_backend, settings.SECRET_KEY
        ),
        prefix="/github",
        tags=["auth"],
    )
