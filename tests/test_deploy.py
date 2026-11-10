"""Deployment contract: Railway must be able to build and health-check us.

These tests exist because a wrong `railway.json` fails silently: Railway ignores
unknown keys, deploys the container, and the only symptom is a restart loop.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _railway() -> dict:
    return json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))


def test_railway_config_uses_the_current_schema():
    cfg = _railway()
    # The old {"services": {...}} shape is silently ignored by Railway.
    assert "services" not in cfg
    assert cfg["build"]["builder"] == "DOCKERFILE"
    assert cfg["build"]["dockerfilePath"] == "Dockerfile"


def test_railway_healthcheck_points_at_the_real_endpoint():
    deploy = _railway()["deploy"]
    assert deploy["healthcheckPath"] == "/healthz"
    assert deploy["healthcheckTimeout"] >= 60
    assert deploy["restartPolicyType"] == "ON_FAILURE"


def test_railway_keeps_a_single_replica():
    """Polling + singleton lock: a second replica exits on purpose."""
    assert _railway()["deploy"]["numReplicas"] == 1


def test_dockerfile_healthcheck_honours_the_injected_port():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile
    assert "os.getenv('PORT'" in dockerfile or 'os.getenv("PORT"' in dockerfile
    # Hard-coding 8080 would break whenever the platform injects another port.
    assert not re.search(r"127\.0\.0\.1:8080/healthz", dockerfile)


def test_container_runs_migrations_before_the_bot():
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert entrypoint.index("alembic upgrade head") < entrypoint.index("python -m app.main")
    assert "exec python -m app.main" in entrypoint


def test_dockerfile_stays_non_root():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER appuser" in dockerfile
    assert dockerfile.index("USER appuser") < dockerfile.index("ENTRYPOINT")


def test_startup_drops_a_leftover_webhook_before_polling():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "delete_webhook" in source
    assert source.index("delete_webhook") < source.index("start_polling")


def test_sigterm_is_handled_for_graceful_shutdown():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "signal.SIGTERM" in source
    assert "shutdown_signal_received" in source


def test_health_server_binds_all_interfaces():
    """Railway proxies from outside the container: 127.0.0.1 would be unreachable."""
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'host="0.0.0.0"' in source


def test_railway_database_url_with_sslmode_is_accepted(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u:p@containers.railway.app:5432/railway?sslmode=require"
    )
    dsn = Settings(BOT_TOKEN="1:x", ADMINS="1").database_dsn

    assert dsn.startswith("postgresql+asyncpg://")
    assert "sslmode=require" in dsn  # asyncpg >= 0.30 understands it


def test_redis_url_from_railway_is_used(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://default:secret@redis.railway.internal:6379/0")
    from app.config import Settings

    settings = Settings(BOT_TOKEN="1:x", ADMINS="1")
    assert settings.redis_url == "redis://default:secret@redis.railway.internal:6379/0"


def test_deployment_doc_covers_the_required_variables():
    doc = (ROOT / "docs" / "DEPLOY-RAILWAY.md").read_text(encoding="utf-8")
    for needle in ("BOT_TOKEN", "ADMINS", "DATABASE_URL", "REDIS_URL", "Postgres", "Redis"):
        assert needle in doc
