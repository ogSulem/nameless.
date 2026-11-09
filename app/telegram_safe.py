from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

logger = logging.getLogger(__name__)


class UserBlockedBot(Exception):
    """The peer blocked the bot or deleted the account."""


# ---------------------------------------------------------------- render cache
# Telegram rejects an edit that does not change anything ("message is not
# modified"), so every redundant render costs a round trip and a visible flicker
# on slow mobile clients. Remembering what each message already shows turns a
# repeated tap into a no-op.
_RENDERED: OrderedDict[tuple[int, int], tuple[str, str]] = OrderedDict()
_RENDERED_MAX = 1024


def _markup_key(markup: Any | None) -> str:
    if markup is None:
        return ""
    # An explicitly empty inline keyboard and "no markup at all" both mean
    # "no buttons": treat them as the same state so a redundant clear is free.
    if not getattr(markup, "inline_keyboard", None) and not getattr(markup, "keyboard", None):
        if hasattr(markup, "inline_keyboard"):
            return ""
    dump = getattr(markup, "model_dump_json", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:  # pragma: no cover - unexpected aiogram object
            return repr(markup)
    return repr(markup)


def _remember(chat_id: int, message_id: int, text: str | None, markup: Any | None) -> None:
    key = (int(chat_id), int(message_id))
    previous = _RENDERED.get(key)
    if text is None and previous is not None:
        text = previous[0]
    _RENDERED[key] = ((text or ""), _markup_key(markup))
    _RENDERED.move_to_end(key)
    while len(_RENDERED) > _RENDERED_MAX:
        _RENDERED.popitem(last=False)


def _unchanged(chat_id: int, message_id: int, text: str | None, markup: Any | None) -> bool:
    entry = _RENDERED.get((int(chat_id), int(message_id)))
    if entry is None:
        return False
    current_text, current_markup = entry
    if text is not None and text != current_text:
        return False
    return _markup_key(markup) == current_markup


def _forget(chat_id: int, message_id: int) -> None:
    _RENDERED.pop((int(chat_id), int(message_id)), None)


def reset_render_cache() -> None:
    """Tests reuse chat ids and message ids; the cache must not leak between them."""
    _RENDERED.clear()


async def _sleep_retry(e: TelegramRetryAfter) -> None:
    delay = float(getattr(e, "retry_after", 1) or 1)
    await asyncio.sleep(min(max(delay, 1.0), 30.0))


async def _sleep_backoff(attempt: int) -> None:
    await asyncio.sleep(min(0.5 * (2 ** max(0, attempt)), 8.0))


def _is_parse_error(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return "can't parse entities" in text or "can't find end" in text or "unsupported start tag" in text


def _is_gone(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return (
        "message to edit not found" in text
        or "message can't be edited" in text
        or "message_id_invalid" in text
        or "message is not modified" in text
    )


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Any | None = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = True,
):
    """Send a message; returns None when the peer is unreachable, raises UserBlockedBot."""
    for attempt in range(3):
        try:
            sent = await bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            if sent is not None:
                _remember(chat_id, sent.message_id, text, reply_markup)
            return sent
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            raise
        except TelegramForbiddenError as e:
            raise UserBlockedBot(str(e)) from e
        except TelegramBadRequest as e:
            if parse_mode and _is_parse_error(e):
                try:
                    sent = await bot.send_message(
                        chat_id, text, reply_markup=reply_markup, parse_mode=None,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    if sent is not None:
                        _remember(chat_id, sent.message_id, text, reply_markup)
                    return sent
                except Exception:
                    raise
            if "chat not found" in str(e).lower():
                raise UserBlockedBot(str(e)) from e
            raise
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            raise


async def safe_copy_message(bot: Bot, chat_id: int, from_chat_id: int, message_id: int):
    for attempt in range(3):
        try:
            return await bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            raise
        except TelegramForbiddenError as e:
            raise UserBlockedBot(str(e)) from e
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                raise UserBlockedBot(str(e)) from e
            raise
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            raise


async def safe_send_photo(
    bot: Bot,
    chat_id: int,
    photo: Any,
    caption: str | None = None,
    parse_mode: str | None = None,
    reply_markup: Any | None = None,
):
    for attempt in range(3):
        try:
            return await bot.send_photo(
                chat_id, photo=photo, caption=caption, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            raise
        except TelegramForbiddenError as e:
            raise UserBlockedBot(str(e)) from e
        except TelegramBadRequest as e:
            if parse_mode and _is_parse_error(e):
                return await bot.send_photo(chat_id, photo=photo, caption=caption, reply_markup=reply_markup)
            raise
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            raise


async def safe_send_document(
    bot: Bot, chat_id: int, document: Any, caption: str | None = None, parse_mode: str | None = None
):
    for attempt in range(3):
        try:
            return await bot.send_document(chat_id, document=document, caption=caption, parse_mode=parse_mode)
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            raise
        except TelegramForbiddenError as e:
            raise UserBlockedBot(str(e)) from e
        except TelegramBadRequest as e:
            if parse_mode and _is_parse_error(e):
                return await bot.send_document(chat_id, document=document, caption=caption)
            raise
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            raise


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    for attempt in range(3):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            _forget(chat_id, message_id)
            return True
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            return False
        except (TelegramBadRequest, TelegramForbiddenError):
            _forget(chat_id, message_id)  # already gone
            return False
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            return False
        except Exception:
            logger.exception("tg_delete_failed chat_id=%s mid=%s", chat_id, message_id)
            return False
    return False


async def safe_edit_message_text(
    bot: Bot, chat_id: int, message_id: int, text: str, reply_markup: Any | None = None, parse_mode: str | None = None
) -> bool:
    if _unchanged(chat_id, message_id, text, reply_markup):
        return True  # nothing to change: no API call, no flicker
    for attempt in range(3):
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode
            )
            _remember(chat_id, message_id, text, reply_markup)
            return True
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            return False
        except TelegramBadRequest as e:
            if _is_gone(e):
                if "message is not modified" in str(e).lower():
                    _remember(chat_id, message_id, text, reply_markup)
                    return True
                _forget(chat_id, message_id)
                return False
            if parse_mode and _is_parse_error(e):
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
                    )
                    _remember(chat_id, message_id, text, reply_markup)
                    return True
                except Exception:
                    return False
            return False
        except TelegramForbiddenError:
            return False
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            return False
        except Exception:
            logger.exception("tg_edit_failed chat_id=%s mid=%s", chat_id, message_id)
            return False
    return False


async def safe_edit_message_reply_markup(
    bot: Bot, chat_id: int, message_id: int, reply_markup: Any | None = None
) -> bool:
    for attempt in range(3):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
            return True
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            return False
        except (TelegramBadRequest, TelegramForbiddenError):
            _forget(chat_id, message_id)  # already gone
            return False
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            return False
    return False


async def safe_answer_callback(callback, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass
