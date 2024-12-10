from pathlib import Path
from typing import Literal, Optional

from pydantic import AnyHttpUrl, EmailStr, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 基本设置
    PROJECT_NAME: str = "Epubox"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    # 数据库设置
    DATABASE_URL: str

    # 安全设置
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    ALGORITHM: str = "HS256"

    # CORS设置
    BACKEND_CORS_ORIGINS: list[AnyHttpUrl] = []

    # OAuth设置
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    GITHUB_CLIENT_ID: Optional[str] = None
    GITHUB_CLIENT_SECRET: Optional[str] = None

    # 第一个超级用户设置
    FIRST_SUPERUSER_EMAIL: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    # AI API Keys
    MISTRAL_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None

    # 日志设置
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    LOG_FILE: Optional[Path] = None
    LOG_RENDER_JSON_LOGS: bool = True

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    @field_validator("LOG_FILE", mode="before")
    def assemble_log_file(cls, v: Optional[str]) -> Optional[Path]:
        if v:
            return Path(v)
        return None

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
