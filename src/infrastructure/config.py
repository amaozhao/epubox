import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 基础配置
    APP_NAME: str = "EPUBox"
    DEBUG: bool = False

    # 数据库配置
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost/epubox"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # 文件存储配置
    UPLOAD_DIR: str = str(Path(__file__).parent.parent.parent / "data" / "uploads")
    TRANSLATION_DIR: str = str(
        Path(__file__).parent.parent.parent / "data" / "translations"
    )
    LOG_DIR: str = str(Path(__file__).parent.parent.parent / "logs")

    # 认证配置
    SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 小时

    # 翻译服务配置
    TRANSLATION_API_KEY: Optional[str] = None
    MAX_CONCURRENT_TRANSLATIONS: int = 5

    # 日志配置
    LOG_LEVEL: str = "INFO"
    LOG_RENDERER: str = "json"  # 可选: json, console
    LOG_ENABLE_TIMESTAMPS: bool = True
    LOG_ENABLE_TRACEBACKS: bool = True

    class Config:
        env_file = ".env"
        case_sensitive = True


# 创建全局配置实例
settings = Settings()

# 确保必要的目录存在
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.TRANSLATION_DIR, exist_ok=True)
os.makedirs(settings.LOG_DIR, exist_ok=True)
