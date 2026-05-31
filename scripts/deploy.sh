#!/usr/bin/env bash
# Saiva — one-command container deploy.
#
# Bootstraps .env (with a secure random SECRET_KEY and DB password), builds the
# images, starts the stack, waits until the app is healthy, and prints how to reach it.
#
# Usage:
#   ./scripts/deploy.sh                Build + start on https://localhost (this machine)
#   ./scripts/deploy.sh --lan          ... reachable over your LAN at https://<auto-detected-ip>
#   ./scripts/deploy.sh --site <addr>  ... at a specific address (https://192.168.1.50 or a domain)
#   ./scripts/deploy.sh --seed         ... and load demo data
#   ./scripts/deploy.sh --down         Stop the stack (database preserved)
#   ./scripts/deploy.sh --destroy      Stop and wipe the database volume
#   ./scripts/deploy.sh --help         Show this help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

SEED=0
LAN=0
SITE=""
ACTION="up"
while [ $# -gt 0 ]; do
  case "$1" in
    --seed|--demo) SEED=1 ;;
    --lan) LAN=1 ;;
    --site) shift; SITE="${1:-}"; [ -n "$SITE" ] || { echo "--site needs a value, e.g. https://192.168.1.50" >&2; exit 2; } ;;
    --site=*) SITE="${1#*=}" ;;
    --down) ACTION="down" ;;
    --destroy) ACTION="destroy" ;;
    -h|--help) ACTION="help" ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$ACTION" = "help" ]; then
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
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

if [ "$ACTION" = "down" ]; then
  echo "Stopping Saiva (database preserved)…"
  exec $COMPOSE down
fi
if [ "$ACTION" = "destroy" ]; then
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

set_env_var() { # key value — update in place or append to .env
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

# 1. Bootstrap .env — never overwrite an existing one's secrets.
if [ ! -f .env ]; then
  echo "Creating .env from .env.example with generated secrets…"
  cp .env.example .env
  set_env_var SECRET_KEY "$(gen_secret)"
  set_env_var POSTGRES_PASSWORD "$(gen_secret | cut -c1-24)"
  echo "  → generated a random SECRET_KEY and POSTGRES_PASSWORD"
else
  echo ".env already exists — leaving its secrets untouched."
fi

# 2. Resolve the site address (LAN auto-detect / explicit), if requested.
if [ "$LAN" -eq 1 ] && [ -z "$SITE" ]; then
  lan_ip="$(detect_lan_ip)"
  [ -n "$lan_ip" ] || { echo "ERROR: couldn't auto-detect a LAN IP — pass --site https://<ip>." >&2; exit 1; }
  SITE="https://${lan_ip}"
fi
if [ -n "$SITE" ]; then
  set_env_var SAIVA_SITE_ADDRESS "$SITE"
  echo "  → serving at ${SITE}"
fi

# 3. Build and start.
echo "Building images and starting the stack…"
$COMPOSE up -d --build

# 4. Wait until the API reports healthy (checked inside the container, so this is
#    independent of the configured hostname / TLS).
echo -n "Waiting for the app to become healthy"
healthy=0
for _ in $(seq 1 60); do
  if $COMPOSE exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" \
      >/dev/null 2>&1; then
    healthy=1
    break
  fi
  echo -n "."
  sleep 2
done
echo

if [ "$healthy" -ne 1 ]; then
  echo "ERROR: the app did not become healthy in time. Recent logs:" >&2
  $COMPOSE logs --tail 40 api caddy >&2 || true
  exit 1
fi
echo "✅ Saiva is up."

# 5. Optional demo data.
if [ "$SEED" -eq 1 ]; then
  echo "Seeding demo data…"
  $COMPOSE exec -T api python -m app.services.seed || echo "  (seed skipped or already present)" >&2
fi

# 6. Where to reach it.
site_raw="$(grep -E '^SAIVA_SITE_ADDRESS=' .env | cut -d= -f2- || true)"
site_raw="${site_raw:-localhost}"
case "$site_raw" in
  http://*|https://*) url="$site_raw" ;;
  *) url="https://$site_raw" ;;
esac

cat <<EOF

  Open:    ${url}/
  Health:  curl -k ${url}/api/health
  Logs:    ${COMPOSE} logs -f api
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
[ "$SEED" -eq 1 ] && echo "  Demo login: demo@saiva.app / demodemodemo"
exit 0
