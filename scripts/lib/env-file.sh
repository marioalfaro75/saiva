# shellcheck shell=bash
# Reading and writing .env, transactionally, with one rule written down.
#
#   PERSISTED STATE IS DERIVED FROM PERSISTED STATE, NEVER FROM THE ABSENCE OF A FLAG.
#
# That rule exists because breaking it took the production site off the LAN. deploy.sh
# used to end its site-address block with:
#
#     else
#       set_env_var SAIVA_BIND_ADDR "127.0.0.1"
#     fi
#
# so a bare `make pull` -- the command the README gives for upgrades, run the way the
# README gives it -- rewrote the binding to loopback and un-published the app. The flag's
# absence meant "the owner did not say", and it was read as "the owner said localhost".
#
# The same shape is one line away at every `set_env_var` call site, so the rule is here
# rather than in a comment next to one of them, and scripts/tests/ has a test that runs
# the deploy twice with no flags and asserts .env comes back byte-identical.
#
# Everything is staged and committed atomically. The old code mutated .env in place,
# before `compose pull`, under `set -euo pipefail` -- so a failed pull left .env changed
# and the stack untouched, a landmine that fired on the next unrelated `up -d`.

# --- plumbing ---------------------------------------------------------------------

# `sed -i` on a bind-mounted or overlay file can change its inode; .env may be watched by
# nothing today, but writing through a temp file and renaming is the only way to be sure
# a reader never sees a half-written file.
_env_stage=""

env_path() { printf '%s' "${SAIVA_ENV_FILE:-$ROOT/.env}"; }

env_begin() { # snapshot .env and start staging changes
  local real; real="$(env_path)"
  _env_stage="${real}.staging.$$"
  if [ -f "$real" ]; then cp -p "$real" "$_env_stage"; else : > "$_env_stage"; fi
}

env_commit() { # atomically replace .env with the staged copy
  [ -n "$_env_stage" ] || return 0
  mv -f "$_env_stage" "$(env_path)"
  _env_stage=""
}

env_abandon() { # throw the staged changes away; .env is untouched
  [ -n "$_env_stage" ] && rm -f "$_env_stage"
  _env_stage=""
}

_env_target() { # the file writes go to: the staging copy when staging, else .env itself
  if [ -n "$_env_stage" ]; then printf '%s' "$_env_stage"; else env_path; fi
}

# --- reading ----------------------------------------------------------------------

env_get() { # KEY [FILE] -> value, or empty if absent
  local key="$1" file="${2:-$(_env_target)}"
  [ -f "$file" ] || return 0
  # The last assignment wins, which is what compose does with a duplicated key.
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

env_has() { # KEY [FILE] -> 0 if the key is present at all (even empty)
  local key="$1" file="${2:-$(_env_target)}"
  [ -f "$file" ] && grep -q "^${key}=" "$file"
}

# --- writing ----------------------------------------------------------------------

env_set() { # KEY VALUE -- update in place, or append
  local key="$1" value="$2" file; file="$(_env_target)"
  local tmp="${file}.w.$$"
  if [ -f "$file" ] && grep -q "^${key}=" "$file"; then
    # awk, not sed: a value containing & or | or a newline-free but slash-heavy path is
    # ordinary here (SAIVA_SITE_ADDRESS is a URL), and sed's replacement text treats
    # several of those as metacharacters. awk compares and substitutes literally.
    awk -v k="$key" -v v="$value" '
      index($0, k "=") == 1 { if (!done) { print k "=" v; done = 1 } ; next }
      { print }
      END { if (!done) print k "=" v }
    ' "$file" > "$tmp"
  else
    [ -f "$file" ] && cat "$file" > "$tmp" || : > "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv -f "$tmp" "$file"
}

env_set_if_missing() { # KEY VALUE -- only when the key is absent or empty
  local key="$1" value="$2"
  if [ -z "$(env_get "$key")" ]; then env_set "$key" "$value"; return 0; fi
  return 1
}

# --- reconcile against .env.example ------------------------------------------------

# Keys a new release may introduce that CANNOT be given a safe default: the deploy must
# stop and ask rather than invent one. Listed here rather than inferred, because the
# failure mode of guessing wrong is silent.
#
# Nothing is in it today. docker-compose.prod.yml already uses `${KEY:?}` on SECRET_KEY,
# so compose fails closed on that one; the improvement this brings is failing closed with
# a sentence the owner can act on, before anything has been pulled or recreated.
SAIVA_ENV_KEYS_NEEDING_A_DECISION=()

# Keys that must exist and must be generated, not copied from the example.
SAIVA_ENV_GENERATED_KEYS=(SECRET_KEY POSTGRES_PASSWORD)

env_reconcile() { # add keys a newer .env.example introduced; never change an existing value
  local example="${SAIVA_ENV_EXAMPLE:-$ROOT/.env.example}" added=() blocked=()
  [ -f "$example" ] || return 0

  local line key value
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) : ;; *) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    # Strip an inline trailing comment the example uses for prose (SMTP_TLS=true # ...),
    # and then the whitespace it leaves behind. That second step is not cosmetic:
    # pydantic parses SMTP_TLS as a bool and rejects "true  " outright, so a stray
    # trailing space here is an api container that will not start -- found by running
    # this against a real .env and reading what it wrote.
    value="${value%%[[:space:]]#*}"
    value="${value%"${value##*[![:space:]]}"}"
    env_has "$key" && continue

    if _env_key_needs_a_decision "$key"; then blocked+=("$key"); continue; fi
    if _env_key_is_generated "$key"; then
      env_set "$key" "$(gen_secret)"
      added+=("$key (generated)")
    else
      env_set "$key" "$value"
      added+=("$key=$value")
    fi
  done < "$example"

  if [ ${#blocked[@]} -gt 0 ]; then
    printf 'ERROR: this release needs settings that have no safe default:\n' >&2
    printf '  %s\n' "${blocked[@]}" >&2
    printf 'Add them to .env and run the update again. Nothing has been changed.\n' >&2
    return 1
  fi
  if [ ${#added[@]} -gt 0 ]; then
    printf '  → .env gained %d setting(s) this release introduced:\n' "${#added[@]}"
    printf '      %s\n' "${added[@]}"
  fi
  return 0
}

_env_key_needs_a_decision() {
  local key="$1" k
  for k in ${SAIVA_ENV_KEYS_NEEDING_A_DECISION+"${SAIVA_ENV_KEYS_NEEDING_A_DECISION[@]}"}; do
    [ "$k" = "$key" ] && return 0
  done
  return 1
}

_env_key_is_generated() {
  local key="$1" k
  for k in "${SAIVA_ENV_GENERATED_KEYS[@]}"; do [ "$k" = "$key" ] && return 0; done
  return 1
}
