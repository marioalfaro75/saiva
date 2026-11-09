#!/usr/bin/env bash
# Restore the household database from one of the dumps in the db_backups volume.
#
# Three kinds of dump land there and this script restores any of them without caring
# which: `-nightly` (scripts/backup.sh, on a timer, and the one you normally want),
# `-premigrate` (the api, just before it migrates) and `-prerestore` (this script, just
# before it replaces a database). If the dump you want is the off-box COPY of a nightly
# one rather than the volume's, put it back in the volume first -- there is a one-liner
# for that in docs/BACKUPS.md -- and then run this.
#
# The README's "Rolling back" section ends by telling the owner to "restore the
# pre-migration dump from the db_backups volume" and then stops. That sentence is the one
# instruction in the whole document that cannot be worked out from first principles at
# the moment it is needed, because four things about this deployment are counter-intuitive
# and each of them breaks the command you would reach for first.
#
# 1. THE DUMPS ARE PLAIN SQL, GZIPPED -- NOT pg_dump ARCHIVES.
#    backend/app/dbinit.py builds its pg_dump argv with no -F/--format, so the format is
#    the default `plain`, and the Python gzips the result afterwards. The file is
#    /backups/saiva-<UTC stamp>-<kind>.sql.gz. So the restore is `gunzip -c | psql`.
#    pg_restore cannot read these at all: it reads archives, and answers "input file
#    appears to be a text format dump, please use psql". Reaching for pg_restore is the
#    first wrong turn, and it is the one every other Postgres guide sends you down.
#
# 2. THE DUMPS ARE ONLY REACHABLE FROM A CONTAINER, AND NOT FROM THE OBVIOUS ONE.
#    docker-compose.prod.yml mounts db_backups into exactly one service: api. The
#    scenario that sends anyone here is the scenario where api will not start -- a bad
#    release, a migration to undo, a crash loop. So `docker compose exec api ...` does not
#    work precisely when it is needed. Everything below runs in a throwaway container
#    built from postgres:16-alpine, the image the db service already uses, so it is
#    already on the host and no registry access is required.
#
# 3. A PLAIN DUMP REPLAYED OVER AN EXISTING SCHEMA FAILS ON EVERY SINGLE CREATE.
#    The dump has no DROPs in it (dbinit passes no --clean), so replaying it into a
#    database that still has its tables produces one "already exists" error per object.
#    With ON_ERROR_STOP that is an immediate abort; without it, the CREATEs all fail, the
#    COPYs all succeed, and the result is the old rows duplicated on top of the new ones.
#    Worse, replaying on top would leave in place every table a migration NEWER than the
#    dump had added, so the schema would be a mixture of two eras while alembic_version
#    claimed the older one. Dropping and recreating the database is the only way to
#    actually arrive at the state the dump describes. That is what this script does, and
#    it is why the confirmation is a typed word rather than a y/n.
#
# 4. A RESTORE IS ITSELF A DESTRUCTIVE WRITE, SO IT GETS ITS OWN BACKUP FIRST.
#    Restoring the wrong dump, or restoring the right one a day later than intended, is
#    exactly as expensive as the failure being recovered from. Before anything is dropped,
#    the CURRENT state is dumped to /backups/saiva-<now>-prerestore.sql.gz in the same
#    format, under a name this script lists and can restore. A mistaken restore is
#    therefore reversible with one more run of this script.
#
# Usage:
#   scripts/restore-backup.sh                      list the dumps, newest first
#   scripts/restore-backup.sh <dump-name>          restore that dump (asks first)
#   scripts/restore-backup.sh --help
#
# Options and environment:
#   --direct                 run psql/pg_dump straight off PATH against PGHOST/PGPORT
#   SAIVA_RESTORE_DIRECT=1   instead of inside a container. For a host that has the
#                            Postgres client tools and reaches the database directly, and
#                            for scripts/tests/test-restore-backup.sh, which exercises the
#                            whole restore against a real Postgres with no Docker at all.
#   SAIVA_BACKUP_DIR         where the dumps are, in --direct mode (default /backups --
#                            the same variable dbinit.py writes them to).
#   SAIVA_CONFIRM_RESTORE    set to the confirmation word to restore without a terminal.
#   SAIVA_PROJECT            compose project name (default saiva), for the volume and
#                            network names.
#   SAIVA_RESTORE_SKIP_SAFETY_DUMP=1
#   --no-safety-dump         skip point 4. Only sensible when the disk is full, which is
#                            itself one of the reasons to be here.
#   POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
#                            read from .env if not already in the environment, which is
#                            the same precedence docker compose applies. Getting that
#                            backwards would connect as a different user than the stack
#                            does and restore into a different database than the one that
#                            is broken.
#
# The listing goes to stdout; everything else -- the warning, the prompt, the progress and
# the verification report -- goes to stderr, so `restore-backup.sh > dumps.txt` captures
# the list and nothing else.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SAIVA_ENV_FILE:-$ROOT/.env}"

# Everything that reaches Postgres or the dumps: the container runners, the settings
# loading, the duration/size formatting, the stamp in a dump's name, and the preflights.
# scripts/backup.sh stands on the same file, which is the point -- a nightly dump and a
# restore that disagreed about which network to join, or which .env key wins, would each
# work perfectly in isolation. It needs ROOT and ENV_FILE (set above) and the log/blank/die
# defined below.
# shellcheck source=lib/pg.sh
. "$ROOT/scripts/lib/pg.sh"

# The word that has to be typed. Not y/n, for the same reason scripts/confirm-destroy.sh
# is not y/n: a y/n prompt in front of a destructive action gets answered reflexively, and
# this one runs at the end of an evening that has already gone wrong.
CONFIRM_WORD="restore-saiva-db"

# --- how far along we are, so every exit path can say what state the database is in -----

PHASE="startup"
SAFETY_DUMP=""        # final path of the pre-restore dump, once it exists
SAFETY_DUMP_TMP=""    # the partial file it is written to first
DB=""                 # filled in by load_settings
DB_META=""            # "encoding|collate|ctype|owner" of DB, filled in by read_db_meta
DB_EXISTED=0          # 1 if DB was on the server when this run started, 0 if it was not

log()  { printf '%s\n' "$*" >&2; }
blank() { printf '\n' >&2; }

die() { log "  $*"; exit 1; }

# Printed by the EXIT trap below. Split out so the wording for each phase sits next to the
# phase it describes rather than inside a case statement nobody reads under pressure.
state_report() {
  case "$PHASE" in
    startup|preflight|inspect|pre-drop|list)
      log "  Nothing was changed. The database is exactly as it was."
      ;;
    confirm)
      log "  Aborted. Nothing was changed; the database is exactly as it was."
      ;;
    safety-dump)
      log "  Nothing was changed: the safety dump failed, so the restore was never"
      log "  started. The database is exactly as it was."
      if [ -n "$SAFETY_DUMP_TMP" ]; then
        log "  A partial file may be left at ${SAFETY_DUMP_TMP}. It is NOT a usable"
        log "  backup -- it is named so that it cannot be mistaken for one."
      fi
      ;;
    drop)
      # This fires only while the DROP itself is in flight, which is why it describes what
      # to check instead of asserting what happened: a drop that is REFUSED changes
      # nothing, and a drop interrupted part way can leave the database present but marked
      # invalid. Claiming the data was gone here, as this used to, sends someone looking
      # for a recovery they do not need.
      log "  STOP AND READ THIS."
      log "  Dropping database \"${DB}\" did not report success, so what is there now has"
      log "  to be checked rather than assumed. A refused drop leaves the data intact; an"
      log "  interrupted one can leave the database in place but marked invalid, which"
      log "  answers every connection with \"cannot connect to invalid database\" until it"
      log "  is dropped again. Either way nothing has been loaded in: no dump has"
      log "  overwritten anything."
      log ""
      log "  What is actually there is listed by:"
      log ""
      if [ "$DIRECT" -eq 1 ]; then
        log "      psql --host ${PGHOST_EFF} --port ${PGPORT_EFF} --username ${PGUSER_EFF} -l"
      else
        log "      docker compose -f docker-compose.prod.yml exec db psql -U ${PGUSER_EFF} -l"
      fi
      recovery_hint
      ;;
    create)
      # The state line comes second and fits on one line on purpose. Whoever reads this is
      # reading that line and nothing else until they have calmed down.
      log "  STOP AND READ THIS."
      log "  Database \"${DB}\" does NOT hold the data it held before this script ran."
      log "  It was dropped, and creating it again failed, so it is missing altogether and"
      log "  nothing has been loaded into it."
      recovery_hint
      ;;
    create-fresh)
      # Nothing was lost, and saying so plainly is the whole point of this phase existing:
      # it is only reachable when the database was already gone before this script ran.
      log "  Nothing was lost. Database \"${DB}\" did not exist when this script started,"
      log "  so nothing was dropped and nothing was overwritten. Creating it failed, so it"
      log "  is still missing and the dump was not loaded."
      log "  Whatever removed it -- a wiped db_data volume, usually -- has to be dealt with"
      log "  first: the database server has to be up, and \"${PGUSER_EFF}\" has to be"
      log "  allowed to CREATE DATABASE."
      ;;
    restore)
      log "  STOP AND READ THIS."
      if [ "$DB_EXISTED" -eq 1 ]; then
        log "  Database \"${DB}\" does NOT hold the data it held before this script ran."
        log "  It was dropped and recreated, and then the dump failed to finish loading."
      else
        log "  Database \"${DB}\" did not exist when this script started, so nothing was"
        log "  lost -- but nothing was restored either: it was created empty and then the"
        log "  dump failed to finish loading."
      fi
      log "  The load runs as a single transaction, so an error rolls the whole dump back:"
      log "  the database almost certainly exists and is completely EMPTY."
      recovery_hint
      ;;
    restore-partial)
      # Deliberately not the report above. --single-transaction does not cover this case:
      # psql commits whatever it has been given when its INPUT ENDS, and a dump stream
      # that stops part way is an end of input, not an error. So psql exits 0 having
      # committed half a ledger. Reporting that as "completely EMPTY" would be the worst
      # wrong answer this script could give -- it describes a database that needs
      # restoring as one that has already been dealt with.
      log "  STOP AND READ THIS."
      log "  Database \"${DB}\" may hold PART of ${DUMP_NAME} -- not all of it, and not"
      log "  nothing. The dump stopped being readable part way through, and psql commits"
      log "  what it has when its input ends, so it reported success on an incomplete"
      log "  load. Do not start the api against this database and do not treat it as"
      log "  restored: load a dump that reads all the way through, or put back the state"
      log "  this run replaced."
      recovery_hint
      ;;
    verify-unreachable)
      log "  The dump loaded and committed, but \"${DB}\" could not be queried afterwards,"
      log "  so none of the checks got an answer -- the error is above. The database holds"
      log "  whatever that dump contained; nothing here has managed to look at it."
      recovery_hint
      ;;
    verify-empty)
      log "  The dump loaded without error and left database \"${DB}\" with no tables at"
      log "  all. That is not a restore of anything: whatever that file holds, it is not a"
      log "  dump of this household's schema. Do not start the api against it."
      recovery_hint
      ;;
    verify)
      log "  The dump loaded, but the checks afterwards did not all pass -- the lines"
      log "  above say which. Database \"${DB}\" holds whatever that dump contained and"
      log "  nothing else. Do not start the api against it until the mismatch is"
      log "  understood."
      recovery_hint
      ;;
    *)
      log "  Unexpected failure in phase '${PHASE}'. Check the database by hand before"
      log "  starting the api."
      ;;
  esac
}

recovery_hint() {
  blank
  if [ -n "$SAFETY_DUMP" ]; then
    log "  The state this script replaced was saved first. To put it back:"
    log ""
    log "      $0 $(basename -- "$SAFETY_DUMP")"
  else
    log "  No safety dump was taken for this run, so there is nothing to put back"
    log "  automatically. The older dumps are still listed by:"
    log ""
    log "      $0"
  fi
  blank
}

on_exit() {
  local rc=$?
  [ "$rc" -eq 0 ] && return 0
  blank
  state_report
  exit "$rc"
}
trap on_exit EXIT

# The database name is interpolated into DROP DATABASE / CREATE DATABASE, which take an
# identifier and not a parameter, so it is checked rather than escaped. Anything outside
# this set is refused instead of guessed at: a name that needs quoting is exactly the case
# where a hand-written restore is safer than this script's.
#
# Checked from the restore path only, deliberately, and not from load_settings. Listing
# the dumps is the one thing here that has to work on a box where everything else is
# wrong, and it does not touch the database at all, so it has no business refusing over a
# name it will never use.
require_usable_db_name() {
  if ! [[ "$DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    die "POSTGRES_DB is \"${DB}\", which needs SQL quoting this script will not attempt.
  Restore it by hand, or rename the database first."
  fi
}

# --- listing ----------------------------------------------------------------------------

# One line per directory entry matching *.sql.gz:
#
#     dump <size> <name>     a readable regular file; size in bytes
#     skip <why> <name>      anything else -- a subdirectory, a dangling symlink, a file
#                            this user cannot read
#
# A name is always the last field, and the reader on the other side takes it with the last
# variable of a `read -r kind size name`, so a name with a space in it arrives whole.
#
# One entry that cannot be read is skipped and reported, not treated as a failure of the
# whole listing. It used to be the latter, and the cost was out of all proportion: a
# single unreadable file -- a half-finished `docker cp`, a directory someone created by
# mistake, a dump restored from a tarball with the wrong owner -- ended the listing AND
# every restore, including of the good dumps sitting right beside it. A backups directory
# with something wrong in it is the normal case at the moment this script gets run, not an
# exceptional one.
#
# Size comes from `wc -c` rather than `stat -c %s`, and the age is worked out on this side
# from the stamp in the name, so the only commands this needs inside the container are
# `cd`, `wc` and a `for` loop. Alpine's busybox is a different build from the host's
# coreutils and the one path that absolutely has to work on a broken box is this one, so
# it leans on as little of either as possible.
#
# Reading the age out of the NAME rather than the file's mtime is also the more correct
# answer: the name is what pg_dump was told to call the dump when it was taken, and it
# survives being copied out of the volume and back, which an mtime does not.
list_raw() {
  backups_sh '
    cd "$1" 2>/dev/null || exit 9
    found=0
    for f in *.sql.gz; do
      # -e is false for a dangling symlink, which is still an entry someone will go
      # looking for, so -L is asked as well. Both false means the glob matched nothing
      # and "$f" is the pattern itself.
      [ -e "$f" ] || [ -L "$f" ] || continue
      found=1
      if [ -d "$f" ]; then
        printf "skip directory %s\n" "$f"
      elif [ ! -f "$f" ]; then
        printf "skip not-a-file %s\n" "$f"
      elif [ ! -r "$f" ]; then
        printf "skip unreadable %s\n" "$f"
      else
        # The redirect is what reads the file, so this catches what -r cannot: a file on
        # a failing disk, or one whose permissions changed between the test and the read.
        size=$(wc -c < "$f" 2>/dev/null) || size=""
        case "$size" in
          "" | *[!0-9]*) printf "skip unreadable %s\n" "$f" ;;
          *)             printf "dump %s %s\n" "$size" "$f" ;;
        esac
      fi
    done
    [ "$found" -eq 1 ] || exit 8
  ' "$BACKUP_MOUNT"
}

# One phrase per reason code list_raw can emit, so the listing and a refused restore
# describe the same entry in the same words.
skip_reason() {
  case "$1" in
    directory)  printf 'a directory, not a file' ;;
    unreadable) printf 'cannot be read -- check its owner and permissions' ;;
    *)          printf 'not a regular file (a dangling symlink, or a device)' ;;
  esac
}

# The entries list_raw stepped over, named out loud. Each one is either the dump someone
# is about to go looking for, or the reason the list is shorter than they expected; both
# are worth a line. On stderr with the rest of the commentary, so the listing on stdout
# stays a list of restorable dumps and nothing else.
report_skipped() {
  local lines="$1" why name
  [ -n "$lines" ] || return 0
  log "  Skipped, and NOT restorable:"
  blank
  while read -r why name; do
    [ -n "${name:-}" ] || continue
    log "    ${name}"
    log "      $(skip_reason "$why")"
  done < <(printf '%s' "$lines")
  blank
}

cmd_list() {
  PHASE="list"
  docker_preflight 0

  local raw rc=0
  raw="$(list_raw)" || rc=$?

  if [ "$rc" -eq 9 ]; then
    die "There is no directory ${BACKUP_MOUNT} to read dumps from.
  In --direct mode set SAIVA_BACKUP_DIR to where they actually are."
  fi
  if [ "$rc" -eq 8 ]; then
    log "  No dumps found: there is no *.sql.gz in ${SOURCE_LABEL}."
    blank
    log "  Two things write dumps here and neither has. scripts/backup.sh takes a nightly"
    log "  one, so an empty directory means the timer is not installed or has never run --"
    log "  check \`./scripts/backup.sh --self-check\`. The api writes one before a migration,"
    log "  but only to a database that is BEHIND it, so a box that has never been upgraded"
    log "  has never written one."
    blank
    log "  If an off-box copy is configured, THAT is where to look next: the Settings page"
    log "  and \`./scripts/backup.sh --status\` both name the destination. Otherwise the data"
    log "  in the db_data volume is all there is."
    blank
    PHASE="done"
    exit 0
  fi
  if [ "$rc" -ne 0 ]; then
    die "Could not read the dumps out of ${SOURCE_LABEL} (exit ${rc})."
  fi

  # Stamped into "<epoch> <size> <name>" here so the sort is a numeric sort on a real
  # timestamp. Dumps with no stamp get epoch 0 and fall to the bottom, where an unlabelled
  # file belongs -- rather than wherever its name happens to sort.
  local kind size name epoch stamped="" skipped="" dumps=0 skips=0
  while read -r kind size name; do
    [ -n "${name:-}" ] || continue
    case "$kind" in
      dump)
        stamped+="$(epoch_from_name "$name") ${size} ${name}"$'\n'
        dumps=$(( dumps + 1 ))
        ;;
      skip)
        skipped+="${size} ${name}"$'\n'
        skips=$(( skips + 1 ))
        ;;
    esac
  done < <(printf '%s\n' "$raw")

  # Entries matching *.sql.gz but not one of them readable. Distinct from the empty
  # directory above, and the files have to be named: "no dumps" would send someone looking
  # for files that are sitting right there.
  if [ "$dumps" -eq 0 ]; then
    report_skipped "$skipped"
    die "Nothing in ${SOURCE_LABEL} can be read as a dump: every entry matching *.sql.gz
  is listed above with the reason it was skipped (${skips} in total). A permissions
  problem on the host is the usual cause."
  fi

  local now; now="$(date -u +%s)"
  log "  Dumps in ${SOURCE_LABEL}, newest first:"
  blank

  # The name gets its own line: it is the thing that has to be typed back, and it is too
  # long to share one with the details inside 80 columns.
  local taken newest=1 marker
  while read -r epoch size name; do
    [ -n "${name:-}" ] || continue
    marker=""
    if [ "$newest" -eq 1 ]; then marker="   <- newest"; newest=0; fi
    taken="$(stamp_from_name "$name")"
    if [ -n "$taken" ] && [ "$epoch" -gt 0 ]; then
      printf '    %s%s\n' "$name" "$marker"
      printf '      %s, taken %s (%s)\n\n' \
        "$(human_size "$size")" "$taken" "$(human_age $(( now - epoch )))"
    else
      printf '    %s%s\n' "$name" "$marker"
      printf '      %s, taken at an unknown time -- the name carries no stamp\n\n' \
        "$(human_size "$size")"
    fi
  done < <(printf '%s' "$stamped" | sort -k1,1nr -k3,3)

  report_skipped "$skipped"

  log "  nightly      written by scripts/backup.sh on a timer, after proving it restores."
  log "  premigrate   written by the api container just before it migrated the schema."
  log "  prerestore   written by this script just before it replaced the database."
  blank
  log "  To restore one, pass its name:"
  log ""
  log "      $0 <dump-name>"
  blank
  PHASE="done"
}

# --- the revision the dump came from ----------------------------------------------------
#
# "What does alembic_version say in the dump" is read out of the dump itself rather than
# guessed from its date, because it is the only thing that makes the check after the
# restore meaningful: without it, verification could only confirm that *some* revision is
# present, which is true of any database at all.
#
# This decompresses the whole file, which is also a full integrity check -- gunzip verifies
# the CRC and length at the end of the stream, so a 0 exit here proves the archive is
# intact and there is no separate `gzip -t` pass. awk reads to EOF rather than exiting at
# the match on purpose: an early exit would hand gunzip a SIGPIPE, and under `set -o
# pipefail` an intact dump would then look like a corrupt one.
revision_in_dump() {
  local name="$1" rev scratch
  local -a rcs
  # The output goes through a temp file rather than being captured with $( ), because the
  # pipeline would then run inside a command substitution's subshell and PIPESTATUS in
  # this shell would describe the assignment, not the pipeline -- which is to say gunzip's
  # exit status, the one thing being checked for here, would be invisible.
  scratch="$(mktemp)"
  set +e
  dump_stream "$name" | awk -v q="'" '
    in_copy {
      if ($0 == "\\.") { in_copy = 0; next }
      if (rev == "") rev = $1
      next
    }
    index($0, "COPY public.alembic_version ") == 1 || index($0, "COPY alembic_version ") == 1 {
      in_copy = 1; next
    }
    index($0, "INSERT INTO public.alembic_version") == 1 || index($0, "INSERT INTO alembic_version") == 1 {
      if (rev == "") { n = split($0, p, q); if (n >= 2) rev = p[2] }
      next
    }
    END { print rev }
  ' >"$scratch"
  rcs=("${PIPESTATUS[@]}")
  set -e
  rev="$(cat "$scratch")"
  rm -f "$scratch"
  if [ "${rcs[0]}" -ne 0 ]; then
    die "Could not read ${name}: gunzip exited ${rcs[0]}.
  Either the file is not there, or it is truncated or corrupt. A truncated dump cannot be
  restored even partially by this script, and that is on purpose -- see the note about
  --single-transaction below. Nothing was changed.
  'scripts/restore-backup.sh' with no arguments lists what is readable."
  fi
  [ "${rcs[1]}" -eq 0 ] || die "Could not scan ${name} for its schema revision (awk exited ${rcs[1]})."
  printf '%s' "$rev"
}

# --- what is on the server right now ----------------------------------------------------

# Read before anything else happens, and separately from the drop, for two reasons.
#
# The database may not be there at all. "The db_data volume is gone and a dump is all that
# is left" is the first case this script's header names, and it used to be the one case
# that could not be restored: the safety dump ran first, pg_dump of a database that does
# not exist fails, and a failed safety dump stops the restore -- so the branch below that
# creates a missing database was unreachable on the normal path. Asking the question here
# is what makes that case work.
#
# And the encoding, collation and owner have to be captured BEFORE the drop, because
# dropping the database is also how that information is lost. A database recreated from
# template1 instead inherits whatever encoding and collation template1 happens to have,
# which on a host set up in a different locale is not what the dump's data was written
# with.
#
# Its own phase, because a failure to READ pg_database has changed nothing. Reporting it
# under the drop's phase -- which is what happened while this SELECT lived inside
# recreate_database -- told the owner their database had been dropped and emptied at the
# one moment that claim was both false and unverifiable by them.
read_db_meta() {
  PHASE="inspect"
  DB_META="$(psql_on "$MAINT_DB" "select pg_encoding_to_char(encoding) || '|' || datcollate || '|' || datctype || '|' || pg_get_userbyid(datdba) from pg_database where datname = '${DB}'")" \
    || die "Could not read database \"${DB}\"'s encoding, collation and owner out of
  pg_database -- the error is above. That read is the first thing this does and nothing
  has been attempted after it."
  if [ -n "$DB_META" ]; then
    DB_EXISTED=1
  else
    DB_EXISTED=0
  fi
}

# --- the safety dump --------------------------------------------------------------------

take_safety_dump() {
  PHASE="safety-dump"

  # "There is nothing to back up" is a reason to carry on; "there IS something and I could
  # not back it up" is a reason to stop, and that is the branch below this one. Collapsing
  # the two -- which is what happened while this ran before the database was looked at --
  # turned the headline scenario into an abort.
  if [ "$DB_EXISTED" -eq 0 ]; then
    log "  No safety dump: database \"${DB}\" does not exist, so there is no current state"
    log "  to save and nothing this restore can overwrite."
    blank
    return 0
  fi

  if [ -n "${SAIVA_RESTORE_SKIP_SAFETY_DUMP:-}" ] || [ "$SKIP_SAFETY" -eq 1 ]; then
    log "  Skipping the safety dump because it was asked for explicitly. If this restore"
    log "  is the wrong one, the state being replaced is gone."
    blank
    return 0
  fi

  local stamp base final tmp rc=0 attempt=1
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  base="${BACKUP_MOUNT}/saiva-${stamp}-prerestore"

  log "  Dumping the CURRENT state first, so this restore is reversible:"

  # The loop over suffixes is not decoration. The stamp has one-second resolution, and the
  # sequence this whole feature exists for -- restore the wrong dump, realise, restore the
  # safety dump -- is two runs seconds apart, which landed in the SAME second often enough
  # to show up as a flapping test while the script was otherwise behaving. Overwriting
  # there is the worst outcome this script could produce: the second run would write a
  # safety dump of the BROKEN database over the safety dump of the good one, under the
  # exact name the first run had just told the owner to restore, and would then restore
  # that. So nothing here overwrites anything; a taken name is answered by taking the next
  # one.
  while : ; do
    if [ "$attempt" -eq 1 ]; then
      final="${base}.sql.gz"
      tmp="${BACKUP_MOUNT}/.saiva-${stamp}-prerestore.inprogress"
    else
      final="${base}-${attempt}.sql.gz"
      tmp="${BACKUP_MOUNT}/.saiva-${stamp}-prerestore-${attempt}.inprogress"
    fi
    SAFETY_DUMP_TMP="$tmp"

    # Three steps rather than `pg_dump | gzip > file`, because that pipeline reports
    # gzip's exit status and hides pg_dump's -- it writes a perfectly valid gzip of a
    # failed dump. The chain below carries a distinct status per step. The partial file is
    # written under a dot-name that does not end in .sql.gz, so an interrupted safety dump
    # can never be listed, or chosen, as though it were a usable backup.
    rc=0
    pg_sh '
      [ -e "$6" ] && exit 74
      [ -e "$5" ] && exit 74
      pg_dump --host "$1" --port "$2" --username "$3" --dbname "$4" \
              --no-owner --no-privileges --file "$5" || exit 71
      gzip "$5" || exit 72
      mv "$5.gz" "$6" || exit 73
    ' "$PGHOST_EFF" "$PGPORT_EFF" "$PGUSER_EFF" "$DB" "$tmp" "$final" || rc=$?

    [ "$rc" -eq 74 ] || break
    attempt=$(( attempt + 1 ))
    if [ "$attempt" -gt 50 ]; then
      die "Could not find an unused name for the safety dump: ${base}.sql.gz and 49
  numbered variants of it all exist already. The restore was not attempted."
    fi
  done

  case "$rc" in
    0) ;;
    71) die "pg_dump of the current database failed, so there is no way back from this
  restore and it will not be attempted." ;;
    72) die "The safety dump was written but could not be compressed, so it is not in the
  format this script restores. The restore was not attempted." ;;
    73) die "The safety dump could not be moved into place. The restore was not attempted." ;;
    *)  die "The safety dump failed (exit ${rc}). The restore was not attempted." ;;
  esac

  log "      ${final}"

  SAFETY_DUMP="$final"
  SAFETY_DUMP_TMP=""

  # Prove it is readable before trusting it with the only copy of the current state. A
  # backup that has not been read is a hope, and this is the one moment where checking is
  # free compared with what it protects.
  local check_rc=0
  dump_stream "$(basename -- "$final")" >/dev/null || check_rc=$?
  [ "$check_rc" -eq 0 ] || die "The safety dump was written but cannot be decompressed (exit ${check_rc}).
  Refusing to destroy the current database on the strength of a backup that does not read.
  The restore was not attempted."

  log "  Safety dump written and verified readable."
  blank
}

# --- the restore ------------------------------------------------------------------------

recreate_database() {
  # Nothing in here has changed anything yet, and the phase has to say so -- otherwise the
  # safety dump's report, which claims the safety dump failed, is still the one on its way
  # out.
  PHASE="pre-drop"

  local enc coll ctype owner clause=""

  if [ "$DB_EXISTED" -eq 1 ]; then
    IFS='|' read -r enc coll ctype owner <<<"$DB_META"
    clause="WITH OWNER = \"${owner}\" ENCODING = '${enc}' LC_COLLATE = '${coll}' LC_CTYPE = '${ctype}' TEMPLATE = template0"
    log "  Dropping and recreating database \"${DB}\" (${enc}, ${coll}, owner ${owner})."
    # WITH (FORCE) terminates the other sessions itself, which matters because the api
    # container restarts on a loop in exactly this situation and will reconnect between a
    # pg_terminate_backend and a plain DROP. Postgres 13 and later only.
    #
    # The phase moves one statement at a time from here on. Each of these two statements
    # leaves the database in a different state, and the report for one is not true of the
    # other.
    PHASE="drop"
    psql_on "$MAINT_DB" "DROP DATABASE \"${DB}\" WITH (FORCE)" >/dev/null
    PHASE="create"
  else
    clause="WITH OWNER = \"${PGUSER_EFF}\" TEMPLATE = template0"
    log "  Database \"${DB}\" does not exist, so there is nothing to drop; creating it."
    PHASE="create-fresh"
  fi

  psql_on "$MAINT_DB" "CREATE DATABASE \"${DB}\" ${clause}" >/dev/null
}

load_dump() {
  PHASE="restore"
  log "  Loading ${DUMP_NAME} into \"${DB}\"..."

  # --single-transaction is what makes a failed restore safe to reason about: the dump
  # either lands completely or not at all, so there is no half-restored ledger to tell
  # apart from a whole one. Plain dumps of a single database contain no statement that
  # refuses to run inside a transaction (no CREATE DATABASE, no CONCURRENTLY, no VACUUM),
  # which is the condition that makes this possible at all.
  # -o /dev/null discards the result rows of the SELECTs the dump uses for setval() and
  # set_config(); errors still come back on stderr.
  # --single-transaction requires -c or -f, so the stream is fed in as `-f -` rather than
  # left to psql's default stdin handling, where the flag would be rejected.
  local -a rcs
  set +e
  dump_stream "$DUMP_NAME" | pg_sh_stdin '
    exec psql -X -q -o /dev/null -v ON_ERROR_STOP=1 --single-transaction \
      --host "$1" --port "$2" --username "$3" --dbname "$4" -f -
  ' "$PGHOST_EFF" "$PGPORT_EFF" "$PGUSER_EFF" "$DB"
  rcs=("${PIPESTATUS[@]}")
  set -e

  if [ "${rcs[1]}" -ne 0 ]; then
    log "  psql exited ${rcs[1]} while loading the dump; the error is above."
    exit 1
  fi
  if [ "${rcs[0]}" -ne 0 ]; then
    # Its own phase: psql did NOT fail here, so --single-transaction rolled nothing back.
    # It was handed a truncated stream, treated the end of it as the end of its input, and
    # committed. See the restore-partial report.
    PHASE="restore-partial"
    log "  The dump could not be read all the way through (gunzip exited ${rcs[0]}),"
    log "  even though it read cleanly a moment ago."
    exit 1
  fi
  log "  Loaded."
  blank
}

verify_restore() {
  # Three phases rather than one, because the states they describe are different and the
  # owner acts on them differently: the checks could not be RUN, the dump left nothing at
  # all, or a check failed. The first query is what tells the first two apart from the
  # third.
  PHASE="verify-unreachable"
  log "  Checking the restored database:"

  local tables marker rev problem=0

  tables="$(psql_on "$DB" "select count(*) from information_schema.tables where table_schema = 'public'")"
  PHASE="verify"
  printf '    %-28s %s\n' "tables in public schema" "$tables" >&2
  if [ "${tables:-0}" -lt 1 ]; then
    printf '    %-28s %s\n' "" "-> EMPTY. The dump put no tables here." >&2
    # Reported under its own phase. The generic report says the database holds what the
    # dump contained, which here would be read as "it holds something" two lines under a
    # check that just said it holds nothing.
    PHASE="verify-empty"
    problem=1
  fi

  # `households` is the table dbinit.py itself uses to tell a real Saiva database from an
  # empty one, so it is the right marker to assert on here: if dbinit would not recognise
  # the result as a Saiva database, neither should this.
  marker="$(psql_on "$DB" "select to_regclass('public.households') is not null")"
  if [ "$marker" = "t" ]; then
    printf '    %-28s %s\n' "households table" "present" >&2
  else
    printf '    %-28s %s\n' "households table" "MISSING" >&2
    problem=1
  fi

  rev="$(psql_on "$DB" "select coalesce(string_agg(version_num, ','), '') from alembic_version" 2>/dev/null || true)"
  if [ -n "$DUMP_REVISION" ]; then
    if [ "$rev" = "$DUMP_REVISION" ]; then
      printf '    %-28s %s\n' "alembic_version" "${rev}  (the dump says ${DUMP_REVISION} -- match)" >&2
    else
      printf '    %-28s %s\n' "alembic_version" "${rev:-<empty>}  (the dump says ${DUMP_REVISION} -- MISMATCH)" >&2
      problem=1
    fi
  else
    printf '    %-28s %s\n' "alembic_version" "${rev:-<empty>}" >&2
    printf '    %-28s %s\n' "" "The dump carries no alembic_version row, so there is" >&2
    printf '    %-28s %s\n' "" "nothing to compare it against. A dump that old predates" >&2
    printf '    %-28s %s\n' "" "Alembic; the api adopts the baseline on its next start." >&2
  fi

  blank
  [ "$problem" -eq 0 ] || exit 1
}

# --- confirmation -----------------------------------------------------------------------

describe_restore() {
  local taken age_secs now
  now="$(date -u +%s)"
  taken="$(stamp_from_name "$DUMP_NAME")"
  age_secs=0
  [ "$DUMP_EPOCH" -gt 0 ] && age_secs=$(( now - DUMP_EPOCH ))

  blank
  log "  RESTORE ${DUMP_NAME}"
  blank
  printf '    %-16s %s\n' "Taken"        "${taken:-unknown (the name carries no stamp)}" >&2
  if [ "$DUMP_EPOCH" -gt 0 ]; then
    printf '    %-16s %s\n' "Age"        "$(human_age "$age_secs")" >&2
  fi
  printf '    %-16s %s\n' "Size"         "$(human_size "$DUMP_SIZE") compressed" >&2
  local into="database \"${DB}\""
  [ "$DB_EXISTED" -eq 1 ] || into="${into} (which does not exist yet)"
  printf '    %-16s %s\n' "Into"         "${into} as ${PGUSER_EFF}@${PGHOST_EFF}:${PGPORT_EFF}" >&2
  printf '    %-16s %s\n' "Schema era"   "${DUMP_REVISION:+alembic revision }${DUMP_REVISION:-no alembic_version row in the dump}" >&2
  blank
  if [ "$DB_EXISTED" -eq 0 ]; then
    # The paragraph below would be a lie here, and a frightening one: there is nothing to
    # lose, because there is nothing there. This is the one case in this script that
    # cannot cost anything, and whoever is reading at this hour should be told that in the
    # same place they would otherwise be warned.
    log "  There is no database \"${DB}\" on this server, so this restore REPLACES NOTHING."
    log "  It creates the database and loads the dump into it. Nothing can be lost, and"
    log "  there is no current state to take a safety dump of."
  else
    log "  This REPLACES the household's database. \"${DB}\" is dropped and rebuilt from"
    log "  the dump, so EVERYTHING WRITTEN SINCE ${taken:-THE DUMP WAS TAKEN} IS LOST:"
    log "  every transaction imported, every category assigned, every budget edited, every"
    if [ "$DUMP_EPOCH" -gt 0 ]; then
      log "  bill marked paid, every login created in the last $(human_span "$age_secs")."
    else
      # No stamp means no duration can be named, and inventing one from the file's mtime
      # would be worse than saying nothing: an mtime changes when a dump is copied about.
      log "  bill marked paid, every login created since then. How long ago that was cannot"
      log "  be read off this file -- check the dump before answering the prompt."
    fi
    blank
    if [ "$SKIP_SAFETY" -eq 1 ] || [ -n "${SAIVA_RESTORE_SKIP_SAFETY_DUMP:-}" ]; then
      log "  The safety dump is switched off for this run, so the state being replaced will"
      log "  NOT be saved anywhere. This is irreversible."
    else
      log "  Before anything is dropped, the current state is dumped to"
      log "  ${BACKUP_MOUNT}/saiva-<now>-prerestore.sql.gz, so if this is the wrong dump the"
      log "  mistake is undone by running this script again with that name."
    fi
  fi
  blank
}

confirm() {
  PHASE="confirm"
  describe_restore

  # Checked before the terminal test so that a rehearsal, or a recovery run over a flaky
  # link with no pty, does not also need a terminal. It carries the word rather than a 1
  # or a yes so that it cannot end up set by habit in a shell profile next to the other
  # force flags.
  if [ -n "${SAIVA_CONFIRM_RESTORE:-}" ]; then
    if [ "$SAIVA_CONFIRM_RESTORE" = "$CONFIRM_WORD" ]; then
      log "  SAIVA_CONFIRM_RESTORE=${CONFIRM_WORD} -- proceeding without a prompt."
      blank
      return 0
    fi
    die "SAIVA_CONFIRM_RESTORE is set but does not say ${CONFIRM_WORD}, so it is not a
  confirmation."
  fi

  # No terminal means nobody read the paragraph above, so there is nobody to confirm.
  # Without this, `restore-backup.sh <name> </dev/null` -- or this script called from
  # another one -- would sail through on an EOF and drop the database.
  if [ ! -t 0 ]; then
    log "  Refusing: stdin is not a terminal, so there is nobody here to confirm."
    die "To mean it without one, set SAIVA_CONFIRM_RESTORE=${CONFIRM_WORD}"
  fi

  printf '  Type %s to proceed (anything else aborts): ' "$CONFIRM_WORD" >&2
  local reply
  if ! IFS= read -r reply; then
    log ""
    die "No answer (end of input)."
  fi
  # Exact compare, no trimming and no case folding: the point of a typed word is that it
  # cannot be arrived at by accident.
  [ "$reply" = "$CONFIRM_WORD" ] || die "That is not ${CONFIRM_WORD}."
  blank
}

# --- restore command --------------------------------------------------------------------

cmd_restore() {
  PHASE="preflight"
  require_usable_db_name
  docker_preflight 1

  case "$DUMP_NAME" in
    */*) die "Pass just the file name, not a path: $(basename -- "$DUMP_NAME")" ;;
  esac
  if ! [[ "$DUMP_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    die "\"${DUMP_NAME}\" is not a plausible dump name. Names are restricted to letters,
  digits, dot, dash and underscore -- partly because anything else is not a name this
  deployment writes, and partly because the name is passed to a shell inside the restore
  container. 'scripts/restore-backup.sh' with no arguments lists the real ones."
  fi
  case "$DUMP_NAME" in
    *.sql.gz) ;;
    *) die "\"${DUMP_NAME}\" does not end in .sql.gz, so it is not one of the dumps this
  script restores. An uncompressed .sql dump goes in directly with
  'psql --single-transaction -v ON_ERROR_STOP=1 -f <file>' against an empty database." ;;
  esac

  # Find it in the listing rather than stat-ing it, so that "no such dump" and "that name
  # is a directory" and "the volume is empty" all arrive as one clear message, and so the
  # size and mtime shown in the warning are the ones actually on disk.
  local raw kind size name rc=0 unreadable=""
  raw="$(list_raw)" || rc=$?
  [ "$rc" -eq 0 ] || die "No readable dumps in ${SOURCE_LABEL} (exit ${rc}), so there is nothing named
  ${DUMP_NAME} to restore. Run '$0' with no arguments."

  DUMP_SIZE=""
  while read -r kind size name; do
    [ "$name" = "$DUMP_NAME" ] || continue
    case "$kind" in
      dump) DUMP_SIZE="$size" ;;
      skip) unreadable="$size" ;;
    esac
  done < <(printf '%s\n' "$raw")
  DUMP_EPOCH="$(epoch_from_name "$DUMP_NAME")"

  # The name is there but the entry is not a dump this script can read. Said separately
  # from "no such dump", because the two are fixed differently and the file being present
  # is the part that would otherwise be baffling.
  [ -z "$unreadable" ] || die "${DUMP_NAME} is in ${SOURCE_LABEL}, but it is $(skip_reason "$unreadable").
  '$0' with no arguments lists it under \"Skipped\", along with the dumps that CAN be
  restored."

  [ -n "$DUMP_SIZE" ] || die "There is no dump called ${DUMP_NAME} in ${SOURCE_LABEL}.
  Run '$0' with no arguments for the list -- the names are long and easy to mistype."
  [ "$DUMP_SIZE" -gt 0 ] || die "${DUMP_NAME} is zero bytes. Something interrupted the dump that wrote it;
  it holds nothing and restoring it would empty the database. Nothing was changed."

  log "  Reading ${DUMP_NAME} ($(human_size "$DUMP_SIZE") compressed)..."
  DUMP_REVISION="$(revision_in_dump "$DUMP_NAME")"

  db_preflight
  # Before the confirmation, not after: whether the database is there decides both what
  # the warning says and whether there is anything to take a safety dump of.
  read_db_meta
  confirm
  take_safety_dump
  recreate_database
  load_dump
  verify_restore

  PHASE="done"
  local taken; taken="$(stamp_from_name "$DUMP_NAME")"
  log "  Restore complete. Database \"${DB}\" now holds the state from"
  log "  ${taken:-${DUMP_NAME}}."
  blank
  if [ -n "$SAFETY_DUMP" ]; then
    log "  The state that was replaced is in:"
    log "      ${SAFETY_DUMP}"
    log "  If this was the wrong dump, put it back with:"
    log "      $0 $(basename -- "$SAFETY_DUMP")"
    blank
  fi
  log "  ONE MORE STEP BEFORE STARTING THE API. The api container migrates on start, and"
  log "  it does not know a restore just happened: against a database it considers BEHIND"
  log "  it will take its own premigrate dump and then migrate this one straight back up,"
  log "  undoing the restore. Pin SAIVA_VERSION in ${ENV_FILE} to the build that matches"
  log "  this schema FIRST, and bring the stack up without pulling:"
  log ""
  log "      docker compose -f docker-compose.prod.yml up -d"
  log ""
  log "  Not 'make pull': that pulls first, and a pull of a local-only rollback tag fails"
  log "  and takes the whole command down with it. An image OLDER than the restored schema"
  log "  is safe -- backend/app/schema_state.py makes that case serve and warn rather than"
  log "  crash-loop."
  blank
}

# --- arguments --------------------------------------------------------------------------

usage() {
  sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

SKIP_SAFETY=0
DUMP_NAME=""
DUMP_SIZE=""
DUMP_EPOCH=0
DUMP_REVISION=""
WANT_LIST=0

if [ -n "${SAIVA_RESTORE_DIRECT:-}" ]; then
  DIRECT=1
fi

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)        usage; exit 0 ;;
    -l|--list)        WANT_LIST=1 ;;
    --direct)         DIRECT=1 ;;
    --no-safety-dump) SKIP_SAFETY=1 ;;
    --)               shift; break ;;
    -*)               die "Unknown option: $1  (try --help)" ;;
    *)
      [ -z "$DUMP_NAME" ] || die "Restore one dump at a time. Got \"$DUMP_NAME\" and \"$1\"."
      DUMP_NAME="$1"
      ;;
  esac
  shift
done
if [ $# -gt 0 ]; then
  [ -z "$DUMP_NAME" ] || die "Restore one dump at a time."
  DUMP_NAME="$1"; shift
  [ $# -eq 0 ] || die "Restore one dump at a time."
fi

load_settings

if [ "$WANT_LIST" -eq 1 ] || [ -z "$DUMP_NAME" ]; then
  cmd_list
else
  cmd_restore
fi

PHASE="done"
exit 0
