# Saiva — Household Finance & Insights App

A self‑hosted web app that helps an Australian family understand their income and
spending, get actionable insights and savings recommendations, benchmark against
similar households (ABS data), and — optionally — chat with an AI advisor (cloud
BYO‑key or a local model) grounded in their own data. Runs in a container, HTTPS‑only.

> **Status:** Planning (PRD **v0.2** — all open questions resolved). No application code
> yet — this repo currently contains the product definition.

## 📄 Product Requirements Document

The full PRD (v0.2 — open questions resolved) lives here:

➡️ **[`docs/PRD.md`](docs/PRD.md)**

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

All [open questions](docs/PRD.md#18-open-questions) are now resolved (PRD **v0.2**).
