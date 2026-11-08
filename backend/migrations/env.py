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


def _run_against(connection) -> None:  # noqa: ANN001 - a sqlalchemy Connection
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A caller may hand us the connection to migrate on, via the documented
    # `config.attributes` channel. app.dbinit uses it so the start-up migration runs on
    # the database it just inspected rather than on whatever DATABASE_URL happens to say
    # at that moment, and so the rollback tests can point a migration at a scratch
    # Postgres without a subprocess. With nothing supplied this behaves exactly as it
    # always did: open a connection from the app's engine.
    supplied = context.config.attributes.get("connection")
    if supplied is not None:
        _run_against(supplied)
        return
    with engine.connect() as connection:
        _run_against(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
