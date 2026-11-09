#!/usr/bin/env bash
# Tests for the deploy path, run with a fake docker and a fake curl.
#
#   ./scripts/tests/run.sh            run everything
#   ./scripts/tests/run.sh env        run the tests whose name contains "env"
#
# There is no Docker daemon in CI's unit lane and none on a developer laptop that has not
# started one, but the parts of this that have actually hurt -- the .env rewrite that
# un-published the site, the gate that could not see the failure, the rollback that only
# existed on paper -- are all testable without one. fakebin/docker records every call so
# a test can assert on the ORDER of what happened, and fakebin/curl lets the site be up,
# down or degraded on demand.
#
# What this cannot cover is whether a real `compose up` produces a working stack; that is
# what the image smoke suite in CI is for.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$TESTS_DIR/../.." && pwd)"
FILTER="${1:-}"

PASS=0; FAIL=0; FAILED_NAMES=()
RED=$'\033[31m'; GREEN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'

# --- assertions ---------------------------------------------------------------------

fail_test() { FAIL=$((FAIL + 1)); FAILED_NAMES+=("$CURRENT"); printf '  %sFAIL%s %s\n' "$RED" "$OFF" "$*"; }

assert_eq() { # EXPECTED ACTUAL MESSAGE
  if [ "$1" = "$2" ]; then return 0; fi
  fail_test "$3
       expected: [$1]
       actual:   [$2]"
  return 1
}

assert_contains() { # HAYSTACK NEEDLE MESSAGE
  case "$1" in *"$2"*) return 0 ;; esac
  fail_test "$3
       looked for: [$2]
       in:         [$(printf '%s' "$1" | head -c 600)]"
  return 1
}

assert_not_contains() { # HAYSTACK NEEDLE MESSAGE
  case "$1" in *"$2"*) fail_test "$3
       must NOT contain: [$2]"; return 1 ;; esac
  return 0
}

assert_file_eq() { # FILE_A FILE_B MESSAGE
  if diff -q "$1" "$2" >/dev/null 2>&1; then return 0; fi
  fail_test "$3
$(diff "$1" "$2" | head -20)"
  return 1
}

assert_before() { # FIRST SECOND CALLS_FILE MESSAGE -- FIRST's first use precedes SECOND's last
  local a b
  a="$(grep -n -m1 -- "$1" "$3" | cut -d: -f1)"
  b="$(grep -n -- "$2" "$3" | tail -1 | cut -d: -f1)"
  if [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]; then return 0; fi
  fail_test "$4
       [$1] at line ${a:-none}, [$2] at line ${b:-none}
$(cat "$3")"
  return 1
}

# --- fixtures -----------------------------------------------------------------------

SANDBOX_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/saiva-deploy-tests.XXXXXX")"
cleanup() { rm -rf "$SANDBOX_ROOT"; }
trap cleanup EXIT

# Files a deployment consists of, copied into every sandbox. Listed rather than copying
# the whole repository so a test fixture stays small and so an accidental dependency on
# something else in the tree shows up as a failure rather than passing silently.
DEPLOY_FILES=(scripts .env.example .gitignore docker-compose.prod.yml docker-compose.yml infra Makefile)

new_sandbox() { # NAME -- sets ORIGIN, WORK and FAKE for the current test
  local id="$1" base="$SANDBOX_ROOT/$1"
  ORIGIN="$base/origin.git"; WORK="$base/work"; FAKE="$base/fake"
  mkdir -p "$base" "$FAKE"

  git init --quiet --bare "$ORIGIN"
  git init --quiet "$WORK"
  git -C "$WORK" config user.email t@example.invalid
  git -C "$WORK" config user.name "Deploy tests"
  git -C "$WORK" remote add origin "$ORIGIN"

  local f
  for f in "${DEPLOY_FILES[@]}"; do
    [ -e "$REPO/$f" ] && cp -R "$REPO/$f" "$WORK/"
  done
  git -C "$WORK" add -A >/dev/null
  git -C "$WORK" commit --quiet -m "v1.0.0"
  git -C "$WORK" tag v1.0.0
  git -C "$WORK" -c push.negotiate=false push --quiet origin HEAD:refs/heads/main --tags
}

add_release() { # WORK -- build a v1.1.0 that differs in the ways a real release does
  local work="$1"
  # A new setting, so the .env reconcile has something to add.
  printf '\n# Added by v1.1.0\nSAIVA_NEW_SETTING=from-the-new-release\n' >> "$work/.env.example"
  # A Caddyfile change, which compose cannot see: the bind mount's DECLARATION is in the
  # service config hash, its CONTENTS are not.
  printf '\n# v1.1.0\n' >> "$work/infra/Caddyfile"
  # A marker inside the release path itself. If the re-exec after `git checkout` is ever
  # removed, the logic that runs is the OLD script against the NEW tree, this line never
  # executes, and the test that looks for it fails. That is the only way to tell the two
  # apart from outside.
  sed -i 's|^_release_apply() {|_release_apply() {\n  log "RUNNING-RELEASE-CODE-FROM-v1.1.0"|' \
    "$work/scripts/lib/release.sh"
  git -C "$work" add -A >/dev/null
  git -C "$work" commit --quiet -m "v1.1.0"
  git -C "$work" tag v1.1.0
  git -C "$work" -c push.negotiate=false push --quiet origin HEAD:refs/heads/main --tags
  git -C "$work" checkout --quiet --detach v1.0.0
  # Undo the working-tree side of the v1.1.0 edits so the box looks like it is simply
  # running the older release.
  git -C "$work" checkout --quiet -- .
}

seed_env() { # WORK [KEY=VALUE ...]
  local work="$1"; shift
  cp "$work/.env.example" "$work/.env"
  local kv
  for kv in "$@"; do
    local k="${kv%%=*}" v="${kv#*=}"
    if grep -q "^$k=" "$work/.env"; then
      awk -v k="$k" -v v="$v" 'index($0,k"=")==1{print k"="v;next}{print}' "$work/.env" > "$work/.env.t"
      mv "$work/.env.t" "$work/.env"
    else
      printf '%s=%s\n' "$k" "$v" >> "$work/.env"
    fi
  done
}

run_deploy() { # WORK ARGS... -> combined output in $OUT, exit code in $RC
  # No `set -e`/`set +e` pair here. An earlier version of this helper ended with
  # `set -e`, which switched errexit ON for the whole suite from the first call: the
  # next assertion that returned 1 killed the run, so only one failure was ever
  # reported and the summary never printed. `&& RC=0 || RC=$?` captures the status
  # without touching the shell's options.
  local work="$1"; shift
  OUT="$(cd "$work" && env \
    PATH="$TESTS_DIR/fakebin:$PATH" \
    SAIVA_FAKE_DIR="$FAKE" \
    SAIVA_STATE_DIR="$FAKE/state" \
    SAIVA_GATE_TRIES=3 \
    HOME="$SANDBOX_ROOT" \
    "$work/scripts/deploy.sh" "$@" 2>&1)" && RC=0 || RC=$?
}

status_json() { cat "$FAKE/state/status.json" 2>/dev/null || echo '{}'; }
status_field() { status_json | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" | head -1; }

site_up()   { printf 'up\n'   > "$FAKE/site"; }
site_down() { printf 'down\n' > "$FAKE/site"; }

# --- the test runner ------------------------------------------------------------------

CURRENT=""
run_test() {
  local name="$1"
  if [ -n "$FILTER" ] && case "$name" in *"$FILTER"*) false ;; *) true ;; esac; then return 0; fi
  CURRENT="$name"
  local before=$FAIL
  printf '%s\n' "• $name"
  OUT=""; RC=0
  "test_$name" || true
  # errexit must be OFF here. A helper that ends with `set -e` switches it on for the
  # whole suite, and the next assertion that returns 1 kills the run silently -- 33 tests
  # become 12 and the summary never prints. It has happened twice, so it is checked.
  case "$-" in
    *e*) printf '  %sHARNESS BUG%s %s left errexit on; the rest of the suite would be skipped\n' \
           "$RED" "$OFF" "$name"
         set +e; FAIL=$((FAIL + 1)); FAILED_NAMES+=("$name (errexit leak)") ;;
  esac
  if [ "$FAIL" -eq "$before" ]; then
    PASS=$((PASS + 1)); printf '  %sok%s\n' "$GREEN" "$OFF"
  else
    # The deploy's own output is the first thing anyone debugging this wants, and an
    # EMPTY one is itself a finding (the sandbox never got as far as running it).
    printf '  %s--- last deploy output (rc=%s, %s bytes) ---%s\n' \
      "$DIM" "${RC:-?}" "${#OUT}" "$OFF"
    printf '%s\n' "${OUT:-(nothing)}" | tail -30 | sed "s/^/  $DIM| $OFF/"
  fi
}

# ======================================================================================
# .env: the rule that a flag's absence never changes persisted state
# ======================================================================================

test_env_is_byte_identical_after_two_bare_deploys() {
  new_sandbox env-idempotent; local work="$WORK"
  seed_env "$work" SAIVA_BIND_ADDR=0.0.0.0 SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --pull
  cp "$work/.env" "$FAKE/env.after-first"
  run_deploy "$work" --pull
  assert_file_eq "$FAKE/env.after-first" "$work/.env" \
    "a second bare deploy changed .env; this is the defect that took the site off the LAN"
}

test_a_bare_deploy_does_not_un_publish_a_lan_install() {
  new_sandbox env-bindaddr; local work="$WORK"
  seed_env "$work" SAIVA_BIND_ADDR=0.0.0.0 SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --pull
  assert_eq "0.0.0.0" "$(grep '^SAIVA_BIND_ADDR=' "$work/.env" | cut -d= -f2-)" \
    "running the deploy with no --site rewrote the binding to loopback"
}

test_site_flag_publishes_and_records_the_address() {
  new_sandbox env-site; local work="$WORK"
  seed_env "$work" SAIVA_BIND_ADDR=127.0.0.1
  site_up
  run_deploy "$work" --pull --site https://192.168.0.11
  assert_eq "0.0.0.0" "$(grep '^SAIVA_BIND_ADDR=' "$work/.env" | cut -d= -f2-)" "--site must publish"
  assert_eq "https://192.168.0.11" "$(grep '^SAIVA_SITE_ADDRESS=' "$work/.env" | cut -d= -f2-)" \
    "--site must be recorded"
  assert_eq "192.168.0.11" "$(grep '^SAIVA_SITE_HOST=' "$work/.env" | cut -d= -f2-)" \
    "the bare host is what Caddy uses as default_sni"
}

test_a_first_install_binds_to_loopback() {
  new_sandbox env-first; local work="$WORK"
  site_up
  run_deploy "$work" --pull
  assert_eq "127.0.0.1" "$(grep '^SAIVA_BIND_ADDR=' "$work/.env" | cut -d= -f2-)" \
    "a brand new install must stay on this machine until asked otherwise"
  assert_contains "$(cat "$work/.env")" "SECRET_KEY=" "the install must generate a SECRET_KEY"
  local key; key="$(grep '^SECRET_KEY=' "$work/.env" | cut -d= -f2-)"
  [ "${#key}" -ge 32 ] || fail_test "SECRET_KEY is only ${#key} characters: $key"
}

test_env_reconcile_adds_new_keys_and_never_overwrites() {
  new_sandbox env-reconcile; local work="$WORK"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://example.invalid
  printf 'BRAND_NEW_KEY=default-value\n' >> "$work/.env.example"
  site_up
  run_deploy "$work" --pull
  assert_contains "$(cat "$work/.env")" "BRAND_NEW_KEY=default-value" \
    "a key the new .env.example introduced was not backfilled"
  assert_eq "https://example.invalid" "$(grep '^SAIVA_SITE_ADDRESS=' "$work/.env" | cut -d= -f2-)" \
    "reconcile overwrote a value that was already set"
}

test_reconcile_writes_values_the_app_can_actually_parse() {
  # .env.example carries inline prose after some values (SMTP_TLS=true # STARTTLS...).
  # Stripping the comment and leaving the whitespace writes `SMTP_TLS=true  `, and
  # pydantic rejects that outright -- so the api container would not start. A reconcile
  # that produces an unparseable .env is worse than one that produces nothing.
  new_sandbox env-trailing; local work="$WORK"
  seed_env "$work"
  printf 'A_FLAG=true   # prose about the flag\nA_PATH=/srv/x\t# tabbed prose\n' \
    >> "$work/.env.example"
  site_up
  run_deploy "$work" --pull
  local offenders
  offenders="$(grep -nE '[[:space:]]+$' "$work/.env" || true)"
  assert_eq "" "$offenders" "reconcile left trailing whitespace on a value: $offenders"
  assert_contains "$(cat "$work/.env")" "A_FLAG=true" "the value itself must survive"
  assert_not_contains "$(grep '^A_PATH=' "$work/.env")" "prose" "the comment must not"
}

test_env_set_survives_values_full_of_metacharacters() {
  new_sandbox env-metachars; local work="$WORK"
  # shellcheck source=/dev/null
  ( ROOT="$work"; SAIVA_ENV_FILE="$work/.env"
    . "$REPO/scripts/lib/env-file.sh"
    : > "$work/.env"
    env_set A 'https://user:p@ss&word@host/path?x=1|2'
    env_set B 'a\b/c&d'
    env_set A 'replaced&again'
    printf '%s\n' "$(env_get A)" > "$work/got-a"
    printf '%s\n' "$(env_get B)" > "$work/got-b"
  )
  assert_eq 'replaced&again' "$(cat "$work/got-a")" "& in a replacement value was mangled"
  assert_eq 'a\b/c&d' "$(cat "$work/got-b")" "backslashes and slashes were mangled"
}

test_the_state_directory_matches_the_one_compose_mounts() {
  # If these disagree the deploy writes its status somewhere no container mounts, and
  # the panel reports "no update has ever run" after every successful update. .env is
  # what compose reads, so .env has to be what the script reads too.
  new_sandbox state-dir; local work="$WORK"
  seed_env "$work" SAIVA_STATE_DIR=/tmp/somewhere-else SAIVA_SITE_ADDRESS=https://192.168.0.11
  local got
  got="$(cd "$work" && env -u SAIVA_STATE_DIR bash -c '
    ROOT="$PWD"; . scripts/lib/env-file.sh; . scripts/lib/state.sh; state_dir')"
  assert_eq "/tmp/somewhere-else" "$got" "state_dir ignored SAIVA_STATE_DIR in .env"
}

test_an_explicit_environment_variable_still_wins() {
  new_sandbox state-dir-env; local work="$WORK"
  seed_env "$work" SAIVA_STATE_DIR=/tmp/from-dotenv
  local got
  got="$(cd "$work" && SAIVA_STATE_DIR=/tmp/from-the-environment bash -c '
    ROOT="$PWD"; . scripts/lib/env-file.sh; . scripts/lib/state.sh; state_dir')"
  assert_eq "/tmp/from-the-environment" "$got" "an explicit override must win over .env"
}

# ======================================================================================
# The typed confirmation on `make destroy`
# ======================================================================================

test_destroy_refuses_without_the_word() {
  # `make destroy` is one word from `make down` and removes the household's records AND
  # every pre-migration dump -- the dumps a restore reads FROM. Nothing tested the gate.
  new_sandbox destroy-wrong; local work="$WORK"
  OUT="$(cd "$work" && printf 'yes\n' | ./scripts/confirm-destroy.sh 2>&1)" && RC=0 || RC=$?
  [ "$RC" -ne 0 ] || fail_test "typing 'yes' was accepted as the confirmation"
  assert_contains "$OUT" "db_backups" "the prompt must name the backups, which is what people miss"
}

test_destroy_refuses_when_there_is_no_terminal() {
  new_sandbox destroy-no-tty; local work="$WORK"
  OUT="$(cd "$work" && ./scripts/confirm-destroy.sh </dev/null 2>&1)" && RC=0 || RC=$?
  [ "$RC" -ne 0 ] || fail_test "a non-interactive run destroyed the database without asking"
}

test_destroy_proceeds_only_on_the_exact_word() {
  new_sandbox destroy-right; local work="$WORK"
  OUT="$(cd "$work" && SAIVA_CONFIRM_DESTROY=destroy-saiva-data ./scripts/confirm-destroy.sh 2>&1)" \
    && RC=0 || RC=$?
  assert_eq "0" "$RC" "the documented non-interactive override did not work: $OUT"
}

test_make_destroy_cannot_be_carried_past_a_refusal() {
  # As two recipe lines, `make -i` / `make -k` carry on past the refused confirmation and
  # run `down -v` anyway. Joined with && there is no line for make to continue past.
  new_sandbox destroy-make; local work="$WORK"
  OUT="$(cd "$work" && make -i destroy COMPOSE="echo WOULD-RUN:" </dev/null 2>&1)" && RC=0 || RC=$?
  assert_not_contains "$OUT" "WOULD-RUN" \
    "make carried on past the refused confirmation and ran the destructive command"
}

# ======================================================================================
# The gate
# ======================================================================================

test_the_gate_fails_when_the_site_does_not_answer() {
  new_sandbox gate-down; local work="$WORK"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --pull             # get the stack running and answering first
  printf 'down\n' > "$FAKE/site.after-up"  # the next `up` leaves the site unreachable
  site_down
  run_deploy "$work" --pull
  [ "$RC" -ne 0 ] || fail_test "the deploy reported success while the site did not answer"
  assert_contains "$OUT" "did not answer" "the failure message must name what it checked"
}

test_the_gate_fails_on_a_degraded_api_not_just_a_dead_one() {
  new_sandbox gate-degraded; local work="$WORK"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  printf 'degraded\n' > "$FAKE/health.status"   # api answers, but 503
  run_deploy "$work" --pull
  [ "$RC" -ne 0 ] || fail_test "a 503 from /api/health was treated as healthy"
}

test_the_gate_probes_the_published_address_from_outside() {
  new_sandbox gate-outside; local work="$WORK"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --pull
  assert_contains "$(cat "$FAKE/calls")" "https://192.168.0.11/api/health" \
    "the gate must probe the configured address, not 127.0.0.1 inside the container"
  assert_not_contains "$(cat "$FAKE/calls")" "exec -T api python" \
    "the in-container probe is what printed a tick during the outage"
}

# ======================================================================================
# Applying a release
# ======================================================================================

test_apply_release_moves_the_tree_and_runs_the_new_code() {
  new_sandbox apply-happy; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 SAIVA_BIND_ADDR=0.0.0.0
  site_up
  run_deploy "$work" --apply-release

  assert_eq "0" "$RC" "apply-release failed: $OUT"
  assert_eq "applied" "$(status_field phase)" "the status file must record the outcome"
  assert_eq "v1.1.0" "$(git -C "$work" describe --tags HEAD)" "the tree did not move to the release"
  assert_contains "$OUT" "RUNNING-RELEASE-CODE-FROM-v1.1.0" \
    "the re-exec did not happen: the OLD script ran against the NEW tree"
  assert_contains "$(cat "$work/.env")" "SAIVA_NEW_SETTING=from-the-new-release" \
    "the release's new .env setting was not applied"
}

test_apply_release_reloads_caddy_when_the_caddyfile_changed() {
  new_sandbox apply-caddy; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --apply-release
  assert_contains "$(cat "$FAKE/calls")" "caddy reload" \
    "a release that changes infra/Caddyfile is inert without an explicit reload"
}

test_apply_release_does_everything_in_the_right_order() {
  new_sandbox apply-order; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --apply-release
  assert_before "compose .*pull" "compose .*up -d" "$FAKE/calls" "it recreated before it pulled"
  assert_before "compose .*up -d" "api/health" "$FAKE/calls" "it gated before it applied"
}

test_apply_release_refuses_to_take_the_box_backwards() {
  new_sandbox apply-downgrade; local work="$WORK"
  add_release "$work"
  # The box is on a branch that has moved PAST the newest release: a checkout left on
  # main, which is the ordinary way this happens. v1.1.0 does not contain HEAD, so
  # checking it out would silently revert the commits this box is running.
  git -C "$work" checkout --quiet main 2>/dev/null || git -C "$work" checkout --quiet -B main v1.1.0
  printf '\n# unreleased work on this box\n' >> "$work/infra/Caddyfile"
  git -C "$work" add -A >/dev/null
  git -C "$work" commit --quiet -m "a commit newer than any release"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --apply-release
  [ "$RC" -ne 0 ] || fail_test "it checked out a tag that reverts commits the box is running"
  assert_contains "$OUT" "BACKWARDS" "the refusal must say what it is protecting against"
  assert_contains "$OUT" "--allow-downgrade" "and name the way to do it anyway"
}

test_apply_release_refuses_a_dirty_checkout() {
  new_sandbox apply-dirty; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  printf '\n# hand-edited on the box\n' >> "$work/infra/Caddyfile"
  site_up
  run_deploy "$work" --apply-release
  [ "$RC" -ne 0 ] || fail_test "it threw away a hand edit in the checkout"
  assert_contains "$OUT" "uncommitted changes" "the refusal must say why"
  assert_contains "$OUT" "Nothing has been changed" "and that nothing was applied"
}

# ======================================================================================
# Rollback
# ======================================================================================

test_a_failed_gate_rolls_the_release_back() {
  new_sandbox rollback-gate; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 SAIVA_VERSION=v1.0.0
  site_up
  printf 'sha256:old-api\n' > "$FAKE/image.api"
  printf 'sha256:old-web\n' > "$FAKE/image.web"
  printf 'ghcr.io/example/saiva-api@sha256:oldapi\n' > "$FAKE/digest.sha256:old-api"
  printf 'ghcr.io/example/saiva-web@sha256:oldweb\n' > "$FAKE/digest.sha256:old-web"
  cp "$work/.env" "$FAKE/env.before"

  # The site answers while we record what is running, and stops answering once the new
  # release is up -- which is exactly the failure the gate exists to catch.
  printf '1\n' > "$FAKE/rc.up"
  run_deploy "$work" --apply-release

  [ "$RC" -ne 0 ] || fail_test "a failed release reported success"
  assert_eq "rolled_back" "$(status_field phase)" "the status file must say it rolled back"
  assert_eq "v1.0.0" "$(git -C "$work" describe --tags HEAD)" "the tree was not put back"
}

test_a_rollback_pins_the_exact_images_that_were_running() {
  new_sandbox rollback-pin; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 SAIVA_VERSION=latest
  site_up
  printf 'sha256:old-api\n' > "$FAKE/image.api"
  printf 'sha256:old-web\n' > "$FAKE/image.web"
  printf 'ghcr.io/example/saiva-api@sha256:oldapi\n' > "$FAKE/digest.sha256:old-api"
  printf 'ghcr.io/example/saiva-web@sha256:oldweb\n' > "$FAKE/digest.sha256:old-web"
  printf '1\n' > "$FAKE/rc.up"
  run_deploy "$work" --apply-release

  assert_contains "$(cat "$work/.env")" "SAIVA_API_IMAGE=ghcr.io/example/saiva-api@sha256:oldapi" \
    "on the latest channel, restoring .env alone is not a rollback: latest has moved"
  assert_contains "$(cat "$work/.env")" "SAIVA_WEB_IMAGE=ghcr.io/example/saiva-web@sha256:oldweb" \
    "both images must be pinned; SAIVA_VERSION applies to api and web together"
}

test_a_successful_update_clears_a_previous_rollback_pin() {
  new_sandbox rollback-unpin; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 \
    SAIVA_API_IMAGE=ghcr.io/example/saiva-api@sha256:stale \
    SAIVA_WEB_IMAGE=ghcr.io/example/saiva-web@sha256:stale
  site_up
  run_deploy "$work" --apply-release
  assert_eq "" "$(grep '^SAIVA_API_IMAGE=' "$work/.env" | cut -d= -f2-)" \
    "a pin nothing ever clears is how the old rollback left the box dark forever"
}

test_a_rollback_after_a_migration_says_so_instead_of_claiming_success() {
  new_sandbox rollback-schema; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 SAIVA_VERSION=v1.0.0
  site_up
  printf 'current\n' > "$FAKE/health.schema"
  printf 'sha256:old-api\n' > "$FAKE/image.api"
  printf 'ghcr.io/example/saiva-api@sha256:oldapi\n' > "$FAKE/digest.sha256:old-api"
  printf '1\n' > "$FAKE/rc.up"
  # After the rollback the old image meets the migrated database and reports `ahead`.
  ( sleep 0.2; printf 'ahead\n' > "$FAKE/health.schema" ) &
  run_deploy "$work" --apply-release
  wait

  assert_eq "rolled_back_code_only" "$(status_field phase)" \
    "a rollback that could not undo the migration must not report a plain rollback"
  assert_contains "$OUT" "restore-backup.sh" "it must name the one command that can finish the job"
}

test_a_failed_pull_changes_nothing() {
  new_sandbox pull-fail; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  printf '1\n' > "$FAKE/rc.pull"
  # Both images, as a running box would have. An earlier version of this fixture
  # recorded only api, which is a different scenario -- see the test below.
  printf 'sha256:old-api\n' > "$FAKE/image.api"
  printf 'sha256:old-web\n' > "$FAKE/image.web"
  printf 'ghcr.io/example/saiva-api@sha256:oldapi\n' > "$FAKE/digest.sha256:old-api"
  printf 'ghcr.io/example/saiva-web@sha256:oldweb\n' > "$FAKE/digest.sha256:old-web"
  run_deploy "$work" --apply-release
  [ "$RC" -ne 0 ] || fail_test "a failed pull reported success"
  assert_eq "rolled_back" "$(status_field phase)" "a failed pull must roll back, not stop half-applied"
}

test_a_rollback_that_could_not_restore_the_images_says_so() {
  # The case that matters and is easy to miss: the stack was already down when the
  # update started, so record_capture had no running containers to read a digest from.
  # Restoring .env is then NOT a rollback on a rolling channel, because `compose pull`
  # has already moved the local `latest` tag to the release that just failed -- `up -d`
  # would bring it straight back and the box would sit on the broken release while the
  # panel said "rolled back".
  new_sandbox rollback-unpinned; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 SAIVA_VERSION=latest
  site_up
  # No image.* files: nothing was running, so nothing recorded a digest.
  printf '1\n' > "$FAKE/rc.up"
  run_deploy "$work" --apply-release

  [ "$RC" -ne 0 ] || fail_test "it reported success having restored nothing"
  assert_eq "rollback_incomplete" "$(status_field phase)" \
    "a rollback that could not restore the images must not call itself a rollback"
  assert_contains "$OUT" "NOT the" "it must name what it failed to put back"
  assert_contains "$OUT" "SAIVA_VERSION" "and the way out"
}

test_a_pinned_channel_does_not_need_a_recorded_digest() {
  # The guard against over-correcting. On a version-pinned box, restoring .env restores
  # the images by definition -- v0.16.1 still means v0.16.1 -- so the absence of a
  # recorded digest is not a hole and must not be reported as one.
  new_sandbox rollback-pinned; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 SAIVA_VERSION=v1.0.0
  site_up
  printf '1\n' > "$FAKE/rc.up"
  run_deploy "$work" --apply-release

  assert_eq "rolled_back" "$(status_field phase)" \
    "a pinned channel restores its own images; this is a clean rollback"
}

test_a_rollback_from_a_failed_gate_reports_a_terminal_phase() {
  # ADDED BY REVIEW -- currently FAILS. Every other rollback test reaches _rollback via
  # `$COMPOSE ... || _rollback ...`, and the right-hand side of `||` is a context where
  # errexit is suppressed for the whole function body. The GATE failure reaches it from
  # an `if ! wait_for_site; then _rollback ...; fi` body, where errexit is LIVE -- and
  # _rollback contains `schema_now="$(health_field schema)"`, a bare assignment from a
  # command substitution. health_field is a `curl | sed | head` pipeline under pipefail,
  # so when the site is not answering yet (which is the normal case a few hundred ms
  # after `up -d` returns) it exits non-zero, the assignment inherits that, and errexit
  # kills the script mid-rollback: no wait_for_site, no status_finish, no rolled_back /
  # rolled_back_code_only / rollback_incomplete. The box is left with phase=running
  # forever and the panel polls for an answer that is never written.
  new_sandbox rollback-gate-terminal; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11 SAIVA_VERSION=v1.0.0
  site_up
  printf 'sha256:old-api\n' > "$FAKE/image.api"
  printf 'sha256:old-web\n' > "$FAKE/image.web"
  printf 'ghcr.io/example/saiva-api@sha256:oldapi\n' > "$FAKE/digest.sha256:old-api"
  printf 'ghcr.io/example/saiva-web@sha256:oldweb\n' > "$FAKE/digest.sha256:old-web"
  # The release starts but does not serve, so the GATE is what fails -- and the site is
  # still not answering at the instant the rollback's `up -d` returns, as on a real box.
  printf 'down\n' > "$FAKE/site.after-up"
  site_down
  # Bring the site back a few seconds AFTER the rollback starts -- well inside
  # wait_for_site's budget, so the only correct outcome is a terminal `rolled_back`.
  # Keyed off the run log rather than a fixed delay so the test cannot race the gate.
  ( while ! grep -q 'rollback:' "$FAKE/state/run.log" 2>/dev/null; do sleep 0.05; done
    sleep 3; printf 'up\n' > "$FAKE/site"; rm -f "$FAKE/site.after-up" ) &
  local flipper=$!
  run_deploy "$work" --apply-release
  kill "$flipper" 2>/dev/null; wait "$flipper" 2>/dev/null

  assert_not_contains "$(status_field phase)" "running" \
    "the rollback left phase=running: no terminal status was ever written, so the panel
       polls forever and --status never says what happened"
  assert_eq "1" "$RC" "a rollback must exit 1, not with curl's exit code"
}

test_a_second_update_does_not_overwrite_the_running_ones_status() {
  # The lock refusal used to go through `fail`, which writes a terminal status -- so a
  # second press of the button marked the update that was still running as failed.
  new_sandbox lock; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  mkdir -p "$FAKE/state"
  printf 'run_id\tin-flight\nphase\trunning\nstep\tpull\n' > "$FAKE/state/status.kv"
  ( cd "$work" && SAIVA_STATE_DIR="$FAKE/state" flock -n 9 || true ) 9>"$FAKE/state/deploy.lock" &
  local holder=$!
  # Hold the lock in a background shell while a second run tries to take it.
  ( flock 9; sleep 2 ) 9>"$FAKE/state/deploy.lock" &
  local keeper=$!
  sleep 0.3
  run_deploy "$work" --apply-release
  kill "$holder" "$keeper" 2>/dev/null || true
  wait "$holder" "$keeper" 2>/dev/null || true

  [ "$RC" -ne 0 ] || fail_test "a second update ran while one held the lock"
  assert_contains "$OUT" "already running" "it must say why it refused"
  assert_eq "running" "$(grep -m1 '^phase' "$FAKE/state/status.kv" | cut -f2)" \
    "the refusal overwrote the in-flight run's status"
}

# ======================================================================================
# The host agent and the privilege boundary
# ======================================================================================

agent_sandbox() { # sets AGENT_HOME, writes a conf and stub deploy.sh / backup.sh
  AGENT_HOME="$SANDBOX_ROOT/agent-$1"
  mkdir -p "$AGENT_HOME/state/requests" "$AGENT_HOME/state/backup-requests" \
           "$AGENT_HOME/checkout/scripts" "$AGENT_HOME/etc"
  git init -q "$AGENT_HOME/checkout"
  cat > "$AGENT_HOME/etc/agent.conf" <<EOF
SAIVA_CHECKOUT=$AGENT_HOME/checkout
SAIVA_STATE_DIR=$AGENT_HOME/state
EOF
  cat > "$AGENT_HOME/checkout/scripts/deploy.sh" <<'STUB'
#!/usr/bin/env bash
case "$1" in --apply-release) exit "${STUB_RC:-0}" ;; esac
STUB
  cat > "$AGENT_HOME/checkout/scripts/backup.sh" <<'STUB'
#!/usr/bin/env bash
exit "${STUB_BACKUP_RC:-0}"
STUB
  chmod +x "$AGENT_HOME/checkout/scripts/deploy.sh" "$AGENT_HOME/checkout/scripts/backup.sh"
}

run_agent() {
  # `&& RC=0 || RC=$?`, never `set +e` / `set -e`. See run_deploy: a helper that ends with
  # `set -e` switches errexit on for the whole suite from its first call, and the next
  # assertion that returns 1 kills the run. That bug was fixed in run_deploy and then
  # written again here, which is why run_test now checks for it after every test.
  OUT="$(SAIVA_AGENT_CONF="$AGENT_HOME/etc/agent.conf" STUB_RC="${STUB_RC:-0}" \
    STUB_BACKUP_RC="${STUB_BACKUP_RC:-0}" \
    "$REPO/scripts/saiva-update-agent" "$@" 2>&1)" && RC=0 || RC=$?
}

test_a_press_the_host_cannot_act_on_does_not_vanish() {
  # The request is consumed BEFORE the work, so a failing release cannot loop -- which
  # means the status file is the only record that the press happened. deploy.sh has exit
  # paths that run before it has any state to write to (no Docker daemon, no compose),
  # and on those the panel would otherwise keep showing the PREVIOUS run as green.
  agent_sandbox vanish
  printf '{"phase":"applied","to_version":"v1.0.0"}\n' > "$AGENT_HOME/state/status.json"
  : > "$AGENT_HOME/state/requests/req-1"
  STUB_RC=1 run_agent

  [ "$RC" -ne 0 ] || fail_test "the agent reported success for a deploy that failed"
  local phase; phase="$(sed -n 's/.*"phase": *"\([^"]*\)".*/\1/p' "$AGENT_HOME/state/status.json")"
  assert_eq "failed" "$phase" "the press vanished: the panel still shows the previous run"
  assert_contains "$(cat "$AGENT_HOME/state/status.json")" "did not start" \
    "and it must say what to check"
}

test_a_deploy_that_reports_for_itself_is_left_alone() {
  # The guard against over-correcting: when deploy.sh DOES write a status, the agent
  # must not overwrite it with a generic failure.
  agent_sandbox reports
  cat > "$AGENT_HOME/checkout/scripts/deploy.sh" <<'STUB'
#!/usr/bin/env bash
# The --apply-release spelling matters: the agent refuses a checkout whose deploy script
# predates the flag, so a stub without it never reaches the branch under test.
case "$1" in
  --apply-release)
    echo '{"phase":"rolled_back","message":"the real reason"}' > "$SAIVA_STATE_DIR/status.json"
    exit 1 ;;
esac
STUB
  chmod +x "$AGENT_HOME/checkout/scripts/deploy.sh"
  : > "$AGENT_HOME/state/requests/req-1"
  OUT="$(SAIVA_AGENT_CONF="$AGENT_HOME/etc/agent.conf" SAIVA_STATE_DIR="$AGENT_HOME/state" \
    "$REPO/scripts/saiva-update-agent" 2>&1)" && RC=0 || RC=$?
  assert_contains "$(cat "$AGENT_HOME/state/status.json")" "the real reason" \
    "the agent overwrote the deploy's own account of what happened"
}

test_the_agent_removes_a_planted_link_without_following_it() {
  # The boundary the first version of this got wrong. Making the request's CONTENTS
  # inert is not enough if something can plant a symlink where root looks.
  agent_sandbox symlink
  printf 'SECRET\n' > "$AGENT_HOME/victim"
  ln -s "$AGENT_HOME/victim" "$AGENT_HOME/state/requests/evil"
  mkfifo "$AGENT_HOME/state/requests/pipe" 2>/dev/null || true
  run_agent

  assert_eq "0" "$RC" "a planted entry must not fail the agent"
  assert_eq "" "$(ls -A "$AGENT_HOME/state/requests")" "the drop-box must be swept clean"
  assert_eq "SECRET" "$(cat "$AGENT_HOME/victim")" "the agent followed the link"
  assert_not_contains "$OUT" "applying" "a link is not a request, so nothing should run"
}

# ======================================================================================
# The SECOND drop-box: "Back up now" over the same boundary
# ======================================================================================

test_a_backup_request_is_consumed_and_the_backup_runs() {
  agent_sandbox backup-ok
  : > "$AGENT_HOME/state/backup-requests/req-1"
  cat > "$AGENT_HOME/checkout/scripts/backup.sh" <<'STUB'
#!/usr/bin/env bash
printf '{"phase":"ok","verified":"true"}\n' > "$SAIVA_STATE_DIR_FOR_STUB/backup-status.json"
echo "trigger=$2"
STUB
  chmod +x "$AGENT_HOME/checkout/scripts/backup.sh"
  OUT="$(SAIVA_AGENT_CONF="$AGENT_HOME/etc/agent.conf" \
    SAIVA_STATE_DIR_FOR_STUB="$AGENT_HOME/state" \
    "$REPO/scripts/saiva-update-agent" --backup 2>&1)" && RC=0 || RC=$?

  assert_eq "0" "$RC" "the agent must report what the backup reported"
  assert_eq "" "$(ls -A "$AGENT_HOME/state/backup-requests")" "the request must be consumed"
  assert_contains "$OUT" "trigger=button" "the backup must be told who asked for it"
  # And it must NOT have touched the UPDATE's record, which would show a backup's outcome
  # on the update panel.
  [ ! -f "$AGENT_HOME/state/status.json" ] \
    || fail_test "a backup wrote the update's status file"
}

test_an_update_request_does_not_trigger_a_backup_or_the_other_way_round() {
  # The whole security argument is that WHICH directory a file landed in is the only
  # information root takes from the app. If either handler swept both drop-boxes, a press
  # of one button would consume the other's request and the second press would vanish.
  agent_sandbox backup-isolation
  : > "$AGENT_HOME/state/requests/u-1"
  : > "$AGENT_HOME/state/backup-requests/b-1"

  run_agent --backup
  assert_eq "u-1" "$(ls -A "$AGENT_HOME/state/requests")" \
    "--backup consumed the UPDATE request as well"
  assert_eq "" "$(ls -A "$AGENT_HOME/state/backup-requests")" "--backup must consume its own"

  run_agent
  assert_eq "" "$(ls -A "$AGENT_HOME/state/requests")" "the update request must be consumed"
}

test_a_backup_press_the_host_cannot_act_on_does_not_vanish() {
  # Same rule as the update button, and it needs its own net because it writes a different
  # file: a request consumed with nothing written leaves the panel showing YESTERDAY's
  # verified backup while the press the owner just made has disappeared.
  agent_sandbox backup-vanish
  printf '{"phase":"ok","verified":"true","last_verified_at":"2026-01-01T00:00:00Z"}\n' \
    > "$AGENT_HOME/state/backup-status.json"
  rm -f "$AGENT_HOME/checkout/scripts/backup.sh"
  : > "$AGENT_HOME/state/backup-requests/req-1"
  run_agent --backup

  [ "$RC" -ne 0 ] || fail_test "the agent reported success with no backup script present"
  local phase
  phase="$(sed -n 's/.*"phase": *"\([^"]*\)".*/\1/p' "$AGENT_HOME/state/backup-status.json")"
  assert_eq "failed" "$phase" "the press vanished: the panel still shows the previous backup"
  assert_contains "$(cat "$AGENT_HOME/state/backup-status.json")" "no scripts/backup.sh" \
    "and it must say what is wrong with this checkout"
  # A backup failure must never be written into the public file Caddy serves: there is
  # deliberately no public backup record at all.
  [ ! -f "$AGENT_HOME/state/status-public.json" ] \
    || fail_test "a backup failure leaked into the publicly served status"
}

test_a_backup_that_reports_for_itself_is_left_alone() {
  agent_sandbox backup-reports
  cat > "$AGENT_HOME/checkout/scripts/backup.sh" <<'STUB'
#!/usr/bin/env bash
printf '{"phase":"failed","verify_message":"the real reason"}\n' \
  > "$SAIVA_STATE_DIR_FOR_STUB/backup-status.json"
exit 1
STUB
  chmod +x "$AGENT_HOME/checkout/scripts/backup.sh"
  : > "$AGENT_HOME/state/backup-requests/req-1"
  OUT="$(SAIVA_AGENT_CONF="$AGENT_HOME/etc/agent.conf" \
    SAIVA_STATE_DIR_FOR_STUB="$AGENT_HOME/state" \
    "$REPO/scripts/saiva-update-agent" --backup 2>&1)" && RC=0 || RC=$?

  assert_contains "$(cat "$AGENT_HOME/state/backup-status.json")" "the real reason" \
    "the agent overwrote the backup's own account of what happened"
}

test_the_agent_sweeps_a_planted_link_out_of_the_backup_drop_box_too() {
  agent_sandbox backup-symlink
  printf 'SECRET\n' > "$AGENT_HOME/victim"
  ln -s "$AGENT_HOME/victim" "$AGENT_HOME/state/backup-requests/evil"
  mkfifo "$AGENT_HOME/state/backup-requests/pipe" 2>/dev/null || true
  run_agent --backup

  assert_eq "0" "$RC" "a planted entry must not fail the agent"
  assert_eq "" "$(ls -A "$AGENT_HOME/state/backup-requests")" "the drop-box must be swept clean"
  assert_eq "SECRET" "$(cat "$AGENT_HOME/victim")" "the agent followed the link"
  assert_not_contains "$OUT" "taking a backup" "a link is not a request, so nothing should run"
}

test_dot_env_decides_where_state_lives_and_a_stale_conf_is_named() {
  # .env is what docker-compose interpolates the api and caddy bind mounts from, so it is
  # the only answer the containers can see -- and therefore the only one worth having.
  # The agent used to prefer /etc/saiva/agent.conf, and the first time the two disagreed it
  # looked for a status file in the wrong place, found nothing changed, and overwrote a
  # good "backed up and verified" with "the run failed silently". So two things have to be
  # true at once now: .env WINS, and the stale conf is named out loud, because the systemd
  # path units the installer wrote are watching the directory the conf named.
  agent_sandbox disagree
  mkdir -p "$AGENT_HOME/from-env"
  printf 'SAIVA_STATE_DIR=%s/from-env   # with prose after it\n' "$AGENT_HOME" \
    > "$AGENT_HOME/checkout/.env"
  run_agent --self-check

  assert_contains "$OUT" "$AGENT_HOME/from-env" \
    "the agent must use the directory .env names, inline comment and all"
  assert_not_contains "$(printf '%s' "$OUT" | sed -n '/^state directory/p')" \
    "$AGENT_HOME/state" "the conf's value must not win over .env"
  [ "$RC" -ne 0 ] || fail_test "--self-check passed while agent.conf still names another directory"
  assert_contains "$OUT" "STALE CONFIG" "it must name the problem"
}

test_a_run_that_dies_mid_way_still_leaves_a_terminal_status() {
  # The sharpest case is the re-exec'd child: the tree has already moved, so an exit
  # before a terminal status is written leaves `running / checkout` set forever -- the
  # panel polls, --status says a run is in progress, and the box is on a release nobody
  # applied. The same net has to cover any unexpected exit.
  new_sandbox dies-midway; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  # Kill the deploy once it reaches the pull step, as a systemd timeout or an OOM would.
  ( while ! grep -q 'pull:' "$FAKE/state/run.log" 2>/dev/null; do sleep 0.05; done
    pkill -TERM -f 'deploy.sh --apply-release' 2>/dev/null ) &
  local killer=$!
  run_deploy "$work" --apply-release
  kill "$killer" 2>/dev/null; wait "$killer" 2>/dev/null

  local phase; phase="$(status_field phase)"
  assert_not_contains "$phase" "running" \
    "a killed run left phase=$phase, so the panel polls forever for a status nobody wrote"
  assert_contains "$(status_json)" "without finishing" "and it must say what happened"
  assert_contains "$(status_json)" "--rollback" "and name the way out"
}

# ======================================================================================
# The status file
# ======================================================================================

test_the_status_file_is_valid_json_at_every_phase() {
  new_sandbox status-json; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --apply-release
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$FAKE/state/status.json" \
    || fail_test "status.json is not valid JSON"
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$FAKE/state/status-public.json" \
    || fail_test "status-public.json is not valid JSON"
}

test_the_public_status_leaks_no_digests_revisions_or_logs() {
  new_sandbox status-public; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  printf 'sha256:secret-digest\n' > "$FAKE/image.api"
  run_deploy "$work" --apply-release
  local public; public="$(cat "$FAKE/state/status-public.json")"
  assert_not_contains "$public" "sha256" "the unauthenticated status must not carry image digests"
  assert_not_contains "$public" "log_tail" "nor log output"
  assert_not_contains "$public" "revision" "nor git revisions"
  assert_contains "$public" '"phase"' "but it must carry the phase, which is the point of it"
}

test_status_prints_the_last_run() {
  new_sandbox status-print; local work="$WORK"
  add_release "$work"
  seed_env "$work" SAIVA_SITE_ADDRESS=https://192.168.0.11
  site_up
  run_deploy "$work" --apply-release
  run_deploy "$work" --status
  assert_contains "$OUT" '"phase": "applied"' "--status must print what the last run did"
}

# ======================================================================================

for t in $(declare -F | awk '{print $3}' | sed -n 's/^test_//p'); do run_test "$t"; done

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '%sfailed:%s %s\n' "$RED" "$OFF" "${FAILED_NAMES[*]}"
  exit 1
fi
printf '%sall deploy tests passed%s %s(fake docker, fake curl)%s\n' "$GREEN" "$OFF" "$DIM" "$OFF"
