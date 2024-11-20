from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # `.env.prod` takes priority over `.env`
        env_file=(".env", ".env.prod")
    )

    SQLALCHEMY_DATABASE_URL: str
    SECRET_KEY: str
    
    # Translation service API keys
    OPENAI_API_KEY: str = ""
    MISTRAL_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    
    # Translation service models
    OPENAI_MODEL: str = "gpt-3.5-turbo"
    MISTRAL_MODEL: str = "mistral-large-latest"


# 获取数据库连接URL
settings = Settings()
