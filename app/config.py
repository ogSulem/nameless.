from __future__ import annotations

import os
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Fail-fast philosophy: a broken deployment must crash at startup, not at 3am.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -----------------------------------------------------------
    bot_token: str = Field(alias="BOT_TOKEN")
    admins: str = Field(default="", alias="ADMINS")          # full access
    moderators: str = Field(default="", alias="MODERATORS")  # users / complaints / bans
    support_users: str = Field(default="", alias="SUPPORT")  # read-only
    alerts_chat_id: int = Field(default=0, alias="ALERTS_CHAT_ID")

    # --- Storage ------------------------------------------------------------
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

    # --- Service ------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    port: int = Field(default=8080, alias="PORT")
    media_root: str = Field(default="/app/media", alias="MEDIA_ROOT")

    # --- Matching / dialogs -------------------------------------------------
    online_ttl_s: int = Field(default=900, alias="ONLINE_TTL_S")
    dialog_max_hours: int = Field(default=12, alias="DIALOG_MAX_HOURS")
    premium_min_rating: float = Field(default=7.0, alias="PREMIUM_MIN_RATING")
    premium_min_appearance: float = Field(default=7.0, alias="PREMIUM_MIN_APPEARANCE")

    # --- Growth -------------------------------------------------------------
    # Premium days for inviting a friend. 0 disables the reward (invites still work).
    referral_reward_days: int = Field(default=30, alias="REFERRAL_REWARD_DAYS")
    # Welcome bonus for the invited person (0 = off). Meant for the launch week.
    referral_welcome_days: int = Field(default=0, alias="REFERRAL_WELCOME_DAYS")
    # Anti-farming: how many referral rewards one inviter can earn per day.
    referral_max_per_day: int = Field(default=5, alias="REFERRAL_MAX_PER_DAY")

    # Promo codes (free Premium campaigns). Wrong guesses per user per hour.
    promo_attempts_per_hour: int = Field(default=10, alias="PROMO_ATTEMPTS_PER_HOUR")

    # --- Reliability --------------------------------------------------------
    # Sized for one polling instance; raise together with the DB max_connections.
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: int = Field(default=10, alias="DB_POOL_TIMEOUT")
    db_pool_recycle_s: int = Field(default=1800, alias="DB_POOL_RECYCLE_S")
    redis_socket_timeout: int = Field(default=5, alias="REDIS_SOCKET_TIMEOUT")
    redis_connect_timeout: int = Field(default=5, alias="REDIS_CONNECT_TIMEOUT")
    redis_max_connections: int = Field(default=50, alias="REDIS_MAX_CONNECTIONS")
    telegram_timeout_s: int = Field(default=30, alias="TELEGRAM_TIMEOUT_S")

    # --- Anti-flood ---------------------------------------------------------
    messages_per_10s: int = Field(default=12, alias="MESSAGES_PER_10S")
    photos_per_minute: int = Field(default=6, alias="PHOTOS_PER_MINUTE")

    # --- Ratings ------------------------------------------------------------
    rating_prior_mean: float = Field(default=5.0, alias="RATING_PRIOR_MEAN")
    rating_prior_weight: int = Field(default=5, alias="RATING_PRIOR_WEIGHT")
    rating_min_count: int = Field(default=3, alias="RATING_MIN_COUNT")
    rating_window: int = Field(default=100, alias="RATING_WINDOW")

    # --- Vision -------------------------------------------------------------
    vision_enabled: bool = Field(default=True, alias="VISION_ENABLED")
    vision_min_conf: float = Field(default=0.65, alias="VISION_MIN_CONF")
    vision_max_side: int = Field(default=640, alias="VISION_MAX_SIDE")
    vision_timeout_s: float = Field(default=4.0, alias="VISION_TIMEOUT_S")
    vision_concurrency: int = Field(default=2, alias="VISION_CONCURRENCY")

    # --- Payments -----------------------------------------------------------
    payments_provider_token: str = Field(default="", alias="PAYMENTS_PROVIDER_TOKEN")
    yookassa_shop_id: str = Field(default="", alias="YOOKASSA_SHOP_ID")
    yookassa_secret_key: str = Field(default="", alias="YOOKASSA_SECRET_KEY")
    yookassa_webhook_secret: str = Field(default="", alias="YOOKASSA_WEBHOOK_SECRET")
    subscription_price_rub: int = Field(default=499, alias="SUBSCRIPTION_PRICE_RUB")
    subscription_days: int = Field(default=30, alias="SUBSCRIPTION_DAYS")

    # --- Legal / retention --------------------------------------------------
    min_age: int = Field(default=18, alias="MIN_AGE")
    terms_version: str = Field(default="1.1", alias="TERMS_VERSION")
    operator_name: str = Field(default="", alias="OPERATOR_NAME")
    support_contact: str = Field(default="", alias="SUPPORT_CONTACT")
    retention_message_days: int = Field(default=30, alias="RETENTION_MESSAGE_DAYS")
    retention_complaint_days: int = Field(default=180, alias="RETENTION_COMPLAINT_DAYS")
    retention_audit_days: int = Field(default=365, alias="RETENTION_AUDIT_DAYS")

    # ------------------------------------------------------------------ basics
    @field_validator("bot_token")
    @classmethod
    def _check_token(cls, v: str) -> str:
        v = (v or "").strip()
        if ":" not in v or not v.split(":", 1)[0].isdigit():
            raise ValueError("BOT_TOKEN must look like '<digits>:<secret>'")
        return v

    @model_validator(mode="after")
    def _check_admin_configuration(self) -> Settings:
        if not self.admins.strip():
            # Admin surface must never exist without an explicit allow-list.
            if self.alerts_chat_id:
                raise ValueError(
                    "ADMINS is empty but ALERTS_CHAT_ID is set: refusing to start with a public admin surface"
                )
        if self.yookassa_shop_id and not self.yookassa_secret_key:
            raise ValueError("YOOKASSA_SHOP_ID is set without YOOKASSA_SECRET_KEY")
        if self.yookassa_secret_key and not self.yookassa_shop_id:
            raise ValueError("YOOKASSA_SECRET_KEY is set without YOOKASSA_SHOP_ID")
        if bool(self.yookassa_shop_id) and bool(self.payments_provider_token):
            # Both configured: prefer Telegram Payments (receipts handled by the provider).
            pass
        return self

    # ------------------------------------------------------------------ derived
    @property
    def database_dsn(self) -> str:
        db_url = (os.getenv("DATABASE_URL") or "").strip()
        if db_url:
            if db_url.startswith("postgres://"):
                db_url = "postgresql+asyncpg://" + db_url[len("postgres://"):]
            elif db_url.startswith("postgresql://"):
                db_url = "postgresql+asyncpg://" + db_url[len("postgresql://"):]
            return db_url

        if not (self.db_host and self.db_user and self.db_password and self.db_name):
            return "postgresql+asyncpg://nameless:nameless@localhost:5432/nameless"

        return (
            f"postgresql+asyncpg://{quote(self.db_user, safe='')}:{quote(self.db_password, safe='')}"
            f"@{self.db_host}:{self.db_port or 5432}/{self.db_name}"
        )

    @staticmethod
    def _parse_ids(raw: str) -> frozenset[int]:
        out: set[int] = set()
        for chunk in (raw or "").replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                out.add(int(chunk))
            except ValueError as exc:  # pragma: no cover - config error path
                raise ValueError(f"ADMINS/MODERATORS/SUPPORT contain a non-numeric id: {chunk!r}") from exc
        return frozenset(out)

    @property
    def admin_ids(self) -> frozenset[int]:
        return self._parse_ids(self.admins)

    @property
    def moderator_ids(self) -> frozenset[int]:
        return self._parse_ids(self.moderators)

    @property
    def support_ids(self) -> frozenset[int]:
        return self._parse_ids(self.support_users)

    @property
    def staff_ids(self) -> frozenset[int]:
        """Anyone with a panel role — staff can never be silently locked out by a ban."""
        return self.admin_ids | self.moderator_ids | self.support_ids

    @property
    def alerts_target_ids(self) -> frozenset[int]:
        """Where operational alerts go. Falls back to admins."""
        if self.alerts_chat_id:
            return frozenset({int(self.alerts_chat_id)})
        return self.admin_ids

    @property
    def payments_enabled(self) -> bool:
        return bool(self.payments_provider_token or (self.yookassa_shop_id and self.yookassa_secret_key))

    @property
    def premium_price_kopecks(self) -> int:
        return max(1, int(self.subscription_price_rub)) * 100
