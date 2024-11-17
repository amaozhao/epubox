from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # `.env.prod` takes priority over `.env`
        env_file=(".env", ".env.prod")
    )

    SQLALCHEMY_DATABASE_URL: str


# 获取数据库连接URL
settings = Settings()
