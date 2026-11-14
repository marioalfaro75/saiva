"""Backup helpers: ask the host to take one, and read back what it said happened.

The sibling of app/services/updates.py, deliberately so. The api process does not take
backups and cannot: it writes one file into a bind-mounted directory and reads another
back, and a root-owned systemd timer on the host does the work (scripts/backup.sh). The
reasoning is the same one that moved updates off the Watchtower sidecar, with one
addition that is specific to backups.

The addition is that the thing which produces the safety net must not be the thing a bad
release can break. A backup is most wanted on the night the api container will not start,
so a regime that lived inside that container would be absent exactly when it was needed.
Putting it on the host also makes the answer outlive the asking: the record is a file
root owns, so a container being replaced is a gap in the transport and not a loss of the
answer.

What this module may NOT do is assert anything it has not read. `verified` is the field
that matters here: scripts/backup.sh proves a dump by restoring it into a throwaway
database and comparing row counts both ways, because `psql --single-transaction -v
ON_ERROR_STOP=1` was measured to exit 0 on a dump truncated in the middle of a COPY
block. An unverified dump is not a backup, and the absence of the field is not evidence
of either answer -- so it is `bool | None` and None travels all the way to the page.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from . import updates

# The SECOND drop-box. A separate directory rather than a flag inside the request file,
# because the whole security argument in scripts/saiva-update-agent rests on root never
# reading a byte the container wrote: the DIRECTORY carries the one bit of information
# ("update" or "backup"), each has its own systemd path unit, and `find -type f` followed
# by `unlink` is still the only thing that ever looks inside either. A flag in the file
# would mean root parsing container-supplied content to decide which root script to run.
BACKUP_REQUESTS_DIRNAME = "backup-requests"

# There is deliberately no public counterpart to this file. The update status has one --
# Caddy serves status-public.json, because reading "failed at the gate, rolled back" from
# a browser while the api container is down is worth more than disclosing which release a
# box runs. Nothing about backups has that property: the age of a household's last backup
# is reconnaissance, and unlike "the update failed" it is never the thing you need while
# the app is down. See SAIVA_BACKUP_STATUS_FIELDS in scripts/lib/state.sh.
BACKUP_STATUS_FILENAME = "backup-status.json"

# How long a run may claim to be `running` before this process stops believing it. The
# button 409s while a backup is in flight, and without an expiry a status file left at
# `running` -- a host killed mid-dump, a systemd timeout, a power cut -- would 409 it
# forever with no way back except a shell on the box, which is the opposite of what this
# design is for.
#
# The number tracks the unit's own ceiling, the way updates.STALE_RUN_AFTER tracks the
# updater's 1800: saiva-backup.service (written by scripts/install-updater.sh) caps itself
# at TimeoutStartSec=3600, so an hour is the longest a run can legitimately last, and five
# minutes of slack covers the status file's final write. A backup is allowed to be this
# slow on purpose -- it dumps, restores the dump into a throwaway database, counts it both
# ways and copies it off the box. If that unit's timeout changes, this has to change with
# it; it is the only number in Python that has to agree with it.
STALE_RUN_AFTER = dt.timedelta(minutes=65)

# The phases scripts/backup.sh can finish in. Kept here so the API, the panel and the
# shell agree on the vocabulary, and so an unrecognised one is visible rather than
# silently rendered as something reassuring --
# test_every_outcome_the_host_can_report_is_one_the_panel_handles greps the phases out of
# the shell and holds all three sides together.
#
# `skipped` is the one that is not in the updater's vocabulary, and it is load-bearing. A
# nightly run that fires while the stack is deliberately down records `skipped` and leaves
# last_success_at / last_verified_at exactly as they were -- because "the backup failed"
# and "no backup was taken this time, and the newest verified one is N hours old" are
# different sentences, and reporting the first when the second is true is how a regime
# trains its owner to ignore it.
#
# A run that could not take the DEPLOY LOCK records nothing at all, which is a different
# case and deliberately not this one: the record belongs to the run that holds the lock,
# and every write here is a read-modify-write of one shared file. See
# acquire_lock_or_leave in scripts/backup.sh. The owner sees the run in flight still being
# reported, and the age climbing if it keeps happening.
TERMINAL_PHASES = frozenset({"ok", "failed", "skipped"})
# Only what scripts/lib/state.sh actually writes: backup_begin() sets `running` and
# nothing sets a queued phase. The window between the button being pressed and the timer
# firing is covered by a pending entry in the drop-box, which is why _pending_request()
# is checked first in backup_in_progress().
ACTIVE_PHASES = frozenset({"running"})


def backup_dir() -> Path | None:
    """The bind-mounted state directory shared with the host, if one is configured.

    Deliberately SAIVA_UPDATE_DIR and not a second variable of its own. The name is
    historical -- it predates there being anything in that directory but updates -- and
    the directory is one bind mount, declared once in docker-compose.prod.yml. A
    SAIVA_BACKUP_DIR here could disagree with that mount, and the symptom would not be an
    error: requests would be written where no path unit watches and the panel would read
    "nothing has been backed up yet" forever. Delegating to updates.update_dir() rather
    than reading the environment a second time means the two cannot drift at all.
    """
    return updates.update_dir()


def requests_dir() -> Path | None:
    """The backup drop-box: one of the two paths this container may write into.

    Everything else under the state directory is mounted read-only. Owning that directory
    would let this process make root open files of its choosing -- see
    scripts/saiva-update-agent for the whole argument, and
    test_the_app_can_write_only_the_drop_box_and_read_the_rest for the invariant.
    """
    directory = backup_dir()
    return directory / BACKUP_REQUESTS_DIRNAME if directory else None


def request_available() -> bool:
    """Whether pressing "Back up now" can actually do anything.

    False is a perfectly good answer and the panel says so plainly: a box whose host side
    has never been installed, or one installed before this drop-box existed, has no
    directory to write into. What it must never do is claim an ability it cannot verify.
    Note that this says nothing about whether the nightly TIMER is installed -- only the
    host can answer that, and `./scripts/backup.sh --self-check` is what asks it. A box
    can therefore have a working button and no nightly regime; the age of the newest
    verified backup is what makes that visible.
    """
    drop_box = requests_dir()
    return bool(drop_box and drop_box.is_dir() and os.access(drop_box, os.W_OK))


def request_backup(requested_by: str) -> str:
    """Drop a request for the host's backup script. Returns the request id.

    Nothing on the host ever opens this file. The path unit fires on the directory, the
    agent asks `find -type f` whether an entry exists and unlinks what it finds, so the
    contents are a note for whoever reads the host's log and nothing else. The most this
    container can ask for is "a backup", never "a backup of this, to there" -- a
    destination or a command taken from a request would be root running a string a
    network-facing container chose, which is why those two settings live in
    /etc/saiva/agent.conf and are ignored unless only root can write it.

    The name is unique per request, carried over from updates.request_update for the
    reason it was needed there: a fixed temp path meant two presses raced on one file, and
    the loser got a 503 whose message blamed the permissions of a plainly writable
    directory -- a false diagnosis is worse than no answer.
    """
    drop_box = requests_dir()
    if drop_box is None:
        raise RuntimeError("SAIVA_UPDATE_DIR is not set in this deployment")
    request_id = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}"
    # Written then renamed, so the host's path unit cannot fire on a half-written file.
    tmp = drop_box / f".{request_id}.tmp"
    tmp.write_text(f"requested_at={request_id} requested_by={requested_by}\n", encoding="utf-8")
    tmp.replace(drop_box / request_id)
    return request_id


def _unreadable_record() -> dict[str, Any]:
    """The record for "the file is there and cannot be read as one".

    A fresh dict each call: callers treat what they get back as theirs.
    """
    return {"phase": "unreadable", "message": "the backup record could not be read"}


def read_backup_status() -> dict[str, Any] | None:
    """The host's record of the last backup run, or None if there has never been one.

    None and "it failed" are different answers and the panel renders them differently, so
    a missing file is not flattened into either.
    """
    directory = backup_dir()
    if directory is None:
        return None
    try:
        with (directory / BACKUP_STATUS_FILENAME).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - a malformed record must not break the settings page
        return _unreadable_record()
    if isinstance(payload, dict):
        return payload
    # Valid JSON that is not an object: `[]`, `"x"`, `3`, `null`. The file IS there and
    # cannot be read as a record, which is the `unreadable` answer and emphatically not
    # "there has never been a backup". Returning None here told the owner "Nothing has
    # been backed up yet" about a box that may have months of backups behind it, and the
    # alerts feed escalates that same sentence to an `alert`. To anyone reading, a parse
    # error and a parsed non-object are the same fact: nothing here can read the record.
    return _unreadable_record()


def _pending_request() -> bool:
    drop_box = requests_dir()
    if drop_box is None:
        return False
    try:
        return any(entry.is_file() for entry in drop_box.iterdir())
    except OSError:
        # 0730 on the drop-box means this container may create and unlink but not list.
        # That is deliberate, and it means "is one pending" is a question only the host
        # can answer; the status file answers it a moment later.
        return False


def backup_in_progress(status: dict[str, Any] | None = None) -> bool:
    """True while a request is queued or a run is genuinely under way.

    `status` lets a caller that has already read the record hand it over rather than
    making this open the file a second time. The alerts feed reads the record exactly
    once per run, on purpose -- every household in one run must be told the same thing
    about the box -- and that invariant has a test.

    Deliberately not "while the record says running", for the reason STALE_RUN_AFTER
    exists. Note which way the unknowns fall: a `running` with no parseable start time is
    treated as live, because refusing a second backup for an hour is a smaller harm than
    two dumps and two throwaway restores against one database at once. scripts/backup.sh
    holds the deploy lock as well, so a race here is refused rather than damaging -- this
    is about not showing the owner a button that will 409.
    """
    if _pending_request():
        return True
    if status is None:
        status = read_backup_status() or {}
    if status.get("phase") not in ACTIVE_PHASES:
        return False
    started = status.get("started_at")
    if not isinstance(started, str):
        return True
    try:
        began = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return True
    return dt.datetime.now(dt.UTC) - began < STALE_RUN_AFTER
