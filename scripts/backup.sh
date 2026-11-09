#!/usr/bin/env bash
# Take a backup of the household database, prove it can be restored, put a copy somewhere
# else, and write down what happened.
#
#   ./scripts/backup.sh                 take one now: dump, verify, copy off-box, prune
#   ./scripts/backup.sh --verify-only   re-verify the newest dump; take nothing
#   ./scripts/backup.sh --status        print the backup record as JSON
#   ./scripts/backup.sh --list          list the dumps (hands over to restore-backup.sh)
#   ./scripts/backup.sh --self-check    is the regime installed, and is it healthy?
#
# FOUR CLAIMS, AND NOTHING MORE. The regime exists to be able to say, honestly:
#
#   1. a dump was taken at time T;
#   2. it was RESTORED into a throwaway database and came back whole;
#   3. a copy of it reached a different filesystem;
#   4. the newest verified dump is N hours old -- here is the number.
#
# Anything it cannot show is reported as unknown. That is the same rule the update panel
# follows, for the same reason: the failure worth designing against is not a backup that
# breaks, it is a backup that is broken and reported as fine.
#
# WHY "VERIFIED" CANNOT MEAN "pg_dump EXITED 0", AND CANNOT MEAN "IT RESTORED EITHER.
# Measured, on PostgreSQL 16.15, against five dumps of the same 504-row database:
#
#   case                                   gunzip  marker  psql --single-transaction  rows
#   a good dump                              0      yes          0                   match
#   gzip truncated mid-stream                1       -           -                     -
#   pg_dump died before any COPY             0      NO           0                 MISMATCH
#   pg_dump died in the middle of a COPY     0      NO           0                 MISMATCH
#   --schema-only by mistake                 0      yes          0                 MISMATCH
#
# The third and fourth rows are the finding that decides this file. `psql
# --single-transaction -v ON_ERROR_STOP=1` EXITS 0 on a dump truncated inside a COPY
# block: when stdin ends without the `\.` terminator, psql finishes the COPY with whatever
# rows it got and reports success. So "it restored without error" is not evidence that a
# dump is complete, and a regime that checked only that would hand the owner a green tick
# over a dump missing 96% of their ledger.
#
# Four checks therefore run, and none of them is redundant:
#
#   gunzip's exit status   the only one that catches a truncated gzip stream (it verifies
#                          the CRC and the length trailer at the end of the member)
#   the completion marker  the only cheap one that catches a pg_dump that died partway.
#                          NOT the last line any more -- pg_dump 16.10+ emits
#                          `\unrestrict <token>` after it -- so it is a grep over the tail
#   row counts, both ways  the only one that catches --schema-only, and it catches both
#                          truncations a second time. Exact and race-free, because both
#                          sides describe the same dump: the COPY blocks declare what the
#                          dump contains and the restored database is counted after
#                          loading it. Nothing is compared against a live database that is
#                          still being written to.
#   the live aggregate     a dump of the right database that came back with no rows in it.
#                          It is deliberately NOT described as catching "a dump of the
#                          wrong database" any more, because it cannot: the dump comes from
#                          $DB and the aggregate counts $DB, so the two always agree.
#   the marker tables      which IS what answers "is this a dump of Saiva at all":
#                          `households` and a schema revision have to be in the restored
#                          copy. Point POSTGRES_DB at an empty database and the four checks
#                          above all pass on a 396-byte dump of nothing -- measured -- and
#                          the retention then retires the real dumps behind it.
#
# WHY IT RUNS ON THE HOST. The thing that produces the safety net must not be the thing a
# bad release can break, and a backup is most wanted on the night the api container will
# not start. Everything here goes through scripts/lib/pg.sh -- the same runners
# scripts/restore-backup.sh uses -- in a throwaway container built from the db service's
# own image, so nothing new is installed and nothing new is trusted.
#
# WHAT IT WILL NOT DELETE. The prune removes old `-nightly` dumps and nothing else. The
# `-premigrate` dumps the api writes before a migration, and the `-prerestore` dumps
# restore-backup.sh writes before it replaces a database, are left alone however old they
# are: this script may remove what it created, and may not remove what other paths did.
# Their count is reported instead, so a volume filling up is visible rather than silently
# tidied.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# ENV_FILE and SAIVA_STATUS_KIND look unread here because their readers are the
# libraries sourced below -- pg.sh reads the first, state.sh the second.
# shellcheck disable=SC2034
ENV_FILE="${SAIVA_ENV_FILE:-$ROOT/.env}"
SCRIPT_DIR="$ROOT/scripts"

# shellcheck source=lib/env-file.sh
. "$SCRIPT_DIR/lib/env-file.sh"
# shellcheck source=lib/state.sh
. "$SCRIPT_DIR/lib/state.sh"

# Everything written from here lands in backup-status.{kv,json} and backup.log rather than
# the updater's files. Set BEFORE anything calls status_* or log(), because that is what
# both of them read to decide which file they are writing.
# shellcheck disable=SC2034
SAIVA_STATUS_KIND=backup-status

# shellcheck source=lib/pg.sh
. "$SCRIPT_DIR/lib/pg.sh"

# pg.sh asks the sourcing script for these three. state.sh already supplies log(), which
# sends a line to stdout (the systemd journal, when a timer runs this) and appends it to
# the run log the status file quotes.
blank() { printf '\n'; }

# die() RECORDS what it says. It used to only log it, and then _finalise_on_exit replaced
# `message` with a generic "stopped at step X without finishing" -- so check_room's
# refusal, which names the free space, the database size and what to do about it, never
# reached the page at all. The owner saw a failed backup blaming whatever step happened to
# be set last, with the actual diagnosis only in the journal.
#
# The guard is `status_get run_id`: before backup_begin there is no run to attach a message
# to, and writing one then would go over the record of whatever ran last.
die() {
  log "  $*"
  if [ -n "$(status_get run_id 2>/dev/null || true)" ]; then
    status_set message "$*"
    status_flush
  fi
  exit 1
}

# The exit status for "I declined to run, and deliberately wrote nothing". 75 is
# EX_TEMPFAIL, and it exists because 0 was ambiguous in a way that destroyed the record:
# the host agent treats "exited 0 and wrote no status" as a run that died silently and
# writes a failure document of its own over backup-status.json -- which does not carry the
# carried-forward fields, so a press of "Back up now" during a release erased
# last_verified_at and the page then said there was no backup at all. Reproduced.
#
# systemd treats any non-zero as a failed unit, which is the honest reading for a backup
# that did not happen; `SuccessExitStatus=75` on saiva-backup.service would soften that if
# the noise ever matters more than the signal.
SAIVA_BACKUP_EX_DECLINED=75

CONF="${SAIVA_AGENT_CONF:-/etc/saiva/agent.conf}"
ACTION="run"
TRIGGER="cli"
SCRATCH_DB=""        # the throwaway database, while it exists
DUMP_NAME=""
DUMP_PART=""
DUMP_REVISION=""
STAGED=""          # the host-side copy of a dump, while an off-box copy is in flight
OFFSITE_STATE=""   # how far away the copy actually got; see SAIVA_OFFSITE_STATES
PREVIOUS_VERIFIED=""   # what the run before this one concluded
VERIFIED_THIS_RUN=0    # this run's own verdict, not read back out of a shared file

# ---------------------------------------------------------------------------------------
# configuration root acts on
#
# SAIVA_BACKUP_COPY_TO and SAIVA_BACKUP_COPY_CMD are read from /etc/saiva/agent.conf and
# NOT from .env, and that split is a security boundary rather than tidiness. This script
# runs as root under a systemd timer. A destination path taken from .env would let anyone
# who can write the checkout have root create files where they chose; a command taken from
# .env would simply be root. agent.conf is root-owned and its own header already says that
# anything in it runs as root.
#
# The file is READ, never sourced, so that reading the configuration cannot itself execute
# anything -- and it is ignored outright unless its ownership and mode say only root can
# change it.
# ---------------------------------------------------------------------------------------

conf_is_trustworthy() {
  [ -f "$CONF" ] || return 1
  local owner mode
  owner="$(stat -c '%u' "$CONF" 2>/dev/null || echo 1)"
  mode="$(stat -c '%a' "$CONF" 2>/dev/null || echo 777)"
  [ "$owner" = "0" ] || return 1
  # Nothing but root may change it: no group-write (2) and no other-write (2) bit.
  case "$mode" in
    *[2367]) return 1 ;;            # other-write
  esac
  local grp="${mode: -2:1}"
  case "$grp" in
    2|3|6|7) return 1 ;;            # group-write
  esac
  return 0
}

conf_value() { # KEY -> value, or empty
  conf_is_trustworthy || return 0
  local line
  line="$(grep -E "^[[:space:]]*$1=" "$CONF" 2>/dev/null | tail -n 1)" || true
  [ -n "$line" ] || return 0
  line="${line#*=}"
  case "$line" in
    \"*\") line="${line#\"}"; line="${line%\"}" ;;
    \'*\') line="${line#\'}"; line="${line%\'}" ;;
  esac
  printf '%s' "$line"
}

# Environment first (an operator running this by hand, and the test suite), then
# agent.conf. Nothing here falls back to .env: see the comment above.
COPY_TO="${SAIVA_BACKUP_COPY_TO-$(conf_value SAIVA_BACKUP_COPY_TO)}"
COPY_CMD="${SAIVA_BACKUP_COPY_CMD-$(conf_value SAIVA_BACKUP_COPY_CMD)}"
MIN_FREE_KB="${SAIVA_BACKUP_MIN_FREE_KB:-$(conf_value SAIVA_BACKUP_MIN_FREE_KB)}"
case "$MIN_FREE_KB" in *[!0-9]*) MIN_FREE_KB="" ;; esac
KEEP_NIGHTLY="${SAIVA_BACKUP_KEEP_NIGHTLY:-$(conf_value SAIVA_BACKUP_KEEP_NIGHTLY)}"
KEEP_NIGHTLY="${KEEP_NIGHTLY:-14}"
case "$KEEP_NIGHTLY" in
  ''|*[!0-9]*) KEEP_NIGHTLY=14 ;;
esac
# Never zero, whatever the configuration says. A retention of 0 means "delete the backup
# you just verified", which is not a policy anybody means to express.
[ "$KEEP_NIGHTLY" -ge 1 ] || KEEP_NIGHTLY=1

# ---------------------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------------------

backup_begin() { # TRIGGER
  local run; run="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  # Captured before the clear below, because the pre-dump prune needs it: the last thing
  # this box knows about its backups decides whether anything may be deleted today.
  PREVIOUS_VERIFIED="$(status_get verified)"
  # Clears only what this run is certain to replace. The VERDICT fields -- verified,
  # verify_message, rows_checked, dump_revision, dump_name, dump_bytes -- are cleared by
  # take_dump and verify_dump instead, at the moment those actually begin.
  #
  # Clearing them here was wrong in a way a reviewer reproduced: a run that then went no
  # further -- the stack was down, the lock was held -- had already erased "a dump was
  # taken but it could not be restored", and the page went from red to a shrug. The
  # carried-forward timestamps survived, so the owner saw a backup age and no reason to
  # distrust it. Only a run that LOOKED at a dump may change what is known about one.
  local f
  for f in step message exit_code finished_at pruned; do
    status_set "$f" ""
  done
  status_set run_id "$run"
  status_set trigger "$1"
  status_set started_at "$(now)"
  status_set phase running
  status_set step preflight
  status_flush
}

# Mirrors _finalise_on_exit in scripts/lib/release.sh, for the same reason: a run that
# stops without saying so leaves YESTERDAY's green record standing, and the owner reads a
# verified backup from before the failure as though it were last night's.
# A signal is not an exit status. systemd's TimeoutStartSec, a host shutdown mid-dump or a
# Ctrl-C all terminate the shell without any command having failed, so $? at the top of the
# EXIT trap is the status of whatever ran last -- usually 0. The record then said "stopped
# without finishing (exit status 0)" with exit_code 0 beside phase failed, which reads as a
# contradiction and tells the owner nothing. Trapping the signals gives the trap a real
# status to report; _finalise_on_exit also refuses to print a 0 it does not believe.
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

_finalise_on_exit() {
  local rc=$?
  drop_scratch_db
  remove_partial_dump
  remove_staged
  case "$(status_get phase)" in
    running) : ;;
    *) return 0 ;;
  esac
  # rc 0 here means the shell was terminated without any command failing -- a signal that
  # arrived before the traps above were installed, or one they do not cover. Saying "exit
  # status 0" beside phase failed is a contradiction; saying nothing is the honest version,
  # and the renderer turns an empty exit_code into null.
  local how="exit status $rc"
  [ "$rc" = "0" ] && how="no exit status -- it was terminated rather than failing"
  # A message die() already wrote is the better one: it names the actual cause. The
  # generic sentence is for the exits nobody wrote a reason for -- a signal, an unguarded
  # command failing under set -e.
  local said step msg
  said="$(status_get message)"
  step="$(status_get step)"
  msg="The backup stopped at step '${step}' without finishing (${how}). Run \
./scripts/backup.sh on the host to see why; \`journalctl -u saiva-backup.service\` has the \
last run's output."
  [ -n "$said" ] && msg="${said} (Stopped at step '${step}', ${how}.)"
  status_finish failed "$msg" "$([ "$rc" = "0" ] || printf '%s' "$rc")"
  log ""
  log "✖ Stopped at step '${step}' without finishing (${how})."
}

# The stamp in a dump's name as an ISO timestamp, which is what the page can parse.
# `stamp_from_name` renders "2026-09-30 03:00:00 UTC" for people; this is for machines.
iso_from_name() { # DUMP_NAME -> 2026-09-30T03:00:00Z, or nothing
  local n="$1" d t
  if [[ "$n" =~ ^saiva-([0-9]{8})-([0-9]{6})- ]]; then
    d="${BASH_REMATCH[1]}"; t="${BASH_REMATCH[2]}"
    printf '%s-%s-%sT%s:%s:%sZ' \
      "${d:0:4}" "${d:4:2}" "${d:6:2}" "${t:0:2}" "${t:2:2}" "${t:4:2}"
  fi
}

finish_ok() { # MESSAGE
  status_set last_success_at "$(now)"
  # last_verified_at is WHEN THE DUMP WAS TAKEN, not when the verification ran. For a
  # nightly run those are seconds apart and it makes no difference; for `--verify-only`
  # it is the whole difference, because that re-verifies whatever is newest in the
  # volume. Taking the clock meant re-verifying a forty-day-old dump moved the panel's
  # one headline to "Last verified backup: 0 hours ago" -- a true statement about the
  # verification presented as a claim about the backup. Reproduced by a reviewer.
  # VERIFIED_THIS_RUN, not status_get: every status_* call is a read-modify-write of a
  # file shared with any other invocation, so reading the verdict back out of it made the
  # decision depend on what else had touched the file in between. A reviewer reproduced a
  # concurrent press clearing the field between `status_set verified true` and this line,
  # after which a verified backup never advanced the one number the page reports.
  if [ "$VERIFIED_THIS_RUN" = "1" ]; then
    local taken; taken="$(iso_from_name "$DUMP_NAME")"
    status_set last_verified_at "${taken:-$(now)}"
  fi
  status_set step "done"
  status_finish ok "$1" 0
}

# ---------------------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------------------

# ONE lock, and it is the UPDATE's lock -- $STATE/deploy.lock, taken by
# scripts/lib/release.sh for the whole of an --apply-release.
#
# A second `backup.lock` was the first design and it was wrong: it made two backups
# mutually exclusive and left a backup free to run while a release recreated the db
# container underneath it. Sharing one lock makes "an update, a rollback and a backup
# never overlap" true in a single line, and there is no window to reason about.
#
# `exec 9>` rather than `flock <file> <cmd>`: an flock belongs to the open file
# DESCRIPTION, so the lock survives the `exec` the release path does after `git checkout`,
# and `if ! flock` already tells "busy" apart from "my command failed" without needing -E.
#
# How long to wait depends on who asked, and that distinction is the point:
#
#   timer / cli   wait up to SAIVA_BACKUP_LOCK_WAIT (10 minutes). A nightly run that
#                 lands during a release should happen after it, not be lost.
#   button        do not wait at all. Somebody is looking at a page; "already running"
#                 now is a better answer than a spinner for ten minutes.
#
# A run that cannot get the lock in time is SKIPPED, not failed, and leaves
# last_success_at / last_verified_at untouched. "The backup failed" and "no backup was
# taken, and the newest one is N hours old" are different sentences; recording the first
# when the second is true is how a regime trains its owner to ignore it.
# A run that cannot get the lock writes NOTHING and exits 0.
#
# That is the same rule acquire_lock in scripts/lib/release.sh follows, and the reason is
# the same: the record belongs to the run that IS going, and this process knows nothing
# about it. Writing "skipped" over a live run's status -- which an earlier version did --
# corrupts the record of the run that is actually working, because every status_set is a
# read-modify-write of one shared file. A reviewer reproduced exactly that: a press of
# "Back up now" during a nightly run overwrote the nightly run's run_id, started_at and
# verdict, and the page then attributed the first run's result to the second.
#
# What the owner sees instead is the page continuing to report the run in flight, and the
# age of the newest VERIFIED backup -- which is the number that matters and which a
# skipped run must never reset.
acquire_lock_or_leave() {
  local lock wait; lock="$(state_dir)/deploy.lock"
  exec 9>"$lock"
  if [ "$TRIGGER" = "button" ]; then
    # No wait at all: somebody is looking at a page, and "already running" now beats a
    # spinner for ten minutes.
    flock -n 9 && return 0
    log "Another Saiva job holds the lock; nothing was taken, and the record of the run"
    log "that holds it has been left alone."
    exit "$SAIVA_BACKUP_EX_DECLINED"
  fi
  wait="${SAIVA_BACKUP_LOCK_WAIT:-600}"
  flock -w "$wait" 9 && return 0
  log "Could not get the deploy lock within ${wait}s -- a release is being applied -- so no"
  log "backup was taken. An update takes a pre-migration dump of its own, and the timer"
  log "runs again tomorrow. The age of the newest VERIFIED backup is the number to watch."
  exit "$SAIVA_BACKUP_EX_DECLINED"
}

# ---------------------------------------------------------------------------------------
# the two reasons not to try at all
# ---------------------------------------------------------------------------------------

# `docker compose down` removes the saiva_default network, and a dump needs the database
# up. A nightly run that fires while the stack is deliberately stopped is SKIPPED, not
# failed -- a unit that reports failure every night for a box somebody turned off trains
# its owner to ignore it, and then the one real failure is ignored too. The age of the
# newest verified backup does the shouting instead, which is what it is for.
skip_unless_stack_is_up() {
  [ "$DIRECT" -eq 1 ] && return 0
  command -v docker >/dev/null 2>&1 || return 0   # docker_preflight says this better
  docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || {
    skip "The Docker daemon is not answering, so nothing could be dumped."
  }
  docker network inspect "$NETWORK" >/dev/null 2>&1 || {
    skip "The stack is down -- there is no ${NETWORK} network, which \`docker compose \
down\` removes -- so the database could not be reached and nothing was dumped. Start it \
with \`make up\` and the timer will catch up."
  }
}

skip() { # MESSAGE -- a run that should not have tried, recorded as such
  status_set step skipped
  status_finish skipped "$1" 0
  log "Skipped: $1"
  exit 0
}

# pg_dump REFUSES to dump from a server newer than itself ("aborting because of server
# version mismatch"). Right now the two match by construction -- scripts/lib/pg.sh pins
# the client image to postgres:16-alpine and the compose file runs the same image as the
# server -- but SAIVA_DB_IMAGE is overridable and that tag is deliberately floating, so
# the day the database is moved to 17 the nightly dump starts failing with a message about
# version numbers and nothing about what to do. Said here instead, before anything is
# written. It also matters for the VERIFY: a dump from a newer server cannot be replayed
# into an older one at all, so there would be no way to prove the backup.
check_version_skew() {
  status_step preflight "Checking the client and the server are the same major version."
  local server client
  server="$(psql_on "$MAINT_DB" 'show server_version_num' 2>/dev/null || true)"
  # `pg_dump (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)` -> 16. Taken as the third
  # FIELD rather than by a regex over the line: the obvious `.*[^0-9]\([0-9]*\)\.` is
  # greedy and pulls "04" out of the Ubuntu build suffix, which then reads as a client
  # older than every server and refuses every backup.
  client="$(pg_sh 'exec pg_dump --version' 2>/dev/null | head -1 | awk '{print $3}' | cut -d. -f1 || true)"
  case "$client" in ''|*[!0-9]*) client="" ;; esac
  [ -n "$server" ] && [ -n "$client" ] || return 0      # cannot tell; pg_dump will say
  local server_major=$(( server / 10000 ))
  [ "$client" -ge "$server_major" ] && return 0
  die "The database server is PostgreSQL ${server_major} and the client this script uses is
  ${client}. pg_dump refuses to dump from a server newer than itself, so no backup can be
  taken until they match -- and a dump taken from ${server_major} could not be replayed
  into ${client} to verify it even if it could be written.
  Point SAIVA_PG_IMAGE at a postgres:${server_major} image (it is also what
  scripts/restore-backup.sh uses), or move the db service back to ${client}."
}

# SIGKILL runs no trap. The OOM killer, `systemctl stop` past TimeoutStopSec, an LXC stop
# and a power cut all deliver one, and what is left behind is a complete copy of the
# household ledger in a throwaway database and another in a staged file -- neither of which
# anything would ever reclaim, because the next run makes its own with a different name.
# A reviewer measured one such leftover at the full size of the database.
#
# On one box this could be unconditional -- the deploy lock means there is no other run
# whose scratch database this could be. But a Postgres SERVER can be shared by more than
# one checkout (this repository's own test suites do exactly that), and dropping a database
# another run is restoring into would be a new way to break a verification. So a scratch
# database is only reclaimed when its own name says nobody is using it: the trailing pid is
# not a live process, or the stamp in it is more than an hour old, which is longer than
# saiva-backup.service's TimeoutStartSec allows a run to last.
sweep_orphans() {
  local stale
  stale="$(psql_on "$MAINT_DB" \
    "select datname from pg_database where datname like 'saiva\\_verify\\_%'" 2>/dev/null || true)"
  local db pid stamp cutoff
  cutoff="$(date -u -d '1 hour ago' +%Y%m%d%H%M%S 2>/dev/null || printf '0')"
  for db in $stale; do
    case "$db" in saiva_verify_*) ;; *) continue ;; esac
    pid="${db##*_}"
    stamp="${db#saiva_verify_}"; stamp="${stamp%_*}"
    case "$pid" in ''|*[!0-9]*) pid=1 ;; esac           # unreadable: treat as alive
    if kill -0 "$pid" 2>/dev/null && [ "$stamp" \> "$cutoff" ]; then
      continue                                          # a run that is still going
    fi
    log "  Dropping an abandoned verification database left by an earlier run: ${db}"
    psql_on "$MAINT_DB" "DROP DATABASE IF EXISTS \"${db}\" WITH (FORCE)" >/dev/null 2>&1 || true
  done
  # And any staged off-box copy from a run that did not get to clean up.
  local f
  for f in "$(state_dir)"/staging/.offsite.*; do
    [ -f "$f" ] || continue
    log "  Removing a staged off-box copy left by an earlier run: $(basename "$f")"
    rm -f -- "$f"
  done
}

# ---------------------------------------------------------------------------------------
# the dump
# ---------------------------------------------------------------------------------------

# How much room to insist on before starting, in KB. A dump that fills the volume leaves a
# partial file AND no space to write the next one, and the prune runs after verification on
# purpose, so there is nothing to reclaim first. Deleting backups to make room for a backup
# is not a trade this script makes.
free_kb_in_volume() {
  backups_sh 'df -P "$1" 2>/dev/null | tail -n 1 | awk "{print \$4}"' "$BACKUP_MOUNT"
}

# The estimate is dominated by the VERIFY, not by the dump. Replaying the dump into a
# throwaway database on the same server writes roughly another whole copy of the data into
# db_data -- which, because db_data and db_backups are both local Docker volumes, is the
# same filesystem the dump is on. So the figure to beat is about the size of the database
# itself, plus room for the dump, and not "twice the last dump".
check_room() {
  status_step room "Checking there is space to dump and to verify the dump."
  local free need size_kb
  free="$(free_kb_in_volume | tr -cd '0-9' || true)"
  [ -n "$free" ] || { log "  Could not read free space in ${SOURCE_LABEL}; continuing."; return 0; }

  # `|| true` on the pipeline, not just 2>/dev/null. Suppressing the MESSAGE is not
  # suppressing the STATUS: psql exits 2 when the database is not there, pipefail hands
  # that to the assignment, and set -e then kills the run at a line whose whole purpose
  # was to make a cautious guess. Every pipeline-into-an-assignment in this file is
  # explicit about what it does when the pipeline fails, for that reason.
  size_kb="$(psql_on "$DB" "select pg_database_size('${DB}') / 1024" 2>/dev/null | tr -cd '0-9' || true)"
  size_kb="${size_kb:-0}"
  # 5/4 of the database for the throwaway copy and its indexes, plus 64 MB of headroom
  # that also covers the dump itself (gzipped plain SQL is a fraction of the database).
  need=$(( size_kb * 5 / 4 + 65536 ))
  # Read the same way as the other three settings -- environment, then agent.conf -- and
  # not from the environment alone. The refusal below names agent.conf as the place to set
  # it, and it was not read from there, so the one escape hatch from this estimate could
  # not be reached by anything the nightly timer runs.
  need="${MIN_FREE_KB:-$need}"
  case "$need" in ''|*[!0-9]*) need=65536 ;; esac

  if [ "$free" -lt "$need" ]; then
    die "Only $(human_size $(( free * 1024 ))) free where the dumps live, and this needs about
  $(human_size $(( need * 1024 ))) -- the database is $(human_size $(( size_kb * 1024 ))) and
  verifying the dump replays it into a throwaway database on the same filesystem.

  Nothing was dumped, and nothing was deleted to make room beyond the retention already
  configured: trading the backups you have for one that has not been verified yet is not a
  trade this script makes. Free space on the host, lower SAIVA_BACKUP_KEEP_NIGHTLY in
  ${CONF}, or set SAIVA_BACKUP_MIN_FREE_KB if you know this estimate is too cautious here.

  This matters more than a failed backup: db_data and db_backups share one filesystem, so
  filling it stops Postgres rather than merely stopping the backup."
  fi
}

take_dump() {
  status_step dump "Dumping database \"${DB}\"."
  # Cleared here rather than in backup_begin: see its comment. From this line on, this run
  # owns the verdict, because from this line on it is about to produce one.
  local f
  for f in dump_name dump_bytes dump_revision verified verify_message rows_checked; do
    status_set "$f" ""
  done
  local stamp; stamp="$(date -u +%Y%m%d-%H%M%S)"
  # The stamp has one-second resolution and that collides in practice, not in theory: a
  # timer firing and a "Back up now" press land in the same second often enough that
  # restore-backup.sh's safety dump already carries this loop and a test that matches
  # `-prerestore(-[0-9]+)?`. Taking the next free name rather than overwriting matters
  # because the file being overwritten would be a verified backup, under exactly the name
  # somebody may have just been told to restore.
  DUMP_NAME="saiva-${stamp}-nightly.sql.gz"
  local n=1
  while backups_sh 'test -e "$1/$2"' "$BACKUP_MOUNT" "$DUMP_NAME"; do
    n=$(( n + 1 ))
    DUMP_NAME="saiva-${stamp}-nightly-${n}.sql.gz"
    [ "$n" -lt 50 ] || die "fifty dumps already carry the stamp ${stamp}; something is looping."
  done
  # A leading dot and a .part suffix, either of which alone keeps it out of the listing's
  # `*.sql.gz` glob. A half-written dump must never be something restore-backup.sh offers.
  DUMP_PART=".${DUMP_NAME}.part"

  # pg_dump compresses its own output rather than being piped into gzip, and that is not a
  # micro-optimisation: `pg_dump | gzip` in the container's shell reports GZIP's exit
  # status, busybox ash cannot be relied on for `set -o pipefail`, and a pg_dump that dies
  # halfway then looks like a success. One process, one exit code. Verified: with plain
  # format and --file, pg_dump 16 writes exactly the name it is given and does not append
  # .gz, so the rename below is safe.
  if ! pg_sh '
      exec pg_dump --host "$1" --port "$2" --username "$3" --dbname "$4" \
        --no-owner --no-privileges --format=plain --compress=6 --file "$5/$6"
    ' "$PGHOST_EFF" "$PGPORT_EFF" "$PGUSER_EFF" "$DB" "$BACKUP_MOUNT" "$DUMP_PART"; then
    die "pg_dump failed; the error is above. Nothing was written that could be mistaken
  for a backup."
  fi

  local bytes
  bytes="$(backups_sh 'wc -c < "$1/$2" 2>/dev/null || printf 0' "$BACKUP_MOUNT" "$DUMP_PART" | tr -cd '0-9')"
  [ "${bytes:-0}" -gt 0 ] || die "pg_dump exited 0 but wrote an empty file. Nothing was kept."

  # 0600 before the rename, so the dump is never briefly readable under its final name.
  # Nothing needs it wider: the restore runs as root, and the api container reads the age
  # out of the status file rather than the directory -- which is also the only way "this
  # dump was verified" and "the copy never left the box" can be expressed at all.
  backups_sh 'chmod 0600 -- "$1/$2" 2>/dev/null; mv -f -- "$1/$2" "$1/$3"' \
    "$BACKUP_MOUNT" "$DUMP_PART" "$DUMP_NAME"
  DUMP_PART=""
  status_set dump_name "$DUMP_NAME"
  status_set dump_bytes "$bytes"
  status_flush
  log "  Wrote ${DUMP_NAME} ($(human_size "$bytes"))."
}

remove_partial_dump() {
  [ -n "$DUMP_PART" ] || return 0
  backups_sh 'rm -f -- "$1/$2"' "$BACKUP_MOUNT" "$DUMP_PART" 2>/dev/null || true
  DUMP_PART=""
}

# ---------------------------------------------------------------------------------------
# the verification -- a real restore into a throwaway database
# ---------------------------------------------------------------------------------------

# Counts of the rows the dump DECLARES, one `<table> <n>` line each, sorted.
#
# A COPY block's data section ends at a line holding exactly `\.`. In COPY text format a
# backslash in a value is written `\\`, so that line cannot occur inside data -- the count
# is exact rather than approximate.
#
# `!intbl &&` on the header rule is load-bearing, and it was missing. A DATA row can frame
# a COPY header: two text columns holding `COPY public.notes (q` and `) FROM stdin;` emit
# the single physical line `COPY public.notes (q<TAB>) FROM stdin;`, which matched, reset
# the counter mid-block and failed verification on a perfectly restorable dump. Reproduced
# with a real pg_dump. Inside a block only `\.` can end it, so the header rule has no
# business being consulted there.
#
# The marker line is counted in the same pass and reported as `=marker <n>`, because
# reading the dump twice doubles the only expensive thing this script does.
dump_declares() { # DUMP_NAME -> lines on stdout; exit 3 if the dump could not be read
  local name="$1" out
  out="$(mktemp)"
  local -a rc
  set +e
  dump_stream "$name" | awk '
    !intbl && /^COPY .+ \(.*\) FROM stdin;$/ {
      # The identifier is taken by position, not as $2. pg_dump quotes a name that needs
      # it -- COPY public."MixedCase Table" (id, v) FROM stdin; -- and the default field
      # splitting then yields public."MixedCase for $2, so the key never matched what the
      # database reported, and verification failed on a restorable dump.
      # (No apostrophes in here: this program is inside a single-quoted shell string, and
      # the word "awk" followed by one is how that string came to end three lines early.)
      t = $0
      sub(/^COPY /, "", t)
      i = index(t, " (")
      if (i > 1) t = substr(t, 1, i - 1)
      n = 0; intbl = 1; next
    }
    intbl && $0 == "\\." { print t, n; intbl = 0; next }
    intbl { n++; next }
    /^-- PostgreSQL database dump complete$/ { marker++ }
    END { print "=marker", marker + 0 }
  ' > "$out"
  rc=("${PIPESTATUS[@]}")
  set -e
  # gunzip's status is the whole point of the pipeline, and it does not survive a command
  # substitution -- inside $( ) PIPESTATUS describes the assignment. Hence the temp file.
  # awk reads to EOF rather than exiting at the match on purpose: an early exit would hand
  # gunzip a SIGPIPE and an intact dump would look like a corrupt one.
  if [ "${rc[0]}" -ne 0 ]; then
    rm -f "$out"
    return 3
  fi
  sort "$out"
  rm -f "$out"
}

# Exact counts for every table in the restored copy, in the same `<table> <n>` shape.
# query_to_xml rather than n_live_tup: the latter is a statistic and means nothing until
# something has ANALYZEd the table, which a freshly restored database has not.
db_declares() { # DBNAME
  # `format('%I.%I', ...)` rather than `'public.' || relname`, and every non-system schema
  # rather than public alone, because the key has to match the one dump_declares reads out
  # of pg_dump's COPY header verbatim. pg_dump writes `schema.table`, quoting an identifier
  # only where SQL requires it -- which is exactly what %I does. The previous version
  # disagreed with the dump on a non-public schema, on a mixed-case name and on any
  # identifier needing quotes: Saiva has none of those today, so the symptom would have
  # been the FIRST migration to add one making every nightly backup fail verification,
  # every night, with a message about row counts and nothing about the real cause.
  #
  # relkind = 'r' deliberately, which includes a partitioned table's PARTITIONS and
  # excludes the parent ('p') -- the same set pg_dump emits COPY blocks for by default. A
  # dump taken with --load-via-partition-root would not match, and nothing here takes one.
  psql_on "$1" "SELECT format('%I.%I', n.nspname, c.relname) || ' ' ||
      (xpath('/row/c/text()', query_to_xml(
        format('SELECT count(*) AS c FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\\_%'" | sort
}

live_row_total() {
  db_declares "$DB" 2>/dev/null | awk '{ t += $2 } END { print t + 0 }' || true
}

drop_scratch_db() {
  [ -n "$SCRATCH_DB" ] || return 0
  # Every exit path, including a failed verification and a Ctrl-C. A throwaway database
  # left behind is a copy of the household's ledger nobody is looking after.
  psql_on "$MAINT_DB" "DROP DATABASE IF EXISTS \"${SCRATCH_DB}\" WITH (FORCE)" >/dev/null 2>&1 || true
  SCRATCH_DB=""
}

verify_dump() { # DUMP_NAME
  local name="$1"
  status_step verify "Restoring ${name} into a throwaway database."
  # --verify-only reaches this without take_dump, so the verdict is cleared here too.
  status_set verified ""
  status_set verify_message ""
  status_set rows_checked ""
  VERIFIED_THIS_RUN=0

  local declared restored problems=() marker rows=0
  declared="$(dump_declares "$name")" || {
    verify_failed "${name} could not be read all the way through: gunzip reported a \
truncated or corrupt archive. This dump is not restorable."
    return 1
  }
  marker="$(printf '%s\n' "$declared" | awk '$1 == "=marker" { print $2 }' || true)"
  declared="$(printf '%s\n' "$declared" | grep -v '^=marker ' || true)"
  rows="$(printf '%s\n' "$declared" | awk '{ t += $2 } END { print t + 0 }' || true)"

  if [ "${marker:-0}" -lt 1 ]; then
    problems+=("pg_dump's \"dump complete\" line is missing, so pg_dump did not finish")
  fi

  # The scratch database. Checked against the live name rather than merely made unlikely
  # to collide: everything below DROPs this database, and the one name it must never be is
  # the one holding the household's records.
  SCRATCH_DB="saiva_verify_$(date -u +%Y%m%d%H%M%S)_$$"
  case "$SCRATCH_DB" in
    *[!A-Za-z0-9_]*) die "internal error: scratch database name \"$SCRATCH_DB\" is not an identifier" ;;
  esac
  [ "$SCRATCH_DB" != "$DB" ] || die "internal error: the scratch database name equals the live one"

  psql_on "$MAINT_DB" "CREATE DATABASE \"${SCRATCH_DB}\" TEMPLATE template0" >/dev/null

  local -a rc
  set +e
  dump_stream "$name" | pg_sh_stdin '
    exec psql -X -q -o /dev/null -v ON_ERROR_STOP=1 --single-transaction \
      --host "$1" --port "$2" --username "$3" --dbname "$4" -f -
  ' "$PGHOST_EFF" "$PGPORT_EFF" "$PGUSER_EFF" "$SCRATCH_DB"
  rc=("${PIPESTATUS[@]}")
  set -e
  if [ "${rc[1]}" -ne 0 ]; then
    problems+=("psql exited ${rc[1]} replaying it into a throwaway database")
  fi
  if [ "${rc[0]}" -ne 0 ]; then
    problems+=("the archive could not be read all the way through on replay (gunzip exited ${rc[0]})")
  fi

  restored="$(db_declares "$SCRATCH_DB")"
  if [ -z "$declared" ] && [ -n "$restored" ]; then
    # Said plainly rather than as a diff against nothing. This is what `--schema-only`
    # produces, and a diff listing every table as "extra" buries the one fact that
    # matters: there is no data in here at all.
    problems+=("it contains no table data at all -- $(printf '%s\n' "$restored" | grep -c .) \
tables were created and not one row was loaded, which is what a --schema-only dump looks like")
  elif [ "$declared" != "$restored" ]; then
    problems+=("what the dump declares and what came back differ: $(
      diff <(printf '%s\n' "$declared") <(printf '%s\n' "$restored") \
        | grep -E '^[<>]' | tr '\n' ';' | cut -c1-240)")
  fi

  # The live aggregate. It used to be described as catching "a dump of the WRONG database",
  # and it cannot: the dump is taken from $DB and `live_row_total` counts $DB, so the two
  # always agree. Point POSTGRES_DB at an empty database and both sides read zero -- which
  # is how a dump of 396 bytes containing nothing at all came back `verified: true`. A
  # reviewer reproduced it three times.
  #
  # What it does catch, and all it claims to: a dump of the RIGHT database that arrived
  # with no rows in it. The structural check below is what makes "this is a dump of Saiva"
  # a question with an answer.
  local live; live="$(live_row_total || true)"
  if [ "${live:-0}" -gt 0 ] && [ "$rows" -eq 0 ]; then
    problems+=("the database holds ${live} rows and this dump holds none")
  fi

  # THE MARKER TABLES. A dump is only a backup of this household's records if those
  # records are in it, and the only way to ask that is structurally. `households` is the
  # table backend/app/dbinit.py itself uses to tell a real Saiva database from an empty
  # one, and scripts/restore-backup.sh's own verification asks the same question -- if
  # dbinit would not recognise the restored copy as a Saiva database, neither should this.
  #
  # This closes the case the aggregate above could not: a mistyped POSTGRES_DB, or one
  # pointing at a database that has never been migrated, otherwise produced a tiny,
  # internally consistent, perfectly restorable dump of nothing, every night, reported as
  # a verified backup -- and the retention then deleted the real dumps behind it. The
  # fourteenth night would have been the last one that could be restored from.
  local marker
  marker="$(psql_on "$SCRATCH_DB" "select to_regclass('public.households') is not null" 2>/dev/null || true)"
  if [ "$marker" != "t" ]; then
    problems+=("the restored copy has no \`households\` table, so it is not a dump of a \
Saiva database at all -- check POSTGRES_DB in .env, which this run read as \"${DB}\"")
  fi

  DUMP_REVISION="$(psql_on "$SCRATCH_DB" \
    "select coalesce(string_agg(version_num, ','), '') from alembic_version" 2>/dev/null || true)"
  status_set dump_revision "$DUMP_REVISION"
  if [ -z "$DUMP_REVISION" ]; then
    problems+=("the restored copy has no schema revision in \`alembic_version\`, so \
either this is not a Saiva database or the dump predates Alembic -- a dump that cannot say \
which schema it is cannot be restored with any confidence")
  fi

  # One real query across three tables, so the verification covers the foreign keys, the
  # enum types and the indexes coming back -- not only that rows arrived somewhere.
  if [ -n "$(psql_on "$SCRATCH_DB" "select to_regclass('public.transactions')" 2>/dev/null || true)" ]; then
    psql_on "$SCRATCH_DB" "select count(*) from households h
        left join accounts a on a.household_id = h.id
        left join transactions t on t.account_id = a.id" >/dev/null 2>&1 \
      || problems+=("the restored copy cannot answer a join across households, accounts and transactions")
  fi

  drop_scratch_db

  if [ "${#problems[@]}" -gt 0 ]; then
    local joined; joined="$(printf '%s; ' "${problems[@]}")"
    verify_failed "${joined%; }"
    return 1
  fi

  VERIFIED_THIS_RUN=1
  status_set verified true
  status_set rows_checked "$rows"
  status_set verify_message "Restored into a throwaway database: ${rows} rows across \
$(printf '%s\n' "$declared" | grep -c . || true) tables came back exactly as the dump declares\
${DUMP_REVISION:+, at schema revision ${DUMP_REVISION}}."
  status_flush
  log "  Verified: ${rows} rows restored and counted back."
}

verify_failed() { # MESSAGE
  status_set verified false
  status_set verify_message "$1"
  status_flush
  log "  VERIFICATION FAILED: $1"
}

# ---------------------------------------------------------------------------------------
# the copy that leaves this box
# ---------------------------------------------------------------------------------------

# Where the dumps really live on the host, so a destination can be compared against it.
# In --direct mode that is the directory itself; otherwise it is the Docker volume's
# mountpoint, which is a real path even under fuse-overlayfs.
dumps_host_path() {
  if [ "$DIRECT" -eq 1 ]; then printf '%s' "$BACKUP_MOUNT"; return 0; fi
  docker volume inspect -f '{{.Mountpoint}}' "$VOLUME" 2>/dev/null || true
}

# How far away a destination really is: `same-filesystem`, `same-host`, `remote`, or
# `unknown`. FOUR answers, not two, and the reason is that st_dev alone gets this wrong in
# precisely the configurations a Proxmox box is most likely to have.
#
# `stat -c %d` is the cheap test and it is only reliable in ONE direction. If the device
# numbers match, the destination is certainly on the same filesystem -- that answer is
# sound, and it is the one that matters most, because a copy on the same disk is not a
# backup. But a DIFFERENT device number does not mean a different disk:
#
#   btrfs subvolume     its own super_block, its own st_dev, the same pool on the same disks
#   ZFS dataset         the same, for the same reason
#   LVM thin volume     a different device, often the same physical PV
#   a Proxmox `mp`      a bind-mount from the host: different st_dev, same machine
#
# Each of those would have been reported as "copied off this box" by a device-number
# comparison, which is the exact shape of false reassurance this whole regime exists to
# remove. So st_dev decides "same filesystem", and findmnt's FSTYPE decides whether what
# is on the other side is a network filesystem -- the only local evidence that a copy has
# actually left the machine. Everything else is `same-host`: better than nothing (it
# survives a volume being deleted, or a bad migration) and not a backup of this box.
#
# A configured push command (rclone, restic, rsync over ssh) reports `remote` on its own
# say-so, because that is the one case where the destination genuinely is not a path here.
#
# Two things this cannot determine from inside the container, which is why the runbook
# asks a person: whether an NFS server is in fact this same machine re-exporting itself,
# and whether a Proxmox mp's host-side storage is a different physical disk.
#
# Measured: `mount --bind` leaves st_dev unchanged on both sides, and findmnt is the only
# one of stat/df/findmnt that reveals the bind at all (its SOURCE carries a bracketed
# subpath). `findmnt -T` on a path that does not exist exits 1 and prints NOTHING rather
# than walking up to the nearest ancestor -- hence the mkdir before this is called.
destination_distance() { # DUMPS_PATH DEST -> same-filesystem|same-host|remote|unknown
  local a="$1" b="$2" da db fstype
  [ -n "$b" ] && [ -d "$b" ] || { printf unknown; return 0; }
  if [ -n "$a" ] && [ -d "$a" ]; then
    da="$(stat -c '%d' "$a" 2>/dev/null || true)"
    db="$(stat -c '%d' "$b" 2>/dev/null || true)"
    if [ -n "$da" ] && [ -n "$db" ] && [ "$da" = "$db" ]; then printf same-filesystem; return 0; fi
  fi
  command -v findmnt >/dev/null 2>&1 || { printf unknown; return 0; }
  fstype="$(findmnt -n -o FSTYPE -T "$b" 2>/dev/null | tail -1 || true)"
  [ -n "$fstype" ] || { printf unknown; return 0; }
  case "$fstype" in
    nfs|nfs4|cifs|smb3|smbfs|fuse.sshfs|fuse.rclone|fuse.s3fs|glusterfs|ceph) printf remote ;;
    *) printf same-host ;;
  esac
}

# The complete off-box vocabulary, declared rather than scattered, because two other
# languages have to agree with it: backend/app/services/backups.py passes it through
# verbatim and frontend/src/components/BackupsPanel.tsx has a sentence for each.
# backend/tests/test_deployment_hardening.py keeps the three in step.
#
# Ordered LEAST protective last, which is how a headline is chosen when more than one
# destination is configured: the page reports the worst outcome, never the best.
SAIVA_OFFSITE_STATES=(remote unknown same-host same-filesystem failed off)

# Keep the least protective state seen so far. There is deliberately no "better than"
# direction: a destination can only lower the verdict, never raise it.
_offsite_worsen() { # STATE
  local candidate="$1" state rank_new=-1 rank_old=-1 i=0
  for state in "${SAIVA_OFFSITE_STATES[@]}"; do
    [ "$state" = "$candidate" ] && rank_new=$i
    [ "$state" = "${OFFSITE_STATE:-}" ] && rank_old=$i
    i=$(( i + 1 ))
  done
  [ "$rank_new" -gt "$rank_old" ] && OFFSITE_STATE="$candidate"
  return 0
}

copy_offsite() { # DUMP_NAME
  local name="$1" at="" notes=() targets=()
  # Starts EMPTY and ends as `unknown` if nothing set it. It used to end as `ok`, via
  # `${worst:-ok}`, and that was the one place in this file where an unset value defaulted
  # to the most reassuring answer in its union -- so a code path that forgot to say what
  # happened said "copied off this box". Found by the agent writing the panel, which could
  # not work out which state a remote destination would actually produce.
  OFFSITE_STATE=""

  if [ -z "$COPY_TO" ] && [ -z "$COPY_CMD" ]; then
    status_set offsite off
    status_set offsite_target ""
    status_set offsite_message "No off-box destination is configured, so the only copy of \
this backup is in the db_backups volume on this host. A disk failure takes the database \
and the backup together. Set SAIVA_BACKUP_COPY_TO in ${CONF}."
    status_flush
    log "  No off-box copy configured."
    return 0
  fi

  status_step offsite "Copying ${name} off this box."
  if ! stage_dump_on_host "$name"; then
    status_set offsite failed
    status_set offsite_message "The dump could not be read out of ${SOURCE_LABEL} to copy it."
    status_flush
    return 0
  fi
  local staged="$STAGED"

  if [ -n "$COPY_TO" ]; then
    targets+=("$COPY_TO")
    if ! mkdir -p "$COPY_TO" 2>/dev/null; then
      notes+=("${COPY_TO}: cannot be created")
      _offsite_worsen failed
    elif [ -L "$COPY_TO/$name" ]; then
      # A symlink at the destination path makes `cp` write THROUGH it, and the chmod
      # below follow it: root then writes the household ledger wherever the link points
      # and relaxes that file to 0600. The destination is root-configured, but agent.conf
      # recommends exactly the places other parties can write -- a NAS, an NFS or CIFS
      # share, a Proxmox mount point -- and a dump name is a stamp anyone can predict a
      # second at a time. A reviewer planted 91 links named for the next 91 seconds and
      # hit the target. So this is refused rather than resolved: nothing here needs to
      # follow a link, and a link where a dump should be is a destination to look at.
      notes+=("${COPY_TO}/${name} is a symlink, so nothing was written -- a dump is never \
copied through a link, because that writes root's copy of the ledger wherever the link \
points. Remove it, or point SAIVA_BACKUP_COPY_TO somewhere nothing else writes")
      _offsite_worsen failed
    elif ! { rm -f -- "$COPY_TO/$name" 2>/dev/null; cp -- "$staged" "$COPY_TO/$name" 2>/dev/null; }; then
      notes+=("${COPY_TO}: the copy failed -- check that it is mounted and writable")
      _offsite_worsen failed
    else
      chmod 0600 "$COPY_TO/$name" 2>/dev/null || true
      local distance fstype
      distance="$(destination_distance "$(dumps_host_path)" "$COPY_TO")"
      fstype="$(findmnt -n -o FSTYPE -T "$COPY_TO" 2>/dev/null | tail -1 || true)"
      case "$distance" in
        same-filesystem)
          notes+=("${COPY_TO}: copied, but it is on the SAME filesystem as the database, \
so losing that disk loses both")
          _offsite_worsen same-filesystem ;;
        same-host)
          notes+=("${COPY_TO}: copied, to ${fstype:-a local filesystem} on this same \
machine. That survives the volume being deleted; it does not survive the machine")
          _offsite_worsen same-host ;;
        remote)
          notes+=("${COPY_TO}: copied, to ${fstype} -- off this machine")
          _offsite_worsen remote ;;
        *)
          notes+=("${COPY_TO}: copied, but this box could not work out how far away it is")
          _offsite_worsen unknown ;;
      esac
      at="$(now)"
    fi
  fi

  if [ -n "$COPY_CMD" ]; then
    targets+=("the configured command")
    # The dump's path as $1, never interpolated into the string -- the same discipline as
    # every runner in scripts/lib/pg.sh, and for the same reason: a path or a password with
    # a space in it is then one argument and there is no quoting to get wrong.
    if sh -c "$COPY_CMD" sh "$staged" "$name"; then
      # The one destination taken on trust, and reasonably so: a command the owner
      # configured to push somewhere is the only one that is not a path on this machine for
      # this script to measure. It is stated rather than left to a default, so that a
      # COPY_CMD-only configuration says what it achieved instead of inheriting an answer.
      notes+=("the configured command accepted it, so this copy is off this machine")
      _offsite_worsen remote
      at="$(now)"
    else
      notes+=("the configured command exited non-zero")
      _offsite_worsen failed
    fi
  fi

  remove_staged
  if [ -z "$OFFSITE_STATE" ]; then
    OFFSITE_STATE=unknown
    notes+=("a destination was configured but this run did not record what happened to it, \
which is a bug in scripts/backup.sh -- treat the copy as not having been made")
  fi
  status_set offsite "$OFFSITE_STATE"
  status_set offsite_target "$(printf '%s, ' "${targets[@]}" | sed 's/, $//')"
  status_set offsite_at "$at"
  status_set offsite_message "$(printf '%s; ' "${notes[@]}" | sed 's/; $//')"
  status_flush
  log "  Off-box: ${OFFSITE_STATE}."
}

# A copy of the dump on the host, outside the volume, so that neither the destination nor
# the command ever sees the inside of a container. Mounting the destination into a
# throwaway container would be the shorter way and is the wrong one: it hands a path the
# operator chose to a container image, and gets the file's ownership from whatever that
# image's user is.
# Sets STAGED, and returns non-zero if it could not. It deliberately does NOT print the
# path, because the obvious `staged="$(stage_dump_on_host "$name")"` is what broke this:
# a command substitution runs in a SUBSHELL, so the STAGED it assigned belonged to a
# process that then exited, the parent's stayed empty, and both remove_staged calls -- the
# one in copy_offsite and the one in the EXIT trap -- removed nothing. Measured: five runs
# with a destination configured left five `.offsite.<pid>` files in the state directory,
# each a byte-identical copy of the household's whole ledger, growing without limit on the
# same filesystem the database is on.
stage_dump_on_host() { # DUMP_NAME -> sets STAGED
  local name="$1" tmp inside outside
  # In the state directory rather than /tmp: /tmp is tmpfs on many hosts, so a large dump
  # would be staged in RAM, and a systemd unit that ever gains PrivateTmp would stage it
  # somewhere the rest of the run cannot see. $STATE is root:root on local disk.
  #
  # In a 0700 SUBDIRECTORY of it rather than in $STATE itself, which is 0755 and
  # bind-mounted into the caddy container -- and the caddy image runs as root, so a 0600
  # root file sitting in a 0755 directory it can traverse is readable from inside it. The
  # file is a complete copy of the ledger; records/ gets the same 0700 treatment for the
  # same reason.
  local dir; dir="$(state_dir)/staging"
  ( umask 077; mkdir -p "$dir" ) 2>/dev/null || return 1
  chmod 0700 "$dir" 2>/dev/null || true
  ( umask 077; : > "$dir/.offsite.$$" ) 2>/dev/null || return 1
  tmp="$dir/.offsite.$$"
  STAGED="$tmp"
  local -a rc
  set +e
  backups_sh 'exec cat -- "$1/$2"' "$BACKUP_MOUNT" "$name" > "$tmp"
  rc=("${PIPESTATUS[@]}")
  set -e
  if [ "${rc[0]}" -ne 0 ] || [ ! -s "$tmp" ]; then rm -f -- "$tmp"; STAGED=""; return 1; fi
  # `cat` failing inside the container and the host redirection succeeding are independent
  # events, and a truncated stream alongside a zero exit is the trap this closes. The only
  # claim worth turning into a tick is that the BYTES arrived, so they are counted on both
  # sides rather than assumed.
  inside="$(backups_sh 'exec md5sum -- "$1/$2"' "$BACKUP_MOUNT" "$name" 2>/dev/null | cut -d" " -f1 || true)"
  outside="$(md5sum -- "$tmp" 2>/dev/null | cut -d' ' -f1 || true)"
  if [ -n "$inside" ] && [ -n "$outside" ] && [ "$inside" != "$outside" ]; then
    rm -f -- "$tmp"; STAGED=""; return 1
  fi
  return 0
}

remove_staged() {
  [ -n "$STAGED" ] || return 0
  rm -f -- "$STAGED"
  STAGED=""
}

# ---------------------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------------------

# Only files ROOT ITSELF WROTE, only `-nightly` names, and never the newest. The rm is
# given exact names one at a time; there is no glob in it.
#
# `-user <the uid that writes dumps here> -perm 600` is the part that matters, and it was
# missing. The prune decides what to keep by sorting FILENAMES, and the directory it sorts
# is one the api container has read-write -- docker-compose.prod.yml mounts db_backups into
# api so the pre-migration dump can be written there. A reviewer reproduced the
# consequence: three files named `saiva-99999999-999999-nightly.sql.gz` planted by anything
# with write access sort newest, fill the keep window, and root then deletes every real
# dump.
#
# The container can forge neither property. It runs as uid 10001, while a dump written
# through `docker run` on postgres:16-alpine is owned by uid 0; and take_dump chmods 0600
# before the rename. So the candidate list is the set of dumps this script wrote and
# nothing else, and a planted file is left where it is for scripts/restore-backup.sh's
# listing to name.
#
# The uid is COMPUTED rather than written as 0, because in --direct mode -- which is how
# the test suite exercises this, with no Docker -- the dump is written by the host process
# instead of by a container, so it carries that process's uid. Hardcoding 0 made every
# fixture unprunable under an unprivileged runner and four retention tests failed in CI
# while passing locally as root. The invariant being expressed is "this process wrote it",
# and 0 was only ever a proxy for that.
#
# `find` also does not follow symlinks, which is the other half: `[ -f ]` does, so the
# previous version counted a planted link as a nightly dump and unlinked it.
prune_nightlies() {
  status_step prune "Keeping the newest ${KEEP_NIGHTLY} nightly dumps."
  local removed owner
  # Who writes dumps here: the container's root through `docker run`, or this process in
  # --direct mode.
  if [ "$DIRECT" -eq 1 ]; then owner="$(id -u)"; else owner=0; fi
  removed="$(backups_sh '
    cd "$1" 2>/dev/null || exit 0
    keep="$2"
    n=0
    # Reverse-sorted by name, which is reverse-chronological: the stamp is YYYYMMDD-HHMMSS.
    for f in $(find . -maxdepth 1 -type f -user "$3" -perm 600 \
                 -name "saiva-????????-??????-nightly*.sql.gz" 2>/dev/null \
               | sed "s|^\./||" \
               | grep -E "^saiva-[0-9]{8}-[0-9]{6}-nightly(-[0-9]+)?\.sql\.gz$" \
               | sort -r); do
      n=$((n + 1))
      [ "$n" -le "$keep" ] && continue
      rm -f -- "$f" && printf "%s\n" "$f"
    done
  ' "$BACKUP_MOUNT" "$KEEP_NIGHTLY" "$owner" | grep -c . || true)"
  status_set kept "$KEEP_NIGHTLY"
  # cmd_run prunes TWICE -- to policy before the dump, and again after the verify -- and a
  # plain `status_set pruned` meant the second call overwrote the first. With 8 nightlies
  # and keep=5 the record said 1 when the run had removed 4 and then 1, under-reporting the
  # one number somebody checks after changing the retention.
  local before; before="$(status_get pruned)"
  case "$before" in ''|*[!0-9]*) before=0 ;; esac
  status_set pruned "$(( before + ${removed:-0} ))"
  [ "${removed:-0}" = "0" ] || log "  Removed ${removed} nightly dump(s) older than the newest ${KEEP_NIGHTLY}."
}

# What is in the volume now, for the page: how many dumps of each kind, and how old the
# oldest is. The `-premigrate` and `-prerestore` counts are reported precisely because
# nothing prunes them -- a volume filling up should be visible.
survey_dumps() {
  local line nightly other oldest
  line="$(backups_sh '
    cd "$1" 2>/dev/null || exit 0
    nightly=0; other=0; oldest=""
    for f in *.sql.gz; do
      [ -f "$f" ] || continue
      case "$f" in
        *-nightly.sql.gz|*-nightly-*.sql.gz) nightly=$((nightly + 1)) ;;
        *)                                   other=$((other + 1)) ;;
      esac
      # Only a STAMPED name can be the oldest, because the age is read out of the stamp.
      # `notes.sql.gz` sorts before `saiva-...`, so without this guard one saved note
      # beside the dumps made oldest_at render as null -- honest, but unexplainable, and
      # the page lost a figure it had.
      case "$f" in
        saiva-????????-??????-*) ;;
        *) continue ;;
      esac
      if [ -z "$oldest" ] || [ "$f" \< "$oldest" ]; then oldest="$f"; fi
    done
    printf "%s %s %s\n" "$nightly" "$other" "${oldest:--}"
  ' "$BACKUP_MOUNT")" || return 0
  read -r nightly other oldest <<<"$line"
  status_set backup_count "${nightly:-0}"
  [ "${oldest:--}" = "-" ] || status_set oldest_at "$(stamp_from_name "$oldest")"
  status_flush
  log "  In ${SOURCE_LABEL}: ${nightly:-0} nightly, ${other:-0} premigrate/prerestore (never pruned)."
}

# ---------------------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------------------

# The newest dump of ANY kind, which is what --verify-only should re-check: if the most
# recent thing in the volume is a pre-migration dump from an upgrade an hour ago, that is
# the one whose restorability matters. Deliberately broader than the PRUNE's pattern, which
# matches only what this script wrote.
newest_dump() {
  backups_sh '
    cd "$1" 2>/dev/null || exit 0
    ls -1 2>/dev/null | grep -E "^saiva-[0-9]{8}-[0-9]{6}-.*\.sql\.gz$" | sort | tail -n 1
  ' "$BACKUP_MOUNT"
}

cmd_run() {
  state_init
  # The lock comes BEFORE the record and before the EXIT trap, so an invocation that is
  # not going to run writes nothing and has no trap to fire. See acquire_lock_or_leave.
  acquire_lock_or_leave
  trap _finalise_on_exit EXIT
  backup_begin "$TRIGGER"

  skip_unless_stack_is_up
  docker_preflight 1
  load_settings
  db_preflight
  check_version_skew
  sweep_orphans
  # To policy, BEFORE the dump. Not to make room below the retention the owner chose --
  # never that -- but because dumps already past it are the only space there is to
  # reclaim, and filling this filesystem does not merely fail the backup: db_data and
  # db_backups are both local Docker volumes under /var/lib/docker, so they share it, and
  # Postgres meeting ENOSPC on its WAL is a hard stop rather than a slowdown. This box has
  # been here before -- see the crash-loop that wrote a full copy of the database on every
  # restart in docs/UPDATING.md.
  #
  # Unless the last thing this box knows is that a dump could NOT be restored. Then
  # nothing is deleted, however far past the retention it is: fourteen bad nights in a row
  # would otherwise retire the last restorable dump one night at a time, each run doing
  # exactly what it was configured to do. A regime that can delete its way to having
  # nothing is not one worth installing.
  if [ "$PREVIOUS_VERIFIED" = "false" ]; then
    log "  Not pruning: the last backup could not be restored, so nothing is being retired"
    log "  until one can. Fix the cause, or run ./scripts/backup.sh --verify-only."
  else
    prune_nightlies
  fi
  check_room
  take_dump
  verify_dump "$DUMP_NAME" || {
    # "nothing was pruned" was false and asserted anyway: the pre-dump prune has already
    # run by here, and on the first failing night after a good one it removed whatever was
    # past the retention. Say what the record says instead.
    local retired; retired="$(status_get pruned)"
    case "$retired" in ''|0) retired="nothing was retired before it" ;;
      *) retired="${retired} dump(s) past the retention had already been retired before \
this one was taken, and nothing will be retired again until a dump verifies" ;;
    esac
    status_finish failed "A dump was taken but it could not be restored, so it is not a \
backup. ${DUMP_NAME} has been kept for inspection; ${retired}. $(status_get verify_message)" 1
    return 1
  }
  copy_offsite "$DUMP_NAME"
  prune_nightlies          # again, now that this run has added one
  survey_dumps
  finish_ok "Backed up and verified by restoring it: $(status_get verify_message)"
}

cmd_verify_only() {
  state_init
  acquire_lock_or_leave
  trap _finalise_on_exit EXIT
  backup_begin "$TRIGGER"
  skip_unless_stack_is_up
  docker_preflight 1
  load_settings
  db_preflight
  # The off-box group is cleared, because this path copies nothing. Keeping it meant the
  # page put "a copy of it left this machine" beside a re-verified dump that had never been
  # copied anywhere -- the verdict was about last night's dump and the headline was about
  # this one. Unknown is the honest answer, and the page has a sentence for it.
  local f
  for f in offsite offsite_target offsite_at offsite_message; do status_set "$f" ""; done

  local name; name="$(newest_dump)"
  [ -n "$name" ] || die "There are no dumps in ${SOURCE_LABEL} to verify."
  # The GLOBAL as well as the record: finish_ok reads DUMP_NAME to work out when the dump
  # was taken, and leaving it empty here made --verify-only fall back to the clock -- the
  # exact defect the stamp is there to fix.
  DUMP_NAME="$name"
  status_set dump_name "$name"
  verify_dump "$name" || {
    status_finish failed "${name} could not be restored, so it is not a backup. \
$(status_get verify_message)" 1
    return 1
  }
  survey_dumps
  finish_ok "Re-verified ${name} by restoring it: $(status_get verify_message)"
}

cmd_status() {
  local f; f="$(state_dir)/backup-status.json"
  if [ -f "$f" ]; then cat "$f"; else echo "No backup has been taken on this box yet."; fi
}

cmd_self_check() {
  local issues=0 dir; dir="$(state_dir)"
  printf 'checkout         : %s\n' "$ROOT"
  printf 'state directory  : %s\n' "$dir"
  printf 'backup requests  : %s  [%s]\n' "$(backup_requests_dir)" \
    "$(stat -c '%U:%G %a' "$(backup_requests_dir)" 2>/dev/null || echo missing)"
  printf 'agent.conf       : %s  [%s]\n' "$CONF" \
    "$(conf_is_trustworthy && echo 'root-owned, not group/other writable' || echo 'missing, or writable by someone other than root -- its backup settings are IGNORED')"
  printf 'off-box copy     : %s\n' "${COPY_TO:-${COPY_CMD:+a command}}"
  printf 'keep nightly     : %s\n' "$KEEP_NIGHTLY"
  if [ -d /run/systemd/system ]; then
    printf 'timer            : %s\n' "$(systemctl is-active saiva-backup.timer 2>/dev/null || echo 'not installed')"
    printf 'next run         : %s\n' \
      "$(systemctl show -p NextElapseUSecRealtime --value saiva-backup.timer 2>/dev/null || echo unknown)"
    systemctl is-enabled saiva-backup.timer >/dev/null 2>&1 || {
      printf '\nNOT INSTALLED: nothing takes a nightly backup on this box.\n'
      printf 'Fix it by re-running: sudo %s/scripts/install-updater.sh\n' "$ROOT"
      issues=1
    }
  else
    printf 'timer            : systemd is not running here; see docs/BACKUPS.md for the cron line\n'
  fi
  if [ -z "$COPY_TO" ] && [ -z "$COPY_CMD" ]; then
    printf '\nNO OFF-BOX COPY: every backup lives on the same disk as the database it\n'
    printf 'protects, so one disk failure loses both. Set SAIVA_BACKUP_COPY_TO in %s.\n' "$CONF"
    issues=1
  fi
  local f="$dir/backup-status.json"
  if [ -f "$f" ]; then
    printf '\nlast run:\n'
    sed -n 's/^  "\(phase\|step\|started_at\|verified\|offsite\|last_verified_at\|message\)"/  \1/p' "$f" \
      | sed 's/^/  /'
  else
    printf '\nNO BACKUP HAS EVER BEEN TAKEN on this box.\n'
    issues=1
  fi
  return "$issues"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --verify-only) ACTION="verify-only" ;;
    --status)      ACTION="status" ;;
    --list)        ACTION="list" ;;
    --self-check)  ACTION="self-check" ;;
    --direct)      DIRECT=1 ;;
    --trigger)     [ $# -ge 2 ] || {
                     printf -- '--trigger needs a value: timer, cli or button\n' >&2; exit 2; }
                   shift; TRIGGER="$1" ;;
    --trigger=*)   TRIGGER="${1#*=}" ;;
    -h|--help)     sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "${SAIVA_BACKUP_DIRECT:-}" ] && DIRECT=1
case "$TRIGGER" in
  timer|cli|button) : ;;
  *) printf 'Unknown --trigger: %s (expected timer, cli or button)\n' "$TRIGGER" >&2; exit 2 ;;
esac

case "$ACTION" in
  run)         cmd_run ;;
  verify-only) cmd_verify_only ;;
  status)      cmd_status ;;
  list)        exec "$SCRIPT_DIR/restore-backup.sh" ;;
  self-check)  cmd_self_check ;;
esac
