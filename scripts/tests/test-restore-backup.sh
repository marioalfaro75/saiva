#!/usr/bin/env bash
# Round-trip test for scripts/restore-backup.sh against a real Postgres.
#
# The only test that proves a restore procedure works is one that loses data and gets it
# back. So this builds a database that looks like Saiva's (an enum type, serial primary
# keys, a foreign key, an index, an alembic_version row), dumps it exactly the way
# backend/app/dbinit.py does, writes MORE rows and adds a table on top -- which is what a
# release-plus-a-day-of-use looks like -- then restores the dump and asserts that the
# later writes are GONE and the earlier ones are BACK.
#
# It then restores the safety dump that restore-backup.sh took on its way past, and
# asserts the later writes come back. That is the half of the procedure that usually
# exists only as a promise: a restore that cannot itself be undone is a second way to lose
# the ledger, not a recovery from the first.
#
# No Docker. restore-backup.sh's --direct mode swaps the runners that every Postgres
# command goes through for plain `sh -c`, so everything below -- the listing, the
# confirmation, the drop and recreate, the single-transaction load, the verification -- is
# the same code production runs, with psql and pg_dump taken off PATH instead of out of a
# container. What --direct does NOT cover is the two `docker run` command lines themselves,
# one in _pg_sh and one in backups_sh; they are the only part of restore-backup.sh a test
# on a host with no Docker cannot execute. The two names inside them that can rot silently
# are checked instead, in section 1b.
#
# Point it at a Postgres with PGHOST/PGPORT/PGUSER. The defaults are the instance this
# repository's sandbox runs. Exits 77 if there is no Postgres to talk to, so a runner can
# tell "cannot run here" from "the restore is broken".

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RESTORE="$ROOT/scripts/restore-backup.sh"

export PGHOST="${PGHOST:-/var/tmp}"
export PGPORT="${PGPORT:-55432}"
export PGUSER="${PGUSER:-saiva}"

TESTDB="saiva_restore_test_$$"
WORK="$(mktemp -d)"
BACKUPS="$WORK/backups"
mkdir -p "$BACKUPS"

PASS=0
FAILED=0

# Matches both shapes of safety-dump name: the plain one, and the numbered one the script
# falls back to when a dump already holds that second's name. Two runs landing in the same
# second is normal rather than exotic -- it is what "restore the wrong dump, then restore
# the safety dump" looks like -- so a pattern that only matched the plain name made this
# test fail about one run in five while the script was behaving correctly.
PRERESTORE_RE='saiva-[0-9]{8}-[0-9]{6}-prerestore(-[0-9]+)?\.sql\.gz'

ok()   { printf '  ok    %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
note() { printf '        %s\n' "$*"; }

assert_eq() { # assert_eq <label> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1 = $3"; else bad "$1: expected [$2], got [$3]"; fi
}

cleanup() {
  psql -d postgres -qc "DROP DATABASE IF EXISTS \"$TESTDB\" WITH (FORCE)" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

q() { psql -X -At -q -d "$TESTDB" -c "$1"; }

# restore-backup.sh in --direct mode, pointed at the scratch database and scratch dumps,
# with an env file that does not exist so that a developer's real .env cannot reach in and
# change which database this test drops.
run_restore() {
  SAIVA_RESTORE_DIRECT=1 \
  SAIVA_ENV_FILE="$WORK/no-such.env" \
  SAIVA_BACKUP_DIR="$BACKUPS" \
  POSTGRES_DB="$TESTDB" \
  POSTGRES_USER="$PGUSER" \
  "$RESTORE" "$@"
}

printf '\nrestore-backup.sh round trip against %s:%s\n\n' "$PGHOST" "$PGPORT"

# --- 0. is there a Postgres at all ------------------------------------------------------

if ! psql -X -At -q -d postgres -c 'select 1' >/dev/null 2>&1; then
  printf '  cannot reach Postgres as %s at %s:%s -- nothing to test against.\n' \
    "$PGUSER" "$PGHOST" "$PGPORT"
  printf '  Set PGHOST/PGPORT/PGUSER to a Postgres 16 this user can create databases on.\n\n'
  exit 77
fi

# --- 1. the dump format this script assumes is still the format dbinit writes -----------
#
# restore-backup.sh is `gunzip -c | psql`, which is correct only while dbinit's pg_dump
# stays in plain format. If someone adds -Fc for the speed, every dump written from then
# on becomes an archive that psql cannot read, and the restore path breaks silently --
# there is no error until the night it is needed. This guard turns that into a failing
# test on the commit that causes it.
DBINIT="$ROOT/backend/app/dbinit.py"
if grep -qE '"-F|--format' "$DBINIT"; then
  bad "dbinit.py now passes a pg_dump format flag; restore-backup.sh expects plain SQL"
  note "if the dumps became archives, the restore has to use pg_restore, not psql"
else
  ok "dbinit.py still dumps in plain format (no -F/--format)"
fi
for flag in --no-owner --no-privileges; do
  if grep -q -- "\"$flag\"" "$DBINIT"; then
    ok "dbinit.py dumps with $flag"
  else
    bad "dbinit.py no longer passes $flag; this test no longer dumps the way it does"
  fi
done
if grep -q 'gzip.open' "$DBINIT"; then
  ok "dbinit.py still gzips the dump"
else
  bad "dbinit.py no longer gzips; the .sql.gz name restore-backup.sh looks for may be wrong"
fi

# --- 1b. the docker-run path joins the network compose actually creates -----------------
#
# The two `docker run` lines are the one part of restore-backup.sh this suite cannot
# execute, and the two values in them are derived, not literal: <project>_default and
# <project>_db_backups. docker-compose.prod.yml pins the project with `name: saiva` and
# declares no networks of its own, so `docker compose config` reports exactly one network
# -- default, created as saiva_default -- and the volumes caddy_config, caddy_data,
# db_backups and db_data. A networks: block added to that file later, or a change to
# `name:`, would send the restore container to a network the db service is not on, and it
# would fail with "network not found" on the one night it is needed.
COMPOSE="$ROOT/docker-compose.prod.yml"
if grep -qE '^networks:' "$COMPOSE"; then
  bad "docker-compose.prod.yml now declares networks of its own"
  note "restore-backup.sh joins <project>_default, which is only right while compose creates just that"
else
  ok "docker-compose.prod.yml declares no networks, so <project>_default is the one compose creates"
fi
if grep -qE '^name: saiva$' "$COMPOSE"; then
  ok "the compose project is pinned to 'saiva', which is what SAIVA_PROJECT defaults to"
else
  bad "docker-compose.prod.yml no longer pins 'name: saiva'; the derived volume name may be wrong"
fi
if grep -q 'NETWORK="${PROJECT}_default"' "$RESTORE"; then
  ok "restore-backup.sh runs its container on <project>_default"
else
  bad "restore-backup.sh no longer joins <project>_default"
fi
if grep -q 'VOLUME="${PROJECT}_db_backups"' "$RESTORE"; then
  ok "and mounts <project>_db_backups, where the dumps are"
else
  bad "restore-backup.sh no longer mounts <project>_db_backups"
fi
if grep -qE '^ *- db_backups:/backups$' "$COMPOSE"; then
  ok "db_backups is still mounted into the api service at /backups"
else
  bad "db_backups is no longer mounted at /backups; the dump names in the listing may be wrong"
fi

# --- 2. a database that looks like the real one ------------------------------------------

psql -qc "DROP DATABASE IF EXISTS \"$TESTDB\" WITH (FORCE)" -d postgres >/dev/null
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
INSERT INTO transactions (account_id, posted_on, payee, amount) VALUES
  (1, '2026-10-01', 'Woolworths', -84.30),
  (1, '2026-10-02', 'Salary',    4210.00),
  (2, '2026-10-03', 'Bunnings',   -62.95);
SQL

BEFORE_TX="$(q 'select count(*) from transactions')"
assert_eq "rows before the dump" "3" "$BEFORE_TX"

# --- 3. dump it exactly the way dbinit.py does -------------------------------------------
#
# Same argv as backend/app/dbinit.py _pg_dump_cmd(), then gzipped, then given the name
# dbinit builds: saiva-<UTC stamp>-premigrate.sql.gz.
STAMP="$(date -u +%Y%m%d-%H%M%S)"
DUMP="saiva-${STAMP}-premigrate.sql.gz"
pg_dump --host "$PGHOST" --port "$PGPORT" --username "$PGUSER" --dbname "$TESTDB" \
        --no-owner --no-privileges --file "$BACKUPS/saiva-${STAMP}-premigrate.sql"
gzip "$BACKUPS/saiva-${STAMP}-premigrate.sql"
[ -s "$BACKUPS/$DUMP" ] && ok "dump written: $DUMP ($(stat -c %s "$BACKUPS/$DUMP") bytes)" \
                        || bad "dump was not written"

# --- 4. a day of use on top of it, including a migration ---------------------------------
#
# A table the dump has never heard of is the case that separates a real restore from
# replaying the dump on top of what is there: replaying leaves `goals` behind, and the
# database ends up a mixture of two schema eras with alembic_version claiming the older.
psql -X -q -v ON_ERROR_STOP=1 -d "$TESTDB" >/dev/null <<'SQL'
INSERT INTO transactions (account_id, posted_on, payee, amount) VALUES
  (1, '2026-11-01', 'Rent',      -1850.00),
  (1, '2026-11-02', 'Coles',       -51.40),
  (2, '2026-11-03', 'Petrol',      -78.10),
  (1, '2026-11-04', 'Interest',      2.15);
CREATE TABLE goals (
  id     serial PRIMARY KEY,
  name   text NOT NULL,
  target numeric(14,2) NOT NULL
);
INSERT INTO goals (name, target) VALUES ('Roof', 9000.00);
UPDATE alembic_version SET version_num = '0004';
SQL

assert_eq "rows after a day of use" "7" "$(q 'select count(*) from transactions')"
assert_eq "alembic_version after the migration" "0004" "$(q 'select version_num from alembic_version')"

# --- 5. listing works, and names the dump ------------------------------------------------

LIST_OUT="$WORK/list.out"
rc=0
run_restore >"$LIST_OUT" 2>&1 </dev/null || rc=$?
assert_eq "listing exits 0" "0" "$rc"
grep -q "$DUMP" "$LIST_OUT" && ok "listing names the dump" || { bad "listing does not name the dump"; cat "$LIST_OUT"; }
grep -q 'premigrate' "$LIST_OUT" && ok "listing explains what premigrate means" || bad "listing does not explain the kinds"

# --- 6. it refuses to restore with nobody watching ---------------------------------------

rc=0
run_restore "$DUMP" >"$WORK/notty.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "refuses with no TTY and no override (exit $rc)" \
                || bad "restored with no TTY and no confirmation"
grep -q 'not a terminal' "$WORK/notty.out" && ok "says why it refused" || bad "refusal does not say why"
grep -q 'SAIVA_CONFIRM_RESTORE' "$WORK/notty.out" && ok "refusal names the override" || bad "refusal hides the override"
assert_eq "database untouched by the refusal" "7" "$(q 'select count(*) from transactions')"

rc=0
SAIVA_CONFIRM_RESTORE=yes run_restore "$DUMP" >"$WORK/wordy.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "refuses an override that is not the word (exit $rc)" \
                || bad "a truthy SAIVA_CONFIRM_RESTORE was accepted as the word"
assert_eq "database untouched by the wrong word" "7" "$(q 'select count(*) from transactions')"

# --- 7. it refuses names that are not dumps ----------------------------------------------

for bogus in 'saiva-20260101-000000-premigrate.sql.gz' 'notadump.txt' 'bad name.sql.gz' '$(id).sql.gz'; do
  rc=0
  SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$bogus" >"$WORK/bogus.out" 2>&1 </dev/null || rc=$?
  [ "$rc" -ne 0 ] && ok "refuses [$bogus] (exit $rc)" || bad "accepted [$bogus]"
  grep -q 'uid=' "$WORK/bogus.out" && bad "a crafted dump name reached a shell" || true
done
grep -q 'uid=' "$WORK/bogus.out" || ok "a crafted dump name never reached a shell"

: > "$BACKUPS/saiva-20260102-030405-premigrate.sql.gz"
rc=0
SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore 'saiva-20260102-030405-premigrate.sql.gz' \
  >"$WORK/empty.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "refuses a zero-byte dump (exit $rc)" || bad "accepted a zero-byte dump"
rm -f "$BACKUPS/saiva-20260102-030405-premigrate.sql.gz"
assert_eq "database untouched by the bad names" "7" "$(q 'select count(*) from transactions')"

# --- 8. THE ROUND TRIP -------------------------------------------------------------------

rc=0
SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$DUMP" >"$WORK/restore.out" 2>&1 </dev/null || rc=$?
if [ "$rc" -ne 0 ]; then
  bad "restore exited $rc"
  cat "$WORK/restore.out"
else
  ok "restore exited 0"
fi

assert_eq "transactions back to the dump's count"  "3"    "$(q 'select count(*) from transactions')"
assert_eq "alembic_version back to the dump's era" "0003" "$(q 'select version_num from alembic_version')"
assert_eq "the later rows are gone"                "0"    "$(q "select count(*) from transactions where posted_on >= '2026-11-01'")"
assert_eq "the earlier rows are back"              "3"    "$(q "select count(*) from transactions where posted_on < '2026-11-01'")"
assert_eq "Salary row is back, to the cent"        "4210.00" "$(q "select amount from transactions where payee = 'Salary'")"
assert_eq "the newer table is gone"                "f"    "$(q "select to_regclass('public.goals') is not null")"
assert_eq "the enum type came back"                "t"    "$(q "select exists (select 1 from pg_type where typname = 'account_kind')")"
assert_eq "the index came back"                    "t"    "$(q "select to_regclass('public.accounts_household_idx') is not null")"
assert_eq "serial sequences came back in step"     "3"    "$(q "select last_value from transactions_id_seq")"

grep -q 'match' "$WORK/restore.out" && ok "it reported the alembic check as a match" \
                                    || { bad "no alembic match line in the report"; cat "$WORK/restore.out"; }
grep -q 'households table *present' "$WORK/restore.out" && ok "it reported the schema marker" \
                                    || bad "no schema marker line in the report"

# --- 9. the restore was itself reversible ------------------------------------------------

SAFETY="$(cd "$BACKUPS" && ls -1 ./*-prerestore*.sql.gz 2>/dev/null | sed 's|^\./||' | tail -n 1 || true)"
if [ -z "$SAFETY" ]; then
  bad "no prerestore dump was written, so the restore was not reversible"
else
  ok "a prerestore dump was written: $SAFETY"
  gzip -t "$BACKUPS/$SAFETY" && ok "the prerestore dump is a readable gzip" \
                             || bad "the prerestore dump is not readable"
  grep -q "$SAFETY" "$WORK/restore.out" && ok "the report names it" || bad "the report does not name it"

  rc=0
  SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$SAFETY" >"$WORK/undo.out" 2>&1 </dev/null || rc=$?
  [ "$rc" -eq 0 ] && ok "restoring the prerestore dump exited 0" \
                  || { bad "undo restore exited $rc"; cat "$WORK/undo.out"; }

  assert_eq "undo brought the later rows back"  "7"    "$(q 'select count(*) from transactions')"
  assert_eq "undo brought the newer table back" "t"    "$(q "select to_regclass('public.goals') is not null")"
  assert_eq "undo brought the newer era back"   "0004" "$(q 'select version_num from alembic_version')"
fi

# --- 10. --no-safety-dump says out loud that it is irreversible ---------------------------

rc=0
run_restore --no-safety-dump "$DUMP" >"$WORK/nosafety.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "--no-safety-dump still needs a confirmation (exit $rc)" \
                || bad "--no-safety-dump skipped the confirmation too"
grep -qi 'irreversible' "$WORK/nosafety.out" && ok "--no-safety-dump warns it is irreversible" \
                || { bad "--no-safety-dump does not warn"; cat "$WORK/nosafety.out"; }
assert_eq "no dump was replaced" "7" "$(q 'select count(*) from transactions')"

# --- 11. a restore that dies half way says what state the database is in -----------------
#
# Point 5 of the task this script was written for: every exit path has to tell the owner
# where the database now stands. The paths that are easy to get right are the ones that
# fail before touching anything. The one that matters is the path that fails AFTER the
# database has been dropped, because that is the moment the owner has nothing and needs to
# be told, in that message, how to get back.
#
# Forced with a dump that is a valid gzip, parses as plain SQL, carries an
# alembic_version row -- so it passes every check made before the destructive step -- and
# then errors part way through the load.
BROKEN="saiva-20260301-121212-premigrate.sql.gz"
cat >"$WORK/broken.sql" <<'SQL'
CREATE TABLE households (id integer NOT NULL);
CREATE TABLE alembic_version (version_num varchar(32) NOT NULL);
COPY public.alembic_version (version_num) FROM stdin;
0099
\.
SELECT 1 / 0;
SQL
gzip -c "$WORK/broken.sql" >"$BACKUPS/$BROKEN"

rc=0
SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$BROKEN" >"$WORK/broken.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "a dump that errors mid-load fails the script (exit $rc)" \
                || bad "a dump that errors mid-load was reported as a success"
grep -q 'STOP AND READ THIS' "$WORK/broken.out" && ok "it says to stop and read" \
                || { bad "no loud warning after a destructive failure"; cat "$WORK/broken.out"; }
grep -qi 'does not hold the data it held before' "$WORK/broken.out" \
  && ok "it says the database no longer holds what it held" \
  || bad "it does not say what state the database is in"

assert_eq "the database really is empty now" "0" \
  "$(q "select count(*) from information_schema.tables where table_schema = 'public'")"

# The recovery command it printed has to be the one that actually works -- read it out of
# the output and run it, rather than reconstructing it here from what it ought to say.
# `|| true` because `grep | tail` under `set -o pipefail` reports grep's failure, so a
# no-match would kill the test here instead of reaching the report below.
RECOVER="$(grep -oE "$PRERESTORE_RE" "$WORK/broken.out" | tail -n 1 || true)"
if [ -z "$RECOVER" ]; then
  bad "the failure message does not name a dump to recover from"
else
  ok "the failure message names the dump to recover from: $RECOVER"
  rc=0
  SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$RECOVER" >"$WORK/recover.out" 2>&1 </dev/null || rc=$?
  [ "$rc" -eq 0 ] && ok "the recovery it printed exits 0" \
                  || { bad "the printed recovery failed (exit $rc)"; cat "$WORK/recover.out"; }
  assert_eq "recovered the ledger"      "7"    "$(q 'select count(*) from transactions')"
  assert_eq "recovered the newer table" "t"    "$(q "select to_regclass('public.goals') is not null")"
  assert_eq "recovered the era"         "0004" "$(q 'select version_num from alembic_version')"
fi
rm -f "$BACKUPS/$BROKEN"

# --- 12. the safety dump never overwrites an existing dump -------------------------------
#
# The stamp in a dump's name has one-second resolution, and the sequence this feature
# exists for -- restore the wrong dump, realise, restore the safety dump -- is two runs
# seconds apart. When they land in the same second, a safety dump that overwrites is the
# worst thing this script could do: the second run would write a dump of the BROKEN
# database over the dump of the good one, under the name the first run had just told the
# owner to restore, and then restore it. The ledger would be gone with every message along
# the way reporting success.
#
# Forced rather than raced: every name the next safety dump could possibly take, for the
# next few seconds, is occupied first, so the no-clobber path has to run.
BEFORE_LIST="$WORK/prerestore.before"
AFTER_LIST="$WORK/prerestore.after"
(cd "$BACKUPS" && ls -1 ./*-prerestore*.sql.gz 2>/dev/null | sort) >"$BEFORE_LIST" || true

DECOYS=()
for offset in 0 1 2 3 4 5; do
  decoy="saiva-$(date -u -d "+${offset} seconds" +%Y%m%d-%H%M%S)-prerestore.sql.gz"
  printf 'not a dump, do not overwrite me\n' | gzip -c >"$BACKUPS/$decoy"
  DECOYS+=("$decoy")
done
DECOY_SUMS="$(cd "$BACKUPS" && md5sum "${DECOYS[@]}" | sort)"

rc=0
SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$DUMP" >"$WORK/clobber.out" 2>&1 </dev/null || rc=$?
[ "$rc" -eq 0 ] && ok "restore still succeeds when the obvious safety-dump name is taken" \
                || { bad "restore failed with the safety-dump name taken (exit $rc)"; cat "$WORK/clobber.out"; }

if [ "$DECOY_SUMS" = "$(cd "$BACKUPS" && md5sum "${DECOYS[@]}" | sort)" ]; then
  ok "it overwrote none of the ${#DECOYS[@]} dumps already holding those names"
else
  bad "the safety dump overwrote an existing dump"
fi

(cd "$BACKUPS" && ls -1 ./*-prerestore*.sql.gz 2>/dev/null | sort) >"$AFTER_LIST" || true
NEWFILES="$(comm -13 "$BEFORE_LIST" "$AFTER_LIST" | grep -vF -- "$(printf '%s\n' "${DECOYS[@]}" | sed 's|^|./|')" || true)"
# comm's output keeps the ./ prefix ls printed; strip it for the name checks below.
NEWSAFETY="$(printf '%s\n' "$NEWFILES" | sed 's|^\./||' | grep -E "^${PRERESTORE_RE}\$" || true)"
NEWCOUNT="$(printf '%s\n' "$NEWSAFETY" | grep -c . || true)"
assert_eq "exactly one new safety dump appeared" "1" "$NEWCOUNT"
if [ "$NEWCOUNT" = "1" ]; then
  case "$NEWSAFETY" in
    *-prerestore-[0-9]*.sql.gz) ok "it took a numbered name instead: $NEWSAFETY" ;;
    *) bad "expected a numbered name, got $NEWSAFETY" ;;
  esac
  gzip -t "$BACKUPS/$NEWSAFETY" && ok "the numbered safety dump is a readable gzip" \
                               || bad "the numbered safety dump is not readable"
  # And it has to be restorable under that name, or the no-clobber fix would have moved
  # the data somewhere the script cannot reach.
  assert_eq "the restore did roll the database back" "3" "$(q 'select count(*) from transactions')"
  rc=0
  SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$NEWSAFETY" >"$WORK/numbered.out" 2>&1 </dev/null || rc=$?
  [ "$rc" -eq 0 ] && ok "the numbered safety dump restores" \
                  || { bad "the numbered safety dump does not restore (exit $rc)"; cat "$WORK/numbered.out"; }
  assert_eq "and it carried the pre-restore state" "7" "$(q 'select count(*) from transactions')"
fi
(cd "$BACKUPS" && rm -f "${DECOYS[@]}")

# --- 13. the verification after the load is asserted on, not decoration -----------------
#
# Every case above restores a dump that is SUPPOSED to verify, so they all pass through
# verify_restore on its happy path and not one of them can tell whether it is checking
# anything. The test for that is to put `problem=0` immediately before verify_restore's
# last line, in a copied tree, and run this suite: it reported 65 passed, 0 failed. Which
# is to say the checks the restore prints -- the table count, the households marker, the
# alembic_version comparison -- were decoration, and a restore that loaded the wrong file
# into the household's database would still have been reported as a success.
#
# The three cases below each load a file that is a valid gzip of plain SQL carrying an
# alembic_version row, so each passes every check made BEFORE the destructive step and
# reaches verify_restore -- and each then fails a different one of its checks. With
# `problem=0` in place all three restores exit 0, and all three cases go red.

# Builds $BACKUPS/$1 from the SQL on stdin and restores it, leaving the exit code in $rc
# and the output in $WORK/verify.out.
run_unverifiable() { # NAME  < SQL
  local name="$1"
  cat >"$WORK/verify.sql"
  gzip -c "$WORK/verify.sql" >"$BACKUPS/$name"
  rc=0
  SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$name" >"$WORK/verify.out" 2>&1 </dev/null || rc=$?
  rm -f "$BACKUPS/$name"
}

# Puts the ledger back from the safety dump the failed run named in its own output. Each
# case below leaves the database holding the bad file, so this runs after every one of
# them -- and it doubles as a check that the recovery line printed under a FAILED
# VERIFICATION is as usable as the one printed under a failed load.
recover_after() { # LABEL
  local label="$1" dump
  dump="$(grep -oE "$PRERESTORE_RE" "$WORK/verify.out" | tail -n 1 || true)"
  if [ -z "$dump" ]; then
    bad "$label: the failure report named no dump to recover from"
    return 0
  fi
  rc=0
  SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$dump" >"$WORK/verify-undo.out" 2>&1 </dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    bad "$label: the recovery it printed failed (exit $rc)"
    cat "$WORK/verify-undo.out"
    return 0
  fi
  assert_eq "$label: the ledger came back" "7" "$(q 'select count(*) from transactions')"
}

# 13a. Tables, a matching alembic_version, and no `households`. dbinit.py uses that table
# to tell a Saiva database from any other, so a dump that leaves it missing is a dump of
# something else -- which is the mistake verification exists to catch.
run_unverifiable "saiva-20260501-010101-premigrate.sql.gz" <<'SQL'
CREATE TABLE widgets (id integer NOT NULL);
CREATE TABLE alembic_version (version_num varchar(32) NOT NULL);
COPY public.alembic_version (version_num) FROM stdin;
0099
\.
SQL
[ "$rc" -ne 0 ] && ok "a dump with no households table fails the script (exit $rc)" \
                || { bad "a dump with no households table was reported as a successful restore"; cat "$WORK/verify.out"; }
grep -q 'households table *MISSING' "$WORK/verify.out" && ok "it names the check that failed" \
                || { bad "the report does not name the failed check"; cat "$WORK/verify.out"; }
grep -q 'checks afterwards did not all pass' "$WORK/verify.out" \
  && ok "it says the load worked and the checks did not" \
  || { bad "the verify-phase report did not fire"; cat "$WORK/verify.out"; }
assert_eq "the database does hold what that file contained" "t" \
  "$(q "select to_regclass('public.widgets') is not null")"
recover_after "13a"

# 13b. An alembic_version row that is not the revision the dump declares in its COPY
# block. Without this check a restore can only confirm that SOME schema era is present,
# which is true of any database at all.
run_unverifiable "saiva-20260502-020202-premigrate.sql.gz" <<'SQL'
CREATE TABLE households (id integer NOT NULL);
CREATE TABLE alembic_version (version_num varchar(32) NOT NULL);
COPY public.alembic_version (version_num) FROM stdin;
0099
\.
UPDATE alembic_version SET version_num = '0077';
SQL
[ "$rc" -ne 0 ] && ok "an alembic_version that disagrees with the dump fails the script (exit $rc)" \
                || { bad "a mismatched alembic_version was reported as a successful restore"; cat "$WORK/verify.out"; }
grep -q 'MISMATCH' "$WORK/verify.out" && ok "it flags the mismatch and prints both revisions" \
                || { bad "the report does not flag the mismatch"; cat "$WORK/verify.out"; }
recover_after "13b"

# 13c. A file that loads cleanly and leaves nothing behind. This is also the second of the
# two state reports that could state the opposite of the truth: with no tables in the
# database at all, the report used to say "Database X is NOT empty" two lines under a
# check that had just printed EMPTY.
run_unverifiable "saiva-20260503-030303-premigrate.sql.gz" <<'SQL'
SELECT 1;
SQL
[ "$rc" -ne 0 ] && ok "a dump that leaves the database empty fails the script (exit $rc)" \
                || { bad "a dump that left the database empty was reported as a success"; cat "$WORK/verify.out"; }
assert_eq "the database really is empty" "0" \
  "$(q "select count(*) from information_schema.tables where table_schema = 'public'")"
grep -q 'no tables at' "$WORK/verify.out" && ok "the report says the database was left with no tables" \
                || { bad "the report does not say the database is empty"; cat "$WORK/verify.out"; }
grep -q 'is NOT empty' "$WORK/verify.out" \
  && bad "the report tells the owner an empty database is not empty" \
  || ok "and does not also claim it is not empty"
recover_after "13c"

# --- 14. every state report is true at the moment it can fire ----------------------------
#
# The EXIT trap prints one paragraph per phase, and that paragraph is the whole of what the
# owner has to act on at the worst moment this script has. Two of them could state the
# opposite of the truth, because the phase was set earlier than the thing it described: a
# failure merely READING pg_database, and a failure of the DROP itself, both reported the
# database as already dropped and emptied when it was untouched.
#
# Forced with a psql on PATH that fails one named statement and passes everything else
# through. --direct mode takes psql off PATH, which is the only reason this is reachable
# from a test at all.
PSQL_REAL="$(command -v psql)"
shim_psql() { # DIR CASE-PATTERN -- a psql that fails when one of its arguments matches
  mkdir -p "$1"
  cat >"$1/psql" <<SH
#!/bin/sh
for a in "\$@"; do
  case "\$a" in
    $2) printf 'psql: error: forced failure from the test shim\n' >&2; exit 1 ;;
  esac
done
exec "$PSQL_REAL" "\$@"
SH
  chmod +x "$1/psql"
}

BEFORE_ROWS="$(q 'select count(*) from transactions')"

shim_psql "$WORK/shim-meta" '*pg_database*'
rc=0
( PATH="$WORK/shim-meta:$PATH"; SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$DUMP" ) \
  >"$WORK/meta.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "a failure reading pg_database fails the script (exit $rc)" \
                || { bad "a failure reading pg_database was ignored"; cat "$WORK/meta.out"; }
grep -q 'Nothing was changed' "$WORK/meta.out" && ok "and reports that nothing was changed" \
                || { bad "it does not say what state the database is in"; cat "$WORK/meta.out"; }
grep -qi 'does not hold the data it held before' "$WORK/meta.out" \
  && { bad "it reports a dropped database after a failure that touched nothing"; cat "$WORK/meta.out"; } \
  || ok "and does not claim the database was dropped"
assert_eq "the database was in fact untouched" "$BEFORE_ROWS" "$(q 'select count(*) from transactions')"

shim_psql "$WORK/shim-drop" '"DROP DATABASE"*'
rc=0
( PATH="$WORK/shim-drop:$PATH"; SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$DUMP" ) \
  >"$WORK/drop.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "a DROP that fails fails the script (exit $rc)" \
                || { bad "a failed DROP was ignored"; cat "$WORK/drop.out"; }
grep -q 'did not report success' "$WORK/drop.out" && ok "it says the drop did not report success" \
                || { bad "the drop-phase report did not fire"; cat "$WORK/drop.out"; }
grep -qi 'does not hold the data it held before' "$WORK/drop.out" \
  && { bad "a refused DROP was reported as a database that lost its data"; cat "$WORK/drop.out"; } \
  || ok "and does not claim the data is gone"
grep -qE "$PRERESTORE_RE" "$WORK/drop.out" && ok "it still names the safety dump it took on the way past" \
                || { bad "the drop-phase report names no way back"; cat "$WORK/drop.out"; }
assert_eq "the data really is still there" "$BEFORE_ROWS" "$(q 'select count(*) from transactions')"

# --- 15. the dump is all that is left ----------------------------------------------------
#
# The db_data volume wiped, or the database dropped by hand to be rid of a corrupt one:
# nothing to dump, nothing to drop, nothing to lose. It is the first case this script's own
# header names, and it was the one case the script could not do -- the safety dump ran
# before anything had looked at the database, pg_dump of a database that does not exist
# fails, a failed safety dump stops the restore, and so the branch that creates a missing
# database was unreachable on the normal path.
psql -qc "DROP DATABASE IF EXISTS \"$TESTDB\" WITH (FORCE)" -d postgres >/dev/null
assert_eq "the database really is gone" "0" \
  "$(psql -X -At -q -d postgres -c "select count(*) from pg_database where datname = '$TESTDB'")"
PRERESTORE_COUNT="$(cd "$BACKUPS" && ls -1 ./*-prerestore*.sql.gz 2>/dev/null | wc -l || true)"

rc=0
SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$DUMP" >"$WORK/gone.out" 2>&1 </dev/null || rc=$?
[ "$rc" -eq 0 ] && ok "it restores into a database that no longer exists (exit 0)" \
                || { bad "it cannot restore when the database is gone (exit $rc)"; cat "$WORK/gone.out"; }
assert_eq "the ledger came back from the dump alone"  "3"    "$(q 'select count(*) from transactions')"
assert_eq "with the dump's schema era"                "0003" "$(q 'select version_num from alembic_version')"
assert_eq "and the dump's own tables"                 "t"    "$(q "select to_regclass('public.households') is not null")"
grep -q 'nothing to drop' "$WORK/gone.out" && ok "it says there was nothing to drop" \
                || { bad "it does not say what it did about the missing database"; cat "$WORK/gone.out"; }
grep -q 'REPLACES NOTHING' "$WORK/gone.out" && ok "and the confirmation does not threaten data that is not there" \
                || { bad "the warning still says everything since the dump will be lost"; cat "$WORK/gone.out"; }
assert_eq "it took no safety dump of a database that was not there" "$PRERESTORE_COUNT" \
  "$(cd "$BACKUPS" && ls -1 ./*-prerestore*.sql.gz 2>/dev/null | wc -l || true)"

# --- 16. a safety dump that cannot be taken still stops the restore ----------------------
#
# The other half of section 15, and the half it must not have loosened: "there is nothing
# to back up" is a reason to carry on, "there IS something and I could not back it up" is a
# reason to stop. A pg_dump on PATH that always fails stands in for the full disk, the
# unwritable backups directory and the server that goes away mid-dump.
mkdir -p "$WORK/shim-dump"
cat >"$WORK/shim-dump/pg_dump" <<'SH'
#!/bin/sh
printf 'pg_dump: error: forced failure from the test shim\n' >&2
exit 1
SH
chmod +x "$WORK/shim-dump/pg_dump"

BEFORE_ROWS="$(q 'select count(*) from transactions')"
rc=0
( PATH="$WORK/shim-dump:$PATH"; SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$DUMP" ) \
  >"$WORK/nodump.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "a safety dump that fails stops the restore (exit $rc)" \
                || { bad "it went ahead and dropped the database with no way back"; cat "$WORK/nodump.out"; }
grep -q 'will not be attempted' "$WORK/nodump.out" && ok "and says the restore was not attempted" \
                || { bad "it does not say whether it went ahead"; cat "$WORK/nodump.out"; }
assert_eq "the rows are untouched"   "$BEFORE_ROWS" "$(q 'select count(*) from transactions')"
assert_eq "the tables are untouched" "t"            "$(q "select to_regclass('public.households') is not null")"

# --- 17. one unreadable entry does not take the readable dumps with it -------------------
#
# A directory created by mistake, a dangling symlink left by a half-finished `docker cp`, a
# file restored from a tarball with the wrong owner: any single entry matching *.sql.gz
# that could not be read used to end the listing AND every restore, including of the good
# dumps sitting beside it. A backups directory with something wrong in it is the normal
# case at the moment anyone runs this, not an exceptional one.
BADDIR="saiva-20260601-010101-premigrate.sql.gz"
BADLINK="saiva-20260602-020202-premigrate.sql.gz"
mkdir -p "$BACKUPS/$BADDIR"
ln -s "$BACKUPS/nothing-here" "$BACKUPS/$BADLINK"

rc=0
run_restore >"$WORK/badlist.out" 2>&1 </dev/null || rc=$?
assert_eq "the listing still exits 0 with a bad entry among the dumps" "0" "$rc"
grep -q "$DUMP" "$WORK/badlist.out" && ok "it still lists the good dump" \
                || { bad "one bad entry hid every good dump"; cat "$WORK/badlist.out"; }
grep -q "$BADDIR" "$WORK/badlist.out" && ok "it names the directory it skipped" \
                || { bad "the skipped directory is not reported at all"; cat "$WORK/badlist.out"; }
grep -q 'a directory, not a file' "$WORK/badlist.out" && ok "and says why it skipped it" \
                || bad "it does not say why the entry was skipped"
grep -q "$BADLINK" "$WORK/badlist.out" && ok "it names the dangling symlink too" \
                || bad "a dangling symlink matching *.sql.gz is not reported"

rc=0
SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$BADDIR" >"$WORK/baddump.out" 2>&1 </dev/null || rc=$?
[ "$rc" -ne 0 ] && ok "it refuses to restore the bad entry (exit $rc)" \
                || { bad "it tried to restore a directory"; cat "$WORK/baddump.out"; }
grep -q 'a directory, not a file' "$WORK/baddump.out" \
  && ok "and says what is wrong with it, rather than that there is no such dump" \
  || { bad "the refusal does not say what is wrong with the entry"; cat "$WORK/baddump.out"; }

# And a good dump still restores with the bad entry in place, which is the half of this
# that actually costs a household its evening.
psql -X -q -v ON_ERROR_STOP=1 -d "$TESTDB" >/dev/null \
  -c "INSERT INTO transactions (account_id, posted_on, payee, amount) VALUES (1, '2026-12-01', 'Later', -1.00)"
rc=0
SAIVA_CONFIRM_RESTORE=restore-saiva-db run_restore "$DUMP" >"$WORK/badrestore.out" 2>&1 </dev/null || rc=$?
[ "$rc" -eq 0 ] && ok "a good dump still restores with an unreadable entry beside it" \
                || { bad "the bad entry broke the restore too (exit $rc)"; cat "$WORK/badrestore.out"; }
assert_eq "and the restore really happened" "3" "$(q 'select count(*) from transactions')"

if [ "$(id -u)" -ne 0 ]; then
  # Root reads mode 000 regardless, so as root this case would assert nothing.
  NOREAD="saiva-20260603-030303-premigrate.sql.gz"
  printf 'x\n' | gzip -c >"$BACKUPS/$NOREAD"
  chmod 000 "$BACKUPS/$NOREAD"
  rc=0
  run_restore >"$WORK/noread.out" 2>&1 </dev/null || rc=$?
  assert_eq "the listing survives a file it is not allowed to read" "0" "$rc"
  grep -q 'check its owner and permissions' "$WORK/noread.out" \
    && ok "it reports the unreadable file as unreadable" \
    || { bad "an unreadable file is not reported"; cat "$WORK/noread.out"; }
  chmod 644 "$BACKUPS/$NOREAD"
  rm -f "$BACKUPS/$NOREAD"
else
  note "skipped the unreadable-file case: this run is root, which can read mode 000"
fi

rm -rf "${BACKUPS:?}/${BADDIR:?}"
rm -f "$BACKUPS/$BADLINK"

# --- 18. --help says the thing that matters most -----------------------------------------

rc=0
run_restore --help >"$WORK/help.out" 2>&1 </dev/null || rc=$?
assert_eq "--help exits 0" "0" "$rc"
grep -q 'pg_restore' "$WORK/help.out" && ok "--help warns that pg_restore cannot read these" \
                                      || bad "--help does not mention pg_restore"

# --- summary -----------------------------------------------------------------------------

printf '\n  %d passed, %d failed\n\n' "$PASS" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
