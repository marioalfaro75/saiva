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
# No Docker. restore-backup.sh's --direct mode swaps its three `docker run` wrappers for
# plain `sh -c`, so everything below -- the listing, the confirmation, the drop and
# recreate, the single-transaction load, the verification -- is the same code production
# runs, with psql and pg_dump taken off PATH instead of out of a container. What --direct
# does NOT cover is the three `docker run` command lines themselves; those are three lines
# in one function, and they are the only part of this script a test on this host cannot
# reach.
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

# --- 13. --help says the thing that matters most -----------------------------------------

rc=0
run_restore --help >"$WORK/help.out" 2>&1 </dev/null || rc=$?
assert_eq "--help exits 0" "0" "$rc"
grep -q 'pg_restore' "$WORK/help.out" && ok "--help warns that pg_restore cannot read these" \
                                      || bad "--help does not mention pg_restore"

# --- summary -----------------------------------------------------------------------------

printf '\n  %d passed, %d failed\n\n' "$PASS" "$FAILED"
[ "$FAILED" -eq 0 ] || exit 1
