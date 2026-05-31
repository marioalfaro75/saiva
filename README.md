# Tally — Household Finance & Insights App

A self‑hosted web app that helps an Australian family understand their income and
spending, get actionable insights and savings recommendations, benchmark against
similar households (ABS data), and — optionally — chat with an AI advisor (cloud
BYO‑key or a local model) grounded in their own data. Runs in a container, HTTPS‑only.

> **Status:** Planning. No application code yet — this repo currently contains the
> product definition.

## 📄 Product Requirements Document

The full PRD (draft v0.1, for review) lives here:

➡️ **[`docs/PRD.md`](docs/PRD.md)**

### Locked decisions
- **Single household, self‑hosted** (your own container).
- **File import (CSV/OFX/QFX/QIF) for v1**, architected for **Open Banking / CDR** feeds later.
- **AI advisor:** bring‑your‑own cloud key (Anthropic/OpenAI/Gemini) **or** local model (Ollama).
- **Benchmarks:** Australian Bureau of Statistics public data.
- **HTTPS only.**

See the PRD's [Open questions](docs/PRD.md#18-open-questions) for the points still to confirm.
