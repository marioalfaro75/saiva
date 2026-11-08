#!/usr/bin/env bash
# Everything CI runs, in one command, with the same invocations CI uses.
#
#   ./scripts/check-all.sh
#
# This exists because "it passed locally" kept meaning something narrower than "CI will
# pass". Twice now the difference was a ruff finding in a file that happened not to be in
# the argument list I typed: CI runs `ruff check .` from backend/, and `ruff check app/`
# is not the same command. Running the real one takes the same few seconds.
#
# Not covered here, and the honest limits of that:
#   * image-smoke.yml boots the built images. That needs a Docker daemon.
#   * The Postgres-backed suites skip when no Postgres is reachable, and say so. Point
#     SAIVA_TEST_PG_URL / PGHOST at one to include them -- without that, the rollback and
#     restore rehearsals are not being run, however green this looks.
#   * A few backend tests reach the network (the AI advisor's provider checks resolve a
#     real host). Behind a proxy that does not resolve it they fail here and pass in CI.
#     They are not special-cased, because a script that skips the tests it finds
#     inconvenient is the kind of green that means nothing -- read the names.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PY="${SAIVA_PYTHON:-$ROOT/backend/.venv/bin/python}"
FAILED=()
run() { # LABEL COMMAND...
  local label="$1"; shift
  printf '\n\033[1m── %s\033[0m\n' "$label"
  if "$@"; then printf '\033[32m   ok\033[0m\n'; else FAILED+=("$label"); printf '\033[31m   FAILED\033[0m\n'; fi
}

run "backend: ruff (as CI runs it, from backend/)" \
  bash -c "cd '$ROOT/backend' && '$PY' -m ruff check ."
run "backend: mypy" bash -c "cd '$ROOT/backend' && '$PY' -m mypy app"
run "backend: bandit" \
  bash -c "cd '$ROOT/backend' && '$PY' -m bandit -r app -q --skip B101,B105,B311"
run "backend: pytest" bash -c "cd '$ROOT/backend' && '$PY' -m pytest -q"
run "backend: lockfile matches pyproject" \
  bash -c "cd '$ROOT/backend' && '$PY' '$ROOT/scripts/check_lockfile.py'"

run "shell: syntax" bash -c '
  for f in scripts/*.sh scripts/lib/*.sh scripts/saiva-update-agent scripts/tests/*.sh; do
    bash -n "$f" || exit 1
  done'
if command -v shellcheck >/dev/null 2>&1; then
  run "shell: shellcheck" shellcheck -S warning \
    scripts/*.sh scripts/lib/*.sh scripts/saiva-update-agent
else
  printf '\n\033[2m── shell: shellcheck (not installed, skipped)\033[0m\n'
fi
run "deploy path (fake docker, fake curl)" ./scripts/tests/run.sh
run "restore round trip (needs Postgres)" ./scripts/tests/test-restore-backup.sh

run "compose files parse" bash -c '
  SECRET_KEY=x SAIVA_SITE_ADDRESS=https://example.invalid \
    docker compose -f docker-compose.prod.yml config >/dev/null &&
  SECRET_KEY=x docker compose -f docker-compose.yml config >/dev/null'
run "workflows parse" "$PY" "$ROOT/scripts/check_workflows.py"

run "frontend: lint" bash -c "cd '$ROOT/frontend' && npm run --silent lint"
run "frontend: types + build" bash -c "cd '$ROOT/frontend' && npm run --silent build"
run "frontend: tests" bash -c "cd '$ROOT/frontend' && npx vitest run"

printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '\033[32mEverything CI runs here passed.\033[0m\n'
  exit 0
fi
printf '\033[31m%d failed:\033[0m %s\n' "${#FAILED[@]}" "${FAILED[*]}"
exit 1
