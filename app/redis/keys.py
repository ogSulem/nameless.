from __future__ import annotations


def queue_city(city: str) -> str:
    return f"queue:city:{city.lower()}"


def queue_global() -> str:
    return "queue:global"


def queue_premium_city(city: str) -> str:
    return f"queue:premium:city:{city.lower()}"


def queue_premium_global() -> str:
    return "queue:premium:global"


def lock_match(user_id: int) -> str:
    return f"lock:match:{user_id}"


def lock_finish_dialog(user_id: int) -> str:
    return f"lock:finish_dialog:{user_id}"


def active_dialog(user_id: int) -> str:
    return f"active_dialog:{user_id}"


def pending_rating(user_id: int) -> str:
    return f"pending_rating:{user_id}"


def pending_rating_has_photos(user_id: int) -> str:
    return f"pending_rating_has_photos:{user_id}"


def pending_rating_partner(user_id: int) -> str:
    return f"pending_rating_partner:{user_id}"


def pending_rating_action(user_id: int) -> str:
    return f"pending_rating_action:{user_id}"


def pending_rating_step(user_id: int) -> str:
    return f"pending_rating_step:{user_id}"


def ui_rating_message_id(user_id: int) -> str:
    return f"ui:rating_message_id:{user_id}"


def ui_search_message_id(user_id: int) -> str:
    return f"ui:search_message_id:{user_id}"


def profile_text(user_id: int) -> str:
    return f"profile:text:{user_id}"


def appearance_rating_required(user_id: int, dialog_id: int) -> str:
    return f"appearance_rating_required:{user_id}:{dialog_id}"


def dialog_sender_human_detected(dialog_id: int, sender_tg_id: int) -> str:
    return f"dialog:{dialog_id}:sender:{sender_tg_id}:human_detected"


def last_partner(user_id: int) -> str:
    return f"last_partner:{user_id}"


def user_id_by_tg(tg_id: int) -> str:
    return f"user:id_by_tg:{tg_id}"


def user_rating_chat_by_tg(tg_id: int) -> str:
    return f"user:rating_chat_by_tg:{tg_id}"


def dialog_partner_tg(dialog_id: int, my_tg_id: int) -> str:
    return f"dialog:{dialog_id}:partner_tg:{my_tg_id}"


def payment_pending(user_id: int) -> str:
    return f"payment:pending:{user_id}"


def payment_processed(provider: str, payment_id: str) -> str:
    # provider examples: "telegram", "yookassa"
    return f"payment:processed:{provider}:{payment_id}"
