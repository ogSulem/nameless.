from __future__ import annotations

from enum import IntEnum

from app.config import Settings


class Role(IntEnum):
    """Admin surface roles. Higher value == more permissions."""

    support = 10      # read-only: stats, user card, complaints
    moderator = 20    # + ban / mute / dismiss complaints / rating reset
    admin = 30        # + premium grants, payments, audit, everything


ROLE_TITLES: dict[Role, str] = {
    Role.support: "поддержка",
    Role.moderator: "модератор",
    Role.admin: "админ",
}


def role_for(settings: Settings, tg_id: int | None) -> Role | None:
    if tg_id is None:
        return None
    if int(tg_id) in settings.admin_ids:
        return Role.admin
    if int(tg_id) in settings.moderator_ids:
        return Role.moderator
    if int(tg_id) in settings.support_ids:
        return Role.support
    return None


def has_role(settings: Settings, tg_id: int | None, minimum: Role) -> bool:
    role = role_for(settings, tg_id)
    return role is not None and role >= minimum


def is_any_admin(settings: Settings, tg_id: int | None) -> bool:
    return role_for(settings, tg_id) is not None
