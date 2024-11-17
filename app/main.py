from fastapi import FastAPI, Depends
from app.routers import storage_router

from app.core.errors import (
    file_upload_error_handler,
    file_size_error_handler,
    file_type_error_handler,
    general_error_handler,
    FileUploadError,
    FileSizeError,
    FileTypeError,
)
from contextlib import asynccontextmanager

from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.users.manager import auth_backend, current_active_user, fastapi_users


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Not needed if you setup a migration system like Alembic
    # await create_db_and_tables()
    yield


app = FastAPI(lifespan=lifespan)


# 注册自定义错误处理器
app.add_exception_handler(FileUploadError, file_upload_error_handler)
app.add_exception_handler(FileSizeError, file_size_error_handler)
app.add_exception_handler(FileTypeError, file_type_error_handler)
app.add_exception_handler(Exception, general_error_handler)  # 捕获其他未处理的异常

app.include_router(
    fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"]
)
app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_reset_password_router(),
    prefix="/auth",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_verify_router(UserRead),
    prefix="/auth",
    tags=["auth"],
)
app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/users",
    tags=["users"],
)


@app.get("/authenticated-route")
async def authenticated_route(user: User = Depends(current_active_user)):
    return {"message": f"Hello {user.email}!"}


# 注册路由
app.include_router(storage_router, prefix="/api", tags=["storages"])
