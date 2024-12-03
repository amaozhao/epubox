import logging
import os
from pathlib import Path
from typing import Optional, Union

from pydantic import validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 基础配置
    APP_NAME: str = "EPUBox"
    DEBUG: bool = False
    PROJECT_NAME: str = "EPUBox"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    BACKEND_CORS_ORIGINS: str = '["http://localhost:8000", "http://localhost:3000"]'

    # 数据库配置
    DATABASE_URL: str = "sqlite+aiosqlite:///./epubox.db"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # 文件存储配置
    UPLOAD_DIR: Union[str, Path] = (
        Path(__file__).parent.parent.parent / "tests" / "output" / "uploads"
    )
    TRANSLATION_DIR: Union[str, Path] = (
        Path(__file__).parent.parent.parent / "tests" / "output" / "translations"
    )
    LOG_DIR: Union[str, Path] = Path(__file__).parent.parent.parent / "logs"
    STORAGE_PATH: Union[str, Path] = Path("tests/output/storage")
    TEMP_PATH: Union[str, Path] = Path("tests/output/temp")

    # 认证配置
    SECRET_KEY: str = "your-secret-key"  # 默认值仅用于开发
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 小时
    FIRST_SUPERUSER_EMAIL: str = "admin@example.com"
    FIRST_SUPERUSER_PASSWORD: str = "admin123"

    # API Keys
    OPENAI_API_KEY: str = "test-openai-key"
    GOOGLE_API_KEY: str = "test-google-key"
    MISTRAL_API_KEY: str = "test-mistral-key"
    DEEPL_API_KEY: str = "test-deepl-key"

    # 翻译服务配置
    TRANSLATION_API_KEY: Optional[str] = None
    MAX_CONCURRENT_TRANSLATIONS: int = 5

    # 日志配置
    LOG_LEVEL: int = logging.INFO
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    LOG_FILE: Union[str, Path] = Path("epubox.log")
    LOG_ENABLE_TIMESTAMPS: bool = True
    LOG_ENABLE_TRACEBACKS: bool = True

    @validator(
        "UPLOAD_DIR",
        "TRANSLATION_DIR",
        "LOG_DIR",
        "STORAGE_PATH",
        "TEMP_PATH",
        "LOG_FILE",
        pre=True,
    )
    def validate_paths(cls, v):
        if isinstance(v, str):
            return Path(v)
        if isinstance(v, Path):
            return v
        raise ValueError(f"Invalid path type: {type(v)}")

    class Config:
        env_file = ".env"
        case_sensitive = True


# 创建全局配置实例
settings = Settings()

# 确保必要的目录存在
for path in [
    settings.UPLOAD_DIR,
    settings.TRANSLATION_DIR,
    settings.LOG_DIR,
    settings.STORAGE_PATH,
    settings.TEMP_PATH,
]:
    os.makedirs(str(path), exist_ok=True)
