from __future__ import annotations

from typing import FrozenSet

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    admins: str = Field(default="", alias="ADMINS")
    alerts_chat_id: int = Field(default=0, alias="ALERTS_CHAT_ID")

    db_host: str | None = Field(default=None, alias="DB_HOST")
    db_port: int | None = Field(default=None, alias="DB_PORT")
    db_name: str | None = Field(default=None, alias="DB_NAME")
    db_user: str | None = Field(default=None, alias="DB_USER")
    db_password: str | None = Field(default=None, alias="DB_PASSWORD")

    redis_host: str | None = Field(default=None, alias="REDIS_HOST")
    redis_port: int | None = Field(default=None, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")
    redis_username: str | None = Field(default=None, alias="REDIS_USERNAME")
    redis_password: str | None = Field(default=None, alias="REDIS_PASSWORD")
    redis_url: str | None = Field(default=None, alias="REDIS_URL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    media_root: str = Field(default="/app/media", alias="MEDIA_ROOT")

    vision_min_conf: float = Field(default=0.7, alias="VISION_MIN_CONF")
    vision_max_side: int = Field(default=640, alias="VISION_MAX_SIDE")
    vision_timeout_s: float = Field(default=4.0, alias="VISION_TIMEOUT_S")
    vision_concurrency: int = Field(default=2, alias="VISION_CONCURRENCY")

    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    hf_token: str | None = Field(default=None, alias="HF_TOKEN")

    payments_provider_token: str = Field(default="", alias="PAYMENTS_PROVIDER_TOKEN")
    yookassa_shop_id: str = Field(default="", alias="YOOKASSA_SHOP_ID")
    yookassa_secret_key: str = Field(default="", alias="YOOKASSA_SECRET_KEY")
    subscription_price_rub: int = Field(default=500, alias="SUBSCRIPTION_PRICE_RUB")
    subscription_days: int = Field(default=30, alias="SUBSCRIPTION_DAYS")

    @property
    def database_dsn(self) -> str:
        # Priority 1: Use DATABASE_URL if available (common in Railway/Heroku)
        import os
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
            elif db_url.startswith("postgresql://"):
                db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return db_url

        # Priority 2: Build from individual components
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def admins_set(self) -> FrozenSet[int]:
        if not self.admins.strip():
            return frozenset()
        return frozenset(int(x.strip()) for x in self.admins.split(",") if x.strip())

    @property
    def alerts_target_ids(self) -> FrozenSet[int]:
        if self.alerts_chat_id:
            return frozenset({int(self.alerts_chat_id)})
        return self.admins_set
