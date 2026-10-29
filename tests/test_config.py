from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.access import Role, has_role, role_for
from app.config import Settings


def _settings(**kwargs) -> Settings:
    base = {"BOT_TOKEN": "123456789:TEST", "ADMINS": "1", "ALERTS_CHAT_ID": 0}
    base.update(kwargs)
    return Settings(**base)


def test_empty_admins_with_alerts_chat_is_rejected():
    """Fail-fast: a public admin surface must never be started by accident."""
    with pytest.raises(ValidationError):
        Settings(BOT_TOKEN="123456789:TEST", ADMINS="", ALERTS_CHAT_ID=-100123)


def test_empty_admins_without_alerts_chat_is_allowed():
    settings = _settings(ADMINS="")
    assert settings.admin_ids == frozenset()
    assert settings.alerts_target_ids == frozenset()


def test_bot_token_must_look_like_a_token():
    with pytest.raises(ValidationError):
        Settings(BOT_TOKEN="not-a-token", ADMINS="1")


def test_admin_ids_parsing_ignores_spaces_and_semicolons():
    settings = _settings(ADMINS=" 1, 2 ;3 ", MODERATORS="4", SUPPORT="5")
    assert settings.admin_ids == frozenset({1, 2, 3})
    assert settings.moderator_ids == frozenset({4})
    assert settings.support_ids == frozenset({5})


def test_roles_hierarchy():
    settings = _settings(ADMINS="1", MODERATORS="2", SUPPORT="3")
    assert role_for(settings, 1) is Role.admin
    assert role_for(settings, 2) is Role.moderator
    assert role_for(settings, 3) is Role.support
    assert role_for(settings, 99) is None
    assert has_role(settings, 2, Role.support)
    assert not has_role(settings, 2, Role.admin)
    assert not has_role(settings, None, Role.support)


def test_price_is_converted_to_kopecks():
    assert _settings(SUBSCRIPTION_PRICE_RUB=499).premium_price_kopecks == 49900
    assert _settings(SUBSCRIPTION_PRICE_RUB=0).premium_price_kopecks == 100


def test_database_dsn_upgrades_heroku_style_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@host:5432/db")
    assert _settings().database_dsn == "postgresql+asyncpg://user:pass@host:5432/db"


def test_payments_enabled_flags():
    assert not _settings().payments_enabled
    assert _settings(PAYMENTS_PROVIDER_TOKEN="token").payments_enabled
    assert _settings(YOOKASSA_SHOP_ID="shop", YOOKASSA_SECRET_KEY="secret").payments_enabled


def test_yookassa_requires_both_credentials():
    with pytest.raises(ValidationError):
        _settings(YOOKASSA_SHOP_ID="shop")
