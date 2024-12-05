from datetime import datetime
from typing import Optional

from fastapi_users import schemas
from pydantic import EmailStr, ConfigDict

from app.db.models import OAuthProvider


class OAuthAccount(schemas.BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: OAuthProvider
    provider_user_id: str
    provider_user_login: Optional[str] = None
    provider_user_email: Optional[str] = None
    created: datetime
    updated: datetime


class UserRead(schemas.BaseUser[int]):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr
    actived: bool = True
    superuser: bool = False
    verified: bool = False
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    oauth_accounts: Optional[list[OAuthAccount]] = None
    created: datetime
    updated: datetime


class UserCreate(schemas.BaseUserCreate):
    model_config = ConfigDict(from_attributes=True)

    username: str
    email: EmailStr
    password: str
    full_name: Optional[str] = None


class UserUpdate(schemas.BaseUserUpdate):
    model_config = ConfigDict(from_attributes=True)

    username: Optional[str] = None
    password: Optional[str] = None
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
