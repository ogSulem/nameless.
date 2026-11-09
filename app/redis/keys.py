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


def seek_display_key(tg_id: int) -> str:
    """The raw choice (male/female/any) for the admin card — the matching cache
    above is normalised, this one is what the person actually tapped."""
    return f"seek:display:{tg_id}"


# --------------------------------------------------------------------- online
def online_key(tg_id: int) -> str:
    return f"online:{tg_id}"


def ban_flag_key(tg_id: int) -> str:
    """Short-lived copy of `users.is_banned`: the ban gate runs on *every* update."""
    return f"ban:{tg_id}"


def seen_key(tg_id: int) -> str:
    """Throttles the `last_seen_at` UPDATE to once per few minutes."""
    return f"seen:{tg_id}"


# --------------------------------------------------------------------- locks
def lock_match(tg_id: int) -> str:
    return f"lock:match:{tg_id}"


# -------------------------------------------------------------------- dialog
def promo_attempt_key(tg_id: int) -> str:
    return f"promo:try:{tg_id}"


def referral_cap_key(tg_id: int, day: str) -> str:
    return f"ref:cap:{tg_id}:{day}"


def premium_pref_key(tg_id: int) -> str:
    return f"premium_pref:{int(tg_id)}"


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
def ui_message_id(tg_id: int) -> str:
    return f"ui:message_id:{tg_id}"


def ui_search_message_id(tg_id: int) -> str:
    return f"ui:search_message_id:{tg_id}"


# ------------------------------------------------------------------ payments
def payment_pending(tg_id: int) -> str:
    return f"payment:pending:{tg_id}"


# --------------------------------------------------------------------- misc
def rate(key: str) -> str:
    return f"rl:{key}"


def throttle(key: str) -> str:
    return f"throttle:{key}"
