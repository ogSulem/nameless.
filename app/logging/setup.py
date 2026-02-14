import logging
import os
import re
from datetime import date
from logging import Logger
from logging.handlers import TimedRotatingFileHandler

from app.logging.context import UpdateIdFilter


class SecretMaskingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) > 4]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        for s in self._secrets:
            if s in msg:
                msg = msg.replace(s, "*" * 8)
        
        # Also mask common patterns like bot tokens or passwords in DSNs
        msg = re.sub(r"([0-9]{8,12}:[a-zA-Z0-9_-]{20,})", "TOKEN_MASKED", msg)
        msg = re.sub(r":([^@/]+)@([^/]+):(\d+)", r":***@\2:\3", msg) # DSN password
        
        record.msg = msg
        return True


def setup_logging(log_level: str) -> Logger:
    # Gather potential secrets to mask
    secrets_to_mask = []
    for env_var in ["BOT_TOKEN", "DB_PASSWORD", "REDIS_PASSWORD", "YOOKASSA_SECRET_KEY", "OPENAI_API_KEY"]:
        val = os.getenv(env_var)
        if val:
            secrets_to_mask.append(val)

    logger = logging.getLogger()
    logger.setLevel(log_level.upper())

    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | upd=%(update_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    upd_filt = UpdateIdFilter()
    sec_filt = SecretMaskingFilter(secrets_to_mask)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(upd_filt)
    console.addFilter(sec_filt)

    logger.addHandler(console)

    # Cloud-first: log to stdout by default. Enable file logging explicitly (useful on VPS).
    log_to_file = os.getenv("LOG_TO_FILE", "").strip().lower() in {"1", "true", "yes", "on"}
    if log_to_file:
        log_dir = os.getenv("LOG_DIR", "logs").strip() or "logs"
        try:
            retention_days = int(os.getenv("LOG_RETENTION_DAYS", "7").strip() or "7")
        except Exception:
            retention_days = 7
        if retention_days < 1:
            retention_days = 1

        os.makedirs(log_dir, exist_ok=True)
        filename = os.path.join(log_dir, "bot.log")
        file_handler = TimedRotatingFileHandler(
            filename,
            when="midnight",
            interval=1,
            backupCount=retention_days,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(upd_filt)
        file_handler.addFilter(sec_filt)
        logger.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    return logger
