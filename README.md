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
- **Duplicate‑proof imports** — overlapping date ranges never double up: matching uses the
  bank's own transaction id where present, then exact matches counted by occurrence, then
  near matches (re‑dated or re‑worded rows) flagged for review. Genuine repeat purchases
  are kept, not silently dropped.
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
docker compose ps                      # all services up; db shows "healthy"
curl -k https://localhost/api/health   # -> {"status":"ok"}   (-k accepts the local cert)
make logs                              # follow API logs
make down                              # stop, keep data   |   make destroy = stop + wipe DB
```

Run `make help` for all targets (`deploy seed up down destroy restart logs ps`).

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

Instead of building locally, you can run images published to GitHub Container Registry —
faster, reproducible, and easy on a low‑powered host (it doesn't compile the frontend):

```bash
make pull                       # pull ghcr.io/marioalfaro75/saiva-{api,web}:latest and start
make pull SAIVA_VERSION=v0.4.0  # pin a specific release;  make pull LAN=1 also works
make pull SAIVA_VERSION=edge    # track the latest green build of main (continuous)
make pull SAIVA_VERSION=sha-1a2b3c4   # pin / roll back to an exact build
# (equivalently: ./scripts/deploy.sh --pull)
```

Upgrades are then `make pull` (re‑pull + restart); the API self‑migrates the database on
start. **Cutting a release (recommended):** run **Actions → [Cut release](.github/workflows/cut-release.yml)
→ Run workflow** and enter a version like `0.8.2` — it builds the multi‑arch
(amd64 + arm64) `saiva-api`/`saiva-web` images and *then* creates the `v0.8.2` tag and
GitHub Release, so the `v` prefix and `:latest` never drift. (Pushing a `v*` tag by hand
still works via the [`Release`](.github/workflows/release.yml) workflow.) After the first
publish, set those GHCR packages to **public** (GitHub → your profile → Packages) if you want to pull without
`docker login`; otherwise `docker login ghcr.io` with a token first.

**Release channels.** `latest` follows tagged releases (`v*`); `edge` follows every green
build of `main` — merges auto-publish the changed image(s) as `edge` + `sha-<short>` via the
[`CI`](.github/workflows/ci.yml) workflow once tests pass; `sha-<short>` is an immutable
build to pin or roll back to. Pick a channel with `SAIVA_VERSION` in `.env`, then `make pull`.

**Pre-migration backups.** On start the API writes a compressed `pg_dump` to the
`db_backups` volume *before* applying any schema migration, so an upgrade is always
reversible (disable with `SAIVA_BACKUP_BEFORE_MIGRATE=0`; the dump lands in
`SAIVA_BACKUP_DIR`). If the backup fails, the container refuses to migrate. To roll back a
bad upgrade: `SAIVA_VERSION=sha-<previous>` then `make pull`, and restore the dump if a
migration had already run.

**In‑app updates.** On a pull‑based deploy, the app checks GitHub for newer releases and
shows the owner an **Update available** badge; **Settings → Software updates → Update now**
pulls and restarts via a token‑protected Watchtower sidecar (the API never touches the
Docker socket). After an update, open tabs get a one‑click **Reload** prompt. The update
check is a public, data‑free request and can be turned off with `UPDATE_CHECK_ENABLED=false`.

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

The gates themselves live in [`.github/workflows/checks.yml`](.github/workflows/checks.yml)
as a reusable workflow, so every path that publishes an image — the `edge` build on
`main`, a `v*` tag, and the manual "Cut release" — runs exactly the same jobs first. On
top of the commands above it adds a Postgres migration check (`alembic upgrade head` +
`alembic check`), bandit SAST, a gitleaks secret scan, and **blocking** dependency
audits (`pip-audit`, `npm audit --audit-level=high`).

[`.github/workflows/security-scan.yml`](.github/workflows/security-scan.yml) runs Semgrep
and Trivy on every PR and weekly, reporting into the repository's Security tab. It is
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

## Automatic updates and the Docker socket

The in-app "Update now" button asks Watchtower to pull and recreate the app
containers. Watchtower talks to a filtering socket proxy on an internal network
rather than holding `/var/run/docker.sock` itself, which blocks the API sections it
has no use for — exec, volumes, networks, build, swarm.

Be clear about what that buys: updating a container means creating one, and
anything that can create a container can create a privileged one and take the host.
The proxy narrows the blast radius of a bug in Watchtower; it does not contain an
attacker who reaches it. If you would rather not make that trade, delete the
`watchtower` and `docker-socket-proxy` services and update by hand — `docker compose
pull && docker compose up -d`. Everything works the same, minus the button.

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
scripts/    deploy.sh — the one-command deploy/pull helper behind `make`
docs/       Product Requirements Document
.github/    CI + release workflows (tests, image publish, Cut release)
docker-compose.yml        local build (Postgres + API + web + Caddy)
docker-compose.prod.yml   prebuilt GHCR images + Caddy + Watchtower (in-app updates)
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
