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
        # 添加mimetype文件（必须是第一个文件且不压缩）
        zip_file.writestr("mimetype", "application/epub+zip")

        # 添加 META-INF/container.xml
        zip_file.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                <rootfiles>
                    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
                </rootfiles>
            </container>""",
        )

        # 添加 content.opf
        zip_file.writestr(
            "OEBPS/content.opf",
            """<?xml version="1.0" encoding="UTF-8"?>
            <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
                <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
                    <dc:title>Test Book</dc:title>
                    <dc:creator>Test Author</dc:creator>
                    <dc:identifier id="uid">test-book-id</dc:identifier>
                    <dc:language>en</dc:language>
                </metadata>
                <manifest>
                    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
                    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
                </manifest>
                <spine>
                    <itemref idref="nav"/>
                    <itemref idref="chapter1"/>
                </spine>
            </package>""",
        )

        # 添加导航文件
        zip_file.writestr(
            "OEBPS/nav.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE html>
            <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
                <head>
                    <title>Navigation</title>
                </head>
                <body>
                    <nav epub:type="toc">
                        <h1>Table of Contents</h1>
                        <ol>
                            <li><a href="chapter1.xhtml">Chapter 1</a></li>
                        </ol>
                    </nav>
                </body>
            </html>""",
        )

        # 添加内容文件
        zip_file.writestr(
            "OEBPS/chapter1.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE html>
            <html xmlns="http://www.w3.org/1999/xhtml">
                <head>
                    <title>Chapter 1</title>
                </head>
                <body>
                    <h1>Chapter 1</h1>
                    <p>This is a test chapter.</p>
                </body>
            </html>""",
        )

    return buffer.getvalue()


@pytest.fixture
def sample_file():
    """创建示例文件内容"""
    return b"Sample file content"


@pytest.mark.asyncio
async def test_save_upload(storage_service, sample_file, test_user):
    """测试文件上传"""
    file = BytesIO(sample_file)
    file_id = await storage_service.save_upload(file, "test.txt", test_user.id)

    assert file_id is not None
    # 获取存储记录
    storage = await storage_service.session.get(Storage, file_id)
    assert os.path.exists(storage.upload_path)


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

    # 获取存储记录
    storage = await storage_service.session.get(Storage, file_id)
    file_path = storage.upload_path

    # 删除文件
    await storage_service.delete_file(file_id)

    # 验证文件已被删除
    assert not os.path.exists(file_path)

    # 验证存储记录状态已更新
    await storage_service.session.refresh(storage)
    assert storage.status == StorageStatus.DELETED


@pytest.mark.asyncio
async def test_verify_epub_valid(storage_service, sample_epub, test_user):
    """测试验证有效的EPUB文件"""
    # 创建一个有效的EPUB文件
    file = BytesIO(sample_epub)
    file_id = await storage_service.save_upload(file, "test.epub", test_user.id)

    # 获取存储记录
    storage = await storage_service.session.get(Storage, file_id)

    # 验证EPUB文件
    is_valid = await storage_service.verify_epub(file_id, "upload")
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
    expired_time = datetime.now(timezone.utc) - timedelta(hours=25)  # 25小时前
    for storage in storages[:2]:
        # 修改文件的修改时间
        file_path = storage.upload_path
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
    assert not os.path.exists(storages[0].upload_path)
    assert not os.path.exists(storages[1].upload_path)
    assert os.path.exists(storages[2].upload_path)

    # 验证数据库状态
    assert storages[0].status == StorageStatus.DELETED
    assert storages[1].status == StorageStatus.DELETED
    assert storages[2].status == StorageStatus.UPLOADED
