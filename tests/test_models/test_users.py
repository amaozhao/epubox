import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from tests.test_infrastructure.models import Storage, StorageStatus, User


@pytest.mark.asyncio
async def test_create_user(db: AsyncSession):
    """测试创建用户"""
    user = User(
        id="test-id",
        username="testuser2",
        email="test2@example.com",
        password_hash="hashed_password",
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    assert user.id == "test-id"
    assert user.username == "testuser2"
    assert user.email == "test2@example.com"
    assert user.is_active is True
    assert user.is_verified is False
    assert user.created_at is not None
    assert user.updated_at is not None
    assert user.last_login_at is None


@pytest.mark.asyncio
async def test_unique_constraints(db: AsyncSession, test_user: User):
    """测试唯一性约束"""
    async with db.begin_nested():
        # 测试重复用户名
        user1 = User(
            id="test-id-1",
            username=test_user.username,  # 重复的用户名
            email="unique@example.com",
            password_hash="hashed_password",
        )
        db.add(user1)
        with pytest.raises(IntegrityError):
            await db.flush()

    async with db.begin_nested():
        # 测试重复邮箱
        user2 = User(
            id="test-id-2",
            username="uniqueuser",
            email=test_user.email,  # 重复的邮箱
            password_hash="hashed_password",
        )
        db.add(user2)
        with pytest.raises(IntegrityError):
            await db.flush()


@pytest.mark.asyncio
async def test_user_storage_relationship(
    db: AsyncSession, test_user: User, test_storage: Storage
):
    """测试用户和存储的关系"""
    result = await db.execute(
        select(User).options(joinedload(User.storages)).where(User.id == test_user.id)
    )
    user = result.unique().scalar_one()

    # 验证存储关系
    assert len(user.storages) == 1
    storage = user.storages[0]
    assert storage.id == test_storage.id
    assert storage.user_id == test_user.id
    assert storage.original_filename == test_storage.original_filename
    assert storage.status == StorageStatus.UPLOADED


@pytest.mark.asyncio
async def test_cascade_delete(db: AsyncSession, test_user: User, test_storage: Storage):
    """测试级联删除"""
    await db.delete(test_user)
    await db.flush()

    # 验证用户和存储记录都被删除
    result = await db.execute(select(User).where(User.id == test_user.id))
    assert result.first() is None

    result = await db.execute(select(Storage).where(Storage.id == test_storage.id))
    assert result.first() is None
