# Changelog

All notable changes to Saiva are documented here. The project follows
[semantic versioning](https://semver.org); the newest release is first.

## [Unreleased]


## [0.15.0] — 2026-09-05

A security release. A full review of the codebase found thirty confirmed problems
and this fixes all of them, several of which were quietly giving wrong answers
rather than failing. Also stops one month downloaded in two formats importing twice.

Existing sessions survive the upgrade, and the database migrates itself on start as
usual. Nothing to do beyond pulling the images.

### Security

Measured against OWASP. These are the findings that change what the app does for
you; the rest were hardening you will never see.

- **Signing out now ends the session.** It used to clear the browser's cookie while
  the token stayed valid for its full fourteen days, so a household that suspected a
  password was known had no move available — and there was no way to change a password
  at all. There is now, along with "sign out everywhere", and both stop every token
  issued earlier from working.
- **Logging in costs the same whether the address exists or not.** The reply used to
  come back instantly for an unknown email and slowly for a known one, which is enough
  to work out who has an account.
- **"Local only" means it.** The privacy mode promised nothing left your network and
  sent your transactions to a cloud provider anyway.
- **Transfer detection needs evidence.** It linked any two equal and opposite amounts a
  few days apart — a rent payment and a salary, a bill and a refund — and removed both
  from every total, silently, across your whole history after each import.
- **Alert emails actually send.** They were sent only for notifications created during
  the cron run, but opening the app creates them too. If any browser tab had loaded
  first, the alert reached nobody. The scheduled run itself had never worked either: it
  was refused before its token was read.
- **Caddy binds where the documentation says.** A default install published on every
  interface, and Docker's routing bypasses the host firewall, so "this machine only"
  was reachable from the whole network.
- **Household settings are owner-only.** The financial-year start and pay-cycle basis
  decide where every period boundary falls, so anyone who could edit them could restate
  every figure the household had ever been shown.
- Three ways a read-only viewer could freeze the app for hours are closed, as are an
  unvalidated AI provider URL that could be aimed at your own network, an import that
  crashed on an absurd amount, and rate limiting that counted every visitor behind the
  proxy as one person.

Behind the scenes: the API image now installs exactly the dependencies recorded in a
hash-pinned lockfile, every build action is pinned to a commit rather than a movable
tag, a version tag can no longer publish an image that skipped the tests, and Semgrep,
Trivy, hadolint and Dependabot run alongside the existing checks.

### Fixed

- **Importing the same month as both OFX and CSV no longer doubles it up.** Banks word
  the same purchase differently in the two formats — `WOOLWORTHS METRO` in one,
  `EFTPOS WOOLWORTHS 4521 SYDNEY NSW` in the other — and the wording is what near-match
  detection compares. Every transaction in a month downloaded in both formats became
  two rows. Matching now also recognises the same merchant for the same amount on the
  same day, and asks you to confirm rather than deciding alone. Two different shops
  charging the same amount on the same day are still two purchases.

### Added

- A data-quality test suite for imports that checks the ledger after a *sequence* of
  real-world exports — overlapping months, mixed formats, files re-exported after the
  bank re-words things, and edits and splits in between — rather than one file at a
  time.


## [0.14.1] — 2026-08-26

Patch release: the handful of rows an import wants you to look at were unreachable
when the file was mostly duplicates.

### Fixed
- **The rows an import wants you to look at can now be reached.** Re-importing a
  statement you already have is thousands of rows that need nothing and a handful
  that look close enough to something existing to want a decision — and the preview
  showed the first 200 rows of one long list, so those few were invisible. Sorting by
  status was meant to be the way to them and is not, because the definite duplicates
  sort ahead and there are thousands of them. The preview now separates what needs an
  answer from what does not, opens on the former, and never holds back a row that
  wants one. Rows you are only reading stay capped, and it says how many and of what.


## [0.14.0] — 2026-08-26

The import page asks for a statement and nothing else, and works out the rest from
the file. Also fixes an OFX download covering several accounts being filed into one.

### Added
- **The import page asks for a file, and nothing else.** It used to open with an
  account dropdown beside the file input — a two-field form whose first question was
  the one nobody can answer yet, since whether the file covers one account or names
  its own is not known until it is read. Now there is a drop zone and a sentence
  saying the account question may not arise at all. If it does, it is asked
  afterwards, in context, as *"These transactions all belong to…"*.
- **Start again**, to abandon a file and everything derived from it, and **Cancel**
  while a preview is running. Importing has no cancel on purpose: the server finishes
  regardless, so the button would be a lie.

### Fixed
- **An OFX file covering more than one account no longer merges them.** Every
  transaction in the download was filed under whichever single account you picked,
  silently — a bank statement download routinely covers several. Each statement's
  transactions now keep the account they came from, and choosing a single account for
  a file that covers several is refused rather than obeyed.


## [0.13.0] — 2026-08-26

The import wizard reads a statement instead of interrogating you: it shows what is
in each column and asks you to confirm, and a file that names its own accounts is
mapped without being asked to choose one.

### Added
- **The import wizard reads a file instead of interrogating you.** It lists every
  column with the first few values from your own file and asks what each one is —
  Date, Description, Money in, Money out, Amount, Account, Balance or Ignore. An
  unfamiliar header stops mattering because you read the data, and nothing is left
  out silently: anything unmapped shows as *Ignore* rather than blank. If something
  needed is missing it says which, rather than greying out a button.
- **A statement that names its own accounts is recognised.** When a column carries
  account numbers the import maps them straight away, and never asks you to pick a
  single account first — which is what used to hide multi-account import entirely.
  Each value shows what it actually is: how many rows, the period it covers, the
  balance it ends on and a sample line, because "7.34364E+11" identifies nothing but
  a balance of −$819,480.37 over 74 rows is obviously the mortgage.
- **The account column is found by the shape of its values**, not only by its header,
  so a column called something unexpected — or a file with no header row — still
  works. BSB-and-number, masked cards and plain account numbers are all recognised.
- **Mappings are remembered per shape of file.** The next export from the same bank
  opens already mapped, ignored columns included. It still shows you the mapping
  every time, so a bank quietly adding a column is noticed rather than absorbed.
- **How dates are read is stated, against a real value from your file.** `01/07/2025`
  is a valid date whether the 1 or the 7 is the month, so nothing fails to parse and
  the wrong assumption files a year of transactions into the wrong months in silence.
  The mapping step says *"01/07/2025 in your file is 1 July 2025"* and lets you switch
  it, and says when a value is unambiguous either way.
- **The separator is shown and can be corrected**, since a tab-separated file whose
  text contains commas is routinely mis-detected and then reads as a single column.

### Fixed
- **Excel-mangled account numbers are flagged.** Opening a statement in Excel turns a
  long account number into something like `7.34364E+11`, losing its digits. The import
  still handles it, says what happened, and declines to remember a value that can
  never recur.
- Switching an import to a single signed amount column no longer pre-selects a column
  called "Debit Amount" — which contains "amount" — and turns every credit into a debit.


## [0.12.0] — 2026-08-22

Navigation moves out of a crowded top bar into a side menu, and the phone becomes a
place you can actually work rather than a scaled-down desktop. Also fixes a bug in
the import wizard that could import or skip the wrong rows from a statement.

### Added
- **A side menu replaces the top bar.** Thirteen links across the top had run out of
  room; they now sit in a sidebar grouped by what you are doing — seeing where you
  stand, working with transactions, planning, and the long view. On a laptop it is
  always there; below 1080px it is a drawer behind a menu button, closing when you
  pick something, on Escape, or on a tap outside.
- **The period you are looking at is always on screen.** The app bar carries it
  permanently and turns amber whenever it is not the current period, so figures from
  a past financial year can't be mistaken for live ones.
- **The phone is a place you can actually work.** Below 640px the transactions list
  becomes a stack of cards with the same controls the table has — selection, the
  category picker, lock, and the transfer tag — plus select‑all, sort chips and
  stacked filter fields in place of column headers. Pages are shorter there (25
  rather than 50) and everything you tap is at least 40px. The import wizard's two
  review tables get the same treatment.
- **Subtitles on the pages whose name doesn't say what they are for** — Forecast,
  Benchmarks, Net worth, Insights, and Bills.
- **Empty states that lead somewhere.** Instead of naming a page — "load demo data
  from Settings" — they link to it.

### Fixed
- **The import wizard can no longer act on a preview that no longer applies.** Ten
  controls feed the preview and none of them cleared it, so changing the account or a
  mapped column left the old preview on screen — and importing then sent row numbers
  taken from it, which could import or skip the wrong rows. The preview now knows
  when it is out of date, says so, and Import waits for a fresh one. That button also
  says what it will do: *Import 84 transactions*.
- **Editing net worth showed the old figures.** Every add, edit and delete wrote its
  result into the wrong cache entry, so the table kept the values from before the
  change until the page was reloaded.
- **Overview no longer reads `$0.00` while it is still loading.** Income, expenses and
  net show an em dash until the figures arrive, as the savings rate already did.
- **Budgets, Goals, Alerts and Benchmarks lead with your data**, not with the form for
  adding more of it.
- **Wide tables scroll inside their card** instead of squashing every column to
  illegibility, and are reachable from the keyboard.
- Group review is its own tab on Transactions rather than a mode hidden inside the
  list, and it says plainly that it looks across all time rather than the selected
  period.
- Keyboard focus is visible everywhere, and the categorise dialog traps focus and
  returns it.
- **Every form label now belongs to its field.** Forty-four of them were text sitting
  beside a control with nothing joining them, so tapping the word didn't focus the
  box — most annoying on a phone — and a screen reader read the field with no name.
  On the sign-in form that meant email and password were both announced as an unnamed
  text field.


## [0.11.0] — 2026-08-20

The AI advisor answers from your own data instead of declining, and stops cutting
its answers off mid-sentence.

### Added
- **The AI advisor can now look things up.** It has read-only access to search your
  transactions, total spending by category or merchant, list uncategorised items and
  compare two periods — so questions like "are there any transactions mentioning
  Helen" or "who do I spend the most with" are answered from your data rather than
  declined. Which lookups it may use follows your **privacy mode**: the ones that
  reveal individual transactions are simply not offered in *Aggregates only*.
- **A far richer picture for the advisor.** It now sees top merchants, an
  uncategorised summary, month‑by‑month figures, a comparison with the previous
  period, budgets needing attention, goals, net worth, and a note of what data you
  hold overall — instead of a household line and ten category totals.
- The advisor **follows the period picker**, so asking while viewing a past financial
  year answers for that year.

### Fixed
- **Answers no longer stop mid‑sentence.** The reply limit was too small for the
  questions this is for, and on Gemini 2.5 models the limit was being spent on the
  model's own reasoning before it wrote anything. The limit is larger, reasoning has
  its own budget, and a reply that does hit the limit now says so rather than
  trailing off.
- **The advisor explains its own limits.** When a question needs data your privacy
  mode withholds, it now says which setting is responsible and where to change it,
  instead of only "the provided data does not include…".
- Amounts in advisor answers read `-$1,200.00` rather than `$-1,200.00`.


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

[0.15.0]: https://github.com/marioalfaro75/saiva/compare/v0.14.1...v0.15.0
[0.14.1]: https://github.com/marioalfaro75/saiva/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/marioalfaro75/saiva/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/marioalfaro75/saiva/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/marioalfaro75/saiva/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/marioalfaro75/saiva/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/marioalfaro75/saiva/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/marioalfaro75/saiva/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/marioalfaro75/saiva/compare/v0.8.4...v0.9.0
[0.8.4]: https://github.com/marioalfaro75/saiva/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/marioalfaro75/saiva/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/marioalfaro75/saiva/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/marioalfaro75/saiva/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/marioalfaro75/saiva/compare/v0.4.0...v0.8.0
