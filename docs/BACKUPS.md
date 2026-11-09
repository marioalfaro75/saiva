# Backups

Saiva takes a backup of the household database every night, **proves it can be restored**,
copies it off this box, and tells you on the Settings page how old the newest proven one
is. If any of that stops working, the page says so in words rather than going quiet.

There are four claims the regime is willing to make, and it will not make a fifth:

1. a dump was taken at time T;
2. it was **restored into a throwaway database** and came back whole;
3. a copy of it reached a different filesystem — and it says *how far away*, because
   "another directory on the same disk" is not a backup;
4. the newest **verified** dump is N hours old. Here is the number.

Anything it cannot show, it reports as unknown. That is deliberate and it is the whole
point: a backup that is broken and reported as fine is worse than no backup, because you
stop checking.

---

## What you need to do on your LXC host

Four steps. Run them in the checkout, as root.

```bash
cd /opt/saiva                        # wherever your checkout is
git fetch --tags && make update      # get the release that carries this

sudo ./scripts/install-updater.sh    # installs the timer, the drop-boxes and the agent
sudo nano /etc/saiva/agent.conf      # set SAIVA_BACKUP_COPY_TO — see the next section
sudo ./scripts/backup.sh             # take the first one NOW, don't find out at 02:37
sudo ./scripts/backup.sh --self-check
```

The last two are the ones people skip. The first proves the whole path works while you are
sitting there; the second is what tells you the regime is installed, where the copies go,
and whether anything is wrong.

Then open **Settings → Backups** in the app and check it agrees.

If you are not sure where the checkout is:

```bash
docker inspect saiva-caddy-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

shows the host path of the mounted Caddyfile.

`install-updater.sh` is idempotent — run it again any time. You need to run it again when
`scripts/saiva-update-agent` or the unit files change; the agent's `--self-check` says when
that is.

### If your LXC has no systemd

Unlikely on the Debian/Ubuntu container the README specifies, but the installer notices and
prints a cron line instead:

```
37 2 * * *  /opt/saiva/scripts/backup.sh --trigger timer
```

Add it to root's crontab. Without it **nothing takes a nightly backup**, and that is the
one failure a backup regime must never have silently — so the Settings page will show the
age climbing and say so.

---

## Choosing where the copy goes

This is the decision that matters, and the page will hold you to it. Edit
`/etc/saiva/agent.conf`:

```bash
SAIVA_BACKUP_COPY_TO=/mnt/backups/saiva
```

The backup script measures how far away that is and the page reports exactly that, with no
rounding up:

| what it finds | the page says | good enough? |
|---|---|---|
| the same filesystem as the database | **same disk** | no. One disk failure loses both. |
| a different local filesystem | **same machine** | survives a deleted volume or a bad migration. Not the machine dying. |
| NFS, CIFS, sshfs, rclone-mount | **off this machine** | yes |
| `SAIVA_BACKUP_COPY_CMD` succeeded | **off this machine** | yes |
| it could not tell | **unknown** | treat as the row above whichever you believe |

On Proxmox, in rough order of how much they protect you:

**A mount point from the PVE host.** On the host:

```bash
pct set <ctid> -mp0 /mnt/backup-disk/saiva,mp=/mnt/backups/saiva
```

The page will call this **same machine**, and it is right to: from inside the container
there is no way to tell whether `/mnt/backup-disk` is a different physical disk or another
directory on the one the container lives on. **That part is yours to check**, on the PVE
host, with `pvesm status` and `lsblk`. If it is a different disk, this is a real
improvement and the page is simply being careful.

**An NFS or CIFS share from a NAS.** Mount it in the container (or pass it through as a
mount point) and point `SAIVA_BACKUP_COPY_TO` at it. The page will say **off this machine**,
because the filesystem type proves it. This is the best option on a home network.

**Object storage, with a command.** For a copy that survives the house:

```bash
SAIVA_BACKUP_COPY_CMD='rclone copyto "$1" "remote:saiva-backups/$2"'
```

**Those single quotes are not optional.** The update agent sources this file, so an
unquoted value is parsed as shell: `rclone` becomes the value, `copyto` becomes a command
to run, and under `set -e` the agent dies — taking both in-app buttons with it. `backup.sh`
reads the file rather than sourcing it, so an unquoted line would work there and fail only
on the button, which is exactly how such a line survives a test.

`$1` is the dump's path on the host, `$2` is its filename. Anything works —
`rclone`, `restic`, `rsync -e ssh`, `aws s3 cp`. It runs as root, after the dump has been
verified, and a non-zero exit is reported as a failed copy with the verified local dump
still intact.

### Why those two settings live in `/etc/saiva/agent.conf` and not in `.env`

Because the backup script runs **as root**. A destination path taken from `.env` would let
anyone who can write the checkout have root create files wherever they chose; a *command*
taken from `.env` would simply be a way to become root. `agent.conf` is root-owned, and the
script refuses to act on either setting unless the file is owned by root and not writable
by group or other.

`backup.sh` **reads the keys it wants** rather than sourcing the file, so reading the
configuration cannot itself run anything. The update agent, which shares the file, **does**
source it — so every line in it has to be a valid shell assignment, and a value containing
spaces has to be quoted. That is why the `SAIVA_BACKUP_COPY_CMD` example above is in single
quotes: unquoted, `rclone` would be taken as the value and `copyto` as a command to run,
and the agent would die on it, taking both in-app buttons with it.

Database settings still come from `.env`, because that is what docker-compose reads and the
backup must dump the same database the stack runs.

### Proxmox Backup Server is not a substitute

If you already back the container up with PBS, keep doing it — but it is not this. PBS
snapshots the container's filesystem, which includes the Postgres data directory **while
Postgres is running**. That is a crash-consistent copy: it will usually recover, and
"usually" is doing a lot of work in that sentence. A `pg_dump` that has been replayed into
a live database and counted back is a different kind of evidence. Run both.

---

## Checking it is working

```bash
make backup-status                       # the last run, then the health check
make backup                              # take one now
systemctl list-timers saiva-backup.timer # when the next one runs
journalctl -u saiva-backup.service -f    # watch one happen
```

The two `make` targets are shorthand for the script, which takes a few more options:

```bash
sudo ./scripts/backup.sh --status        # the last run, as JSON
sudo ./scripts/backup.sh --self-check    # is the regime installed, and healthy?
sudo ./scripts/backup.sh --verify-only   # re-verify the newest dump; take nothing
sudo ./scripts/backup.sh --list          # every dump in the volume, newest first
```

**Settings → Backups** in the app shows the same thing and is the one you will actually
look at. It reads a file the host writes, so it keeps working when the app itself is
unhealthy, and it never fills a gap in its knowledge with good news:

- the headline is the age of the newest **verified** backup. If nothing has been verified,
  it says that, and it does not quietly fall back to "a dump was taken";
- `a dump was taken but it could not be restored` is its own state, in red, with the reason;
- a timestamp in the future is called out rather than rendered as "0 hours ago" — an
  unprivileged LXC takes its clock from the PVE host, and a restored snapshot really does
  produce this. Fix NTP on the host; the container cannot set its own clock.

### Take one on demand

Settings → Backups → **Back up now**, or on the host:

```bash
make backup          # or: sudo ./scripts/backup.sh
```

Worth doing before anything you are unsure about — an import, a bulk edit, an upgrade you
have a feeling about.

---

## What it actually does, in order

```
lock        take the deploy lock, so a backup and a release can never overlap
prune       remove nightly dumps already past the retention you configured
room        refuse if there is not enough free space — see below
dump        pg_dump, plain SQL, gzipped, straight into the db_backups volume
verify      restore it into a throwaway database and compare it, row for row
offsite     copy it out and measure how far away that got
prune       again, now that this run has added one
record      write it all down where the app can read it
```

### Why "verified" means more than "pg_dump exited 0"

Measured, on PostgreSQL 16.15, against five dumps of the same 504-row database:

| the dump | `gunzip` | pg_dump's "complete" marker | `psql --single-transaction` | row counts |
|---|---|---|---|---|
| a good one | 0 | yes | 0 | match |
| gzip truncated | **1** | — | — | — |
| pg_dump died before any COPY | 0 | **no** | **0** | **mismatch** |
| pg_dump died mid-COPY | 0 | **no** | **0** | **mismatch** |
| `--schema-only` by mistake | 0 | yes | 0 | **mismatch** |

The two middle rows are the finding the whole design turns on. **`psql
--single-transaction -v ON_ERROR_STOP=1` exits 0 on a dump truncated inside a COPY block**
— when its input ends without the `\.` terminator it finishes the COPY with whatever rows
it got and reports success. "It restored without error" is therefore *not* evidence that a
dump is complete, and a regime that checked only that would hand you a green tick over a
dump missing 96% of your ledger.

So four checks run and none of them is spare: gunzip's exit status (the only one that sees
a truncated archive), pg_dump's completion marker (the only cheap one that sees a dump that
died partway), a row-for-row comparison between what the dump declares and what came back
(the only one that sees `--schema-only`, and it catches both truncations a second time),
one aggregate comparison against the live database,
which catches a dump of the *right* database that came back with no rows in it; and a
structural check that the restored copy really contains a `households` table and a schema
revision.

That last one is what answers *is this a dump of Saiva at all*, and it is there because the
aggregate cannot be: the dump comes from `POSTGRES_DB` and the aggregate counts
`POSTGRES_DB`, so the two always agree. Point it at an empty database and every other check
passes on a 396-byte dump of nothing — measured — while the retention retires the real
dumps behind it. That was a real defect in the first version of this, found by review.

### Disk space

The verify is the expensive part: replaying the dump into a throwaway database writes
roughly another copy of the data, and because `db_data` and `db_backups` are both local
Docker volumes they are on the **same filesystem**. So the script insists on about
1¼ × the database size free before it starts, and refuses with the numbers if that is not
there.

It refuses rather than deleting backups to make room, because trading the backups you have
for one that has not been verified yet is not a trade worth making. It matters more than a
failed backup would: filling that filesystem stops Postgres, not just the backup. This box
has been there before — see the crash-loop in [UPDATING.md](UPDATING.md) that wrote a full
copy of the database on every restart attempt.

If the estimate is too cautious for your setup, set `SAIVA_BACKUP_MIN_FREE_KB` in
`/etc/saiva/agent.conf` — the installer leaves it there commented out.

### What it will never delete

The prune removes **old `-nightly` dumps and nothing else.** The `-premigrate` dumps the
app writes before a migration, and the `-prerestore` dumps a restore writes before
replacing a database, are left alone however old they get: this script may remove what it
created and may not remove what other paths did. Their count is reported instead, so a
volume filling up is something you can see rather than something that gets tidied away.

`SAIVA_BACKUP_KEEP_NIGHTLY` in `agent.conf` sets the retention (default 14). It is never
allowed to be zero.

---

## Restoring

From a dump in the volume — the normal case:

```bash
./scripts/restore-backup.sh                                  # list them, newest first
./scripts/restore-backup.sh saiva-20261109-023700-nightly.sql.gz
```

It takes a `-prerestore` dump of the current database first, so the restore itself can be
undone. [UPDATING.md](UPDATING.md) covers the rest.

From the **off-box copy** — put it back in the volume first, then restore as above:

```bash
docker run --rm -i -v saiva_db_backups:/b postgres:16-alpine \
  sh -c 'cat > /b/saiva-20261109-023700-nightly.sql.gz' < /mnt/backups/saiva/saiva-20261109-023700-nightly.sql.gz
./scripts/restore-backup.sh saiva-20261109-023700-nightly.sql.gz
```

Nothing automates that, on purpose. A restore destroys everything written since the dump,
and on a box holding a household's financial records that is a decision for a person.

### Rehearse it

Once, on a quiet evening, restore last night's dump and look at the app. The regime proves
every dump is *restorable*; only you can prove you know how to restore one. The rehearsal
is cheap — `-prerestore` means you can put it straight back — and it is the difference
between having backups and being able to recover.

---

## When something is wrong

**"Nightly backups aren't set up on this box."** `sudo ./scripts/install-updater.sh`, then
reload the page.

**The age is climbing and nothing says why.** `journalctl -u saiva-backup.service --since
'3 days ago'`. If the unit is not there, the installer has not been run since the release
that added it.

**`skipped`, repeatedly.** A nightly run records `skipped` rather than failing when the
stack is deliberately down — reporting failure every night for a box somebody turned off is
how a regime teaches its owner to ignore it. Repeated skips mean the stack is down:
`make up`.

**The record has not changed for a day, and nothing says why.** A run that could not take
the deploy lock — because a release was being applied — writes *nothing at all*, on
purpose: the record belongs to the run that holds the lock, and every write to it is a
read-modify-write of one shared file, so a second run saying "skipped" over a live one
corrupts the live one. The journal has it:
`journalctl -u saiva-backup.service --since '2 days ago'`. And the age on the page is the
number that notices.

**`a dump was taken but it could not be restored`.** The most important message here, and
the dump is kept for inspection. The reason is on the page and in `backup.sh --status`. If
it names `invalid command \restrict`, the Postgres client image is older than the one that
wrote the dump: `SAIVA_PG_IMAGE` wants a newer `postgres:16-alpine`.

**`the database server is PostgreSQL 17 and the client this script uses is 16`.** Something
moved `SAIVA_DB_IMAGE` to a new major. `pg_dump` refuses to dump from a server newer than
itself, so point `SAIVA_PG_IMAGE` at a matching image — and note that the data directory
does not move majors by itself, so this is a change somebody made.

**The copy says `same disk` and you expected better.** The destination is on the filesystem
the database is on. On Proxmox, check the mount point really is backed by other storage.

---

## Why it is built this way

**It runs on the host, not in a container.** The thing that produces the safety net must
not be the thing a bad release can break — and a backup is most wanted on the night the app
will not start. `scripts/backup.sh` runs under a systemd timer, reaches Postgres through a
throwaway container built from the database's own image, and writes its record to a file on
the host. The app only ever *reads* that file.

**The nightly run does not go through the update agent.** A stale copy of the agent cannot
stop backups happening. The timer runs the script directly.

**"Back up now" is a second drop-box, not a flag.** The app writes a file into
`/var/lib/saiva/update/backup-requests`, and root notices that *a file is there* without
ever opening it — `find -type f`, which does not follow symlinks, then unlink. Which of two
directories the file landed in is the single bit of information a network-facing container
gets to express. Everything else — which release, which destination, which command — is
resolved on the host. [UPDATING.md](UPDATING.md) has the full argument, including what went
wrong with the first version of it.

**The age is surfaced because the age is the thing that fails.** Dumps rarely break. Regimes
stop running, quietly, and nobody finds out until the night it matters. A number on a page
that climbs when nothing is happening is the whole mechanism.
