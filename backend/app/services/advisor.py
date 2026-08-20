"""AI advisor (PRD §10): a provider-agnostic "ask your data" chat. We build a
privacy-scoped snapshot of the household's aggregates (and, only in full/local
modes, recent transactions) and send it as system context to the chosen model.
No tool-calling yet — the model answers from the snapshot. BYO key, encrypted."""

from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..constants import UNCATEGORISED
from . import crypto, periods
from . import forecast as forecast_service
from . import recurring as recurring_service
from .budgets import list_budgets
from .dashboard import _spendable_leaves, category_breakdown, summary, trends
from .goals import list_goals
from .networth import get_net_worth
from .periods import fy_bounds

PROVIDERS = {"none", "anthropic", "openai", "gemini"}
PRIVACY_MODES = {"local_only", "aggregates", "full"}

SYSTEM_PROMPT = (
    "You are Saiva's financial information assistant for an Australian household. "
    "Answer using only the data provided below, and quote the figures from it rather "
    "than estimating. Be concise and practical. Give general information, not "
    "personal financial advice, and never recommend specific financial products.\n"
    "The data covers one period, stated below; if asked about another, say which "
    "period you are looking at and that they can change it with the period selector "
    "at the top of the app.\n"
    "If a question needs something you were not given, say plainly what is missing "
    "and — where the WHAT YOU CAN SEE note explains a privacy setting is the reason "
    "— tell them which setting to change. Never guess at values you cannot see."
)


class NotConfiguredError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    """The LLM provider returned an error; the message carries its own detail."""


def _raise_for_provider(resp: httpx.Response) -> None:
    """Raise ProviderError with the provider's own error message on a 4xx/5xx."""
    if resp.status_code < 400:
        return
    detail: str = ""
    try:
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            detail = str(data["error"].get("message") or data["error"])
        elif isinstance(data, dict) and data.get("error"):
            detail = str(data["error"])
    except ValueError:
        pass
    if not detail:
        detail = resp.text[:400] or f"HTTP {resp.status_code}"
    raise ProviderError(f"{resp.status_code} — {detail}")


def settings_for(db: Session, household_id: str) -> models.AiSettings:
    ai = db.get(models.AiSettings, household_id)
    if ai is None:
        ai = models.AiSettings(household_id=household_id)
        db.add(ai)
        db.commit()
        db.refresh(ai)
    return ai


def _m(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# The snapshot is meant to fit in roughly a page: enough to answer the common
# questions in one round trip, with detail fetched on demand rather than always sent.
MAX_CATEGORIES = 15
MAX_MERCHANTS = 10
MAX_UNCATEGORISED = 15
MAX_TRANSACTIONS = 20

# Told to the model so it can explain its own limits accurately. Without this it
# says only "the provided data does not include…", which leaves the user with no
# idea that a setting caused it or that they can change it.
CAPABILITIES = {
    "aggregates": (
        "You can see totals and category/merchant summaries, but NOT individual "
        "transactions or their descriptions — the household chose the 'Aggregates "
        "only' privacy mode. If a question needs transaction detail, say so and tell "
        "them they can switch to 'Full detail' in Settings > AI advisor."
    ),
    "full": "You can see summaries and individual transactions for this period.",
    "local_only": "You can see summaries and individual transactions for this period.",
}


def _coverage(db: Session, household_id: str) -> str:
    """What data exists overall, so the model knows the shape of the history even
    when the question is about one period."""
    first, last, count = db.execute(
        select(
            func.min(models.Transaction.txn_date),
            func.max(models.Transaction.txn_date),
            func.count(models.Transaction.id),
        ).where(models.Transaction.household_id == household_id)
    ).one()
    if first is None:
        return "No transactions have been imported yet."
    accounts = db.execute(
        select(func.count(models.Account.id)).where(
            models.Account.household_id == household_id
        )
    ).scalar_one()
    return (
        f"Records span {first:%d %b %Y} to {last:%d %b %Y} — {count:,} transactions "
        f"across {accounts} account(s)."
    )


def _merchant_totals(txns: list[models.Transaction]) -> list[tuple[str, int, int]]:
    """Spend per merchant, biggest first."""
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for t in txns:
        if t.amount_cents >= 0:
            continue
        name = t.merchant or t.raw_description or "(no description)"
        totals[name] = totals.get(name, 0) + -t.amount_cents
        counts[name] = counts.get(name, 0) + 1
    return sorted(((n, v, counts[n]) for n, v in totals.items()), key=lambda r: -r[1])


def _previous_window(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    span = end - start
    prev_end = start - dt.timedelta(days=1)
    return prev_end - span, prev_end


def _is_uncategorised(t: models.Transaction, names: dict[str, str]) -> bool:
    # A row can be uncategorised either by having no category at all or by sitting in
    # the seeded "Uncategorised" bucket; both mean the same thing to the user.
    return t.category_id is None or names.get(t.category_id) == UNCATEGORISED


def build_context(
    db: Session,
    household: models.Household,
    privacy_mode: str,
    window: periods.ResolvedPeriod | None = None,
) -> str:
    """A snapshot of the household's finances for the selected period.

    Follows the app's period picker rather than always describing the financial
    year, so asking about a past year answers for that year.
    """
    today = dt.date.today()
    if window is None:
        fy_start, fy_end = fy_bounds(household, today)
        window = periods.resolve(household, f"fy:{fy_start.year}", today=today)
    start, end, label = window.start, window.end, window.label

    names = {
        c.id: c.name
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household.id)
        )
        .scalars()
        .all()
    }
    txns = _spendable_leaves(db, household.id, start, end)
    s = summary(db, household, "custom", start, end)
    cb = category_breakdown(db, household, "custom", start, end)

    when = (
        "This period is in progress."
        if window.is_current
        else ("This period has ended." if end < today else "This period has not started.")
    )
    lines = [
        f"PERIOD: {label} ({start:%d %b %Y} to {end:%d %b %Y}). Today is {today:%d %b %Y}. {when}",
        f"DATA HELD: {_coverage(db, household.id)}",
        f"WHAT YOU CAN SEE: {CAPABILITIES.get(privacy_mode, CAPABILITIES['aggregates'])}",
        "",
        f"HOUSEHOLD: {household.name} — {household.adults} adults, "
        f"{household.children} children, {household.state or 'AU'}.",
        f"TOTALS FOR {label}: income {_m(s.income_cents)}, expenses {_m(s.expense_cents)}, "
        f"net {_m(s.net_cents)}, savings rate {s.savings_rate * 100:.0f}%.",
    ]

    prev_start, prev_end = _previous_window(start, end)
    prev = summary(db, household, "custom", prev_start, prev_end)
    if prev.expense_cents:
        change = (s.expense_cents - prev.expense_cents) / prev.expense_cents * 100
        lines.append(
            f"VERSUS THE PREVIOUS {(end - start).days + 1} DAYS "
            f"({prev_start:%d %b %Y}–{prev_end:%d %b %Y}): expenses {_m(prev.expense_cents)} "
            f"-> {_m(s.expense_cents)} ({change:+.0f}%)."
        )

    lines.append(f"\nSPENDING BY CATEGORY IN {label}:")
    lines += [
        f"- {it.category_name}: {_m(it.amount_cents)} ({it.pct * 100:.0f}%)"
        for it in cb.items[:MAX_CATEGORIES]
    ]
    rest = cb.items[MAX_CATEGORIES:]
    if rest:
        lines.append(
            f"- plus {len(rest)} smaller categories totalling "
            f"{_m(sum(it.amount_cents for it in rest))}"
        )

    merchants = _merchant_totals(txns)
    if merchants:
        if privacy_mode in ("full", "local_only"):
            lines.append(f"\nTOP MERCHANTS IN {label}:")
            lines += [
                f"- {name}: {_m(total)} across {count} transaction(s)"
                for name, total, count in merchants[:MAX_MERCHANTS]
            ]
        else:
            # A merchant name is the transaction description, tidied up — naming them
            # would leak exactly what "aggregates only" promises to withhold.
            lines.append(
                f"\nMERCHANTS IN {label}: {len(merchants)} distinct merchants. Their "
                "names are not available in this privacy mode."
            )

    uncat = [t for t in txns if _is_uncategorised(t, names) and t.amount_cents < 0]
    if uncat:
        total = sum(-t.amount_cents for t in uncat)
        lines.append(
            f"\nUNCATEGORISED IN {label}: {len(uncat)} transactions totalling {_m(total)}."
        )
        if privacy_mode in ("full", "local_only"):
            # Descriptions are raw transaction data, so only in the permissive modes.
            lines.append("Largest of them, with their descriptions:")
            biggest = sorted(uncat, key=lambda t: t.amount_cents)[:MAX_UNCATEGORISED]
            lines += [
                f"- {t.txn_date:%d %b %Y} {t.raw_description or t.merchant}: "
                f"{_m(t.amount_cents)}"
                for t in biggest
            ]
        else:
            lines.append(
                "Their descriptions are not available in this privacy mode."
            )

    points = trends(db, household, "custom", start, end).points
    if len(points) > 1:
        lines.append(f"\nMONTH BY MONTH IN {label}:")
        lines += [
            f"- {p.period_start:%b %Y}: income {_m(p.income_cents)}, "
            f"expenses {_m(p.expense_cents)}"
            for p in points
        ]

    budgets = [b for b in list_budgets(db, household, window.as_at) if b.status != "ok"]
    if budgets:
        lines.append("\nBUDGETS NEEDING ATTENTION:")
        lines += [
            f"- {b.category_name}: {_m(b.actual_cents)} of {_m(b.limit_cents)} "
            f"({b.pct_used * 100:.0f}%, {b.status}) for {b.period_label}"
            for b in budgets
        ]

    goals = list_goals(db, household, window.as_at)
    if goals:
        lines.append("\nSAVINGS GOALS:")
        lines += [
            f"- {g.name}: {_m(g.current_cents)} of {_m(g.target_cents)} "
            f"({g.pct_complete * 100:.0f}%)"
            for g in goals
        ]

    nw = get_net_worth(db, household.id, as_at=window.as_at)
    if nw.items:
        lines.append(
            f"\nNET WORTH: assets {_m(nw.assets_cents)}, liabilities "
            f"{_m(nw.liabilities_cents)}, net {_m(nw.net_cents)}."
        )

    series = recurring_service.detect(db, household.id, today=window.as_at)
    committed = sum(x.monthly_amount_cents for x in series if x.active and x.direction == "expense")
    income = sum(x.monthly_amount_cents for x in series if x.active and x.direction == "income")
    lines.append(
        f"\nRECURRING: committed {_m(committed)}/mo of expenses; "
        f"recurring income {_m(income)}/mo."
    )

    fc = forecast_service.forecast(db, household.id, days=60, today=window.as_at)
    lines.append(
        f"FORECAST (60 days from {window.as_at:%d %b %Y}): balance "
        f"{_m(fc.starting_balance_cents)}, projected {_m(fc.end_balance_cents)}, "
        f"low {_m(fc.low_balance_cents)} around {fc.low_balance_date:%d %b %Y}."
    )

    if privacy_mode in ("full", "local_only") and txns:
        spend = sorted((t for t in txns if t.amount_cents < 0), key=lambda t: t.amount_cents)
        if spend:
            lines.append(f"\nLARGEST TRANSACTIONS IN {label}:")
            lines += [
                f"- {t.txn_date:%d %b %Y} {t.raw_description or t.merchant}: {_m(t.amount_cents)}"
                for t in spend[:MAX_TRANSACTIONS]
            ]
        recent = sorted(txns, key=lambda t: t.txn_date, reverse=True)[:MAX_TRANSACTIONS]
        lines.append(f"\nMOST RECENT TRANSACTIONS IN {label}:")
        lines += [
            f"- {t.txn_date:%d %b %Y} {t.raw_description or t.merchant}: {_m(t.amount_cents)}"
            for t in recent
        ]

    return "\n".join(lines)


# Room for a full answer. The old 1024 was too small for the kind of question this
# is for — "list my uncategorised transactions and say what they look like" needs
# far more than that — and answers were being cut off mid-sentence.
MAX_OUTPUT_TOKENS = 4096
# Gemini 2.5 models reason before answering and charge that thinking to the *same*
# budget as the visible reply, so a large maxOutputTokens can still yield one
# sentence. Reserving a slice for thinking leaves the rest for the answer.
GEMINI_THINKING_BUDGET = 1024

TRUNCATED_NOTE = (
    "\n\n_[This answer was cut off because it reached the length limit. "
    "Ask a narrower question, or ask me to continue.]_"
)


def _note_if_truncated(text: str, stop_reason: str | None) -> str:
    """Say so when the model ran out of room.

    Every provider reports this differently and none of it used to be read, so a
    half-finished answer was indistinguishable from a complete one.
    """
    truncated = (stop_reason or "").lower() in {"max_tokens", "length", "maxtokens"}
    return f"{text}{TRUNCATED_NOTE}" if truncated else text


def _call_provider(ai: models.AiSettings, system: str, messages: list[dict[str, str]]) -> str:
    key = crypto.decrypt(ai.api_key_encrypted) if ai.api_key_encrypted else None
    if ai.provider == "anthropic":
        base = (ai.base_url or "https://api.anthropic.com").rstrip("/")
        resp = httpx.post(
            f"{base}/v1/messages",
            timeout=60,
            headers={
                "x-api-key": key or "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ai.model or "claude-haiku-4-5-20251001",
                "max_tokens": MAX_OUTPUT_TOKENS,
                "system": system,
                "messages": messages,
            },
        )
        _raise_for_provider(resp)
        data = resp.json()
        blocks = [b for b in data.get("content", []) if b.get("type") == "text"]
        if not blocks:
            raise ProviderError("The model returned no text (it may have hit the length limit)")
        return _note_if_truncated(str(blocks[0]["text"]), data.get("stop_reason"))
    if ai.provider == "openai":  # OpenAI-compatible (OpenAI, Ollama, gateways)
        base = (ai.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"content-type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        resp = httpx.post(
            f"{base}/chat/completions",
            timeout=60,
            headers=headers,
            json={
                "model": ai.model or "gpt-4o-mini",
                "messages": [{"role": "system", "content": system}, *messages],
                "max_tokens": MAX_OUTPUT_TOKENS,
            },
        )
        _raise_for_provider(resp)
        choice = resp.json()["choices"][0]
        return _note_if_truncated(
            str(choice["message"]["content"] or ""), choice.get("finish_reason")
        )
    if ai.provider == "gemini":  # Google Generative Language API
        base = (ai.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        model = ai.model or "gemini-2.5-flash"
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
        ]
        resp = httpx.post(
            f"{base}/v1beta/models/{model}:generateContent",
            timeout=60,
            headers={"x-goog-api-key": key or "", "content-type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": MAX_OUTPUT_TOKENS,
                    "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET},
                },
            },
        )
        _raise_for_provider(resp)
        candidates = resp.json().get("candidates") or []
        if not candidates:
            raise ProviderError("Gemini returned no answer (the prompt may have been blocked)")
        first = candidates[0]
        # A 2.5 model that spends its whole budget thinking returns a candidate with
        # no parts at all; that used to raise a KeyError and surface as a 500.
        parts = [p for p in (first.get("content") or {}).get("parts", []) if p.get("text")]
        if not parts:
            raise ProviderError(
                "The model used its whole response budget before answering "
                f"(finish reason: {first.get('finishReason') or 'unknown'}). Try a "
                "narrower question."
            )
        return _note_if_truncated(str(parts[0]["text"]), first.get("finishReason"))
    raise NotConfiguredError("AI is not configured")


def chat(
    db: Session,
    household: models.Household,
    messages: list[dict[str, str]],
    window: periods.ResolvedPeriod | None = None,
) -> str:
    """`window` follows the app's period picker, so a question asked while viewing a
    past financial year is answered from that year's figures rather than today's."""
    ai = settings_for(db, household.id)
    if ai.provider not in ("anthropic", "openai", "gemini"):
        raise NotConfiguredError("AI is not configured")
    context = build_context(db, household, ai.privacy_mode, window)
    system = f"{SYSTEM_PROMPT}\n\nData you may use:\n{context}"
    return _call_provider(ai, system, messages)


# Substrings that mark an OpenAI model as not chat-capable (kept out of the picker).
_OPENAI_NON_CHAT = (
    "embedding", "whisper", "tts", "dall-e", "moderation", "audio",
    "realtime", "image", "transcribe", "search", "davinci", "babbage",
)

# A curated, current baseline of chat-capable models per provider, offered in the
# picker even before a key is saved. When a key is present the provider's own live
# /models list is authoritative and merged on top of this; it is only a starting
# point, and "Custom…" always lets the user type any model id. Most-capable first.
# Keep these current — deprecated ids here become broken suggestions.
CURATED_MODELS: dict[str, list[dict[str, str]]] = {
    "anthropic": [
        {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-4o", "label": "GPT-4o"},
        {"id": "gpt-4o-mini", "label": "GPT-4o mini"},
        {"id": "gpt-4.1", "label": "GPT-4.1"},
        {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini"},
    ],
    "gemini": [
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
        {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite"},
    ],
}


def curated_models(provider: str, base_url: str | None = None) -> list[dict[str, str]]:
    """Known-good models to offer for a provider even before a key is saved. For
    OpenAI-compatible providers this applies only to OpenAI's own endpoint — a
    custom host (e.g. Ollama, a gateway) serves its own catalogue, so we leave the
    list to the live fetch there instead of suggesting models it may not have."""
    if provider == "openai" and base_url and "api.openai.com" not in base_url:
        return []
    return [dict(m) for m in CURATED_MODELS.get(provider, [])]


def list_models(ai: models.AiSettings) -> list[dict[str, str]]:
    """Fetch the models the configured provider/key can use, as {id, label}."""
    key = crypto.decrypt(ai.api_key_encrypted) if ai.api_key_encrypted else None
    if ai.provider == "anthropic":
        base = (ai.base_url or "https://api.anthropic.com").rstrip("/")
        resp = httpx.get(
            f"{base}/v1/models",
            timeout=30,
            params={"limit": 100},
            headers={"x-api-key": key or "", "anthropic-version": "2023-06-01"},
        )
        _raise_for_provider(resp)
        return [
            {"id": str(m["id"]), "label": str(m.get("display_name") or m["id"])}
            for m in resp.json().get("data", [])
            if m.get("id")
        ]
    if ai.provider == "openai":  # OpenAI-compatible (OpenAI, Ollama, gateways)
        base = (ai.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        resp = httpx.get(f"{base}/models", timeout=30, headers=headers)
        _raise_for_provider(resp)
        ids = sorted(str(m["id"]) for m in resp.json().get("data", []) if m.get("id"))
        return [
            {"id": i, "label": i}
            for i in ids
            if not any(skip in i.lower() for skip in _OPENAI_NON_CHAT)
        ]
    if ai.provider == "gemini":
        base = (ai.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        resp = httpx.get(f"{base}/v1beta/models", timeout=30, headers={"x-goog-api-key": key or ""})
        _raise_for_provider(resp)
        out: list[dict[str, str]] = []
        for m in resp.json().get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            name = str(m.get("name", ""))
            model_id = name.split("/", 1)[1] if name.startswith("models/") else name
            if model_id:
                out.append({"id": model_id, "label": str(m.get("displayName") or model_id)})
        return out
    raise NotConfiguredError("AI is not configured")


def available_models(ai: models.AiSettings) -> list[dict[str, str]]:
    """Models to offer in the picker: a curated, current per-provider baseline plus
    the provider's own live list when a key is set (deduped, curated first, curated
    labels kept). Never raises for a missing/invalid key — the baseline still shows
    and key problems surface via Test connection instead of an empty dropdown."""
    out = curated_models(ai.provider, ai.base_url)
    seen = {m["id"] for m in out}
    # Only hit the network when it can plausibly succeed: key-based providers need a
    # key; OpenAI-compatible hosts (Ollama/gateways) often don't.
    if ai.api_key_encrypted or ai.provider == "openai":
        try:
            live = list_models(ai)
        except (ProviderError, httpx.HTTPError):
            live = []
        for m in live:
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
    return out


def test_connection(ai: models.AiSettings) -> str:
    """A minimal round-trip that exercises the provider + key + model together —
    so a bad key (401) or an unknown model (400/404) both surface here."""
    return _call_provider(
        ai,
        "You are a connection test.",
        [{"role": "user", "content": "Reply with the single word: OK"}],
    )
