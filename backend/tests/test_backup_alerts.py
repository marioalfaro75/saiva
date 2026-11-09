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
from app.config import get_settings
from app.db import SessionLocal
from app.services import backups, notifications

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


def test_a_box_with_no_backup_regime_stays_silent(auth_client: TestClient, no_regime: None) -> None:
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
    file, and a second key for the same state would mean a box that failed its very
    first run says the same thing twice.

    The failure itself is a different question and gets its own, quieter row -- see
    section 21. "Nothing has ever been backed up" is the state; "it died at the
    preflight" is tonight's reason; they do not contradict each other, and before the
    review the reason was not reported at all.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="20260101T000000Z-1",
        phase="failed",
        step="preflight",
        message="the stack is down",
    )

    by_key = {key: (sev, body) for key, sev, _title, body in _generate(household_id)}

    assert sorted(by_key) == ["backup_never_run", "backup_run_failed:preflight"]
    assert by_key["backup_never_run"][0] == "alert"
    assert by_key["backup_run_failed:preflight"][0] == "warn"
    assert "the stack is down" in by_key["backup_run_failed:preflight"][1]


# --------------------------------------------------------------------------------------
# 3. A dump was taken and it does not restore
# --------------------------------------------------------------------------------------


def test_verified_false_names_the_dump_and_quotes_the_reason(
    auth_client: TestClient, state_dir: Path
) -> None:
    """How loudly it says this depends on the age; WHAT it says does not.

    This record has a verified backup from two hours ago, so it is `warn` -- see
    test_how_loud_a_broken_dump_is_depends_on_whether_a_backup_still_exists.
    """
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

    assert key == "backup_verify_failed:saiva-20260310-030000-nightly.sql.gz", (
        "the key must name the DUMP: the verdict is about a dump, and it is carried "
        "forward across runs that never looked at one"
    )
    assert severity == "warn"
    assert "not a backup" in title
    assert reason in body, "the host's own diagnosis is the whole value of this alert"


def test_a_verdict_with_no_dump_to_name_speaks_once_and_not_nightly(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The fallback, when the record reports a verdict with nothing to attach it to.

    take_dump() sets dump_name and the verdict together, so this record can really only
    have been hand-edited -- but the key has to be BARE rather than falling back to the
    run, because falling back to the run is precisely what made this alert nag every
    night. Said once is the right amount for a state nothing can identify.
    """
    household_id = _household_id(auth_client)
    fresh = _hours_ago(2)
    for run in ("run-one", "run-two"):
        _write_status(
            state_dir,
            run_id=run,
            verified="false",
            verify_message="truncated",
            last_verified_at=fresh,
        )
        created = [c[0] for c in _generate(household_id)]
        assert created == (["backup_verify_failed"] if run == "run-one" else []), (
            f"{run} produced {created}"
        )

    assert len(_stored(household_id)) == 1


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
        step="verify",
        dump_name="saiva-20260101-030000-nightly.sql.gz",
        verified="false",
        verify_message="The restored copy has no `households` table.",
    )

    created = _generate(household_id)
    by_key = {key: body for key, _, _, body in created}

    assert sorted(by_key) == [
        "backup_never_run",
        "backup_verify_failed:saiva-20260101-030000-nightly.sql.gz",
    ]
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
    _write_status(state_dir, run_id="odd", verified="maybe", last_verified_at=_hours_ago(1))

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
    _write_status(state_dir, run_id="20260301T030000Z-1", phase="ok", last_verified_at=verified_at)

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
            {
                "phase": "ok",
                "last_success_at": "2026-01-01T00:00:00Z",
                "last_verified_at": "2026-01-01T00:00:00Z",
            },
            id="stale",
        ),
        pytest.param({"phase": "ok", "last_verified_at": "2999-01-01T00:00:00Z"}, id="future"),
        pytest.param({"phase": "ok", "last_verified_at": "whenever"}, id="unreadable date"),
        pytest.param({"phase": "unreadable", "message": "no"}, id="unreadable record"),
        pytest.param(
            {
                "phase": "failed",
                "verified": "false",
                "run_id": "r1",
                "offsite": "off",
                "last_verified_at": "2020-01-01T00:00:00Z",
            },
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


def test_an_absent_off_box_field_says_nothing(auth_client: TestClient, state_dir: Path) -> None:
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
    monkeypatch.setattr(notifications, "send_email", lambda *a, **k: (sent.append(a), True)[1])
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
    assert "[warn]" in body, (
        "it must carry its severity like every other alert -- and `warn` is what this "
        "record now earns, because a backup verified 26 hours ago is still inside the "
        "window. `alert` is reserved for having nothing to restore from; both are "
        "emailed, which is what this test is really about."
    )
    assert "not without a backup" in body
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


# --------------------------------------------------------------------------------------
# 21. What an adversarial review of this module found
#
# Four reviewers with different lenses reported the same blocking defect, and the pass
# that tried to refute it reproduced it by driving the real scripts/backup.sh rather
# than by argument. The mutation lens then proved several assertions above vacuous: the
# staleness threshold, which timestamp the age is measured from, the state-to-sentence
# mapping and the alert's link all survived being broken. Each test below failed, or
# passed vacuously, against the code as first written.
# --------------------------------------------------------------------------------------


def test_a_broken_dump_is_one_alert_however_many_nights_it_stands(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The blocking finding: the key named the RUN when the verdict is about the DUMP.

    scripts/backup.sh backup_begin() clears step/message/exit_code/finished_at/pruned
    and writes a new run_id, but deliberately leaves the verdict group standing --
    only a run that looked at a dump may change what is known about one. So a night
    that never reaches take_dump, because the stack is down or a preflight dies, leaves
    last night's `verified: false` beside a brand-new run_id. Keyed on the run, that
    minted a fresh alert and a fresh email EVERY NIGHT, each asserting that a dump had
    been taken on a night the record's own message says nothing was dumped.
    """
    household_id = _household_id(auth_client)
    broken = {
        "dump_name": "saiva-20261109-030000-nightly.sql.gz",
        "verified": "false",
        "verify_message": "row counts differ",
        "last_success_at": _hours_ago(27),
        "last_verified_at": _hours_ago(27),
        "offsite": "remote",
    }
    _write_status(state_dir, run_id="20261109T030000Z-22", phase="failed", step="verify", **broken)
    assert _only(_generate(household_id))[0] == (
        "backup_verify_failed:saiva-20261109-030000-nightly.sql.gz"
    ), "the key has to name the dump, which is the thing the verdict is about"

    for run in ("20261110T030000Z-33", "20261111T030000Z-44"):
        _write_status(
            state_dir,
            run_id=run,
            phase="skipped",
            step="skipped",
            exit_code="0",
            message="The stack is down -- nothing was dumped.",
            **broken,
        )
        assert _generate(household_id) == [], f"{run} re-alerted about a dump taken two nights ago"

    assert len(_stored(household_id)) == 1


def test_a_genuinely_new_broken_dump_is_a_second_alert(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The other half: silence must not be bought at the price of the next failure."""
    household_id = _household_id(auth_client)
    names = (
        "saiva-20261109-030000-nightly.sql.gz",
        "saiva-20261110-030000-nightly.sql.gz",
    )
    for name in names:
        _write_status(
            state_dir,
            run_id=f"run-for-{name}",
            phase="failed",
            step="verify",
            dump_name=name,
            verified="false",
            verify_message="the gzip stream ended early",
            last_success_at=_hours_ago(2),
            last_verified_at=_hours_ago(27),
            offsite="remote",
        )
        assert _only(_generate(household_id))[0] == f"backup_verify_failed:{name}"

    assert len(_stored(household_id)) == 2


def test_how_loud_a_broken_dump_is_depends_on_whether_a_backup_still_exists(
    auth_client: TestClient, state_dir: Path
) -> None:
    """`alert` is reserved for "you have no backup you can restore from".

    The rule is stated in _backup_offsite_candidate's docstring and this branch broke
    it: a run can fail its verification while a dump verified nine hours ago sits on a
    NAS. Shouting there sent the owner to a Settings page that renders the very same
    record at `warn` and tells them, correctly, that they are not without a backup.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="failed",
        step="verify",
        dump_name="saiva-20261109-030000-nightly.sql.gz",
        verified="false",
        verify_message="row counts differ",
        last_verified_at=_hours_ago(9),
        last_success_at=_hours_ago(9),
        offsite="remote",
    )
    key, severity, _title, body = _only(_generate(household_id))
    assert key.startswith("backup_verify_failed:")
    assert severity == "warn", "there is a verified backup from nine hours ago"
    assert "not without a backup" in body

    # The same failure on a box whose newest verified backup has aged out IS the
    # loudest thing it can say, and the age ladder speaks beside it.
    _write_status(
        state_dir,
        run_id="r2",
        phase="failed",
        step="verify",
        dump_name="saiva-20261110-030000-nightly.sql.gz",
        verified="false",
        verify_message="row counts differ",
        last_verified_at=_hours_ago(99),
        last_success_at=_hours_ago(2),
        offsite="remote",
    )
    by_key = {key: severity for key, severity, _t, _b in _generate(household_id)}
    assert by_key["backup_verify_failed:saiva-20261110-030000-nightly.sql.gz"] == "alert"
    assert any(key.startswith("backup_stale:") for key in by_key)


def test_a_run_that_never_reached_a_dump_still_says_so(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The silent-failure finding, and disk space is what makes it matter.

    db_data and db_backups share one filesystem, so the host's own refusal says this
    matters MORE than a failed backup: filling that disk stops Postgres, not merely the
    backup. The feed read only last_verified_at, verified and offsite, so this night
    produced no candidate at all and the box stayed quiet for the whole stale window
    before blaming the age.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="20261110T030000Z-66",
        phase="failed",
        step="room",
        exit_code="1",
        message=(
            "Only 180 MB free where the dumps live, and this needs about 2.1 GB. "
            "Nothing was dumped."
        ),
        last_success_at=_hours_ago(27),
        last_verified_at=_hours_ago(27),
        offsite="remote",
    )

    key, severity, title, body = _only(_generate(household_id))

    assert key == "backup_run_failed:room"
    assert severity == "warn", "a failed run is not by itself the absence of a backup"
    assert "180 MB free" in body, "the host's own reason has to travel with it"
    assert "room" in body
    assert "has been backed up" not in title

    for _ in range(3):
        assert _generate(household_id) == [], "one alert per failing step, not per night"

    # A box that starts failing somewhere else is telling you something new.
    _write_status(
        state_dir,
        run_id="20261111T030000Z-77",
        phase="failed",
        step="preflight",
        message="Docker is not answering on this host.",
        last_success_at=_hours_ago(51),
        last_verified_at=_hours_ago(51),
        offsite="remote",
    )
    assert "backup_run_failed:preflight" in [k for k, _s, _t, _b in _generate(household_id)]


def test_a_failed_run_does_not_speak_twice_about_one_night(
    auth_client: TestClient, state_dir: Path
) -> None:
    """A verification that failed is more specific than "the run failed", so it wins."""
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="failed",
        step="verify",
        dump_name="saiva-20261109-030000-nightly.sql.gz",
        verified="false",
        verify_message="row counts differ",
        last_verified_at=_hours_ago(9),
        last_success_at=_hours_ago(9),
        offsite="remote",
    )
    keys = [k for k, _s, _t, _b in _generate(household_id)]
    assert keys == ["backup_verify_failed:saiva-20261109-030000-nightly.sql.gz"]


def test_the_first_run_in_flight_does_not_claim_there_is_no_backup(
    auth_client: TestClient, state_dir: Path
) -> None:
    """backup_begin() writes its record before there is anything to report.

    A run_id, phase `running`, started_at, and no timestamps at all -- which the age
    ladder reads as "nothing has ever been backed up". True for as long as the run
    takes; but the key is bare and the severity is emailed, so the row could never be
    retracted and would never fire again once the backup succeeded. A permanent "you
    have no backup" on a box that has one.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="first",
        phase="running",
        started_at=_stamp(dt.datetime.now(dt.UTC)),
    )

    assert _generate(household_id) == []

    # Once that run is no longer live, the same absence is reported honestly.
    _write_status(state_dir, run_id="first", phase="failed", step="dump", message="pg_dump died")
    assert "backup_never_run" in [k for k, _s, _t, _b in _generate(household_id)]


def test_the_staleness_threshold_is_the_setting_and_not_a_number_in_this_module(
    auth_client: TestClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation-proof for the policy: hard-coding 36, and doubling it, both left green.

    Every age test above used the default, so any threshold that happened to put their
    fixtures on the right side of the line passed. This holds one record to both
    answers.
    """
    household_id = _household_id(auth_client)
    _write_status(state_dir, run_id="r1", phase="ok", last_verified_at=_hours_ago(10))

    monkeypatch.setattr(get_settings(), "backup_stale_hours", 48)
    assert _generate(household_id) == [], "ten hours is not stale where the box allows 48"

    monkeypatch.setattr(get_settings(), "backup_stale_hours", 6)
    key, _severity, _title, body = _only(_generate(household_id))
    assert key.startswith("backup_stale:")
    assert "6 hours" in body, "the body has to quote the threshold it actually applied"


def test_the_age_is_measured_from_the_verified_backup_and_not_the_last_dump(
    auth_client: TestClient, state_dir: Path
) -> None:
    """Measuring from last_success_at instead survived all 39 tests.

    It is the difference between "a dump was taken an hour ago" and "a dump was PROVED
    restorable an hour ago", which is the one distinction this whole regime exists to
    make. A box dumping nightly and verifying none of them has to read as stale.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="ok",
        last_success_at=_hours_ago(1),
        last_verified_at=_hours_ago(24 * 5),
        offsite="remote",
    )

    key, _severity, title, _body = _only(_generate(household_id))

    assert key.startswith("backup_stale:")
    assert "5 days ago" in title, (
        "the age must come from last_verified_at -- last_success_at was an hour ago"
    )


@pytest.mark.parametrize(
    ("state", "must_say"),
    [
        ("off", "No off-box destination is configured"),
        ("same-filesystem", "same filesystem the database lives on"),
        ("same-host", "this same machine"),
        ("unknown", "nothing here could establish"),
        ("failed", "could not be written to"),
    ],
)
def test_each_off_box_state_is_told_in_its_own_words(
    auth_client: TestClient, state_dir: Path, state: str, must_say: str
) -> None:
    """Mutation-proof for the vocabulary, which the test above left unpinned.

    That one asserted the key and the severity only, so dropping `same-filesystem` from
    the table -- it then fell through to the unknown-state branch -- and swapping the
    `off` and `failed` sentences both left the suite green. These states say different
    things because they MEAN different things, and a household acting on the wrong one
    is the failure this feature exists to prevent.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="ok",
        verified="true",
        last_verified_at=_hours_ago(2),
        offsite=state,
    )
    _key, _severity, title, body = _only(
        [c for c in _generate(household_id) if c[0].startswith("backup_offsite:")]
    )
    assert must_say in body, f"{state} is not being told in its own words: {body}"
    assert "does not know" not in title, f"{state} fell through to the unknown branch"


def test_a_record_that_is_valid_json_but_not_an_object_is_unreadable(
    auth_client: TestClient, state_dir: Path
) -> None:
    """`[]`, `"x"`, `3` and `null`: the file is there and cannot be read as a record.

    read_backup_status() mapped only EXCEPTIONS to the `unreadable` record and returned
    None for anything that parsed to a non-object -- so the feed said "Nothing has been
    backed up yet", as an emailed alert, about a box that may have months of backups
    behind it. To a reader, a parse error and a parsed non-object are the same fact.
    """
    household_id = _household_id(auth_client)
    for payload in ("[1, 2, 3]", '"hello"', "3", "null"):
        (state_dir / "backup-status.json").write_text(payload, encoding="utf-8")
        record = backups.read_backup_status()
        assert record is not None, f"{payload} was read as 'there has never been one'"
        assert record.get("phase") == "unreadable", payload

    key, severity, _title, body = _only(_generate(household_id))
    assert key == "backup_unreadable"
    assert severity == "warn"
    assert "no backup" not in body.lower()


def test_a_record_value_can_never_outgrow_the_column_it_lands_in(
    auth_client: TestClient, state_dir: Path
) -> None:
    """`key` and `title` are both String(200), and the record is hand-editable.

    The off-box state was pasted into the title at whatever length it came in at, so
    one long value would fail the INSERT and take every other alert in the same run
    down with it -- budgets, bills, the lot. SQLite will not enforce the width, so this
    asserts the lengths rather than relying on the write to fail.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="ok",
        verified="true",
        last_verified_at=_hours_ago(2),
        offsite="x" * 10_000,
    )

    key, _severity, title, _body = _only(_generate(household_id))

    assert len(key) <= 200, f"key is {len(key)} characters"
    assert len(title) <= 200, f"title is {len(title)} characters"
    assert len(_stored(household_id)) == 1


def test_every_backup_alert_offers_to_take_you_to_the_page(
    auth_client: TestClient, state_dir: Path
) -> None:
    """The link was unpinned: dropping it from the verify alert left the suite green.

    Each of these is about something the owner has to go and look at, and the feed row
    is the only thing offering to take them there.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="failed",
        step="verify",
        dump_name="d.sql.gz",
        verified="false",
        verify_message="row counts differ",
        last_verified_at=_hours_ago(99),
        offsite="same-host",
    )
    with SessionLocal() as db:
        household = db.get(models.Household, household_id)
        assert household is not None
        created = [n for n in notifications.generate(db, household) if n.type == "backup"]

    assert len(created) == 3, f"expected age, verify and off-box: {[n.key for n in created]}"
    assert [n.link for n in created] == ["/settings"] * 3


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", " false ", "no", "0", "off"])
def test_the_host_saying_no_in_any_of_its_spellings_is_a_failed_verification(
    auth_client: TestClient, state_dir: Path, raw: str
) -> None:
    """The host renders every field as a string, so `verified` arrives as a WORD."""
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="ok",
        dump_name="d.sql.gz",
        verified=raw,
        last_verified_at=_hours_ago(9),
        offsite="remote",
    )
    assert _only(_generate(household_id))[0].startswith("backup_verify_failed")


@pytest.mark.parametrize("raw", ["maybe", "", "  ", "null", "2", None])
def test_a_verified_value_this_version_cannot_read_is_neither_answer(
    auth_client: TestClient, state_dir: Path, raw: object
) -> None:
    """Not known must never render as either answer.

    A dump nobody has restored is not disproved, and a value this version cannot read
    is not a tick. The age ladder speaks for such a record instead, and here the age is
    fine, so nothing is said at all.
    """
    household_id = _household_id(auth_client)
    _write_status(
        state_dir,
        run_id="r1",
        phase="ok",
        dump_name="d.sql.gz",
        verified=raw,
        last_verified_at=_hours_ago(9),
        offsite="remote",
    )
    assert _generate(household_id) == []
