from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter


logger = logging.getLogger(__name__)


async def _sleep_retry(e: TelegramRetryAfter) -> None:
    delay = float(getattr(e, "retry_after", 1) or 1)
    if delay < 0:
        delay = 1
    if delay > 30:
        delay = 30
    await asyncio.sleep(delay)


async def _sleep_backoff(attempt: int) -> None:
    delay = 0.5 * (2**max(0, attempt))
    if delay > 8:
        delay = 8
    await asyncio.sleep(delay)


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    for attempt in range(3):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            return False
        except TelegramBadRequest:
            return False
        except TelegramForbiddenError:
            return False
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            logger.exception("tg_delete_network_error chat_id=%s mid=%s", chat_id, message_id)
            return False
        except Exception:
            logger.exception("tg_delete_failed chat_id=%s mid=%s", chat_id, message_id)
            return False


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Any | None = None,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
):
    for attempt in range(3):
        try:
            return await bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            raise
        except TelegramBadRequest as e:
            # Common in admin/alerts when Markdown is malformed (e.g. underscores).
            # Retry once without parse_mode to avoid crashing the handler.
            if parse_mode and attempt == 0 and "can't parse entities" in str(e).lower():
                try:
                    return await bot.send_message(
                        chat_id,
                        text,
                        reply_markup=reply_markup,
                        parse_mode=None,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                except Exception:
                    raise
            raise


async def safe_send_document(
    bot: Bot,
    chat_id: int,
    document: Any,
    caption: str | None = None,
    parse_mode: str | None = None,
):
    for attempt in range(3):
        try:
            return await bot.send_document(chat_id, document=document, caption=caption, parse_mode=parse_mode)
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            raise
        except TelegramBadRequest as e:
            if parse_mode and attempt == 0 and "can't parse entities" in str(e).lower():
                return await bot.send_document(chat_id, document=document, caption=caption, parse_mode=None)
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
):
    for attempt in range(3):
        try:
            return await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode=parse_mode)
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            raise
        except TelegramBadRequest as e:
            if parse_mode and attempt == 0 and "can't parse entities" in str(e).lower():
                return await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode=None)
            raise
        except (TelegramNetworkError, ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            raise


async def safe_edit_message_text(bot: Bot, chat_id: int, message_id: int, text: str, reply_markup: Any | None = None) -> bool:
    for attempt in range(3):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
            return True
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            return False
        except TelegramBadRequest as e:
            s = str(e).lower()
            if "message is not modified" in s:
                return True
            if "message to edit not found" in s:
                return False
            return False
        except TelegramForbiddenError:
            return False
        except TelegramNetworkError:
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            logger.exception("tg_edit_network_error chat_id=%s mid=%s", chat_id, message_id)
            return False
        except (ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            logger.exception("tg_edit_network_error chat_id=%s mid=%s", chat_id, message_id)
            return False
        except Exception:
            logger.exception("tg_edit_failed chat_id=%s mid=%s", chat_id, message_id)
            return False


async def safe_edit_message_reply_markup(bot: Bot, chat_id: int, message_id: int, reply_markup: Any | None = None) -> bool:
    for attempt in range(3):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
            return True
        except TelegramRetryAfter as e:
            if attempt < 2:
                await _sleep_retry(e)
                continue
            return False
        except TelegramBadRequest as e:
            s = str(e).lower()
            if "message is not modified" in s:
                return True
            return False
        except TelegramForbiddenError:
            return False
        except TelegramNetworkError:
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            logger.exception("tg_edit_markup_network_error chat_id=%s mid=%s", chat_id, message_id)
            return False
        except (ConnectionResetError, TimeoutError):
            if attempt < 2:
                await _sleep_backoff(attempt)
                continue
            logger.exception("tg_edit_markup_network_error chat_id=%s mid=%s", chat_id, message_id)
            return False
        except Exception:
            logger.exception("tg_edit_markup_failed chat_id=%s mid=%s", chat_id, message_id)
            return False
