"""Where the database's schema sits relative to the image that is running.

This module exists because that question has four answers, not two, and the one that
was missing is the one that bricked the box.

`alembic upgrade head` answers "apply whatever is outstanding". It has no opinion about
a database that is *ahead* of the code — it simply cannot find the revision the database
claims to be at, and raises::

    CommandError: Can't locate revision identified by '0012'

That is exactly what an image rollback produces. Release N migrates the database to 0012;
the owner rolls the image back to N-1, which knows only up to 0010; the start-up migration
raises, and because the container's CMD is `python -m app.dbinit && uvicorn`, uvicorn never
runs. `restart: unless-stopped` then retries forever. The box crash-loops, which looks
exactly like the failure the rollback was meant to undo.

It got worse from there. The check that decided whether to take a pre-migration backup
used to be `current != head` — it answered *different*, not *behind*. An ahead database
answers "different", so every restart attempt first wrote a complete gzipped pg_dump of
the new schema into the backups volume, which shares a filesystem with the database. The
recovery attempt was a disk-exhaustion engine.

The fix rests on one fact: the database's stored revision is readable even when Alembic
cannot resolve it. `MigrationContext.get_current_revision()` returns the raw string out of
`alembic_version` without consulting the script directory, so "this database is at a
revision I have never heard of" is a question we can ask and answer cheaply, before
anything destructive runs.

The four states:

    fresh    the database has no alembic_version row yet -> migrate it
    behind   its revision is one this image knows, but not head -> back up, then migrate
    current  its revision is this image's head -> nothing to do
    ahead    its revision is one this image has never heard of -> DO NOT migrate, DO NOT
             dump, serve anyway and say so loudly

`ahead` being survivable is what makes an image rollback a real recovery path rather than
a second outage. It is safe to promise only because every migration in this repository is
expand-only (add tables, add indexes, add columns that are nullable or carry a server
default) — see the migration-discipline test, which enforces that for new ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy.engine import Connection, Engine

FRESH = "fresh"
BEHIND = "behind"
CURRENT = "current"
AHEAD = "ahead"
UNKNOWN = "unknown"

# alembic.ini sits beside the `app` package in both the repository (backend/) and the
# image (/app), so resolve it from this file rather than from the working directory —
# uvicorn and pytest do not agree on what the working directory is.
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def alembic_config() -> Config:
    """The project's Alembic config, resolved independently of the working directory."""
    cfg = Config(str(ALEMBIC_INI))
    # script_location in alembic.ini is the relative path `migrations`, which Alembic
    # resolves against the config file's directory only when it was given one. Being
    # explicit costs nothing and removes the last dependency on cwd.
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    return cfg


@dataclass(frozen=True)
class SchemaState:
    """A reading of the database's revision against this image's migration history."""

    state: str
    db_revision: str | None
    image_head: str | None

    @property
    def is_ahead(self) -> bool:
        """The database knows a revision this image does not. Do not migrate it."""
        return self.state in (AHEAD, UNKNOWN)

    @property
    def needs_upgrade(self) -> bool:
        """There are migrations in this image that the database has not applied."""
        return self.state in (FRESH, BEHIND)

    @property
    def needs_backup(self) -> bool:
        """A dump is worth taking: the schema of an existing database is about to change.

        Deliberately false for `fresh` (there is nothing to lose) and for `ahead` (the
        schema is not about to change, and dumping on every crash-loop restart is how the
        disk fills up).
        """
        return self.state == BEHIND

    def as_dict(self) -> dict[str, str | None]:
        return {
            "schema": self.state,
            "db_revision": self.db_revision,
            "image_head": self.image_head,
        }


def _stored_revision(conn: Connection) -> str | None:
    """The revision string in alembic_version, without resolving it against the scripts.

    Returns None for a database that has never been migrated. Where the table somehow
    holds more than one row, Alembic raises rather than guessing; that is reported as an
    unknown revision, because a branched history is no more safe to migrate blindly than
    a future one.
    """
    ctx = MigrationContext.configure(conn)
    try:
        return ctx.get_current_revision()
    except CommandError:
        heads = ctx.get_current_heads()
        return ",".join(heads) if heads else None


def read_schema_state(engine: Engine, cfg: Config | None = None) -> SchemaState:
    """Compare the database's stored revision with this image's migration history."""
    cfg = cfg or alembic_config()
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()

    with engine.connect() as conn:
        current = _stored_revision(conn)

    if current is None:
        return SchemaState(FRESH, None, head)
    if current == head:
        return SchemaState(CURRENT, current, head)

    try:
        script.get_revision(current)
    except CommandError:
        # The database is at a revision this image's migrations do not contain. The usual
        # cause is an image rollback after a release that migrated. It could also be a
        # DATABASE_URL pointing at a different application's database, which is why the
        # message the caller prints says both.
        return SchemaState(AHEAD, current, head)

    return SchemaState(BEHIND, current, head)


def ahead_warning(state: SchemaState) -> str:
    """The message a rolled-back image prints instead of crashing.

    Written out in full because it is read exactly once, by somebody whose finance app is
    not working, who did not expect to be reading container logs, and who needs to be told
    what to do rather than what happened.
    """
    return f"""
{"=" * 72}
Saiva: this database is NEWER than this image. Starting anyway, WITHOUT migrating.

  database schema revision : {state.db_revision}
  this image knows up to   : {state.image_head}

The usual cause is an update that was applied and then rolled back. (The other cause is
DATABASE_URL pointing at a different application's database — check that first if you
have not rolled anything back.)

Nothing has been changed or deleted. The app is starting so you can reach your data.
Reads are expected to work: every migration in this project only adds tables, indexes and
columns. Writes that touch something the newer release added are not covered by any test,
so treat this as a state to leave, not to live in. Pick one:

  * Roll forward — run the newer image again (set SAIVA_VERSION back to the newer tag and
    `make update`). Nothing was lost; this is the quickest way back to a matching pair.

  * Stay on this version — restore the pre-migration dump taken just before the upgrade:
    `./scripts/restore-backup.sh` with no arguments lists them. That DESTROYS everything
    written since the upgrade, so it is a deliberate choice and the script will make you
    confirm it.
{"=" * 72}
""".rstrip()
