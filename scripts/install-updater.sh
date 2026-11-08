#!/usr/bin/env bash
# One-time install of the host side of the in-app update button.
#
#   sudo ./scripts/install-updater.sh
#
# Run it once per box, and again whenever `scripts/saiva-update-agent` changes (the
# agent's --self-check will tell you when that is). Everything else about Saiva updates
# itself; this does not, and that is the deliberate boundary: the agent runs as root,
# so a release must not be able to change it without a person looking.
#
# It is idempotent. Running it twice changes nothing the second time.
#
# What it installs:
#   /usr/local/lib/saiva/saiva-update-agent   the agent, copied out of the checkout
#   /etc/saiva/agent.conf                     where the checkout and the state live
#   /var/lib/saiva/update                     root-owned; status, logs, rollback records
#   /var/lib/saiva/update/requests            the one directory the app may write into
#   systemd units (if systemd is running)     a .path that watches for a request
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AGENT_SRC="$SCRIPT_DIR/saiva-update-agent"
AGENT_DST=/usr/local/lib/saiva/saiva-update-agent
CONF_DIR=/etc/saiva
CONF="$CONF_DIR/agent.conf"
STATE="${SAIVA_STATE_DIR:-/var/lib/saiva/update}"
UNIT_DIR=/etc/systemd/system

[ "$(id -u)" = "0" ] || { echo "This needs root: sudo $0" >&2; exit 1; }
[ -r "$AGENT_SRC" ] || { echo "Cannot read $AGENT_SRC — run this from the checkout." >&2; exit 1; }
[ -d "$ROOT/.git" ] || { echo "WARNING: $ROOT is not a git checkout, so the updater cannot" >&2
                         echo "         deliver compose, Caddyfile or script changes." >&2; }

# Which uid the api container runs as matters: it writes the request file into $STATE
# through a bind mount, so that directory has to be writable by it. backend/Dockerfile
# fixes the uid at 10001 (`useradd --uid 10001 appuser`), which is read from the
# Dockerfile rather than hardcoded here so the two cannot drift, and confirmed against a
# running container when there is one.
#
# There is deliberately no world-writable fallback. An earlier version chmod 0777 when it
# could not work the uid out, which puts a directory Caddy serves files from, and that a
# root oneshot watches, within reach of every account on the box. If the uid cannot be
# determined this refuses and says what to pass.
api_uid_from_dockerfile() {
  sed -n 's/.*useradd[^#]*--uid[[:space:]]\{1,\}\([0-9]\{1,\}\).*/\1/p' \
    "$ROOT/backend/Dockerfile" 2>/dev/null | head -n 1
}
API_UID="${SAIVA_API_UID:-}"
if [ -z "$API_UID" ] && command -v docker >/dev/null 2>&1; then
  API_UID="$(docker compose -f "$ROOT/docker-compose.prod.yml" exec -T api id -u 2>/dev/null \
    | tr -cd '0-9' || true)"
fi
[ -n "$API_UID" ] || API_UID="$(api_uid_from_dockerfile)"
case "$API_UID" in
  ''|*[!0-9]*|0)
    echo "ERROR: could not work out the uid the api container runs as." >&2
    echo "       Pass it: sudo SAIVA_API_UID=\$(docker compose -f docker-compose.prod.yml \\" >&2
    echo "                  exec -T api id -u) $0" >&2
    exit 1 ;;
esac

echo "Installing the Saiva update agent"
echo "  checkout : $ROOT"
echo "  state    : $STATE"

install -d -m 0755 "$(dirname "$AGENT_DST")"
install -m 0755 "$AGENT_SRC" "$AGENT_DST"
echo "  → $AGENT_DST"

install -d -m 0755 "$CONF_DIR"
cat > "$CONF" <<EOF
# Read by $AGENT_DST as root. Two values, no logic: anything sourced here runs as root.
SAIVA_CHECKOUT=$ROOT
SAIVA_STATE_DIR=$STATE
EOF
chmod 0644 "$CONF"
echo "  → $CONF"

# The layout, and why each mode is what it is. The first version of this handed the
# directory itself to the api container, and an adversarial review showed what that
# allows: owning a directory means being able to unlink and rename root-owned entries
# inside it, so the container could point root's `deploy.lock` and `run.log` at any file
# on the host (both are opened by shell redirections, which follow symlinks), point
# `run.log` at a file it wanted to READ back through the status API, and rename
# `records/` aside so a rollback restored its .env and pinned its images.
#
# So the container gets a drop-box, not the directory:
#
#   $STATE            root:root 0755   everything root writes; the container reads only
#   $STATE/records    root:root 0700   holds a copy of .env -- nobody else reads it
#   $STATE/requests   root:<api> 0730  create and delete only; 0730 means it cannot even
#                                      list what is in there
#
# And root never opens anything inside requests/: the agent asks `find -type f`, which
# does not follow symlinks, and unlinks what it finds.
install -d -o root -g root -m 0755 "$STATE"
install -d -o root -g root -m 0700 "$STATE/records"
install -d -o root -g "$API_UID" -m 0730 "$STATE/requests"
# Re-assert on an upgrade from the old layout, where install -d leaves an existing
# directory's mode alone.
chown root:root "$STATE" && chmod 0755 "$STATE"
chown root:root "$STATE/records" && chmod 0700 "$STATE/records"
chown "root:$API_UID" "$STATE/requests" && chmod 0730 "$STATE/requests"
echo "  → $STATE (root-owned)"
echo "  → $STATE/requests (the api container, uid $API_UID, may drop a request here)"

if [ -d /run/systemd/system ]; then
  cat > "$UNIT_DIR/saiva-update.service" <<EOF
[Unit]
Description=Apply the newest Saiva release
Documentation=file://$ROOT/docs/UPDATING.md
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$AGENT_DST
# An update pulls images, recreates containers and waits on a health gate. Generous, but
# finite: a hung update must not hold the lock forever.
TimeoutStartSec=1800
EOF

  cat > "$UNIT_DIR/saiva-update.path" <<EOF
[Unit]
Description=Watch for an in-app Saiva update request
Documentation=file://$ROOT/docs/UPDATING.md

[Path]
# A glob over the drop-box rather than one fixed filename: the api writes a uniquely
# named request so two presses cannot collide on the same path, and the agent removes
# everything it finds there, so the unit cannot be left re-triggering on an entry it
# refused to act on.
PathExistsGlob=$STATE/requests/*
Unit=saiva-update.service

[Install]
WantedBy=paths.target
EOF

  systemctl daemon-reload
  systemctl enable --now saiva-update.path >/dev/null
  echo "  → systemd: saiva-update.path enabled (watching $STATE/request)"
  echo ""
  echo "Installed. The in-app Update now button will work on the next page load."
  echo "Check it with:  systemctl status saiva-update.path"
  echo "Watch a run:    journalctl -u saiva-update.service -f"
else
  echo "  → systemd is not running here, so no units were installed."
  echo ""
  echo "Installed, but nothing is watching for requests yet. Start the agent's poll"
  echo "loop under whatever supervises services on this host:"
  echo "    $AGENT_DST --watch"
  echo 'Until then the button records a request that nothing acts on; `make update` on'
  echo "the host works regardless."
fi
echo ""
echo "The agent is a COPY, on purpose: a release cannot change what runs as root here."
echo "Re-run this installer when it changes. It will tell you:"
echo "    sudo $AGENT_DST --self-check"
