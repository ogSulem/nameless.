from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

NEXT = "⏭️ Следующий"
STOP = "🛑 Завершить"


def dialog_reply_kb() -> ReplyKeyboardMarkup:
    """Controls shown while a dialog is active.

    Labels are explicit: bare "⏭️"/"🛑" (the previous version) left users guessing
    what each button does.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=NEXT), KeyboardButton(text=STOP)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
        input_field_placeholder="Напишите сообщение…",
    )
