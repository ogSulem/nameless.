from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

def payment_ui_kb(invoice_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=invoice_url)],
            [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="check_payment")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_payment")],
        ]
    )
