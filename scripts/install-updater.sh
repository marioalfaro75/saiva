#!/usr/bin/env bash
# One-time install of the host side of Saiva: the update button, and the nightly backup.
#
#   sudo ./scripts/install-updater.sh
#
# Run it once per box, and again whenever `scripts/saiva-update-agent` or the unit files
# change (the agent's --self-check will tell you when that is). Everything else about
# Saiva updates itself; this does not, and that is the deliberate boundary: the agent runs
# as root, so a release must not be able to change it without a person looking.
#
# It is idempotent. Running it twice changes nothing the second time.
#
# What it installs:
#   /usr/local/lib/saiva/saiva-update-agent   the agent, copied out of the checkout
#   /etc/saiva/agent.conf                     the checkout, the state, the backup settings
#   /var/lib/saiva/update                     root-owned; status, logs, rollback records
#   /var/lib/saiva/update/requests            "Update now" -- the app may write here
#   /var/lib/saiva/update/backup-requests     "Back up now" -- and here, and nowhere else
#   systemd units (if systemd is running)     two .path units, and a nightly .timer
#
# WHY THE AGENT IS A COPY AND THE TWO SCRIPTS ARE NOT. The agent decides WHETHER to act,
# and is pinned here so that a release cannot change what runs as root unattended. What it
# then runs -- scripts/deploy.sh and scripts/backup.sh -- comes from the checkout, because
# those improve with releases and because installing a release already means trusting its
# scripts: deploy.sh runs as root the moment you press Update now. The nightly timer runs
# scripts/backup.sh from the checkout for the same reason, with one addition worth being
# explicit about: it runs unattended, at night, so a release you have applied gets root on
# this box at 02:30 whether or not you are watching. That is the same trust you gave it
# when you applied it, not a new one -- but it is the reason the unit names the checkout
# path verbatim rather than following a symlink that could be repointed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AGENT_SRC="$SCRIPT_DIR/saiva-update-agent"
AGENT_DST=/usr/local/lib/saiva/saiva-update-agent
CONF_DIR=/etc/saiva
CONF="$CONF_DIR/agent.conf"
UNIT_DIR=/etc/systemd/system

# WHERE THE STATE DIRECTORY IS, decided the same way everything else decides it: .env,
# because .env is what docker-compose interpolates the api and caddy bind mounts from, so
# it is the only answer the containers can actually see. An explicit SAIVA_STATE_DIR in
# this script's environment still wins, for the box where somebody is moving it.
#
# This used to default straight to /var/lib/saiva/update, and then an .env that said
# anything else got a drop-box created and permissioned in one place, systemd path units
# watching that place, and the scripts writing their records somewhere else entirely.
state_dir_from_env() {
  local v=""
  if [ -r "$ROOT/.env" ]; then
    v="$(sed -n 's/^[[:space:]]*SAIVA_STATE_DIR=//p' "$ROOT/.env" | tail -n 1)"
    v="${v%%[[:space:]]#*}"
    v="${v%"${v##*[![:space:]]}"}"
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  fi
  printf '%s' "${v:-/var/lib/saiva/update}"
}
STATE="${SAIVA_STATE_DIR:-$(state_dir_from_env)}"

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

echo "Installing the Saiva host side: the update agent, and the nightly backup"
echo "  checkout : $ROOT"
echo "  state    : $STATE"

install -d -m 0755 "$(dirname "$AGENT_DST")"
install -m 0755 "$AGENT_SRC" "$AGENT_DST"
echo "  → $AGENT_DST"

install -d -m 0755 "$CONF_DIR"
# Written once and then left alone: the backup settings below are the owner's to edit, and
# re-running this installer must not reset a destination somebody configured.
if [ -f "$CONF" ]; then
  echo "  → $CONF (already present, left as it is)"
  # The two values the agent cannot work out for itself are still re-asserted, because a
  # moved checkout is the one change that silently stops everything.
  for kv in "SAIVA_CHECKOUT=$ROOT" "SAIVA_STATE_DIR=$STATE"; do
    key="${kv%%=*}"
    if grep -qE "^[[:space:]]*${key}=" "$CONF"; then
      sed -i "s|^[[:space:]]*${key}=.*|${kv}|" "$CONF"
    else
      printf '%s\n' "$kv" >> "$CONF"
    fi
  done
else
  cat > "$CONF" <<EOF
# Read by $AGENT_DST and by $ROOT/scripts/backup.sh, as root.
#
# NOTHING HERE IS SOURCED by the backup script -- it reads the keys it wants and ignores
# the rest -- but the agent does source this file, so anything added below runs as root.
# backup.sh also REFUSES to act on the two settings at the bottom unless this file is
# owned by root and not writable by group or other, because a copy command read from a
# file anyone could edit would simply be a way to become root.
SAIVA_CHECKOUT=$ROOT
SAIVA_STATE_DIR=$STATE

# --- nightly backups -------------------------------------------------------------------
# WHERE A VERIFIED BACKUP GOES AFTER THIS BOX. Empty means nowhere, and then every copy of
# your database lives on the disk it is protecting you against. A directory on a NAS, an
# NFS or CIFS mount, or a Proxmox mount point from the host -- the panel says how far away
# it actually got, and only a network filesystem or the push command below counts as off
# this machine.
SAIVA_BACKUP_COPY_TO=

# Instead of, or as well as, a directory: a command handed the dump's path as \$1 and its
# name as \$2. This is how rclone, restic or rsync-over-ssh fit, e.g.
#   SAIVA_BACKUP_COPY_CMD=rclone copyto "\$1" "remote:saiva/\$2"
SAIVA_BACKUP_COPY_CMD=

# How many nightly dumps to keep. The -premigrate dumps the app writes before a migration,
# and the -prerestore dumps a restore writes before replacing a database, are NEVER pruned
# automatically -- the backup script may remove what it created and nothing else.
SAIVA_BACKUP_KEEP_NIGHTLY=14
EOF
  echo "  → $CONF"
fi
chmod 0644 "$CONF"
chown root:root "$CONF"

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
install -d -o root -g "$API_UID" -m 0730 "$STATE/backup-requests"
# Re-assert on an upgrade from the old layout, where install -d leaves an existing
# directory's mode alone.
chown root:root "$STATE" && chmod 0755 "$STATE"
chown root:root "$STATE/records" && chmod 0700 "$STATE/records"
chown "root:$API_UID" "$STATE/requests" && chmod 0730 "$STATE/requests"
chown "root:$API_UID" "$STATE/backup-requests" && chmod 0730 "$STATE/backup-requests"
echo "  → $STATE (root-owned)"
echo "  → $STATE/requests        (the api container, uid $API_UID, may drop a request here)"
echo "  → $STATE/backup-requests (and here -- two directories, because WHICH one a request"
echo "                            landed in is the only thing the container gets to say)"

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

  # The nightly backup. ExecStart points into the checkout rather than at a copy: see the
  # header for why, and for the one thing that is different about it running unattended.
  cat > "$UNIT_DIR/saiva-backup.service" <<EOF
[Unit]
Description=Nightly Saiva database backup, verified by a real restore
Documentation=file://$ROOT/docs/BACKUPS.md
After=docker.service network-online.target
# Wants, not Requires. Requires turns a readable, actionable error into a generic unit
# failure, and scripts/backup.sh already says exactly what is wrong and what to do about
# it -- including telling the difference between "the stack is down" (which it records as
# skipped) and "the dump failed" (which it does not).
Wants=docker.service

[Service]
Type=oneshot
ExecStart=$ROOT/scripts/backup.sh --trigger timer
# A dump plus a verify-by-real-restore is slow on a large ledger. Generous, but finite.
TimeoutStartSec=3600
# Hardening that is safe for a unit that has to drive the docker CLI. Everything here was
# checked with systemd-analyze verify. Deliberately ABSENT, and each for a reason:
#   PrivateTmp           a host path passed to \`docker run -v\` is resolved in the
#                        DAEMON's mount namespace, so PrivateTmp silently mounts the real
#                        /tmp/x instead of the private one -- a wrong directory, not an
#                        error.
#   ProtectSystem=strict /var/lib/saiva would be read-only and the status file could not
#                        be written.
#   ProtectHome          hides /home and /root, and a checkout under either then vanishes.
#   PrivateNetwork, IPAddressDeny, RestrictAddressFamilies without AF_UNIX
#                        each breaks either the docker socket or the off-box copy.
#   User= / DynamicUser  no docker access without the docker group, and membership of the
#                        docker group IS root. A "backup" user here would be theatre.
NoNewPrivileges=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true
EOF

  cat > "$UNIT_DIR/saiva-backup.timer" <<EOF
[Unit]
Description=Nightly Saiva database backup
Documentation=file://$ROOT/docs/BACKUPS.md

[Timer]
OnCalendar=*-*-* 02:37:00
# Persistent, so a box that was off at 02:37 backs up when it comes back rather than
# silently skipping the night. On a Proxmox LXC that means a container started at 09:00
# runs a dump and a verify during the working day -- which is the right trade: the
# alternative is a machine that is off overnight never backing up and never saying so.
Persistent=true
# Spread across half an hour, so this does not land on the same minute as whatever else
# the host does at 02:30.
RandomizedDelaySec=1800
# Named rather than left to default, to say that systemd should not widen the window to
# coalesce this with other timers.
AccuracySec=1m
Unit=saiva-backup.service

[Install]
WantedBy=timers.target
EOF

  # And the "Back up now" button's path unit, watching the SECOND drop-box. A separate
  # unit, because the directory a request landed in is the only information root takes
  # from it -- the file itself is never opened.
  cat > "$UNIT_DIR/saiva-backup-request.service" <<EOF
[Unit]
Description=Take a Saiva backup because the app asked for one
Documentation=file://$ROOT/docs/BACKUPS.md
After=docker.service network-online.target
Wants=docker.service

[Service]
Type=oneshot
ExecStart=$AGENT_DST --backup
TimeoutStartSec=3600
NoNewPrivileges=true
EOF

  cat > "$UNIT_DIR/saiva-backup-request.path" <<EOF
[Unit]
Description=Watch for an in-app Saiva backup request
Documentation=file://$ROOT/docs/BACKUPS.md

[Path]
PathExistsGlob=$STATE/backup-requests/*
Unit=saiva-backup-request.service

[Install]
WantedBy=paths.target
EOF

  systemctl daemon-reload
  systemctl enable --now saiva-update.path >/dev/null
  systemctl enable --now saiva-backup-request.path >/dev/null
  systemctl enable --now saiva-backup.timer >/dev/null
  echo "  → systemd: saiva-update.path         (watching $STATE/requests)"
  echo "  → systemd: saiva-backup-request.path (watching $STATE/backup-requests)"
  echo "  → systemd: saiva-backup.timer        (nightly, 02:37 + up to 30 min)"
  echo ""
  echo "Installed. The in-app Update now and Back up now buttons work on the next page load."
  echo "Check it with:  systemctl status saiva-update.path saiva-backup.timer"
  echo "Next backup:    systemctl list-timers saiva-backup.timer"
  echo "Watch a run:    journalctl -u saiva-backup.service -f"
  echo ""
  echo "Take the first one now, rather than finding out at 02:37 whether it works:"
  echo "    sudo $ROOT/scripts/backup.sh"
else
  echo "  → systemd is not running here, so no units were installed."
  echo ""
  echo "Installed, but nothing is watching for requests and NOTHING TAKES A NIGHTLY"
  echo "BACKUP yet. Start the agent's poll loop under whatever supervises services here:"
  echo "    $AGENT_DST --watch"
  echo "and add the nightly backup to cron, as root:"
  echo "    37 2 * * *  $ROOT/scripts/backup.sh --trigger timer"
  echo 'Until then the buttons record requests that nothing acts on; `make update` and'
  echo "\`./scripts/backup.sh\` on the host work regardless."
fi

if ! grep -qE '^[[:space:]]*SAIVA_BACKUP_COPY_(TO|CMD)=.+' "$CONF"; then
  echo ""
  echo "NO OFF-BOX COPY IS CONFIGURED, so every backup will live on the same disk as the"
  echo "database it protects. One disk failure loses both. Set SAIVA_BACKUP_COPY_TO (or"
  echo "SAIVA_BACKUP_COPY_CMD) in $CONF -- the Settings page says which of the two it is"
  echo "looking at, and will not call a copy on this machine an off-box backup."
fi
echo ""
echo "The agent is a COPY, on purpose: a release cannot change what runs as root here."
echo "Re-run this installer when it changes. It will tell you:"
echo "    sudo $AGENT_DST --self-check"
