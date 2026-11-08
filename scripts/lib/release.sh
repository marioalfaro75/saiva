# shellcheck shell=bash
# Applying a release: the one code path, used by `make update`, by `./scripts/deploy.sh
# --apply-release`, and by the host agent behind the in-app button.
#
# There used to be three paths and they delivered different things. Watchtower pulled two
# images and recreated two containers -- it never reads docker-compose.yml, it recreates a
# container from the old container's own inspect output, so no compose change, no .env
# value and no Caddyfile edit could ever reach the box through it. `make pull` applied new
# images against the OLD compose file and the OLD Caddyfile, because nothing on the box
# ever ran `git pull`. `make restart` built from source. Of the eight kinds of change a
# release can contain, the button delivered three.
#
# This delivers all of them, in one order, with one gate and one rollback:
#
#   record    write down what is running: git revision, .env, image digests
#   fetch     git fetch --tags
#   checkout  move the tree to the release, then RE-EXEC (see below)
#   env       reconcile .env against the new .env.example, transactionally
#   pull      compose pull -- images for every service, not just the two labelled ones
#   up        compose up -d, which is what applies the new compose file
#   caddy     reload Caddy when the Caddyfile's CONTENTS changed (compose cannot see that)
#   gate      probe the published address FROM OUTSIDE the containers
#   rollback  on any failure: put .env, the tree and the exact images back, and re-gate
#
# THE RE-EXEC is not an optimisation. `git checkout` replaces this file with a new inode
# while bash still holds the old one open and reads it a line at a time. Without the
# re-exec, the logic that runs is version N-1 applied to version N's tree -- including any
# fix to that logic. The script hands over to the checked-out copy of itself and that copy
# finishes the job.
#
# THE GATE probes the address in SAIVA_SITE_ADDRESS from the host, not from inside the
# api container. The old wait loop ran `compose exec -T api python -c "...urlopen(
# 'http://127.0.0.1:8000/api/health')"`, deliberately, to be "independent of the
# configured hostname / TLS". That is why it printed a tick during the outage: the site
# was unreachable from the LAN and the probe could not see the difference. A gate that
# cannot observe the failure it exists to catch is worse than no gate, because it
# certifies.

# Every service, not just the two a release builds. A release pulls all four, so a bad
# Caddy or Postgres patch is as capable of taking the site down as a bad api image -- and
# rolling back only api and web would leave the box running the combination that failed.
SAIVA_SERVICES=(api web db caddy)
SAIVA_ROLLBACK_PIN_KEYS=(SAIVA_API_IMAGE SAIVA_WEB_IMAGE SAIVA_DB_IMAGE SAIVA_CADDY_IMAGE)

# --- talking to the running stack --------------------------------------------------

compose_container() { $COMPOSE ps -q "$1" 2>/dev/null | head -n 1; }

compose_image_id() { # SERVICE -> the local image id its container was created from
  local cid; cid="$(compose_container "$1")"
  [ -n "$cid" ] || return 1
  docker inspect --format '{{.Image}}' "$cid" 2>/dev/null
}

compose_repo_digest() { # SERVICE -> repo@sha256:... , or empty for a locally built image
  # The container records only a local image id, so ask the image for its repo digest:
  # that is the reference compose can resolve again without guessing a tag.
  local image_id
  image_id="$(compose_image_id "$1")" || return 1
  [ -n "$image_id" ] || return 1
  docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' \
    "$image_id" 2>/dev/null
}

compose_digest() { # SERVICE -> what we will roll back to: a repo digest if there is one
  local digest; digest="$(compose_repo_digest "$1" 2>/dev/null || true)"
  if [ -n "$digest" ]; then printf '%s' "$digest"; return 0; fi
  compose_image_id "$1" 2>/dev/null || return 1
}

compose_image_repo() { # SERVICE -> ghcr.io/owner/saiva-api  (no tag, no digest)
  local cid ref; cid="$(compose_container "$1")"
  [ -n "$cid" ] || return 1
  ref="$(docker inspect --format '{{.Config.Image}}' "$cid" 2>/dev/null)" || return 1
  ref="${ref%@*}"           # drop a digest
  printf '%s' "${ref%:*}"   # drop a tag
}

compose_supports_wait() { $COMPOSE up --help 2>/dev/null | grep -q -- '--wait'; }

# --- where the site is, and whether it answers -------------------------------------

site_url() {
  local raw; raw="$(env_get SAIVA_SITE_ADDRESS)"
  raw="${raw:-localhost}"
  case "$raw" in http://*|https://*) printf '%s' "$raw" ;; *) printf 'https://%s' "$raw" ;; esac
}

_probe_once() { # URL -> 0 if the whole stack answers through the published address
  local url="$1" host body resolve=()
  host="${url#*://}"; host="${host%%/*}"; host="${host%%:*}"
  [ $# -gt 1 ] && resolve=(--resolve "${host}:443:127.0.0.1" --resolve "${host}:80:127.0.0.1")

  # -k: a LAN install uses Caddy's internal CA, which is in no trust store. -f: a 503
  # from a degraded api must fail the gate, not be read as a page.
  body="$(curl -fsS -k --max-time 10 ${resolve+"${resolve[@]}"} "$url/api/health" 2>/dev/null)" || return 1
  printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' || return 1
  # The SPA is a different container behind the same proxy. Without this the gate passes
  # while the app shows nothing.
  curl -fsS -k --max-time 10 ${resolve+"${resolve[@]}"} -o /dev/null "$url/" 2>/dev/null || return 1
  return 0
}

probe_site() { # URL -> 0 if reachable either directly or pinned to loopback
  _probe_once "$1" || _probe_once "$1" with-resolve
}

health_field() { # FIELD -> its value from the running stack's /api/health, or empty
  local url; url="$(site_url)"
  curl -fsS -k --max-time 10 "$url/api/health" 2>/dev/null \
    | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" \
    | head -n 1
}

wait_for_site() { # -> 0 when the site answers, within the configured budget
  local tries="${SAIVA_GATE_TRIES:-90}" url; url="$(site_url)"
  local i
  for i in $(seq 1 "$tries"); do
    if probe_site "$url"; then
      log "    the site answered at $url after $(( i * 2 ))s"
      return 0
    fi
    sleep 2
  done
  return 1
}

# --- choosing what to deploy --------------------------------------------------------

have_git() { git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; }

git_fetch() {
  local tries=0
  until git -C "$ROOT" fetch --tags --prune --quiet origin; do
    tries=$((tries + 1))
    [ "$tries" -ge 3 ] && return 1
    log "    fetch failed, retrying in $((tries * 2))s"
    sleep $((tries * 2))
  done
  return 0
}

newest_release_tag() {
  # Release tags only: v<semver>. Sorted by version, not by date, because a patch cut
  # after a minor must not win on timestamp.
  git -C "$ROOT" tag --list 'v[0-9]*' --sort=-v:refname | head -n 1
}

guard_target() { # TAG -- refuse anything that would take this box backwards
  local target="$1" head_sha target_sha
  head_sha="$(git -C "$ROOT" rev-parse HEAD)"
  target_sha="$(git -C "$ROOT" rev-parse "${target}^{commit}")" || {
    fail "cannot resolve '$target' -- is it a tag that exists on origin?"
  }

  # Tracked changes only. `git checkout` leaves untracked files alone, and a real box
  # always has some: .env, the state directory, a stray log. Counting those as dirty
  # would make the update refuse to run on every box, forever.
  if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
    fail "the checkout has uncommitted changes to files this release also changes,
so updating it would throw them away. Commit or stash them and run the update again.
Nothing has been changed.

$(git -C "$ROOT" status --short --untracked-files=no | sed 's/^/  /')"
  fi

  if [ "$head_sha" = "$target_sha" ]; then return 0; fi

  # "Is the target a descendant of what we are running?" is the right question. A
  # clean-tree check is NOT: a box sitting on a branch ahead of the newest tag has a
  # clean tree, and checking out that tag would silently revert every commit on it.
  if ! git -C "$ROOT" merge-base --is-ancestor "$head_sha" "$target_sha"; then
    if [ "${ALLOW_DOWNGRADE:-0}" = "1" ]; then
      log "    WARNING: $target does not contain what this box is running, and"
      log "             --allow-downgrade was given. Continuing."
      return 0
    fi
    fail "$target does not contain the commit this box is running.

  running : $(git -C "$ROOT" rev-parse --short HEAD) $(git -C "$ROOT" describe --tags --always HEAD 2>/dev/null)
  target  : $(git -C "$ROOT" rev-parse --short "$target_sha") $target

Applying it would take this box BACKWARDS, reverting $(git -C "$ROOT" rev-list --count "$target_sha..$head_sha") commit(s) it is
running. That is almost always a mistake (a checkout left on a branch, or a tag that
was never pushed). Nothing has been changed.

If you really mean to go back to $target, pass --allow-downgrade."
  fi
  return 0
}

# --- the state machine --------------------------------------------------------------

acquire_lock() {
  local lock; lock="$(state_dir)/deploy.lock"
  exec 9>"$lock"
  if ! flock -n 9; then
    # Deliberately NOT `fail`: that writes a terminal status, and the run holding this
    # lock is still going. Reporting it as failed from the process that could not start
    # is the same class of mistake as the panel that reported success it could not know.
    echo "ERROR: another update is already running on this box." >&2
    echo "Watch it with: ./scripts/deploy.sh --status" >&2
    exit 1
  fi
}

fail() { # print, record and exit -- every exit path says what state the box is in
  log ""
  log "ERROR: $*"
  status_finish failed "$(printf '%s' "$1" | head -n 1)" 1
  exit 1
}

new_run_id() {
  local rand
  rand="$( (head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n') 2>/dev/null || printf '%04x' $$ )"
  printf '%s-%s' "$(date -u +%Y%m%dT%H%M%SZ)" "$rand"
}

apply_release() {
  state_init
  acquire_lock

  if [ -z "${RESUME_RUN:-}" ]; then
    _release_prepare
  else
    RUN_ID="$RESUME_RUN"
    log "Resumed in the checked-out tree (run $RUN_ID)."
  fi

  record_capture_if_missing
  _release_apply
}

record_capture_if_missing() {
  record_has "$RUN_ID" || record_capture "$RUN_ID"
}

_release_prepare() {
  RUN_ID="$(new_run_id)"
  : > "$(state_dir)/run.log"
  rm -f "$(_status_file)"
  status_set run_id "$RUN_ID"
  status_set phase running
  status_set trigger "${SAIVA_UPDATE_TRIGGER:-cli}"
  status_set started_at "$(now)"
  status_flush

  status_step record "writing down what is running now"
  record_capture "$RUN_ID"
  status_set schema_before "$(health_field schema)"
  status_set db_revision "$(health_field db_revision)"
  status_flush

  if ! have_git; then
    log "    this deployment is not a git checkout, so the tree cannot be updated."
    log "    images will be applied; compose, Caddyfile and script changes will not."
    return 0
  fi

  status_step fetch "fetching releases from origin"
  git_fetch || fail "could not fetch from origin. Check the box's network, then retry.
Nothing has been changed."

  local target
  target="${TARGET_REF:-$(newest_release_tag)}"
  [ -n "$target" ] || fail "no release tag found on origin. Nothing has been changed."
  status_set to_version "$target"
  status_flush

  guard_target "$target"

  local target_sha; target_sha="$(git -C "$ROOT" rev-parse "${target}^{commit}")"
  if [ "$(git -C "$ROOT" rev-parse HEAD)" = "$target_sha" ]; then
    log "    already on $target; applying it anyway so images and config converge."
    status_set to_revision "$(printf '%s' "$target_sha" | cut -c1-12)"
    status_flush
    return 0
  fi

  status_step checkout "moving the checkout to $target"
  git -C "$ROOT" checkout --detach --quiet "$target_sha" \
    || fail "git checkout of $target failed. Nothing has been applied."
  status_set to_revision "$(printf '%s' "$target_sha" | cut -c1-12)"
  status_flush

  # Hand over to the checked-out copy of this script. See the header: git replaced this
  # file's inode a moment ago and bash is still reading the old bytes.
  log "    handing over to $target's own deploy script"
  exec "$ROOT/scripts/deploy.sh" --apply-release --resume "$RUN_ID"
}

_release_apply() {
  local from_rev to_rev
  from_rev="$(cat "$(record_dir "$RUN_ID")/revision" 2>/dev/null || true)"
  to_rev="$(have_git && git -C "$ROOT" rev-parse HEAD || true)"

  status_step env "reconciling .env with this release"
  env_begin
  if ! env_reconcile; then env_abandon; fail "update stopped before anything was applied."; fi
  env_clear_rollback_pins
  env_commit

  # `set -o pipefail` is on (deploy.sh), so these pipelines report the command's status
  # and not tee's. Without it a failed pull would read as a success.
  status_step pull "pulling images for every service"
  $COMPOSE pull 2>&1 | tee -a "$(state_dir)/run.log" || _rollback "could not pull the new images"

  status_step up "applying the compose file"
  local up_args=(-d --remove-orphans)
  compose_supports_wait && up_args+=(--wait --wait-timeout "${SAIVA_UP_TIMEOUT:-300}")
  $COMPOSE up "${up_args[@]}" 2>&1 | tee -a "$(state_dir)/run.log" \
    || _rollback "the stack did not come up"

  if _caddyfile_changed "$from_rev" "$to_rev"; then
    status_step caddy "reloading Caddy (its configuration changed)"
    _reload_caddy || log "    WARNING: caddy reload failed; the gate below is the real test"
  fi

  status_step gate "checking the published address answers, from outside the containers"
  if ! wait_for_site; then
    _rollback "the site did not answer at $(site_url) after the update"
  fi

  status_set schema_after "$(health_field schema)"
  status_set db_revision "$(health_field db_revision)"
  status_set to_version "$(env_get SAIVA_VERSION)"
  status_set to_build "$(health_field build)"
  local svc
  for svc in api web; do status_set "to_digest_$svc" "$(compose_digest "$svc" || true)"; done


  status_finish applied "Update applied and the site answered at $(site_url)." 0
  log ""
  log "✅ Applied. Running $(status_get to_version) at $(site_url)"
  log "   Undo it with: ./scripts/deploy.sh --rollback"
}

_caddyfile_changed() { # FROM_REV TO_REV
  local from="$1" to="$2"
  have_git || return 1
  [ -n "$from" ] && [ -n "$to" ] || return 0   # cannot tell -> reload, it is cheap
  [ "$from" = "$to" ] && return 1
  ! git -C "$ROOT" diff --quiet "$from" "$to" -- infra/Caddyfile 2>/dev/null
}

_reload_caddy() {
  # Compose will never do this for you: a bind mount's DECLARATION is part of the service
  # config hash, its CONTENTS are not, and the caddy image's default command has no
  # --watch. A release that changes only infra/Caddyfile is otherwise inert.
  $COMPOSE exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile \
    >>"$(state_dir)/run.log" 2>&1
}

env_clear_rollback_pins() {
  # A successful update un-pins the exact images a previous rollback pinned, so the box
  # goes back to following its channel. Leaving them would reproduce the trap the old
  # README's rollback created: a pin nothing ever cleared, a badge dark forever, and an
  # update button that reported success while doing nothing.
  local key
  for key in "${SAIVA_ROLLBACK_PIN_KEYS[@]}"; do
    [ -n "$(env_get "$key")" ] && { env_set "$key" ""; log "    cleared the rollback pin $key"; }
  done
  return 0
}

# --- rollback -----------------------------------------------------------------------

_rollback() { # MESSAGE -- put the box back, then say exactly what state it is in
  local why="$1"
  log ""
  log "FAILED: $why"
  status_set message "$why"
  status_step rollback "putting the box back the way it was"

  local record; record="$(record_dir "$RUN_ID")"
  if ! record_has "$RUN_ID"; then
    status_finish failed "$why -- and there is no rollback record, so nothing was undone." 1
    log "There is no record of what was running, so nothing was rolled back."
    log "Recover by hand: ./scripts/restore-backup.sh (lists dumps) and the README's"
    log "\"Rolling back\" section."
    exit 1
  fi

  # .env first: it is what decides which images compose resolves.
  [ -f "$record/env" ] && cp -p "$record/env" "$(env_path)"

  # Pin the exact images that were running, by digest. Restoring .env alone is not a
  # rollback on the `latest` channel, because `latest` now points at the release being
  # rolled back from.
  local svc digest repo
  for svc in "${SAIVA_SERVICES[@]}"; do
    digest="$(cat "$record/digest-$svc" 2>/dev/null || true)"
    [ -n "$digest" ] || continue
    case "$digest" in
      *@sha256:*) env_set "SAIVA_$(upper "$svc")_IMAGE" "$digest" ;;
      sha256:*)
        # A locally built image has no repo digest. Give it a name compose can resolve.
        repo="$(compose_image_repo "$svc" 2>/dev/null || true)"
        if [ -n "$repo" ] && docker tag "$digest" "$repo:saiva-rollback" >/dev/null 2>&1; then
          env_set "SAIVA_$(upper "$svc")_IMAGE" "$repo:saiva-rollback"
        fi
        ;;
    esac
  done

  if have_git && [ -s "$record/revision" ]; then
    git -C "$ROOT" checkout --detach --quiet "$(cat "$record/revision")" \
      || log "    WARNING: could not restore the previous revision of the checkout"
  fi

  $COMPOSE up -d --remove-orphans >>"$(state_dir)/run.log" 2>&1 || true
  _reload_caddy || true

  local schema_now; schema_now="$(health_field schema)"
  local migrated=0
  [ "$schema_now" = "ahead" ] && migrated=1

  if wait_for_site; then
    if [ "$migrated" = "1" ]; then
      # The dangerous case, and the one no design can automate away: the release's
      # migration succeeded, so the database is at the NEW head while the code is the
      # OLD one. Rolling the images back cannot roll the schema back, and restoring the
      # pre-migration dump destroys everything written since. That call belongs to a
      # person.
      status_finish rolled_back_code_only \
        "Rolled the CODE back and the site is up, but the database was already migrated: \
running $(status_get from_version) against the $(status_get to_version) schema. \
Either roll forward, or restore the pre-migration dump with ./scripts/restore-backup.sh \
(which destroys everything written since the update)." 1
      log ""
      log "⚠  The site is UP, on the previous version -- but the database was already"
      log "   migrated by the release that failed, so the schema was NOT rolled back."
      log "   Pick one:"
      log "     roll forward : ./scripts/deploy.sh --apply-release"
      log "     restore      : ./scripts/restore-backup.sh    (destroys newer data)"
      exit 1
    fi
    status_finish rolled_back \
      "$why. Rolled back to $(status_get from_version) and the site answered again." 1
    log ""
    log "↩  Rolled back to $(status_get from_version). The site is up."
    log "   What went wrong is in: $(state_dir)/run.log"
    exit 1
  fi

  status_finish rollback_failed \
    "$why -- and the site did not answer after rolling back either. The box needs hands." 1
  log ""
  log "✖  Rolled back, but the site still does not answer. Look at:"
  log "     $COMPOSE logs --tail 60 api web caddy db"
  log "     $(state_dir)/run.log"
  exit 1
}

rollback_last() { # --rollback: undo the most recent applied run, on purpose
  state_init
  acquire_lock
  RUN_ID="$(status_get run_id)"
  [ -n "$RUN_ID" ] && record_has "$RUN_ID" \
    || fail "no update record to roll back to. ./scripts/restore-backup.sh lists database dumps."
  _rollback "rollback requested by the operator"
}

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }
