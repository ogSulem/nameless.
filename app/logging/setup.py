import logging
import os
import re
from logging import Logger
from logging.handlers import TimedRotatingFileHandler

from app.logging.context import UpdateIdFilter


class _Masker:
    """Shared masking rules for tokens, DSN passwords and configured secrets."""

    _TOKEN_RE = re.compile(r"([0-9]{8,12}:[a-zA-Z0-9_-]{20,})")
    _DSN_RE = re.compile(r":([^@/]+)@([^/]+):(\d+)")

    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets = [s for s in (secrets or []) if s and len(s) > 4]

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "*" * 8)
        text = self._TOKEN_RE.sub("TOKEN_MASKED", text)
        return self._DSN_RE.sub(r":***@\2:\3", text)


class SecretMaskingFilter(logging.Filter):
    """Mask secrets that arrive as `%s` arguments.

    Values interpolated at format time never pass through `record.msg`, so
    `logger.info("token=%s", token)` used to slip past the mask. The message
    template itself is left alone — rewriting it would eat its placeholders —
    and the finished line is masked by `MaskingFormatter`.
    """

    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self._masker = _Masker(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._masker.mask(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: self._masker.mask(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


class MaskingFormatter(logging.Formatter):
    """Mask secrets in the fully rendered line (f-strings, exception text, ...)."""

    def __init__(self, *args, secrets: list[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._masker = _Masker(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return self._masker.mask(super().format(record))


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

    fmt = MaskingFormatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | upd=%(update_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        secrets=secrets_to_mask,
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
