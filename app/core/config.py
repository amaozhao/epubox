from typing import Optional

from pydantic import EmailStr, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "EPUBox"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    # Environment
    DEBUG_MODE: bool = False  # Set to True for development

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./epubox.db"

    # JWT
    SECRET_KEY: str = "your-secret-key-here"  # Change in production
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # CORS
    BACKEND_CORS_ORIGINS: list[str] = ["http://localhost:8000", "http://localhost:3000"]

    # File Upload Settings
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_SIZE: int = 100 * 1024 * 1024  # 100MB
    ALLOWED_EXTENSIONS: list[str] = [".epub"]

    # First Superuser
    FIRST_SUPERUSER_EMAIL: Optional[EmailStr] = None
    FIRST_SUPERUSER_PASSWORD: Optional[SecretStr] = None

    MISTRAL_API_KEY: Optional[str] = None

    class Config:
        case_sensitive = True
        env_file = ".env"


settings = Settings()
