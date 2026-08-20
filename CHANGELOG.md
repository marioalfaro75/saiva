# Changelog

All notable changes to Saiva are documented here. The project follows
[semantic versioning](https://semver.org); the newest release is first.

## [Unreleased]


## [0.10.1] — 2026-08-20

Patch release: fixes a security-token error that blocked saving once the app
had been opened in more than one tab.

### Fixed
- **"CSRF token missing or invalid" when the app is open in more than one tab.**
  Loading Saiva in a second tab reissued the security token, leaving the first tab
  sending one that no longer matched — so saving anything there failed until it was
  reloaded, while reading kept working. The token is no longer reissued when you
  already have one, each request now reads the current one rather than a copy kept
  from startup, and a request rejected for this reason fetches a fresh token and
  retries once.


## [0.10.0] — 2026-08-17

Every table in the app can now be sorted by any column and filtered column by
column.

### Added
- **Sort and filter every table.** Every column in every table is sortable, and each
  has its own filter box (behind a **Filter** toggle on each table). Sorting uses the
  underlying value, so amounts and dates order properly rather than alphabetically,
  and blank cells always sort last. How you left a table is remembered.
- **Transactions sorts and filters across the whole list, not the visible page** —
  the ordering and filtering happen in the database, and the choice is kept in the
  URL so a sorted, filtered view can be reloaded or shared.
- **Search now covers every column** on the Transactions page — previously only the
  description and merchant; it now also matches the account, category, notes, date
  and amount.
- **Net worth** gained a header row, so assets and liabilities can be sorted too.


## [0.9.0] — 2026-08-15

A feature release: look at any financial year across the whole app, import
statements that cover several accounts, and imports that no longer double up —
or quietly lose — transactions.

### Added
- **Global period picker** — a selector in the top bar that every period‑aware view
  follows: financial years, their quarters and months, relative ranges, and *all
  time*. Quarters and labels come from your own **FY start** in Settings, so a
  July–June household sees `FY2025–26` with quarters from July, and a calendar‑year
  one sees `2025`. The choice persists across reloads and rides in the URL, so a link
  opens on the same period. Views that look forward (forecast, bills, goals) or
  report a position (net worth) answer *as at* the selected period rather than today,
  and a banner appears whenever you are not looking at the current period.
- **Import — statements covering several accounts.** Tick “rows belong to more than
  one account”, pick the column that names it, and map each value to an account or
  create one inline. Unmapped values are skipped and reported rather than filed
  somewhere arbitrary; the mapping is remembered for next time.

### Fixed
- **Import no longer drops genuine repeat transactions.** Two identical purchases on
  one day (two coffees, two ATM withdrawals) hashed the same, so the second was
  silently discarded — on the first import and on every later one. Duplicates are now
  matched by count, so repeats survive while re‑importing a file still skips it.
- **Import catches duplicates it used to miss** — OFX/QFX `FITID` is now stored and
  matched first, identifying a transaction even when the bank re‑dates and re‑words
  it; and near matches (same amount, within a few days, similar wording after
  stripping receipt numbers) are flagged for review, skipped by default. Preview shows
  every row with its verdict and the transaction it matched, each overridable.

### Upgrade notes
- Adds migrations **0008–0009**; they apply automatically on start (after the
  pre‑migration backup). No manual steps, and the de‑duplication fingerprint is
  unchanged, so existing transactions keep matching.


## [0.8.4] — 2026-08-15

### Added
- **AI advisor — curated model lists per provider**: the Model dropdown now shows a
  built‑in list of valid, current models for the selected provider (Anthropic, OpenAI,
  Gemini) as soon as you pick it — no need to save a key first. When a key *is* set the
  provider's own live list is merged on top (deduped), and "Custom…" still accepts any id.

### Changed
- **AI advisor — Gemini default** bumped to `gemini-2.5-flash` (the previous
  `gemini-1.5-flash` default has been retired by Google).


## [0.8.3] — 2026-07-18

### Fixed
- **In‑app "Update now"** on newer Docker Engines. The archived `containrrr/watchtower`
  ships an old Docker client (API 1.25) that recent daemons reject, so updates silently
  did nothing. Switched the sidecar to the maintained drop‑in fork `nickfedor/watchtower`
  (auto‑negotiates the API version); `DOCKER_API_VERSION` stays as an optional override.


## [0.8.2] — 2026-07-17

### Added
- **AI advisor — Google Gemini** as a native provider (alongside Anthropic and
  OpenAI‑compatible), with model listing and the Test‑connection check.
- **Cut‑release workflow** — a manually‑triggered GitHub Action that builds the
  images then creates the tag + Release (the `v` prefix and `:latest` can't drift).


## [0.8.1] — 2026-07-17

Patch release: fixes the container image build and adds AI‑advisor
quality‑of‑life. First v0.8.x with publishable images (the v0.8.0 image build
failed).

### Fixed
- **API image build** — the `python:3.11-slim` base moved to Debian trixie while
  the Dockerfile pinned the `bookworm` PostgreSQL repo, breaking `make deploy` /
  `make pull` with an unmet‑dependency error. Pinned the base to
  `python:3.11-slim-bookworm` and derive the PGDG repo codename from the base OS.

### Added
- **AI advisor — model dropdown**: the Model field is populated live from the
  provider (Anthropic `/v1/models`; OpenAI‑compatible `/models`, including local
  Ollama), with a refresh and a “Custom…” fallback for anything not listed.
- **AI advisor — Test connection**: a one‑click round‑trip through the configured
  provider + key + model that reports success or the provider's own error.

### Changed
- AI provider errors now surface the provider's actual message instead of a bare
  “400”, and new setups default to a current model.


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

[0.10.1]: https://github.com/marioalfaro75/saiva/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/marioalfaro75/saiva/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/marioalfaro75/saiva/compare/v0.8.4...v0.9.0
[0.8.4]: https://github.com/marioalfaro75/saiva/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/marioalfaro75/saiva/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/marioalfaro75/saiva/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/marioalfaro75/saiva/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/marioalfaro75/saiva/compare/v0.4.0...v0.8.0
