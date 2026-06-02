"""Bring the database schema up to date with Alembic on container start.

Handles three situations safely:
  * brand-new database         -> `upgrade head` runs the baseline (+ any later migrations)
  * legacy DB from create_all  -> `stamp 0001` (adopt the baseline), then `upgrade head`
  * already-migrated database   -> `upgrade head`
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from .db import engine

BASELINE_REVISION = "0001"


def main() -> None:
    tables = set(inspect(engine).get_table_names())
    cfg = Config("alembic.ini")
    if "alembic_version" not in tables and "households" in tables:
        # Database predates Alembic (created by an earlier create_all build). Adopt the
        # baseline revision without re-creating tables, then apply anything newer.
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")
    print("Saiva: database schema up to date.")


if __name__ == "__main__":
    main()
