from __future__ import annotations

CITY_GLOBAL = "global"


# ----------------------------------------------------------------- queues (v2)
def queue_key(gender: str, city: str | None) -> str:
    city_part = (city or CITY_GLOBAL).strip().lower().replace(" ", "_")
    return f"v2:queue:{gender}:{city_part}"


def queue_member_key(tg_id: int) -> str:
    """Which queue a user currently sits in (O(1) dequeue instead of 4x LREM)."""
    return f"v2:queue:member:{tg_id}"


def seek_gender_key(tg_id: int) -> str:
    return f"v2:seek_gender:{tg_id}"


# --------------------------------------------------------------------- online
def online_key(tg_id: int) -> str:
    return f"online:{tg_id}"


# --------------------------------------------------------------------- locks
def lock_match(tg_id: int) -> str:
    return f"lock:match:{tg_id}"


def lock_finish_dialog(tg_id: int) -> str:
    return f"lock:finish_dialog:{tg_id}"


def lock_rating(tg_id: int) -> str:
    return f"lock:rating:{tg_id}"


# -------------------------------------------------------------------- dialog
def active_dialog(tg_id: int) -> str:
    return f"active_dialog:{tg_id}"


def dialog_partner_tg(dialog_id: int, my_tg_id: int) -> str:
    return f"dialog:{dialog_id}:partner_tg:{my_tg_id}"


def last_partner(tg_id: int) -> str:
    return f"last_partner:{tg_id}"


def sender_human_detected(dialog_id: int, sender_tg_id: int) -> str:
    return f"dialog:{dialog_id}:sender:{sender_tg_id}:human"


# ------------------------------------------------------------------- ratings
def pending_rating(tg_id: int) -> str:
    return f"pending_rating:{tg_id}"


def pending_rating_has_photos(tg_id: int) -> str:
    return f"pending_rating_has_photos:{tg_id}"


def pending_rating_partner(tg_id: int) -> str:
    return f"pending_rating_partner:{tg_id}"


def pending_rating_action(tg_id: int) -> str:
    return f"pending_rating_action:{tg_id}"


def pending_rating_step(tg_id: int) -> str:
    return f"pending_rating_step:{tg_id}"


def appearance_rating_required(tg_id: int, dialog_id: int) -> str:
    return f"appearance_rating_required:{tg_id}:{dialog_id}"


# ------------------------------------------------------------------------ ui
def user_id_by_tg(tg_id: int) -> str:
    return f"user:id_by_tg:{tg_id}"


def user_rating_chat_by_tg(tg_id: int) -> str:
    return f"user:rating_chat_by_tg:{tg_id}"


def profile_text(tg_id: int) -> str:
    return f"profile:text:{tg_id}"


def ui_message_id(tg_id: int) -> str:
    return f"ui:message_id:{tg_id}"


def ui_search_message_id(tg_id: int) -> str:
    return f"ui:search_message_id:{tg_id}"


def ui_rating_message_id(tg_id: int) -> str:
    return f"ui:rating_message_id:{tg_id}"


# ------------------------------------------------------------------ payments
def payment_pending(tg_id: int) -> str:
    return f"payment:pending:{tg_id}"


def payment_processed(provider: str, external_id: str) -> str:
    return f"payment:processed:{provider}:{external_id}"


# --------------------------------------------------------------------- misc
def rate(key: str) -> str:
    return f"rl:{key}"


def throttle(key: str) -> str:
    return f"throttle:{key}"
