"""Alerts (PRD R35) and email digests (R34).

Alerts are derived from existing data — budgets, the insight feed, upcoming bills,
large transactions and the cashflow forecast — and stored idempotently keyed by a
stable `key`, so regenerating never duplicates them. The cron entrypoint also
emails new alerts and sends due digests. All email is opt-in and quiet by default.

One source is not household data at all: the state of the backup regime on this box,
read from the record scripts/backup.sh leaves on the host. It is here because a failure
signal whose delivery mechanism is "the owner opens the Settings page sometime" is
weaker than one that arrives, and the age of the newest VERIFIED backup is the number
this deployment most needs to hear about while it is still fixable. See the block above
_backup_candidates for why its keys are shaped the way they are; dedup by key is what
makes "tell me when it breaks, not every night while it stays broken" expressible.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import backups
from . import forecast as forecast_service
from . import recurring as recurring_service
from .budgets import list_budgets
from .insights import generate as generate_insights
from .mailer import send_email

LARGE_TXN_LOOKBACK_DAYS = 7
BILL_LOOKAHEAD_DAYS = 7
LOW_BALANCE_HORIZON_DAYS = 30
INSIGHT_ALERT_TYPES = {"mover", "creep", "fee", "duplicate"}


@dataclass
class Candidate:
    key: str
    type: str
    severity: str
    title: str
    body: str
    link: str | None = None
    amount_cents: int | None = None


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def settings_for(db: Session, household_id: str) -> models.NotificationSettings:
    ns = db.get(models.NotificationSettings, household_id)
    if ns is None:
        ns = models.NotificationSettings(household_id=household_id)
        db.add(ns)
        db.commit()
        db.refresh(ns)
    return ns


def _recipients(db: Session, household_id: str) -> list[str]:
    return list(
        db.execute(
            select(models.User.email).where(models.User.household_id == household_id)
        ).scalars()
    )


# --------------------------------------------------------------------------------------
# The backup regime
#
# The age of the newest VERIFIED backup was, until this block existed, a number on the
# Settings page and nowhere else -- a failure signal whose delivery mechanism was "look
# at a page sometime". These candidates put it where the household already looks: the
# in-app feed, and (at `alert` and `warn`, which is what _pending_email selects) the
# email that goes with it.
#
# WHY THE KEYS ARE SHAPED LIKE THIS, which is the whole design. `generate()` skips any
# candidate whose key already exists for the household, for all time -- dedup is by key
# across history, not per period. So the key encodes the STATE and never the date the
# run happened:
#
#   * a box that is stale for three weeks alerts ONCE. `backup_stale:<last_verified_at>`
#     is keyed on the timestamp it is stale ABOUT, and that value is exactly what does
#     not change while the nightly run is broken, so nights two to twenty-one are the
#     same key and are silently dropped.
#   * a good backup, then a second stale period, alerts AGAIN. The good run moved
#     `last_verified_at`, so the second stale period is a different key, and the second
#     failure is as loud as the first.
#   * `backup_verify_failed:<run_id>` says it once per run that produced an unrestorable
#     dump, so a regime that fails every night does shout every night -- which is right,
#     because each night is a fresh dump that was taken and found broken.
#   * a key with today's date in it would nag daily while nothing changed, and a key with
#     nothing variable in it would report the SECOND occurrence of a recurring state
#     never. Where there is genuinely nothing in the record to key on, the bare key is
#     used deliberately and the trade-off is noted at the branch.
#
# WHY EVERY HOUSEHOLD ON A MULTI-HOUSEHOLD BOX SEES THE SAME ALERT. The backup is a
# property of the BOX, not of a household: one database, one dump, one record. Two
# households are two sets of rows inside the database that either has or has not been
# backed up, so "there is no verified backup" is true for both and belongs in both
# feeds -- their owners may be different people, and whoever reads it is whoever can
# act. The record is therefore read ONCE per run and handed down (run_all ->
# run_for_household -> generate -> _candidates), because read_backup_status() touches
# the filesystem and the answer cannot differ between households in one run.
# --------------------------------------------------------------------------------------

# Where the record is rendered for a human. Owner-only, like the endpoint behind it.
BACKUP_LINK = "/settings"
# The one off-box state that means the copy left the machine, from SAIVA_OFFSITE_STATES
# in scripts/backup.sh. Not a boolean and not a longer allowlist: `stat -c %d` reports a
# btrfs subvolume, a ZFS dataset, an LVM thin volume and a Proxmox bind-mount as
# different devices while they share one disk, so "a different filesystem" is not "off
# this box". Mirrors offsiteIsOffBox() in frontend/src/components/BackupsPanel.tsx.
BACKUP_OFF_BOX = "remote"


@dataclass(frozen=True)
class BackupFacts:
    """What this box knows about its own backups, read once per run.

    `drop_box` is backups.request_available(): the backup drop-box exists and this
    container can write to it. It is the ONLY evidence the app has that the host side
    was installed at all, and it is deliberately not read as more than that -- it says
    nothing about whether the nightly timer exists, which only the host can answer.
    """

    record: dict[str, Any] | None
    drop_box: bool
    stale_after_hours: int
    now: dt.datetime


def backup_facts() -> BackupFacts:
    """The host's backup record, plus the three local facts needed to read it.

    The only thing in this block that touches the filesystem or the clock, which is why
    it is called once per run and its result handed down rather than re-read per
    household -- and why every household in one run is told the same thing about the
    box, instead of a run that straddles a nightly backup telling the first household
    the box is stale and the second that it is fine.
    """
    return BackupFacts(
        record=backups.read_backup_status(),
        drop_box=backups.request_available(),
        stale_after_hours=get_settings().backup_stale_hours,
        now=dt.datetime.now(dt.UTC),
    )


def _host_stamp(raw: object) -> dt.datetime | None:
    """A timestamp the host wrote, as an aware datetime, or None if it cannot be read.

    scripts/lib/state.sh writes `date -u +%Y-%m-%dT%H:%M:%SZ`. None means exactly "this
    cannot be read as a time" and never "a long time ago": a value that does not parse
    gets its own sentence instead of falling through the age ladder, because "we cannot
    read the date" and "there is no backup" are different sentences.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _host_flag(raw: object) -> bool | None:
    """`verified` as the three answers it has: proved, disproved, and not known.

    The host's renderer has one type -- every field is a JSON string or null -- so this
    arrives as "true"/"false" rather than as a boolean. (The Settings endpoint lets
    pydantic coerce it; nothing has coerced it here.) Anything else is None, and None
    must read as neither answer: a dump nobody has restored is not disproved, and a
    value this version cannot read is not a tick.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in {"true", "yes", "1", "on"}:
            return True
        if token in {"false", "no", "0", "off"}:
            return False
    return None


def _span(delta: dt.timedelta) -> str:
    """A duration as a number and a unit, with no direction attached to it.

    Clamped at zero so that no caller can put a negative age in front of the owner --
    the future-timestamp branch below passes the skew, which is the figure that is true
    there, and the clamp is the second guard rather than the first. Units mirror
    spanLabel() in BackupsPanel.tsx: hours up to two days, because the staleness policy
    is written in hours and "1.5 days" is not comparable to "36 hours" at a glance.
    """
    seconds = max(delta.total_seconds(), 0.0)
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = int(seconds // 3600)
    if hours < 48:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    days = int(seconds // 86400)
    return f"{days} day{'' if days == 1 else 's'}"


def _key_part(raw: object) -> str:
    """A record value made safe to put in a key: one short token, never empty.

    `key` is String(200) and a record can be hand-edited, so a field is never pasted in
    at whatever length it happens to be.
    """
    text = " ".join(str(raw).split())[:60]
    return text or "unknown"


def _backup_age_candidate(facts: BackupFacts, record: dict[str, Any]) -> Candidate | None:
    """AT MOST ONE candidate for "how old is the backup".

    Every branch below is a different reading of that single question, so exactly one of
    them may speak. Two would put two contradictory sentences in one feed -- "nothing has
    ever been backed up" above "last verified backup: 9 hours ago" -- which is the
    failure the whole regime exists to avoid, delivered by the thing meant to prevent it.
    """
    verified_at_raw = record.get("last_verified_at")
    verified_at = _host_stamp(verified_at_raw)

    if verified_at is None:
        # A field that is absent, null or blank is the same answer -- the host has never
        # recorded one -- and only a value with something in it can be an unreadable one.
        recorded = verified_at_raw is not None and (
            not isinstance(verified_at_raw, str) or bool(verified_at_raw.strip())
        )
        if recorded:
            # The host recorded something that is not a time. Not stale (unknowable) and
            # not absent (there is a value), so it gets its own sentence, keyed on the
            # unreadable value: a different unreadable value is a different fact.
            return Candidate(
                key=f"backup_date_unreadable:{_key_part(verified_at_raw)}",
                type="backup",
                severity="warn",
                title="The last verified backup has no readable date",
                body=f'The host recorded "{verified_at_raw}" as the time of the newest '
                "verified backup, which cannot be read as a time, so this box cannot tell "
                "you how old your backup is. Run `./scripts/backup.sh --status` on the "
                "host for the raw record.",
                link=BACKUP_LINK,
            )
        success_raw = record.get("last_success_at")
        if isinstance(success_raw, str) and success_raw.strip():
            # Dumps are being taken and nothing has ever been restored from one. The
            # loudest kind of quiet failure: every number on the page moves, and none of
            # it is evidence of a backup.
            #
            # A BARE key, on purpose. The state does not change when tonight's unverified
            # dump replaces last night's, so keying on last_success_at would nag every
            # night while nothing changed. And it cannot recur: last_verified_at is
            # carried forward by the host for ever once set, so once anything has been
            # verified this branch is unreachable and there is no second occurrence to
            # report.
            return Candidate(
                key="backup_never_verified",
                type="backup",
                severity="alert",
                title="No backup on this box has ever been verified",
                body=f"Dumps are being taken (the last at {success_raw}) but nothing on "
                "this box has ever been restored from one, so there is no evidence that "
                "any of them is a backup. A dump can be complete, gzip cleanly and still "
                "restore into an empty database. Run `./scripts/backup.sh --verify-only` "
                "on the host, or take one from Settings, to turn the newest dump into "
                "something you can rely on.",
                link=BACKUP_LINK,
            )
        # A record exists and no run has ever got as far as a dump. Same key as the
        # no-record-at-all case below, because it is the same state told by a different
        # file: nothing has ever been backed up here.
        return _backup_never_run()

    skew = verified_at - facts.now
    if skew > dt.timedelta(0):
        # An LXC container takes its clock from the Proxmox host, so a restored snapshot
        # produces exactly this. The age is NOT reported, because the clock it would be
        # measured against is the thing in doubt; the skew is reported instead, as a
        # positive span, because that figure is true.
        #
        # A BARE key, on purpose, and this is the one place the trade-off cuts the other
        # way: a host whose clock is ahead writes a NEW future timestamp every night, so
        # keying on last_verified_at would alert nightly about one unchanged fact. One
        # alert per box for "the host clock is ahead" is the honest amount; the cost is
        # that a clock fixed and later broken again is reported in the feed only once.
        return Candidate(
            key="backup_clock_ahead",
            type="backup",
            severity="warn",
            title="The last verified backup is timestamped in the future",
            body=f"The host recorded it at {verified_at_raw}, which is {_span(skew)} ahead "
            "of this box's clock, so nothing here can tell you how old your backup is. An "
            "LXC container takes its clock from the Proxmox host, and restoring a snapshot "
            "produces exactly this. Check `timedatectl` on the host.",
            link=BACKUP_LINK,
        )

    age = facts.now - verified_at
    if age.total_seconds() / 3600 > facts.stale_after_hours:
        # Keyed on the timestamp it is stale about: see the block comment. Rendered from
        # the parsed instant rather than the raw field so that the key is bounded and two
        # spellings of one instant cannot produce two alerts.
        stamp = verified_at.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        return Candidate(
            key=f"backup_stale:{stamp}",
            type="backup",
            severity="warn",
            title=f"Last verified backup: {_span(age)} ago",
            body=f"That is older than the {facts.stale_after_hours} hours this box expects "
            "(SAIVA_BACKUP_STALE_HOURS), so the nightly run has not been working for at "
            "least a day. Everything entered since is in the database and nowhere else. "
            "Check `systemctl list-timers saiva-backup.timer` and `journalctl -u "
            "saiva-backup.service` on the host.",
            link=BACKUP_LINK,
        )
    # Inside the window. Nothing is said, because there is nothing wrong with the age --
    # and the feed is not where good news belongs. Whether the LAST run's dump could be
    # restored, and whether a copy left the box, are separate questions answered below.
    return None


def _backup_never_run() -> Candidate:
    """Nothing has ever been backed up on this box, and the regime is installed.

    One alert, ever. In the documented flow the owner runs `./scripts/backup.sh`
    immediately after installing, so this normally never fires at all; if they skipped
    it, one alert is exactly right and a second would add nothing.

    The wording is the panel's ("Nothing has been backed up yet") and is careful not to
    say that no dump was ever TAKEN, because one reachable state puts this sentence and
    the verify-failure sentence in the feed together: the very first nightly run takes a
    dump, the verification fails, and the record then has a broken dump and no
    successful run. Both are true there -- there is no backup, and here is why -- and
    the headline on the Settings page does exactly the same thing. "No dump has ever
    been taken" would have been the contradiction.
    """
    return Candidate(
        key="backup_never_run",
        type="backup",
        severity="alert",
        title="Nothing has been backed up yet",
        body="The backup regime is installed here, but no run has ever finished "
        "successfully, so there is no backup of this household's data at all. Take one "
        "now from Settings, or run `./scripts/backup.sh` on the host, rather than waiting "
        "for tonight's timer.",
        link=BACKUP_LINK,
    )


def _backup_verify_candidate(record: dict[str, Any]) -> Candidate | None:
    """A dump was taken and it could not be restored, so it is not a backup.

    The loudest state on this box. It is deliberately NOT part of the age ladder: a run
    can fail its verification while a verified backup from last night is still the
    newest thing on the disk, and both facts are then true at once -- the age is fine
    and the regime is failing now. The panel renders them together for the same reason.
    """
    if _host_flag(record.get("verified")) is not False:
        # None included: an absent or unreadable `verified` is "not known", and not
        # known must never be rendered as either answer.
        return None
    said = record.get("verify_message")
    quoted = (
        f'The host said: "{said}"'
        if isinstance(said, str) and said.strip()
        else "The host did not record what the restore proved, only that it did not come "
        "back whole."
    )
    # Keyed on the run, so each night that produces a broken dump says so. That is not
    # nagging: a fresh dump was taken and found unrestorable, which is new news every
    # time. run_id is written before anything else in a run, so it is there in practice;
    # the fallbacks exist because a field that is absent must not silently drop the one
    # alert that matters most.
    identity = record.get("run_id") or record.get("dump_name") or record.get("started_at")
    return Candidate(
        key=f"backup_verify_failed:{_key_part(identity)}",
        type="backup",
        severity="alert",
        title="A dump was taken, but it could not be restored, so it is not a backup",
        body=f"{quoted} A dump that does not restore is not a backup, however cleanly "
        "pg_dump exited, so the regime is failing now and the age of your newest verified "
        "backup has stopped moving. The dump has been kept for inspection; run "
        "`./scripts/backup.sh --verify-only` on the host to watch the restore fail.",
        link=BACKUP_LINK,
    )


# What each off-box state means, in the words BackupsPanel.tsx already uses for it
# rather than new ones for the same states. Every one of them is a sentence about where
# the copy got to, and not one of them may read as good news: only `remote` means the
# copy left the machine, and `remote` produces no candidate at all.
_OFFSITE_SENTENCES: dict[str, tuple[str, str]] = {
    "off": (
        "Nothing is copied off this box",
        "No off-box destination is configured, so the only copy of each dump is "
        "in the db_backups volume on this host, beside the database it protects. One disk "
        "failure or one lost container takes both. Set SAIVA_BACKUP_COPY_TO in "
        "/etc/saiva/agent.conf to a path on a network share, or SAIVA_BACKUP_COPY_CMD to "
        "a command that pushes it somewhere.",
    ),
    "same-filesystem": (
        "The backup copy is on the same filesystem as the database",
        "It was copied, but to the same filesystem the database lives on, so losing that "
        "disk loses the database and every backup of it in the same moment. This protects "
        "you from deleting the volume; it does not protect you from the disk.",
    ),
    "same-host": (
        "The backup copy is still on this machine",
        "It was copied to a different filesystem, but one on this same machine -- and "
        "this box cannot tell from the inside whether that is a different disk, because a "
        "btrfs subvolume, a ZFS dataset, an LVM thin volume and a Proxmox mount point all "
        "look like separate devices while sharing one pool. It does not survive losing "
        "the machine, so it is not yet a backup of this box.",
    ),
    "unknown": (
        "This box could not work out how far away the backup copy went",
        "The copy was made, but nothing here could establish whether the destination is "
        "on this machine or another one, so this box will not claim either. Check by hand "
        "what SAIVA_BACKUP_COPY_TO in /etc/saiva/agent.conf actually points at.",
    ),
    "failed": (
        "The off-box copy of the backup did not happen",
        "The destination could not be written to, or the configured command exited "
        "non-zero, so no copy left this box on the last run. The dump itself is "
        "still in the db_backups volume -- this is about the copy, not the dump. Check "
        "that the destination is mounted and writable.",
    ),
}


def _backup_offsite_candidate(record: dict[str, Any]) -> Candidate | None:
    """Where the copy got to -- one candidate per distinct state, and none for `remote`.

    WHETHER THIS BELONGS IN THE FEED AT ALL was the judgement call. It is a standing
    configuration fact rather than a change of state, so it could reasonably have been
    left on the Settings page. It is here because dedup by key makes it cheap in exactly
    the way that objection fears: keyed on the STATE, `off` fires once for the lifetime
    of the box and then never again, and the only thing that can make it speak a second
    time is the state getting worse or different (off -> failed, same-host ->
    same-filesystem), which is a change worth hearing about. A box one disk failure away
    from losing the database and every backup of it should not have to be asked.
    Weighed against that, one line in the feed on the day the box is installed is not
    noise.

    `warn`, never `alert`, for all of them, and that is the other half of the judgement:
    `alert` here is reserved for "you have no backup you can restore from", and in every
    one of these states the verified dump is still on the box. (BackupsPanel gives
    `failed` its loudest TONE, which is about weight on one card where the states sit
    side by side; this is about how loudly a feed entry reads next to an over-budget
    category, and the two questions have different answers.) An absent field says
    nothing at all -- a run that never reached the copy step has not reported on it.
    """
    state = record.get("offsite")
    if not isinstance(state, str) or not state.strip():
        return None
    state = state.strip()
    if state == BACKUP_OFF_BOX:
        return None
    where = record.get("offsite_target")
    said = record.get("offsite_message")
    if state in _OFFSITE_SENTENCES:
        title, body = _OFFSITE_SENTENCES[state]
    else:
        # Rendered as itself, like the panel's default branch. A state this version has
        # not learned is something the owner should see, not something to drop -- and it
        # is certainly not to be assumed benign.
        title = f"The off-box copy reported a state this version does not know: {state}"
        body = (
            "Nothing here can say whether a copy of your backup left this machine. Run "
            "`./scripts/backup.sh --self-check` on the host to see what it means."
        )
    if isinstance(where, str) and where.strip():
        body = f"{body} The destination recorded was {where.strip()}."
    if isinstance(said, str) and said.strip():
        body = f"{body} The host said: \"{said.strip()}\""
    return Candidate(
        key=f"backup_offsite:{_key_part(state)}",
        type="backup",
        severity="warn",
        title=title,
        body=body,
        link=BACKUP_LINK,
    )


def _backup_candidates(facts: BackupFacts) -> list[Candidate]:
    """Everything the feed has to say about the backup regime on this box."""
    record = facts.record
    if record is None:
        if not facts.drop_box:
            # NOTHING. There is no status file and no drop-box, so the host side was
            # never installed -- the owner may be updating over SSH and may never have
            # wanted this. The Settings page says so plainly, and an alert here would
            # fire on every install that does not use the feature, which is the fastest
            # way to teach a household to ignore this type.
            return []
        # The drop-box is there, so the regime is installed and nothing has ever run.
        return [_backup_never_run()]

    if record.get("phase") == "unreadable":
        # read_backup_status() returns this synthetic record when the file is there and
        # cannot be parsed. It carries no timestamps, so the age ladder would read it as
        # "nothing has ever been backed up" -- a thing we do not know. It is therefore
        # the whole answer and nothing else is attempted: not being able to say anything
        # about the backups is not the same as there being none.
        #
        # A BARE key: the record could not be parsed, so by definition there is no value
        # in it to key on. One alert per box, and the page carries the rest.
        return [
            Candidate(
                key="backup_unreadable",
                type="backup",
                severity="warn",
                title="The host's backup record could not be read",
                body="The file is there but nothing here could parse it, so this box can "
                "say nothing about your backups either way -- which is not the same as "
                "there being none. Run `./scripts/backup.sh --status` on the host for the "
                "raw record.",
                link=BACKUP_LINK,
            )
        ]

    return [
        candidate
        for candidate in (
            _backup_age_candidate(facts, record),
            _backup_verify_candidate(record),
            _backup_offsite_candidate(record),
        )
        if candidate is not None
    ]


def _candidates(
    db: Session,
    household: models.Household,
    today: dt.date,
    ns: models.NotificationSettings,
    backup: BackupFacts | None = None,
) -> list[Candidate]:
    out: list[Candidate] = []

    for b in list_budgets(db, household, today=today):
        if b.status == "over":
            out.append(
                Candidate(
                    key=f"budget_over:{b.id}:{b.period_start}",
                    type="budget",
                    severity="alert",
                    title=f"Over budget: {b.category_name}",
                    body=f"{_money(b.actual_cents)} of {_money(b.limit_cents)} "
                    f"spent on {b.category_name} this {b.period}.",
                    link="/budgets",
                    amount_cents=b.actual_cents - b.limit_cents,
                )
            )
        elif b.status == "warning":
            out.append(
                Candidate(
                    key=f"budget_warn:{b.id}:{b.period_start}",
                    type="budget",
                    severity="warn",
                    title=f"Nearing budget: {b.category_name}",
                    body=f"{_money(b.actual_cents)} of {_money(b.limit_cents)} "
                    f"used on {b.category_name} this {b.period}.",
                    link="/budgets",
                    amount_cents=b.actual_cents,
                )
            )

    for bill in recurring_service.upcoming_bills(
        db, household.id, days=BILL_LOOKAHEAD_DAYS, today=today
    ):
        out.append(
            Candidate(
                key=f"bill:{bill.merchant}:{bill.due_date}",
                type="bill",
                severity="info",
                title=f"Bill due: {bill.merchant}",
                body=f"{bill.merchant} ~{_money(bill.amount_cents)} due {bill.due_date}.",
                link="/bills",
                amount_cents=bill.amount_cents,
            )
        )

    large = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household.id,
                models.Transaction.txn_date >= today - dt.timedelta(days=LARGE_TXN_LOOKBACK_DAYS),
                models.Transaction.amount_cents <= -ns.large_txn_threshold_cents,
                models.Transaction.is_transfer.is_(False),
                models.Transaction.split_parent_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for t in large:
        out.append(
            Candidate(
                key=f"large_txn:{t.id}",
                type="large_txn",
                severity="info",
                title="Large transaction",
                body=f"{t.merchant or t.raw_description} "
                f"{_money(-t.amount_cents)} on {t.txn_date}.",
                link="/transactions",
                amount_cents=-t.amount_cents,
            )
        )

    f = forecast_service.forecast(db, household.id, days=LOW_BALANCE_HORIZON_DAYS, today=today)
    if f.low_balance_cents < ns.low_balance_threshold_cents:
        out.append(
            Candidate(
                key=f"low_balance:{f.low_balance_date}",
                type="low_balance",
                severity="alert" if f.low_balance_cents < 0 else "warn",
                title="Low balance ahead",
                body=f"Projected balance dips to {_money(f.low_balance_cents)} "
                f"around {f.low_balance_date}.",
                link="/forecast",
                amount_cents=f.low_balance_cents,
            )
        )

    for ins in generate_insights(db, household, today=today).insights:
        if ins.severity in ("alert", "warn") and ins.type in INSIGHT_ALERT_TYPES:
            out.append(
                Candidate(
                    key=f"insight:{ins.key}",
                    type="unusual_spend",
                    severity=ins.severity,
                    title=ins.title,
                    body=ins.body,
                    link=ins.link,
                    amount_cents=ins.amount_cents,
                )
            )

    # The state of the box, not of this household -- see the block above. Read here when
    # no caller supplied it (the feed endpoint calls generate() straight), handed down
    # once per run by run_all(), which is the path that loops over households.
    out += _backup_candidates(backup if backup is not None else backup_facts())
    return out


def generate(
    db: Session,
    household: models.Household,
    today: dt.date | None = None,
    backup: BackupFacts | None = None,
) -> list[models.Notification]:
    """Idempotently create notifications for any new candidates; return new ones.

    `backup` is the host's backup record, read once by the caller when there is more
    than one household to generate for. Left out, it is read here.
    """
    today = today or dt.date.today()
    ns = settings_for(db, household.id)
    existing = set(
        db.execute(
            select(models.Notification.key).where(
                models.Notification.household_id == household.id
            )
        ).scalars()
    )
    created: list[models.Notification] = []
    for c in _candidates(db, household, today, ns, backup=backup):
        if c.key in existing:
            continue
        existing.add(c.key)
        note = models.Notification(
            household_id=household.id,
            key=c.key,
            type=c.type,
            severity=c.severity,
            title=c.title,
            body=c.body,
            link=c.link,
            amount_cents=c.amount_cents,
        )
        db.add(note)
        created.append(note)
    if created:
        db.commit()
        for note in created:
            db.refresh(note)
    return created


def _digest_due(ns: models.NotificationSettings, today: dt.date) -> bool:
    if ns.digest not in ("weekly", "monthly"):
        return False
    if ns.last_digest_at is None:
        return True
    period = 7 if ns.digest == "weekly" else 30
    return (today - ns.last_digest_at.date()).days >= period


def _digest_body(db: Session, household: models.Household, today: dt.date) -> str:
    f = forecast_service.forecast(db, household.id, days=30, today=today)
    bills = recurring_service.upcoming_bills(db, household.id, days=14, today=today)
    over = [b for b in list_budgets(db, household, today=today) if b.status == "over"]
    lines = [
        f"Your Saiva summary for {today:%d %b %Y}",
        "",
        f"Balance today: {_money(f.starting_balance_cents)}",
        f"Projected in 30 days: {_money(f.end_balance_cents)} "
        f"(low {_money(f.low_balance_cents)} around {f.low_balance_date})",
        "",
        f"Budgets over limit: {len(over)}",
        f"Bills due in the next 14 days: {len(bills)} "
        f"({_money(sum(b.amount_cents for b in bills))})",
    ]
    return "\n".join(lines)


def _alerts_body(notes: list[models.Notification]) -> str:
    lines = ["New alerts from Saiva:", ""]
    lines += [f"- [{n.severity}] {n.title} — {n.body}" for n in notes]
    return "\n".join(lines)


# How far back an un-emailed alert is still worth an email. Past this it is history,
# and the app's own feed is the right place to read it.
EMAIL_BACKLOG_DAYS = 7
# Ceiling on one email's contents, so a first run after a quiet week is a digest
# rather than a wall.
MAX_ALERTS_PER_EMAIL = 25


def _pending_email(
    db: Session, household_id: str, today: dt.date
) -> list[models.Notification]:
    """Alerts that still need emailing — not merely the ones created just now.

    `generate` is idempotent on `key`, and the notifications feed calls it on every
    read. So whenever a browser tab loaded before the cron ran, the row already
    existed, `generate` returned nothing new, and the alert was never emailed to
    anybody. The condition that matters is "has this been sent", which is what
    `emailed_at` records; anything already read in the app is dropped, since the
    household has plainly seen it.
    """
    cutoff = dt.datetime.combine(today - dt.timedelta(days=EMAIL_BACKLOG_DAYS), dt.time.min)
    rows = db.execute(
        select(models.Notification)
        .where(
            models.Notification.household_id == household_id,
            models.Notification.emailed_at.is_(None),
            models.Notification.read_at.is_(None),
            models.Notification.severity.in_(("alert", "warn")),
            models.Notification.created_at >= cutoff,
        )
        .order_by(models.Notification.created_at.desc())
        .limit(MAX_ALERTS_PER_EMAIL)
    ).scalars().all()
    return list(rows)


def run_for_household(
    db: Session,
    household: models.Household,
    today: dt.date | None = None,
    backup: BackupFacts | None = None,
) -> dict[str, int]:
    """Generate alerts and (if enabled) email new ones plus any due digest."""
    today = today or dt.date.today()
    ns = settings_for(db, household.id)
    created = generate(db, household, today=today, backup=backup)
    emailed = 0
    digest_sent = 0

    if ns.email_enabled and get_settings().smtp_configured:
        recipients = _recipients(db, household.id)
        urgent = _pending_email(db, household.id, today)
        if urgent and recipients and send_email(
            recipients, f"{len(urgent)} new alert(s) from Saiva", _alerts_body(urgent)
        ):
            now = dt.datetime.utcnow()
            for n in urgent:
                n.emailed_at = now
            emailed = len(urgent)
            db.commit()
        if _digest_due(ns, today) and recipients and send_email(
            recipients, f"Your {ns.digest} Saiva summary", _digest_body(db, household, today)
        ):
            ns.last_digest_at = dt.datetime.utcnow()
            db.commit()
            digest_sent = 1

    return {"created": len(created), "emailed": emailed, "digest_sent": digest_sent}


def run_all(db: Session, today: dt.date | None = None) -> dict[str, int]:
    # Read ONCE, before the loop. read_backup_status() and request_available() touch the
    # filesystem, and the backup is a property of the box: the answer cannot differ
    # between two households generated in the same run, so reading it per household
    # would be one stat and one open each for a value already in hand.
    backup = backup_facts()
    totals = {"households": 0, "created": 0, "emailed": 0, "digests": 0}
    for household in db.execute(select(models.Household)).scalars():
        r = run_for_household(db, household, today=today, backup=backup)
        totals["households"] += 1
        totals["created"] += r["created"]
        totals["emailed"] += r["emailed"]
        totals["digests"] += r["digest_sent"]
    return totals
