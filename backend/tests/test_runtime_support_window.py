"""When each runtime this repository pins stops getting security fixes.

The reason this exists, concretely: on 2026-11-01 the API image is built `FROM
python:3.11-slim-bookworm`, and Debian 12 "bookworm" came off regular security
support about four months ago. Fixes still arrive, from the Debian LTS team, until
2028-06-30 — but a narrower team covering a narrower set, and nothing in this
repository said a word about the handover when it happened. It was noticed by
someone reading a Dockerfile, which is not a control.

A dated assertion is an odd thing to find in a test suite, so here is the case for
this one. Nothing else in this repository will ever volunteer "bookworm is going
out of support":

  * The weekly `images` job in .github/workflows/security-scan.yml files its SARIF
    with `ignore-unfixed: true`. That is right for routine base-image noise and
    exactly backwards at end of life. Trivy models a dropped package as a status,
    not as an absence: trivy-db maps the Debian tracker's `end-of-life` state to
    `StatusEndOfLife`, and `--ignore-unfixed` expands to "ignore every status
    except Fixed", so a package Debian LTS has stopped covering is filtered out of
    the Security tab entirely. The job reports *less* as the image stops being
    maintained. Trivy does raise an EOSL warning of its own, but only from the date
    hard-coded in its own table (`eolDates` in pkg/detector/ospkg/debian puts
    Debian 12 at 2028-06-10, i.e. the LTS date, twenty days before Debian's own),
    only in the table output, and never in SARIF — so it lands in a job log nobody
    is required to read, and not until 2028.
  * .github/dependabot.yml silences python and node base-image bumps. That is the
    right call — a tag bump on its own ships a runtime CI never tested — but it
    removed the only thing in the repo that ever mentioned a newer runtime, and its
    own comment said as much until this file replaced that sentence.
  * refresh-images.yml rebuilds the current release every Tuesday, so a patch that
    upstream *does* publish now reaches the image without anyone cutting a version.
    That is the strongest reason for a calendar rather than the weakest: the refresh
    keeps succeeding, weekly and green, after upstream stops publishing anything to
    pick up. "The rebuild worked" and "the image is patched" stop being the same
    sentence at end of life, and nothing in that workflow can tell.

That leaves a calendar as the cheapest honest alarm, and a calendar has to live
somewhere that runs on every pull request. It lives in its own file rather than in
test_supply_chain.py on purpose: every assertion in that file is a function of the
files in the commit, so it cannot go red without someone changing something. These
can, which is the whole point — and when one does, it should be obvious from the
filename that the suite's one time-dependent check is what tripped, rather than
leaving someone to wonder what their pull request did to the supply chain.

One interaction is worth knowing before this goes red rather than after.
refresh-images.yml gates on checks.yml, which runs this suite, so from the day a
window opens the Tuesday refresh fails too and stops delivering the patches that
*are* still being published. That is the right outcome once support has actually
ended — rebuilding an abandoned base image weekly and calling it a patch is the
blind spot itself — and it is a real cost during the lead time. It is also why the
windows below are measured in months and not years: the way out is to do the
upgrade, and the lead time is sized so that there is time to.

The versions are read out of the Dockerfiles and the compose file, never restated
here, so this cannot stay green while describing a runtime the repo stopped using.
The dates are restated, because there is nowhere in the repo to read them from;
each carries its source and the day it was looked up.

Two things this file deliberately does not cover, stated as gaps rather than
argued away:

  * Alpine, which is not a gap for `web` and is one for `db` and `caddy`.
    `node:22-alpine`, `caddy:2-alpine` and `postgres:16-alpine` name no Alpine
    release, so there is no version in this repo to assert on — the publishers move
    those tags onto a newer Alpine themselves (as of 2026-11-01 node:22-alpine and
    postgres:16-alpine are built on Alpine 3.24 and caddy:2-alpine on 3.23, per
    docker-library/official-images and caddyserver/caddy-docker, supported to
    2028-06-01 and 2027-11-01). For saiva-web that is genuinely enough: its
    Dockerfile rebuilds `FROM caddy:2-alpine` every Tuesday and the container is
    labelled for the updater, so a newer Alpine arrives on its own. For the `db`
    and `caddy` services it is not enough, because neither carries
    `com.centurylinklabs.watchtower.enable` and neither sets `pull_policy`, so
    nothing moves those images until a person runs `docker compose pull`. There is
    no version in the repo for a test to read and no date to assert, so the honest
    statement is that those two images age until someone pulls them, and that this
    file does not watch them.
  * Caddy 2, `tecnativa/docker-socket-proxy:0.3.0` and `nickfedor/watchtower:1`,
    none of which publishes a support calendar to assert against. Dependabot covers
    all three (see the compose-file entry in .github/dependabot.yml).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BACKEND_DOCKERFILE = REPO / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = REPO / "frontend" / "Dockerfile"
COMPOSE_PROD = REPO / "docker-compose.prod.yml"

# Debian names its releases twice. The Dockerfile tag carries the codename; every
# support calendar is published against the number. An unknown codename falls
# through to END_OF_SECURITY_FIXES as itself and fails there, which is the correct
# outcome: someone moved the base image to a release nobody has dated yet.
DEBIAN_RELEASES = {
    "bullseye": "11",
    "bookworm": "12",
    "trixie": "13",
    "forky": "14",
}

# The day upstream stops publishing security fixes for that line — not the day bug
# fixes stopped, which for most of these is years earlier and is not what matters
# to a box on the internet. Successor versions are listed as well as the ones in
# use, so that doing the upgrade this file asks for does not immediately trip the
# "no recorded end of support" test below.
#
# All looked up 2026-11-01.
#
# Debian: the "End of LTS" column of the release table on debian.org/releases,
# which is the column endoflife.date/debian scrapes into its `eol` field
# (confirmed from that product file's own field map). For bookworm the LTS number
# is the one that matters, because *regular* support has already ended — and the
# exact day it ended is genuinely not agreed, so this comment does not pick one.
# Debian's handover announcement (debian.org/News/2026/20260712) is dated
# 2026-07-12 and says regular support ended that day, "three years after the
# initial release"; debian.org's own release table carries 2026-07-11, the date of
# the final point release 12.15; the schedule published back in the 2024 bullseye
# handover said 2026-06-10, and Freexian's Debian 12 page says LTS took over
# 2026-06-11. Four to five months either way, so nothing here turns on it. What
# the handover does change is narrower than it sounds and worse than it sounds:
# fixes continue to 2028-06-30, but from the LTS team, for amd64/i386/arm64/armhf/
# ppc64el only, and excluding section `games` plus whatever the
# debian-security-support package lists. Packages in that excluded set are the
# ones Trivy marks `StatusEndOfLife` and `ignore-unfixed: true` discards, which is
# why the `images` job in security-scan.yml now prints an unfixed count as well.
# None of this is a reason to move the date below: fixes really do run to
# 2028-06-30.
#
# Python: the `eol` field of endoflife.date/python, which is itself the month-end
# reading of each version's release-schedule PEP — PEP 664 says only "approximately
# October 2027" for 3.11, with no day. Realised days have landed earlier than that
# placeholder: 3.9 ended 2025-10-31 but 3.10's final release was 2026-10-01, thirty
# days before its own month-end entry. So read 3.11's date as "sometime in October
# 2027". The recorded date is the published one rather than a guess at the realised
# day, because a table that disagrees with its own cited source gets "corrected"
# back by the next person who checks it; the 180-day lead time is what absorbs the
# difference. Concretely: the window opens 2027-05-04, so if 3.11 actually ends
# 2027-10-01 like 3.10 did, the effective notice is 150 days rather than 180 —
# still comfortably over the floor below — and the only cost is that the deadline
# this file prints is up to a month later than the real one, by which time the
# alarm has been red for five months anyway.
#
# Node: the `end` field of nodejs/Release's own schedule.json.
#
# PostgreSQL: the Final Release column of postgresql.org/support/versioning, via
# endoflife.date/postgresql.
END_OF_SECURITY_FIXES: dict[tuple[str, str], dt.date] = {
    ("debian", "11"): dt.date(2026, 8, 31),
    ("debian", "12"): dt.date(2028, 6, 30),
    ("debian", "13"): dt.date(2030, 6, 30),
    ("python", "3.11"): dt.date(2027, 10, 31),
    ("python", "3.12"): dt.date(2028, 10, 31),
    ("python", "3.13"): dt.date(2029, 10, 31),
    ("python", "3.14"): dt.date(2030, 10, 31),
    ("python", "3.15"): dt.date(2031, 10, 31),
    ("node", "22"): dt.date(2027, 4, 30),
    ("node", "24"): dt.date(2028, 4, 30),
    ("node", "26"): dt.date(2029, 4, 30),
    ("postgres", "16"): dt.date(2028, 11, 9),
    ("postgres", "17"): dt.date(2029, 11, 8),
    ("postgres", "18"): dt.date(2030, 11, 14),
}

# How long before that date this check goes red. Not one number, because the four
# upgrades are not the same size of job, and a lead time that is wrong in either
# direction is worse than useless: too short and the alarm arrives after it is
# useful, too long and the suite spends years red and gets widened by whoever is
# trying to merge something unrelated.
#
# The floor under all of them is the release cadence. refresh-images.yml does not
# help here: it rebuilds the tree of the highest released tag, so a runtime bump
# merged to main reaches the box only when someone runs cut-release.yml — and the
# last two releases were 57 days apart. So every number below has to cover notice +
# the change + review + at least one release window, for a project touched at
# weekends. test_the_lead_times_are_long_enough_to_be_worth_having pins that floor,
# because narrowing these numbers is the one edit that silences the alarm without
# looking like it.
LEAD_TIME_DAYS = {
    # 180: a Python minor is the widest of these changes — the tag in
    # backend/Dockerfile, two `python-version` keys in checks.yml, three settings
    # in pyproject.toml, and a full regeneration of the hash-pinned
    # requirements.lock, any of whose ~30 pins may not have a wheel for the new
    # interpreter yet. Six months is also comfortably inside one Python line's
    # five-year life, so this never overlaps the previous version's window, and it
    # absorbs the month of slack in the "approximately October" dates above.
    "python": 180,
    # 180: same number for the same reason — the codename is half of the same tag,
    # so in practice these two move together. It also moves the PGDG apt repo,
    # since the install step derives the repo codename from ${VERSION_CODENAME}.
    "debian": 180,
    # 90: node only ever runs in a build stage that is thrown away —
    # frontend/Dockerfile's final image is caddy:2-alpine serving static files, so
    # nothing node ships reaches the box. The exposure is a build host running an
    # unpatched node over npm, which is real but is not a family's records, and
    # the change itself is two lines plus `npm ci`. This is the shortest window
    # here and it is the one that opens first: node 22 goes red 2027-01-30.
    "node": 90,
    # 270: the only one of these that is not a tag bump. The db_data volume holds
    # the household's actual records in the old major's on-disk format, so a
    # Postgres major needs a planned dump/restore with a tested rollback, on a box
    # whose owner is not on call. Nine months buys the chance to do that once,
    # carefully, instead of in the week the fixes stop.
    "postgres": 270,
}

# One observed interval between releases: v0.15.0 (2026-09-05) to v0.16.0
# (2026-11-01). A runtime bump merged to main does not reach the box until someone
# runs cut-release.yml, so this is the wait that any lead time has to cover before
# it covers anything else.
RELEASE_INTERVAL_DAYS = 57

# The floor a lead time has to clear to be worth having: one full release interval
# for the cut, plus a month for writing the change and getting it reviewed. Below
# this the alarm fires with less time than shipping the fix takes, which is the
# same as not having it.
MIN_USEFUL_LEAD_DAYS = RELEASE_INTERVAL_DAYS + 30

# What the failure tells you to do. Kept beside the lead times because the two
# numbers only make sense together: the window is long because the work is this.
UPGRADE_RECIPE = {
    "python": (
        "Move the tag in backend/Dockerfile, both `python-version` keys in "
        ".github/workflows/checks.yml, and `requires-python` + "
        "[tool.ruff] target-version + [tool.mypy] python_version in "
        "backend/pyproject.toml, then regenerate backend/requirements.lock with "
        "hashes. The Dockerfile tag alone ships a runtime nothing tested, which is "
        "exactly why .github/dependabot.yml does not offer this as a pull request."
    ),
    "debian": (
        "The codename is the second half of the `FROM python:` tag in "
        "backend/Dockerfile. Moving it also moves the PostgreSQL apt repo, because "
        "the install step derives the repo codename from ${VERSION_CODENAME} — "
        "confirm apt.postgresql.org publishes postgresql-client-16 for the new "
        "codename before you move it, or the image stops building."
    ),
    "node": (
        "Move the tag in frontend/Dockerfile and `node-version` in "
        ".github/workflows/checks.yml together, then `npm ci` to confirm the "
        "lockfile still resolves on the new major."
    ),
    "postgres": (
        "Not a tag bump: the db_data volume is in the old major's on-disk format "
        "and the new server will refuse to start on it. Plan a pg_dump/restore "
        "with a tested rollback, then move postgres:NN-alpine in "
        "docker-compose.prod.yml and docker-compose.yml, the `migrations` service "
        "image in .github/workflows/checks.yml, and postgresql-client-NN in "
        "backend/Dockerfile in one change."
    ),
}

# Where each runtime's version is read from, quoted back in the failure so that
# "it was not found" names the line that should have held it.
PINNED_IN = {
    "python": "the `FROM python:<x.y>-slim-<codename>` line in backend/Dockerfile",
    "debian": "the codename on that same `FROM python:` line in backend/Dockerfile",
    "node": "the `FROM node:<major>-...` line in frontend/Dockerfile",
    "postgres": "the `db` service's `postgres:<major>-...` image in docker-compose.prod.yml",
}

# Each of these keeps only as much of the version as the support calendars are
# published against — the line, not the point release — but tolerates a tag that
# names more than that. `python:3.11.18-slim-bookworm` and `postgres:16.15-alpine`
# are both legitimate ways to write what is pinned here today, and a parser that
# only matched the shorter spelling would answer a harmless tag with "no python
# version could be read out of the repository", which is the worst kind of false
# alarm: the one that teaches people the alarm is wrong.
#
# Both FROM patterns allow flags between FROM and the image, because
# frontend/Dockerfile already writes `FROM --platform=$BUILDPLATFORM node:22-alpine`
# and cut-release.yml builds linux/arm64, so the same flag arriving on the backend
# stage is an ordinary edit and must not read as "python disappeared".
#
# POSTGRES_IMAGE allows the major to end the tag, because `postgres:18` is as
# legitimate a way to write the pin as `postgres:16-alpine`.
FROM_PYTHON = re.compile(
    r"^FROM\s+(?:--\S+\s+)*python:(?P<python>\d+\.\d+)(?:\.\d+)?-slim-(?P<debian>[a-z]+)\b",
    re.MULTILINE,
)
FROM_NODE = re.compile(
    r"^FROM\s+(?:--\S+\s+)*node:(?P<node>\d+)(?:\.\d+)*(?:[-:\s]|$)",
    re.MULTILINE,
)
POSTGRES_IMAGE = re.compile(r"^postgres:(?P<postgres>\d+)(?:\.\d+)*(?:[-:]|$)")


class Pin(NamedTuple):
    """One runtime version as the repository actually states it today."""

    runtime: str
    version: str
    where: str
    line: str


def _at(text: str, offset: int, path: Path) -> tuple[str, str]:
    """Where a match was found, and the whole line it was found on.

    The whole line and not the matched span: the patterns stop at the separator
    after the version, so quoting the match would print `node:22-` into a failure
    that is trying to show someone the line they have to edit.
    """
    before = text[:offset]
    end = text.find(chr(10), offset)
    return (
        f"{path.relative_to(REPO)}:{before.count(chr(10)) + 1}",
        text[offset : end if end != -1 else len(text)].strip(),
    )


def _read(path: Path) -> str:
    """The file's text, or empty if it has moved.

    Deliberately not letting FileNotFoundError out: this runs at import time, so a
    renamed Dockerfile would turn into a collection error that takes the whole
    module down — including the alarm below, which is the one thing here that must
    still be able to speak. Empty text means no pins, which
    test_every_watched_runtime_was_actually_found reports as a sentence naming the
    file instead of a traceback.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _pins() -> list[Pin]:
    """Every runtime version this file watches, read from the files that set it."""
    pins: list[Pin] = []

    backend = _read(BACKEND_DOCKERFILE)
    for found in FROM_PYTHON.finditer(backend):
        where, line = _at(backend, found.start(), BACKEND_DOCKERFILE)
        pins.append(Pin("python", found["python"], where, line))
        codename = found["debian"]
        pins.append(Pin("debian", DEBIAN_RELEASES.get(codename, codename), where, line))

    frontend = _read(FRONTEND_DOCKERFILE)
    for found in FROM_NODE.finditer(frontend):
        where, line = _at(frontend, found.start(), FRONTEND_DOCKERFILE)
        pins.append(Pin("node", found["node"], where, line))

    compose = yaml.safe_load(_read(COMPOSE_PROD) or "{}") or {}
    for name, service in (compose.get("services") or {}).items():
        image = str(service.get("image") or "")
        image_match = POSTGRES_IMAGE.match(image)
        if image_match:
            pins.append(
                Pin("postgres", image_match["postgres"], f"docker-compose.prod.yml ({name})", image)
            )

    return pins


PINS = _pins()
WATCHED = tuple(PINNED_IN)


def test_every_watched_runtime_was_actually_found() -> None:
    """Guards the parametrised tests below, which are empty and green without this.

    The ways this goes quiet are all plausible: the base image moves to
    `python:3.11-slim` (which now tracks trixie, so the codename stops being
    stated at all), the frontend build stage is replaced, or the compose service is
    renamed or its file moved. Each would silently remove a runtime from the
    calendar while leaving the suite green, which is the one failure a calendar
    cannot afford.
    """
    found = {pin.runtime for pin in PINS}
    missing_files = [
        str(path.relative_to(REPO))
        for path in (BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE, COMPOSE_PROD)
        if not path.is_file()
    ]
    for runtime in WATCHED:
        assert runtime in found, (
            f"no {runtime} version could be read out of the repository, so nothing "
            f"below is checking {runtime}'s support window any more. It should have "
            f"come from {PINNED_IN[runtime]}. "
            + (f"These watched files are missing: {missing_files}. " if missing_files else "")
            + "Either the pin moved — fix the parser in this file — or the runtime "
            "genuinely went away, in which case delete its entries here rather than "
            "leaving an assertion that passes on an absence."
        )


def test_a_more_precisely_pinned_tag_still_reads_as_the_same_line() -> None:
    """Writing out the point release must not read as "the runtime disappeared".

    The test above fails loudly when a version cannot be found, which is right for
    a pin that genuinely moved and wrong for `python:3.11.18-slim-bookworm` — the
    same runtime, spelled more exactly. Support calendars are published per line
    (Python 3.11, Node 22, PostgreSQL 16), so the extra precision is noise to this
    file and has to be dropped rather than tripped over. The `--platform` cases are
    here because frontend/Dockerfile already carries that flag and the backend
    stage could acquire it the day anyone debugs an arm64 build.
    """
    cases = [
        (FROM_PYTHON, "FROM python:3.11.18-slim-bookworm AS base", {"python": "3.11"}),
        (FROM_PYTHON, "FROM python:3.11-slim-bookworm AS base", {"python": "3.11"}),
        (
            FROM_PYTHON,
            "FROM --platform=$TARGETPLATFORM python:3.11-slim-bookworm AS base",
            {"python": "3.11", "debian": "bookworm"},
        ),
        (FROM_NODE, "FROM --platform=$BUILDPLATFORM node:22.23.4-alpine AS build", {"node": "22"}),
        (FROM_NODE, "FROM node:22-alpine", {"node": "22"}),
        (FROM_NODE, "FROM node:24", {"node": "24"}),
        (POSTGRES_IMAGE, "postgres:16.15-alpine", {"postgres": "16"}),
        (POSTGRES_IMAGE, "postgres:16-alpine", {"postgres": "16"}),
        (POSTGRES_IMAGE, "postgres:18", {"postgres": "18"}),
    ]
    for pattern, line, expected in cases:
        found = pattern.search(line)
        assert found is not None, f"{pattern.pattern!r} no longer reads {line!r}"
        for group, value in expected.items():
            assert found[group] == value, (
                f"{line!r} read as {group}={found[group]!r}, not {value!r}"
            )


@pytest.mark.parametrize("pin", PINS, ids=lambda pin: f"{pin.runtime}{pin.version}")
def test_every_pinned_runtime_has_a_recorded_end_of_support(pin: Pin) -> None:
    """The half that cannot drift: a new pin with no date fails rather than passes.

    Bumping a runtime is the moment its calendar changes, and it is also the moment
    nobody is thinking about calendars. This fails the bump itself, so the date is
    looked up by the person who has the context, not by whoever is next surprised.
    """
    key = (pin.runtime, pin.version)
    assert key in END_OF_SECURITY_FIXES, (
        f"{pin.where} pins {pin.runtime} {pin.version} and nothing in this file "
        f"records when its security fixes stop:\n    {pin.line}\n"
        f"Add it to END_OF_SECURITY_FIXES with the date and where the date came "
        f"from. For debian, check DEBIAN_RELEASES knows the codename too — an "
        f"unmapped codename arrives here as itself."
    )
    assert pin.runtime in LEAD_TIME_DAYS, f"no lead time recorded for {pin.runtime}"


def test_the_lead_times_are_long_enough_to_be_worth_having() -> None:
    """The alarm can be silenced by shrinking a lead time, so the lead times are pinned too.

    Everything else here proves that the window opens `lead` days before the
    deadline. None of it says anything about whether `lead` is a useful number, and
    setting all four to zero would leave this suite green while removing every
    month of notice — the one edit that turns the calendar off without looking like
    it, and the edit most tempting to whoever is trying to merge something
    unrelated on the day it first goes red.

    Two floors, both derived rather than chosen:

      * A lead time shorter than one release interval plus a month cannot deliver
        the fix, because a runtime bump merged to main sits on main until someone
        runs cut-release.yml.
      * The window for one version must be shorter than the gap to the next
        version's deadline, or doing the upgrade would land straight inside the
        successor's window and the red could not be cleared by doing the work. That
        is the failure mode that would make someone delete this file.
    """
    for runtime, lead in LEAD_TIME_DAYS.items():
        assert lead >= MIN_USEFUL_LEAD_DAYS, (
            f"the {runtime} lead time is {lead} days, under the {MIN_USEFUL_LEAD_DAYS}-day "
            f"floor ({RELEASE_INTERVAL_DAYS} days for one observed release interval plus 30 "
            "to write and review the change). A window this short reports the problem "
            "after the last moment it could have been fixed in time, which is worse than "
            "silence because it also blocks the Tuesday refresh. Raise it, or if the "
            "release cadence genuinely changed, move RELEASE_INTERVAL_DAYS and say where "
            "the new figure came from."
        )

    by_runtime: dict[str, list[dt.date]] = {}
    for (runtime, _version), deadline in END_OF_SECURITY_FIXES.items():
        by_runtime.setdefault(runtime, []).append(deadline)

    for runtime, deadlines in by_runtime.items():
        lead = LEAD_TIME_DAYS[runtime]
        ordered = sorted(deadlines)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            gap = (later - earlier).days
            assert lead < gap, (
                f"the {runtime} lead time is {lead} days but only {gap} days separate "
                f"{earlier.isoformat()} from {later.isoformat()}, so upgrading off the "
                f"version that ends {earlier.isoformat()} would land inside the next "
                "version's window and leave this check red. Either the lead time is too "
                "long for this runtime's cadence, or a date is wrong."
            )


def _countdown(days: int) -> str:
    """Render a day count as English, because "-1 days from today" is arithmetic.

    This is read by someone who did not expect the failure, in the middle of a
    pull request about something else. A negative day count there reads as a bug in
    the test rather than as the fact that support ended last year.
    """
    if days < 0:
        return f"{-days} day{'' if days == -1 else 's'} past the end"
    return f"{days} day{'' if days == 1 else 's'} left"


def _verdict(pin: Pin, today: dt.date) -> str | None:
    """The message to fail with, or None while there is still lead time left.

    A pure function of (pin, today) rather than an inline assertion, so that the
    test below can run it against dates this suite will not otherwise reach for
    years. A dated check nobody has watched fire is indistinguishable from a
    comment, and this one is the only alarm there is.
    """
    deadline = END_OF_SECURITY_FIXES.get((pin.runtime, pin.version))
    if deadline is None:
        # An undated pin is already a failure, reported by
        # test_every_pinned_runtime_has_a_recorded_end_of_support with the date to
        # look up and where to put it. Raising KeyError here would add a second,
        # bare traceback for the same cause and bury that message.
        return None

    lead = LEAD_TIME_DAYS[pin.runtime]
    remaining = (deadline - today).days
    if remaining > lead:
        return None

    standing = "\n".join(
        f"      {other.runtime} {other.version}: "
        + (
            _countdown((due - today).days)
            + f", against a {LEAD_TIME_DAYS[other.runtime]}-day window"
            if (due := END_OF_SECURITY_FIXES.get((other.runtime, other.version))) is not None
            else "no recorded end of support"
        )
        for other in PINS
    )
    return (
        f"{pin.where} pins {pin.runtime} {pin.version}, whose security fixes "
        f"{'stopped' if remaining < 0 else 'stop'} on {deadline.isoformat()} — "
        f"{_countdown(remaining)} as of {today.isoformat()}, inside the {lead}-day "
        f"window this check keeps clear for the upgrade.\n"
        f"    {pin.line}\n\n"
        "    Nothing in this pull request caused this. The assertion is a calendar, "
        "today is the day it came due, and it stays red until the runtime moves or "
        "the date is corrected.\n\n"
        f"    What to do: {UPGRADE_RECIPE[pin.runtime]}\n\n"
        "    Nothing else here will raise it again. The weekly image scan files its "
        "findings with `ignore-unfixed: true`, so it reports less rather than more as "
        "fixes stop being published — its readable table prints the unfixed count, but "
        "in a job log nobody is required to read; .github/dependabot.yml silences this "
        "base image on purpose; and the Tuesday refresh in refresh-images.yml goes on "
        "rebuilding and republishing the same layers, green, with nothing new to pick "
        "up. Note that while this is red that refresh cannot publish at all — it gates "
        "on checks.yml, which runs this suite — so the patches still being issued today "
        "stop arriving too. That is the clock, not a reason to wait.\n\n"
        f"    Where every pinned runtime stands on {today.isoformat()}:\n{standing}\n\n"
        "    If the date itself is wrong, correct END_OF_SECURITY_FIXES in this file "
        "and record where the new date came from. Narrowing LEAD_TIME_DAYS is the one "
        "response that leaves the box in exactly the state the red was about, which is "
        "why there is a floor under it a few tests up."
    )


@pytest.mark.parametrize("pin", PINS, ids=lambda pin: f"{pin.runtime}{pin.version}")
def test_no_pinned_runtime_is_inside_its_upgrade_window(pin: Pin) -> None:
    """The alarm itself. Read the assertion message, not this docstring.

    `date.today()` is the host's local day, so a laptop in another timezone can
    disagree with CI by one. At ninety days of lead time and up, that does not
    change an outcome.
    """
    verdict = _verdict(pin, dt.date.today())
    assert verdict is None, verdict


def test_the_window_actually_opens_on_the_day_it_should() -> None:
    """Proof that the check above fires, run against a clock it will not see for years.

    Iterating END_OF_SECURITY_FIXES rather than PINS on purpose: this proves the
    predicate for every runtime and version the table knows, so it keeps proving
    something after a pin moves, and it cannot be quietly emptied by a parser that
    stopped matching.

    The day-before case is the half that makes this non-vacuous. A `_verdict` that
    had been changed to return a message unconditionally would satisfy every
    assertion about firing, and would then fail the suite on every pull request
    for the rest of the project's life.
    """
    assert len(END_OF_SECURITY_FIXES) >= 12, "the support table has been gutted"

    for (runtime, version), deadline in END_OF_SECURITY_FIXES.items():
        lead = LEAD_TIME_DAYS[runtime]
        pin = Pin(runtime, version, "backend/Dockerfile:3", f"FROM {runtime}:{version}")

        quiet = _verdict(pin, deadline - dt.timedelta(days=lead + 1))
        assert quiet is None, (
            f"{runtime} {version} is reported {lead + 1} days before its deadline, "
            f"one day outside its own {lead}-day window:\n{quiet}"
        )

        for today, when in (
            (deadline - dt.timedelta(days=lead), "the day the window opens"),
            (deadline, "the last day fixes are published"),
            (deadline + dt.timedelta(days=1), "the day after support ends"),
        ):
            verdict = _verdict(pin, today)
            assert verdict is not None, (
                f"{runtime} {version} is still reported as fine on {today} ({when}), "
                f"so this check would not have fired at all"
            )
            # The message has to carry enough to act on without opening this file:
            # which runtime, which version, where it is pinned, when it runs out,
            # and what the upgrade involves.
            for needed in (
                runtime,
                version,
                pin.where,
                deadline.isoformat(),
                UPGRADE_RECIPE[runtime],
            ):
                assert needed in verdict, (
                    f"the failure for {runtime} {version} on {today} does not mention "
                    f"{needed!r}, so whoever hits it has to come and read this file:\n"
                    f"{verdict}"
                )
