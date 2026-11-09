# shellcheck shell=bash
# The deployment's memory: what is running now, and what an update is doing about it.
#
# Two jobs, and the reason they are in one file is that they are the same idea.
#
# 1. THE ROLLBACK RECORD. Before anything is pulled or recreated, write down exactly what
#    is running: the git revision, the .env, and the resolved image DIGESTS. Digests, not
#    tags, because `latest` is not a thing you can go back to -- it is a moving pointer,
#    and by the time you want to roll back it already points at the release you are
#    rolling back from.
#
# 2. THE STATUS FILE. Where an update has got to, written on the HOST.
#
#    This is the part that matters most and is easiest to get wrong. The old design had
#    the api container trigger its own replacement, which means the process that would
#    report the outcome is the process being killed: app/services/updates.py threw the
#    response away in `except Exception: return`, the endpoint returned the literal
#    string "update_started", and the panel polled for four minutes and then rendered
#    "Update applied." whether or not anything had been applied. Three layers, each
#    independently discarding the failure.
#
#    Writing the status to a file on the host fixes that at the root, and it keeps
#    working for a reason worth stating: the STATE does not live in any container, so a
#    container dying is a gap in the TRANSPORT, not a loss of the answer. The panel's
#    poll fails for a couple of seconds while api is recreated and then reads the true
#    state. Serving the status from a container -- even one outside the update set --
#    would not have that property, because any release that changes a service's
#    environment recreates it.
#
# Two files are written, because they have different readers:
#
#    status.json         the full record. Read through the authenticated API.
#    status-public.json  phase, step, times, versions and a sentence. Served directly by
#                        Caddy so it is still readable when the api container is down --
#                        which is exactly when somebody needs it. It deliberately carries
#                        no digests, no git revisions, no log output and nothing from
#                        .env. Release version strings are in it: every version that
#                        exists is already public on GitHub, so the only thing disclosed
#                        is which one this box runs, and that is worth far less than
#                        being able to read "failed at the gate, rolled back" from a
#                        browser when nothing else answers.

# Where state lives.
#
# The order matters and is not arbitrary: docker-compose.prod.yml bind-mounts
# `${SAIVA_STATE_DIR:-/var/lib/saiva/update}` into api and caddy, so if this function
# and that interpolation can disagree, the deploy writes its status somewhere nothing
# mounts and the panel reports "no update has ever run" after every successful update.
# Reading .env second is what keeps the two in step, because .env is what compose reads.
#
# Every `return` below says `return 0` explicitly, and that is not style. A BARE `return`
# takes its status from $?, and bash sets $? to the PENDING EXIT STATUS for the duration
# of an EXIT trap -- so inside a trap handler a bare `return` hands back the status of
# whatever killed the script, however well the function itself did. Measured: with the
# shell exiting 1, `[ -n "$SAIVA_STATE_DIR" ]` returns 0, the `printf` returns 0, and the
# function still returns 1.
#
# That is enough to break the thing the trap exists for. `status_flush` begins
# `dir="$(state_dir)"`, a bare assignment takes the substitution's status, and under
# `set -e` the handler dies there -- after updating the .kv and before re-rendering the
# JSON. The record on disk then still says `running`, which is precisely the "a run that
# stops without saying so leaves yesterday's answer standing" failure both
# _finalise_on_exit functions were written to prevent. It was invisible because
# status_get/status_set reach state_dir through a `printf`, whose own status is 0 whatever
# its arguments did.
state_dir() {
  if [ -n "${SAIVA_STATE_DIR:-}" ]; then printf '%s' "$SAIVA_STATE_DIR"; return 0; fi
  local from_env=""
  [ -f "$(env_path)" ] && from_env="$(env_get SAIVA_STATE_DIR "$(env_path)")"
  if [ -n "$from_env" ]; then printf '%s' "$from_env"; return 0; fi
  if [ -d /var/lib/saiva/update ] || [ -w /var/lib ]; then
    printf '%s' /var/lib/saiva/update; return 0
  fi
  # A developer checkout with no installer run and no write access to /var/lib. Keeps
  # everything inside the working tree; .gitignore covers it so the release path's
  # dirty-tree guard does not trip over it.
  printf '%s' "$ROOT/.saiva-state"
}

# The layout, and why each mode is what it is. An adversarial review proved the first
# version of this wrong end to end: the api container owned $STATE, and owning a
# directory means being able to unlink and rename root-owned entries inside it. From
# there the container could point `deploy.lock` and `run.log` at any host file (both are
# opened by shell redirections, which follow symlinks, so root truncated /etc/shadow and
# appended its log over a crontab), could point `run.log` at a file it wanted to READ
# (status_flush puts its last 40 lines into status.json, which the api serves), and could
# rename `records/` aside and substitute its own -- so a rollback restored an attacker's
# .env and pinned every service to an attacker-chosen image.
#
#   $STATE            root:root 0755   everything root writes; the container only reads
#   $STATE/records    root:root 0700   holds a copy of .env, so not readable by anyone else
#   $STATE/requests   root:<api> 0730  the ONLY thing the container may write into, and
#                                      0730 so it cannot even list what is in there
#
# The container gets $STATE read-only and $STATE/requests read-write, as two mounts. Root
# never opens anything inside requests/: it looks for a regular file with `find -type f`,
# which does not follow symlinks, and unlinks what it finds. That is what makes "the most
# a compromised api container can ask for is an update" true rather than merely intended.
requests_dir() { printf '%s/requests' "$(state_dir)"; }

# The SECOND drop-box, for "Back up now". A separate directory rather than a flag inside
# the request, because the whole argument above rests on root never reading a byte the
# container wrote: the DIRECTORY carries the one bit of information, each has its own
# systemd path unit, and `find -type f` is still the only thing that ever looks inside.
backup_requests_dir() { printf '%s/backup-requests' "$(state_dir)"; }

state_init() {
  local dir; dir="$(state_dir)"
  mkdir -p "$dir" "$dir/records" "$(requests_dir)" "$(backup_requests_dir)"
  chmod 0700 "$dir/records" 2>/dev/null || true
}

# --- the status record ------------------------------------------------------------
#
# Held as tab-separated key/value lines while the run is in progress, and rendered to
# JSON on every flush. Keeping the source of truth as lines rather than as JSON means no
# dependency on jq (not installed on every box) and no string surgery on JSON in bash,
# which is where this kind of script usually acquires its first quoting bug.

# WHICH status is being written. The update path leaves this unset and gets `status.kv` /
# `status.json` / `status-public.json` exactly as before; scripts/backup.sh sets it to
# `backup-status` and gets the same renderer, the same atomic rename and the same
# json_escape -- which is the whole reason this is parameterised rather than copied. That
# escape function is the one piece of this file that took a real failure to get right (a
# compose error carrying an ANSI escape produced invalid JSON in BOTH status files at
# once), and a second hand-rolled copy of it in another script is how that comes back.
_status_kind() { printf '%s' "${SAIVA_STATUS_KIND:-status}"; }

_status_file() { printf '%s/%s.kv' "$(state_dir)" "$(_status_kind)"; }

status_set() { # KEY VALUE
  local key="$1" value="$2" file tmp
  file="$(_status_file)"; tmp="${file}.w.$$"
  # Values are single-line: newlines would break the format and nothing stored here
  # needs them (the log tail is stored as a separate file).
  value="$(printf '%s' "$value" | tr '\n\t' '  ')"
  { [ -f "$file" ] && grep -v "^${key}	" "$file" || true; } > "$tmp"
  printf '%s\t%s\n' "$key" "$value" >> "$tmp"
  mv -f "$tmp" "$file"
}

status_get() { # KEY -> value
  local file; file="$(_status_file)"
  [ -f "$file" ] || return 0
  sed -n "s/^$1	//p" "$file" | tail -n 1
}

json_escape() { # stdin -> a JSON string body (no surrounding quotes)
  # The named escapes, and then EVERY remaining C0 control byte as \u00XX. Leaving them
  # raw produces invalid JSON, and because both status files come out of the same
  # renderer, one stray byte in any value breaks both at once -- the private one the API
  # reads and the public one Caddy serves, which is the one that is supposed to still
  # answer when everything else does not. A compose error message carrying an ANSI escape
  # is enough to do it.
  sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\r//g' \
      -e 's/\x08/\\b/g' -e 's/\x0c/\\f/g' -e 's/\t/\\t/g' \
      | LC_ALL=C awk '
          BEGIN { ORS = ""; for (i = 0; i < 32; i++) ctl[sprintf("%c", i)] = sprintf("\\u%04x", i) }
          {
            if (NR > 1) print "\\n"
            line = ""
            n = length($0)
            for (j = 1; j <= n; j++) { c = substr($0, j, 1); line = line (c in ctl ? ctl[c] : c) }
            print line
          }'
}

_json_kv() { # KEY VALUE -> "key": "value" or "key": null
  local key="$1" value="$2"
  # `return 0`, not `return`: see state_dir's comment. _json_kv is called from
  # _render_status, which status_flush calls from inside an EXIT trap.
  if [ -z "$value" ]; then printf '  "%s": null' "$key"; return 0; fi
  printf '  "%s": "%s"' "$key" "$(printf '%s' "$value" | json_escape)"
}

# Everything the full record carries, in the order it is written. Listed explicitly so a
# field cannot be added to one renderer and forgotten in the other.
SAIVA_STATUS_FIELDS=(
  run_id phase step trigger started_at finished_at message exit_code
  from_version from_build from_revision from_digest_api from_digest_web
  to_version to_build to_revision to_digest_api to_digest_web
  schema_before schema_after db_revision
)
# The subset Caddy serves to an unauthenticated reader.
SAIVA_STATUS_PUBLIC_FIELDS=(
  run_id phase step started_at finished_at message from_version to_version
)
# What a BACKUP run records. Three groups, and the split is the point:
#
#   this run        phase step trigger run_id started_at finished_at message exit_code
#   what it made    dump_name dump_bytes dump_revision verified verify_message rows_checked
#   carried over    last_success_at last_verified_at offsite* backup_count oldest_at kept
#
# The carried-over group survives a failed run on purpose. "The backup failed last night"
# and "there is no backup" are different sentences, and a regime that forgets the second
# one the moment the first happens cannot tell the owner which they are in. backup_begin()
# clears the first two groups and leaves the third alone.
#
# There is deliberately NO public rendering of this. The update status is public because
# reading "failed at the gate, rolled back" from a browser when the api container is down
# is worth more than disclosing which release a box runs. Nothing about backups has that
# property: the age of a household's last backup is reconnaissance, and it is never the
# thing you need while the app is down.
SAIVA_BACKUP_STATUS_FIELDS=(
  run_id phase step trigger started_at finished_at message exit_code
  dump_name dump_bytes dump_revision verified verify_message rows_checked
  last_success_at last_verified_at
  offsite offsite_target offsite_at offsite_message
  backup_count oldest_at kept pruned
)

_render_status() { # FIELD... -> JSON on stdout
  local first=1 field
  printf '{\n'
  printf '  "schema_version": 1,\n'
  for field in "$@"; do
    [ $first -eq 1 ] || printf ',\n'
    first=0
    _json_kv "$field" "$(status_get "$field")"
  done
  printf '\n}\n'
}

status_flush() { # render the JSON file(s) for this kind, atomically
  local dir kind; dir="$(state_dir)"; kind="$(_status_kind)"
  mkdir -p "$dir"
  local -a fields
  case "$kind" in
    backup-status) fields=("${SAIVA_BACKUP_STATUS_FIELDS[@]}") ;;
    *)             fields=("${SAIVA_STATUS_FIELDS[@]}") ;;
  esac
  _render_status "${fields[@]}" > "$dir/.$kind.json.$$"
  # The log tail goes only into the private record, appended as a JSON array so the
  # panel can show it without a second request.
  if [ -f "$dir/$(_log_name)" ]; then
    python3 - "$dir/.$kind.json.$$" "$dir/$(_log_name)" <<'PY' 2>/dev/null || true
import json, sys
status_path, log_path = sys.argv[1], sys.argv[2]
with open(status_path) as fh:
    payload = json.load(fh)
with open(log_path, errors="replace") as fh:
    payload["log_tail"] = [line.rstrip("\n") for line in fh.readlines()[-40:]]
with open(status_path, "w") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY
  fi
  mv -f "$dir/.$kind.json.$$" "$dir/$kind.json"
  # World-readable: Caddy serves the public one, and the api container reads the private
  # one through a read-only mount. Neither contains a secret -- the private one carries
  # the deploy's log tail, which the API only ever returns to an authenticated owner.
  chmod 0644 "$dir/$kind.json" 2>/dev/null || true
  chmod 0640 "$dir/$(_log_name)" 2>/dev/null || true
  # Only the update status has a public face. See SAIVA_BACKUP_STATUS_FIELDS for why the
  # backup status deliberately has none.
  [ "$kind" = "status" ] || return 0
  _render_status "${SAIVA_STATUS_PUBLIC_FIELDS[@]}" > "$dir/.status-public.json.$$"
  mv -f "$dir/.status-public.json.$$" "$dir/status-public.json"
  chmod 0644 "$dir/status-public.json" 2>/dev/null || true
}

status_step() { # STEP [MESSAGE] -- advance and publish in one call
  status_set step "$1"
  [ $# -gt 1 ] && status_set message "$2"
  status_flush
  log "→ $1${2:+: $2}"
}

status_finish() { # PHASE MESSAGE EXIT_CODE
  status_set phase "$1"
  status_set message "$2"
  status_set exit_code "${3:-0}"
  status_set finished_at "$(now)"
  status_flush
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# One log per kind of run. Without this a nightly backup would append to run.log and its
# lines would be quoted back inside the UPDATE's status as "what the updater logged",
# which is a small lie of exactly the kind this whole design exists to remove.
_log_name() {
  case "$(_status_kind)" in
    backup-status) printf 'backup.log' ;;
    *)             printf 'run.log' ;;
  esac
}

log() { # to the terminal and to the run log the status file quotes
  printf '%s\n' "$*"
  printf '%s %s\n' "$(now)" "$*" >> "$(state_dir)/$(_log_name)" 2>/dev/null || true
}

# --- the rollback record ----------------------------------------------------------

record_dir() { printf '%s/records/%s' "$(state_dir)" "$1"; }

record_capture() { # RUN_ID -- write down what is running right now
  local run="$1" dir; dir="$(record_dir "$run")"
  mkdir -p "$dir"
  chmod 0700 "$dir" 2>/dev/null || true
  # A verbatim copy of .env: SECRET_KEY, POSTGRES_PASSWORD, SMTP_PASSWORD and the
  # notifications token. 0600 under a 0700 records/ -- the first version left it 0644
  # inside a directory both containers had mounted, which handed the api process two
  # secrets compose deliberately does not give it.
  if [ -f "$(env_path)" ]; then
    ( umask 077; cp "$(env_path)" "$dir/env" )
    chmod 0600 "$dir/env" 2>/dev/null || true
  fi
  git -C "$ROOT" rev-parse HEAD > "$dir/revision" 2>/dev/null || : > "$dir/revision"
  git -C "$ROOT" describe --tags --exact-match HEAD > "$dir/tag" 2>/dev/null || : > "$dir/tag"
  local svc
  for svc in "${SAIVA_SERVICES[@]}"; do
    compose_digest "$svc" > "$dir/digest-$svc" 2>/dev/null || : > "$dir/digest-$svc"
  done
  status_set from_version "$(env_get SAIVA_VERSION)"
  status_set from_revision "$(cut -c1-12 < "$dir/revision")"
  status_set from_digest_api "$(cat "$dir/digest-api")"
  status_set from_digest_web "$(cat "$dir/digest-web")"
  status_flush
}

record_has() { [ -f "$(record_dir "$1")/revision" ]; }
