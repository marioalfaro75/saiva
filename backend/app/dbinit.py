"""Bring the database schema up to date with Alembic on container start.

Handles four situations safely:
  * brand-new database          -> `upgrade head` runs the baseline (+ anything later)
  * legacy DB from create_all   -> `stamp 0001` (adopt the baseline), then `upgrade head`
  * database behind this image  -> back up, then `upgrade head`
  * database AHEAD of this image -> serve without migrating, and say so loudly

The fourth case is the one that used to take the box down. An image rolled back after a
release that migrated meets a revision it has never heard of; `command.upgrade` raises
`CommandError: Can't locate revision identified by 'NNNN'`; the CMD chains the migration
to uvicorn with `&&`, so nothing ever binds :8000; and `restart: unless-stopped` retries
forever. `app.schema_state` explains the reading that makes that survivable.

Before applying migrations to an *existing* Postgres database that is genuinely BEHIND,
a compressed `pg_dump` is written to SAIVA_BACKUP_DIR so any upgrade is reversible. This
runs regardless of how the deploy was triggered (make update, the in-app button, etc.).
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
from sqlalchemy import inspect
from sqlalchemy.engine import URL, Engine

from .db import engine
from .schema_state import SchemaState, ahead_warning, alembic_config, read_schema_state

BASELINE_REVISION = "0001"


def _backup_enabled() -> bool:
    return os.environ.get("SAIVA_BACKUP_BEFORE_MIGRATE", "1").lower() not in ("0", "false", "no")


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


def _backup_database(db: Engine) -> None:
    backup_dir = os.environ.get("SAIVA_BACKUP_DIR", "/backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    sql_path = os.path.join(backup_dir, f"saiva-{stamp}-premigrate.sql")
    gz_path = f"{sql_path}.gz"

    env = {**os.environ}
    if db.url.password:
        env["PGPASSWORD"] = db.url.password

    print(f"Saiva: backing up database to {gz_path} before migrating…", flush=True)
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args
    # Semgrep flags DATABASE_URL as user-controlled. It is operator-controlled, and
    # more to the point `_pg_dump_cmd` returns a fixed argv list with no shell, so
    # each value is one argument and cannot become a command.
    result = subprocess.run(  # nosec B603 B607 - fixed argv, no shell; pg_dump via PATH
        _pg_dump_cmd(db.url, sql_path),
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


def main(db: Engine | None = None, cfg: Config | None = None) -> SchemaState:
    """Bring `db` to this image's head, or decline to, and say which.

    The arguments exist so the four branches can be tested against a real Postgres
    without a subprocess and without moving files around in the working tree: a test
    passes its own engine and an Alembic config whose script_location is a trimmed copy
    of migrations/, which is precisely what an older image is. Production calls it with
    neither and gets the module-level engine and the image's own migrations.

    Returns the state it read, so a caller (and a test) can tell `migrated` from
    `declined to migrate` without parsing stdout.
    """
    db = db if db is not None else engine
    cfg = cfg if cfg is not None else alembic_config()
    tables = set(inspect(db).get_table_names())

    with db.connect() as conn:
        # Hand migrations/env.py the connection to run on, rather than letting it open
        # its own from whatever DATABASE_URL says. One database is inspected, migrated
        # and reported on, which is what makes the four-way decision below trustworthy.
        cfg.attributes["connection"] = conn
        try:
            return _migrate(db, cfg, tables)
        finally:
            cfg.attributes.pop("connection", None)


def _migrate(db: Engine, cfg: Config, tables: set[str]) -> SchemaState:
    if "alembic_version" not in tables and "households" in tables:
        # Database predates Alembic (created by an earlier create_all build). Adopt the
        # baseline revision without re-creating tables, then apply anything newer.
        command.stamp(cfg, BASELINE_REVISION)

    state = read_schema_state(db, cfg)

    if state.is_ahead:
        # Rolled back onto an older image. Serve rather than crash-loop: see
        # app/schema_state.py for why this is the whole rollback story. Returning here
        # also means no dump is written, which matters because this path used to run on
        # every restart and each attempt wrote a full copy of the database next to it.
        print(ahead_warning(state), file=sys.stderr, flush=True)
        return state

    # Back up an existing Postgres database before changing its schema. `needs_backup`
    # is true only for BEHIND — strictly behind, not merely different.
    if (
        _backup_enabled()
        and db.dialect.name == "postgresql"
        and "households" in tables
        and state.needs_backup
    ):
        _backup_database(db)

    command.upgrade(cfg, "head")
    print(f"Saiva: database schema up to date (revision {state.image_head}).")
    return state


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # surface a clear reason, then fail the container start
        print(f"Saiva: startup migration step failed: {exc}", file=sys.stderr)
        raise
