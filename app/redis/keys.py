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
