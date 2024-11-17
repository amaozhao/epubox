import pytest
from app.main import app
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_upload():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        with open("test_file.txt", "rb") as file:
            response = await ac.post(
                "/api/storages/upload", files={"file": ("test_file.txt", file)}
            )
        print(response.status_code, response.json())
        assert response.status_code == 200
        assert "epub" in response.json()


@pytest.mark.asyncio
async def test_download():
    file_id = 1  # 假设你要下载的文件ID是1
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.get(f"/api/storages/download/{file_id}")
    assert response.status_code == 200
    print(response.headers)
    assert "application/octet-stream" in response.headers["content-type"]
