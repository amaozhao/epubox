from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi_users import FastAPIUsers

from app.core.auth import auth_backend, get_user_manager
from app.core.config import settings
from app.core.logging import app_logger as logger
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.api.endpoints import epub_files

# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)

# Configure CORS
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# FastAPI Users instance
fastapi_users = FastAPIUsers[User, int](
    get_user_manager,
    [auth_backend],
)

# Include routers
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
    fastapi_users.get_verify_router(UserRead),
    prefix=f"{settings.API_V1_STR}/auth",
    tags=["auth"],
)

app.include_router(
    fastapi_users.get_reset_password_router(),
    prefix=f"{settings.API_V1_STR}/auth",
    tags=["auth"],
)

app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix=f"{settings.API_V1_STR}/users",
    tags=["users"],
)

# Include EPUB file management endpoints
app.include_router(
    epub_files.router,
    prefix=f"{settings.API_V1_STR}/epub-files",
    tags=["epub-files"],
    dependencies=[Depends(fastapi_users.current_user())],
)


@app.get("/")
async def root():
    """Root endpoint."""
    logger.info("root_endpoint_accessed", status="success")
    return {"message": "Welcome to EPUBox API"}


@app.on_event("startup")
async def startup_event():
    """Log when the application starts."""
    logger.info(
        "application_startup",
        project_name=settings.PROJECT_NAME,
        api_v1_str=settings.API_V1_STR,
        backend_cors_origins=settings.BACKEND_CORS_ORIGINS,
    )


@app.on_event("shutdown")
async def shutdown_event():
    """Log when the application shuts down."""
    logger.info("application_shutdown", project_name=settings.PROJECT_NAME)
