# Saiva — Household Finance & Insights App

A self‑hosted web app that helps an Australian family understand their income and
spending, track bills and forecast cashflow, get actionable insights and savings
recommendations, benchmark against similar households (ABS data), and — optionally —
chat with an AI advisor (cloud BYO‑key or a local model) grounded in their own data.
Runs in a container, HTTPS‑only.

> **Status:** MVP through **Phase 3 complete** — the full feature set below is
> implemented and shipping. See [Features](#features) for the list, and the
> [PRD](docs/PRD.md) for the roadmap and design rationale.

## Features

**Accounts & import**
- File import — **CSV** and **OFX/QFX**. Saiva shows what is in each column, with the
  first few values from your own file, and asks you to confirm — so an unfamiliar header
  is no obstacle and nothing is left out without saying so. What it works out is
  remembered per shape of file, and still shown every time, so a bank adding a column is
  noticed rather than absorbed.
- **Statements covering several accounts** are handled without being asked to pick one:
  a CSV column of account numbers, or an OFX download carrying several statements, is
  recognised and each account matched to yours — or created inline. Each one is shown
  with its rows, date range and closing balance, because a bare account number tells
  you nothing.
- **Australian dates by default**, and it says so against a real value from your file:
  `01/07/2025` parses either way round, so the wrong assumption would file a year of
  transactions into the wrong months without a single row failing.
- **Duplicate‑proof imports** — overlapping date ranges never double up, and neither does
  the same month downloaded as both OFX and CSV. Matching uses the bank's own transaction
  id where present, then exact matches counted by occurrence, then near matches (re‑dated
  or re‑worded rows), then the same merchant for the same amount on the same day — which
  is what catches a bank wording one purchase two ways in two formats. The last two ask
  you to confirm rather than deciding alone, and genuine repeat purchases are kept, not
  silently dropped. A row that wants an answer shows you the transaction it matched —
  amount, wording, category, and whether it came from a file or your own typing — plus
  what else that account already holds nearby, since the count is what tells a re-dated
  duplicate from a weekly repeat.
- Rule + **ML categorisation** (confidence‑thresholded), an assisted per‑row categorise
  popover with scopes and "make a rule", a **rules** manager, and per‑transaction locking.
- Automatic **transfer detection** between your own accounts.

**Understand your money**
- **Global period picker** — view any **financial year**, quarter, month or relative range;
  every relevant page follows it, derived from your own FY start date.
- **Overview** dashboard — income, spending and balances at a glance.
- **Transactions** — search across every column, sort and filter by any column, and
  bulk categorise / review. Sorting and filtering run over your whole history, not
  just the page on screen, and the view can be shared as a link.
- **Sortable, filterable tables everywhere** — every table sorts by any column and has
  per-column filters, and remembers how you left it.
- **Insights** — a rule‑based feed of savings opportunities and notable changes.
- **ABS benchmarks** — compare your spending against similar Australian households.

**Plan ahead**
- **Budgets** — per‑category tracking (flexible by default).
- **Bills & recurring** — automatic detection of subscriptions, bills and salary, an
  upcoming‑bills view, and committed‑monthly totals.
- **Cashflow forecast** — projects your balance forward from recurring income and a
  per‑category run‑rate, flags the lowest projected point, and runs simple what‑if scenarios.
- **Net worth** — a manual assets & liabilities balance sheet.
- **Savings goals** — targets with suggested contributions.

**Stay informed**
- **Alerts** — an in‑app feed (over‑budget categories, unusual spend, upcoming bills, large
  transactions, low projected balance) with opt‑in **email** and weekly/monthly digests.
- **AI advisor (BYO key)** — ask questions about your own data via Anthropic (Claude),
  Google Gemini, any OpenAI‑compatible endpoint, or a local **Ollama**. It can search your
  transactions and summarise spending by category, merchant or period, and follows the
  period picker. **Privacy modes** control what it may see: *Aggregates only* withholds
  individual transactions and their descriptions, and the advisor says so when a question
  needs them.
- **Financial‑year PDF report** — a one‑click accountant summary for any financial year.

**Self‑host & operate**
- **HTTPS‑only** via Caddy — internal CA for localhost/LAN, or Let's Encrypt for a domain.
- **Prebuilt GHCR images** (`edge` / `latest` / pinned channels), **pre‑migration backups**,
  and **in‑app updates** (the owner clicks *Update now*; the app never touches the Docker socket).
- Multi‑user household with **role‑based access**, an **audit log**, and a full **JSON export**.

## Run it in a container

**Prerequisites:** Docker Engine + the Docker Compose plugin, and ports **80** and
**443** free on the host.

**1. Start it — one command.** Generates `.env` (with a secure random `SECRET_KEY`
and DB password), builds the images, starts the stack, and waits until it's healthy:

```bash
make deploy            # equivalently: ./scripts/deploy.sh
make deploy SEED=1     # also load demo data (prints a generated demo password)
```

<details>
<summary>Prefer to run Compose by hand?</summary>

```bash
cp .env.example .env
# edit .env: set SECRET_KEY (e.g. `openssl rand -hex 32`) and POSTGRES_PASSWORD
docker compose up -d --build
```
</details>

**2. Open it.** Browse to **https://localhost**. Caddy serves HTTPS using its own
internal CA; because that CA lives inside the container your browser won't recognise
it, so accept the one‑time certificate warning (**Advanced → Proceed** — it's your own
machine). On first visit you create your household and owner login; for sample data use
**Settings → Load demo data** (or `make deploy SEED=1`).

**3. Verify & manage.**

```bash
make ps                                # all services up; db shows "healthy"
curl -k https://localhost/api/health   # -> {"status":"ok", "schema":"current", ...}
make logs                              # follow API logs
make down                              # stop the stack, keep the data
```

`make destroy` is the one to be careful with: it deletes the household's records **and
every pre-migration backup dump**, which live in the same Docker volume set. It asks you
to type a word first, and refuses outright when there is no terminal to ask.

Run `make help` for all targets.

**Reach it from other devices (LAN over HTTPS).** By default Saiva listens on
`https://localhost` (this machine only). To expose it to your home network over HTTPS:

```bash
make deploy LAN=1                      # auto-detects this host's LAN IP
make deploy SITE=https://192.168.1.50  # …or pin a specific address
# (equivalently: ./scripts/deploy.sh --lan   or   --site https://192.168.1.50)
```

That sets `SAIVA_SITE_ADDRESS` (and `SAIVA_SITE_HOST`, used as the TLS SNI default so
serving by raw IP works) and Caddy issues a cert from its **internal CA**. Open
`https://<host-ip>` from any device — you'll get a one‑time "not private" warning
(**Advanced → Proceed**) because the cert isn't from a public authority.

**Custom hostname / trusted certificate.** For a public domain, set
`SAIVA_SITE_ADDRESS=finance.example.com` and `SAIVA_TLS=you@example.com` in `.env` — Caddy
then provisions a trusted Let's Encrypt cert (no warnings, on any device). To clear the
warning for a LAN/internal setup instead, trust Caddy's root CA:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
# then import caddy-root.crt into your OS / browser trust store
```

### Deploy from prebuilt images (GHCR)

Instead of building locally, run the images published to GitHub Container Registry —
faster, reproducible, and easy on a low‑powered host (it doesn't compile the frontend):

```bash
make pull          # first install: pull ghcr.io/marioalfaro75/saiva-{api,web} and start
make pull LAN=1    # ... and publish it on your LAN over HTTPS
```

After the first publish, set those GHCR packages to **public** (GitHub → your profile →
Packages) if you want to pull without `docker login`; otherwise `docker login ghcr.io`
with a token first.

**Cutting a release:** Actions → [Cut release](.github/workflows/cut-release.yml) → Run
workflow, and enter a version like `0.8.2`. It runs the checks, builds the multi‑arch
(amd64 + arm64) images, boots one and proves it serves, and only then creates the
`v0.8.2` tag and GitHub Release. A release that cannot start never gets a tag.

### Updating

```bash
make update
```

…or press **Settings → Software updates → Update now**. Both run the same script, and
that is the point: there is one code path that applies a release, so the two cannot
deliver different things.

It applies **everything** a release contains — the images, `docker-compose.prod.yml`,
`infra/Caddyfile`, new `.env` settings, every service — then asks your actual published
address whether the site answers, from outside the containers. If it doesn't, the box is
put back: your `.env`, the previous checkout, and the exact image digests that were
running, pinned so the rollback works with no network at all. Then it tells you which of
those two things happened.

**[docs/UPDATING.md](docs/UPDATING.md) has the full story**, including the one case that
cannot be made automatic — a release whose migration succeeded and whose code is then
unhealthy, where the images can be rolled back and the schema cannot.

To enable the in-app button, once per box:

```bash
sudo ./scripts/install-updater.sh
```

That installs a small root-owned agent on the host. It takes no parameters and reads
nothing out of the request that could choose what gets deployed, so the most the app can
ask for is "an update", never "this code". Without it everything still works; the panel
just says in-app updates aren't set up here.

**Release channels.** `latest` follows tagged releases (`v*`) and is what a box should
normally run. `edge` follows every green build of `main`. `sha-<short>` is an immutable
build. Set `SAIVA_VERSION` in `.env`. Note that `edge` and `sha-` builds carry no release
number, so the app can tell you which build you're on but *cannot* tell you whether a
newer release exists — and it says so rather than showing a tick.

### If an update goes wrong

```bash
./scripts/deploy.sh --status      # what the last run did, step by step
./scripts/deploy.sh --rollback    # undo the last applied release
```

The same status is at `https://<your address>/_saiva/update-status.json`, served by Caddy
straight off the host — so it still answers when the app itself is the thing that's
broken.

**Pre-migration backups.** Before applying any schema migration to an existing database,
the API writes a compressed `pg_dump` into the `db_backups` volume. If that dump fails,
it refuses to migrate.

**Restoring one** — this is the command that is worth knowing before you need it:

```bash
./scripts/restore-backup.sh                 # list the dumps, newest first
./scripts/restore-backup.sh saiva-20260411-021500-premigrate.sql.gz
```

It runs from a throwaway container, because the dumps are mounted only into the `api`
container and the whole reason you're reading this is that `api` won't start. It takes a
dump of the current state *first*, so a mistaken restore is itself reversible, and it
asks you to type a word, because it destroys everything written since the dump was taken.

**Rolling back further.** Pin an exact build and apply it:

```bash
# Edit .env:  SAIVA_VERSION=v0.16.1    (or sha-1a2b3c4 for an exact build)
make up
```

Set it **in `.env`**, not on the make command line: a variable passed to `make` lasts for
that one command, and the next `make update` would read `.env` and roll straight forward
again.

An older image meeting a newer database is handled rather than fatal — it starts, serves,
and prints a loud explanation telling you to roll forward or restore a dump. It used to
crash-loop instead, and write a full copy of the database on every restart attempt; see
`backend/app/schema_state.py`.

**Images are never pruned automatically**, because the image a rollback needs is dangling
by definition. `docker system df` shows where you are; `docker image prune` reclaims it
once a release has proved itself.


**Alerts & email digests.** The **Alerts** page always shows an in‑app feed (over‑budget
categories, unusual spend, upcoming bills, large transactions, low projected balance).
Email is opt‑in: set `SMTP_*` in `.env`, enable it on the Alerts page, and have a scheduler
hit the run endpoint to send new alerts and weekly/monthly digests — for example a cron line
`*/30 * * * * curl -fsS -X POST -H "X-Notify-Token: $NOTIFICATIONS_TOKEN" https://<host>/api/notifications/run`.

**AI advisor (BYO key).** Connect Anthropic (Claude), Google Gemini, any OpenAI‑compatible endpoint, or a
local **Ollama** in **Settings → AI advisor**, then ask questions on the **Advisor** page. Your
key is stored encrypted (derived from `SECRET_KEY`). A **privacy mode** controls what's shared —
*local only*, *aggregates only* (default: category totals & summaries, no raw transactions), or
*full detail*. Every call is recorded in the audit log; answers are general information, not advice.

### Running on Proxmox LXC

Saiva runs as Docker containers, so on Proxmox you first need an LXC that *can* run
Docker (or use a VM, which needs no special setup). For an LXC:

1. Use a **Debian/Ubuntu** container — an *unprivileged* one is fine.
2. **Enable nesting** (required for Docker), plus `keyctl`. From the Proxmox host shell:
   ```bash
   pct set <ctid> --features nesting=1,keyctl=1
   ```
   (or *Container → Options → Features → Nesting* in the web UI), then start the container.
3. **Install Docker Engine + the Compose plugin** inside the container.
4. Clone the repo and run `make deploy` exactly as above.

Notes:
- **Storage driver:** Docker's `overlay2` works with nesting on modern kernels; on
  ZFS‑backed containers you may need `fuse-overlayfs` if Docker complains on first start.
- **Access / TLS:** `https://localhost` only works from *inside* the container. From your
  LAN, reach it at the container's IP — `make deploy LAN=1` sets `SAIVA_SITE_ADDRESS` to
  that IP and Caddy serves HTTPS with its internal CA automatically. Point a domain at it
  (`SAIVA_SITE_ADDRESS=yourdomain`) for a trusted Let's Encrypt certificate.
- **Easiest alternative:** run Docker in a Proxmox **VM** instead — the steps above then
  work verbatim, with no nesting or storage tweaks.

## Local development

**Backend** (FastAPI, Python 3.11):

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
# Point at a local Postgres, or use SQLite for a quick spin:
export DATABASE_URL="sqlite+pysqlite:///./saiva.db" SECRET_KEY=dev ENVIRONMENT=development
python -m app.services.seed          # creates the schema + a demo login
uvicorn app.main:app --reload --port 8000
```

**Frontend** (React + TypeScript + Vite):

```bash
cd frontend
npm install
npm run dev                          # http://localhost:5173 (proxies /api → :8000)
```

Demo login (after seeding): `demo@saiva.app`, with a password generated during
seeding and printed once. Seeding is refused on an install that already has real
accounts — the demo user is an owner, and adding one to a household in use would
be indistinguishable from planting a way in.

## Testing & quality gates

```bash
# Backend: lint, types, tests (SQLite, no DB server needed)
cd backend && ruff check . && mypy app && pytest --cov=app

# Frontend: lint, type-check + build, unit tests
cd frontend && npm run lint && npm run build && npm run test
```

[`backend/tests/test_import_data_quality.py`](backend/tests/test_import_data_quality.py)
is worth knowing about separately: it checks the ledger after a *sequence* of imports —
overlapping months, mixed formats, files re-exported after the bank re-words things,
edits and splits in between — because a duplicate never announces itself. It ends with a
property test that slices a year of transactions into overlapping windows, shuffles them,
renders each in a random format, and requires the result to reconstruct the original
exactly.

The gates themselves live in [`.github/workflows/checks.yml`](.github/workflows/checks.yml)
as a reusable workflow, so every path that publishes an image — the `edge` build on
`main`, a `v*` tag, and the manual "Cut release" — runs exactly the same jobs first. On
top of the commands above it adds a Postgres migration check (`alembic upgrade head` +
`alembic check`), bandit SAST, a gitleaks secret scan, and **blocking** dependency
audits (`pip-audit`, `npm audit --audit-level=high`).

[`.github/workflows/security-scan.yml`](.github/workflows/security-scan.yml) runs Semgrep,
Trivy and hadolint on every PR and weekly, reporting into the repository's Security tab
and printing findings to the run log — a scan whose results live only in a SARIF file
is one nobody reads. Trivy also emits a CycloneDX SBOM, kept with the run for 90 days. It is
separate from the gates on purpose: those scanners re-fetch their rules and CVE data on
every run, so their verdict on an unchanged commit changes over time, and a tag that
built on Tuesday should not fail on Wednesday for a reason nobody introduced. The weekly
run is the point — it finds the CVE published after the code merged.

## Dependency pinning

The API image installs from [`backend/requirements.lock`](backend/requirements.lock) with
`pip install --require-hashes`, not from the ranges in `pyproject.toml`. Ranges are right
for humans and wrong for a production build: anything published inside `httpx>=0.27`
would otherwise land in the image on the next rebuild, unreviewed.

After changing `[project].dependencies`, regenerate the lock:

```bash
pip install pip-tools
cd backend && pip-compile --generate-hashes --strip-extras --no-header \
    --output-file=requirements.lock pyproject.toml
# keep the explanatory header at the top of the file
```

`scripts/check_lockfile.py` runs in CI and fails if the two files disagree, so a
dependency added to `pyproject.toml` cannot quietly miss the image.

Third-party GitHub Actions are pinned to 40-character commit SHAs rather than tags — a
tag is a mutable pointer its owner can move, which is exactly how the
`tj-actions/changed-files` compromise (CVE-2025-30066) reached thousands of repositories.
Dependabot understands SHA pins and keeps them current.

## The AI advisor and untrusted text

Transaction descriptions come from statement files, which come from banks, which
pass through whatever a payee typed into a payment reference. That text reaches the
model's system prompt, and the model has tools.

Two things keep that bounded. The tools are bound to the caller's session household
and the privacy mode is re-checked when each one runs, so no amount of prompt
trickery reaches another household, writes anything, or sees detail the household
asked to keep back. And the statement text is fenced inside a labelled block, with
the fence markers and control characters stripped out of it, so a merchant named
"IGNORE ALL PREVIOUS INSTRUCTIONS" cannot close the fence and address the model
directly.

`backend/tests/test_prompt_injection.py` pins both.

## Rate limiting and the reverse proxy

Login, first-run setup and password change share one per-caller ceiling; file
import, the AI advisor and PDF reports each get their own, so exhausting one cannot
lock a household out of signing in. Tune them with `RATE_LIMIT_LOGIN_PER_MINUTE`,
`RATE_LIMIT_IMPORT_PER_MINUTE`, `RATE_LIMIT_AI_PER_MINUTE` and
`RATE_LIMIT_REPORT_PER_MINUTE` (0 disables one).

Behind a proxy, `request.client.host` is the proxy, so every visitor would share a
bucket and every audit-log row would record the same address. `TRUSTED_PROXIES`
(comma-separated IPs, CIDRs or hostnames) names the proxies whose
`X-Forwarded-For` may be believed — the shipped Compose file sets it to `caddy`.
Leave it empty when nothing fronts the API: an unset value means the header is
ignored, which is the safe default, because a client can send that header itself.

## How updates reach this box, and what has access to what

Nothing in the stack can reach the Docker daemon. There is no socket mounted into any
container, and no updater service. That used to be false, and the change is the point of
the whole design, so it is worth stating plainly.

The old arrangement was Watchtower behind a filtering socket proxy, triggered by the app
over a shared token. It worked as far as it went, and that was the problem — it went
three-eighths of the way. Watchtower never reads a compose file; it recreates a container
from the *old* container's own inspect output, so no compose change, no `.env` value, no
port binding and no Caddyfile edit could reach this box through it. It also treats a
container that merely *starts* as a successful update, so there was no gate and nothing
to roll back to. And the proxy's own documentation said the quiet part: updating a
container means creating one, and anything that can create a container can create a
privileged one and take the host. That access existed so a button could pull two images.

What replaces it:

- **`scripts/deploy.sh --apply-release` on the host** applies the whole release — images,
  compose file, Caddyfile, `.env`, every service — and gates on the published address
  answering from outside the containers. On any failure it restores `.env`, the previous
  checkout and the exact image digests, then re-gates.
- **The button writes a file.** The app has one bind-mounted directory. It writes
  `request` into it; a root-owned agent on the host (`scripts/saiva-update-agent`, put
  there once by `scripts/install-updater.sh`) notices, deletes it, and runs that script
  with no arguments. The agent resolves the target release itself and reads nothing out
  of the request that could select what gets deployed, so the most a compromised app
  container can ask for is the update the button was going to do anyway.
- **The status is a file on the host**, served by Caddy at `/_saiva/update-status.json`.
  That matters because the app container is the thing being replaced: the state has to
  outlive it, or the panel can only guess. It used to guess, and it guessed "applied".

The agent is a **copy**, not a symlink into the checkout, because otherwise a release
could change what runs as root here. It therefore goes stale, and says so:
`sudo /usr/local/lib/saiva/saiva-update-agent --self-check`.

**Nothing updates itself on a timer.** A release arrives when you press **Update now** or
run `make update`, with someone at the keyboard. That is deliberate: the hazard on a box
holding a family's financial records is an update that cannot be undone, not an
unsupervised rollout, and a timer would remove the one person who can tell the difference.

**Images are never pruned automatically**, because the image a rollback needs is dangling
by definition — the tag it had moved to the release that replaced it. Every applied
update holds roughly 140 MB. `docker system df` to see, `docker image prune` once a
release has proved itself.

Full detail: **[docs/UPDATING.md](docs/UPDATING.md)**.


### Repository settings to set by hand

Two protections cannot be committed to the repo and must be set in GitHub's settings:

- **Tag protection for `v*`** — branch protection does not cover tags. Without it,
  anyone who can push a tag can start a release build.
- **Require the `Checks` status** on pull requests to `main`, so the gates cannot be
  merged past.

## Database migrations

Schema changes are versioned with **Alembic**. The API container runs `alembic upgrade
head` on start (a legacy `create_all` database is adopted automatically), so deploying a
newer image migrates the database with no manual step and no data loss.

After changing a model, generate a migration, review it, and commit it:

```bash
cd backend
alembic revision --autogenerate -m "describe the change"   # review migrations/versions/*.py
alembic upgrade head                                         # apply locally
```

CI fails if a model change ships without a matching migration (`alembic check`).

## Project structure

```
backend/    FastAPI API — auth, accounts, import, categorisation, transfers, dashboard,
            budgets, bills/recurring, forecast, net worth, goals, insights, benchmarks,
            alerts/notifications, financial-year reports, AI advisor; pytest (≈90% coverage)
frontend/   React + TS SPA (Vite) — overview, insights, advisor, alerts, transactions,
            accounts, budgets, bills, forecast, net worth, goals, benchmarks, import, settings
infra/      Caddy reverse-proxy config (auto-HTTPS)
scripts/    deploy.sh (install, update, rollback), restore-backup.sh, the host
            update agent and its installer, and lib/ + tests/ behind them
docs/       Product Requirements Document, UPDATING.md
.github/    CI + release workflows (tests, image publish, Cut release)
docker-compose.yml        local build (Postgres + API + web + Caddy)
docker-compose.prod.yml   prebuilt GHCR images + Caddy (production; the one `make update` applies)
```

## 📄 Product Requirements Document

The full PRD (v0.3) lives at ➡️ **[`docs/PRD.md`](docs/PRD.md)**.

### Locked decisions
- **Name:** Saiva.
- **Stack:** Python/FastAPI + React/TypeScript + PostgreSQL + Caddy (auto‑HTTPS).
- **Single household, self‑hosted** (your own container); a few friendly families pilot it, each self‑hosting.
- **File import (CSV/OFX/QFX/QIF) for v1**, architected for **Open Banking / CDR** feeds later.
- **AI advisor:** bring‑your‑own cloud key (default; Anthropic/OpenAI/Gemini) **or** local model (Ollama).
- **Budgets:** flexible tracking by default, optional envelope/rollover.
- **Net worth:** simple manual assets & liabilities.
- **Periods:** configurable — weekly/fortnightly/monthly pay cycle, or calendar months over the FY.
- **Alerts:** in‑app + email for v1; PWA/web push later.
- **Benchmarks:** Australian Bureau of Statistics public data.
- **HTTPS only.**
- **Security:** secure‑by‑design (OWASP ASVS L2 target) — defense in depth, least privilege, privacy by default.
- **Testing:** full test pyramid (unit → integration → e2e) + security scanning, enforced by CI quality gates.

All [open questions](docs/PRD.md#18-open-questions) are resolved (PRD **v0.3**).
