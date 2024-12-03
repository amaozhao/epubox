from typing import Optional
from pydantic import BaseModel, EmailStr, Field


class UserBase(BaseModel):
    """用户基础信息"""
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    avatar_url: Optional[str] = None
    actived: bool = True
    verified: bool = False
    superuser: bool = False


class UserCreate(UserBase):
    """用户创建"""
    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)


class UserUpdate(UserBase):
    """用户更新"""
    password: Optional[str] = Field(None, min_length=6)


class UserInDB(UserBase):
    """数据库中的用户"""
    id: int
    hashed_password: str

    class Config:
        from_attributes = True


class User(UserBase):
    """API响应中的用户"""
    id: int

    class Config:
        from_attributes = True


class Token(BaseModel):
    """访问令牌"""
    access_token: str
    token_type: str = "bearer"
    refresh_token: Optional[str] = None


class TokenData(BaseModel):
    """令牌数据"""
    username: Optional[str] = None
    scopes: list[str] = []


class UserBase2(BaseModel):
    """用户基础模型"""
    email: Optional[EmailStr] = None
    username: str


class UserCreate2(UserBase2):
    """用户创建模型"""
    password: str


class UserLogin(BaseModel):
    """用户登录模型"""
    username: str
    password: str


class Token2(BaseModel):
    """令牌模型"""
    access_token: str
    token_type: str
    refresh_token: str


class TokenPayload(BaseModel):
    """令牌载荷模型"""
    sub: Optional[str] = None
    scopes: list[str] = []
    type: Optional[str] = None