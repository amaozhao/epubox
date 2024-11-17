# app/schemas/user.py
from fastapi_users import schemas


class UserCreate(schemas.BaseUserCreate):
    pass


class UserUpdate(schemas.BaseUserUpdate):
    pass


class UserRead(schemas.BaseUser):
    pass
