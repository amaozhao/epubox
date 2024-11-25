import pytest
from httpx import AsyncClient
from fastapi import status

from app.core.logging import test_logger as logger

# Configure test logging


@pytest.fixture
def test_user_data():
    return {
        "email": "test@example.com",
        "username": "testuser",
        "password": "testpassword123",
        "phone": "+1234567890",
    }


@pytest.fixture
def another_user_data():
    return {
        "email": "another@example.com",
        "username": "anotheruser",
        "password": "testpassword123",
        "phone": "+1987654321",
    }


async def test_register_user(async_client: AsyncClient, test_user_data):
    """Test successful user registration."""
    logger.info(
        "starting_user_registration_test",
        user_email=test_user_data["email"],
        username=test_user_data["username"],
    )

    response = await async_client.post("/api/v1/auth/register", json=test_user_data)
    assert response.status_code == status.HTTP_201_CREATED

    data = response.json()
    logger.info(
        "registration_response_received",
        status_code=response.status_code,
        user_id=data.get("id"),
        email=data.get("email"),
        username=data.get("username"),
    )

    assert data["email"] == test_user_data["email"]
    assert data["username"] == test_user_data["username"]
    assert data["is_active"] is True
    assert data["is_superuser"] is False
    assert data["is_verified"] is False
    assert "id" in data


async def test_register_duplicate_email(async_client: AsyncClient, test_user_data):
    """Test registration with duplicate email."""
    # First register the user
    await test_register_user(async_client, test_user_data)

    # Try to register with the same email but different username
    duplicate_email_data = {**test_user_data, "username": "different_user"}

    logger.info(
        "testing_duplicate_email_registration",
        email=duplicate_email_data["email"],
        username=duplicate_email_data["username"],
    )

    response = await async_client.post(
        "/api/v1/auth/register", json=duplicate_email_data
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    data = response.json()
    assert data["detail"] == "REGISTER_USER_ALREADY_EXISTS"


async def test_register_duplicate_username(async_client: AsyncClient, test_user_data):
    """Test registration with duplicate username."""
    # First register the user
    await test_register_user(async_client, test_user_data)

    # Try to register with the same username but different email
    duplicate_username_data = {**test_user_data, "email": "different@example.com"}

    logger.info(
        "testing_duplicate_username_registration",
        email=duplicate_username_data["email"],
        username=duplicate_username_data["username"],
    )

    response = await async_client.post(
        "/api/v1/auth/register", json=duplicate_username_data
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    data = response.json()
    assert data["detail"] == "REGISTER_USER_ALREADY_EXISTS"


async def test_login_invalid_password(async_client: AsyncClient, test_user_data):
    """Test login with invalid password."""
    # First register the user
    await test_register_user(async_client, test_user_data)

    # Try to login with wrong password
    login_data = {"username": test_user_data["email"], "password": "wrongpassword"}

    logger.info("testing_invalid_password_login", email=login_data["username"])

    response = await async_client.post("/api/v1/auth/jwt/login", data=login_data)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    data = response.json()
    assert data["detail"] == "LOGIN_BAD_CREDENTIALS"


async def test_password_validation(async_client: AsyncClient, test_user_data):
    """Test password validation rules."""
    # Test short password
    short_password_data = {**test_user_data, "password": "short"}

    logger.info(
        "testing_short_password_validation",
        email=short_password_data["email"],
        password_length=len(short_password_data["password"]),
    )

    response = await async_client.post(
        "/api/v1/auth/register", json=short_password_data
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    data = response.json()
    assert data["detail"]["code"] == "REGISTER_INVALID_PASSWORD"
    assert data["detail"]["reason"] == "Password should be at least 8 characters"

    # Test password containing email
    email_in_password_data = {
        **test_user_data,
        "password": f"pass{test_user_data['email']}word",
    }

    logger.info(
        "testing_email_in_password_validation", email=email_in_password_data["email"]
    )

    response = await async_client.post(
        "/api/v1/auth/register", json=email_in_password_data
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    data = response.json()
    assert data["detail"]["code"] == "REGISTER_INVALID_PASSWORD"
    assert data["detail"]["reason"] == "Password should not contain email"


async def test_access_protected_route_without_token(async_client: AsyncClient):
    """Test accessing protected route without token."""
    logger.info("testing_protected_route_access_without_token")

    response = await async_client.get("/api/v1/users/me")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    data = response.json()
    assert "Unauthorized" in str(data)


async def test_access_protected_route_invalid_token(async_client: AsyncClient):
    """Test accessing protected route with invalid token."""
    headers = {"Authorization": "Bearer invalid_token"}

    logger.info("testing_protected_route_access_with_invalid_token")

    response = await async_client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    data = response.json()
    assert "Unauthorized" in str(data)


async def test_login_user(async_client: AsyncClient, test_user_data):
    """Test successful user login."""
    logger.info("starting_user_login_test", email=test_user_data["email"])

    # First register a user
    await test_register_user(async_client, test_user_data)

    login_data = {
        "username": test_user_data["email"],
        "password": test_user_data["password"],
    }
    response = await async_client.post("/api/v1/auth/jwt/login", data=login_data)

    logger.info(
        "login_response_received",
        status_code=response.status_code,
        has_token=(
            "access_token" in response.json()
            if response.status_code == status.HTTP_200_OK
            else False
        ),
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    return data


async def test_get_current_user(async_client: AsyncClient, test_user_data):
    """Test getting current user details."""
    logger.info("starting_get_current_user_test", email=test_user_data["email"])

    # First login to get the token
    login_data = await test_login_user(async_client, test_user_data)
    token = login_data["access_token"]

    # Get current user with token
    headers = {"Authorization": f"Bearer {token}"}
    response = await async_client.get("/api/v1/users/me", headers=headers)

    logger.info(
        "get_current_user_response_received",
        status_code=response.status_code,
        has_user_data=response.status_code == status.HTTP_200_OK,
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["email"] == test_user_data["email"]
    assert data["username"] == test_user_data["username"]
    assert data["phone"] == test_user_data["phone"]


async def test_update_user(async_client: AsyncClient, test_user_data):
    """Test updating user details."""
    logger.info("starting_update_user_test", email=test_user_data["email"])

    # First login to get the token
    login_data = await test_login_user(async_client, test_user_data)
    token = login_data["access_token"]

    update_data = {"full_name": "Test User", "phone": "+9876543210"}

    # Update user
    headers = {"Authorization": f"Bearer {token}"}
    response = await async_client.patch(
        "/api/v1/users/me", json=update_data, headers=headers
    )

    logger.info(
        "update_user_response_received",
        status_code=response.status_code,
        has_user_data=response.status_code == status.HTTP_200_OK,
    )

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["full_name"] == update_data["full_name"]
    assert data["phone"] == update_data["phone"]
