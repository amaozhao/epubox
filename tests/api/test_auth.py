import pytest
from httpx import AsyncClient
from fastapi import status
import uuid

pytestmark = pytest.mark.asyncio

TEST_USER = {
    "email": "test@example.com",
    "password": "testpassword123"
}


async def test_register_user(client: AsyncClient):
    """Test user registration."""
    response = await client.post(
        "/api/auth/register",
        json=TEST_USER
    )
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["email"] == TEST_USER["email"]
    assert "id" in data
    assert "is_active" in data
    assert "is_verified" in data
    assert "is_superuser" in data


async def test_login_user(client: AsyncClient):
    """Test user login."""
    # First register the user
    await client.post("/api/auth/register", json=TEST_USER)
    
    # Then try to login
    response = await client.post(
        "/api/auth/jwt/login",
        data={
            "username": TEST_USER["email"],
            "password": TEST_USER["password"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


async def test_get_current_user(client: AsyncClient, authenticated_client: AsyncClient):
    """Test getting current user information."""
    response = await authenticated_client.get("/api/me")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["email"] == TEST_USER["email"]


async def test_unauthorized_access(client: AsyncClient):
    """Test accessing protected endpoint without authentication."""
    response = await client.get("/api/me")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_register_duplicate_email(client: AsyncClient):
    """Test registering with an email that's already in use."""
    # Register first user
    await client.post("/api/auth/register", json=TEST_USER)
    
    # Try to register again with same email
    response = await client.post(
        "/api/auth/register",
        json=TEST_USER
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_login_wrong_password(client: AsyncClient):
    """Test login with wrong password."""
    # First register the user
    await client.post("/api/auth/register", json=TEST_USER)
    
    # Then try to login with wrong password
    response = await client.post(
        "/api/auth/jwt/login",
        data={
            "username": TEST_USER["email"],
            "password": "wrongpassword",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_password_reset_request(client: AsyncClient):
    """Test requesting password reset."""
    # First register the user
    await client.post("/api/auth/register", json=TEST_USER)
    
    response = await client.post(
        "/api/auth/forgot-password",
        json={"email": TEST_USER["email"]}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED


async def test_verify_user(client: AsyncClient, test_user_token: str):
    """Test user verification."""
    response = await client.post(
        f"/api/auth/verify",
        json={"token": test_user_token}
    )
    assert response.status_code == status.HTTP_200_OK
