from __future__ import annotations

import logging

from app.logging.setup import MaskingFormatter, SecretMaskingFilter

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
SECRET = "sup3rs3cret"


def _record(msg, args=()):
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)


def test_token_inside_lazy_args_is_masked():
    """`logger.info("token=%s", token)` must not leak the token."""
    record = _record("invoice failed token=%s", (TOKEN,))
    SecretMaskingFilter([SECRET]).filter(record)
    assert TOKEN not in record.getMessage()
    assert "TOKEN_MASKED" in record.getMessage()


def test_formatter_masks_the_finished_line():
    record = _record("connect postgres://user:%s@db:5432/nameless", (SECRET,))
    SecretMaskingFilter([SECRET]).filter(record)
    line = MaskingFormatter("%(message)s", secrets=[SECRET]).format(record)
    assert SECRET not in line
    assert "postgres://user:***@db:5432/nameless" in line


def test_placeholders_survive_masking():
    """Masking must never eat a `%s`: that would break the log call itself."""
    record = _record("dsn=%s", (SECRET,))
    SecretMaskingFilter([SECRET]).filter(record)
    assert record.getMessage() == "dsn=" + "*" * 8


def test_plain_messages_are_untouched():
    record = _record("match_created dialog_id=%s", (42,))
    SecretMaskingFilter([SECRET]).filter(record)
    assert record.getMessage() == "match_created dialog_id=42"
