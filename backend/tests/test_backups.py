"""The backup surface: "Back up now", and the record it reads back.

The sibling of test_update_button.py, and the property under test is the same one in a
harder setting. An update panel that over-claims shows the owner the wrong version
number; a backup panel that over-claims tells them they have a safety net on the night
they find out they do not. So the defaults all fall the other way:

  * no record at all is `phase=None`, not "fine" -- "nothing has ever been backed up
    here" and "last night's backup failed" are different sentences;
  * `verified` is three-valued, and the absent answer is None rather than False, because
    "not proved" is not "disproved" and neither is "proved";
  * a record this version cannot parse is `unreadable`, not a 500 -- the page whose job
    is to report on the backup regime cannot be taken down by it;
  * and a run stuck at `running` stops blocking the button after an hour, because the
    way back must not be a shell on the LXC.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest
from conftest import DEFAULT_PASSWORD, sync_csrf
from fastapi.testclient import TestClient

from app.services import backups, updates


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The layout scripts/install-updater.sh builds: one directory, two drop-boxes.

    SAIVA_UPDATE_DIR is the whole state directory and the name is historical -- there is
    one bind mount and the backup drop-box lives inside it, so a second variable could
    only ever disagree with the compose file.
    """
    directory = tmp_path / "saiva-update"
    (directory / "requests").mkdir(parents=True)
    (directory / "backup-requests").mkdir(parents=True)
    monkeypatch.setenv("SAIVA_UPDATE_DIR", str(directory))
    return directory


def _write_status(directory: Path, **fields: object) -> None:
    (directory / "backup-status.json").write_text(json.dumps(fields), encoding="utf-8")


def _stamp(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def no_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAIVA_UPDATE_DIR", raising=False)


def _non_owner(client: TestClient) -> TestClient:
    """An adult in the same household, signed in. Everything here is owner-only."""
    resp = client.post(
        "/api/household/users",
        json={
            "name": "Adult",
            "email": "adult@example.com",
            "password": DEFAULT_PASSWORD,
            "role": "adult",
        },
    )
    assert resp.status_code == 201, resp.text

    from app.main import app

    other = TestClient(app)
    sync_csrf(other)
    login = other.post(
        "/api/auth/login",
        json={"email": "adult@example.com", "password": DEFAULT_PASSWORD},
    )
    assert login.status_code == 200, login.text
    sync_csrf(other, login.json()["csrf_token"])
    return other


# --------------------------------------------------------------------------------------
# Whether the button can do anything at all
# --------------------------------------------------------------------------------------


def test_the_button_is_unavailable_without_a_configured_directory(
    no_state_dir: None,
) -> None:
    assert backups.request_available() is False


def test_the_button_is_unavailable_on_a_box_installed_before_this_drop_box_existed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgraded-but-not-re-installed box must report honestly.

    Such a box has SAIVA_UPDATE_DIR set and requests/ in it -- so the UPDATE button works
    -- and no backup-requests/, because that directory arrived with this feature. The
    right answer is "backups aren't set up here", not a request dropped where nothing is
    watching.
    """
    directory = tmp_path / "update-only"
    (directory / "requests").mkdir(parents=True)
    monkeypatch.setenv("SAIVA_UPDATE_DIR", str(directory))

    assert backups.request_available() is False
    assert updates.apply_available() is True, (
        "the fixture no longer isolates the two drop-boxes, so this proves nothing"
    )


def test_the_button_is_unavailable_when_the_drop_box_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount that exists but is read-only must not advertise a working button."""
    directory = tmp_path / "read-only"
    (directory / "backup-requests").mkdir(parents=True)
    (directory / "backup-requests").chmod(0o555)
    monkeypatch.setenv("SAIVA_UPDATE_DIR", str(directory))
    try:
        if os.geteuid() == 0:
            pytest.skip("running as root, which can write to a 0555 directory anyway")
        assert backups.request_available() is False
    finally:
        (directory / "backup-requests").chmod(0o755)


def test_the_endpoint_refuses_and_says_how_to_fix_it(
    auth_client: TestClient, no_state_dir: None
) -> None:
    response = auth_client.post("/api/admin/backup")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "install-updater.sh" in detail, "the refusal must name the command that fixes it"
    assert "backup.sh" in detail, "and the way to take one without the button"


# --------------------------------------------------------------------------------------
# Asking for a backup
# --------------------------------------------------------------------------------------


def test_pressing_back_up_now_writes_a_request_the_host_can_see(
    auth_client: TestClient, state_dir: Path
) -> None:
    response = auth_client.post("/api/admin/backup")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "requested"
    assert body["request_id"]
    dropped = list((state_dir / "backup-requests").iterdir())
    assert len(dropped) == 1 and dropped[0].is_file()
    assert not dropped[0].name.startswith("."), "the staging file must be renamed away"


def test_a_backup_request_lands_in_its_own_drop_box_and_nowhere_else(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The protocol, asserted rather than assumed.

    WHICH directory the file landed in is the only thing this container gets to say --
    root never opens the file, so a flag inside it would mean nothing. A backup request
    written into requests/ would be picked up by the update path unit and apply a
    release, which is the one mistake the two-directory design exists to make impossible.
    """
    before = sorted(p.name for p in state_dir.iterdir())
    auth_client.post("/api/admin/backup")

    assert sorted(p.name for p in state_dir.iterdir()) == before, "it wrote outside the drop-box"
    assert list((state_dir / "requests").iterdir()) == [], (
        "a backup request landed in the UPDATE drop-box, which would apply a release"
    )
    assert updates.update_in_progress() is False, (
        "asking for a backup made the update button think an update was running"
    )


def test_two_presses_do_not_collide_on_one_filename(state_dir: Path) -> None:
    """Carried over from the update button, where a fixed temp name produced a 503 that
    blamed the permissions of a plainly writable directory."""
    first = backups.request_backup(requested_by="a@example.invalid")
    second = backups.request_backup(requested_by="b@example.invalid")

    assert first != second
    names = sorted(p.name for p in (state_dir / "backup-requests").iterdir())
    assert names == sorted([first, second])


def test_the_request_carries_nothing_that_selects_where_the_backup_goes(
    state_dir: Path,
) -> None:
    """The security property this rests on, asserted rather than assumed.

    The api container is reachable from the network and scripts/backup.sh runs as root.
    If the request could name a destination path or a copy command, "a container can
    write a file" would become "a container can make root write where it likes, or run
    what it likes". Those two settings live in /etc/saiva/agent.conf instead, and are
    ignored unless only root can write it.
    """
    request_id = backups.request_backup(requested_by="owner@example.invalid")
    contents = (state_dir / "backup-requests" / request_id).read_text()

    assert contents.splitlines() == [contents.strip()], "one line, so there is nowhere to hide"
    keys = {part.split("=", 1)[0] for part in contents.split() if "=" in part}
    assert keys == {"requested_at", "requested_by"}, f"unexpected keys in the request: {keys}"


def test_a_second_press_while_one_is_running_is_refused(
    auth_client: TestClient, state_dir: Path
) -> None:
    auth_client.post("/api/admin/backup")

    second = auth_client.post("/api/admin/backup")

    assert second.status_code == 409
    assert "already running" in second.json()["detail"]


def test_a_run_stuck_at_running_eventually_stops_blocking_the_button(
    auth_client: TestClient, state_dir: Path
) -> None:
    """There has to be a way back that is not a shell on the LXC.

    A record left at `running` -- a host killed mid-dump, the unit's own
    TimeoutStartSec, a power cut -- would otherwise 409 the button forever. The dump is
    the slowest thing this box does, so the window is an hour and a bit rather than the
    updater's thirty-five minutes; past it, nothing is running.
    """
    stale = dt.datetime.now(dt.UTC) - (backups.STALE_RUN_AFTER + dt.timedelta(minutes=5))
    _write_status(state_dir, phase="running", step="verify", started_at=_stamp(stale))

    assert backups.backup_in_progress() is False
    assert auth_client.post("/api/admin/backup").status_code == 202


def test_a_run_that_started_moments_ago_still_blocks_the_button(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The guard against over-correcting: two dumps at once is not an improvement."""
    _write_status(
        state_dir, phase="running", step="dump", started_at=_stamp(dt.datetime.now(dt.UTC))
    )

    assert backups.backup_in_progress() is True
    assert auth_client.post("/api/admin/backup").status_code == 409


def test_a_long_running_backup_inside_the_window_still_blocks_the_button(
    state_dir: Path,
) -> None:
    """The verifying restore is slow on a large database, so the window has to cover it.

    Thirty-five minutes -- the updater's number -- would call an ordinary verify stale.
    """
    begun = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=50)
    _write_status(state_dir, phase="running", step="verify", started_at=_stamp(begun))

    assert backups.backup_in_progress() is True


def test_a_running_record_with_no_timestamp_is_treated_as_live(state_dir: Path) -> None:
    """Unknown means in progress, not free to start another."""
    _write_status(state_dir, phase="running")
    assert backups.backup_in_progress() is True
    _write_status(state_dir, phase="running", started_at="not a timestamp")
    assert backups.backup_in_progress() is True


def test_a_finished_run_does_not_block_the_next_one(state_dir: Path) -> None:
    """Every phase the shell can finish in has to let the button go again."""
    for phase in sorted(backups.TERMINAL_PHASES):
        _write_status(
            state_dir, phase=phase, started_at=_stamp(dt.datetime.now(dt.UTC))
        )
        assert backups.backup_in_progress() is False, f"{phase} still blocks the button"


def test_requesting_a_backup_is_audited(auth_client: TestClient, state_dir: Path) -> None:
    auth_client.post("/api/admin/backup")

    actions = [entry["action"] for entry in auth_client.get("/api/admin/audit").json()]
    assert "backup_requested" in actions


# --------------------------------------------------------------------------------------
# Reading back what the host did
# --------------------------------------------------------------------------------------


def test_the_record_is_reported_verbatim(auth_client: TestClient, state_dir: Path) -> None:
    """Every value the host writes is a JSON string, including the counts and `verified`.

    So this also pins the one coercion the API does make: "true" is the shell's way of
    writing a boolean, and the page gets a boolean.
    """
    _write_status(
        state_dir,
        run_id="20260101T020304Z-1234",
        phase="ok",
        step="done",
        trigger="timer",
        dump_name="saiva-20260101-020304-nightly.sql.gz",
        dump_bytes="4211337",
        dump_revision="a1b2c3d4e5f6",
        verified="true",
        verify_message="Restored into a throwaway database: 504 rows across 11 tables",
        rows_checked="504",
        last_verified_at="2026-01-01T02:05:00Z",
        offsite="same-filesystem",
        offsite_message="copied, but it is on the SAME filesystem as the database",
        backup_count="14",
        log_tail=["one", "two"],
    )

    body = auth_client.get("/api/admin/backup-status").json()

    assert body["phase"] == "ok"
    assert body["verified"] is True
    assert body["rows_checked"] == "504", "a count the shell wrote as a string stays one"
    assert body["offsite"] == "same-filesystem"
    assert body["dump_revision"] == "a1b2c3d4e5f6"
    assert body["log_tail"] == ["one", "two"]
    assert body["available"] is True


def test_nothing_has_been_backed_up_yet_is_said_rather_than_implied(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The drop-box exists, so the regime is installed; no record means it has never run.

    Those are different states and the page says different things about them, so the API
    must not flatten the second into "failed" or into silence.
    """
    body = auth_client.get("/api/admin/backup-status").json()

    assert body["available"] is True, "the drop-box is there, so the button works"
    assert body["phase"] is None, "no record means unknown, not ok and not failed"
    assert body["verified"] is None, "and certainly not that an absent backup was verified"
    assert body["last_verified_at"] is None
    assert body["message"] is None


def test_backups_not_set_up_is_distinguishable_from_never_run(
    auth_client: TestClient, no_state_dir: None
) -> None:
    """Two warnings with different fixes: install the host side, versus wait a night."""
    body = auth_client.get("/api/admin/backup-status").json()

    assert body["available"] is False
    assert body["phase"] is None


def test_an_unverified_dump_is_reported_as_unverified_not_as_unknown(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The worst outcome the regime can report, and it must survive the round trip.

    A dump that was taken and could not be restored is not a backup. `verified=false` and
    `verified=null` get different sentences from the page -- "it could not be restored"
    against "nothing has proved it either way" -- so False must not arrive as None.
    """
    _write_status(
        state_dir,
        phase="failed",
        dump_name="saiva-20260101-020304-nightly.sql.gz",
        verified="false",
        verify_message="the dump is missing its completion marker",
        last_verified_at="2025-12-20T02:05:00Z",
    )

    body = auth_client.get("/api/admin/backup-status").json()

    assert body["verified"] is False
    assert "completion marker" in body["verify_message"]
    assert body["last_verified_at"] == "2025-12-20T02:05:00Z", (
        "a failed run must not erase the last backup that WAS verified"
    )


def test_a_skipped_run_keeps_the_carried_forward_timestamps(
    auth_client: TestClient, state_dir: Path
) -> None:
    """`skipped` is not a failure and not a success, and the API invents neither.

    The host writes it when a release holds the deploy lock or the stack is down, and
    leaves last_success_at / last_verified_at alone on purpose, so the age of the newest
    verified backup keeps ageing. Nothing here may fill those in.
    """
    _write_status(
        state_dir,
        phase="skipped",
        step="skipped",
        message="A release held the deploy lock, so no backup was taken this time.",
        last_success_at="2026-01-01T02:05:00Z",
        last_verified_at="2026-01-01T02:06:00Z",
    )

    body = auth_client.get("/api/admin/backup-status").json()

    assert body["phase"] == "skipped"
    assert body["last_verified_at"] == "2026-01-01T02:06:00Z"
    assert body["verified"] is None, "a skipped run proved nothing about any dump"


def test_a_malformed_record_does_not_break_the_page(
    auth_client: TestClient, state_dir: Path
) -> None:
    (state_dir / "backup-status.json").write_text("{not json at all", encoding="utf-8")

    response = auth_client.get("/api/admin/backup-status")

    assert response.status_code == 200
    assert response.json()["phase"] == "unreadable"


def test_a_record_in_an_unexpected_shape_does_not_break_the_page(
    auth_client: TestClient, state_dir: Path
) -> None:
    """A field of the wrong type is reported, not raised.

    `verified` is the likely one: the renderer writes "true", and anything else a
    hand-edit or a future version puts there must not take the settings page down.
    """
    (state_dir / "backup-status.json").write_text(
        json.dumps({"phase": "ok", "verified": "probably"}), encoding="utf-8"
    )

    response = auth_client.get("/api/admin/backup-status")

    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == "unreadable"
    assert "backup.sh --status" in (body["message"] or "")
    assert body["verified"] is None, "an unreadable record must not claim a verified dump"


def test_the_record_cannot_overstate_what_this_box_can_do(
    auth_client: TestClient, state_dir: Path
) -> None:
    """`available` is the filesystem's answer, not the record's.

    A stale record copied from another box -- or restored with the state directory -- must
    not be able to turn on a button whose drop-box is not there.
    """
    _write_status(state_dir, phase="ok", available=True, stale_after_hours=999)
    (state_dir / "backup-requests").rmdir()

    body = auth_client.get("/api/admin/backup-status").json()

    assert body["available"] is False
    assert body["stale_after_hours"] != 999, "the record set the policy the owner chose"


def test_the_staleness_policy_comes_from_the_settings_and_not_from_the_page(
    auth_client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page must not hold a second copy of SAIVA_BACKUP_STALE_HOURS.

    If it did, a box that set 12 would still be told its 30-hour-old backup was current.
    """
    from app.api import admin

    body = auth_client.get("/api/admin/backup-status").json()
    assert body["stale_after_hours"] == admin.settings.backup_stale_hours
    monkeypatch.setattr(admin.settings, "backup_stale_hours", 12)

    assert auth_client.get("/api/admin/backup-status").json()["stale_after_hours"] == 12


def test_the_default_staleness_is_the_one_a_fresh_env_documents() -> None:
    """Two literals in two languages, and nothing else holds them together.

    .env.example is what an owner copies; the default here is what a box that never
    edited it gets. If they drift, every such box runs a policy neither file states, and
    the symptom is a page that calls a backup current for longer than the file says.

    The default is read off the model rather than off a live Settings instance so that a
    .env sitting next to the tests cannot make this pass or fail for the wrong reason.
    """
    from app.config import Settings

    default = Settings.model_fields["backup_stale_hours"].default
    env_example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()

    assert f"SAIVA_BACKUP_STALE_HOURS={default}" in env_example, (
        f"the default here is {default} hours and .env.example documents something else"
    )


# --------------------------------------------------------------------------------------
# Who may ask
# --------------------------------------------------------------------------------------


def test_only_the_owner_may_read_the_record_or_press_the_button(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Both endpoints are owner-only, and the GET for a reason beyond consistency: when
    the last backup was taken, and where it was copied, is reconnaissance."""
    adult = _non_owner(auth_client)
    assert adult.get("/api/auth/me").status_code == 200, "the second user never signed in"

    assert adult.get("/api/admin/backup-status").status_code == 403
    assert adult.post("/api/admin/backup").status_code == 403
    assert list((state_dir / "backup-requests").iterdir()) == [], (
        "a non-owner's refused request still reached the host"
    )
