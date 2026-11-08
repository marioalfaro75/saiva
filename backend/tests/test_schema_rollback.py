"""An image rolled back onto a newer schema must serve, not crash-loop.

This is the test for the defect that made the retained-image rollback useless: release N
migrates the database to 0012, the owner rolls the image back to N-1 which knows only up
to 0010, `command.upgrade` cannot resolve 0012, the container's `python -m app.dbinit &&
uvicorn` never reaches uvicorn, and `restart: unless-stopped` loops forever. The box
crash-loops, which looks exactly like the failure the rollback was meant to undo.

Every test here runs against a real Postgres, because the defect is not reproducible on
SQLite: the backup branch is guarded on `dialect.name == "postgresql"`, and the thing that
made the crash loop dangerous rather than merely annoying was a full `pg_dump` written on
every restart attempt, into a volume that shares a filesystem with the database.

"An older image" is simulated the way an older image actually differs: an Alembic config
whose script_location is a copy of migrations/ with the newest revisions removed. No
subprocess, and nothing is moved around in the working tree, so an interrupted run cannot
leave the repository missing a migration.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError

from app import dbinit
from app.schema_state import AHEAD, BEHIND, CURRENT, FRESH, read_schema_state

BACKEND = Path(__file__).resolve().parent.parent
MIGRATIONS = BACKEND / "migrations"

# Where to find a Postgres to test against. CI sets the first; the second is the local
# development server. Without one, these tests skip rather than silently pass on SQLite,
# which would prove nothing at all.
PG_CANDIDATES = [
    os.environ.get("SAIVA_TEST_PG_URL", ""),
    "postgresql+psycopg://saiva@127.0.0.1:55432/postgres",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres",
]


def _reachable_admin_url() -> str | None:
    for url in PG_CANDIDATES:
        if not url:
            continue
        try:
            eng = sa.create_engine(
                url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3}
            )
            with eng.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            eng.dispose()
            return url
        except Exception:  # noqa: BLE001 - any failure to connect means "try the next one"
            continue
    return None


ADMIN_URL = _reachable_admin_url()

pytestmark = pytest.mark.skipif(
    ADMIN_URL is None,
    reason="no Postgres reachable; set SAIVA_TEST_PG_URL to run the rollback tests",
)


def _client_server_version_mismatch() -> str | None:
    """pg_dump must be the server's major, or these tests fail for an unrelated reason.

    Debian's /usr/bin/pg_dump is pg_wrapper, which picks the HIGHEST installed client
    version. On a machine with a newer client than the server, pg_dump refuses outright
    on a version mismatch and the backup branch of dbinit raises -- which looks exactly
    like the safety property under test having broken, and is not. Saying so costs four
    lines and saves an afternoon.
    """
    if ADMIN_URL is None:
        return None
    try:
        dumped = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["pg_dump", "--version"], capture_output=True, text=True, check=False
        ).stdout
        client = re.search(r"\b(\d+)\.", dumped)
        engine = sa.create_engine(ADMIN_URL)
        with engine.connect() as conn:
            server = conn.execute(sa.text("SHOW server_version")).scalar_one()
        engine.dispose()
        server_major = re.match(r"(\d+)", str(server))
    except Exception as exc:  # noqa: BLE001 - any failure here is itself the diagnosis
        return f"could not compare pg_dump and server versions: {exc}"
    if not client or not server_major:
        return f"could not parse versions (pg_dump said {dumped!r}, server said {server!r})"
    if client.group(1) != server_major.group(1):
        return (
            f"pg_dump is {client.group(1)}.x but the server is {server_major.group(1)}.x. "
            f"On Debian /usr/bin/pg_dump is pg_wrapper and picks the newest installed "
            f"client, so put /usr/lib/postgresql/{server_major.group(1)}/bin first on PATH."
        )
    return None


def test_the_tools_these_tests_need_are_the_right_versions() -> None:
    """Runs first and fails loudly, so a toolchain problem is not read as a regression."""
    problem = _client_server_version_mismatch()
    assert problem is None, problem


@pytest.fixture
def pg() -> Iterator[sa.Engine]:
    """A fresh, empty Postgres database, dropped afterwards."""
    assert ADMIN_URL is not None
    name = f"saiva_rollback_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))

    engine = sa.create_engine(sa.make_url(ADMIN_URL).set(database=name))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _config_for(script_location: Path) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(script_location))
    return cfg


def _revisions_oldest_first() -> list[str]:
    script = ScriptDirectory.from_config(_config_for(MIGRATIONS))
    return [rev.revision for rev in reversed(list(script.walk_revisions()))]


def _older_image(tmp_path: Path, drop_newest: int) -> Config:
    """An Alembic config that knows every revision except the newest `drop_newest`."""
    copied = tmp_path / f"migrations_minus_{drop_newest}"
    shutil.copytree(MIGRATIONS, copied)
    for revision in _revisions_oldest_first()[-drop_newest:]:
        for path in (copied / "versions").glob(f"{revision}_*.py"):
            path.unlink()
    return _config_for(copied)


def _dumps_in(directory: Path) -> list[Path]:
    return sorted(directory.glob("saiva-*-premigrate.sql.gz"))


@pytest.fixture
def backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "backups"
    directory.mkdir()
    monkeypatch.setenv("SAIVA_BACKUP_DIR", str(directory))
    monkeypatch.setenv("SAIVA_BACKUP_BEFORE_MIGRATE", "1")
    return directory


def _stored_revision(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    return row


# --------------------------------------------------------------------------------------
# The defect itself
# --------------------------------------------------------------------------------------


def test_the_old_behaviour_really_did_crash(pg: sa.Engine, tmp_path: Path) -> None:
    """Non-vacuity guard: prove the thing we are protecting against actually happens.

    Without this, every other test here could pass because of some unrelated change and
    nobody would notice that the hazard had stopped being real.
    """
    dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))
    old_image = _older_image(tmp_path, drop_newest=2)

    with pg.connect() as conn:
        old_image.attributes["connection"] = conn
        with pytest.raises(CommandError) as raised:
            command.upgrade(old_image, "head")

    assert "Can't locate revision" in str(raised.value)
    assert _stored_revision(pg) in str(raised.value)


def test_a_database_ahead_of_the_image_serves_instead_of_crashing(
    pg: sa.Engine, tmp_path: Path, backups: Path
) -> None:
    dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))
    head = _stored_revision(pg)

    state = dbinit.main(db=pg, cfg=_older_image(tmp_path, drop_newest=2))

    assert state.state == AHEAD, "an unknown revision must read as ahead, not as behind"
    assert state.db_revision == head
    assert _stored_revision(pg) == head, "a declining start must not touch alembic_version"


def test_a_database_ahead_of_the_image_is_never_dumped(
    pg: sa.Engine, tmp_path: Path, backups: Path
) -> None:
    """The restart loop used to write a full copy of the database on every attempt.

    `_has_pending_migrations` compared revisions for *difference*, so an ahead database
    answered yes, `_backup_database` ran, and only then did the upgrade raise. Under
    `restart: unless-stopped` that is a disk-exhaustion engine pointed at the volume
    holding the backups, on the same filesystem as the database it is protecting.
    """
    dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))
    old_image = _older_image(tmp_path, drop_newest=2)

    for _ in range(5):
        dbinit.main(db=pg, cfg=old_image)

    assert _dumps_in(backups) == [], "a rolled-back image must not dump on every restart"


def test_data_written_before_the_rollback_is_still_there(
    pg: sa.Engine, tmp_path: Path, backups: Path
) -> None:
    dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))
    with pg.begin() as conn:
        conn.execute(sa.text("CREATE TABLE rollback_probe (id text primary key)"))
        conn.execute(sa.text("INSERT INTO rollback_probe VALUES ('keep-me')"))

    dbinit.main(db=pg, cfg=_older_image(tmp_path, drop_newest=2))

    with pg.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM rollback_probe")).scalar_one() == 1


def test_rolling_forward_after_a_rollback_still_migrates(
    pg: sa.Engine, tmp_path: Path, backups: Path
) -> None:
    """A rollback must be a round trip, not a one-way door."""
    dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))
    head = _stored_revision(pg)
    dbinit.main(db=pg, cfg=_older_image(tmp_path, drop_newest=2))

    state = dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))

    assert state.state == CURRENT
    assert _stored_revision(pg) == head


# --------------------------------------------------------------------------------------
# The branches that must keep working
# --------------------------------------------------------------------------------------


def test_a_fresh_database_migrates_without_a_backup(pg: sa.Engine, backups: Path) -> None:
    state = dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))

    assert state.state == FRESH
    assert _dumps_in(backups) == [], "there is nothing to lose in an empty database"
    assert _stored_revision(pg) is not None


def test_a_database_behind_the_image_is_backed_up_exactly_once_then_migrated(
    pg: sa.Engine, tmp_path: Path, backups: Path
) -> None:
    """The guard against over-correcting: `behind` must still take its dump.

    Narrowing the backup condition from "different" to "strictly behind" is what stops
    the crash loop filling the disk. It would be easy to narrow it into "never", and the
    pre-migration dump is the only thing standing between a bad migration and a family's
    financial records.
    """
    old_image = _older_image(tmp_path, drop_newest=2)
    dbinit.main(db=pg, cfg=old_image)
    before = _stored_revision(pg)

    state = dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))

    assert state.state == BEHIND
    assert state.db_revision == before
    assert len(_dumps_in(backups)) == 1, "exactly one dump, taken before the schema changed"
    assert _dumps_in(backups)[0].stat().st_size > 0
    assert _stored_revision(pg) != before, "and then it actually migrated"


def test_a_failed_backup_still_refuses_to_migrate(
    pg: sa.Engine, tmp_path: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing safety property, re-asserted because this file rewrote its caller."""
    old_image = _older_image(tmp_path, drop_newest=2)
    dbinit.main(db=pg, cfg=old_image)
    before = _stored_revision(pg)
    monkeypatch.setenv("SAIVA_BACKUP_DIR", "/proc/cannot-write-here")

    with pytest.raises(Exception):  # noqa: B017 - OSError or RuntimeError, both are refusals
        dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))

    assert _stored_revision(pg) == before, "no backup, no migration"


# --------------------------------------------------------------------------------------
# The reading itself
# --------------------------------------------------------------------------------------


def test_read_schema_state_names_all_four_cases(pg: sa.Engine, tmp_path: Path) -> None:
    current = _config_for(MIGRATIONS)
    old_image = _older_image(tmp_path, drop_newest=2)

    assert read_schema_state(pg, current).state == FRESH

    dbinit.main(db=pg, cfg=old_image)
    assert read_schema_state(pg, current).state == BEHIND
    assert read_schema_state(pg, old_image).state == CURRENT

    dbinit.main(db=pg, cfg=current)
    assert read_schema_state(pg, current).state == CURRENT
    assert read_schema_state(pg, old_image).state == AHEAD


def test_needs_backup_is_true_only_for_behind(pg: sa.Engine, tmp_path: Path) -> None:
    """Spelled out, because this one boolean is the disk-exhaustion fix."""
    current = _config_for(MIGRATIONS)
    old_image = _older_image(tmp_path, drop_newest=2)

    assert read_schema_state(pg, current).needs_backup is False  # fresh
    dbinit.main(db=pg, cfg=old_image)
    assert read_schema_state(pg, current).needs_backup is True  # behind
    dbinit.main(db=pg, cfg=current)
    assert read_schema_state(pg, current).needs_backup is False  # current
    assert read_schema_state(pg, old_image).needs_backup is False  # ahead


def test_the_warning_tells_the_owner_what_to_do(pg: sa.Engine, tmp_path: Path) -> None:
    """The message is read once, by somebody whose finance app is down. It must act."""
    dbinit.main(db=pg, cfg=_config_for(MIGRATIONS))
    state = read_schema_state(pg, _older_image(tmp_path, drop_newest=2))

    from app.schema_state import ahead_warning

    message = ahead_warning(state)
    assert state.db_revision in message and state.image_head in message
    assert "restore-backup.sh" in message, "it must name the command, not describe it"
    assert "DESTROYS" in message, "and say what that command costs"
    assert "Roll forward" in message, "and offer the cheaper option first"
