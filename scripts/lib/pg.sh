# shellcheck shell=bash
# Reaching Postgres and the dumps -- the plumbing scripts/restore-backup.sh and
# scripts/backup.sh both stand on.
#
# This was restore-backup.sh's own, and it is here because the nightly backup needs the
# SAME runners rather than a second set that looks like them. Two copies of a `docker run`
# line is how one of them quietly stops joining the right network, or starts putting a
# password on a command line; and the comments below record decisions that cost something
# to learn, which a reimplementation does not inherit.
#
# WHAT THE CALLER MUST PROVIDE BEFORE SOURCING THIS:
#
#   ROOT        the checkout
#   ENV_FILE    which .env to read settings out of
#
# WHAT THE CALLER MUST DEFINE (as functions, by the time anything here is called):
#
#   log MESSAGE   one line of commentary
#   blank         one empty line
#   die MESSAGE   complain and exit non-zero
#
# That seam is deliberate and not laziness. restore-backup.sh sends commentary to stderr
# so that `restore-backup.sh > dumps.txt` captures the listing and nothing else;
# backup.sh runs under a systemd timer and sends it to stdout AND to the run log the
# status file quotes. Both are right, and a library that picked one would be wrong in the
# other place.
#
# DIRECT is set here to 0 and the caller may set it to 1 afterwards (restore-backup.sh
# does, from --direct or SAIVA_RESTORE_DIRECT). In that mode every runner below swaps its
# `docker run` for a plain `sh -c`, which is what lets the test suites exercise the real
# code path on a host with no Docker daemon.
#
# Those two early returns say `return $?` rather than a bare `return`, because a runner
# must hand back the status of the command it ran -- and a bare `return` does not reliably
# do that: bash sets $? to the pending exit status for the duration of an EXIT trap, so a
# bare `return` inside one hands back whatever killed the script. scripts/lib/state.sh's
# state_dir() carries the full measurement.

# Both names are derived the way compose derives them, from the project name.
# docker-compose.prod.yml pins that with `name: saiva`, so it is not the directory name
# and renaming the checkout does not move the volume; SAIVA_PROJECT is here for the box
# where it was overridden anyway. That file declares no networks of its own, so
# `docker compose -f docker-compose.prod.yml config` reports exactly one -- default, which
# compose creates as <project>_default -- alongside volumes caddy_config, caddy_data,
# db_backups and db_data, and services api, caddy, db and web. <project>_default is
# therefore the network the db service is on, and the only one worth joining.
PROJECT="${SAIVA_PROJECT:-saiva}"
VOLUME="${PROJECT}_db_backups"
NETWORK="${PROJECT}_default"

# Deliberately the db service's own image rather than a generic alpine: it is already
# pulled on any host that has ever started this stack, so nothing here needs a registry,
# and its psql/pg_dump are version 16, matching the server exactly. A client older than
# the server can fail to dump at all; a client newer is merely untested.
PG_IMAGE="${SAIVA_PG_IMAGE:-postgres:16-alpine}"

# --- running psql and pg_dump, in a container or not ------------------------------------
#
# Every command that touches Postgres or the dumps goes through one of the three runners
# below, and they hold the two `docker run` lines that actually run anything -- one in
# _pg_sh, one in backups_sh. (A third mention of `docker run` is inside an error message,
# not a command.) (docker_preflight calls docker as well, but only to inspect: it starts
# nothing.) Funnelling the work through them is what makes --direct mode a real code path
# rather than a second implementation: the test exercises the same lines production does,
# with the runner swapped underneath.
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
    return $?
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
    return $?
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

# Everything this sets is for the SOURCING script to read -- DB, PGUSER_EFF, PGPASSWORD,
# PGHOST_EFF, PGPORT_EFF, BACKUP_MOUNT, SOURCE_LABEL, MAINT_DB -- which is what the
# directive below is about: inside this file some of them look written and never read.
# shellcheck disable=SC2034
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
