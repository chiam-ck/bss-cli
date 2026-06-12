"""Alembic environment — async engine from BSS_DB_URL."""

import asyncio
import os

from alembic import context

# Import Base so metadata includes every model registered via bss_models.__init__
from bss_models import Base
from sqlalchemy.ext.asyncio import create_async_engine

target_metadata = Base.metadata


def get_url() -> str:
    url = os.environ.get("BSS_DB_URL")
    if not url:
        raise RuntimeError("BSS_DB_URL environment variable is not set")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect via async engine."""
    engine = create_async_engine(get_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
