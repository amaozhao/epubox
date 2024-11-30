import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from tests.test_infrastructure.models import Storage, StorageStatus, User


@pytest.mark.asyncio
async def test_create_storage(db: AsyncSession, test_user: User):
    """测试创建存储记录"""
    storage = Storage(
        id="test-storage-id-2",
        user_id=test_user.id,
        original_filename="test2.epub",
        file_size=2048,
        mime_type="application/epub+zip",
        upload_path="/tmp/test2.epub",
        status=StorageStatus.UPLOADING,
    )
    db.add(storage)
    await db.flush()
    await db.refresh(storage)

    assert storage.id == "test-storage-id-2"
    assert storage.original_filename == "test2.epub"
    assert storage.file_size == 2048
    assert storage.mime_type == "application/epub+zip"
    assert storage.upload_path == "/tmp/test2.epub"
    assert storage.status == StorageStatus.UPLOADING
    assert storage.translation_path is None
    assert storage.error_message is None
    assert storage.created_at is not None
    assert storage.updated_at is not None
    assert storage.completed_at is None


@pytest.mark.asyncio
async def test_storage_status_transitions(db: AsyncSession, test_storage: Storage):
    """测试存储状态转换"""
    # 更新状态为处理中
    test_storage.status = StorageStatus.PROCESSING
    await db.flush()
    await db.refresh(test_storage)
    assert test_storage.status == StorageStatus.PROCESSING

    # 更新状态为翻译中
    test_storage.status = StorageStatus.TRANSLATING
    test_storage.translation_path = "/tmp/test_translated.epub"
    await db.flush()
    await db.refresh(test_storage)
    assert test_storage.status == StorageStatus.TRANSLATING
    assert test_storage.translation_path == "/tmp/test_translated.epub"

    # 更新状态为完成
    test_storage.status = StorageStatus.COMPLETED
    test_storage.completed_at = test_storage.updated_at
    await db.flush()
    await db.refresh(test_storage)
    assert test_storage.status == StorageStatus.COMPLETED
    assert test_storage.completed_at is not None


@pytest.mark.asyncio
async def test_storage_failure_handling(db: AsyncSession, test_storage: Storage):
    """测试存储失败处理"""
    # 设置失败状态
    error_message = "Failed to process EPUB file: invalid format"
    test_storage.status = StorageStatus.FAILED
    test_storage.error_message = error_message
    await db.flush()
    await db.refresh(test_storage)

    assert test_storage.status == StorageStatus.FAILED
    assert test_storage.error_message == error_message


@pytest.mark.asyncio
async def test_storage_user_relationship(db: AsyncSession, test_storage: Storage):
    """测试存储和用户的关系"""
    result = await db.execute(
        select(Storage)
        .options(joinedload(Storage.user))
        .where(Storage.id == test_storage.id)
    )
    storage = result.unique().scalar_one()

    assert storage.user is not None
    assert storage.user.id == test_storage.user_id


@pytest.mark.asyncio
async def test_required_fields(db: AsyncSession, test_user: User):
    """测试必填字段"""
    async with db.begin_nested():
        # 测试缺少必填字段
        storage = Storage(
            id="test-storage-id-3",
            user_id=test_user.id,
            # 缺少 original_filename
            file_size=1024,
            mime_type="application/epub+zip",
            upload_path="/tmp/test3.epub",
        )
        db.add(storage)
        with pytest.raises(IntegrityError):
            await db.flush()

    async with db.begin_nested():
        # 测试缺少用户ID
        storage = Storage(
            id="test-storage-id-3",
            # 缺少 user_id
            original_filename="test3.epub",
            file_size=1024,
            mime_type="application/epub+zip",
            upload_path="/tmp/test3.epub",
        )
        db.add(storage)
        with pytest.raises(IntegrityError):
            await db.flush()
