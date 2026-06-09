"""Bring the database schema up to date with Alembic on container start.

Handles three situations safely:
  * brand-new database         -> `upgrade head` runs the baseline (+ any later migrations)
  * legacy DB from create_all  -> `stamp 0001` (adopt the baseline), then `upgrade head`
  * already-migrated database   -> `upgrade head`

Before applying migrations to an *existing* Postgres database, a compressed
`pg_dump` is written to SAIVA_BACKUP_DIR so any upgrade is reversible. This runs
regardless of how the deploy was triggered (make pull, the in-app updater, etc.).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import os
import shutil
import subprocess  # nosec B404 - argv is built from our own config, never user input
import sys

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import URL

from .db import engine

BASELINE_REVISION = "0001"


def _backup_enabled() -> bool:
    return os.environ.get("SAIVA_BACKUP_BEFORE_MIGRATE", "1").lower() not in ("0", "false", "no")


def _has_pending_migrations(cfg: Config) -> bool:
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return current != head


def _pg_dump_cmd(url: URL, out_path: str) -> list[str]:
    """pg_dump invocation for a SQLAlchemy URL (password is passed via PGPASSWORD)."""
    return [
        "pg_dump",
        "--host", url.host or "localhost",
        "--port", str(url.port or 5432),
        "--username", url.username or "postgres",
        "--dbname", url.database or "postgres",
        "--no-owner",
        "--no-privileges",
        "--file", out_path,
    ]


def _backup_database() -> None:
    backup_dir = os.environ.get("SAIVA_BACKUP_DIR", "/backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    sql_path = os.path.join(backup_dir, f"saiva-{stamp}-premigrate.sql")
    gz_path = f"{sql_path}.gz"

    env = {**os.environ}
    if engine.url.password:
        env["PGPASSWORD"] = engine.url.password

    print(f"Saiva: backing up database to {gz_path} before migrating…", flush=True)
    result = subprocess.run(  # nosec B603 B607 - fixed argv, no shell; pg_dump via PATH
        _pg_dump_cmd(engine.url, sql_path),
        env=env,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        for path in (sql_path, gz_path):
            with contextlib.suppress(OSError):
                os.remove(path)
        raise RuntimeError(
            "pg_dump failed; refusing to migrate without a backup. "
            "Set SAIVA_BACKUP_BEFORE_MIGRATE=0 to override.\n"
            + result.stderr.decode(errors="replace").strip()
        )

    with open(sql_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(sql_path)
    print("Saiva: backup complete.", flush=True)


def main() -> None:
    tables = set(inspect(engine).get_table_names())
    cfg = Config("alembic.ini")
    if "alembic_version" not in tables and "households" in tables:
        # Database predates Alembic (created by an earlier create_all build). Adopt the
        # baseline revision without re-creating tables, then apply anything newer.
        command.stamp(cfg, BASELINE_REVISION)

    # Back up an existing Postgres database before changing its schema.
    upgrading_existing_db = "households" in tables
    if (
        _backup_enabled()
        and engine.dialect.name == "postgresql"
        and upgrading_existing_db
        and _has_pending_migrations(cfg)
    ):
        _backup_database()

    command.upgrade(cfg, "head")
    print("Saiva: database schema up to date.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # surface a clear reason, then fail the container start
        print(f"Saiva: startup migration step failed: {exc}", file=sys.stderr)
        raise
