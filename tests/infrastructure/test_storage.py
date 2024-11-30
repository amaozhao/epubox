import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.database import Base
from src.infrastructure.storage import StorageService
from src.models.storage import Storage, StorageStatus
from src.models.users import User
from src.utils.errors import FileError

# 测试数据库URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def engine():
    """创建测试数据库引擎"""
    engine = create_async_engine(TEST_DATABASE_URL, echo=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine):
    """创建测试会话"""
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        yield session


@pytest.fixture
async def test_user(session):
    """创建测试用户"""
    user = User(
        id="test-user-id",
        username="testuser",
        email="test@example.com",
        password_hash="dummy_hash",
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
def storage_service(tmp_path, session):
    """创建测试用的存储服务实例"""
    upload_dir = tmp_path / "uploads"
    translation_dir = tmp_path / "translations"
    return StorageService(
        session=session,
        upload_dir=str(upload_dir),
        translation_dir=str(translation_dir),
    )


@pytest.fixture
def sample_file():
    """创建示例文件内容"""
    return b"Sample file content"


@pytest.fixture
def sample_epub():
    """创建示例EPUB文件"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as zip_file:
        # 添加mimetype文件
        zip_file.writestr("mimetype", "application/epub+zip")
        # 添加container.xml
        zip_file.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0"?>
            <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                <rootfiles>
                    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
                </rootfiles>
            </container>""",
        )
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_save_upload(storage_service, sample_file, test_user):
    """测试文件上传"""
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    assert file_id is not None
    assert os.path.exists(os.path.join(storage_service.upload_dir, file_id))


@pytest.mark.asyncio
async def test_create_work_copy(storage_service, sample_file, test_user):
    """测试创建工作副本"""
    # 先上传文件
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    # 创建工作副本
    work_copy_path = await storage_service.create_work_copy(file_id)

    assert work_copy_path is not None
    assert os.path.exists(work_copy_path)


@pytest.mark.asyncio
async def test_get_file_not_found(storage_service):
    """测试获取不存在的文件"""
    with pytest.raises(FileError):
        await storage_service.get_file("nonexistent")


@pytest.mark.asyncio
async def test_delete_file(storage_service, sample_file, test_user):
    """测试删除文件"""
    # 先上传文件
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    # 删除文件
    await storage_service.delete_file(file_id)

    assert not os.path.exists(os.path.join(storage_service.upload_dir, file_id))


@pytest.mark.asyncio
async def test_verify_epub_valid(storage_service, sample_epub, test_user):
    """测试验证有效的EPUB文件"""
    file = BytesIO(sample_epub)
    file_id = await storage_service.save_upload(file, "test.epub", test_user.id)

    is_valid = await storage_service.verify_epub(file_id)
    assert is_valid


@pytest.mark.asyncio
async def test_verify_epub_invalid(storage_service, sample_file, test_user):
    """测试验证无效的EPUB文件"""
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    is_valid = await storage_service.verify_epub(file_id)
    assert not is_valid


@pytest.mark.asyncio
async def test_cleanup_expired_files(storage_service, sample_file, test_user):
    """测试清理过期文件"""
    # 上传一些文件
    files = []
    storages = []
    for i in range(3):
        file = BytesIO(sample_file)
        file_id = await storage_service.save_upload(file, f"test{i}.txt", test_user.id)
        files.append(file_id)
        # 获取存储记录
        storage = await storage_service.session.get(Storage, file_id)
        storages.append(storage)

    # 修改前两个文件的创建时间为过期时间
    expired_time = datetime.now(timezone.utc) - timedelta(days=8)
    for storage in storages[:2]:
        # 修改文件的修改时间
        file_path = os.path.join(storage_service.upload_dir, storage.id)
        os.utime(file_path, (expired_time.timestamp(), expired_time.timestamp()))
        # 修改数据库中的创建时间
        storage.created_at = expired_time

    # 确保更改被提交到数据库
    await storage_service.session.commit()
    # 刷新会话以确保下一次查询获取最新数据
    await storage_service.session.flush()

    # 清理过期文件
    await storage_service.cleanup_expired_files()

    # 刷新会话以确保获取最新状态
    await storage_service.session.refresh(storages[0])
    await storage_service.session.refresh(storages[1])
    await storage_service.session.refresh(storages[2])

    # 验证结果：前两个文件应该被删除，最后一个文件应该保留
    assert not os.path.exists(os.path.join(storage_service.upload_dir, files[0]))
    assert not os.path.exists(os.path.join(storage_service.upload_dir, files[1]))
    assert os.path.exists(os.path.join(storage_service.upload_dir, files[2]))

    # 验证数据库状态
    assert storages[0].status == StorageStatus.DELETED
    assert storages[1].status == StorageStatus.DELETED
    assert storages[2].status == StorageStatus.UPLOADED

    # 验证文件的物理删除
    for file_id in files[:2]:
        file_path = os.path.join(storage_service.upload_dir, file_id)
        assert not os.path.exists(file_path), f"File {file_id} should have been deleted"
