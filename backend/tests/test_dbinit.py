from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from app.dbinit import _backup_enabled, _pg_dump_cmd


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
