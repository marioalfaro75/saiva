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
from sqlalchemy import and_, inspect, or_, select, update
from sqlalchemy.engine import URL, Engine

from .db import engine
from .models import Category, Transaction
from .schema_state import SchemaState, ahead_warning, alembic_config, read_schema_state
from .services.categorise import ML_MAX_CONFIDENCE, PERSON_CONFIDENCE

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
            state = _migrate(db, cfg, tables)
        finally:
            cfg.attributes.pop("connection", None)
    if not state.is_ahead:
        _record_category_keys(db)
        _mark_automatic_categories(db)
    return state


def _record_category_keys(db: Engine) -> None:
    """Give each built-in category its system_key, once, before the app serves anything.

    Revision 0013 only adds the column, because a migration may not touch a row. This
    fills it, and on the first start of the release that added it, a built-in category's
    name is exactly its key: nothing could rename a category before that release. After
    that it only ever fills an EMPTY key -- a household created by an older image after a
    rollback, say -- so a later rename can never be mistaken for the original name.

    Skipped when the table or the column is not there -- an older image's migrations
    under this code, or a test standing in for the upgrade -- never a real deployment.
    """
    inspector = inspect(db)
    if "categories" not in inspector.get_table_names():
        return
    if "system_key" not in {c["name"] for c in inspector.get_columns("categories")}:
        return
    with db.begin() as conn:
        conn.execute(
            update(Category)
            .where(Category.is_system.is_(True), Category.system_key.is_(None))
            .values(system_key=Category.name)
        )


# How long after a transaction was created an untouched row can still show a change:
# created_at and updated_at are two clock reads at insert, microseconds apart.
_UNTOUCHED = dt.timedelta(seconds=2)


def _mark_automatic_categories(db: Engine) -> None:
    """Mark categories no person chose as automatic, so rules may re-file them.

    Rules leave alone a category a person chose, read from its confidence: 1.0 is a
    person's, and no score at all is treated as one too. Two kinds of older row look
    like that and are not:

    * the learner's guesses from before guesses were capped below 1.0. A confident
      guess for a regular merchant was stored as exactly 1.0. One that nothing has
      touched since it was imported was never a person's choice: a person's change
      moves updated_at, and nothing but a person (or transfer detection, on transfers,
      which rules never reach) changes an imported row afterwards.
    * imported rows with a category and no score: the demo loader's, and the other half
      of a transfer whose first half was deleted, left in Internal transfers. An import
      always scores the categories it sets, and a person's choice through the app is
      1.0, so an imported row with a category and no score was never chosen.

    Both become ordinary guesses (ML_MAX_CONFIDENCE). Runs at every start and only ever
    finds rows written by an older release, so after the first it changes nothing.
    """
    inspector = inspect(db)
    if "transactions" not in inspector.get_table_names():
        return
    # Skipped, like _record_category_keys, when the table is an older shape than this
    # code reads -- an older image's migrations under it, or a test standing in for one.
    needed = {
        "category_id",
        "category_locked",
        "is_transfer",
        "confidence",
        "source",
        "created_at",
        "updated_at",
    }
    if not needed <= {c["name"] for c in inspector.get_columns("transactions")}:
        return
    with db.begin() as conn:
        rows = conn.execute(
            select(
                Transaction.id,
                Transaction.confidence,
                Transaction.created_at,
                Transaction.updated_at,
            ).where(
                Transaction.category_id.is_not(None),
                Transaction.category_locked.is_(False),
                Transaction.is_transfer.is_(False),
                or_(
                    and_(
                        Transaction.confidence >= PERSON_CONFIDENCE,
                        Transaction.source.in_(("import", "manual")),
                    ),
                    and_(Transaction.confidence.is_(None), Transaction.source == "import"),
                ),
            )
        ).all()
        ids = [
            r.id
            for r in rows
            if r.confidence is None or r.updated_at - r.created_at < _UNTOUCHED
        ]
        for start in range(0, len(ids), 500):
            conn.execute(
                update(Transaction)
                .where(Transaction.id.in_(ids[start : start + 500]))
                # updated_at kept: this is bookkeeping, not a change anyone made.
                .values(confidence=ML_MAX_CONFIDENCE, updated_at=Transaction.updated_at)
            )


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
