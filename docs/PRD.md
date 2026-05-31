# Product Requirements Document — Household Finance & Insights App

**Working title:** Tally _(placeholder — name candidates in Appendix E)_
**Version:** 0.1 — Draft for review
**Date:** 31 May 2026
**Author:** Mario Alfaro
**Status:** 🟡 Draft / awaiting review & refinement
**Primary market:** Australia (en‑AU, AUD)

> This is a first‑pass PRD intended as a starting point for discussion. Sections marked **[Decision]** capture choices already made; sections marked **[Open]** still need input. Please mark up anything you want to change, add, or cut.

---

## Table of Contents
1. [Summary](#1-summary)
2. [Goals & non‑goals](#2-goals--non-goals)
3. [Target users & personas](#3-target-users--personas)
4. [Problem statement](#4-problem-statement)
5. [Competitive research & insights](#5-competitive-research--insights)
6. [Product principles](#6-product-principles)
7. [Scope & phased delivery](#7-scope--phased-delivery)
8. [Functional requirements](#8-functional-requirements)
9. [Australian localisation](#9-australian-localisation)
10. [AI advisor](#10-ai-advisor)
11. [Non‑functional requirements](#11-non-functional-requirements)
12. [Proposed technical architecture](#12-proposed-technical-architecture)
13. [Data model (high level)](#13-data-model-high-level)
14. [UX & visual design direction](#14-ux--visual-design-direction)
15. [Security & privacy](#15-security--privacy)
16. [Success metrics](#16-success-metrics)
17. [Risks & mitigations](#17-risks--mitigations)
18. [Open questions](#18-open-questions)
19. [Appendices](#19-appendices)

---

## 1. Summary

**Tally** is a self‑hosted web application that helps an Australian family understand their income and spending, and take action to manage money better. A user uploads bank/credit‑card export files; the app automatically sorts transactions into a small set of meaningful home‑budget categories, then surfaces the family's financial picture through rich, interactive visuals — from a one‑glance overview down to individual transactions.

Beyond tracking, Tally is **insight‑first**: it proactively highlights where money is going, flags categories where the household spends more than comparable Australian families (using Australian Bureau of Statistics data), detects recurring subscriptions and bills, tracks net worth and savings goals, and forecasts cashflow. Users can connect an LLM of their choice — a cloud provider via their own API key, or a fully local model — to receive tailored, plain‑English advice grounded in the app's own insights and their data.

It runs as a container, on the home network behind HTTPS, with the option to expose it to the internet later. **Ease of use is a first‑class requirement**: a family should get genuine insight within minutes of their first upload, with minimal manual bookkeeping.

### Decisions locked for this draft **[Decision]**
| Area | Decision |
|---|---|
| Deployment & scope | **Single household, self‑hosted.** One shared dataset; multiple family‑member logins. |
| Bank data ingestion | **File import (CSV/OFX/QFX/QIF) for v1**, architected so automated **Open Banking / CDR feeds** can be added later. |
| AI advisor | **Bring‑your‑own** cloud API key (Anthropic / OpenAI / Gemini) **or** a **local model** (Ollama). User chooses; privacy‑preserving by default. |
| Peer benchmarking | **ABS public data** (Household Expenditure Survey + Monthly Household Spending Indicator). Community‑pooled benchmarks deferred. |
| Transport security | **HTTPS only**, automatic TLS. |
| Tech stack | **Python/FastAPI + React/TypeScript + PostgreSQL + Caddy** (auto‑HTTPS). |

---

## 2. Goals & non‑goals

### 2.1 Product goals
1. **Understand the money in/out** — give a family a clear, accurate picture of income vs. expenses with almost no manual data entry.
2. **Make insight effortless** — meaningful insights within ~5 minutes of the first import; ongoing use measured in minutes per week, not hours.
3. **Recommend, don't just report** — concrete, prioritised suggestions for where to save and how to manage money better.
4. **Provide context** — show how the family compares to similar Australian households (size, income band, location) so "is this normal?" has an answer.
5. **High‑level → detail** — a single overview screen that drills all the way down to a transaction.
6. **Bring your own AI** — optional LLM advisor that uses the app's insights and data, with the user in control of which model and how much data it sees.
7. **Own your data** — self‑hosted, private, HTTPS‑only, easy to run in a container and back up.

### 2.2 Non‑goals (for now) **[Decision]**
- Not a multi‑tenant SaaS (single household per deployment).
- Not a bank, broker, or payment system — Tally never moves money.
- Not a tax‑return product (though category data should be export‑friendly for an accountant).
- Not a provider of **personal financial product advice** under an AFSL — Tally gives general, factual budgeting information and the user's own AI does the rest (see [§17](#17-risks--mitigations)).
- Not investment portfolio management / trading (basic asset tracking for net worth only).
- No native iOS/Android apps in v1 — responsive web + installable PWA instead.

---

## 3. Target users & personas

**Primary:** An Australian family/household that wants to get on top of its finances without becoming bookkeepers.

| Persona | Description | Key needs |
|---|---|---|
| **Sam — the household CFO** | One adult who takes the lead on money. Comfortable downloading bank statements; wants the big picture and the levers to pull. | Fast import, trustworthy categorisation, clear overview, actionable savings ideas, benchmarking. |
| **Alex — the partner** | Shares the household but is less hands‑on. Logs in occasionally. | Simple shared view, "are we okay?" answer, low friction. |
| **The family** | Couple + kids; a mortgage or rent, two incomes, school/childcare, BNPL, subscriptions. | See total picture, find savings, set goals, avoid bill shock. |
| **(Later) Jordan — the tinkerer** | Self‑hosts other apps; wants local LLM, API access, control. | Docker, env config, local model support, data export, API. |

**Anti‑persona:** day‑traders, small‑business bookkeeping (GST/BAS) — explicitly out of scope, though we won't actively block personal use of business‑like features.

---

## 4. Problem statement

Families know roughly what they earn but rarely have a clear, current view of where it all goes. Existing options force a trade‑off:

- **Bank apps** show one institution and weak categorisation.
- **Manual spreadsheets** are powerful but high‑effort and quickly abandoned.
- **Commercial aggregator apps** (see §5) are convenient but require handing all bank data to a third party, often upsell financial products, and store sensitive data in the cloud. Several well‑loved ones have shut down (Mint globally; Pocketbook in Australia), stranding users.
- **Self‑hosted tools** keep data private but are built for hobbyist accountants (double‑entry, manual rules) and are neither easy nor insight‑led.

**The gap:** a *private, self‑hosted, genuinely easy* app that turns raw bank exports into clear insight, AU‑aware benchmarking, and AI‑assisted advice — with almost no manual work.

---

## 5. Competitive research & insights

Research across Australian and global apps, plus the self‑hosted ecosystem (sources in [Appendix F](#appendix-f--sources)).

### 5.1 Australian apps
| App | Strengths | Watch‑outs / gaps |
|---|---|---|
| **PocketSmith** | Best‑in‑class **cashflow forecasting** (project balances years ahead), detailed income/expense statements, strong **file import** + bank feeds, calendar view. | Forecasting depth can feel complex; paid tiers for the good stuff. |
| **Frollo** | Free; **Open Banking (CDR)** native; "Financial Passport" 12‑month summary of income/spend/assets/liabilities. | Less polished; insight depth limited. |
| **WeMoney** | Holistic health view, multi‑account, **credit score**, community. | Heavily monetises via **loan/mortgage referrals** (Lendi) — conflicts with neutral advice. |
| **Pocketbook** (defunct) | Was the beloved simple AU app. | **Shut down 2023** — proof that "easy + private + durable" is an unmet need. |

### 5.2 Global leaders
| App | What to learn from it |
|---|---|
| **Monarch Money** | Clean visual dashboards; **household/collaborative mode**; AI assistant you can *ask questions*; balance **forecasts**; **smart savings goals**; **anomaly/spending alerts**; net worth + investments in one place. |
| **YNAB** | Opinionated **zero‑based / envelope** method; recently added auto‑categorisation. Discipline and "give every dollar a job." |
| **Copilot Money** | **ML categorisation that learns from your corrections**; investments + cash in one view; delightful, fast UX. |
| **Goodbudget** | **Envelope budgeting** that's easy to share across a household. |

### 5.3 Self‑hosted / open‑source (closest architectural analogs)
| App | What to learn from it |
|---|---|
| **Firefly III** | Self‑hosted, **never phones home unless told**; powerful **rule engine** ("if description contains AMAZON → Shopping"); full **REST API**; Docker‑native. Downside: double‑entry, accountant‑ish, not insight‑led. |
| **Actual Budget** | Fast, **privacy‑first**, "you own your data," optional **end‑to‑end encryption**, envelope budgeting. |
| **Maybe Finance** | Open‑source "**OS for personal finances**," net‑worth centric. |

### 5.4 What this tells us — features to build in
**Table stakes:** multi‑account aggregation, reliable auto‑categorisation, budgets, net worth, clear dashboards, transaction search/edit.
**Differentiators users love (and we should adopt):**
- **Subscription/recurring detection** ("you have 14 subscriptions totalling $312/mo").
- **Bill reminders / upcoming cashflow** so there's no bill shock.
- **Cashflow forecasting** (PocketSmith's edge).
- **Savings goals** with progress and suggested contributions.
- **Anomaly / unusual‑spend alerts**.
- **Ask‑your‑data AI assistant** (Monarch) — our BYO‑LLM is a stronger, more private take.
- **Categorisation that learns from corrections** (Copilot).
- **Rule engine + API + "doesn't phone home"** (Firefly III) — fits our self‑hosted ethos.

**Our wedge (what no one combines):** *self‑hosted + private + genuinely easy + AU benchmarking + bring‑your‑own‑AI*, with neutral advice (no product upsell).

---

## 6. Product principles
1. **Insight in minutes, not bookkeeping for hours.** Every feature is judged on effort‑to‑value. Sensible defaults over configuration.
2. **Private by default.** Data stays on the user's box. Nothing leaves it without an explicit, visible action.
3. **Glance, then dig.** One overview answers "are we okay?"; every number is clickable down to the transaction.
4. **Recommend, with reasons.** Insights say *what*, *why it matters*, and *what to do* — and link to the evidence.
5. **Neutral.** No product referrals, no upsells, no conflicts of interest.
6. **Trustworthy numbers.** If categorisation is unsure, say so; make correction one click; never silently guess on money that matters.
7. **Australian‑first.** Currency, dates, financial year, language, and benchmarks all feel local.
8. **Boringly reliable & ownable.** Easy container deploy, easy backup, easy export. No lock‑in.

---

## 7. Scope & phased delivery

A phased plan that gets to value fast, then layers on intelligence. (Estimates are relative sizing, not commitments.)

### Phase 0 — Foundations
Containerised app skeleton, HTTPS via reverse proxy, household + user accounts, secure login, settings (locale/currency/financial year), backup/restore.

### Phase 1 — MVP: "See where the money goes" 🎯
The smallest thing that delivers the core promise.
- Account setup (manual: name, type, institution).
- **File import** (CSV with guided column‑mapping; OFX/QFX native) with de‑duplication.
- **Auto‑categorisation** (rules + starter ML model) with one‑click correction and "learn from this."
- Default **AU category taxonomy** ([Appendix A](#appendix-a--default-category-taxonomy)).
- **Overview dashboard**: income vs. expense, net cashflow, spend by category, trend over time.
- **Drill‑down**: category → subcategory → transactions; search & filter.
- Transfers detection (internal account‑to‑account excluded from spend).
- Manual transactions (cash) + transaction notes/tags.
- Mobile‑responsive layout.

**Exit criteria:** a new user imports a real CBA/Westpac/NAB/ANZ export and gets an accurate, useful overview in under 5 minutes, with ≥85% of transactions auto‑categorised.

### Phase 2 — "Understand & improve"
- **Budgets** per category (monthly/fortnightly/annual; optional rollover/envelope).
- **Insights & recommendations engine** (rule‑based): top movers, creeping categories, savings opportunities, fee/interest detection, duplicate subscriptions.
- **Recurring & subscription detection**; **upcoming bills** view.
- **Net worth** (assets/liabilities incl. mortgage/offset) over time.
- **Savings goals** with progress + suggested contributions.
- **ABS peer benchmarking**: "Compared to similar AU households you spend X% more on Y."
- Richer visuals (Sankey of money flow, heatmaps, calendar).

### Phase 3 — "Advice & foresight"
- **AI advisor** (BYO cloud key + local Ollama) with structured access to the user's insights/data and privacy controls.
- **Cashflow forecasting** (project balances forward; "what‑if" scenarios).
- **Anomaly/alert notifications** (email/push/PWA).
- Scheduled **email digests** (weekly/monthly summary).
- Annual **financial‑year report** export (PDF/CSV) for the accountant.

### Phase 4 — "Automate & extend" (later)
- **Open Banking / CDR feeds** via an accredited aggregator (e.g., Basiq) so transactions flow in automatically — architected for since v1.
- Receipt capture / OCR; rules marketplace; public REST API; optional **anonymised community benchmarks**.

---

## 8. Functional requirements

> Notation: **R#** requirement; **(MVP)** = Phase 1; **(P2/P3/P4)** = later phase.

### 8.1 Onboarding & setup
- **R1 (MVP)** First‑run wizard: create household, set country=Australia, currency=AUD, locale=en‑AU, state/territory, financial‑year start (default 1 July), household size & composition (adults/children) and income band (used for benchmarking; optional, skippable).
- **R2 (MVP)** Add accounts manually (name, institution, type: everyday/savings/credit card/home loan/offset/personal loan/cash/investment, opening balance).
- **R3 (MVP)** Invite/create additional family‑member logins with roles: **Owner** (full control), **Adult** (full data, no destructive admin), **Viewer** (read‑only). Optional **kid/teen view** later.

### 8.2 Data ingestion (file import) **[Decision: files in v1]**
- **R4 (MVP)** Upload one or more files per account: **CSV, OFX, QFX**; **QIF** best‑effort. (Format matrix in [Appendix B](#appendix-b--bank-file-format-support).)
- **R5 (MVP)** CSV **guided column‑mapping**: detect/choose date, description, amount (single signed column *or* separate debit/credit), balance; AU date formats (DD/MM/YYYY) default; save a reusable **mapping profile per institution** so future imports are one‑click.
- **R6 (MVP)** **De‑duplication** on re‑import (hash of date+amount+description+account; configurable window) so overlapping exports don't double‑count.
- **R7 (MVP)** Import preview: show what will be added/skipped before committing; per‑batch undo.
- **R8 (MVP)** Robust parsing of messy AU bank descriptions (trailing reference numbers, card suffixes, `EFTPOS`, `OSKO`, `PayID`, BPAY, direct‑debit strings).
- **R9 (P4)** Automated **CDR/Open Banking** account linking via aggregator; same downstream pipeline as files.

### 8.3 Categorisation
- **R10 (MVP)** **Merchant normalisation**: clean raw descriptions to a tidy merchant name (e.g., `WOOLWORTHS 1234 SYDNEY` → "Woolworths").
- **R11 (MVP)** **Rule engine**: user/system rules (contains / starts‑with / regex / merchant match → category, with priority), applied on import and re‑runnable. (Firefly‑style.)
- **R12 (MVP)** **ML categoriser**: starter model seeded with common AU merchants; **learns from user corrections** over time (Copilot‑style). Show a confidence indicator; route low‑confidence to a quick "review" queue.
- **R13 (MVP)** One‑click recategorise with "apply to all like this / make a rule."
- **R14 (MVP)** **Transfer detection**: match equal/opposite movements between the household's own accounts; exclude from income/expense.
- **R15 (MVP)** **Split transactions** across categories (e.g., a supermarket shop that's groceries + alcohol).
- **R16 (P2)** **Recurring detection**: identify subscriptions/bills by cadence + stable merchant/amount; group them.

### 8.4 Dashboard & visuals (high level → detail)
- **R17 (MVP)** **Overview dashboard** with period selector (this month / pay cycle / FY / custom): total **income**, total **expenses**, **net cashflow**, **savings rate**, top categories, and trend sparkals.
- **R18 (MVP)** **Spend‑by‑category** breakdown (donut/bar) → click a category → subcategory → transaction list.
- **R19 (MVP)** **Trends over time** (stacked area/line) by category and total.
- **R20 (MVP)** **Transaction explorer**: fast search, filter (date/account/category/amount/merchant/tag), inline edit, bulk actions.
- **R21 (P2)** **Money‑flow Sankey** (income → categories → savings), **calendar heatmap** of daily spend, **merchant leaderboard**.
- **R22 (P2)** **Net‑worth** chart (assets − liabilities) over time.
- **R23 (MVP)** Every chart element is drill‑through and every figure has a visible definition/tooltip.

### 8.5 Budgets, goals & bills
- **R24 (P2)** Create **budgets** per category (monthly/fortnightly/annual), with progress bars, projected end‑of‑period, and over/under alerts; optional **rollover (envelope)** behaviour.
- **R25 (P2)** **Savings goals** (target amount + date, linked account), progress, and suggested per‑pay contribution.
- **R26 (P2)** **Upcoming bills / recurring** calendar; **reminders** ahead of due dates.
- **R27 (P3)** **Cashflow forecast**: project account balances forward from recurring items + budgets; simple **what‑if** (e.g., "cut dining 20%").

### 8.6 Insights, recommendations & benchmarking
- **R28 (P2)** **Insight feed**: ranked, plain‑English cards — *what changed*, *why it matters*, *suggested action*, with a link to the evidence. Examples:
  - "Dining out is up 34% vs your 3‑month average (+$180)."
  - "You're paying $312/mo across 14 subscriptions; 3 look unused."
  - "$47 in account fees and $120 in credit‑card interest last quarter — avoidable."
  - "Your power bill is seasonal; set aside $65/fortnight to smooth it."
- **R29 (P2)** **ABS peer benchmarking** **[Decision: ABS data]**: compare the household's category spend against comparable Australian households (by **household size/composition, income band, and state**) using the **ABS Household Expenditure Survey** (detailed category structure) refreshed against the **ABS Monthly Household Spending Indicator** (for currency/inflation). Present as "you vs. similar households," with clear methodology and caveats (HES detail is dated; treat as a guide, not a verdict). See [Appendix C](#appendix-c--abs-benchmarking-approach).
- **R30 (P2)** **Fee & interest finder**: detect bank fees, overdraft, interest, late fees, FX fees → quantify annualised cost.
- **R31 (P3)** Insights are also exposed to the AI advisor as structured context (§10).

### 8.7 Reporting & export
- **R32 (P2)** Export transactions/categories to **CSV/Excel**; **(P3)** **financial‑year PDF report** for the accountant.
- **R33 (MVP)** Full **data export** (open format) and **import/restore** — no lock‑in.
- **R34 (P3)** Scheduled **email digest** (weekly/monthly).

### 8.8 Notifications
- **R35 (P3)** Channels: in‑app, **email (SMTP)**, and **PWA/web push**. Events: budget thresholds, unusual spend, upcoming bills, large transactions, low projected balance. All opt‑in and quiet by default.

### 8.9 Administration
- **R36 (MVP)** **Backup/restore** (one‑click DB export + scheduled dumps), **audit log** of logins and destructive actions, settings for locale/security/AI/SMTP.

---

## 9. Australian localisation **[Decision]**
- **Language:** en‑AU spelling throughout (categorise, organise, favourite, etc.).
- **Currency:** AUD, format `$1,234.56`; thousands/decimal per en‑AU. Multi‑currency accounts are out of scope for v1 (single‑currency household assumed).
- **Dates:** DD/MM/YYYY display; importer defaults to AU date parsing.
- **Financial year:** default **1 July – 30 June**; period selectors and FY reports respect it.
- **Location:** state/territory (NSW/VIC/QLD/SA/WA/TAS/NT/ACT) captured for benchmarking and (later) public‑holiday/bill‑timing context.
- **Domain awareness:** recognise common AU constructs — **BPAY**, **PayID/Osko**, **EFTPOS**, **direct debit**, **BNPL** (Afterpay/Zip/Humm), **Medicare/private health**, **childcare/CCS**, **strata**, **rego**, **offset accounts**, super contributions, HECS/HELP — in merchant rules and the category taxonomy.
- **Banks:** first‑class CSV/OFX mapping profiles for CBA, Westpac, NAB, ANZ, ING, Macquarie, Bendigo, UP, Bankwest, Suncorp.
- **Timezone:** household‑level (default Australia/Sydney), per‑user override.

---

## 10. AI advisor **[Decision: BYO cloud key + local]**

A user can connect an LLM to get tailored, conversational advice grounded in their own data and Tally's insights.

### 10.1 Capabilities
- **R37 (P3)** **Provider‑agnostic** connection: Anthropic (Claude), OpenAI, Google Gemini, or **local via Ollama** (or any OpenAI‑compatible endpoint). User supplies endpoint/model/API key; keys stored encrypted.
- **R38 (P3)** **Ask‑your‑data chat**: "Where can we realistically save $400/month?", "Why was March so expensive?", "Are our utilities high for a family of four in QLD?" The model answers using structured tools.
- **R39 (P3)** **Tool/function calling** over a safe, read‑only data API: `get_summary(period)`, `spend_by_category(period)`, `compare_to_benchmark(category)`, `list_subscriptions()`, `list_recurring_bills()`, `get_goals()`, `find_fees(period)`, `forecast_balance(...)`. The model requests aggregates rather than dumping raw ledgers.
- **R40 (P3)** **Proactive narratives**: optionally let the model turn the insight feed and FY report into a plain‑English summary with prioritised actions.

### 10.2 Privacy modes (the heart of "your data, your choice")
- **R41 (P3)** Per the user's selection, one of:
  1. **Local‑only** — all prompts/data go to a local model; nothing leaves the network (default if Ollama configured).
  2. **Aggregates‑only to cloud** — only category totals, benchmarks, and insights (no raw transactions, no account numbers, names masked) are sent to the chosen cloud provider. **Default for cloud.**
  3. **Full detail to cloud** — explicit opt‑in to include transaction‑level data.
- **R42 (P3)** Always show, before sending, **what** will be shared and with **whom**; redact account numbers/PII by default; per‑conversation data‑scope control; full local audit of AI calls.
- **R43 (P3)** **Guardrails & disclaimer:** responses are framed as **general information, not personal financial advice**; a persistent disclaimer and a system prompt that avoids product‑specific recommendations (see [§17](#17-risks--mitigations)).

---

## 11. Non‑functional requirements
- **NFR1 — HTTPS only [Decision]:** all traffic over TLS; HTTP redirects to HTTPS; HSTS. Automatic certificate management (Let's Encrypt when internet‑exposed; self‑signed/local CA for LAN‑only). See §12.
- **NFR2 — Containerised:** ships as Docker images with a one‑command **`docker compose up`**; configuration via env/`.env`; runs comfortably on a NAS/mini‑PC/Raspberry Pi‑class host.
- **NFR3 — Performance:** overview loads < 1.5 s for ~5 years of transactions (~50–100k rows); import of a 12‑month file < 10 s; UI interactions < 200 ms.
- **NFR4 — Reliability & data safety:** transactional imports (all‑or‑nothing per batch); automated, restorable backups; no data loss on upgrade (versioned DB migrations).
- **NFR5 — Privacy:** no telemetry/phone‑home by default; outbound calls only to user‑configured services (AI provider, later CDR aggregator, SMTP).
- **NFR6 — Security:** see [§15](#15-security--privacy).
- **NFR7 — Accessibility:** WCAG 2.1 AA target; keyboard‑navigable; colour‑blind‑safe chart palettes; respects reduced‑motion.
- **NFR8 — Internationalisation‑ready:** en‑AU first, but strings externalised and locale/currency pluggable for future markets.
- **NFR9 — Maintainability:** documented REST API, automated tests, seedable demo dataset for evaluation.
- **NFR10 — Resource footprint:** sensible defaults to run on ~2 vCPU / 2–4 GB RAM (excluding any local LLM, which is heavier and optional).

---

## 12. Proposed technical architecture

> Proposal for discussion — open to your stack preferences ([§18](#18-open-questions)).

### 12.1 Components (single‑host Docker Compose)
```
                    ┌──────────────────────────────────────────────┐
   Browser  ─HTTPS─▶│  Reverse proxy (Caddy)                        │
   (PWA)            │  • automatic TLS / HTTP→HTTPS / HSTS          │
                    └───────────────┬───────────────┬──────────────┘
                                    │               │
                          static web│           /api│
                    ┌───────────────▼──┐   ┌────────▼─────────────┐
                    │ Web app (React/  │   │ API (FastAPI/Python) │
                    │ TypeScript, PWA) │   │ • import & parsing   │
                    └──────────────────┘   │ • categorisation     │
                                           │ • insights/benchmark │
                                           │ • AI orchestration   │
                                           └───┬───────────┬──────┘
                                               │           │
                                    ┌──────────▼──┐   ┌────▼─────────────┐
                                    │ PostgreSQL  │   │ Worker/scheduler │
                                    │ (encrypted  │   │ (jobs, digests,  │
                                    │  at rest)   │   │ recurring detect)│
                                    └─────────────┘   └──────────────────┘
   Optional, user‑provided:  ▢ Ollama (local LLM)   ▢ Cloud LLM (BYO key)   ▢ SMTP   ▢ CDR aggregator (P4)
```

### 12.2 Stack proposal & rationale
| Layer | Proposal | Why |
|---|---|---|
| **Reverse proxy / TLS** | **Caddy** | Automatic HTTPS (Let's Encrypt or internal CA) with near‑zero config — directly satisfies "HTTPS only," LAN‑now/internet‑later. |
| **Frontend** | **React + TypeScript**, Vite, a component kit (shadcn/ui or Mantine), **PWA** | Rich interactive visuals, installable, responsive, large ecosystem. |
| **Charts** | **ECharts** (or Recharts/visx) | Handles Sankey, heatmaps, large series, drill‑through. |
| **Backend** | **Python + FastAPI** | Strong for file parsing (pandas), ML categorisation (scikit‑learn), and clean LLM orchestration; great typing/OpenAPI. |
| **DB** | **PostgreSQL** | Reliable, great for analytical queries; mature backup tooling. |
| **Jobs** | Lightweight scheduler/worker (APScheduler or RQ/Celery) | Recurring detection, digests, scheduled backups. |
| **Categorisation** | Rules engine + scikit‑learn classifier (incremental, learns from corrections) | Accuracy without cloud dependency. |
| **AI orchestration** | Provider adapter (Anthropic/OpenAI/Gemini/OpenAI‑compatible/Ollama) + tool/function calling over a read‑only insights API | One interface, many models; privacy modes enforced server‑side. |
| **Auth** | App‑native accounts, Argon2id hashing, session or JWT, optional **TOTP 2FA** | Simple, self‑contained, no external IdP required. |
| **Packaging** | Docker images + `docker compose` (+ optional Ollama profile) | One‑command deploy; clean upgrades. |

> **Alternative considered:** a single‑language TypeScript stack (Node/NestJS) to simplify contribution. Python is proposed because data wrangling + ML + LLM tooling are central. Happy to switch — flag in §18.

### 12.3 Deployment & HTTPS
- **LAN‑only:** Caddy issues a certificate from an internal CA (or self‑signed) for a `.local`/chosen hostname; documented trust step. HTTP always redirects to HTTPS.
- **Internet‑exposed (later):** point a domain at the host; Caddy auto‑provisions Let's Encrypt certs and renews them. Recommend pairing with 2FA and (optionally) a VPN/Cloudflare Tunnel rather than raw port‑forwarding.
- **Config** via `.env` (hostname, TLS mode, DB creds, SMTP, AI defaults). Secrets never baked into images.

---

## 13. Data model (high level)

Core entities (indicative, not final schema):

- **Household** — settings (currency, locale, state, FY start, timezone), composition (adults, children), income band.
- **User** — belongs to Household; role (owner/adult/viewer); auth (password hash, TOTP); preferences.
- **Account** — name, institution, type, currency, opening/current balance, owning user (optional).
- **ImportBatch** — source file, format, mapping profile, status, counts, timestamps (enables undo/audit).
- **Transaction** — account, date, signed amount, raw description, normalised merchant, category, subcategory, tags, notes, `is_transfer` + transfer pair, `is_recurring`, confidence, dedup hash, batch ref.
- **Category** — name, parent, type (income/expense/transfer/savings), ABS mapping code, icon, colour, system/user flag.
- **CategorisationRule** — match type/pattern, target category, priority, source (user/system).
- **Budget** — category, period, amount, rollover flag.
- **Goal** — name, target amount/date, linked account, progress.
- **Recurring/Subscription** — merchant, cadence, est. amount, next due, category, status (active/unused?).
- **Benchmark (reference data)** — dimensions (household size/composition, income band, state), category, period amount, source/vintage.
- **Insight** — type, severity, title, body, supporting data, period, created/dismissed.
- **AIConfig** — provider, endpoint, model, encrypted key, privacy mode. **AIConversation/Message** — chat history + data‑scope used.
- **AuditLog** — actor, action, entity, timestamp, IP.

---

## 14. UX & visual design direction
- **Three‑altitude navigation:** **Overview** (one screen, "are we okay?") → **Category/Trends** (mid‑level) → **Transactions** (detail). Always one click between altitudes.
- **Overview screen:** hero cards (income, expenses, net, savings rate) with up/down vs. previous period; a money‑flow visual; top categories; the insight feed; "this month vs. similar households" teaser.
- **Visual language:** clean, calm, high‑contrast, mobile‑first; colour‑blind‑safe palette; numbers always with context (Δ vs. prior period / vs. benchmark). Charts are interactive and drill‑through, never dead‑ends.
- **Low‑effort loops:** import → review only the low‑confidence items → done. A persistent "needs your attention" count (uncategorised, low‑confidence, possible transfers) keeps maintenance tiny.
- **Trust cues:** show categorisation confidence; make corrections one tap; show benchmarking methodology on hover; show exactly what the AI will see before it sees it.
- **Empty/first‑run states** that teach: sample/demo dataset toggle so a family can explore before importing real data.

---

## 15. Security & privacy
- **Transport:** HTTPS‑only, HSTS, modern TLS (NFR1).
- **At rest:** encrypted database volume; **field‑level encryption** for secrets (AI keys, SMTP creds) and sensitive PII.
- **AuthN/Z:** Argon2id password hashing, optional **TOTP 2FA**, session timeout, lockout/back‑off on brute force, role‑based access (owner/adult/viewer).
- **App hardening:** CSRF protection, strict CSP, security headers, input validation, parameterised queries, rate limiting, dependency scanning in CI.
- **Data minimisation & control:** no analytics/telemetry by default; outbound only to user‑configured endpoints; **AI privacy modes** with pre‑send disclosure and redaction (§10.2).
- **Auditability:** login + destructive‑action audit log; AI‑call log (model, data scope, timestamp).
- **Backups:** scheduled, restorable, and exportable; documented restore drill.
- **Secrets:** via env/secret store, never in images or VCS.
- **Threat note (internet exposure):** recommend 2FA + reverse‑proxy hardening + optional VPN/tunnel; document a safe exposure checklist.

---

## 16. Success metrics
Because this is a self‑hosted family tool (not a growth product), success = realised value:

| Goal | Metric | Target |
|---|---|---|
| Effortless setup | Time from first upload → first useful overview | < 5 min |
| Categorisation quality | % auto‑categorised correctly on first import | ≥ 85% (→ ≥ 95% after corrections) |
| Low maintenance | Median weekly time spent maintaining data | < 10 min |
| Insightfulness | % of users who act on ≥1 recommendation in first month | ≥ 50% |
| Stickiness | Households with ≥1 login/week after 4 weeks | ≥ 60% |
| Trust | Imports completed without a support workaround | ≥ 90% of supported banks |
| Reliability | Failed/duplicated imports | < 1% |

(For a self‑hosted build, these are validated with a small pilot group / dogfooding rather than fleet analytics.)

---

## 17. Risks & mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| **Regulatory: personal financial advice (AFSL)** | Giving tailored "what to do with your money" could stray into licensed *personal financial product advice* in Australia. | Frame outputs as **general information / factual budgeting**; persistent disclaimer; AI system prompt avoids product‑specific recommendations; no product referrals. Seek legal review before any public/commercial release. |
| **Benchmark data is dated/coarse** | ABS HES detail is from 2015‑16; could mislead. | Refresh against ABS **Monthly Household Spending Indicator**; show methodology + "guide, not verdict" caveats; band comparisons; revisit when ABS updates HES. |
| **Categorisation errors erode trust** | Wrong numbers = abandonment. | Confidence scores, easy corrections, learn‑from‑corrections, review queue, transparent rules. |
| **Bank CSV format drift** | Imports break silently. | Per‑institution mapping profiles, validation + preview, dedup, graceful errors, community‑shareable profiles. |
| **Privacy breach (esp. if internet‑exposed)** | Highly sensitive data. | HTTPS‑only, 2FA, encryption at rest, no telemetry, exposure checklist, AI privacy modes. |
| **AI leaks data to cloud** | Sensitive data to third party. | Default to local or aggregates‑only; pre‑send disclosure; redaction; full local model option. |
| **Scope creep / complexity** | Slips the "really easy" promise. | Phase gating; ruthless "effort‑to‑value" test; sensible defaults over options. |
| **CDR ingestion cost/complexity (P4)** | Accreditation + aggregator fees. | Defer; abstract ingestion so files and feeds share a pipeline; use accredited intermediary (e.g., Basiq) when ready. |

---

## 18. Open questions
1. **Tech stack:** OK with **Python/FastAPI + React** (and **Caddy** for TLS), or do you prefer an all‑TypeScript stack / another DB?
2. **Budgeting philosophy:** flexible category tracking (default), or also offer **envelope/zero‑based** budgeting (YNAB/Goodbudget style) as an option in P2?
3. **Net worth depth:** simple manual asset/liability tracking, or do you want property value / super / investment tracking emphasised?
4. **Pay‑cycle:** should periods centre on a **pay cycle** (e.g., fortnightly) as much as calendar months? What's your household's cycle?
5. **Notifications:** is **email (SMTP)** enough for v1‑of‑alerts, or is PWA push important early?
6. **Local LLM hardware:** do you have a machine that can run Ollama (GPU/RAM), or should cloud BYO‑key be the practical default initially?
7. **Name:** keep **"Tally,"** or pick from [Appendix E](#appendix-e--name-ideas)?
8. **Pilot scope:** is this just your household, or do you want a couple of friendly families to pilot it (affects how we generalise bank formats/benchmarks)?
9. **Existing data:** any historical data (old spreadsheets/exports) you want migrated in at launch?

---

## 19. Appendices

### Appendix A — Default category taxonomy
A deliberately small, home‑budget‑meaningful set (users can edit). Parent → examples of subcategories.

**Income:** Salary/Wages · Government benefits (incl. CCS, FTB) · Investment/Interest · Refunds · Other income
**Housing:** Mortgage · Rent · Offset/Extra repayments · Strata/Body corporate · Council rates · Home insurance · Repairs & maintenance
**Utilities:** Electricity · Gas · Water · Internet · Mobile phone
**Groceries:** Supermarkets (Woolworths/Coles/Aldi/IGA) · Butcher/Greengrocer
**Transport:** Fuel · Public transport (Opal/Myki/etc.) · Tolls · Parking · Rego & CTP · Car insurance · Servicing/Repairs · Rideshare/Taxi
**Health:** Private health insurance · Medicare/GP/Specialist · Pharmacy · Dental · Optical
**Children & Education:** Childcare · School fees · School supplies · Activities/Sport · Tutoring
**Food & Drink (out):** Restaurants/Cafés · Takeaway · Alcohol/Bars · Coffee
**Shopping:** Clothing · Homewares · Electronics · Gifts · General merchandise
**Subscriptions & Memberships:** Streaming · Software/Apps · Gym · News/Media · Cloud storage
**Insurance & Finance:** Life/Income protection · Bank fees · Interest charges · BNPL (Afterpay/Zip) · Loan repayments
**Personal care:** Hair/Beauty · Cosmetics
**Entertainment & Recreation:** Events/Movies · Hobbies · Sport · Books/Games
**Travel & Holidays:** Flights · Accommodation · Holiday spending
**Pets:** Food · Vet · Other
**Health & Wellbeing/Donations:** Charity/Donations · Gifts given
**Savings & Investments:** Transfers to savings · Super contributions (voluntary) · Investments
**Taxes & Government:** ATO/Tax · Fines/Penalties · HECS/HELP
**Transfers (excluded from spend):** Internal transfers · Credit‑card payments
**Uncategorised / Needs review**

### Appendix B — Bank file format support
| Format | v1 support | Notes |
|---|---|---|
| **OFX / QFX** | ✅ Native parse | Structured; dates/amounts/payees well‑defined; preferred where banks offer it. |
| **CSV** | ✅ Guided mapping | Non‑standard layouts; per‑institution mapping profiles; AU DD/MM/YYYY default; single signed amount *or* debit/credit columns. |
| **QIF** | ◻️ Best‑effort | Legacy; many AU banks have retired it; supported where feasible. |
| **PDF statements** | ❌ (later) | Possible via OCR in a later phase; unreliable, deprioritised. |
| **CDR/Open Banking feed** | ❌ → **P4** | Automated, via accredited aggregator; same downstream pipeline. |

Target first‑class CSV/OFX mapping profiles: **CBA, Westpac, NAB, ANZ, ING, Macquarie, Bendigo, UP, Bankwest, Suncorp**.

### Appendix C — ABS benchmarking approach
- **Category structure & cross‑tabs:** ABS **Household Expenditure Survey (HES)** provides average weekly spend across 600+ items, cross‑classified by **income**, **household composition**, **net worth**, and **broad geography** — the backbone for "similar households" comparisons. (Latest detailed HES vintage is 2015‑16.)
- **Currency/recency adjustment:** scale/contextualise with the ABS **Monthly Household Spending Indicator** (category‑level, updated monthly) and CPI so figures aren't a decade stale.
- **Mapping:** maintain a mapping from Tally's category taxonomy ([Appendix A](#appendix-a--default-category-taxonomy)) to ABS categories; pick the comparison cohort from the household's size/composition + income band + state.
- **Presentation:** "You vs. similar AU households" per category, with a confidence/caveat note. **Always a guide, never a verdict.**
- **Later:** optional **anonymised community benchmarks** once there's a user base and explicit consent (deferred per [Decision]).

### Appendix D — Indicative MVP backlog (epics)
1. Container + Caddy HTTPS + Compose scaffold.
2. Household/user/auth (+ roles, 2FA later).
3. Accounts CRUD.
4. Import pipeline: OFX parser, CSV mapper, dedup, preview/undo.
5. Merchant normalisation + rules engine + ML categoriser + review queue.
6. Transfer detection + splits + manual transactions.
7. Overview dashboard + category drill‑down + transaction explorer.
8. Settings (locale/currency/FY) + backup/restore + data export.
9. Seed/demo dataset + onboarding wizard.

### Appendix E — Name ideas
Tally (working) · Nestworth · Cobber · Kanga · Ledgerly · Pennan · Households · Tucker · Brolly · Nestegg · Moolah · Worth.

### Appendix F — Sources
Competitive & domain research consulted for this PRD:

- PocketSmith — Australia personal finance software & alternatives: https://www.pocketsmith.com/global-personal-finance-software/australia/ · https://www.pocketsmith.com/frollo-alternative/ · https://www.pocketsmith.com/wemoney-app-alternative/
- Finder — Best Budgeting Apps in Australia: https://www.finder.com.au/budgeting/top-apps-save-money
- Man of Many — Best budgeting apps Australia: https://manofmany.com/culture/advice/best-budgeting-apps-australia
- Monarch / Copilot / YNAB comparisons: https://era.app/articles/era-vs-monarch-vs-copilot-vs-ynab/ · https://www.fool.com/money/personal-finance/monarch-money-vs-ynab/ · https://www.monarch.com/
- NerdWallet — Best budget apps (features users want): https://www.nerdwallet.com/finance/learn/best-budget-apps
- Firefly III (self‑hosted): https://github.com/firefly-iii/firefly-iii · https://docs.firefly-iii.org/explanation/firefly-iii/about/introduction/
- Actual Budget / Maybe / self‑hosted roundup: https://selfhosting.sh/best/personal-finance/
- ABS Household Expenditure Survey: https://www.abs.gov.au/statistics/economy/finance/household-expenditure-survey-australia-summary-results/latest-release
- ABS Monthly Household Spending Indicator: https://www.abs.gov.au/statistics/economy/finance/monthly-household-spending-indicator/latest-release
- Finder — Australian household spending statistics: https://www.finder.com.au/insights/australian-household-spending-statistics
- Bank file formats (CSV/OFX/QIF) & AU exports: https://learn.pocketsmith.com/article/145-preparing-your-bank-files-accepted-file-types · https://www.macquarie.com.au/help/business/manage-your-accounts/statements-and-transactions/export-transactions-as-csv-or-qif-files.html · https://www.easybankconvert.com/articles/qif-ofx-csv-comparison
- Australia CDR / Open Banking & Basiq: https://www.accc.gov.au/by-industry/banking-and-finance/the-consumer-data-right · https://www.cdr.gov.au/for-providers/become-accredited-data-recipient · https://api.basiq.io/docs/cdr-compliance

---

_End of draft v0.1 — please review and refine. Comments and changes welcome inline._
