from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_users import FastAPIUsers

from app.api.v1.auth import auth_backend, get_user_manager
from app.core.config import settings
from app.db.models import User
from app.schemas.user import UserCreate, UserRead, UserUpdate

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)

# Set up CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

fastapi_users = FastAPIUsers[User, int](
    get_user_manager,
    [auth_backend],
)

# Add FastAPI Users routes
app.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix=f"{settings.API_V1_STR}/auth/jwt",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix=f"{settings.API_V1_STR}/auth",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_reset_password_router(),
    prefix=f"{settings.API_V1_STR}/auth",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_verify_router(UserRead),
    prefix=f"{settings.API_V1_STR}/auth",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix=f"{settings.API_V1_STR}/users",
    tags=["users"],
)

# OAuth routes
if settings.GOOGLE_OAUTH_CLIENT_ID:
    from app.api.v1.auth import google_oauth_client

    app.include_router(
        fastapi_users.get_oauth_router(
            google_oauth_client, auth_backend, settings.SECRET_KEY
        ),
        prefix=f"{settings.API_V1_STR}/auth/google",
        tags=["auth"],
    )

if settings.GITHUB_OAUTH_CLIENT_ID:
    from app.api.v1.auth import github_oauth_client

    app.include_router(
        fastapi_users.get_oauth_router(
            github_oauth_client, auth_backend, settings.SECRET_KEY
        ),
        prefix=f"{settings.API_V1_STR}/auth/github",
        tags=["auth"],
    )


@app.get("/")
async def root():
    return {"message": "Welcome to EPUBox API"}
