"""
Configuration settings for the Epubox application.
"""

from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 基础配置
    PROJECT_NAME: str = "Epubox"
    DEBUG: bool = True

    # 模型提供商配置
    MODEL_PROVIDER: Literal["mistral", "openai", "deepseek", "kimi"] = "mistral"

    # OpenAI 配置
    OPENAI_API_KEY: str = "your-api-key-here"
    OPENAI_API_BASE: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o"

    # Mistral 配置
    MISTRAL_API_KEY: str = "your-api-key-here"
    # MISTRAL_MODEL: str = "devstral-small-2505"
    MISTRAL_MODEL: str = "mistral-medium-latest"

    # Deepseek引擎配置
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_API_KEY: str = "your-api-key-here"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # Kimi引擎配置
    KIMI_BASE_URL: str = "https://api.moonshot.cn/v1"
    KIMI_API_KEY: str = "your-api-key-here"
    KIMI_MODEL: str = "kimi-latest"

    # Gemini 引擎配置
    GEMINI_API_KEY: str = "your-api-key-here"
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # 日志设置
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    LOG_FILE: Optional[Path] = Path("./logs/engine.log")
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
