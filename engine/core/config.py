"""
Configuration settings for the Epubox application.
"""

from pathlib import Path
from typing import Literal

from pydantic import PositiveInt
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 基础配置
    PROJECT_NAME: str = "Epubox"
    DEBUG: bool = True

    # OpenAI 配置
    OPENAI_API_KEY: str = "your-api-key-here"
    OPENAI_API_BASE: str | None = None
    OPENAI_MODEL: str = "gpt-4o"

    # Agnes 配置
    AGNES_API_KEY: str = ""
    AGNES_BASE_URL: str = "https://apihub.agnes-ai.com/v1"
    AGNES_MODEL: str = "agnes-2.0-flash"
    AGNES_TEXT_RPM: PositiveInt = 10

    # Deepseek引擎配置
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_API_KEY: str = "your-api-key-here"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # Kimi引擎配置
    KIMI_BASE_URL: str = "https://api.moonshot.cn/v1"
    KIMI_API_KEY: str = "your-api-key-here"
    KIMI_MODEL: str = "kimi-latest"

    # GLM引擎配置
    # GLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/"
    GLM_BASE_URL: str = "https://api-ai.gitcode.com/v1"
    GLM_API_KEY: str = "your-api-key-here"
    GLM_MODEL: str = "GLM-4.7-Flash"

    # Gemini 引擎配置
    GEMINI_API_KEY: str = "your-api-key-here"
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # BAISHAN 配置
    BAISHAN_API_KEY: str = "sk-"
    BAISHAN_MODEL: str = "DeepSeek-R1-0528-Qwen3-8B"
    BAISHAN_BASE_URL: str = "https://api.edgefn.net/v1"

    # STEPFUN 配置
    STEPFUN_API_KEY: str = "sk-"
    STEPFUN_MODEL: str = "step-1v-8k"
    STEPFUN_BASE_URL: str = "https://api.stepfun.com/v1"

    # MiniMax 配置
    MINIMAX_API_KEY: str = "sk-"
    MINIMAX_MODEL: str = "MiniMax-M2.7"
    MINIMAX_BASE_URL: str = "https://api.minimax.com/v1"

    # CR proxy OpenAI-compatible 配置（要求 stream=true）
    CR_PROXY_API_KEY: str = "sk-"
    CR_PROXY_MODEL: str = "gpt-5.3-codex-spark"
    CR_PROXY_BASE_URL: str = "http://3.93.42.33:3000/api/v1"

    # EPUB 分块配置
    EPUB_CHUNK_MAX_TOKENS: PositiveInt = 2000

    # 日志设置
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    LOG_FILE: Path | None = Path("./logs/engine.log")
    JSON_LOGS: bool = True

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "allow",  # 允许额外的字段
        "validate_default": True,
    }


# Create a global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Return the settings instance.

    Returns:
        Settings: The application settings.
    """
    return settings


def resolve_chunk_limit(limit: int | None = None, *, configured: int | None = None) -> int:
    """Resolve an explicit chunk limit before the configured environment value."""
    value = limit if limit is not None else settings.EPUB_CHUNK_MAX_TOKENS if configured is None else configured
    if type(value) is not int or value < 1:
        raise ValueError("chunk token limit must be a positive integer")
    return value
