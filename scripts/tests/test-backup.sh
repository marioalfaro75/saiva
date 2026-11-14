#!/usr/bin/env bash
# What "verified" has to mean -- tested against a real Postgres, on scripts/backup.sh.
#
# The nightly regime exists to be able to say four things honestly: a dump was taken, it
# was restored into a throwaway database and came back whole, a copy of it reached a
# different filesystem, and the newest verified one is N hours old. The failure worth
# designing against is not a backup that breaks. It is a backup that is broken and
# reported as fine -- so most of this file is dumps that a weaker check would have called
# good, each put through the real code path and each required to come back red.
#
# THE MEASUREMENT THIS SUITE RE-TAKES ON EVERY RUN. `psql --single-transaction -v
# ON_ERROR_STOP=1` EXITS 0 on a dump truncated in the middle of a COPY block: stdin ends
# without the `\.` terminator, psql finishes the COPY with whatever rows it got, and
# reports success. Sections 4b and 4c do not cite that finding from a document, they
# measure it -- restoring the bad dump by hand with the weak check and asserting it comes
# back 0 -- and then require backup.sh to fail the same dump. Without those two
# assertions, "the suite caught it" would not distinguish a verification that works from a
# dump that happened to be unloadable.
#
# No Docker. backup.sh's --direct mode (SAIVA_BACKUP_DIRECT=1) swaps the runners in
# scripts/lib/pg.sh that every Postgres command goes through for a plain `sh -c`, exactly
# as restore-backup.sh's does. So the dump, the throwaway-database restore, the row-count
# comparison, the off-box copy, the prune and the status record below are all the same code
# the systemd timer runs, with psql and pg_dump taken off PATH instead of out of a
# container. What --direct does NOT cover is the two `docker run` command lines themselves;
# scripts/tests/test-restore-backup.sh section 1b guards the names inside them, and there
# is no second copy of them to guard here because both scripts stand on the same library.
#
# Point it at a Postgres with PGHOST/PGPORT/PGUSER. The defaults are the instance this
# repository's sandbox runs. Exits 77 if there is no Postgres to talk to, so a runner can
# tell "cannot run here" from "the backup is broken".
#
# ONE COPY AT A TIME against one server. The scratch databases are named for this process,
# but two concurrent runs still interfere: each cleanup drops every `saiva_verify_%`
# database it finds, including the one the other run is restoring into. CI runs this once;
# a developer running it beside another copy will see verifications fail for reasons that
# have nothing to do with the code. (scripts/backup.sh's own orphan sweep is guarded
# against this -- it only reclaims a scratch database whose pid is dead or whose stamp is
# over an hour old -- but this suite's cleanup is deliberately blunter, because a suite
# that leaves rubbish behind when it fails is harder to re-run than one that does not.)

# errexit is on, with the same known cost test-restore-backup.sh states: when a section
# fails in a way the assertions do not catch -- a bare command returning non-zero -- the
# suite stops there instead of finishing, so a real regression costs the diagnosis the
# later sections would have given. Left as is for the same reason: the sections build on
# one database and one record that carries forward between them, and the failure mode is
# "less detail on a run that is already red", not a wrong answer.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP="$ROOT/scripts/backup.sh"

export PGHOST="${PGHOST:-/var/tmp}"
export PGPORT="${PGPORT:-55432}"
export PGUSER="${PGUSER:-saiva}"

TESTDB="saiva_backup_test_$$"
# Named apart from the saiva_verify_% family on purpose: section 4 asserts that NO database
# matching that pattern is left behind, and a scratch database of this suite's own that
# collided with it would make that assertion pass or fail for the wrong reason.
WEAKDB="saiva_backup_weakcheck_$$"
WORK="$(mktemp -d)"
BACKUPS="$WORK/backups"
STATE="$WORK/state"
OUTSIDE="$WORK/outside"
mkdir -p "$BACKUPS" "$STATE" "$OUTSIDE"

# The name restore-backup.sh's stamp_from_name / epoch_from_name actually match. A nightly
# dump has to be restorable by the already-rehearsed procedure with no change to it, which
# is the single most important design constraint on the whole regime, so the name this
# script writes is asserted against the pattern that other script reads.
NIGHTLY_RE='^saiva-[0-9]{8}-[0-9]{6}-nightly(-[0-9]+)?\.sql\.gz$'
RESTORABLE_RE='^saiva-[0-9]{8}-[0-9]{6}-'

PASS=0
FAILED=0

ok()   { printf '  ok    %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
note() { printf '        %s\n' "$*"; }

assert_eq() { # assert_eq <label> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1 = $3"; else bad "$1: expected [$2], got [$3]"; fi
}

cleanup() {
  psql -d postgres -qc "DROP DATABASE IF EXISTS \"$TESTDB\" WITH (FORCE)" >/dev/null 2>&1 || true
  psql -d postgres -qc "DROP DATABASE IF EXISTS \"$WEAKDB\" WITH (FORCE)" >/dev/null 2>&1 || true
  # A throwaway verification database surviving this suite would be a copy of the test
  # ledger left lying about, and would also poison the next run's section-4 assertion.
  # Dropped here as well as asserted on, because a suite that leaves rubbish behind when it
  # fails is harder to re-run than one that does not.
  #
  # But only the ones NOBODY IS USING, by the same test scripts/backup.sh's own orphan
  # sweep applies: the trailing pid is not a live process, or the stamp is over an hour
  # old. This used to drop every saiva_verify_% database it could see, and the cost showed
  # up as soon as two copies of this suite ran against one Postgres server -- each one's
  # cleanup dropped the other's scratch database mid-restore, and the failures landed in
  # whichever section happened to coincide. They read exactly like real verification
  # failures, which cost an hour of looking for a defect that was not there.
  local leftover pid stamp cutoff
  cutoff="$(date -u -d '1 hour ago' +%Y%m%d%H%M%S 2>/dev/null || printf '0')"
  while read -r leftover; do
    [ -n "$leftover" ] || continue
    pid="${leftover##*_}"
    stamp="${leftover#saiva_verify_}"; stamp="${stamp%_*}"
    case "$pid" in ''|*[!0-9]*) pid=1 ;; esac
    if kill -0 "$pid" 2>/dev/null && [ "$stamp" \> "$cutoff" ]; then continue; fi
    psql -d postgres -qc "DROP DATABASE IF EXISTS \"$leftover\" WITH (FORCE)" >/dev/null 2>&1 || true
  done < <(psql -X -At -q -d postgres \
             -c "select datname from pg_database where datname like 'saiva\\_verify\\_%'" 2>/dev/null || true)
  chmod -R u+rwX "$WORK" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

q() { psql -X -At -q -d "$TESTDB" -c "$1"; }
meta() { psql -X -At -q -d postgres -c "$1"; }
meta_rows() { psql -X -At -q -d postgres -c "$1" 2>/dev/null || true; }

# When this suite started, as a dump-name-shaped stamp. Used to tell a scratch
# verification database this suite left behind from one belonging to another copy of the
# suite running against the same server -- see verify_dbs.
SUITE_STARTED_STAMP="$(date -u +%Y%m%d%H%M%S)"

# Section 11 needs a file that only root can change, and the whole point of that section is
# that backup.sh looks at the OWNER. On a developer box this suite is usually root already;
# on a GitHub runner it is not, but passwordless sudo is there, and without it that section
# would silently skip in CI -- which is where the privilege boundary most needs a test.
# `sudo -n` so a box with an interactive sudo never hangs waiting for a password.
as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; return $?; fi
  command -v sudo >/dev/null 2>&1 || return 1
  sudo -n true 2>/dev/null || return 1
  sudo -n "$@"
}

# The throwaway databases backup.sh creates to rehearse a restore. Exactly the query the
# task names, wildcards and all: `_` is a LIKE wildcard, so this is a superset of the real
# family, which is the cautious direction for an assertion that something is NOT there.
# Scratch verification databases that COULD have been created by this suite's own runs:
# every backup.sh this suite starts is a child of this process, and the pid is the tail of
# the name. Counting every saiva_verify_% on the server instead made this assertion fail
# whenever another copy of the suite had one in flight -- a failure that reads exactly like
# a verification that did not clean up after itself.
verify_dbs() {
  local n=0 db pid
  while read -r db; do
    [ -n "$db" ] || continue
    pid="${db##*_}"
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    # A live process whose parent is this suite. `ps -o ppid=` is enough: the only thing
    # starting backup.sh here is this script.
    if [ "$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')" = "$$" ]; then n=$((n + 1)); continue; fi
    # Not running any more. If its stamp is from this suite's lifetime it is ours and it
    # was left behind; older than that, it belongs to somebody else's run.
    local stamp="${db#saiva_verify_}"; stamp="${stamp%_*}"
    [ "$stamp" \> "$SUITE_STARTED_STAMP" ] && n=$((n + 1))
  done < <(meta_rows "select datname from pg_database where datname like 'saiva\\_verify\\_%'")
  printf '%s' "$n"
}
live_db_exists() { meta "select count(*) from pg_database where datname = '$TESTDB'"; }

# backup.sh in --direct mode, pointed at the scratch database, a scratch dumps directory
# and a scratch state directory, with an env file and an agent.conf that do not exist -- so
# that neither a developer's real .env nor a real /etc/saiva/agent.conf can reach in and
# change which database this suite dumps or where it copies the dump to. COPY_TO and
# COPY_CMD are set to the empty string rather than left unset, because backup.sh reads them
# with `${VAR-default}`: unset means "ask agent.conf", empty means "there is no
# destination", and only the second is a property of this test.
run_backup() {
  SAIVA_BACKUP_DIRECT=1 \
  SAIVA_ENV_FILE="$WORK/no-such.env" \
  SAIVA_AGENT_CONF="$WORK/no-such.conf" \
  SAIVA_STATE_DIR="$STATE" \
  SAIVA_BACKUP_DIR="$BACKUPS" \
  POSTGRES_DB="$TESTDB" \
  POSTGRES_USER="$PGUSER" \
  SAIVA_BACKUP_COPY_TO="" \
  SAIVA_BACKUP_COPY_CMD="" \
  "$BACKUP" "$@"
}

# The same, against a database of the caller's choosing. run_backup pins POSTGRES_DB to
# the scratch ledger, which is right for almost every case here -- but section 18a is
# specifically about what happens when POSTGRES_DB names the WRONG database, and a prefixed
# assignment cannot win against the one inside run_backup.
run_backup_as() { # DBNAME [ARG...]
  local db="$1"; shift
  SAIVA_BACKUP_DIRECT=1 \
  SAIVA_ENV_FILE="$WORK/no-such.env" \
  SAIVA_AGENT_CONF="$WORK/no-such.conf" \
  SAIVA_STATE_DIR="$STATE" \
  SAIVA_BACKUP_DIR="$BACKUPS" \
  POSTGRES_DB="$db" \
  POSTGRES_USER="$PGUSER" \
  SAIVA_BACKUP_COPY_TO="" \
  SAIVA_BACKUP_COPY_CMD="" \
  "$BACKUP" "$@"
}

# The same, with the off-box destination or command under the caller's control.
run_backup_to() { # DEST CMD [ARG...]
  local dest="$1" cmd="$2"; shift 2
  SAIVA_BACKUP_DIRECT=1 \
  SAIVA_ENV_FILE="$WORK/no-such.env" \
  SAIVA_AGENT_CONF="$WORK/no-such.conf" \
  SAIVA_STATE_DIR="$STATE" \
  SAIVA_BACKUP_DIR="$BACKUPS" \
  POSTGRES_DB="$TESTDB" \
  POSTGRES_USER="$PGUSER" \
  SAIVA_BACKUP_COPY_TO="$dest" \
  SAIVA_BACKUP_COPY_CMD="$cmd" \
  "$BACKUP" "$@"
}

# The same, but with every SAIVA_BACKUP_* override REMOVED from the environment, so that
# $WORK/agent.conf is the only thing that can supply them. Section 11 needs that: the
# environment wins over the conf file by design, so a test that left those variables set
# would prove nothing about whether the conf was read.
run_backup_conf_only() {
  env -u SAIVA_BACKUP_COPY_TO -u SAIVA_BACKUP_COPY_CMD -u SAIVA_BACKUP_KEEP_NIGHTLY \
    SAIVA_BACKUP_DIRECT=1 \
    SAIVA_ENV_FILE="$WORK/no-such.env" \
    SAIVA_AGENT_CONF="$WORK/agent.conf" \
    SAIVA_STATE_DIR="$STATE" \
    SAIVA_BACKUP_DIR="$BACKUPS" \
    POSTGRES_DB="$TESTDB" \
    POSTGRES_USER="$PGUSER" \
    PGHOST="$PGHOST" PGPORT="$PGPORT" PGUSER="$PGUSER" \
    "$BACKUP" "$@"
}

# Every read of the record goes through python3's JSON parser and never through grep, and
# that is item 12 of this suite rather than a convenience. scripts/lib/state.sh renders the
# status file by hand -- there is no jq on a household box -- so the escaping is code that
# can be wrong, and one stray byte in one value makes the whole file unparseable. A panel
# reading an unparseable status file shows nothing at all, which is the one outcome worse
# than showing bad news. So the parse happens after every single run below, and section 10
# constructs a value carrying a quote, a backslash, a control byte and a newline on purpose.
read_status() { # LABEL
  if python3 - "$STATE/backup-status.json" >"$WORK/status.fields" 2>"$WORK/status.err" <<'PY'
import json, sys

with open(sys.argv[1]) as handle:
    payload = json.load(handle)
for key, value in payload.items():
    if isinstance(value, list):
        value = " | ".join(str(item) for item in value)
    if value is None:
        value = "<null>"
    # Flattened for the line-oriented cache only. The exact bytes of a value are checked
    # against the JSON itself in section 10, not through this.
    print("%s=%s" % (key, str(value).replace("\n", " ").replace("\t", " ")))
PY
  then
    ok "$1: the record is valid JSON"
  else
    bad "$1: backup-status.json does not parse as JSON"
    sed 's/^/        /' "$WORK/status.err"
    : > "$WORK/status.fields"
  fi
}

sf() { # FIELD -> its value, or <null> for a JSON null, or nothing if the field is absent
  sed -n "s/^$1=//p" "$WORK/status.fields" | tail -n 1
}

# Everything in the dumps directory that restore-backup.sh's listing would offer, which is
# the `*.sql.gz` glob. A half-written dump must never appear in this.
listed_dumps() { ( cd "$BACKUPS" && ls -1 ./*.sql.gz 2>/dev/null | sed 's|^\./||' | sort ) || true; }
nightly_dumps() { listed_dumps | grep -E "$NIGHTLY_RE" || true; }
clear_backups() { find "$BACKUPS" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null || true; }

# Plant a dump the way scripts/backup.sh writes one: owned by root, mode 0600. The prune
# considers ONLY files with both of those properties -- see section 19a for the reason --
# so a fixture that leaves the default mode is not standing in for a previous night's
# dump, it is standing in for a file something else put there.
plant_nightly() { # NAME [CONTENT]
  printf '%s\n' "${2:-planted dump $1}" | gzip -c >"$BACKUPS/$1"
  chmod 0600 "$BACKUPS/$1"
}

# The three things that must be true after every run, successful or not. The live database
# is the household's ledger: a verification that reaches it, or a throwaway copy of it left
# behind, is worse than no verification at all.
assert_nothing_else_touched() { # LABEL
  assert_eq "$1: no throwaway verification database is left behind" "0" "$(verify_dbs)"
  assert_eq "$1: the live database still exists"                    "1" "$(live_db_exists)"
  assert_eq "$1: the live ledger is untouched"      "$LIVE_ROWS" "$(q 'select count(*) from transactions')"
}

printf '\nbackup.sh against %s:%s\n\n' "$PGHOST" "$PGPORT"

# --- 0. is there a Postgres at all ------------------------------------------------------

if ! psql -X -At -q -d postgres -c 'select 1' >/dev/null 2>&1; then
  printf '  cannot reach Postgres as %s at %s:%s -- nothing to test against.\n' \
    "$PGUSER" "$PGHOST" "$PGPORT"
  printf '  Set PGHOST/PGPORT/PGUSER to a Postgres 16 this user can create databases on.\n\n'
  exit 77
fi

PG_DUMP_REAL="$(command -v pg_dump)"

# --- 1. the contract this suite and three other layers code against --------------------
#
# Four things that are decisions rather than details, and would break silently.

if grep -q '^\. "\$SCRIPT_DIR/lib/pg.sh"$' "$BACKUP"; then
  ok "backup.sh sources scripts/lib/pg.sh, so --direct below is the real code path"
else
  bad "backup.sh no longer sources scripts/lib/pg.sh"
  note "if it grew its own container runners, --direct stops exercising what production runs"
  note "and every behavioural assertion in this file quietly starts testing a second path"
fi

# The off-box vocabulary. `ok` used to be in it, as the `${worst:-ok}` default, which meant
# a code path that forgot to say what happened said "copied off this box" -- the exact
# shape of false reassurance the regime exists to remove. Six states, and the only one that
# may read as good news is `remote`.
if grep -q '^SAIVA_OFFSITE_STATES=(' "$BACKUP"; then
  OFFSITE_VOCAB="$(sed -n 's/^SAIVA_OFFSITE_STATES=(\(.*\))$/\1/p' "$BACKUP")"
  assert_eq "the off-box vocabulary is the six states the API and the panel share" \
    "remote unknown same-host same-filesystem failed off" "$OFFSITE_VOCAB"
  case " $OFFSITE_VOCAB " in
    *" ok "*) bad "'ok' is back in the off-box vocabulary; it is the reassuring default this regime removed" ;;
    *)        ok  "and 'ok' is not one of them" ;;
  esac
else
  bad "backup.sh no longer declares SAIVA_OFFSITE_STATES"
  note "frontend/src/components/BackupsPanel.tsx carries a sentence per state, so a state"
  note "that is renamed here and not there renders as whatever the panel's fallback is"
fi

# No public counterpart, and the guard is on the Caddyfile rather than on the compose file:
# caddy already mounts the whole state directory read-only, so what keeps the backup record
# private is that Caddy serves exactly ONE rewritten path out of it. A new `handle` block
# is what would change that, and this goes red on the commit that adds one.
CADDYFILE="$ROOT/infra/Caddyfile"
assert_eq "exactly one Caddy handle has its root in the state directory" "1" \
  "$(grep -c 'root \* /srv/saiva-update' "$CADDYFILE" || true)"
assert_eq "and the one file it rewrites to is the UPDATE status" "1" \
  "$(grep -c 'rewrite \* /status-public.json' "$CADDYFILE" || true)"
if grep -q 'backup-status' "$CADDYFILE"; then
  bad "the Caddyfile now serves a backup status without authentication"
  note "the age of a household's last backup is reconnaissance, and unlike 'the update"
  note "failed' it is never what you need while the app is down. The API serves it instead."
else
  ok "and nothing in the Caddyfile serves a backup status unauthenticated"
fi

# The stale-after policy lives in .env, once, because the API hands the number to the page
# rather than the page hard-coding it.
if grep -qE '^SAIVA_BACKUP_STALE_HOURS=[0-9]+$' "$ROOT/.env.example"; then
  ok ".env.example still carries SAIVA_BACKUP_STALE_HOURS"
else
  bad ".env.example no longer sets SAIVA_BACKUP_STALE_HOURS; the UI would have to invent the policy"
fi

# The second drop-box. A backup request is told apart from an update request by WHICH
# directory it lands in, which is what lets root unlink a request without ever reading a
# byte the container wrote.
if grep -qE '^ *- \$\{SAIVA_STATE_DIR:-[^}]*\}/backup-requests:' "$ROOT/docker-compose.prod.yml"; then
  ok "docker-compose.prod.yml still bind-mounts the backup-requests drop-box into api"
else
  bad "the backup-requests drop-box is no longer mounted into the api container"
fi

# --- 2. a database that looks like the real one -----------------------------------------
#
# Same shape as test-restore-backup.sh builds: an enum type, serial primary keys, a foreign
# key, an index and an alembic_version row -- so the verification's join across households,
# accounts and transactions is a real query rather than one that is skipped. 50 transactions
# rather than 3 because section 4b truncates a COPY block part way through and needs rows
# on both sides of the cut.

psql -qc "DROP DATABASE IF EXISTS \"$TESTDB\" WITH (FORCE)" -d postgres >/dev/null 2>&1
psql -qc "CREATE DATABASE \"$TESTDB\"" -d postgres >/dev/null

psql -X -q -v ON_ERROR_STOP=1 -d "$TESTDB" >/dev/null <<'SQL'
CREATE TYPE account_kind AS ENUM ('bank', 'card', 'loan');
CREATE TABLE households (
  id   serial PRIMARY KEY,
  name text NOT NULL
);
CREATE TABLE accounts (
  id           serial PRIMARY KEY,
  household_id integer NOT NULL REFERENCES households(id),
  name         text NOT NULL,
  kind         account_kind NOT NULL,
  balance      numeric(14,2) NOT NULL DEFAULT 0
);
CREATE INDEX accounts_household_idx ON accounts (household_id);
CREATE TABLE transactions (
  id         serial PRIMARY KEY,
  account_id integer NOT NULL REFERENCES accounts(id),
  posted_on  date NOT NULL,
  payee      text NOT NULL,
  amount     numeric(14,2) NOT NULL
);
CREATE TABLE alembic_version (
  version_num varchar(32) NOT NULL,
  CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

INSERT INTO alembic_version VALUES ('0003');
INSERT INTO households (name) VALUES ('Alfaro');
INSERT INTO accounts (household_id, name, kind, balance)
  VALUES (1, 'Everyday', 'bank', 2140.55), (1, 'Visa', 'card', -318.20);
INSERT INTO transactions (account_id, posted_on, payee, amount)
  SELECT 1 + (g % 2), date '2026-10-01' + g, 'Payee ' || g, (g * 7)::numeric / 100
  FROM generate_series(1, 50) g;
SQL

LIVE_ROWS="$(q 'select count(*) from transactions')"
assert_eq "transactions in the live ledger" "50" "$LIVE_ROWS"
# What the dump will declare and what the restore must count back: every row in every
# table, which is the aggregate the record calls rows_checked.
TOTAL_ROWS="$(psql -X -At -q -d "$TESTDB" -c "
  select (select count(*) from households) + (select count(*) from accounts)
       + (select count(*) from transactions) + (select count(*) from alembic_version)")"
assert_eq "rows across every table" "54" "$TOTAL_ROWS"

# --- 3. a good dump passes, and the record says exactly what was proved ------------------

rc=0
run_backup >"$WORK/good.out" 2>&1 </dev/null || rc=$?
if [ "$rc" -ne 0 ]; then
  bad "a good backup exited $rc"
  cat "$WORK/good.out"
else
  ok "a good backup exits 0"
fi
read_status "good run"

assert_eq "phase"       "ok"           "$(sf phase)"
assert_eq "step"        "done"         "$(sf step)"
assert_eq "trigger"     "cli"          "$(sf trigger)"
assert_eq "verified"    "true"         "$(sf verified)"
assert_eq "rows_checked" "$TOTAL_ROWS" "$(sf rows_checked)"
assert_eq "exit_code"   "0"            "$(sf exit_code)"
assert_eq "dump_revision reads alembic_version out of the dump" "0003" "$(sf dump_revision)"
assert_eq "backup_count" "1"           "$(sf backup_count)"
assert_eq "other_count with no safety copies yet" "0" "$(sf other_count)"
assert_eq "kept"         "14"          "$(sf kept)"
assert_eq "offsite with no destination configured" "off" "$(sf offsite)"

GOOD_DUMP="$(sf dump_name)"
GOOD_SUCCESS_AT="$(sf last_success_at)"
GOOD_VERIFIED_AT="$(sf last_verified_at)"
[ "$GOOD_SUCCESS_AT" != "<null>" ] && ok "last_success_at was set: $GOOD_SUCCESS_AT" \
                                   || bad "a successful run recorded no last_success_at"
[ "$GOOD_VERIFIED_AT" != "<null>" ] && ok "last_verified_at was set: $GOOD_VERIFIED_AT" \
                                    || bad "a verified run recorded no last_verified_at"

if printf '%s' "$GOOD_DUMP" | grep -qE "$NIGHTLY_RE"; then
  ok "the dump is named the way the prune's own pattern expects: $GOOD_DUMP"
else
  bad "the dump name [$GOOD_DUMP] does not match the pattern the prune matches"
fi
if printf '%s' "$GOOD_DUMP" | grep -qE "$RESTORABLE_RE"; then
  ok "and matches restore-backup.sh's stamp pattern, so the rehearsed procedure restores it"
else
  bad "[$GOOD_DUMP] does not match restore-backup.sh's ^saiva-<date>-<time>- pattern"
  note "a nightly dump that the rehearsed restore cannot name is not a backup of anything"
fi
assert_eq "the dump is on disk" "$GOOD_DUMP" "$(nightly_dumps)"
assert_eq "and it is 0600 -- nothing but root needs to read a copy of the ledger" \
  "600" "$(stat -c '%a' "$BACKUPS/$GOOD_DUMP")"
assert_eq "dump_bytes matches the file" "$(stat -c '%s' "$BACKUPS/$GOOD_DUMP")" "$(sf dump_bytes)"
gzip -t "$BACKUPS/$GOOD_DUMP" 2>/dev/null && ok "the dump is a complete gzip stream" \
                                          || bad "the dump is not a readable gzip"

# oldest_at comes out of the NAME, not the mtime: a file's mtime survives a `docker cp` or
# a volume copy with a different value and the name does not.
OLDEST_EXPECT="$(printf '%s' "$GOOD_DUMP" | sed -E 's/^saiva-(....)(..)(..)-(..)(..)(..)-.*/\1-\2-\3 \4:\5:\6 UTC/')"
assert_eq "oldest_at is read out of the dump's name" "$OLDEST_EXPECT" "$(sf oldest_at)"

# The record has no public face, and that is a decision rather than an omission.
if ls "$STATE"/*public*.json >/dev/null 2>&1; then
  bad "a backup run rendered a public status file: $(ls "$STATE"/*public*.json)"
else
  ok "no public counterpart to backup-status.json was written"
fi
assert_nothing_else_touched "good run"

# --- 4. three dumps a weaker check would have called good --------------------------------
#
# Each is built from a REAL pg_dump of the database above, so each is the mistake it stands
# for rather than a hand-written approximation of it, and each is then re-verified through
# `--verify-only` -- which is the same verify_dump() the nightly path calls, reached without
# having to make pg_dump misbehave.
#
# The good run in section 3 left last_success_at / last_verified_at set. Every case below
# must leave both of them exactly as they were: a failed verification says "the backup
# failed", and it must not also erase the answer to "when did one last work".

pg_dump --host "$PGHOST" --port "$PGPORT" --username "$PGUSER" --dbname "$TESTDB" \
        --no-owner --no-privileges --file "$WORK/plain.sql"
pg_dump --host "$PGHOST" --port "$PGPORT" --username "$PGUSER" --dbname "$TESTDB" \
        --no-owner --no-privileges --schema-only --file "$WORK/schema-only.sql"

# Restores a dump with nothing but the weak check -- `psql --single-transaction -v
# ON_ERROR_STOP=1`, which is what "it restored without error" means -- and leaves its exit
# status in $WEAK_RC and the rows that arrived in $WEAK_ROWS. This is the measurement, not
# a quotation of one.
weak_check() { # PATH-TO-DUMP-GZ
  psql -d postgres -qc "DROP DATABASE IF EXISTS \"$WEAKDB\" WITH (FORCE)" >/dev/null 2>&1
  psql -d postgres -qc "CREATE DATABASE \"$WEAKDB\" TEMPLATE template0" >/dev/null
  local -a status
  set +e
  gunzip -c "$1" 2>/dev/null \
    | psql -X -q -o /dev/null -v ON_ERROR_STOP=1 --single-transaction -d "$WEAKDB" -f - >/dev/null 2>&1
  status=("${PIPESTATUS[@]}")
  set -e
  WEAK_GUNZIP_RC="${status[0]}"
  WEAK_RC="${status[1]}"
  WEAK_ROWS="$(psql -X -At -q -d "$WEAKDB" -c 'select count(*) from transactions' 2>/dev/null || printf 'no table')"
  psql -d postgres -qc "DROP DATABASE IF EXISTS \"$WEAKDB\" WITH (FORCE)" >/dev/null
}

# Plants one dump as the only thing in the directory and re-verifies it. --verify-only takes
# the NEWEST dump of any kind, so the directory is cleared first rather than relying on the
# planted stamp winning a sort against whatever an earlier section wrote.
verify_only_on() { # STAMPED-NAME  < uncompressed SQL on stdin, or via $2 as a path
  clear_backups
  if [ -n "${2:-}" ]; then gzip -c "$2" >"$BACKUPS/$1"; else gzip -c >"$BACKUPS/$1"; fi
  chmod 0600 "$BACKUPS/$1"
  VO_RC=0
  run_backup --verify-only >"$WORK/verify.out" 2>&1 </dev/null || VO_RC=$?
  read_status "$1"
}

assert_failed_verification() { # LABEL
  [ "$VO_RC" -ne 0 ] && ok "$1: the run fails (exit $VO_RC)" \
                     || { bad "$1: a dump that is not restorable was reported as a backup"; cat "$WORK/verify.out"; }
  assert_eq "$1: phase"    "failed" "$(sf phase)"
  assert_eq "$1: verified" "false"  "$(sf verified)"
  assert_eq "$1: exit_code" "1"     "$(sf exit_code)"
  [ -n "$(sf verify_message)" ] && ok "$1: and it says what the rehearsal found" \
                                || bad "$1: verified=false with no verify_message"
  assert_eq "$1: last_success_at is carried forward"  "$GOOD_SUCCESS_AT"  "$(sf last_success_at)"
  assert_eq "$1: last_verified_at is carried forward" "$GOOD_VERIFIED_AT" "$(sf last_verified_at)"
  assert_nothing_else_touched "$1"
}

# --- 4a. a --schema-only dump ------------------------------------------------------------
#
# The mistake that gets made by hand, and the one the completion marker cannot catch: this
# dump is COMPLETE. pg_dump finished, the marker is there, psql replays it without a
# murmur. Only the row-count comparison knows the difference between a schema and a backup.
gzip -c "$WORK/schema-only.sql" >"$WORK/schema-only.sql.gz"
weak_check "$WORK/schema-only.sql.gz"
assert_eq "4a: gunzip reads a --schema-only dump fine"     "0" "$WEAK_GUNZIP_RC"
assert_eq "4a: and psql replays it with exit 0"            "0" "$WEAK_RC"
assert_eq "4a: leaving the transactions table empty"       "0" "$WEAK_ROWS"
if grep -q '^-- PostgreSQL database dump complete$' "$WORK/schema-only.sql"; then
  ok "4a: the completion marker IS present, so that check cannot catch this either"
else
  bad "4a: pg_dump --schema-only no longer writes the completion marker"
  note "if that changed, re-measure which of the four checks are still load-bearing"
fi
note "4a: so a regime that checked gunzip + marker + 'psql exited 0' would have ticked this"

verify_only_on "saiva-20260401-010101-nightly.sql.gz" "$WORK/schema-only.sql"
assert_failed_verification "4a --schema-only"
if grep -q 'schema-only' "$WORK/verify.out"; then
  ok "4a: and it names the mistake rather than printing a table-by-table diff"
else
  bad "4a: the failure does not say that there is no data in the dump at all"
  cat "$WORK/verify.out"
fi

# --- 4b. truncated inside a COPY block ---------------------------------------------------
#
# THE finding. psql ends the COPY with whatever rows it got and exits 0, so this is the dump
# that would have been handed to a household as a verified backup while missing most of
# their ledger. Measured below, both ways, on every run of this suite.
COPY_LINE="$(grep -n '^COPY public\.transactions ' "$WORK/plain.sql" | head -1 | cut -d: -f1 || true)"
if [ -z "$COPY_LINE" ]; then
  bad "4b: no 'COPY public.transactions' header in a plain pg_dump; cannot build the case"
  note "pg_dump's COPY header shape is what scripts/backup.sh's dump_declares awk matches,"
  note "so if this moved, that parser needs re-measuring before this suite can test it"
else
  head -n "$(( COPY_LINE + 10 ))" "$WORK/plain.sql" >"$WORK/mid-copy.sql"
  gzip -c "$WORK/mid-copy.sql" >"$WORK/mid-copy.sql.gz"
  weak_check "$WORK/mid-copy.sql.gz"
  assert_eq "4b: gunzip reads the truncated-mid-COPY dump fine" "0" "$WEAK_GUNZIP_RC"
  assert_eq "4b: and psql --single-transaction -v ON_ERROR_STOP=1 EXITS 0 on it" "0" "$WEAK_RC"
  assert_eq "4b: having loaded 10 of the ${LIVE_ROWS} transactions"  "10" "$WEAK_ROWS"
  note "4b: that is the measurement the completion marker and the row-count check exist for"

  verify_only_on "saiva-20260402-020202-nightly.sql.gz" "$WORK/mid-copy.sql"
  assert_failed_verification "4b mid-COPY truncation"
  if grep -q 'dump complete' "$WORK/verify.out"; then
    ok "4b: the missing completion marker is named"
  else
    bad "4b: the failure does not mention the missing completion marker"
  fi
  if grep -q 'public.transactions' "$WORK/verify.out"; then
    ok "4b: and the row-count comparison names the table that came up short"
  else
    bad "4b: the failure does not name the table whose counts disagree"
    cat "$WORK/verify.out"
  fi
fi

# --- 4c. a truncated gzip stream ---------------------------------------------------------
#
# The only one of the three that the restore itself would notice, and the only one gunzip's
# exit status can see: the CRC and length trailer at the end of the member are gone.
gzip -c "$WORK/plain.sql" >"$WORK/whole.sql.gz"
WHOLE_BYTES="$(stat -c '%s' "$WORK/whole.sql.gz")"
head -c "$(( WHOLE_BYTES - 120 ))" "$WORK/whole.sql.gz" >"$WORK/cut.sql.gz"
if gzip -t "$WORK/cut.sql.gz" 2>/dev/null; then
  bad "4c: the deliberately truncated gzip still passes gzip -t, so this case proves nothing"
else
  ok "4c: the truncated stream really is an unreadable gzip"
fi
clear_backups
cp -- "$WORK/cut.sql.gz" "$BACKUPS/saiva-20260403-030303-nightly.sql.gz"
VO_RC=0
run_backup --verify-only >"$WORK/verify.out" 2>&1 </dev/null || VO_RC=$?
read_status "truncated gzip"
assert_failed_verification "4c truncated gzip"
if grep -qi 'truncated or corrupt' "$WORK/verify.out"; then
  ok "4c: and it says the archive could not be read all the way through"
else
  bad "4c: the failure does not say the archive is truncated"
  cat "$WORK/verify.out"
fi

# --- 5. a failed run must not leave the previous run's green record standing -------------
#
# The "yesterday's answer" failure, and the reason it is a test rather than a comment: the
# status file is the only thing the panel reads, so a run that dies without rewriting it
# leaves a verified backup from BEFORE the failure on the page, dated and ticked. This
# forces it through the full nightly path -- pg_dump really runs, a dump is really written
# -- by putting a pg_dump on PATH that quietly adds --schema-only, which is the shape of a
# real misconfiguration rather than a contrived crash.
clear_backups
rc=0
run_backup >/dev/null 2>&1 </dev/null || rc=$?
read_status "re-seed"
assert_eq "a fresh good run before the failure" "ok" "$(sf phase)"
SEED_SUCCESS_AT="$(sf last_success_at)"
SEED_VERIFIED_AT="$(sf last_verified_at)"
SEED_DUMPS="$(listed_dumps)"

mkdir -p "$WORK/shim-schema"
cat >"$WORK/shim-schema/pg_dump" <<SH
#!/bin/sh
# --version goes straight through: backup.sh asks for it in check_version_skew, and a shim
# that answered that wrongly would abort the run before it ever reached the dump.
for a in "\$@"; do case "\$a" in --version) exec "$PG_DUMP_REAL" "\$@" ;; esac; done
exec "$PG_DUMP_REAL" --schema-only "\$@"
SH
chmod +x "$WORK/shim-schema/pg_dump"

rc=0
( PATH="$WORK/shim-schema:$PATH"; run_backup ) >"$WORK/stale.out" 2>&1 </dev/null || rc=$?
read_status "failed nightly run"
[ "$rc" -ne 0 ] && ok "a nightly run whose dump cannot be verified exits non-zero ($rc)" \
                || { bad "an unverifiable nightly dump was reported as a success"; cat "$WORK/stale.out"; }
assert_eq "phase becomes failed"    "failed" "$(sf phase)"
assert_eq "verified becomes false"  "false"  "$(sf verified)"
assert_eq "exit_code"               "1"      "$(sf exit_code)"
assert_eq "last_success_at is PRESERVED, not cleared and not refreshed" \
  "$SEED_SUCCESS_AT" "$(sf last_success_at)"
assert_eq "last_verified_at is PRESERVED" "$SEED_VERIFIED_AT" "$(sf last_verified_at)"
STALE_DUMP="$(sf dump_name)"
if [ "$STALE_DUMP" != "$(printf '%s\n' "$SEED_DUMPS" | tail -1)" ] && [ "$STALE_DUMP" != "<null>" ]; then
  ok "the record names THIS run's dump, not the last good one: $STALE_DUMP"
else
  bad "dump_name still names a previous run's dump [$STALE_DUMP]"
fi
if grep -q "$STALE_DUMP" "$WORK/stale.out"; then
  ok "and the failed dump is kept and named, so it can be looked at"
else
  bad "the failure does not name the dump it kept"
fi
assert_nothing_else_touched "failed nightly run"
# The unverifiable dump is deliberately kept by backup.sh. Removed here so the retention
# arithmetic in section 8 counts only dumps this suite planted on purpose.
rm -f "$BACKUPS/$STALE_DUMP"

# --- 6. a half-written dump is invisible -------------------------------------------------
#
# pg_dump dying part way is the ordinary failure -- a full volume, a server that goes away
# -- and the one thing that must never come of it is a file in the listing. backup.sh writes
# to a dot-prefixed `.part` name, either half of which keeps it out of the `*.sql.gz` glob,
# and renames only after counting the bytes. This shim writes a partial file to whatever
# --file it was given and then fails, which is the case the rename exists for.
mkdir -p "$WORK/shim-partial"
cat >"$WORK/shim-partial/pg_dump" <<SH
#!/bin/sh
for a in "\$@"; do case "\$a" in --version) exec "$PG_DUMP_REAL" "\$@" ;; esac; done
out=""; want=0
for a in "\$@"; do
  if [ "\$want" = 1 ]; then out="\$a"; want=0; continue; fi
  if [ "\$a" = "--file" ]; then want=1; fi
done
[ -n "\$out" ] && printf '%s' 'half a dump, no gzip trailer' | gzip -c > "\$out"
printf 'pg_dump: error: forced failure from the test shim, after writing a partial file\n' >&2
exit 1
SH
chmod +x "$WORK/shim-partial/pg_dump"

BEFORE_DUMPS="$(listed_dumps | tr '\n' ' ')"
# Every entry, dotfiles included, because the partial dump is written as
# `.<name>.sql.gz.part` -- a leading dot AND a .part suffix, either of which alone keeps it
# out of the listing's glob. The glob check above would not see one left behind; this does.
BEFORE_ALL="$(ls -a1 "$BACKUPS" | sort | tr '\n' ' ')"
rc=0
( PATH="$WORK/shim-partial:$PATH"; run_backup ) >"$WORK/partial.out" 2>&1 </dev/null || rc=$?
read_status "pg_dump died part way"
[ "$rc" -ne 0 ] && ok "a pg_dump that dies part way fails the run (exit $rc)" \
                || { bad "a failed pg_dump was reported as a backup"; cat "$WORK/partial.out"; }
assert_eq "phase" "failed" "$(sf phase)"
assert_eq "the run says which step it stopped at" "dump" "$(sf step)"
assert_eq "nothing new appears in the listing's *.sql.gz glob" "$BEFORE_DUMPS" "$(listed_dumps | tr '\n' ' ')"
assert_eq "and no .part file is left in the directory either" \
  "$BEFORE_ALL" "$(ls -a1 "$BACKUPS" | sort | tr '\n' ' ')"
assert_eq "dump_name is not set to a dump that does not exist" "<null>" "$(sf dump_name)"
assert_eq "last_success_at is still carried forward"  "$SEED_SUCCESS_AT"  "$(sf last_success_at)"
assert_eq "last_verified_at is still carried forward" "$SEED_VERIFIED_AT" "$(sf last_verified_at)"
assert_nothing_else_touched "pg_dump died part way"

# --- 7. a run that cannot take the deploy lock LEAVES THE RECORD ALONE -------------------
#
# ONE lock, and it is the UPDATE's lock, so an update, a rollback and a backup can never
# overlap.
#
# This section used to assert that such a run recorded `phase: skipped`, and an adversarial
# review showed why that was wrong: every status_set is a read-modify-write of one shared
# file, so writing anything at all goes over the record of the run that HOLDS the lock. A
# press of "Back up now" during a nightly run replaced that run's run_id, started_at and
# verdict with the press's own. The rule is now the same one scripts/lib/release.sh states
# for acquire_lock: the record belongs to the run that is going, and a process that could
# not start knows nothing about it.
#
# So: exit 0, say so on stdout, write nothing. What the owner sees is the run in flight
# still being reported, and the age of the newest VERIFIED backup -- which a run that took
# nothing must never reset, and which does the shouting if this keeps happening.
PRE_LOCK_PHASE="$(sf phase)"
PRE_LOCK_RUN_ID="$(sf run_id)"
PRE_LOCK_STARTED="$(sf started_at)"
( flock 9; sleep 6 ) 9>"$STATE/deploy.lock" &
LOCK_HOLDER=$!
sleep 0.4

rc=0
run_backup --trigger=button >"$WORK/button.out" 2>&1 </dev/null || rc=$?
read_status "button press while the lock is held"
# 75, not 0. EX_TEMPFAIL, because 0 was ambiguous in a way that destroyed the record: the
# host agent treats "exited 0 and wrote no status" as a run that died silently, and wrote a
# failure document of its own over backup-status.json -- which does not carry the
# carried-forward fields, so a press during a release erased last_verified_at and the page
# then said there had never been a backup. Reproduced by review.
assert_eq "a button press waits for nothing and declines with EX_TEMPFAIL" "75" "$rc"
assert_eq "it does not overwrite the holder's phase"      "$PRE_LOCK_PHASE"   "$(sf phase)"
assert_eq "nor the holder's run id"                       "$PRE_LOCK_RUN_ID"  "$(sf run_id)"
assert_eq "nor when the holder started"                   "$PRE_LOCK_STARTED" "$(sf started_at)"
assert_eq "verified is left as it was -- this run proved nothing" "<null>" "$(sf verified)"
assert_eq "last_success_at is untouched"  "$SEED_SUCCESS_AT"  "$(sf last_success_at)"
assert_eq "last_verified_at is untouched" "$SEED_VERIFIED_AT" "$(sf last_verified_at)"
case "$(cat "$WORK/button.out")" in
  *"holds the lock"*) ok "and it says on stdout why nothing was taken" ;;
  *) bad "a press that took nothing said nothing"; note "got: $(cat "$WORK/button.out")" ;;
esac

rc=0
SAIVA_BACKUP_LOCK_WAIT=1 run_backup >"$WORK/timer.out" 2>&1 </dev/null || rc=$?
read_status "timer run that waited and gave up"
assert_eq "a timer run that waits and gives up declines the same way" "75" "$rc"
assert_eq "and leaves the record alone too" "$PRE_LOCK_RUN_ID" "$(sf run_id)"
assert_eq "last_verified_at is still untouched" "$SEED_VERIFIED_AT" "$(sf last_verified_at)"
if grep -qi 'verified' "$WORK/timer.out"; then
  ok "and it points at the number that does matter: the age of the newest verified backup"
else
  bad "a run that took nothing does not say what the owner should watch instead"
fi
wait "$LOCK_HOLDER" 2>/dev/null || true
assert_nothing_else_touched "runs that could not take the lock"

# --- 8. retention: the oldest go, the newest never, and only what this script wrote ------
#
# Eight planted nightlies and keep=5. cmd_run prunes TWICE -- once to policy before the
# dump, because dumps already past the retention are the only space there is to reclaim and
# filling this filesystem stops Postgres rather than merely stopping the backup, and once
# after, now that this run has added one. So the arithmetic is: the newest four planted
# dumps survive alongside the new one, and the four oldest are gone.
clear_backups
for day in 01 02 03 04 05 06 07 08; do
  plant_nightly "saiva-202603${day}-010101-nightly.sql.gz"
done
# Never pruned, however old. This script may remove what it created and may not remove what
# the api's pre-migration dump or restore-backup.sh's safety dump created -- their count is
# reported instead, so a volume filling up is visible rather than silently tidied.
# At 0600, and owned by whoever writes dumps here, so the prune's NAME pattern is the only
# thing standing between them and deletion. They were 0644 before, which meant the
# candidate list's -perm filter excluded them and this section proved nothing about the
# name: widening the pattern from `-nightly` to `-.*` kept the whole suite green.
plant_nightly "saiva-20200101-010101-premigrate.sql.gz" "premigrate"
plant_nightly "saiva-20200102-020202-prerestore.sql.gz" "prerestore"

rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=5 run_backup >"$WORK/prune.out" 2>&1 </dev/null || rc=$?
read_status "retention"
assert_eq "the run with a retention set exits 0" "0" "$rc"
assert_eq "kept" "5" "$(sf kept)"
NEW_DUMP="$(sf dump_name)"
assert_eq "the four newest planted nightlies survive, with this run's dump" \
  "$(printf '%s\n' \
      saiva-20260305-010101-nightly.sql.gz \
      saiva-20260306-010101-nightly.sql.gz \
      saiva-20260307-010101-nightly.sql.gz \
      saiva-20260308-010101-nightly.sql.gz \
      "$NEW_DUMP" | sort | tr '\n' ' ')" \
  "$(nightly_dumps | tr '\n' ' ')"
assert_eq "the newest planted nightly was never a candidate" "1" \
  "$(nightly_dumps | grep -c '^saiva-20260308-010101-nightly\.sql\.gz$' || true)"
assert_eq "the four oldest planted nightlies are gone" "0" \
  "$(nightly_dumps | grep -cE '^saiva-202603(01|02|03|04)-' || true)"
assert_eq "the 2020 premigrate dump is untouched, six years old" "1" \
  "$(listed_dumps | grep -c '^saiva-20200101-010101-premigrate\.sql\.gz$' || true)"
assert_eq "the 2020 prerestore dump is untouched too" "1" \
  "$(listed_dumps | grep -c '^saiva-20200102-020202-prerestore\.sql\.gz$' || true)"
# Counted and RECORDED, not only logged: oldest_at is now 2020 because of these two, and a
# page that cannot say they exist reads that date as a six-year-old nightly.
assert_eq "other_count records the safety copies the prune leaves alone" "2" "$(sf other_count)"
assert_eq "and oldest_at is the oldest of ALL of them" "2020-01-01 01:01:01 UTC" "$(sf oldest_at)"

# --- 9. the prune never removes a file whose name it did not write -----------------------
#
# A backups directory with something unexpected in it is the normal case at the moment
# anyone looks, not an exceptional one: a note somebody saved beside the dumps, a file
# restored from a tarball under a mangled name, a symlink left by a half-finished
# `docker cp`. keep=1 so that everything prunable is pruned and there is nowhere for a
# survivor to hide.
clear_backups
plant_nightly "saiva-20260501-010101-nightly.sql.gz" "planted nightly A"
plant_nightly "saiva-20260502-020202-nightly.sql.gz" "planted nightly B"
printf 'these are my notes, not a dump\n' >"$BACKUPS/notes.sql.gz"
printf 'wrong shape of name\n'            >"$BACKUPS/saiva-bad-name-nightly.sql.gz"
printf 'PRECIOUS, and not in the dumps directory\n' >"$OUTSIDE/precious.txt"
NOTES_SUM="$(md5sum <"$BACKUPS/notes.sql.gz")"
BADNAME_SUM="$(md5sum <"$BACKUPS/saiva-bad-name-nightly.sql.gz")"
PRECIOUS_SUM="$(md5sum <"$OUTSIDE/precious.txt")"
# A symlink whose NAME the prune's pattern matches exactly, pointing at a file outside the
# directory. Two things must be true of it, and the second is the one this suite moved:
#
#   the TARGET survives            always. The alternative is a prune that deletes whatever
#                                  anyone can get a symlink to point at.
#   the LINK ITSELF survives too   because the prune now steps over it (`[ -L ] && continue`)
#                                  rather than counting it as a nightly dump and unlinking
#                                  it. Writing this test is what found that: `[ -f ]`
#                                  follows symlinks, so the previous version counted the
#                                  link, removed it, and the comment above prune_nightlies
#                                  claimed `-type f` semantics it did not have. Removing
#                                  the link was never a way to delete someone else's file,
#                                  but a prune that can be aimed at all is one to close --
#                                  and the listing in scripts/restore-backup.sh names a
#                                  planted link as "not a regular file" rather than this
#                                  quietly tidying it away.
ln -s "$OUTSIDE/precious.txt" "$BACKUPS/saiva-20260101-010101-nightly.sql.gz"

rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=1 run_backup >"$WORK/decoy.out" 2>&1 </dev/null || rc=$?
read_status "prune decoys"
assert_eq "the run with decoys beside the dumps exits 0" "0" "$rc"
assert_eq "notes.sql.gz is still there, byte for byte" "$NOTES_SUM" "$(md5sum <"$BACKUPS/notes.sql.gz")"
assert_eq "saiva-bad-name-nightly.sql.gz is still there too" \
  "$BADNAME_SUM" "$(md5sum <"$BACKUPS/saiva-bad-name-nightly.sql.gz")"
if [ -e "$OUTSIDE/precious.txt" ]; then
  assert_eq "the symlink's TARGET outside the directory survives" \
    "$PRECIOUS_SUM" "$(md5sum <"$OUTSIDE/precious.txt")"
else
  bad "the prune followed a symlink and deleted a file outside the dumps directory"
  note "a prune that can be aimed with a symlink can be aimed at anything root can unlink"
fi
if [ -L "$BACKUPS/saiva-20260101-010101-nightly.sql.gz" ]; then
  ok "the planted symlink itself is left for the listing to report, not pruned"
else
  bad "the prune removed a symlink; it must only ever touch regular files it could have written"
  note "scripts/backup.sh's prune needs [ -L ] && continue -- [ -f ] follows symlinks"
fi
# The planted symlink is therefore still in the listing, and the two REGULAR planted
# nightlies are gone: with keep=1 the only regular nightly left is this run's own dump.
assert_eq "only this run's dump is left of the regular nightlies, with keep=1" \
  "$(sf dump_name)" \
  "$(nightly_dumps | while read -r n; do [ -L "$BACKUPS/$n" ] || printf '%s\n' "$n"; done)"

# --- 10. the copy that leaves this box, and the record that must stay parseable ----------
#
# Two things at once, because one destination can carry both. A path on the SAME filesystem
# as the dumps is NOT an off-box copy and must never be recorded as one -- losing that disk
# loses the database and the backup together. And the destination's own name is the one
# value in the record that an operator supplies verbatim, so it is also where a quote, a
# backslash or a control byte would reach scripts/lib/state.sh's hand-written JSON escape.
BEL="$(printf '\007')"
HOSTILE="$WORK/dest-\"quoted\"-\\backslash-${BEL}-$(printf 'two\nlines')-end"
mkdir -p "$HOSTILE"
clear_backups
rc=0
run_backup_to "$HOSTILE" "" >"$WORK/offsite.out" 2>&1 </dev/null || rc=$?
read_status "off-box copy to a hostile path"
assert_eq "the run with an off-box destination exits 0" "0" "$rc"
assert_eq "phase" "ok" "$(sf phase)"
assert_eq "a destination on the same filesystem is recorded as same-filesystem, NOT as success" \
  "same-filesystem" "$(sf offsite)"
OFF_DUMP="$(sf dump_name)"
if [ -f "$HOSTILE/$OFF_DUMP" ]; then
  ok "the copy landed: $OFF_DUMP"
  assert_eq "and it is 0600 at the destination" "600" "$(stat -c '%a' "$HOSTILE/$OFF_DUMP")"
  assert_eq "and it is the dump, byte for byte" \
    "$(md5sum <"$BACKUPS/$OFF_DUMP")" "$(md5sum <"$HOSTILE/$OFF_DUMP")"
else
  bad "no copy of $OFF_DUMP reached the destination"
  cat "$WORK/offsite.out"
fi
[ -n "$(sf offsite_at)" ] && [ "$(sf offsite_at)" != "<null>" ] \
  && ok "offsite_at records when the copy was made" \
  || bad "a copy that landed recorded no offsite_at"

# The exact bytes, against the JSON itself rather than the flattened cache. status_set
# collapses a newline to a space -- values in the record are single-line by construction,
# because the log tail is a separate file -- so that is what the target must come back as.
if python3 - "$STATE/backup-status.json" "${HOSTILE//$'\n'/ }" <<'PY'
import json, sys

with open(sys.argv[1]) as handle:
    payload = json.load(handle)
sys.exit(0 if payload.get("offsite_target") == sys.argv[2] else 1)
PY
then
  ok "a destination carrying a quote, a backslash, a BEL byte and a newline round-trips exactly"
else
  bad "offsite_target did not survive the JSON escape"
  note "expected: $(printf '%s' "${HOSTILE//$'\n'/ }" | cat -v)"
  note "got:      $(sf offsite_target | cat -v)"
fi

# A destination that is on this machine but NOT on the dumps' filesystem. This is the case
# `stat -c %d` alone gets wrong, and the reason the vocabulary has six states rather than
# four: a btrfs subvolume, a ZFS dataset, an LVM thin volume and a Proxmox mp bind-mount
# each report a different device number while living on the same physical disk -- which is
# exactly what a Proxmox box looks like. /dev/shm stands in for all of them here because it
# is a real second filesystem that needs no mount. It must come back as a WARNING.
if [ -d /dev/shm ] && [ -w /dev/shm ] \
   && [ "$(stat -c '%d' /dev/shm)" != "$(stat -c '%d' "$BACKUPS")" ]; then
  SHMDEST="/dev/shm/saiva-backup-test-$$"
  rm -rf "$SHMDEST"; mkdir -p "$SHMDEST"
  clear_backups
  rc=0
  run_backup_to "$SHMDEST" "" >"$WORK/samehost.out" 2>&1 </dev/null || rc=$?
  read_status "a different filesystem on the same machine"
  assert_eq "a different device on the same machine is same-host, not off-box" \
    "same-host" "$(sf offsite)"
  if printf '%s' "$(sf offsite_message)" | grep -q 'does not survive the machine'; then
    ok "and the message says what that does and does not protect against"
  else
    bad "same-host is recorded without saying it is not a backup of this box"
    note "got: $(sf offsite_message)"
  fi
  rm -rf "$SHMDEST"
else
  note "skipped the same-host case: no writable second filesystem here to stand in for a"
  note "btrfs subvolume or a Proxmox mp, so a different device number cannot be produced"
fi

# A configured push command is the one destination this box cannot measure, so it is the one
# taken on the command's own say-so -- and it must be stated rather than inherited.
clear_backups
rc=0
run_backup_to "" "cp -- \"\$1\" \"$WORK/pushed.sql.gz\"" >"$WORK/push.out" 2>&1 </dev/null || rc=$?
read_status "a push command that succeeds"
assert_eq "the run with a push command exits 0" "0" "$rc"
assert_eq "a push command that succeeds is the only thing recorded as remote" "remote" "$(sf offsite)"
[ -s "$WORK/pushed.sql.gz" ] && ok "and the command really was handed the dump as \$1" \
                             || bad "the push command was recorded as having accepted nothing"

clear_backups
rc=0
run_backup_to "" "exit 3" >"$WORK/pushfail.out" 2>&1 </dev/null || rc=$?
read_status "a push command that fails"
assert_eq "a failed off-box copy does not fail the backup itself" "0" "$rc"
assert_eq "the dump was still taken and verified" "true" "$(sf verified)"
assert_eq "but the copy is recorded as failed" "failed" "$(sf offsite)"
assert_eq "and offsite_at stays unknown, because nothing arrived" "<null>" "$(sf offsite_at)"

# --- 11. /etc/saiva/agent.conf is ignored unless only root can change it -----------------
#
# SAIVA_BACKUP_COPY_CMD out of that file is run as root by a systemd timer, and
# SAIVA_BACKUP_COPY_TO makes root create files at a path somebody else chose. So the file is
# READ rather than sourced, and it is ignored outright unless its owner and mode say nobody
# but root can change it. Both directions are proved, because a check that only rejects is
# indistinguishable from a check that rejects everything -- including the conf the installer
# writes, which would leave every box with no off-box copy and no complaint.
SENTINEL="$WORK/the-conf-command-ran-as-root"
CONFDEST="$WORK/confdest"
mkdir -p "$CONFDEST"
cat >"$WORK/agent.conf" <<EOF
SAIVA_BACKUP_COPY_TO=$CONFDEST
SAIVA_BACKUP_COPY_CMD=touch "$SENTINEL"
SAIVA_BACKUP_KEEP_NIGHTLY=3
EOF
as_root chown 0:0 "$WORK/agent.conf" 2>/dev/null || true

if [ "$(stat -c '%u' "$WORK/agent.conf")" != "0" ]; then
  note "skipped the agent.conf cases: this run is not root and has no passwordless sudo, so"
  note "it cannot make a root-owned file -- and without one, neither direction of the"
  note "ownership check could be told apart from the other. Re-run as root to include them."
else
  for mode in 0666 0664; do
    as_root chmod "$mode" "$WORK/agent.conf"
    # The mode is asserted before it is relied on. Without this, an as_root that could
    # chown but not chmod would leave the file at the 0644 the heredoc created -- which
    # backup.sh correctly trusts -- and the "its settings are NOT acted on" assertions
    # below would then be failing for a reason that has nothing to do with backup.sh.
    assert_eq "the conf really is ${mode} before the run" \
      "${mode#0}" "$(stat -c '%a' "$WORK/agent.conf")"
    rm -f "$SENTINEL"
    find "$CONFDEST" -mindepth 1 -delete 2>/dev/null || true
    clear_backups
    rc=0
    run_backup_conf_only >"$WORK/conf-$mode.out" 2>&1 </dev/null || rc=$?
    read_status "agent.conf $mode"
    assert_eq "a $mode agent.conf: the run still succeeds" "0" "$rc"
    assert_eq "a $mode agent.conf: its destination is NOT acted on" "off" "$(sf offsite)"
    assert_eq "a $mode agent.conf: its retention is NOT acted on"   "14"  "$(sf kept)"
    if [ -e "$SENTINEL" ]; then
      bad "a $mode agent.conf had its SAIVA_BACKUP_COPY_CMD run as root"
      note "anyone who can write that file would have root on this box"
    else
      ok "a $mode agent.conf: its copy command never ran"
    fi
    assert_eq "a $mode agent.conf: nothing was copied to its destination" "0" \
      "$(find "$CONFDEST" -mindepth 1 | wc -l)"
    # Captured to a file rather than piped into grep: --self-check exits non-zero when it
    # finds something to report -- which it does here, by design -- and under `pipefail`
    # that status is the pipeline's, so `--self-check | grep` fails however well the grep
    # matched. Measured the hard way: both of these cases went red while the output said
    # exactly what the assertion was looking for.
    run_backup_conf_only --self-check >"$WORK/selfcheck-$mode.out" 2>&1 </dev/null || true
    if grep -q 'IGNORED' "$WORK/selfcheck-$mode.out"; then
      ok "a $mode agent.conf: --self-check says out loud that its settings are ignored"
    else
      bad "a $mode agent.conf is ignored silently, so the owner cannot tell why"
      sed 's/^/        /' "$WORK/selfcheck-$mode.out"
    fi
  done

  as_root chmod 0644 "$WORK/agent.conf"
  assert_eq "the conf really is 0644 before the run" "644" "$(stat -c '%a' "$WORK/agent.conf")"
  rm -f "$SENTINEL"
  find "$CONFDEST" -mindepth 1 -delete 2>/dev/null || true
  clear_backups
  rc=0
  run_backup_conf_only >"$WORK/conf-0644.out" 2>&1 </dev/null || rc=$?
  read_status "agent.conf 0644 root-owned"
  assert_eq "a root-owned 0644 agent.conf: the run succeeds" "0" "$rc"
  assert_eq "a root-owned 0644 agent.conf: its retention IS acted on" "3" "$(sf kept)"
  if printf '%s' "$(sf offsite_target)" | grep -qF "$CONFDEST"; then
    ok "a root-owned 0644 agent.conf: its destination IS acted on"
  else
    bad "a root-owned 0644 agent.conf was ignored; the installer's own conf would not work"
    note "got offsite_target: $(sf offsite_target)"
  fi
  [ -e "$SENTINEL" ] && ok "a root-owned 0644 agent.conf: its copy command DID run" \
                     || bad "a root-owned 0644 agent.conf: its copy command did not run"
  assert_eq "and the dump reached the destination the conf named" "1" \
    "$(find "$CONFDEST" -mindepth 1 -type f | wc -l)"
fi

# --- 12. the sweep -----------------------------------------------------------------------
#
# Across every run above -- the ones that failed verification, the one that failed at the
# dump, the two that were skipped and all the ones that succeeded -- not one throwaway
# database may be left behind. Each is a full copy of the household's ledger with nobody
# looking after it, so this is asserted once more over the whole suite and not only after
# each case: a drop that happens on the next run rather than this one would pass every
# per-case check above and still leave one standing overnight.
# Scoped to databases this suite's own runs could have created -- the pid in the name is a
# child of this process -- because another copy of the suite running against the same server
# legitimately has its own in flight. See the cleanup function for what that cost.
assert_eq "no saiva_verify_% database from this suite survives it" "0" "$(verify_dbs)"
assert_eq "the live ledger is exactly as it started" "$LIVE_ROWS" "$(q 'select count(*) from transactions')"
assert_eq "and its schema era is untouched" "0003" "$(q 'select version_num from alembic_version')"

# This suite found the leak and it is now fixed, so it is asserted rather than noted: every
# run with an off-box destination used to leave its staged host-side copy of the dump in
# the state directory as `.offsite.<pid>`, because stage_dump_on_host set STAGED inside a
# command substitution -- a subshell -- so remove_staged in the parent never saw it. Mode
# 0600, so not a disclosure, but one dump-sized copy of the whole ledger per run,
# accumulating on the same filesystem as the database.
assert_eq "no staged off-box copy is left in the state directory" \
  "0" "$(find "$STATE" -maxdepth 1 -name '.offsite.*' | wc -l)"


# ==========================================================================================
# 18. What an adversarial review of this regime found, each case now pinned
# ==========================================================================================
#
# Five findings, four of them reproduced by a reviewer against this script rather than
# reasoned about. Each is here because it was a way for the regime to be broken and report
# itself fine, which is the one failure the whole design exists to prevent.

clear_backups
rc=0

# 18a. BLOCKING. The live-aggregate check was described as catching "a dump of the WRONG
# database", and it could not: the dump comes from $DB and the aggregate counts $DB, so the
# two always agree. Point POSTGRES_DB at a database with no tables and both read zero -- a
# 396-byte dump of nothing came back `verified: true`, every night, while the retention
# retired the real dumps behind it. The structural check (`households` present, a schema
# revision present) is what answers "is this a dump of Saiva at all".
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_empty\" WITH (FORCE)" -d postgres >/dev/null
psql -qc "CREATE DATABASE \"${TESTDB}_empty\"" -d postgres >/dev/null
rc=0
run_backup_as "${TESTDB}_empty" >"$WORK/wrongdb.out" 2>&1 </dev/null || rc=$?
read_status "a dump of an empty database"
assert_eq "18a a dump of a database with no Saiva tables fails" "1" "$rc"
assert_eq "18a and is NOT recorded as verified" "false" "$(sf verified)"
assert_eq "18a phase" "failed" "$(sf phase)"
case "$(sf verify_message)" in
  *households*) ok "18a the message names the missing table and the database it read" ;;
  *) bad "18a the message does not name what made it not a Saiva dump"
     note "got: $(sf verify_message)" ;;
esac
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_empty\" WITH (FORCE)" -d postgres >/dev/null

# 18b. The compounding harm, and the guard against it. A regime that keeps failing must not
# be able to delete its way to having nothing: fourteen bad nights would otherwise retire
# the last restorable dump one night at a time, each run doing exactly what it was
# configured to do.
clear_backups
for d in 01 02 03 04 05; do plant_nightly "saiva-202601${d}-000000-nightly.sql.gz"; done
rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=2 run_backup >/dev/null 2>&1 </dev/null || rc=$?
assert_eq "18b a good run prunes to policy as usual" "0" "$rc"
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_empty\" WITH (FORCE)" -d postgres >/dev/null
psql -qc "CREATE DATABASE \"${TESTDB}_empty\"" -d postgres >/dev/null
rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=2 run_backup_as "${TESTDB}_empty" >/dev/null 2>&1 </dev/null || rc=$?
BEFORE_BAD="$(nightly_dumps | wc -l)"
for _ in 1 2 3; do
  SAIVA_BACKUP_KEEP_NIGHTLY=2 run_backup_as "${TESTDB}_empty" >/dev/null 2>&1 </dev/null || true
done
AFTER_BAD="$(nightly_dumps | wc -l)"
if [ "$AFTER_BAD" -ge "$BEFORE_BAD" ]; then
  ok "18b three more failing runs deleted nothing (${BEFORE_BAD} -> ${AFTER_BAD} dumps)"
else
  bad "18b a failing run pruned: ${BEFORE_BAD} dumps became ${AFTER_BAD}"
  note "while verification is failing nothing may be retired, however far past the retention"
fi
rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=2 run_backup >/dev/null 2>&1 </dev/null || rc=$?
assert_eq "18b and a good run re-enables the policy" "0" "$rc"
assert_eq "18b back to the retention it was told" "2" "$(nightly_dumps | wc -l)"
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_empty\" WITH (FORCE)" -d postgres >/dev/null

# 18c. SERIOUS. `last_verified_at` is when the DUMP WAS TAKEN, not when the verification
# ran. The panel's one headline is derived from it, so taking the clock meant re-verifying
# a forty-day-old dump moved the headline to "Last verified backup: 0 hours ago" -- a true
# statement about the verification presented as a claim about the backup.
clear_backups
pg_dump -d "$TESTDB" --no-owner --no-privileges | gzip -c \
  >"$BACKUPS/saiva-20260930-030000-nightly.sql.gz"
rc=0
run_backup --verify-only >/dev/null 2>&1 </dev/null || rc=$?
read_status "re-verifying an old dump"
assert_eq "18c re-verifying an old dump succeeds" "0" "$rc"
assert_eq "18c last_verified_at is the DUMP's stamp, not the clock" \
  "2026-09-30T03:00:00Z" "$(sf last_verified_at)"
case "$(sf last_success_at)" in
  2026-09-30*) bad "18c last_success_at took the dump's stamp; it is when the RUN finished" ;;
  "") bad "18c last_success_at was not set" ;;
  *) ok "18c while last_success_at is when the run finished = $(sf last_success_at)" ;;
esac

# 18d. A data row can frame a COPY header. Two text columns holding `COPY public.notes (q`
# and `) FROM stdin;` emit one physical line that matched the header pattern, reset the row
# counter mid-block and failed verification on a perfectly restorable dump.
psql -q -d "$TESTDB" >/dev/null <<'SQL'
DROP TABLE IF EXISTS copy_decoy;
CREATE TABLE copy_decoy (a text, b text);
INSERT INTO copy_decoy VALUES ('COPY public.copy_decoy (q', ') FROM stdin;'), ('ordinary', 'row');
SQL
clear_backups
rc=0
run_backup >/dev/null 2>&1 </dev/null || rc=$?
read_status "a row that frames a COPY header"
assert_eq "18d a dump whose DATA frames a COPY header still verifies" "0" "$rc"
assert_eq "18d and is recorded as verified" "true" "$(sf verified)"

# 18e. The two sides of the row-count comparison keyed tables differently: the dump's side
# took pg_dump's `schema.table` verbatim, the database's side synthesised `'public.' ||
# relname`. Saiva has no non-public schema and no quoted identifier today, so the symptom
# would have been the first migration to add one making every nightly backup fail
# verification, every night, with a message about row counts and nothing about the cause.
psql -q -d "$TESTDB" >/dev/null <<'SQL'
CREATE SCHEMA IF NOT EXISTS review_audit;
CREATE TABLE IF NOT EXISTS review_audit.events (id serial primary key, note text);
INSERT INTO review_audit.events (note) SELECT 'e' || g FROM generate_series(1, 7) g;
CREATE TABLE IF NOT EXISTS "MixedCase Table" (id serial primary key, v int);
INSERT INTO "MixedCase Table" (v) VALUES (1), (2);
SQL
clear_backups
rc=0
run_backup >/dev/null 2>&1 </dev/null || rc=$?
read_status "a non-public schema and a quoted name"
assert_eq "18e a table outside public, and one needing quotes, still verify" "0" "$rc"
assert_eq "18e and are recorded as verified" "true" "$(sf verified)"
case "$(sf verify_message)" in
  *" rows across "*) ok "18e and their rows are counted = $(sf rows_checked)" ;;
  *) bad "18e the verification said something unexpected: $(sf verify_message)" ;;
esac
psql -q -d "$TESTDB" >/dev/null <<'SQL'
DROP SCHEMA IF EXISTS review_audit CASCADE;
DROP TABLE IF EXISTS "MixedCase Table";
DROP TABLE IF EXISTS copy_decoy;
SQL

# 18f. BLOCKING. A run that cannot take the lock used to write "skipped" over the record of
# the run that HOLDS it -- every status_set is a read-modify-write of one shared file, so a
# press of "Back up now" during a nightly run replaced that run's run_id, started_at and
# verdict. And because backup_begin ran before the lock, a run that went no further had
# already erased "a dump was taken but it could not be restored": the page went from red to
# a shrug while the carried-forward age still looked fine.
clear_backups
pg_dump -d "$TESTDB" --no-owner --no-privileges | gzip -c | head -c 900 \
  >"$BACKUPS/saiva-20261101-010101-nightly.sql.gz"
rc=0
run_backup --verify-only >/dev/null 2>&1 </dev/null || rc=$?
read_status "a dump that will not restore"
assert_eq "18f a truncated dump is recorded as not verified" "false" "$(sf verified)"
VERDICT_RUN_ID="$(sf run_id)"
VERDICT_STARTED="$(sf started_at)"
( flock 9; sleep 6 ) 9>"$STATE/deploy.lock" &
LOCK_HOLDER=$!
sleep 1
rc=0
run_backup --trigger button >"$WORK/locked.out" 2>&1 </dev/null || rc=$?
assert_eq "18f a press while the lock is held declines without taking anything" "75" "$rc"
read_status "after a press that could not get the lock"
assert_eq "18f the verdict is untouched" "false" "$(sf verified)"
assert_eq "18f phase is untouched" "failed" "$(sf phase)"
assert_eq "18f and so is the run it belongs to" "$VERDICT_RUN_ID" "$(sf run_id)"
assert_eq "18f and when that run started" "$VERDICT_STARTED" "$(sf started_at)"
wait "$LOCK_HOLDER" 2>/dev/null || true

# 18g. A signal is not an exit status. systemd's TimeoutStartSec, a host shutdown mid-dump
# or a Ctrl-C terminate the shell without any command having failed, so $? at the top of
# the EXIT trap was 0 -- and the record said "stopped without finishing (exit status 0)"
# beside phase failed, which reads as a contradiction.
clear_backups
: >"$STATE/backup-status.kv"
# The SCRIPT is backgrounded, not run_backup -- backgrounding the shell function forks a
# subshell, $! is that subshell, and `kill` then hit the wrapper while backup.sh ran to
# completion as its child. All three assertions below passed against a run that was never
# signalled and never failed. The environment is therefore inlined here rather than
# borrowed from run_backup.
SAIVA_BACKUP_DIRECT=1 \
SAIVA_ENV_FILE="$WORK/no-such.env" \
SAIVA_AGENT_CONF="$WORK/no-such.conf" \
SAIVA_STATE_DIR="$STATE" \
SAIVA_BACKUP_DIR="$BACKUPS" \
POSTGRES_DB="$TESTDB" \
POSTGRES_USER="$PGUSER" \
SAIVA_BACKUP_COPY_TO="" \
SAIVA_BACKUP_COPY_CMD="" \
  "$BACKUP" >/dev/null 2>&1 </dev/null &
KILL_ME=$!
# Wait for the run to be doing real work, then terminate it the way systemd would.
for _ in $(seq 1 60); do
  [ -s "$STATE/backup-status.kv" ] && grep -q '^step' "$STATE/backup-status.kv" && break
  sleep 0.2
done
if ! kill -0 "$KILL_ME" 2>/dev/null; then
  bad "18g the run finished before it could be signalled; this case proves nothing"
  note "it must still be running when the TERM arrives, or every assertion below is vacuous"
fi
kill -TERM "$KILL_ME" 2>/dev/null || true
SIGNALLED_RC=0
wait "$KILL_ME" 2>/dev/null || SIGNALLED_RC=$?
case "$SIGNALLED_RC" in
  143|1) ok "18g the run really was terminated by the signal = $SIGNALLED_RC" ;;
  0) bad "18g the run exited 0, so it was never signalled" ;;
  *) ok "18g the run was terminated = $SIGNALLED_RC" ;;
esac
read_status "a run terminated by a signal"
assert_eq "18g a terminated run is recorded as failed" "failed" "$(sf phase)"
case "$(sf exit_code)" in
  0) bad "18g a signal was recorded as exit_code 0, beside phase failed"
     note "a status nobody can act on: nothing failed, and yet it failed" ;;
  "" ) ok "18g and its exit code is left unknown rather than claimed as 0" ;;
  *) ok "18g and carries the real status = $(sf exit_code)" ;;
esac
case "$(sf message)" in
  *"exit status 0"*) bad "18g the message claims exit status 0 for a terminated run" ;;
  *) ok "18g and the message does not claim a status it does not have" ;;
esac

# 18h. `--trigger` with its value missing consumed the argument list, the loop's own shift
# then failed under set -e, and the script died before the dispatch -- exit 1, nothing on
# stdout, nothing on stderr, no record. A cron line with a typo in it would have looked
# like a backup regime that simply never ran.
rc=0
TRIGGER_OUT="$(run_backup --trigger 2>&1 </dev/null)" || rc=$?
assert_eq "18h --trigger with no value exits 2, like any other usage error" "2" "$rc"
case "$TRIGGER_OUT" in
  *"--trigger needs a value"*) ok "18h and says what it needs" ;;
  *) bad "18h it exited without saying why"; note "got: ${TRIGGER_OUT:-<nothing at all>}" ;;
esac

# ==========================================================================================
# 19. What a second adversarial review found: the prune's trust, and what a kill leaves
# ==========================================================================================

# 19a. BLOCKING. The prune decided what to keep by sorting FILENAMES in a directory the api
# container has read-write -- docker-compose.prod.yml mounts db_backups into api so the
# pre-migration dump can land there. Three files named `saiva-99999999-999999-nightly.sql.gz`
# therefore sorted newest, filled the keep window, and root deleted every real dump. The
# candidate list is now restricted to files root itself wrote (uid 0, mode 600), neither of
# which anything in a container can forge.
clear_backups
rc=0
run_backup >/dev/null 2>&1 </dev/null || rc=$?
read_status "a verified dump to protect"
assert_eq "19a a real verified dump to protect" "0" "$rc"
REAL_DUMP="$(sf dump_name)"
[ -n "$REAL_DUMP" ] || bad "19a the fixture produced no dump to protect"
# What a compromised api container could write: uid != 0, and not mode 600.
for n in 1 2 3; do
  printf 'not a dump, planted by something that is not root\n' | gzip -c \
    >"$BACKUPS/saiva-99999999-99999${n}-nightly.sql.gz"
  chmod 0644 "$BACKUPS/saiva-99999999-99999${n}-nightly.sql.gz"
done
rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=2 run_backup >/dev/null 2>&1 </dev/null || rc=$?
assert_eq "19a the run after the plant still succeeds" "0" "$rc"
if [ -f "$BACKUPS/$REAL_DUMP" ]; then
  ok "19a a planted newest-sorting name did not get the real dump deleted"
else
  bad "19a planted filenames made the prune delete a verified dump"
  note "the candidate list must be restricted to files root wrote: -user 0 -perm 600"
fi
for n in 1 2 3; do
  [ -f "$BACKUPS/saiva-99999999-99999${n}-nightly.sql.gz" ] \
    || bad "19a the prune deleted a file it did not write (the planted one)"
done
ok "19a and the planted files are left for the listing to report, not tidied away"

# The mode is one of the two properties the candidate list requires; the OWNER is the other,
# and it is the unforgeable one in production -- a dump written through `docker run` on
# postgres:16-alpine is owned by uid 0 while the api container is uid 10001. The cases above
# exercise the mode half, because --direct mode writes dumps as whoever runs this suite. The
# owner half needs a file owned by somebody else, which needs root to create.
if [ "$(id -u)" = "0" ] && id -u nobody >/dev/null 2>&1; then
  FOREIGN="saiva-99999999-999900-nightly.sql.gz"
  printf 'owned by somebody else, mode 0600\n' | gzip -c >"$BACKUPS/$FOREIGN"
  chown nobody "$BACKUPS/$FOREIGN"
  chmod 0600 "$BACKUPS/$FOREIGN"
  rc=0
  SAIVA_BACKUP_KEEP_NIGHTLY=1 run_backup >/dev/null 2>&1 </dev/null || rc=$?
  assert_eq "19a a run with a foreign 0600 dump planted still succeeds" "0" "$rc"
  if [ -f "$BACKUPS/$FOREIGN" ]; then
    ok "19a a 0600 dump owned by another uid is not a prune candidate either"
  else
    bad "19a the prune deleted a dump it did not write, identified only by its mode"
    note "the candidate list needs -user as well as -perm: the mode is forgeable from a container"
  fi
  rm -f "$BACKUPS/$FOREIGN"
else
  note "19a skipped the owner half: it needs root to create a file owned by another uid"
fi

# 19b. SIGKILL runs no trap. The OOM killer, `systemctl stop` past TimeoutStopSec and a
# power cut all deliver one, and what was left behind was a complete copy of the ledger in
# a throwaway database and another in a staged file, which nothing would ever reclaim --
# the next run makes its own under a different name. Every run now sweeps first.
clear_backups
psql -qc "CREATE DATABASE \"saiva_verify_19700101000000_1\"" -d postgres >/dev/null 2>&1 || true
install -d -m 0700 "$STATE/staging"
: >"$STATE/staging/.offsite.999999"
rc=0
run_backup >"$WORK/sweep.out" 2>&1 </dev/null || rc=$?
assert_eq "19b the run succeeds with orphans to clear" "0" "$rc"
assert_eq "19b an abandoned verification database from an earlier run is dropped" \
  "0" "$(psql -At -d postgres -c "select count(*) from pg_database where datname = 'saiva_verify_19700101000000_1'")"
assert_eq "19b and an abandoned staged copy of the ledger is removed" \
  "0" "$(find "$STATE/staging" -maxdepth 1 -name '.offsite.*' 2>/dev/null | wc -l)"
case "$(cat "$WORK/sweep.out")" in
  *"abandoned"*) ok "19b and it says what it cleared, rather than doing it silently" ;;
  *) bad "19b the sweep was silent"; note "a dropped database is worth a line in the log" ;;
esac

# 19c. The staged copy is a complete plaintext ledger. $STATE is 0755 and bind-mounted into
# the caddy container, which runs as root in its own image -- so a 0600 root file sitting
# directly in $STATE is readable from inside it. It lives in a 0700 subdirectory now, the
# same treatment records/ already gets.
assert_eq "19c the staging directory is 0700" "700" "$(stat -c '%a' "$STATE/staging" 2>/dev/null || echo missing)"

# 19d. SERIOUS. `cp` writes THROUGH a symlink at the destination, and the chmod that
# follows follows it too: root would write the household ledger wherever the link points
# and relax that file to 0600. The destination is root-configured, but agent.conf
# recommends exactly the places other parties can write, and a dump name is a stamp anyone
# can predict a second at a time.
OFFBOX="$WORK/offbox-link"
mkdir -p "$OFFBOX"
printf 'IMPORTANT HOST FILE - do not clobber\n' >"$WORK/offbox-victim"
VICTIM_SUM="$(md5sum <"$WORK/offbox-victim")"
clear_backups
rc=0
run_backup >/dev/null 2>&1 </dev/null || rc=$?
LINK_TARGET_NAME="$(sf dump_name)"
clear_backups
# Re-take the dump with the destination configured, having planted a link under the name
# the next dump will have. The stamp is the second it is taken, so the test plants a link
# for every second in the next minute -- which is what an attacker able to write the
# destination would do.
for off in $(seq 0 90); do
  n="saiva-$(date -u -d "+${off} seconds" +%Y%m%d-%H%M%S)-nightly.sql.gz"
  ln -sfn "$WORK/offbox-victim" "$OFFBOX/$n"
done
rc=0
run_backup_to "$OFFBOX" "" >"$WORK/link.out" 2>&1 </dev/null || rc=$?
read_status "an off-box destination holding a planted symlink"
assert_eq "19d the victim outside the destination is untouched" \
  "$VICTIM_SUM" "$(md5sum <"$WORK/offbox-victim")"
assert_eq "19d the backup itself still succeeds -- the dump is verified and local" "0" "$rc"
assert_eq "19d and the copy is recorded as failed, not as success" "failed" "$(sf offsite)"
case "$(sf offsite_message)" in
  *symlink*) ok "19d and it says a link is why nothing was written" ;;
  *) bad "19d the refusal does not name the cause"; note "got: $(sf offsite_message)" ;;
esac
rm -rf "$OFFBOX"

# 19e. A run that fails verification asserted "nothing was pruned" in its own message, and
# the pre-dump prune had already run. Say what the record says instead.
case "$(printf '%s' "$LINK_TARGET_NAME")" in
  saiva-*) ok "19e (a dump was taken for 19d to work with = $LINK_TARGET_NAME)" ;;
  *) bad "19e the 19d fixture did not produce a dump" ;;
esac
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_e\" WITH (FORCE)" -d postgres >/dev/null
psql -qc "CREATE DATABASE \"${TESTDB}_e\"" -d postgres >/dev/null
clear_backups
for d in 01 02 03 04; do
  plant_nightly "saiva-202602${d}-000000-nightly.sql.gz"
  chmod 0600 "$BACKUPS/saiva-202602${d}-000000-nightly.sql.gz"
done
rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=2 run_backup_as "${TESTDB}_e" >/dev/null 2>&1 </dev/null || rc=$?
read_status "a failed verification after a prune"
assert_eq "19e a dump of a non-Saiva database fails" "1" "$rc"
case "$(sf message)" in
  *"nothing was pruned"*)
    bad "19e the failure says nothing was pruned, and the pre-dump prune had already run" ;;
  *"already been retired"*|*"nothing was retired before it"*)
    ok "19e the failure reports the retirement that actually happened" ;;
  *) bad "19e the failure says nothing about the prune either way"
     note "got: $(sf message)" ;;
esac
# And the SHAPE OF THE VOLUME has to describe it as it now is. survey_dumps used to run
# only on the success paths, so after a prune followed by a failed verification the record
# carried the previous run's figures -- and the page named as the oldest surviving dump a
# file this run had just deleted, and overstated what was left. Found by review.
assert_eq "19e the count describes the volume as it NOW is" \
  "$(nightly_dumps | wc -l)" "$(sf backup_count)"
OLDEST_LEFT="$(nightly_dumps | head -n 1)"
case "$(sf oldest_at)" in
  "") bad "19e oldest_at was not recorded on the failure path" ;;
  *) if printf '%s' "$OLDEST_LEFT" | grep -q "$(printf '%s' "$(sf oldest_at)" | cut -c1-4)"; then
       ok "19e and oldest_at names a dump that still exists = $(sf oldest_at)"
     else
       bad "19e oldest_at names a dump this run deleted: $(sf oldest_at), oldest left is $OLDEST_LEFT"
     fi ;;
esac
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_e\" WITH (FORCE)" -d postgres >/dev/null

# 19f. BLOCKING. `--verify-only` copies nothing, so leaving the off-box group in place put
# "a copy of it left this machine" beside a re-verified dump that had never been copied
# anywhere: the verdict was about one dump and the headline about another.
clear_backups
rc=0
run_backup_to "$WORK/offbox-ok" "" >/dev/null 2>&1 </dev/null || rc=$?
mkdir -p "$WORK/offbox-ok"
read_status "a run that did copy off-box"
OFFSITE_AFTER_COPY="$(sf offsite)"
rc=0
run_backup --verify-only >/dev/null 2>&1 </dev/null || rc=$?
read_status "a --verify-only after a run that copied"
assert_eq "19f --verify-only succeeds" "0" "$rc"
assert_eq "19f and leaves no off-box verdict about a dump it never copied" "<null>" "$(sf offsite)"
case "$OFFSITE_AFTER_COPY" in
  "<null>"|"") note "19f (the preceding run recorded no off-box state, so this is weak)" ;;
  *) ok "19f (the preceding run had recorded offsite=${OFFSITE_AFTER_COPY})" ;;
esac
rm -rf "$WORK/offbox-ok"

# ==========================================================================================
# 20. Four guards that were only pinned by greps, and one that was not pinned at all
# ==========================================================================================
#
# A third review mutated the implementation and ran this suite against each mutation. These
# five survived -- the suite stayed green while the thing each of them protects was broken.

# 20a. `[ "$KEEP_NIGHTLY" -ge 1 ] || KEEP_NIGHTLY=1` is what stops
# SAIVA_BACKUP_KEEP_NIGHTLY=0 in agent.conf meaning "delete the backup you just verified".
# The suite used 5, 3, 2 and 1 and never 0, so the only guard was a grep for the literal
# `KEEP_NIGHTLY=1` in backend/tests/test_backup_install.py -- which a mutation that changed
# `-ge 1` to `-ge 0` left intact.
clear_backups
rc=0
SAIVA_BACKUP_KEEP_NIGHTLY=0 run_backup >/dev/null 2>&1 </dev/null || rc=$?
read_status "retention configured as zero"
assert_eq "20a a retention of 0 still succeeds" "0" "$rc"
assert_eq "20a and is floored at 1, never 0" "1" "$(sf kept)"
ZERO_DUMP="$(sf dump_name)"
if [ -n "$ZERO_DUMP" ] && [ -f "$BACKUPS/$ZERO_DUMP" ]; then
  ok "20a and the dump this run just verified is still on disk"
else
  bad "20a a retention of 0 deleted the backup the same run had just verified"
fi
# The same floor has to hold for a value that is not a number at all.
clear_backups
rc=0
SAIVA_BACKUP_KEEP_NIGHTLY="not a number" run_backup >/dev/null 2>&1 </dev/null || rc=$?
read_status "retention configured as nonsense"
assert_eq "20a a nonsense retention still succeeds" "0" "$rc"
assert_eq "20a and falls back to the default, not to zero" "14" "$(sf kept)"

# 20b. conf_is_trustworthy rejects agent.conf unless root owns it AND nobody else can write
# it, because SAIVA_BACKUP_COPY_CMD from that file runs as root under the nightly timer.
# Section 11 varied only the MODE, so a mutation that trusted a conf owned by anybody kept
# the suite green. The owner half needs a file owned by somebody else, which needs root.
if [ "$(id -u)" = "0" ] && id -u nobody >/dev/null 2>&1; then
  clear_backups
  FOREIGN_CONF="$WORK/foreign-agent.conf"
  FOREIGN_DEST="$WORK/foreign-dest"
  mkdir -p "$FOREIGN_DEST"
  printf 'SAIVA_BACKUP_COPY_TO=%s
SAIVA_BACKUP_KEEP_NIGHTLY=3
' "$FOREIGN_DEST" >"$FOREIGN_CONF"
  chown nobody "$FOREIGN_CONF"
  chmod 0644 "$FOREIGN_CONF"
  assert_eq "20b the conf really is owned by somebody other than root" \
    "nobody" "$(stat -c '%U' "$FOREIGN_CONF")"
  rc=0
  SAIVA_BACKUP_DIRECT=1 \
  SAIVA_ENV_FILE="$WORK/no-such.env" \
  SAIVA_AGENT_CONF="$FOREIGN_CONF" \
  SAIVA_STATE_DIR="$STATE" \
  SAIVA_BACKUP_DIR="$BACKUPS" \
  POSTGRES_DB="$TESTDB" \
  POSTGRES_USER="$PGUSER" \
    "$BACKUP" >/dev/null 2>&1 </dev/null || rc=$?
  read_status "an agent.conf owned by somebody other than root"
  assert_eq "20b the run still succeeds" "0" "$rc"
  assert_eq "20b but the conf's destination is NOT acted on" "off" "$(sf offsite)"
  assert_eq "20b nor its retention" "14" "$(sf kept)"
  assert_eq "20b and nothing reached the destination it named" \
    "0" "$(find "$FOREIGN_DEST" -maxdepth 1 -name '*.sql.gz' | wc -l)"
  rm -rf "$FOREIGN_DEST" "$FOREIGN_CONF"
else
  note "20b skipped: it needs root to create a conf owned by another uid"
fi

# 20c. _offsite_worsen's rule is that the record carries the LEAST protective outcome, so a
# destination that stopped working is visible even when another one is fine. No test
# configured both, so reversing the rank comparison kept the suite green.
clear_backups
BOTH_DEST="$WORK/both-dest"
mkdir -p "$BOTH_DEST"
rc=0
run_backup_to "$BOTH_DEST" "exit 3" >/dev/null 2>&1 </dev/null || rc=$?
read_status "a working directory beside a failing command"
assert_eq "20c the backup itself still succeeds" "0" "$rc"
assert_eq "20c and the WORST of the two outcomes is what the record carries" "failed" "$(sf offsite)"
case "$(sf offsite_message)" in
  *"$BOTH_DEST"*) ok "20c and the message still names the destination that did work" ;;
  *) bad "20c the message lost the destination that worked"; note "got: $(sf offsite_message)" ;;
esac
# The mirror: a same-filesystem directory beside a command that succeeds. `remote` is the
# better of the two and must NOT be the headline.
clear_backups
rc=0
run_backup_to "$BOTH_DEST" "true" >/dev/null 2>&1 </dev/null || rc=$?
read_status "a same-filesystem directory beside a working command"
assert_eq "20c the mirror case succeeds too" "0" "$rc"
assert_eq "20c and same-filesystem still wins over remote" "same-filesystem" "$(sf offsite)"
rm -rf "$BOTH_DEST"

# 20d. verify_dump fails a dump whose restored copy has no row in alembic_version, on the
# stated ground that a dump which cannot say which schema it is cannot be restored with any
# confidence. Nothing tested it: replacing the check with `if false` kept the suite green.
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_norev\" WITH (FORCE)" -d postgres >/dev/null
psql -qc "CREATE DATABASE \"${TESTDB}_norev\"" -d postgres >/dev/null
psql -q -d "${TESTDB}_norev" >/dev/null <<'SQL'
CREATE TABLE households (id serial primary key, name text);
CREATE TABLE accounts (id serial primary key, household_id int references households(id));
CREATE TABLE transactions (id serial primary key, account_id int references accounts(id), cents bigint);
CREATE TABLE alembic_version (version_num varchar(32) primary key);
INSERT INTO households (name) VALUES ('a real household');
INSERT INTO accounts (household_id) VALUES (1);
INSERT INTO transactions (account_id, cents) SELECT 1, g FROM generate_series(1, 20) g;
SQL
clear_backups
rc=0
run_backup_as "${TESTDB}_norev" >/dev/null 2>&1 </dev/null || rc=$?
read_status "a Saiva database with no schema revision"
assert_eq "20d a dump with no schema revision fails" "1" "$rc"
assert_eq "20d and is NOT recorded as verified" "false" "$(sf verified)"
case "$(sf verify_message)" in
  *alembic_version*|*"schema revision"*) ok "20d and the message names what is missing" ;;
  *) bad "20d the message does not say the schema revision is missing"
     note "got: $(sf verify_message)" ;;
esac
psql -qc "DROP DATABASE IF EXISTS \"${TESTDB}_norev\" WITH (FORCE)" -d postgres >/dev/null

# --- summary -----------------------------------------------------------------------------

printf '\n  %d passed, %d failed\n\n' "$PASS" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
