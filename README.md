# Saiva — Household Finance & Insights App

A self‑hosted web app that helps an Australian family understand their income and
spending, get actionable insights and savings recommendations, benchmark against
similar households (ABS data), and — optionally — chat with an AI advisor (cloud
BYO‑key or a local model) grounded in their own data. Runs in a container, HTTPS‑only.

> **Status:** Phase 0 + Phase 1 (MVP) implemented — accounts, file import
> (CSV/OFX/QFX) with de‑duplication, rule + ML categorisation, transfer
> detection, and an overview dashboard. **Phase 2 in progress:** per‑category
> **budgets** (progress, projection, over/under) and a manual **net‑worth** balance
> sheet with a trend over time. See the [PRD](docs/PRD.md) for the full plan.

## Run it in a container

**Prerequisites:** Docker Engine + the Docker Compose plugin, and ports **80** and
**443** free on the host.

**1. Start it — one command.** Generates `.env` (with a secure random `SECRET_KEY`
and DB password), builds the images, starts the stack, and waits until it's healthy:

```bash
make deploy            # equivalently: ./scripts/deploy.sh
make deploy SEED=1     # also load demo data (login: demo@saiva.app / demodemodemo)
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
# (equivalently: ./scripts/deploy.sh --pull)
```

Upgrades are then `make pull` (re‑pull + restart); the API self‑migrates the database on
start. **Publishing** images: push a tag — `git tag v0.4.0 && git push origin v0.4.0` —
and the [`Release`](.github/workflows/release.yml) workflow builds and pushes multi‑arch
(amd64 + arm64) `saiva-api`/`saiva-web` images. After the first publish, set those GHCR
packages to **public** (GitHub → your profile → Packages) if you want to pull without
`docker login`; otherwise `docker login ghcr.io` with a token first.

**In‑app updates.** On a pull‑based deploy, the app checks GitHub for newer releases and
shows the owner an **Update available** badge; **Settings → Software updates → Update now**
pulls and restarts via a token‑protected Watchtower sidecar (the API never touches the
Docker socket). After an update, open tabs get a one‑click **Reload** prompt. The update
check is a public, data‑free request and can be turned off with `UPDATE_CHECK_ENABLED=false`.

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

Demo login (after seeding): `demo@saiva.app` / `demodemodemo`.

## Testing & quality gates

```bash
# Backend: lint, types, tests (SQLite, no DB server needed)
cd backend && ruff check . && mypy app && pytest --cov=app

# Frontend: lint, type-check + build, unit tests
cd frontend && npm run lint && npm run build && npm run test
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same gates plus a
Postgres migration check (`alembic upgrade head` + `alembic check`) and security scans
(bandit SAST, pip‑audit, gitleaks secret scan) on every push/PR.

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
backend/    FastAPI API — auth, accounts, import pipeline, categorisation,
            transfers, dashboard, budgets, net worth; pytest suite (≈90% coverage)
frontend/   React + TS SPA (Vite) — dashboard, transactions, accounts, budgets,
            net worth, import, settings
infra/      Caddy reverse-proxy config (auto-HTTPS)
docs/       Product Requirements Document
docker-compose.yml   one-command deploy (Postgres + API + web + Caddy)
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
