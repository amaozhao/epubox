import pytest
from fastapi import status
from app.models.user import UserRole
import uuid
import os

# Get the absolute path of the test EPUB file
TEST_EPUB_PATH = os.path.join(os.path.dirname(__file__), "data", "test.epub")

@pytest.fixture(name="test_user")
def test_user_fixture():
    """Get test user data."""
    return {
        "email": "test@example.com",
        "password": "password123",
        "role": UserRole.FREE
    }

@pytest.mark.asyncio
async def test_health_check(client):
    """Test the health check endpoint returns correct status."""
    response = await client.get("/health")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
class TestUserAPI:
    async def test_create_user(self, client, test_user):
        """Test user creation endpoint."""
        user_data = {
            "email": "newuser@example.com",
            "password": "strongpassword123",
            "role": UserRole.FREE
        }
        response = await client.post("/api/v1/users/register", json=user_data)
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["email"] == user_data["email"]
        assert "id" in data
        assert "password" not in data

    async def test_login_user(self, client, test_user):
        """Test user login endpoint."""
        # First create a user
        response = await client.post("/api/v1/users/register", json=test_user)
        assert response.status_code == status.HTTP_201_CREATED
        
        # Then try to login
        login_data = {
            "username": test_user["email"],
            "password": test_user["password"],
        }
        response = await client.post("/api/v1/users/login", data=login_data)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "access_token" in data

    async def test_invalid_login(self, client):
        """Test login with invalid credentials."""
        login_data = {
            "username": "nonexistent@example.com",
            "password": "wrongpassword",
        }
        response = await client.post("/api/v1/users/login", data=login_data)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

@pytest.fixture(name="auth_headers")
async def auth_headers_fixture(client, test_user):
    """Fixture to get authentication headers."""
    # Generate unique email for each test
    unique_email = f"test_{uuid.uuid4()}@example.com"
    test_user["email"] = unique_email
    
    # Register user
    response = await client.post("/api/v1/users/register", json=test_user)
    if response.status_code != status.HTTP_201_CREATED:
        print(f"Register failed with status {response.status_code}")
        print(f"Response: {response.json()}")
    assert response.status_code == status.HTTP_201_CREATED
    
    # Login user
    login_data = {
        "username": test_user["email"],
        "password": test_user["password"]
    }
    response = await client.post("/api/v1/users/login", data=login_data)
    assert response.status_code == status.HTTP_200_OK
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}

@pytest.mark.asyncio
class TestTranslationAPI:
    async def test_upload_epub(self, client, auth_headers):
        """Test EPUB file upload endpoint."""
        # Use the real test EPUB file
        with open(TEST_EPUB_PATH, "rb") as f:
            epub_content = f.read()

        files = {
            "file": ("test.epub", epub_content, "application/epub+zip")
        }
        headers = await auth_headers
        # Remove Content-Type from headers to let it be set by the multipart form
        headers.pop("Content-Type", None)
        response = await client.post(
            "/api/v1/files/upload",
            files=files,
            headers=headers
        )
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert "id" in data.get('file_info')
        assert "filename" in data.get('file_info')

    async def test_translation_request(self, client, auth_headers):
        """Test translation request endpoint."""
        headers = await auth_headers
        # Add Content-Type for JSON request
        headers["Content-Type"] = "application/json"
        response = await client.post(
            "/api/v1/translation/translate/text",
            headers=headers,
            json={
                "text": "Hello",
                "source_language": "en",
                "target_language": "zh-CN",
                "context": None
            }
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "translated_text" in data
        assert data["source_language"] == "en"
        assert data["target_language"] == "zh-CN"

    async def test_translation_status(self, client, auth_headers):
        """Test translation status endpoint."""
        headers = await auth_headers
        
        # Use the real test EPUB file
        with open(TEST_EPUB_PATH, "rb") as f:
            epub_content = f.read()

        files = {
            "file": ("test.epub", epub_content, "application/epub+zip")
        }
        upload_headers = headers.copy()
        # Remove Content-Type from headers to let it be set by the multipart form
        upload_headers.pop("Content-Type", None)
        
        response = await client.post(
            "/api/v1/translation/translate",
            files=files,
            headers=upload_headers,
            data={
                "source_language": "en",
                "target_language": "zh-CN"
            }
        )
        assert response.status_code == status.HTTP_200_OK
        task_id = response.json()["task_id"]
        
        # Then check the task status
        response = await client.get(
            f"/api/v1/translation/tasks/{task_id}",
            headers=headers
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "status" in data
        # assert data["status"] in ["PENDING", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED"]
        assert "progress" in data
        assert isinstance(data["progress"], float)
        assert 0 <= data["progress"] <= 1.0
