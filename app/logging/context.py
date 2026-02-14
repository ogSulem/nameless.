from __future__ import annotations

import contextvars
import logging


_update_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("update_id", default=None)


def set_update_id(update_id: int | str | None) -> None:
    if update_id is None:
        _update_id_var.set(None)
        return
    _update_id_var.set(str(update_id))


def clear_update_id() -> None:
    _update_id_var.set(None)


def get_update_id() -> str | None:
    return _update_id_var.get()


class UpdateIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.update_id = get_update_id() or "-"
        return True
