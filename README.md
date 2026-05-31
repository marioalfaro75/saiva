# Saiva — Household Finance & Insights App

A self‑hosted web app that helps an Australian family understand their income and
spending, get actionable insights and savings recommendations, benchmark against
similar households (ABS data), and — optionally — chat with an AI advisor (cloud
BYO‑key or a local model) grounded in their own data. Runs in a container, HTTPS‑only.

> **Status:** Phase 0 + Phase 1 (MVP) implemented — accounts, file import
> (CSV/OFX/QFX) with de‑duplication, rule + ML categorisation, transfer
> detection, and an overview dashboard. See the [PRD](docs/PRD.md) for the full plan.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env: set SECRET_KEY (e.g. `openssl rand -hex 32`) and a DB password
docker compose up -d --build
```

Then open **https://localhost** (Caddy issues a locally‑trusted certificate for dev).
On first visit you'll create your household and owner login. To explore with realistic
sample data, go to **Settings → Load demo data**.

For a LAN address or a public domain, set `SAIVA_SITE_ADDRESS` (and `ACME_EMAIL` for a
real domain) in `.env` — see [`infra/Caddyfile`](infra/Caddyfile).

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

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same gates plus
security scans (bandit SAST, pip‑audit, gitleaks secret scan, trivy) on every push/PR.

## Project structure

```
backend/    FastAPI API — auth, accounts, import pipeline, categorisation,
            transfers, dashboard; pytest suite (≈90% coverage)
frontend/   React + TS SPA (Vite) — dashboard, transactions, import, settings
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
