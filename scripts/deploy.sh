#!/usr/bin/env bash
# Saiva — one-command container deploy.
#
# Bootstraps .env (with a secure random SECRET_KEY and DB password), builds the
# images, starts the stack, waits until the app is healthy, and prints how to reach it.
#
# Usage:
#   ./scripts/deploy.sh            Build + start (creates .env on first run)
#   ./scripts/deploy.sh --seed     ... and load demo data
#   ./scripts/deploy.sh --down     Stop the stack (database preserved)
#   ./scripts/deploy.sh --destroy  Stop and wipe the database volume
#   ./scripts/deploy.sh --help     Show this help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

SEED=0
ACTION="up"
for arg in "$@"; do
  case "$arg" in
    --seed|--demo) SEED=1 ;;
    --down) ACTION="down" ;;
    --destroy) ACTION="destroy" ;;
    -h|--help) ACTION="help" ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$ACTION" = "help" ]; then
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
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

# 1. Bootstrap .env — never overwrite an existing one.
if [ ! -f .env ]; then
  echo "Creating .env from .env.example with generated secrets…"
  cp .env.example .env
  secret="$(gen_secret)"
  dbpass="$(gen_secret | cut -c1-24)"
  sed -i.bak \
    -e "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${dbpass}|" \
    .env
  rm -f .env.bak
  echo "  → generated a random SECRET_KEY and POSTGRES_PASSWORD"
else
  echo ".env already exists — leaving it untouched."
fi

# 2. Build and start.
echo "Building images and starting the stack…"
$COMPOSE up -d --build

# 3. Wait until the API reports healthy (checked inside the container, so this is
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

# 4. Optional demo data.
if [ "$SEED" -eq 1 ]; then
  echo "Seeding demo data…"
  if $COMPOSE exec -T api python -m app.services.seed; then
    :
  else
    echo "  (seed skipped or already present)" >&2
  fi
fi

site="$(grep -E '^SAIVA_SITE_ADDRESS=' .env | cut -d= -f2- || true)"
site="${site:-localhost}"
cat <<EOF

  Open:    https://${site}/      (accept the local TLS certificate the first time)
  Health:  curl -k https://localhost/api/health
  Logs:    ${COMPOSE} logs -f api
  Stop:    ./scripts/deploy.sh --down       (keep data)
           ./scripts/deploy.sh --destroy    (wipe data)
EOF
[ "$SEED" -eq 1 ] && echo "  Demo login: demo@saiva.app / demodemodemo"
exit 0
