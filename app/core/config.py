from pathlib import Path
from typing import Dict, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    # 项目信息
    PROJECT_NAME: str = "EpuBox"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    # 安全配置
    SECRET_KEY: str = "your-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_DAYS: int = 1  # access token 1天有效期
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30  # refresh token 30天有效期
    MIN_PASSWORD_LENGTH: int = 8
    # 密码正则表达式，使用原始字符串避免转义问题
    PASSWORD_REGEX: str = r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,}$"
    RATE_LIMIT_PER_SECOND: int = 10

    # OAuth配置
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = ""

    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_REDIRECT_URI: str = ""

    # WeChat settings
    # - 开放平台（网站应用）
    WECHAT_APP_ID: str = ""
    WECHAT_APP_SECRET: str = ""
    WECHAT_REDIRECT_URI: str = ""

    # - 小程序
    WECHAT_MINI_APP_ID: str = ""
    WECHAT_MINI_APP_SECRET: str = ""

    # - 公众号
    WECHAT_MP_APP_ID: str = ""
    WECHAT_MP_APP_SECRET: str = ""

    # - 开放平台（unionid）
    WECHAT_OPEN_PLATFORM: bool = False  # 是否启用微信开放平台（用于获取unionid）

    # - 其他设置
    WECHAT_API_TIMEOUT: int = 30  # API超时时间（秒）
    WECHAT_ACCESS_TOKEN_EXPIRE: int = 7200  # 访问令牌过期时间（秒）
    WECHAT_REFRESH_TOKEN_EXPIRE: int = 2592000  # 刷新令牌过期时间（30天）

    # OAuth回调基础URL
    OAUTH_CALLBACK_BASE_URL: str = "http://localhost:8000"  # 开发环境默认值

    # 数据库配置
    DATABASE_URL: str = "sqlite+aiosqlite:///./epub.db"
    DB_ECHO: bool = True
    DB_POOL_SIZE: int = 5
    DB_POOL_TIMEOUT: int = 30
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 300

    # CORS配置
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8000"]

    # 文件上传配置
    MAX_UPLOAD_SIZE: int = 104857600  # 100MB
    UPLOAD_DIR: str = "./uploads"
    SUPPORTED_FILE_TYPES: List[str] = ["epub"]

    # 日志配置
    LOG_LEVEL: str = "INFO"
    LOG_INFO_RETENTION_DAYS: int = 30
    LOG_ERROR_RETENTION_DAYS: int = 90
    LOG_DEBUG_RETENTION_DAYS: int = 7
    LOG_ROTATION_TIME: str = "midnight"
    ENABLE_DEBUG_LOGGING: bool = True

    # 超级用户配置
    FIRST_SUPERUSER_EMAIL: Optional[str] = None
    FIRST_SUPERUSER_PASSWORD: Optional[str] = None

    # API密钥配置
    OPENAI_API_KEY: Optional[str] = None
    MISTRAL_API_KEY: Optional[str] = None

    # Frontend settings
    FRONTEND_URL: str = "http://localhost:3000"

    # OAuth settings
    OAUTH_STATE_TTL: int = 600  # 10分钟

    @property
    def LOG_PATHS(self) -> Dict[str, Path]:
        """获取日志文件路径"""
        return {
            "info": Path("logs/info.log"),
            "error": Path("logs/error.log"),
            "debug": Path("logs/debug.log"),
        }

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "allow"  # 允许额外的字段


# 创建全局设置实例
settings = Settings()

__all__ = ["settings"]
