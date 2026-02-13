from __future__ import annotations

import logging
import os
from datetime import date
from logging import Logger
from logging.handlers import TimedRotatingFileHandler


def setup_logging(log_level: str) -> Logger:
    os.makedirs("logs", exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(log_level.upper())

    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    # Use TimedRotatingFileHandler for 7-day rotation
    filename = os.path.join("logs", "bot.log")
    file_handler = TimedRotatingFileHandler(
        filename,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    logger.addHandler(console)
    logger.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    return logger
