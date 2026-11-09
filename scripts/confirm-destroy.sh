#!/usr/bin/env bash
# Typed confirmation for `make destroy`. Exits 0 only once the operator has confirmed;
# any non-zero exit means do not proceed.
#
# This lives outside the Makefile because the Makefile is the wrong place to get it
# right: a heredoc, a TTY test and an exact string compare all need `$$` doubling and
# `;\` continuations inside a recipe, which is unreadable and impossible to test without
# running the destructive command sitting behind it. Here it can be exercised on its
# own, with stdin pointed anywhere.
set -euo pipefail

# The word the operator has to type. Deliberately not y/n: `make destroy` is two letters
# from `make down` on the keyboard and one arrow-up from it in shell history, and a y/n
# prompt gets answered reflexively.
WORD="destroy-saiva-data"

describe() {
  # Volume names carry the compose project prefix, which both compose files set to
  # `saiva` via `name: saiva`. That shared project name is also why there is one destroy
  # target rather than one per compose file: either file's `down -v` removes exactly
  # these objects.
  cat >&2 <<'EOF'

  This permanently deletes the following Docker volumes:

    saiva_db_data      the household's ledger — accounts, transactions, budgets,
                       categories, logins, audit log. Everything.
    saiva_db_backups   EVERY dump: the nightly ones scripts/backup.sh takes and verifies,
                       the pre-migration ones the api writes, and the pre-restore ones.
                       They are what a restore reads FROM, and they live inside this
                       volume rather than on the host filesystem, so they go with it.
                       An OFF-BOX copy, if one is configured, survives this. That is the
                       only thing that will. Check it is current before you type the word:
                       ./scripts/backup.sh --status names the destination and says how
                       recent the last verified copy is.
    saiva_caddy_data   Caddy's internal CA and the certificates it issued. Devices that
                       were taught to trust the old root CA will warn again.
    saiva_caddy_config

  Docker keeps no copy of a removed volume. To keep the dumps, copy them out first — this
  works whether or not the stack is running:

    docker run --rm -v saiva_db_backups:/b -v "$PWD":/out alpine \
      tar czf /out/saiva-backups.tar.gz -C /b .

EOF
}

refuse() {
  printf '  Aborted. Nothing was deleted.\n\n' >&2
  exit 1
}

# Checked before the TTY test, so a release script that genuinely means it does not also
# need a terminal. It has to carry the word rather than a 1/yes/true so that it cannot be
# set once by habit, alongside the other FORCE-style flags, and left in a shell profile.
if [ -n "${SAIVA_CONFIRM_DESTROY:-}" ]; then
  if [ "$SAIVA_CONFIRM_DESTROY" = "$WORD" ]; then
    printf '  SAIVA_CONFIRM_DESTROY=%s — proceeding without a prompt.\n' "$WORD" >&2
    exit 0
  fi
  describe
  printf '  SAIVA_CONFIRM_DESTROY is set but does not say %s, so it is not a confirmation.\n' \
    "$WORD" >&2
  refuse
fi

describe

# No terminal means nobody is reading the warning above, so there is nobody to confirm.
# Refusing is the only safe reading: `make destroy </dev/null`, or a CI job that inherited
# the target from a copied Makefile, would otherwise sail straight through on an EOF.
if [ ! -t 0 ]; then
  printf '  Refusing: stdin is not a terminal, so there is nobody here to confirm.\n' >&2
  printf '  To mean it non-interactively, set SAIVA_CONFIRM_DESTROY=%s\n' "$WORD" >&2
  refuse
fi

printf '  Type %s to proceed (anything else aborts): ' "$WORD" >&2
if ! IFS= read -r reply; then
  printf '\n  No answer (end of input).\n' >&2
  refuse
fi

# Exact compare, no trimming and no case folding: the point of a typed word is that it
# cannot be arrived at by accident.
if [ "$reply" != "$WORD" ]; then
  refuse
fi

printf '  Confirmed.\n' >&2
exit 0
