from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import Settings
from app.database.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Arbitrary but fixed: two replicas starting at once must not migrate concurrently.
ADVISORY_LOCK_ID = 8_247_112_001


def get_url() -> str:
    return Settings().database_dsn


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    url = get_url()
    configuration["sqlalchemy.url"] = url
    is_postgres = url.startswith("postgresql")

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        if is_postgres:
            # Serialise concurrent deploys; released automatically with the session.
            await connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        try:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
        finally:
            if is_postgres:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:id)"), {"id": ADVISORY_LOCK_ID}
                )
                await connection.commit()

    await connectable.dispose()


def run_migrations_online_entrypoint() -> None:
    asyncio.run(run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online_entrypoint()
