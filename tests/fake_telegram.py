"""A minimal in-process Telegram stand-in.

We intentionally exercise the *real* dispatcher, middlewares, FSM storage and
handlers. Only the network boundary (`Bot.__call__`) is faked, which keeps the
tests fast while still catching routing/DI/callback-data regressions — exactly
the class of bug that broke this bot before (`Callable` typo, bot-id invoices).
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    CreateInvoiceLink,
    DeleteMessage,
    EditMessageText,
    GetMe,
    SendChatAction,
    SendMessage,
    SendPhoto,
    SetMyCommands,
)
from aiogram.types import Chat, Message


def fresh_router():
    """`build_router()` re-uses module-level routers, which aiogram refuses to attach twice."""
    from app.handlers import (
        admin,
        commands,
        dialog,
        fallback,
        rating,
        search,
        subscription,
    )
    from app.handlers.router import build_router

    for module in (commands, admin, search, rating, subscription, dialog, fallback):
        module.router._parent_router = None  # noqa: SLF001 - test-only detach
    return build_router()


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Any] = []
        self.edited: list[Any] = []
        self.deleted: list[int] = []
        self.text_log: list[str] = []
        # What each chat would actually show right now: (chat_id, message_id) -> text.
        self.live: dict[tuple[int, int], str] = {}
        self.answers: list[str | None] = []
        self.copied: list[Any] = []
        self.invoices: list[Any] = []
        self._next_id = 1000
        # Chats where the bot is blocked: every send fails like real Telegram does.
        self.blocked_chats: set[int] = set()

    async def close(self) -> None:  # pragma: no cover - nothing to close
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        chat_id = getattr(method, "chat_id", None)
        if chat_id in self.blocked_chats and not isinstance(
            method, (GetMe, SetMyCommands, AnswerCallbackQuery, DeleteMessage)
        ):
            from aiogram.exceptions import TelegramForbiddenError

            raise TelegramForbiddenError(
                method=method, message="Forbidden: bot was blocked by the user"
            )
        self.sent.append(method)
        self._next_id += 1

        if isinstance(method, GetMe):
            return method.__returning__.model_construct(id=bot.id, is_bot=True, first_name="TestBot",
                                                       username="testbot")
        if isinstance(method, SetMyCommands):
            return True
        if isinstance(method, SendMessage):
            self.text_log.append(method.text or "")
            self.live[(method.chat_id, self._next_id)] = method.text or ""
            return Message.model_construct(
                message_id=self._next_id,
                date=None,
                chat=Chat.model_construct(id=method.chat_id, type="private"),
                text=method.text,
                from_user=None,
            )
        if isinstance(method, EditMessageText):
            self.text_log.append(method.text or "")
            return Message.model_construct(
                message_id=1,
                date=None,
                chat=Chat.model_construct(id=1, type="private"),
                text=method.text,
            )
        if isinstance(method, CopyMessage):
            self.copied.append(method)
            return Message.model_construct(
                message_id=self._next_id,
                date=None,
                chat=Chat.model_construct(id=method.chat_id, type="private"),
            )
        if isinstance(method, (SendPhoto,)):
            return Message.model_construct(
                message_id=self._next_id,
                date=None,
                chat=Chat.model_construct(id=method.chat_id, type="private"),
            )
        if isinstance(method, DeleteMessage):
            self.deleted.append(method.message_id)
            self.live.pop((method.chat_id, method.message_id), None)
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method.text)
            return True
        if isinstance(method, SendChatAction):
            return True
        if isinstance(method, CreateInvoiceLink):
            self.invoices.append(method)
            return "https://t.me/$test-invoice"
        return True

    # ---------------------------------------------------------------- helpers
    def texts(self) -> list[str]:
        return list(self.text_log)

    def last_text(self) -> str:
        assert self.text_log, "the bot rendered nothing"
        return self.text_log[-1]

    def live_count(self, chat_id: int | None = None) -> int:
        if chat_id is None:
            return len(self.live)
        return sum(1 for cid, _ in self.live if cid == chat_id)

    def live_text(self, chat_id: int) -> str:
        return "\n".join(text for (cid, _), text in self.live.items() if cid == chat_id)

    def all_text(self) -> str:
        return "\n".join(self.text_log)

    def markup_buttons(self) -> list[str]:
        """All callback_data strings of the last inline keyboard we sent/edited."""
        payloads: list[Any] = []
        for method in [*self.sent, *self.edited]:
            markup = getattr(method, "reply_markup", None)
            if markup is not None:
                payloads.append(markup)
        assert payloads, "no keyboard was sent"
        markup = payloads[-1]
        data: list[str] = []
        for row in getattr(markup, "inline_keyboard", []) or []:
            for button in row:
                if button.callback_data:
                    data.append(button.callback_data)
        return data
