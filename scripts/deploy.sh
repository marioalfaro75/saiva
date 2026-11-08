#!/usr/bin/env bash
# Saiva — the one command that installs, updates and rolls back this deployment.
#
# Usage:
#   ./scripts/deploy.sh                  Build + start on https://localhost (this machine)
#   ./scripts/deploy.sh --lan            ... reachable over your LAN at https://<auto-detected-ip>
#   ./scripts/deploy.sh --site <addr>    ... at a specific address (https://192.168.1.50 or a domain)
#   ./scripts/deploy.sh --pull           ... using prebuilt GHCR images (no local build)
#   ./scripts/deploy.sh --seed           ... and load demo data
#   ./scripts/deploy.sh --apply-release  Apply the newest release: tree, config and images,
#                                        gated on the site answering, rolled back if it does not
#   ./scripts/deploy.sh --rollback       Undo the last applied release
#   ./scripts/deploy.sh --status         What the last update did, and where it got to
#   ./scripts/deploy.sh --down           Stop the stack (database preserved)
#   ./scripts/deploy.sh --destroy        Stop and wipe the database volume
#   ./scripts/deploy.sh --help           Show this help
#
# --apply-release is the path `make update` and the in-app "Update now" button both take.
# scripts/lib/release.sh describes what it does and why it is one path and not three.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

SEED=0
LAN=0
PULL=0
SITE=""
ACTION="up"
# Read by scripts/lib/release.sh, which is sourced below. Exported rather than left as
# shell variables so that shellcheck can see they are used, and so that the hand-over in
# the release path (which replaces this process after `git checkout`) carries them.
export TARGET_REF=""
export RESUME_RUN=""
export ALLOW_DOWNGRADE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --seed|--demo) SEED=1 ;;
    --lan) LAN=1 ;;
    --pull) PULL=1 ;;
    --site) shift; SITE="${1:-}"; [ -n "$SITE" ] || { echo "--site needs a value, e.g. https://192.168.1.50" >&2; exit 2; } ;;
    --site=*) SITE="${1#*=}" ;;
    --apply-release|--update) ACTION="apply-release"; PULL=1 ;;
    --ref) shift; TARGET_REF="${1:-}"; [ -n "$TARGET_REF" ] || { echo "--ref needs a tag, e.g. v0.17.0" >&2; exit 2; } ;;
    --ref=*) TARGET_REF="${1#*=}" ;;
    --resume) shift; RESUME_RUN="${1:-}"; [ -n "$RESUME_RUN" ] || { echo "--resume needs a run id" >&2; exit 2; } ;;
    --resume=*) RESUME_RUN="${1#*=}" ;;
    --allow-downgrade) ALLOW_DOWNGRADE=1 ;;
    --rollback) ACTION="rollback"; PULL=1 ;;
    --status) ACTION="status" ;;
    --down) ACTION="down" ;;
    --destroy) ACTION="destroy" ;;
    -h|--help) ACTION="help" ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$ACTION" = "help" ]; then
  sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

# shellcheck source=lib/env-file.sh
. "$SCRIPT_DIR/lib/env-file.sh"
# shellcheck source=lib/state.sh
. "$SCRIPT_DIR/lib/state.sh"
# shellcheck source=lib/release.sh
. "$SCRIPT_DIR/lib/release.sh"

if [ "$ACTION" = "status" ]; then
  f="$(state_dir)/status.json"
  if [ -f "$f" ]; then cat "$f"; else echo "No update has been run on this box yet."; fi
  exit 0
fi

# Prefer the Compose v2 plugin; fall back to legacy docker-compose.
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "ERROR: Docker Compose not found. Install Docker (Desktop) or the compose plugin." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: the Docker daemon isn't running. Start Docker and retry." >&2
  exit 1
fi

# Pull mode — and every release path — uses the production compose file.
if [ "$PULL" -eq 1 ]; then
  COMPOSE="$COMPOSE -f docker-compose.prod.yml"
fi

if [ "$ACTION" = "down" ]; then
  echo "Stopping Saiva (database preserved)…"
  exec $COMPOSE down
fi
if [ "$ACTION" = "destroy" ]; then
  # The confirmation lives in one place so the Makefile and this script cannot drift. It
  # names the backups, because `down -v` removes the db_backups volume as well as the
  # data, and that is the thing people do not expect.
  "$SCRIPT_DIR/confirm-destroy.sh" || exit 1
  echo "Stopping Saiva and wiping the database volume…"
  exec $COMPOSE down -v
fi

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

detect_lan_ip() {
  local lan_ip=""
  if command -v ip >/dev/null 2>&1; then
    lan_ip="$(ip route get 1.1.1.1 2>/dev/null \
      | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  fi
  [ -n "$lan_ip" ] || lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "$lan_ip"
}

# --- The release paths. Everything below this point is the install/dev path. ---------

if [ "$ACTION" = "rollback" ]; then
  rollback_last
  exit 0
fi
if [ "$ACTION" = "apply-release" ]; then
  apply_release
  exit 0
fi

# 1. Bootstrap .env — never overwrite an existing one's secrets.
FIRST_INSTALL=0
if [ ! -f .env ]; then
  FIRST_INSTALL=1
  echo "Creating .env from .env.example with generated secrets…"
  cp .env.example .env
  env_set SECRET_KEY "$(gen_secret)"
  env_set POSTGRES_PASSWORD "$(gen_secret | cut -c1-24)"
  # Loopback only, written exactly once, on the install that creates the file. The
  # binding is persisted state from here on: see scripts/lib/env-file.sh for why the
  # `else` branch that used to live further down took the production site off the LAN.
  env_set SAIVA_BIND_ADDR "127.0.0.1"
  echo "  → generated a random SECRET_KEY and POSTGRES_PASSWORD"
else
  echo ".env already exists — leaving its secrets untouched."
fi

# Backfill anything a newer release introduced, so upgrading an older .env needs no
# manual edit. Adds keys that are missing; never changes a value that is already there.
env_reconcile || exit 1

# 2. Resolve the site address (LAN auto-detect / explicit), if requested.
if [ "$LAN" -eq 1 ] && [ -z "$SITE" ]; then
  lan_ip="$(detect_lan_ip)"
  [ -n "$lan_ip" ] || { echo "ERROR: couldn't auto-detect a LAN IP — pass --site https://<ip>." >&2; exit 1; }
  SITE="https://${lan_ip}"
fi
if [ -n "$SITE" ]; then
  env_set SAIVA_SITE_ADDRESS "$SITE"
  host="${SITE#http://}"; host="${host#https://}"; host="${host%%/*}"
  env_set SAIVA_SITE_HOST "$host"
  # Asking to be served at an address is asking to be reachable at it, so the published
  # ports follow the same decision.
  env_set SAIVA_BIND_ADDR "0.0.0.0"
  echo "  → serving at ${SITE} (published on all interfaces)"
fi
# There is deliberately no `else` here. A flag's ABSENCE must never change persisted
# state: `make pull` with no flags used to land in one and rewrite SAIVA_BIND_ADDR to
# 127.0.0.1, which un-published the site on a box that had been reachable on the LAN for
# months. scripts/tests/ runs the deploy twice with no flags and asserts .env comes back
# byte-identical, so the branch cannot come back by accident.

# HSTS only where a browser can actually verify the certificate. Caddy's `internal`
# CA is not in any trust store, and a non-zero max-age there turns the certificate
# warning into a dead end with no way past it — on a LAN IP that may later belong to
# a different machine entirely. An explicit 0 also clears a stale policy from a host
# that used to serve HSTS.
#
# This one derives persisted state from persisted state (SAIVA_TLS out of .env), which is
# why it is self-healing and the bind address was not.
tls_mode="$(env_get SAIVA_TLS)"
if [ -n "$tls_mode" ] && [ "$tls_mode" != "internal" ]; then
  env_set SAIVA_HSTS_MAX_AGE "31536000"
  [ "$FIRST_INSTALL" = "1" ] && echo "  → HSTS enabled (publicly trusted certificate)"
else
  env_set SAIVA_HSTS_MAX_AGE "0"
fi

# 3. Build (or pull) and start.
if [ "$PULL" -eq 1 ]; then
  echo "Pulling prebuilt images from GHCR and starting…"
  $COMPOSE pull
  $COMPOSE up -d
else
  echo "Building images and starting the stack…"
  $COMPOSE up -d --build
fi

# 4. Wait until the site answers at the address it is configured to serve, probed from
#    OUTSIDE the containers. The old loop ran urlopen inside the api container, which is
#    why it printed a tick while the site was unreachable from the LAN.
state_init
echo -n "Waiting for the app to answer at $(site_url)"
healthy=0
for _ in $(seq 1 "${SAIVA_GATE_TRIES:-60}"); do
  if probe_site "$(site_url)"; then healthy=1; break; fi
  echo -n "."
  sleep 2
done
echo

if [ "$healthy" -ne 1 ]; then
  echo "ERROR: the app did not answer at $(site_url). Recent logs:" >&2
  $COMPOSE logs --tail 40 api web caddy >&2 || true
  echo "" >&2
  echo "If the containers are running, check SAIVA_SITE_ADDRESS and SAIVA_BIND_ADDR in .env." >&2
  exit 1
fi
echo "✅ Saiva is up."

# 5. Optional demo data.
if [ "$SEED" -eq 1 ]; then
  echo "Seeding demo data…"
  $COMPOSE exec -T api python -m app.services.seed || echo "  (seed skipped or already present)" >&2
fi

# 6. Where to reach it.
url="$(site_url)"

cat <<EOF

  Open:    ${url}/
  Health:  curl -k ${url}/api/health
  Logs:    ${COMPOSE} logs -f api
  Update:  make update                     (or press Update now in the app)
  Stop:    ./scripts/deploy.sh --down       (keep data)
           ./scripts/deploy.sh --destroy    (wipe data)
EOF
case "$url" in
  https://localhost*) : ;;
  *)
    echo "  Note: LAN/IP addresses use Caddy's internal certificate — browsers show a one-time"
    echo "        warning until you trust its root CA (see the README's LAN section)."
    ;;
esac
# The demo password is generated per install and printed by the seed step above.
exit 0
