from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
import jwt
from pydantic import EmailStr

from app.db.session import get_session
from app.models.user import User, UserRole
from app.services.user_service import UserService
from app.core.config import settings
from app.docs.schemas import (
    UserCreate, UserResponse, UserLogin, Token,
    ErrorResponse
)

router = APIRouter()
user_service = UserService()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_session)
) -> User:
    """Get current user from JWT token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )
        user_id: int = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user = await user_service.get_user_by_id(db, user_id)
    if user is None:
        raise credentials_exception
    return user

async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """Check if current user is active."""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )
    return current_user

async def get_current_admin_user(
    current_user: User = Depends(get_current_active_user)
) -> User:
    """Check if current user is admin."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions"
        )
    return current_user

@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse}
    }
)
async def register_user(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_session)
):
    """Register a new user."""
    return await user_service.create_user(
        db=db,
        email=user_data.email,
        password=user_data.password,
        role=user_data.role
    )

@router.post(
    "/login",
    response_model=Token,
    responses={
        400: {"model": ErrorResponse}
    }
)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_session)
):
    """Login and get JWT token."""
    user = await user_service.authenticate_user(
        db=db,
        email=form_data.username,
        password=form_data.password
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token = user_service.create_access_token(
        data={"sub": str(user.id)}
    )
    return {"access_token": access_token, "token_type": "bearer"}

@router.get(
    "/me",
    response_model=UserResponse,
    responses={
        401: {"model": ErrorResponse}
    }
)
async def read_users_me(
    current_user: User = Depends(get_current_active_user)
):
    """Get current user information."""
    return current_user

@router.put(
    "/me",
    response_model=UserResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse}
    }
)
async def update_user_me(
    email: Optional[EmailStr] = None,
    current_password: Optional[str] = None,
    new_password: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_session)
):
    """Update current user information."""
    # If changing password, verify current password
    if new_password:
        if not current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password required to change password"
            )
        await user_service.change_password(
            db=db,
            user_id=current_user.id,
            current_password=current_password,
            new_password=new_password
        )

    # Update other fields
    return await user_service.update_user(
        db=db,
        user_id=current_user.id,
        email=email
    )

@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse}
    }
)
async def delete_user_me(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_session)
):
    """Delete current user account."""
    await user_service.delete_user(db=db, user_id=current_user.id)

# Admin endpoints
@router.get(
    "/users",
    response_model=list[UserResponse],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse}
    }
)
async def list_users(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_session)
):
    """List all users (admin only)."""
    result = await db.execute(
        select(User)
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()

@router.get(
    "/users/{user_id}",
    response_model=UserResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse}
    }
)
async def read_user(
    user_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_session)
):
    """Get user by ID (admin only)."""
    user = await user_service.get_user_by_id(db=db, user_id=user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return user

@router.put(
    "/users/{user_id}",
    response_model=UserResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse}
    }
)
async def update_user(
    user_id: int,
    email: Optional[EmailStr] = None,
    password: Optional[str] = None,
    role: Optional[UserRole] = None,
    current_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_session)
):
    """Update user information (admin only)."""
    user = await user_service.update_user(
        db=db,
        user_id=user_id,
        email=email,
        password=password,
        role=role
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return user

@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse}
    }
)
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_session)
):
    """Delete user (admin only)."""
    deleted = await user_service.delete_user(db=db, user_id=user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
