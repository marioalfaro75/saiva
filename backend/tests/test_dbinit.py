"""The cold-start path: what runs before uvicorn is allowed to bind a port.

The API's CMD is `python -m app.dbinit && uvicorn app.main:app`, chained with `&&`
under `restart: unless-stopped`. So everything in this module is on the critical path
of every single deploy — the release the owner cut, and every Watchtower recreate of
the same image. If `main()` raises, uvicorn never starts, the HEALTHCHECK has no port
to probe, and the box retries the same failure forever while the household's records
are offline. Until this file grew past the two helpers it used to cover — `_pg_dump_cmd`
and `_backup_enabled` — nothing exercised `main()` at all: its only coverage was the
`python -c "import app.main, app.dbinit"` in the Dockerfile, which proves the module
imports and nothing whatever about what it does.

These tests drive the real Alembic chain against a throwaway SQLite file, so
`upgrade head` genuinely runs 0001 through head here rather than being mocked. Read
that for what it is: it covers `main()`'s control flow, not production's DDL. No
migration branches on the dialect, but Alembic renders two of them differently —
under SQLite 0009's `batch_alter_table` becomes a four-statement table rebuild where
Postgres gets a single `ALTER COLUMN ... DROP NOT NULL`, and 0005's boolean default
renders `0` rather than `false`. The rebuild also only succeeds because nothing here
turns on `PRAGMA foreign_keys`, which production has no way to turn off; if `app/db.py`
ever sets it, 0009 will fail against the seeded rows below for a real SQLite-only
reason rather than a bug in this file. Real Postgres DDL belongs to the `migrations`
job in checks.yml, and the real `pg_dump` binary to the image smoke steps — this
module complements both and replaces neither.
"""

from __future__ import annotations

import datetime as dt
import gzip
import re
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from app import db as app_db
from app import dbinit
from app.dbinit import _backup_enabled, _pg_dump_cmd

REPO = Path(__file__).resolve().parents[2]

# Spelled out rather than read from `dbinit.BASELINE_REVISION`, because the whole point
# of the legacy test below is that this is the revision a create_all-era database
# already contains. Taking it from the module under test would let the constant and the
# assertion move together: with both reading `dbinit.BASELINE_REVISION`, changing it to
# "0005" left all of this file green while `main()` stamped a revision that silently
# skips 0002-0005 on the one deploy that path ever runs.
LEGACY_BASELINE = "0001"

LEGACY_HOUSEHOLD = "legacyhousehold0000000000000000"
LEGACY_ACCOUNT = "legacyaccount00000000000000000a"
LEGACY_BATCH = "legacybatch00000000000000000001"
LEGACY_TXNS = ("legacytxn000000000000000000001a", "legacytxn000000000000000000002b")


# --- The argv and the toggle ----------------------------------------------------


def test_pg_dump_cmd_built_from_url() -> None:
    url = make_url("postgresql+psycopg://saiva:s3cret@db:5432/saiva")
    cmd = _pg_dump_cmd(url, "/backups/out.sql")
    assert cmd[0] == "pg_dump"
    assert cmd[cmd.index("--host") + 1] == "db"
    assert cmd[cmd.index("--port") + 1] == "5432"
    assert cmd[cmd.index("--username") + 1] == "saiva"
    assert cmd[cmd.index("--dbname") + 1] == "saiva"
    assert cmd[cmd.index("--file") + 1] == "/backups/out.sql"
    # The password must never appear on the argv (it travels via PGPASSWORD).
    assert "s3cret" not in cmd


def test_pg_dump_cmd_uses_defaults_when_url_is_sparse() -> None:
    cmd = _pg_dump_cmd(make_url("postgresql://user@/saiva"), "/tmp/x.sql")
    assert cmd[cmd.index("--host") + 1] == "localhost"
    assert cmd[cmd.index("--port") + 1] == "5432"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("1", True), ("true", True), ("0", False), ("false", False), ("no", False)],
)
def test_backup_enabled_toggle(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("SAIVA_BACKUP_BEFORE_MIGRATE", raising=False)
    else:
        monkeypatch.setenv("SAIVA_BACKUP_BEFORE_MIGRATE", value)
    assert _backup_enabled() is expected


# --- Pointing main() at a database it is allowed to destroy ---------------------


@pytest.fixture
def cold_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """An empty database standing in for the one a container boots against.

    Two names have to move, not one. `app.dbinit` does `from .db import engine`, so
    patching `app.dbinit.engine` reaches `main()`, `_has_pending_migrations()` and
    `_backup_database()` together — they all read that one module global. But
    `migrations/env.py` does its own `from app.db import Base, engine`, and Alembic
    re-executes that file on every `command.upgrade` and every `command.stamp`.
    The first draft of this fixture patched only `app.dbinit.engine`, and the
    migrations consequently ran against the suite's own SQLite database: the tests
    failed with "table households already exists", because conftest's `_schema`
    fixture had created them there already.

    Patching `app.db.engine` as well is what closes that, and it is load-bearing for
    more than the error message. conftest's `_schema` teardown runs *inside* this
    patch — the measured order is drop_all, then monkeypatch.undo — so it is only
    pointed at the right database because conftest did `from app.db import Base,
    engine`, which binds the engine by value and so is not rebound here. And
    `alembic_version` is not on `Base.metadata`, so `drop_all` cannot reach it: any
    stamp that landed in the suite database would sit there for the rest of the
    session, in a file every other test shares.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'cold-start.db'}", future=True)
    monkeypatch.setattr(dbinit, "engine", engine)
    monkeypatch.setattr(app_db, "engine", engine)
    return engine


def _head_revision() -> str:
    """The head Alembic knows about, read rather than hardcoded so adding a migration
    does not quietly turn the assertions below into assertions about an old head."""
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert head, "Alembic reports no head revision, so these tests are checking nothing"
    return head


def _stamped_revision(engine: Engine) -> str:
    with engine.connect() as conn:
        rows = conn.execute(text("select version_num from alembic_version")).scalars().all()
    assert len(rows) == 1, f"expected exactly one stamped revision, found {rows}"
    return str(rows[0])


def _make_legacy_database(engine: Engine) -> None:
    """Shape the database the way a pre-Alembic install left it, and put data in it.

    Those boxes were built by `Base.metadata.create_all` before migrations existed, so
    they carry the baseline schema and no `alembic_version` table. Building the
    baseline with `upgrade 0001` and then dropping `alembic_version` reproduces that
    exactly, and is better than `create_all` of today's models, which would produce
    the *head* schema under a baseline stamp and make `upgrade head` try to re-create
    tables that are already there.

    The rows matter as much as the schema. Seeding only `households` is what the first
    version of this helper did, and no migration after the baseline touches that table,
    so a migration that dropped every row of real data passed anyway: adding
    `DELETE FROM transactions` to 0009 left this whole file green. An import batch with
    transactions hanging off it is the case with teeth, because 0009 is the one
    migration that rebuilds a table under SQLite and therefore the one that could lose
    rows or silently drop the foreign key they depend on.
    """
    command.upgrade(Config("alembic.ini"), LEGACY_BASELINE)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(text("drop table alembic_version"))
        conn.execute(
            text(
                "insert into households (id, name, country, currency, locale, timezone,"
                " fy_start_month, fy_start_day, period_basis, adults, children,"
                " created_at, updated_at) values (:id, :name, 'AU', 'AUD', 'en-AU',"
                " 'Australia/Sydney', 7, 1, 'monthly', 2, 1, :now, :now)"
            ),
            {"id": LEGACY_HOUSEHOLD, "name": "Pre-Alembic", "now": now},
        )
        conn.execute(
            text(
                "insert into accounts (id, household_id, name, type, currency,"
                " opening_balance_cents, created_at, updated_at) values (:id, :hh,"
                " 'Everyday', 'checking', 'AUD', 0, :now, :now)"
            ),
            {"id": LEGACY_ACCOUNT, "hh": LEGACY_HOUSEHOLD, "now": now},
        )
        conn.execute(
            text(
                "insert into import_batches (id, household_id, account_id, filename,"
                " file_format, status, added_count, skipped_count, created_at,"
                " updated_at) values (:id, :hh, :acct, 'jan.csv', 'csv', 'complete',"
                " 2, 0, :now, :now)"
            ),
            {"id": LEGACY_BATCH, "hh": LEGACY_HOUSEHOLD, "acct": LEGACY_ACCOUNT, "now": now},
        )
        for txn_id, amount in zip(LEGACY_TXNS, (-1500, -2500), strict=True):
            conn.execute(
                text(
                    "insert into transactions (id, household_id, account_id, txn_date,"
                    " amount_cents, raw_description, is_transfer, is_recurring, source,"
                    " dedup_hash, import_batch_id, tags, created_at, updated_at) values"
                    " (:id, :hh, :acct, :day, :amount, 'COLES', 0, 0, 'import', :id,"
                    " :batch, '[]', :now, :now)"
                ),
                {
                    "id": txn_id,
                    "hh": LEGACY_HOUSEHOLD,
                    "acct": LEGACY_ACCOUNT,
                    "day": now.date(),
                    "amount": amount,
                    "batch": LEGACY_BATCH,
                    "now": now,
                },
            )


# --- The relative path that every deploy depends on -----------------------------


def test_main_looks_for_alembic_ini_in_whatever_directory_it_was_started_from() -> None:
    """`main()` builds `Config("alembic.ini")` from a relative path, so cwd decides.

    cwd, and nothing else: pytest's rootdir is backend/ whether it is invoked from
    backend/, from backend/tests/ or from the repository root, and the path resolves in
    only the first of those. What actually holds it in CI is `working-directory:
    backend` on the `backend` job in checks.yml; in the container it is the Dockerfile's
    WORKDIR being the directory alembic.ini is copied into, with the CMD running
    `python -m app.dbinit` from there. Move either one and every deploy dies in its
    first second with a FileNotFoundError, before uvicorn is ever reached — which is why
    this is asserted here rather than left to be discovered on the box.
    """
    assert Path("alembic.ini").is_file(), (
        "these tests are running from somewhere other than backend/, which is the only "
        'cwd from which main()\'s Config("alembic.ini") resolves'
    )
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    workdirs = re.findall(r"^WORKDIR\s+(\S+)", dockerfile, re.MULTILINE)
    copies = re.findall(r"^COPY\s+alembic\.ini\s+(\S+)", dockerfile, re.MULTILINE)
    assert workdirs and copies, (
        "read no WORKDIR or no `COPY alembic.ini` out of the Dockerfile, so this test "
        "asserted nothing about where the container looks for it"
    )
    assert copies == ["./"], (
        f"alembic.ini is copied to {copies} rather than into the WORKDIR {workdirs[-1]}, "
        'so the relative Config("alembic.ini") in main() will not find it at boot'
    )


# --- main()'s three documented situations ---------------------------------------


def test_a_brand_new_database_gets_the_whole_migration_chain(cold_start: Engine) -> None:
    """First install: an empty database and one shot at building it correctly.

    Comparing the result against the models' own metadata is the point. `alembic check`
    in checks.yml proves the migrations agree with the models, but nothing proved that
    the code path the container actually runs is what applies them — a `main()` that
    silently skipped the upgrade would have left both of those green. What this does not
    prove is that the DDL production receives is right: this is SQLite, so 0009 takes
    Alembic's table-rebuild path rather than the `ALTER COLUMN` a Postgres box gets.
    """
    dbinit.main()

    tables = set(inspect(cold_start).get_table_names())
    assert tables == set(app_db.Base.metadata.tables) | {"alembic_version"}
    assert _stamped_revision(cold_start) == _head_revision()


def test_a_legacy_pre_alembic_database_is_adopted_rather_than_rebuilt(
    cold_start: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-way door: adopting a create_all-era database without touching its rows.

    This path runs once per box, ever, on the deploy that first carries Alembic — which
    is precisely why it is the easiest one to break without noticing. Getting it wrong
    does not fail safe: without the stamp, `upgrade head` runs 0001 against a database
    that already has those tables and the deploy dies; a stamp of the wrong revision
    skips real migrations and leaves the schema silently behind the models, which is why
    the revision is asserted as a literal here and not as `dbinit.BASELINE_REVISION`.
    """
    _make_legacy_database(cold_start)
    stamped: list[str] = []
    real_stamp = command.stamp

    def record_stamp(cfg: Config, revision: str) -> None:
        stamped.append(revision)
        real_stamp(cfg, revision)

    monkeypatch.setattr(dbinit.command, "stamp", record_stamp)

    dbinit.main()

    assert stamped == [LEGACY_BASELINE], (
        f"expected exactly one stamp of {LEGACY_BASELINE}, got {stamped}"
    )
    assert _stamped_revision(cold_start) == _head_revision()

    columns = {column["name"] for column in inspect(cold_start).get_columns("transactions")}
    assert {"category_locked", "provider_txn_id"} <= columns, (
        "the migrations after the baseline did not run, so the stamp jumped the queue "
        "rather than adopting it"
    )

    # Everything seeded before the upgrade has to still be there afterwards, still
    # joined up. 0009 rebuilds import_batches under SQLite, so this is where a lost row
    # or a dropped foreign key would show up.
    with cold_start.connect() as conn:
        assert conn.execute(text("select name from households")).scalar_one() == "Pre-Alembic"
        batch = conn.execute(text("select id, account_id, added_count from import_batches")).all()
        assert batch == [(LEGACY_BATCH, LEGACY_ACCOUNT, 2)], (
            f"the rebuilt import_batches table did not come back intact: {batch}"
        )
        txns = conn.execute(text("select id, amount_cents from transactions order by id")).all()
        assert txns == [(LEGACY_TXNS[0], -1500), (LEGACY_TXNS[1], -2500)], (
            f"transactions were lost or altered by the upgrade: {txns}"
        )
        orphans = conn.execute(
            text(
                "select count(*) from transactions t left join import_batches b"
                " on t.import_batch_id = b.id where b.id is null"
            )
        ).scalar_one()
        assert orphans == 0, "transactions were orphaned from their import batch"


def test_the_restart_path_is_a_no_op_and_stamps_nothing(
    cold_start: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By far the most-run path: a container recreated on an unchanged schema.

    Watchtower recreates the API container on every update it applies, and `make pull`
    does the same by hand, so this runs far more often than either path above. It has
    to be a no-op — and in particular it must not stamp, because a stamp here would
    rewrite `alembic_version` on a database that was already correct.
    """
    dbinit.main()
    before = _stamped_revision(cold_start)

    stamped: list[str] = []
    monkeypatch.setattr(dbinit.command, "stamp", lambda cfg, revision: stamped.append(revision))
    backups: list[None] = []
    monkeypatch.setattr(dbinit, "_backup_database", lambda: backups.append(None))

    dbinit.main()

    assert _stamped_revision(cold_start) == before
    assert stamped == [], "a database already at head was re-stamped"
    assert backups == [], "a backup was taken with nothing to migrate"


# --- The backup decision --------------------------------------------------------


def test_a_sqlite_database_is_never_pg_dumped(
    cold_start: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg_dump against SQLite would fail the deploy over a backup that cannot exist.

    `_backup_database` raises RuntimeError when pg_dump exits non-zero, and with `&&`
    in the CMD that raise is the whole container. A developer's first `make up` on the
    SQLite dev compose file would therefore die in dbinit without the dialect guard.
    Every other term of the condition is deliberately true here: the households table
    exists, there are genuinely pending migrations, and the toggle is at its default.
    """
    _make_legacy_database(cold_start)
    monkeypatch.delenv("SAIVA_BACKUP_BEFORE_MIGRATE", raising=False)
    assert dbinit._has_pending_migrations(Config("alembic.ini")), (
        "nothing is pending, so this test would pass without the dialect guard it is about"
    )

    backups: list[None] = []
    monkeypatch.setattr(dbinit, "_backup_database", lambda: backups.append(None))

    dbinit.main()

    assert backups == [], "dbinit tried to pg_dump a SQLite database"
    assert _stamped_revision(cold_start) == _head_revision()


@pytest.mark.parametrize(
    ("dialect", "existing_db", "pending", "toggle", "expect_backup"),
    [
        # The only combination worth the minutes a dump costs: a real database, on
        # Postgres, whose schema is about to change.
        ("postgresql", True, True, None, True),
        # Every Watchtower recreate. Dumping here would add minutes of downtime to a
        # restart that changes nothing.
        ("postgresql", True, False, None, False),
        # First install: there is no data to lose yet.
        ("postgresql", False, True, None, False),
        # The dev/test dialect, covered end to end by the test above as well.
        ("sqlite", True, True, None, False),
        # The documented escape hatch, named in the RuntimeError's own message.
        ("postgresql", True, True, "0", False),
    ],
)
def test_the_pre_migration_backup_is_taken_exactly_when_it_is_worth_taking(
    cold_start: Engine,
    monkeypatch: pytest.MonkeyPatch,
    dialect: str,
    existing_db: bool,
    pending: bool,
    toggle: str | None,
    expect_backup: bool,
) -> None:
    """All four terms of the guard, as a table, because they are an `and` of four things.

    The dialect is faked on a SQLite engine rather than run against a real Postgres,
    which is the honest limitation here: both `command.upgrade` and `command.stamp` are
    recorded instead of executed, because Alembic picks its DDL implementation off
    `dialect.name` and would otherwise drive PostgresqlImpl at a SQLite connection — a
    combination no real deployment has, and which errors on 0009's `batch_alter_table`.
    So this test covers main()'s decision and not the migration run; the tests above
    cover the migration run, and checks.yml's `migrations` job covers real Postgres DDL.
    """
    if existing_db:
        _make_legacy_database(cold_start)
    if dialect != cold_start.dialect.name:
        monkeypatch.setattr(cold_start.dialect, "name", dialect)
    if toggle is None:
        monkeypatch.delenv("SAIVA_BACKUP_BEFORE_MIGRATE", raising=False)
    else:
        monkeypatch.setenv("SAIVA_BACKUP_BEFORE_MIGRATE", toggle)

    monkeypatch.setattr(dbinit, "_has_pending_migrations", lambda cfg: pending)
    backups: list[None] = []
    monkeypatch.setattr(dbinit, "_backup_database", lambda: backups.append(None))
    upgrades: list[str] = []
    monkeypatch.setattr(dbinit.command, "upgrade", lambda cfg, revision: upgrades.append(revision))
    stamps: list[str] = []
    monkeypatch.setattr(dbinit.command, "stamp", lambda cfg, revision: stamps.append(revision))

    dbinit.main()

    assert bool(backups) is expect_backup, (
        f"dialect={dialect} existing_db={existing_db} pending={pending} toggle={toggle}: "
        f"expected backup={expect_backup}, got {len(backups)} call(s)"
    )
    assert upgrades == ["head"], "the deploy did not go on to migrate"
    # Whatever the backup decision was, the adoption stamp has to have fired exactly on
    # the legacy shape and nowhere else — a brand-new database must not be stamped at the
    # baseline, because `upgrade head` would then skip every migration after it.
    assert stamps == ([LEGACY_BASELINE] if existing_db else []), (
        f"existing_db={existing_db}: expected stamps="
        f"{[LEGACY_BASELINE] if existing_db else []}, got {stamps}"
    )


def test_a_failed_pg_dump_stops_the_deploy_instead_of_migrating_anyway(
    cold_start: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual safety property, and the reason the dump is in-line and not a side task.

    Migrating a household's real records with no dump to go back to is the one outcome
    worth crashing to avoid. Crashing is loud and recoverable — the operator sees the
    RuntimeError in `docker logs`, the old schema is untouched, and rolling back to the
    previous image gets them running again. Migrating anyway is quiet, and if the new
    schema turns out to be wrong there is nothing left to restore from.

    `command.stamp` is recorded rather than executed for the same reason as the table
    above: the dialect is faked, and a real Alembic call under a faked dialect drives
    PostgresqlImpl at a SQLite connection. It happens to work for a stamp, because the
    version table's DDL compiles the same either way, but it is luck and not a thing to
    rest a safety test on.
    """
    _make_legacy_database(cold_start)
    monkeypatch.setattr(cold_start.dialect, "name", "postgresql")
    monkeypatch.setattr(dbinit, "_has_pending_migrations", lambda cfg: True)
    monkeypatch.setattr(dbinit.command, "stamp", lambda cfg, revision: None)

    def failed_backup() -> None:
        raise RuntimeError("pg_dump failed; refusing to migrate without a backup.")

    monkeypatch.setattr(dbinit, "_backup_database", failed_backup)
    upgrades: list[str] = []
    monkeypatch.setattr(dbinit.command, "upgrade", lambda cfg, revision: upgrades.append(revision))

    with pytest.raises(RuntimeError, match="refusing to migrate"):
        dbinit.main()

    assert upgrades == [], "the schema was migrated even though the pre-migration backup failed"


# --- The dump itself, without a pg_dump binary ----------------------------------


@pytest.fixture
def pg_engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A Postgres engine that is never connected to.

    `_backup_database` reads `engine.url` and nothing else, and `create_engine` resolves
    the DBAPI without opening a socket, so this needs no Postgres server — which matters
    because neither this sandbox nor the `backend` job in checks.yml has one. It does
    import psycopg, which is a pinned runtime dependency whose wheel bundles libpq, so
    it needs no system client library either.
    """
    engine = create_engine("postgresql+psycopg://saiva:s3cret@db:5432/saiva")
    monkeypatch.setattr(dbinit, "engine", engine)
    return engine


def test_the_dump_is_gzipped_and_the_plain_sql_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_engine: Engine
) -> None:
    """What ends up on the volume the owner would restore from.

    The uncompressed dump must not survive: these are a household's complete financial
    records sitting on a Proxmox LXC whose disk is the smallest thing about it, and the
    gzip is roughly an order of magnitude. The backup directory is created here rather
    than assumed, because /backups is a named volume that may be mounted empty.
    """
    backups = tmp_path / "backups"
    monkeypatch.setenv("SAIVA_BACKUP_DIR", str(backups))
    recorded: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        recorded["argv"] = argv
        recorded["env"] = kwargs["env"]
        Path(argv[argv.index("--file") + 1]).write_text("-- PostgreSQL database dump\n")
        return subprocess.CompletedProcess(argv, 0, stderr=b"")

    monkeypatch.setattr(dbinit.subprocess, "run", fake_run)

    dbinit._backup_database()

    argv = recorded["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "pg_dump"
    assert argv[argv.index("--dbname") + 1] == "saiva"
    assert argv[argv.index("--file") + 1].startswith(str(backups))
    assert "s3cret" not in argv, "the password reached the process table"
    env = recorded["env"]
    assert isinstance(env, dict)
    assert env["PGPASSWORD"] == "s3cret"

    written = sorted(backups.iterdir())
    assert [path.name.endswith("-premigrate.sql.gz") for path in written] == [True], (
        f"expected exactly one gzipped dump, found {[path.name for path in written]}"
    )
    assert gzip.decompress(written[0].read_bytes()) == b"-- PostgreSQL database dump\n"


def test_a_failed_dump_raises_and_leaves_nothing_that_looks_like_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_engine: Engine
) -> None:
    """A truncated dump that looks like a backup is worse than no backup at all.

    pg_dump creates its output file before it fails, and the failure this guards
    against is a real one: pg_dump comes from the PGDG apt repo keyed on the base
    image's VERSION_CODENAME, so a base image that moves to a new Debian release pulls
    an incompatible libpq5 and pg_dump exits non-zero partway through. The operator
    needs the reason in the logs and nothing left behind that they might later restore.
    """
    backups = tmp_path / "backups"
    monkeypatch.setenv("SAIVA_BACKUP_DIR", str(backups))

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(argv[argv.index("--file") + 1]).write_text("-- half a dump\n")
        return subprocess.CompletedProcess(
            argv, 1, stderr=b"pg_dump: error: server version mismatch"
        )

    monkeypatch.setattr(dbinit.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        dbinit._backup_database()

    message = str(excinfo.value)
    assert "pg_dump failed" in message
    assert "SAIVA_BACKUP_BEFORE_MIGRATE=0" in message, "the override is not in the message"
    assert "server version mismatch" in message, "pg_dump's own stderr was swallowed"
    assert list(backups.iterdir()) == [], (
        "a partial dump was left in the backup directory, where the next person looking "
        "for something to restore will find it"
    )


def test_a_url_without_a_password_does_not_export_an_empty_pgpassword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peer and trust authentication have no password, and an empty one is not the same.

    Exporting PGPASSWORD="" makes libpq offer an empty password rather than fall through
    to peer auth, which turns a dump that would have worked into a failed one — and a
    failed dump stops the deploy.
    """
    monkeypatch.setattr(dbinit, "engine", create_engine("postgresql+psycopg://saiva@db/saiva"))
    monkeypatch.setenv("SAIVA_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("PGPASSWORD", raising=False)
    captured: dict[str, str] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        captured.update(env)
        Path(argv[argv.index("--file") + 1]).write_text("-- dump\n")
        return subprocess.CompletedProcess(argv, 0, stderr=b"")

    monkeypatch.setattr(dbinit.subprocess, "run", fake_run)

    dbinit._backup_database()

    assert captured, "subprocess.run was never called, so nothing was asserted"
    assert "PGPASSWORD" not in captured
