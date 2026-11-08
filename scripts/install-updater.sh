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
#   /var/lib/saiva/update                     the request + status directory
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

# Which user the containers run as matters: the api container writes the request file
# into $STATE through a bind mount, so the directory has to be writable by the uid the
# api process runs under. The image runs as a non-root user, so the simplest arrangement
# that is not world-writable is a dedicated group both sides are in.
API_UID="${SAIVA_API_UID:-}"
if [ -z "$API_UID" ] && command -v docker >/dev/null 2>&1; then
  API_UID="$(docker compose -f "$ROOT/docker-compose.prod.yml" exec -T api id -u 2>/dev/null | tr -d '\r' || true)"
fi

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

install -d -m 0775 "$STATE"
install -d -m 0755 "$STATE/records"
if [ -n "$API_UID" ] && [ "$API_UID" -gt 0 ] 2>/dev/null; then
  # Group-own the directory by the api container's gid so the button can write its
  # request file, without making it world-writable.
  chgrp "$API_UID" "$STATE" 2>/dev/null || chown ":$API_UID" "$STATE" 2>/dev/null || true
  chmod 0775 "$STATE"
  echo "  → $STATE (group-writable by the api container's uid $API_UID)"
else
  chmod 0777 "$STATE"
  echo "  → $STATE"
  echo "    NOTE: could not determine the api container's uid, so this directory is"
  echo "          world-writable. Nothing secret is kept in it and the agent ignores the"
  echo "          request file's contents, but you can tighten it with:"
  echo "            sudo chgrp <uid> $STATE && sudo chmod 0775 $STATE"
  echo '          where <uid> is: docker compose -f docker-compose.prod.yml exec api id -u'
fi

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
PathExists=$STATE/request
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
