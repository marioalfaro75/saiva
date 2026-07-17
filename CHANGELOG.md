# Changelog

All notable changes to Saiva are documented here. The project follows
[semantic versioning](https://semver.org); the newest release is first.

## [0.8.0] — 2026-06-09

A large feature release: smarter categorisation, a full "Advice & foresight"
suite (Phase 3), and safer, more automated deployments.

### Added

**Assisted categorisation**
- Per-row categorise popover with a scope chosen each time — *this only*, *all
  from this merchant*, *exact description*, or *contains text* — plus optional
  "make a rule" and a per-transaction **lock** (exempt from auto-categorisation).
- **Group review** (uncategorised grouped by merchant/description) and
  **multi-select** bulk categorise / lock.
- **Rules** manager with a live match/fill preview, apply-now backfill, and
  inline editing. User rules take priority over the built-in starter rules.

**Bills & recurring** — automatic detection of subscriptions, bills and salary by
cadence and amount stability, an upcoming-bills view, and committed-monthly /
subscriptions / recurring-income totals.

**Cashflow forecasting** — projects your balance forward from recurring income and
a per-category spending run-rate, highlights the lowest projected point, and
supports simple what-if scenarios ("cut a category by N%").

**Alerts & email digests** — an in-app alert feed (over-budget categories, unusual
spend, upcoming bills, large transactions, low projected balance) with opt-in
email and weekly/monthly digests via a scheduled run endpoint. Quiet by default.

**Financial-year PDF report** — a one-click accountant summary (totals, spend by
category, month-by-month, top merchants) for any financial year.

**AI advisor (bring your own key)** — ask questions about your own data using
Anthropic (Claude), any OpenAI-compatible endpoint, or a local Ollama. Three
privacy modes (local-only / aggregates-only / full detail), the key stored
encrypted, and every call recorded in the audit log. General information, not
personal financial advice.

### Changed / Infrastructure
- **Continuous delivery:** merges to `main` publish `edge` + `sha-<short>` images;
  a version tag publishes `:latest` and versioned images.
- **Pre-migration backups:** the API writes a compressed `pg_dump` before applying
  any schema migration and refuses to migrate if the backup fails, so every
  upgrade is reversible.

### Upgrade notes
- Adds database migrations **0005–0007**; they apply automatically on start (after
  the pre-migration backup). No manual steps.
- **Email/alerts (optional):** set `SMTP_*` and `NOTIFICATIONS_TOKEN` in `.env`,
  enable email on the Alerts page, and add a cron that POSTs to
  `/api/notifications/run` with the `X-Notify-Token` header.
- **AI advisor (optional):** configured in-app under **Settings → AI advisor** — no
  environment variables; the key is encrypted at rest.
- On the first GHCR publish, set the `saiva-api` / `saiva-web` packages to public
  (or `docker login ghcr.io` on the host) so image pulls are authorised.

[0.8.0]: https://github.com/marioalfaro75/saiva/compare/v0.4.0...v0.8.0
