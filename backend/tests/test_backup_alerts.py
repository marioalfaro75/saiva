"""The backup regime reaching the alerts feed (and the email that goes with it).

The sibling of test_backups.py, which proves the Settings page tells the truth about
the backups. This file is about the stronger claim: that nobody has to go and look. A
regime whose failure signal is a number on a page the owner may never open is weaker
than one that arrives, so the states that mean "you do not have a backup" are
candidates in app/services/notifications.py and are asserted here end to end.

Two properties carry most of the weight, and both are properties of the KEY, because
`generate()` dedupes on it across all time:

  * a box that is broken for three weeks says so ONCE -- a feed that repeats itself
    nightly is a feed its household learns to scroll past, and then the one alert that
    mattered is scrolled past too;
  * a box that is fixed and then breaks again says so AGAIN -- a key that could only
    ever fire once would buy the silence above at the price of the second failure.

The third is the one state that must stay silent: a box whose host side was never
installed. `make update` over SSH is a supported way to run this deployment and such a
box never wanted any of this, so an alert there would fire on every install that does
not use the feature -- which teaches the same lesson as nagging, faster.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import models
from app.db import SessionLocal
from app.services import notifications

# Every key the "how old is the backup" question can produce. One of these may be in a
# run and never two: they are different readings of one question, and two of them in one
# feed would be two contradictory sentences about the same box.
AGE_QUESTION_KEYS = (
    "backup_never_run",
    "backup_never_verified",
    "backup_stale:",
    "backup_clock_ahead",
    "backup_date_unreadable:",
    "backup_unreadable",
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The layout scripts/install-updater.sh builds, as test_backups.py builds it.

    The presence of backup-requests/ is what the app reads as "the host side is
    installed", so this fixture is the difference between state 1 (say nothing) and
    state 2 (nothing has ever run).
    """
    directory = tmp_path / "saiva-update"
    (directory / "requests").mkdir(parents=True)
    (directory / "backup-requests").mkdir(parents=True)
    monkeypatch.setenv("SAIVA_UPDATE_DIR", str(directory))
    return directory


@pytest.fixture
def no_regime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box updated over SSH: no state directory, so no drop-box and no record."""
    monkeypatch.delenv("SAIVA_UPDATE_DIR", raising=False)


def _write_status(directory: Path, **fields: object) -> None:
    (directory / "backup-status.json").write_text(json.dumps(fields), encoding="utf-8")


def _stamp(when: dt.datetime) -> str:
    """The format scripts/lib/state.sh writes: `date -u +%Y-%m-%dT%H:%M:%SZ`."""
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_ago(hours: float) -> str:
    return _stamp(dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours))


def _household_id(client: TestClient) -> str:
    return str(client.get("/api/auth/me").json()["household"]["id"])


def _generate(household_id: str) -> list[tuple[str, str, str, str]]:
    """One generate() pass; the backup alerts it created THIS time, as plain tuples."""
    with SessionLocal() as db:
        household = db.get(models.Household, household_id)
        assert household is not None
        created = notifications.generate(db, household)
        return [(n.key, n.severity, n.title, n.body) for n in created if n.type == "backup"]


def _stored(household_id: str) -> list[tuple[str, str, str, str]]:
    """Every backup alert in the household's feed, however many runs put it there."""
    with SessionLocal() as db:
        rows = (
            db.query(models.Notification)
            .filter(
                models.Notification.household_id == household_id,
                models.Notification.type == "backup",
            )
            .order_by(models.Notification.created_at)
            .all()
        )
        return [(n.key, n.severity, n.title, n.body) for n in rows]


def _only(created: list[tuple[str, str, str, str]]) -> tuple[str, str, str, str]:
    assert len(created) == 1, f"expected exactly one backup alert, got {created}"
    return created[0]


# --------------------------------------------------------------------------------------
# 1. No status file and no drop-box: say nothing
# --------------------------------------------------------------------------------------


def test_a_box_with_no_backup_regime_stays_silent(
    auth_client: TestClient, no_regime: None
) -> None:
    """The host side was never installed, so there is nothing to report on.

    This is also what keeps every other test in this suite honest: they construct
    households and run generate() on a box with no backup record at all, and a
    candidate here would appear in all of them.
    """
    household_id = _household_id(auth_client)

    assert _generate(household_id) == []
    assert _generate(household_id) == []
    assert _stored(household_id) == []


def test_the_feed_endpoint_says_nothing_about_backups_on_such_a_box(
    auth_client: TestClient, no_regime: None
) -> None:
    """The same thing through the surface the household actually reads."""
    items = auth_client.get("/api/notifications").json()["items"]

    assert [n for n in items if n["type"] == "backup"] == []


# --------------------------------------------------------------------------------------
# 2. The regime is installed and nothing has ever run
# --------------------------------------------------------------------------------------


def test_an_installed_regime_that_has_never_run_alerts_once(
    auth_client: TestClient, state_dir: Path
) -> None:
    household_id = _household_id(auth_client)

    key, severity, title, body = _only(_generate(household_id))

    assert key == "backup_never_run"
    assert severity == "alert"
    assert "no backup" in f"{title} {body}".lower()
    assert "backup.sh" in body, "it must name the thing that fixes it"
    # And not again, however many times the cron or a browser runs the generator.
    assert _generate(household_id) == []
    assert _generate(household_id) == []
    assert len(_stored(household_id)) == 1


def test_a_record_that_has_never_reached_a_dump_is_the_same_state(
    auth_client: TestClient, state_dir: Path
) -> None:
    """A run that failed before taking anything is still "nothing has been backed up".

    Deliberately the same key as the no-record case: it is one state told by a different
    file, and two keys would mean a box that failed its very first run says it twice.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="20260101T000000Z-1",
        phase="failed",
        step="preflight",
        message="the stack is down",
    )

    key, severity, _, _ = _only(_generate(household_id))

    assert key == "backup_never_run"
    assert severity == "alert"


# --------------------------------------------------------------------------------------
# 3. A dump was taken and it does not restore
# --------------------------------------------------------------------------------------


def test_verified_false_is_the_loud_one_and_quotes_the_reason(
    auth_client: TestClient, state_dir: Path
) -> None:
    household_id = _household_id(auth_client)
    reason = "Row counts disagree: the dump declares 504 rows and 19 came back."
    _write_status(
        state_dir,
        run_id="20260310T030000Z-4242",
        phase="failed",
        # The host's renderer has one type: every field is a JSON string or null, so
        # `verified` arrives as the word rather than as a boolean.
        verified="false",
        verify_message=reason,
        dump_name="saiva-20260310-030000-nightly.sql.gz",
        last_success_at=_hours_ago(2),
        last_verified_at=_hours_ago(2),
    )

    created = _generate(household_id)
    failures = [c for c in created if c[0].startswith("backup_verify_failed:")]
    key, severity, title, body = _only(failures)

    assert key == "backup_verify_failed:20260310T030000Z-4242", (
        "the key must identify the run, so tonight's broken dump is news again"
    )
    assert severity == "alert"
    assert "not a backup" in title
    assert reason in body, "the host's own diagnosis is the whole value of this alert"


def test_a_second_failing_run_is_a_second_alert(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Each night's dump is a fresh dump that was taken and found broken."""
    household_id = _household_id(auth_client)
    fresh = _hours_ago(2)
    _write_status(
        state_dir,
        run_id="run-one",
        verified="false",
        verify_message="truncated",
        last_verified_at=fresh,
    )
    assert [c[0] for c in _generate(household_id)] == ["backup_verify_failed:run-one"]

    _write_status(
        state_dir,
        run_id="run-two",
        verified="false",
        verify_message="truncated",
        last_verified_at=fresh,
    )
    second = _generate(household_id)

    assert [c[0] for c in second] == ["backup_verify_failed:run-two"]


def test_a_first_run_that_fails_its_verification_says_both_true_things(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The one state where two candidates are right, and they must not disagree.

    The very first nightly run takes a dump and the verification fails. The record then
    carries a broken dump and no successful run at all, so both sentences are true:
    there is no backup of this household's data, AND a dump was taken that will not
    restore. The Settings page shows the same pair. What this guards is the wording --
    "no dump has ever been taken" beside "a dump was taken" would be the self-
    contradicting feed the whole design exists to avoid.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="first-ever",
        phase="failed",
        verified="false",
        verify_message="The restored copy has no `households` table.",
    )

    created = _generate(household_id)
    by_key = {key: body for key, _, _, body in created}

    assert sorted(by_key) == ["backup_never_run", "backup_verify_failed:first-ever"]
    absent = by_key["backup_never_run"].lower()
    assert "no run has ever finished successfully" in absent
    assert "no dump" not in absent and "nothing was dumped" not in absent, (
        "it must not deny the dump the other alert is about"
    )


def test_a_verified_field_this_version_cannot_read_is_not_a_failure_either(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Three answers, not two: proved, disproved, and not known.

    A value that is neither word must not be read as a failed verification (which would
    be an assertion nobody made) nor as a passing one (which is the lie the whole regime
    exists to remove -- the age below is what speaks instead).
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir, run_id="odd", verified="maybe", last_verified_at=_hours_ago(1)
    )

    assert _generate(household_id) == []


# --------------------------------------------------------------------------------------
# 4. Dumps are being taken and nothing has ever been restored from one
# --------------------------------------------------------------------------------------


def test_dumps_that_have_never_been_verified_are_an_alert(
    auth_client: TestClient, state_dir: Path
) -> None:
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="20260310T030000Z-9",
        phase="ok",
        last_success_at=_hours_ago(3),
        # last_verified_at absent: no dump on this box has ever been restored.
    )

    key, severity, title, body = _only(_generate(household_id))

    assert key == "backup_never_verified"
    assert severity == "alert"
    assert "verified" in title.lower()
    assert "verify-only" in body, "it must name the command that turns this into a backup"
    # Not every night a fresh unverified dump lands: nothing about the state has changed.
    _write_status(state_dir, run_id="later", phase="ok", last_success_at=_hours_ago(1))
    assert _generate(household_id) == []


# --------------------------------------------------------------------------------------
# 5. The age of the newest verified backup
# --------------------------------------------------------------------------------------


def test_a_stale_box_alerts_once_across_repeated_runs(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Three weeks of a broken timer is one alert, not twenty-one."""
    household_id = _household_id(auth_client)
    verified_at = _hours_ago(24 * 21)
    _write_status(
        state_dir, run_id="20260301T030000Z-1", phase="ok", last_verified_at=verified_at
    )

    key, severity, title, body = _only(_generate(household_id))

    assert key.startswith("backup_stale:")
    assert severity == "warn"
    assert "21 days ago" in title, f"the age has to be in the headline: {title}"
    assert "36 hours" in body, "and the threshold it is being compared against"
    assert "SAIVA_BACKUP_STALE_HOURS" in body

    for _ in range(5):
        assert _generate(household_id) == []
    assert len(_stored(household_id)) == 1


def test_the_key_is_the_timestamp_and_not_the_day_it_was_noticed(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The anti-nagging property, asserted on the key itself.

    A key carrying today's date would be a new key every night, so a box that is stale
    for a month would produce thirty identical alerts. The stale key therefore carries
    the timestamp it is stale ABOUT -- the one value that does not move while the
    nightly run is broken.
    """
    household_id = _household_id(auth_client)
    verified_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=9)
    _write_status(state_dir, last_verified_at=_stamp(verified_at))

    key = _only(_generate(household_id))[0]

    assert key == f"backup_stale:{verified_at.strftime('%Y%m%dT%H%M%SZ')}"
    today = dt.date.today()
    assert today.isoformat() not in key and today.strftime("%Y%m%d") not in key, (
        "a key that moves with today's date nags every night"
    )


def test_a_good_backup_then_stale_again_alerts_a_second_time(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The other half: silence must not be bought by never speaking twice."""
    household_id = _household_id(auth_client)
    first_stale = _hours_ago(24 * 4)
    _write_status(state_dir, last_verified_at=first_stale)
    first = _only(_generate(household_id))[0]

    # The timer is fixed and a run verifies a dump. Nothing new to say.
    _write_status(state_dir, phase="ok", last_verified_at=_hours_ago(1), offsite="remote")
    assert _generate(household_id) == []

    # And then it breaks again, with a different newest verified backup.
    second_stale = _hours_ago(24 * 3)
    _write_status(state_dir, last_verified_at=second_stale, offsite="remote")
    second = _only(_generate(household_id))[0]

    assert second != first, "the second stale period must be audible"
    assert second.startswith("backup_stale:")
    keys = [row[0] for row in _stored(household_id)]
    assert keys == [first, second]


def test_a_fresh_verified_backup_with_a_copy_off_the_box_says_nothing(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The feed is not where good news goes."""
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="20260310T030000Z-1",
        phase="ok",
        verified="true",
        verify_message="Restored into a throwaway database: 504 rows across 14 tables.",
        last_success_at=_hours_ago(4),
        last_verified_at=_hours_ago(4),
        offsite="remote",
        offsite_target="/mnt/nas/saiva",
    )

    assert _generate(household_id) == []


# --------------------------------------------------------------------------------------
# 6. A timestamp in the future
# --------------------------------------------------------------------------------------


def test_a_future_timestamp_warns_about_the_clock_and_never_shows_a_negative_age(
    auth_client: TestClient, state_dir: Path
) -> None:
    """An LXC takes its clock from the Proxmox host; a restored snapshot does this.

    The age is not reported at all, because the clock it would be measured against is
    the thing in doubt. The skew is, as a positive span.
    """
    household_id = _household_id(auth_client)
    ahead = _stamp(dt.datetime.now(dt.UTC) + dt.timedelta(hours=30))
    _write_status(state_dir, phase="ok", last_verified_at=ahead)

    key, severity, title, body = _only(_generate(household_id))

    assert key == "backup_clock_ahead"
    assert severity == "warn"
    assert "future" in title.lower()
    assert "29 hours ahead" in body or "30 hours ahead" in body, body
    text = f"{title} {body}"
    assert not re.search(r"-\s*\d+(\.\d+)?\s*(second|minute|hour|day|week)", text), (
        f"a negative age reached the owner: {text}"
    )
    assert "ago" not in text, (
        "an age claim at all is wrong here: the clock it would be measured against is "
        f"the thing in doubt. {text}"
    )


def test_a_host_clock_that_stays_ahead_does_not_nag_nightly(
    auth_client: TestClient, state_dir: Path
) -> None:
    """A skewed clock writes a NEW future timestamp every night, all one unchanged fact."""
    household_id = _household_id(auth_client)
    _write_status(
        state_dir, last_verified_at=_stamp(dt.datetime.now(dt.UTC) + dt.timedelta(days=2))
    )
    assert len(_generate(household_id)) == 1

    _write_status(
        state_dir, last_verified_at=_stamp(dt.datetime.now(dt.UTC) + dt.timedelta(days=3))
    )

    assert _generate(household_id) == []


def test_the_span_helper_cannot_render_a_negative_duration() -> None:
    """The second guard, under the branch rather than beside it."""
    assert notifications._span(dt.timedelta(hours=-9)) == "0 minutes"
    assert notifications._span(dt.timedelta(minutes=-1)) == "0 minutes"
    assert notifications._span(dt.timedelta(hours=9)) == "9 hours"
    assert notifications._span(dt.timedelta(days=3)) == "3 days"


def test_a_timestamp_that_is_not_a_time_says_so_rather_than_guessing(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Neither stale nor absent: there is a value and it cannot be read."""
    household_id = _household_id(auth_client)
    _write_status(state_dir, phase="ok", last_verified_at="last Tuesday")

    key, severity, title, body = _only(_generate(household_id))

    assert key == "backup_date_unreadable:last Tuesday"
    assert severity == "warn"
    assert "readable" in title.lower()
    assert "last Tuesday" in body
    assert "stale" not in body.lower()


def test_a_blank_timestamp_is_an_absent_one_and_not_an_unreadable_one(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The host's renderer writes null for an empty value, but a record can be edited.

    An empty or whitespace-only field is the same answer as a missing one -- the host
    has never recorded a verified backup -- and reporting it as "the date cannot be
    read" would be a different and wrong sentence.
    """
    household_id = _household_id(auth_client)
    _write_status(state_dir, phase="ok", last_verified_at="   ", last_success_at=_hours_ago(3))

    key = _only(_generate(household_id))[0]

    assert key == "backup_never_verified"


# --------------------------------------------------------------------------------------
# 7. The record itself cannot be read
# --------------------------------------------------------------------------------------


def test_an_unreadable_record_is_reported_as_unreadable(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Not being able to say anything is not the same as there being no backup."""
    household_id = _household_id(auth_client)
    (state_dir / "backup-status.json").write_text("{not json at all", encoding="utf-8")

    key, severity, title, body = _only(_generate(household_id))

    assert key == "backup_unreadable"
    assert severity == "warn"
    assert "could not be read" in title.lower()
    assert "backup.sh --status" in body
    assert "no backup" not in body.lower(), (
        "an unparseable record must not be reported as an absent backup"
    )
    assert _generate(household_id) == []


# --------------------------------------------------------------------------------------
# One sentence per question
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({}, id="a record with nothing in it"),
        pytest.param(
            {"phase": "ok", "last_success_at": "2026-01-01T00:00:00Z"},
            id="never verified",
        ),
        pytest.param(
            {"phase": "ok", "last_success_at": "2026-01-01T00:00:00Z",
             "last_verified_at": "2026-01-01T00:00:00Z"},
            id="stale",
        ),
        pytest.param({"phase": "ok", "last_verified_at": "2999-01-01T00:00:00Z"}, id="future"),
        pytest.param({"phase": "ok", "last_verified_at": "whenever"}, id="unreadable date"),
        pytest.param({"phase": "unreadable", "message": "no"}, id="unreadable record"),
        pytest.param(
            {"phase": "failed", "verified": "false", "run_id": "r1", "offsite": "off",
             "last_verified_at": "2020-01-01T00:00:00Z"},
            id="everything wrong at once",
        ),
    ],
)
def test_only_one_sentence_about_how_old_the_backup_is(
    auth_client: TestClient, state_dir: Path, record: dict[str, object]
) -> None:
    """States 2 to 7 are readings of ONE question, so only one of them may speak.

    Two in a feed would be two contradictory sentences about the same box -- "nothing
    has ever been backed up" above "last verified backup: 9 hours ago" -- which is the
    exact failure this whole regime exists to prevent, delivered by the thing meant to
    prevent it.
    """
    household_id = _household_id(auth_client)
    _write_status(state_dir, **record)

    keys = [key for key, *_ in _generate(household_id)]
    about_the_age = [k for k in keys if any(k.startswith(p) for p in AGE_QUESTION_KEYS)]

    assert len(about_the_age) <= 1, f"two readings of one question: {about_the_age}"


# --------------------------------------------------------------------------------------
# 8. The off-box copy
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", ["off", "same-host", "same-filesystem", "unknown", "failed", "sideways"]
)
def test_a_copy_that_did_not_leave_the_box_never_reads_as_good_news(
    auth_client: TestClient, state_dir: Path, state: str
) -> None:
    """`remote` is the only state that means the copy left the machine.

    The last parameter is not in SAIVA_OFFSITE_STATES on purpose: a state a future host
    gains and this version has not learned must be rendered as itself, not assumed
    benign. backend/tests/test_deployment_hardening.py holds the declared vocabulary and
    the panel together; this holds the declared vocabulary and the feed together.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="ok",
        verified="true",
        last_verified_at=_hours_ago(2),
        offsite=state,
        offsite_message="the host's own words about it",
    )

    created = _generate(household_id)
    key, severity, title, body = _only([c for c in created if c[0].startswith("backup_offsite:")])

    assert key == f"backup_offsite:{state}"
    assert severity == "warn", "warn and not info: info is never emailed"
    assert "the host's own words about it" in body
    assert state != "remote"
    # One per distinct state: a standing configuration fact is not nightly news.
    assert _generate(household_id) == []


def test_a_copy_that_left_the_box_produces_nothing(
    auth_client: TestClient, state_dir: Path
) -> None:
    household_id = _household_id(auth_client)
    _write_status(
        state_dir, phase="ok", verified="true", last_verified_at=_hours_ago(2), offsite="remote"
    )

    assert _generate(household_id) == []


def test_the_off_box_state_getting_worse_is_audible_again(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Keyed on the state, so a destination that breaks is new news."""
    household_id = _household_id(auth_client)
    _write_status(state_dir, last_verified_at=_hours_ago(2), offsite="same-host")
    assert [c[0] for c in _generate(household_id)] == ["backup_offsite:same-host"]

    _write_status(state_dir, last_verified_at=_hours_ago(2), offsite="failed")

    assert [c[0] for c in _generate(household_id)] == ["backup_offsite:failed"]


def test_an_absent_off_box_field_says_nothing(
    auth_client: TestClient, state_dir: Path
) -> None:
    """A run that never reached the copy step has not reported on it."""
    household_id = _household_id(auth_client)
    _write_status(state_dir, phase="ok", verified="true", last_verified_at=_hours_ago(2))

    assert _generate(household_id) == []


# --------------------------------------------------------------------------------------
# It is a property of the box, so it is read once and reaches every household
# --------------------------------------------------------------------------------------


def test_the_record_is_read_once_per_run_not_once_per_household(
    auth_client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_backup_status() opens a file, and the answer cannot differ per household.

    It also must not be read lazily per household and then disagree with itself: a run
    that straddles a nightly backup would otherwise tell one household the box is stale
    and the next that it is fine, out of the same cron invocation.
    """
    _write_status(state_dir, last_verified_at=_hours_ago(24 * 5))
    with SessionLocal() as db:
        db.add(models.Household(name="The other household", state="VIC"))
        db.commit()

    reads = []
    real = notifications.backups.read_backup_status
    monkeypatch.setattr(
        notifications.backups,
        "read_backup_status",
        lambda: (reads.append(1), real())[1],
    )

    with SessionLocal() as db:
        totals = notifications.run_all(db)

    assert totals["households"] == 2, "the second household was not generated for"
    assert len(reads) == 1, f"the record was read {len(reads)} times for one run"


def test_the_same_box_alert_reaches_every_household_on_it(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Two households on one box are two sets of rows in one database.

    "There is no verified backup" is therefore true for both, and belongs in both feeds:
    their owners may be different people, and whoever reads it is whoever can act.
    """
    _write_status(state_dir, last_verified_at=_hours_ago(24 * 5))
    with SessionLocal() as db:
        other = models.Household(name="The other household", state="VIC")
        db.add(other)
        db.commit()
        other_id = other.id

    with SessionLocal() as db:
        notifications.run_all(db)

    first = _stored(_household_id(auth_client))
    second = _stored(other_id)
    assert len(first) == 1 and len(second) == 1
    assert first[0][0] == second[0][0], "the same state, so the same key, in both feeds"


# --------------------------------------------------------------------------------------
# And it gets out of the app
# --------------------------------------------------------------------------------------


def test_the_backup_alert_is_emailed_like_every_other_alert(
    auth_client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the exercise: nobody has to open the Settings page.

    Driven through run_for_household so the whole path is the real one -- generate,
    _pending_email's severity filter, _alerts_body, send_email -- with only the SMTP
    transport replaced.
    """
    household_id = _household_id(auth_client)
    # A box that had good backups until tonight: the age is fine and the regime is
    # failing now, which is one alert and not two.
    _write_status(
        state_dir,
        run_id="20260310T030000Z-7",
        phase="failed",
        verified="false",
        verify_message="Row counts disagree: 504 declared, 19 restored.",
        last_success_at=_hours_ago(26),
        last_verified_at=_hours_ago(26),
        offsite="remote",
    )
    with SessionLocal() as db:
        notifications.settings_for(db, household_id).email_enabled = True
        db.commit()

    sent: list[tuple] = []
    monkeypatch.setattr(
        notifications, "send_email", lambda *a, **k: (sent.append(a), True)[1]
    )
    monkeypatch.setattr(
        notifications.get_settings(), "smtp_host", "smtp.example.com", raising=False
    )
    monkeypatch.setattr(
        notifications.get_settings(), "smtp_from", "saiva@example.com", raising=False
    )

    with SessionLocal() as db:
        household = db.get(models.Household, household_id)
        assert household is not None
        result = notifications.run_for_household(db, household)

    assert result["emailed"] == 1, "the backup alert never reached the email path"
    recipients, subject, body = sent[0][:3]
    assert recipients == ["owner@example.com"]
    assert "[alert]" in body, "it must carry its severity like every other alert"
    assert "not a backup" in body
    assert "504 declared, 19 restored" in body, "and the host's diagnosis with it"


def test_the_feed_endpoint_carries_the_backup_alert_with_a_link_to_the_page(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The in-app half, through the surface the household reads."""
    _write_status(state_dir, last_verified_at=_hours_ago(24 * 6))

    items = auth_client.get("/api/notifications").json()["items"]
    backup = [n for n in items if n["type"] == "backup"]

    assert len(backup) == 1, items
    assert backup[0]["severity"] == "warn"
    assert backup[0]["link"] == "/settings", "the alert has to land where the record is"
    assert backup[0]["amount_cents"] is None, "there is no money in this one"
