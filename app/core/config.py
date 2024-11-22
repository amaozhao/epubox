from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, List, Dict, Any
from functools import lru_cache

class Settings(BaseSettings):
    PROJECT_NAME: str = "EPUBox"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"
    
    # JWT
    SECRET_KEY: str = "your-secret-key-here"  # Change in production
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30  # 30 minutes
    
    # Database
    SQLALCHEMY_DATABASE_URL: str = "sqlite+aiosqlite:///database.db"
    DB_ECHO: bool = True
    
    # CORS
    BACKEND_CORS_ORIGINS: List[str] = ["*"]
    
    # Translation Services
    GOOGLE_TRANSLATE_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    MISTRAL_API_KEY: Optional[str] = None
    DEEPL_API_KEY: Optional[str] = None
    TRANSLATION_SERVICE_DEFAULT: str = "google"
    TRANSLATION_QUOTA_LIMIT: int = 1000000
    
    # File Upload
    MAX_UPLOAD_SIZE: int = 100 * 1024 * 1024  # 100MB
    ALLOWED_EXTENSIONS: List[str] = ["epub"]
    UPLOAD_DIR: str = "uploads"
    TEMP_UPLOAD_DIR: str = "uploads/temp"  # Temporary upload directory
    PROCESSED_FILES_DIR: str = "uploads/processed"  # Processed files directory
    
    # Redis Configuration
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=".env",
        extra="allow"
    )

@lru_cache()
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
