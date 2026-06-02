"""Alembic environment — wires migrations to the app's models and DATABASE_URL.

The engine (and thus the database URL) comes from app.db, which reads DATABASE_URL
from the environment, so migrations target the same database as the running app.
"""

from __future__ import annotations

from alembic import context

from app import models  # noqa: F401  (import registers every table on Base.metadata)
from app.db import Base, engine

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
