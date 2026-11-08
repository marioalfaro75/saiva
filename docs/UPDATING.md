# Updating Saiva

There is one code path that applies a release, and two ways to start it:

| | |
|---|---|
| **Settings → Software updates → Update now** | asks the host to run it |
| **`make update`** on the box | runs it directly |

Both execute `scripts/deploy.sh --apply-release`. They cannot drift apart, because they
are the same script.

---

## What an update actually does

```
record    write down what is running: git revision, .env, the image digest of
          every service
fetch     git fetch --tags from origin
checkout  move the checkout to the newest release tag, then RE-EXECUTE
env       add any setting the new release introduced; never change one you set
pull      compose pull — every service, not a labelled subset
up        compose up -d — this is what applies the new compose file
caddy     reload Caddy if the Caddyfile's contents changed
gate      ask https://<your address>/ and /api/health, from the host, whether
          the site actually answers
```

If the gate fails — or the pull fails, or the stack does not come up — the box is put
back: your `.env`, the previous checkout, and the exact image digests that were running,
pinned so the rollback works offline and ignores where `latest` has moved to. Then it
re-gates, and tells you which of those two things happened.

Three details that are easy to miss and that each cost something real:

- **The re-execution after `checkout` is load-bearing.** `git checkout` replaces the
  script's file while bash is still reading the old one a line at a time. Without the
  hand-over, the logic that runs is the previous release's, applied to the new release's
  tree — including any fix to that logic.
- **The gate probes from outside the containers.** The old wait loop ran inside the api
  container, deliberately, to be independent of hostname and TLS. That is exactly why it
  printed a success tick during the outage that took the site off the LAN: it could not
  see the failure it existed to catch.
- **Caddy is reloaded explicitly.** Compose will never do it for you. A bind mount's
  *declaration* is part of the service config hash; its *contents* are not, and the
  Caddy image's default command has no `--watch`. A release that changes only
  `infra/Caddyfile` is otherwise inert.

---

## The one case that stops and asks

A release's migration succeeds, and then the new code turns out to be unhealthy.

The images can be rolled back. The schema cannot: rolling it back means restoring the
pre-migration dump, which destroys everything written since the update. On a box holding
a household's financial records, that is a decision for a person.

So the updater stops in a state it names exactly — `rolled_back_code_only` — leaves the
box **serving on the previous version**, and the panel says, in these words rather than
"rolled back":

> This box is running v0.16.1 against the v0.17.0 database schema. Roll forward, or
> restore the pre-migration dump.

Running old code against a newer schema works — every migration in this project only adds
tables, indexes and columns, and `backend/tests/test_migrations_expand_only.py` keeps it
that way — but it is a state to leave, not to live in. Roll forward (`make update`) or
restore (`./scripts/restore-backup.sh`).

This used to be far worse. An older image meeting a newer schema raised
`CommandError: Can't locate revision identified by '0012'`, and because the container's
command chains the migration to the server with `&&`, nothing ever started. The box
crash-looped — and because the check that decided whether to take a backup asked whether
the revision *differed* rather than whether it was *behind*, every restart attempt first
wrote a full copy of the database into the volume next to it. The recovery attempt filled
the disk. `backend/app/schema_state.py` has the whole story.

---

## Getting there from a box that runs the old updater

The first upgrade is the one the updater cannot do for you, because the version on the
box does not have it yet. On the LXC, once:

```bash
cd /opt/saiva                      # wherever the checkout is
git fetch --tags
git checkout --detach v0.17.0      # or whichever release carries this
sudo ./scripts/install-updater.sh  # creates the state directory and the host agent
make update                        # applies the release the new way
```

`make update` passes `--remove-orphans`, so the `watchtower` and `docker-socket-proxy`
containers the old design ran are stopped and removed as part of it. Nothing needs to be
done about `WATCHTOWER_TOKEN` in `.env`: it is simply no longer read, and the reconcile
step never deletes a key it does not recognise.

After that, every update is `make update` or the button.

If the checkout has local edits, the release path refuses rather than throwing them away
— commit or stash them first. If you are not sure where the checkout is:
`docker inspect saiva-caddy-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'`
shows the host path of the mounted Caddyfile.

---

## Setting up the in-app button

Once per box, as root:

```bash
sudo ./scripts/install-updater.sh
```

That installs a small agent at `/usr/local/lib/saiva/saiva-update-agent`, a systemd path
unit watching for a request, and the shared directory the api container writes into.

The agent **takes no parameters, and never opens the request at all.** It asks whether a
regular file exists in one directory, deletes whatever it finds there, and resolves the
target release itself from the tags on `origin`.

That second half is not belt-and-braces; it is the half the first version of this got
wrong. Making the request's *contents* inert is not enough if the container owns the
*directory*, because owning a directory means being able to unlink and rename root-owned
entries inside it. A review demonstrated the consequences end to end: the container could
point root's `deploy.lock` and `run.log` at any file on the host (shell redirections
follow symlinks, so root truncated a file and appended its log over another), point
`run.log` at a file it wanted read back through the status API, and rename the rollback
records aside so a rollback restored its own `.env` and pinned every service to an image
of its choosing.

So the boundary is a drop-box, not a shared directory:

```
/var/lib/saiva/update            root:root 0755   status, logs, rollback records
/var/lib/saiva/update/records    root:root 0700   holds a copy of .env
/var/lib/saiva/update/requests   root:<api> 0730  the only thing the app may write
```

The app mounts the outer directory read-only and the drop-box read-write. `0730` means it
cannot even list what is in there. Root looks with `find -type f`, which does not follow
symlinks, and unlinks rather than reads. With that, the claim holds: the most a
compromised api container can ask for is the update the button was going to do anyway.

It is a **copy**, not a symlink into the checkout, for the same reason — otherwise every
release could change what runs as root on your box. The cost is that it can go stale, so
it tells you:

```bash
sudo /usr/local/lib/saiva/saiva-update-agent --self-check
```

Without this installed, everything still works; the button simply reports that in-app
updates are not set up here, and `make update` is unaffected.

---

## The other case it refuses to call a rollback

If the stack was already down when the update started, nothing recorded which image
digests were running — there were no containers to read them from. Restoring `.env` then
is *not* a rollback on the `latest` channel, because `compose pull` has already moved the
local `latest` tag to the release that failed, and `up -d` would bring it straight back.

So that outcome is reported as `rollback_incomplete`, not as a rollback: the configuration
went back, the images did not, and the box may still be on the broken release. The way out
is to pin `SAIVA_VERSION` in `.env` to a known-good tag and update again.

On a version-pinned box this cannot happen — `v0.16.1` still means `v0.16.1`, so restoring
`.env` restores the images by definition.

---

## Where to look when something goes wrong

```bash
./scripts/deploy.sh --status          # what the last run did, in full
journalctl -u saiva-update.service -f # the host agent, live
make logs                             # the API
```

The status is also at `https://<your address>/_saiva/update-status.json`, served by Caddy
straight off the host filesystem. That route exists for one reason: it still answers when
the api container is the thing that is broken. It carries only the redacted record —
phase, step, timestamps, release versions and one sentence; no digests, no git revisions,
no log output, nothing from `.env`.

---

## Rolling back deliberately

```bash
./scripts/deploy.sh --rollback
```

Undoes the last applied release: `.env`, the checkout and every image, by digest. If a
migration ran, you get the same honest half-answer described above.

To go back further, or to recover a box where the update never completed:

```bash
./scripts/restore-backup.sh              # list the pre-migration dumps
./scripts/restore-backup.sh <dump>       # restore one (destroys newer data)
```

---

## What stays manual, on purpose

- **Restoring a database dump.** It destroys everything written since. Not automatic.
- **A new setting with no safe default.** The reconcile step adds settings that have one
  and refuses the update, before anything is pulled, on one that does not.
- **Changing the host agent.** It runs as root; a release must not be able to change it.
- **The first install** of the agent.
- **Pruning old images.** Nothing prunes behind you, because the image a rollback needs
  is dangling by definition. `docker system df`, then `docker image prune` once a release
  has proved itself.

---

## Why it is built this way

The short version: **the deployment used to not be part of the release.**

A release is two container images. A working Saiva is those images *plus*
`docker-compose.prod.yml`, *plus* `infra/Caddyfile` (bind-mounted live from the checkout),
*plus* `.env`, *plus* the deploy script itself — and nothing on the box ever pulled the
repository, so four of those stayed at whatever commit the box happened to be sitting on.

Watchtower, which used to apply updates, made that worse rather than better: it never
reads a compose file at all. It recreates a container from the *old* container's inspect
output, so no compose change, no `.env` value, no port binding and no Caddyfile edit could
reach the box through it. It also treats a container that merely *starts* as a success, so
there was no gate and nothing to roll back to. Of the eight kinds of change a release can
contain, the button delivered three.

And the thing that watched the update was inside the blast radius. The api container
triggered its own replacement, so the process that would report the outcome was the
process being killed: the HTTP response was discarded in a bare `except`, the endpoint
returned the literal string `update_started`, and the panel polled for four minutes and
then rendered **"Update applied."** either way. Three layers, each independently
discarding the failure — which is why no amount of editing the panel could have fixed it.

Now: one script applies everything, gated on the real published address answering from
outside; and the record of what happened is a file on the **host**, so a container being
replaced is a gap in the transport rather than a loss of the answer.
