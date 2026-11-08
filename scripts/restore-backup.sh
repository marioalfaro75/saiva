#!/usr/bin/env bash
# Restore the household database from one of the dumps in the db_backups volume.
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
#    /backups/saiva-<UTC stamp>-premigrate.sql.gz. So the restore is `gunzip -c | psql`.
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

PROJECT="${SAIVA_PROJECT:-saiva}"
VOLUME="${PROJECT}_db_backups"
NETWORK="${PROJECT}_default"

# Deliberately the db service's own image rather than a generic alpine: it is already
# pulled on any host that has ever started this stack, so nothing here needs a registry,
# and its psql/pg_dump are version 16, matching the server exactly. A client older than
# the server can fail to dump at all; a client newer is merely untested.
PG_IMAGE="${SAIVA_PG_IMAGE:-postgres:16-alpine}"

# The word that has to be typed. Not y/n, for the same reason scripts/confirm-destroy.sh
# is not y/n: a y/n prompt in front of a destructive action gets answered reflexively, and
# this one runs at the end of an evening that has already gone wrong.
CONFIRM_WORD="restore-saiva-db"

# --- how far along we are, so every exit path can say what state the database is in -----

PHASE="startup"
SAFETY_DUMP=""        # final path of the pre-restore dump, once it exists
SAFETY_DUMP_TMP=""    # the partial file it is written to first
DB=""                 # filled in by load_settings

log()  { printf '%s\n' "$*" >&2; }
blank() { printf '\n' >&2; }

die() { log "  $*"; exit 1; }

# Printed by the EXIT trap below. Split out so the wording for each phase sits next to the
# phase it describes rather than inside a case statement nobody reads under pressure.
state_report() {
  case "$PHASE" in
    startup|preflight|list)
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
    recreate)
      # The state line comes first and fits on one line on purpose. Whoever reads this is
      # reading the first line and nothing else until they have calmed down.
      log "  STOP AND READ THIS."
      log "  Database \"${DB}\" does NOT hold the data it held before this script ran."
      log "  The failure happened while dropping and recreating it, so it is now missing"
      log "  or empty, and nothing has been loaded into it."
      recovery_hint
      ;;
    restore)
      log "  STOP AND READ THIS."
      log "  Database \"${DB}\" does NOT hold the data it held before this script ran."
      log "  It was dropped and recreated, and then the dump failed to finish loading."
      log "  The load runs as a single transaction, so an error rolls the whole dump back:"
      log "  the database almost certainly exists and is completely EMPTY."
      recovery_hint
      ;;
    verify)
      log "  The dump loaded, but the checks afterwards did not pass -- read the lines"
      log "  above for which one. Database \"${DB}\" is NOT empty; it holds whatever the"
      log "  dump contained. Do not start the api against it until the mismatch is"
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

# --- running psql and pg_dump, in a container or not ------------------------------------
#
# Every command that touches Postgres or the dumps goes through one of the three runners
# below, and they are the only places in this file that know Docker exists. That is what
# makes --direct mode a real code path rather than a second implementation: the test
# exercises the same lines production does, with the runner swapped underneath.
#
# The snippets are passed to `sh -c` with their values as POSITIONAL ARGUMENTS, never
# interpolated into the string. A password with a space in it, or a dollar sign, is then
# just an argument, and there is no quoting to get wrong at the one moment nobody can
# afford to debug quoting.

DIRECT=0
BACKUP_MOUNT="/backups"

_pg_sh() {
  local iflag="$1" script="$2"
  shift 2
  if [ "$DIRECT" -eq 1 ]; then
    sh -c "$script" sh "$@"
    return
  fi
  # -e PGPASSWORD with no value passes it in from our own environment instead of putting
  # it on the command line, where `ps` and the shell history would both have it.
  docker run --rm ${iflag:+-i} \
    -e PGPASSWORD \
    --network "$NETWORK" \
    -v "${VOLUME}:${BACKUP_MOUNT}" \
    "$PG_IMAGE" sh -c "$script" sh "$@"
}

# Reaches the database and the dumps.
pg_sh()       { _pg_sh "" "$@"; }
# The same, with stdin attached, for feeding a dump into psql.
pg_sh_stdin() { _pg_sh "-i" "$@"; }

# Reaches the dumps ONLY -- no --network. That is not tidiness: `docker compose down`
# removes the saiva_default network, so joining it would make `restore-backup.sh` with no
# arguments fail with "network not found" on a stopped stack. Listing the dumps has to
# work when nothing is running, because deciding which dump to restore comes before
# starting anything.
backups_sh() {
  local script="$1"
  shift
  if [ "$DIRECT" -eq 1 ]; then
    sh -c "$script" sh "$@"
    return
  fi
  docker run --rm -v "${VOLUME}:${BACKUP_MOUNT}" "$PG_IMAGE" sh -c "$script" sh "$@"
}

# One query, one line of output on stdout.
psql_on() {
  local dbname="$1" sql="$2"
  # -X so a ~/.psqlrc on the host cannot switch on aligned output and break the parsing
  # below; -At for one bare value; ON_ERROR_STOP so a failed query is a failed command.
  pg_sh 'exec psql -X -q -At -v ON_ERROR_STOP=1 --host "$1" --port "$2" --username "$3" --dbname "$4" -c "$5"' \
    "$PGHOST_EFF" "$PGPORT_EFF" "$PGUSER_EFF" "$dbname" "$sql"
}

dump_stream() { backups_sh 'exec gunzip -c "$1/$2"' "$BACKUP_MOUNT" "$1"; }

# --- settings ---------------------------------------------------------------------------

# The last assignment of KEY in .env, with one layer of surrounding quotes removed.
# Deliberately does NOT strip a trailing `# comment`: a password containing a # is far
# more likely in this file than an inline comment after one, and silently truncating a
# password at a # would present as "password authentication failed" with no hint why.
env_value() {
  local key="$1" line
  [ -f "$ENV_FILE" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1)"
  [ -n "$line" ] || return 0
  line="${line#*=}"
  case "$line" in
    \"*\") line="${line#\"}"; line="${line%\"}" ;;
    \'*\') line="${line#\'}"; line="${line%\'}" ;;
  esac
  printf '%s' "$line"
}

load_settings() {
  # Shell environment first, then .env, then the same defaults docker-compose.prod.yml
  # applies with ${POSTGRES_USER:-saiva}. This is the order docker compose itself uses;
  # reversing it would quietly connect as a different user than the stack does.
  DB="${POSTGRES_DB:-$(env_value POSTGRES_DB)}";             DB="${DB:-saiva}"
  PGUSER_EFF="${POSTGRES_USER:-$(env_value POSTGRES_USER)}"; PGUSER_EFF="${PGUSER_EFF:-saiva}"
  PGPASSWORD="${POSTGRES_PASSWORD:-$(env_value POSTGRES_PASSWORD)}"
  PGPASSWORD="${PGPASSWORD:-saiva}"
  export PGPASSWORD

  if [ "$DIRECT" -eq 1 ]; then
    PGHOST_EFF="${PGHOST:-localhost}"
    PGPORT_EFF="${PGPORT:-5432}"
    BACKUP_MOUNT="${SAIVA_BACKUP_DIR:-/backups}"
    # Messages say where the dumps actually are. Naming the Docker volume in --direct
    # mode would send someone looking inside a volume that has nothing to do with the
    # files they are reading.
    SOURCE_LABEL="$BACKUP_MOUNT"
  else
    # Inside saiva_default the db service answers to its own name on the default port.
    PGHOST_EFF="db"
    PGPORT_EFF="5432"
    BACKUP_MOUNT="/backups"
    SOURCE_LABEL="volume ${VOLUME}"
  fi

  # DROP DATABASE cannot be issued from inside the database being dropped, so it is sent
  # over a connection to another one. `postgres` always exists in the official image;
  # template1 is the fallback for the unusual case of POSTGRES_DB=postgres.
  # Written as an if rather than `[ x = y ] && MAINT_DB=...` because this is the last
  # statement in the function: an AND-list whose test is false returns 1, which under
  # `set -e` makes the *call* to load_settings a failed command and exits the script
  # before it has done anything. That is not a hypothetical -- it is how this function
  # was first written, and it made every run abort with "Nothing was changed".
  MAINT_DB="${SAIVA_RESTORE_MAINTENANCE_DB:-postgres}"
  if [ "$MAINT_DB" = "$DB" ]; then
    MAINT_DB="template1"
  fi
}

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

# --- formatting -------------------------------------------------------------------------

human_size() {
  local b="$1"
  if   [ "$b" -lt 1024 ]       ; then printf '%d B' "$b"
  elif [ "$b" -lt 1048576 ]    ; then printf '%d KB' $(( (b + 512) / 1024 ))
  elif [ "$b" -lt 1073741824 ] ; then awk -v b="$b" 'BEGIN { printf "%.1f MB", b / 1048576 }'
  else                                awk -v b="$b" 'BEGIN { printf "%.2f GB", b / 1073741824 }'
  fi
}

# "7 hours", for the middle of a sentence. human_age's "7 hours ago" does not fit there,
# and "everything written in the last 7 hours ago" is the kind of sentence that makes a
# tired reader stop trusting the rest of the warning.
human_span() {
  local s="$1"
  if   [ "$s" -lt 90 ]    ; then printf 'minute or so'
  elif [ "$s" -lt 5400 ]  ; then printf '%d minutes' $(( (s + 30) / 60 ))
  elif [ "$s" -lt 172800 ]; then printf '%d hours' $(( (s + 1800) / 3600 ))
  else                           printf '%d days' $(( (s + 43200) / 86400 ))
  fi
}

human_age() {
  local s="$1"
  if   [ "$s" -lt -60 ]  ; then printf 'dated in the future -- check the host clock'
  elif [ "$s" -lt 90 ]   ; then printf 'just now'
  elif [ "$s" -lt 5400 ] ; then printf '%d minutes ago' $(( (s + 30) / 60 ))
  elif [ "$s" -lt 172800 ]; then printf '%d hours ago' $(( (s + 1800) / 3600 ))
  else                          printf '%d days ago' $(( (s + 43200) / 86400 ))
  fi
}

# saiva-20261108-034512-premigrate.sql.gz -> 2026-11-08 03:45:12 UTC
# The stamp in the name is the authority for when a dump was TAKEN; a file's mtime
# survives a `docker cp` or a volume copy with a different value, and the name does not.
stamp_from_name() {
  local n="$1" d t
  if [[ "$n" =~ ^saiva-([0-9]{8})-([0-9]{6})- ]]; then
    d="${BASH_REMATCH[1]}"; t="${BASH_REMATCH[2]}"
    printf '%s-%s-%s %s:%s:%s UTC' \
      "${d:0:4}" "${d:4:2}" "${d:6:2}" "${t:0:2}" "${t:2:2}" "${t:4:2}"
  fi
}

# Seconds since the epoch for the stamp in a dump's name, or 0 when the name carries no
# stamp. 0 then means "age unknown" everywhere downstream, which is treated as a reason to
# say so rather than to print a nonsense duration.
epoch_from_name() {
  local n="$1" d t
  if [[ "$n" =~ ^saiva-([0-9]{8})-([0-9]{6})- ]]; then
    d="${BASH_REMATCH[1]}"; t="${BASH_REMATCH[2]}"
    date -u -d "${d:0:4}-${d:4:2}-${d:6:2} ${t:0:2}:${t:2:2}:${t:4:2} UTC" +%s 2>/dev/null \
      || printf '0'
  else
    printf '0'
  fi
}

# --- preflight --------------------------------------------------------------------------

docker_preflight() {
  local need_network="$1"
  [ "$DIRECT" -eq 1 ] && return 0

  command -v docker >/dev/null 2>&1 \
    || die "Docker is not on PATH. With the Postgres client tools installed instead, run
  this with --direct and point PGHOST/PGPORT/SAIVA_BACKUP_DIR at the database and dumps."

  docker version --format '{{.Server.Version}}' >/dev/null 2>&1 \
    || die "The Docker daemon is not answering. Nothing can be read or restored until it
  is: try 'systemctl start docker'."

  docker volume inspect "$VOLUME" >/dev/null 2>&1 || die "There is no Docker volume called ${VOLUME}.
  That is where the dumps live. If the compose project was renamed, set SAIVA_PROJECT;
  'docker volume ls' shows what does exist. Note that a 'docker run -v ${VOLUME}:...'
  would have CREATED an empty one and reported no dumps, which is why this is checked
  first rather than discovered as an empty list."

  docker image inspect "$PG_IMAGE" >/dev/null 2>&1 || log "  Note: ${PG_IMAGE} is not on this host, so Docker will try to pull it, which
  needs working network and registry access. It is the db service's own image, so
  'docker compose -f docker-compose.prod.yml up -d db' pulls it too."

  if [ "$need_network" -eq 1 ]; then
    docker network inspect "$NETWORK" >/dev/null 2>&1 || die "There is no Docker network called ${NETWORK}, which means the stack is down --
  'docker compose down' removes it. The database has to be running to restore into it.
  Start just the database and nothing else:

      docker compose -f docker-compose.prod.yml up -d db

  then run this again. Listing the dumps works either way."
  fi
}

db_preflight() {
  local out
  if ! out="$(psql_on "$MAINT_DB" 'select 1' 2>&1)"; then
    log "  Cannot reach the database to restore into."
    log ""
    printf '%s\n' "$out" | sed 's/^/      /' >&2
    log ""
    if [ "$DIRECT" -eq 1 ]; then
      die "Checked ${PGUSER_EFF}@${PGHOST_EFF}:${PGPORT_EFF}, database \"${MAINT_DB}\"."
    fi
    die "Checked user \"${PGUSER_EFF}\" against the db service on ${NETWORK}. If that says
  authentication failed, POSTGRES_PASSWORD in ${ENV_FILE} no longer matches the password
  the db volume was initialised with -- Postgres keeps the first one and ignores later
  changes to the variable. If it says the host is unknown, the db container is not up:
  'docker compose -f docker-compose.prod.yml up -d db'."
  fi
}

# --- listing ----------------------------------------------------------------------------

# "<size> <name>" per dump. Size comes from `wc -c` rather than `stat -c %s`, and the age
# is worked out on this side from the stamp in the name, so the only commands this needs
# inside the container are `cd`, `wc` and a `for` loop. Alpine's busybox is a different
# build from the host's coreutils and the one path that absolutely has to work on a broken
# box is this one, so it leans on as little of either as possible.
#
# Reading the age out of the NAME rather than the file's mtime is also the more correct
# answer: the name is what pg_dump was told to call the dump when it was taken, and it
# survives being copied out of the volume and back, which an mtime does not.
list_raw() {
  backups_sh '
    cd "$1" 2>/dev/null || exit 9
    found=0
    for f in *.sql.gz; do
      [ -e "$f" ] || continue
      found=1
      size=$(wc -c < "$f") || exit 1
      printf "%s %s\n" "$size" "$f"
    done
    [ "$found" -eq 1 ] || exit 8
  ' "$BACKUP_MOUNT"
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
    log "  The api container writes one automatically, but only when it is about to apply"
    log "  a migration to a database that is BEHIND it -- see backend/app/dbinit.py. A box"
    log "  that has never been upgraded has never written one. There is nothing to restore"
    log "  from here; the data in the db_data volume is all there is."
    blank
    PHASE="done"
    exit 0
  fi
  if [ "$rc" -ne 0 ]; then
    die "Could not read the dumps out of ${SOURCE_LABEL} (exit ${rc})."
  fi

  local now; now="$(date -u +%s)"
  log "  Dumps in ${SOURCE_LABEL}, newest first:"
  blank

  # Stamped into "<epoch> <size> <name>" here so the sort is a numeric sort on a real
  # timestamp. Dumps with no stamp get epoch 0 and fall to the bottom, where an unlabelled
  # file belongs -- rather than wherever its name happens to sort.
  local size name epoch stamped=""
  while read -r size name; do
    [ -n "${name:-}" ] || continue
    stamped+="$(epoch_from_name "$name") ${size} ${name}"$'\n'
  done < <(printf '%s\n' "$raw")

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

# --- the safety dump --------------------------------------------------------------------

take_safety_dump() {
  PHASE="safety-dump"

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
  PHASE="recreate"

  # Captured before the drop, because dropping the database is also how this information
  # is lost. A database recreated from template1 instead inherits whatever encoding and
  # collation template1 happens to have, which on a host set up in a different locale is
  # not what the dump's data was written with.
  local meta enc coll ctype owner clause=""
  meta="$(psql_on "$MAINT_DB" "select pg_encoding_to_char(encoding) || '|' || datcollate || '|' || datctype || '|' || pg_get_userbyid(datdba) from pg_database where datname = '${DB}'")"

  if [ -n "$meta" ]; then
    IFS='|' read -r enc coll ctype owner <<<"$meta"
    clause="WITH OWNER = \"${owner}\" ENCODING = '${enc}' LC_COLLATE = '${coll}' LC_CTYPE = '${ctype}' TEMPLATE = template0"
    log "  Dropping and recreating database \"${DB}\" (${enc}, ${coll}, owner ${owner})."
    # WITH (FORCE) terminates the other sessions itself, which matters because the api
    # container restarts on a loop in exactly this situation and will reconnect between a
    # pg_terminate_backend and a plain DROP. Postgres 13 and later only.
    psql_on "$MAINT_DB" "DROP DATABASE \"${DB}\" WITH (FORCE)" >/dev/null
  else
    clause="WITH OWNER = \"${PGUSER_EFF}\" TEMPLATE = template0"
    log "  Database \"${DB}\" does not exist yet; creating it."
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
    log "  The dump could not be read all the way through (gunzip exited ${rcs[0]}),"
    log "  even though it read cleanly a moment ago."
    exit 1
  fi
  log "  Loaded."
  blank
}

verify_restore() {
  PHASE="verify"
  log "  Checking the restored database:"

  local tables marker rev problem=0

  tables="$(psql_on "$DB" "select count(*) from information_schema.tables where table_schema = 'public'")"
  printf '    %-28s %s\n' "tables in public schema" "$tables" >&2
  if [ "${tables:-0}" -lt 1 ]; then
    printf '    %-28s %s\n' "" "-> EMPTY. The dump put no tables here." >&2
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
  printf '    %-16s %s\n' "Into"         "database \"${DB}\" as ${PGUSER_EFF}@${PGHOST_EFF}:${PGPORT_EFF}" >&2
  printf '    %-16s %s\n' "Schema era"   "${DUMP_REVISION:+alembic revision }${DUMP_REVISION:-no alembic_version row in the dump}" >&2
  blank
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
  local raw size name rc=0
  raw="$(list_raw)" || rc=$?
  [ "$rc" -eq 0 ] || die "No readable dumps in ${SOURCE_LABEL} (exit ${rc}), so there is nothing named
  ${DUMP_NAME} to restore. Run '$0' with no arguments."

  DUMP_SIZE=""
  while read -r size name; do
    if [ "$name" = "$DUMP_NAME" ]; then DUMP_SIZE="$size"; fi
  done < <(printf '%s\n' "$raw")
  DUMP_EPOCH="$(epoch_from_name "$DUMP_NAME")"

  [ -n "$DUMP_SIZE" ] || die "There is no dump called ${DUMP_NAME} in ${SOURCE_LABEL}.
  Run '$0' with no arguments for the list -- the names are long and easy to mistype."
  [ "$DUMP_SIZE" -gt 0 ] || die "${DUMP_NAME} is zero bytes. Something interrupted the dump that wrote it;
  it holds nothing and restoring it would empty the database. Nothing was changed."

  log "  Reading ${DUMP_NAME} ($(human_size "$DUMP_SIZE") compressed)..."
  DUMP_REVISION="$(revision_in_dump "$DUMP_NAME")"

  db_preflight
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
