"""AI advisor (PRD §10): a provider-agnostic "ask your data" chat. We build a
privacy-scoped snapshot of the household's aggregates (and, only in full/local
modes, recent transactions) and send it as system context to the chosen model.
No tool-calling yet — the model answers from the snapshot. BYO key, encrypted."""

from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy.orm import Session

from .. import models
from . import crypto
from . import forecast as forecast_service
from . import recurring as recurring_service
from .dashboard import _spendable_leaves, category_breakdown, summary
from .periods import fy_bounds

PROVIDERS = {"none", "anthropic", "openai"}
PRIVACY_MODES = {"local_only", "aggregates", "full"}

SYSTEM_PROMPT = (
    "You are Saiva's financial information assistant for an Australian household. "
    "Answer using only the data provided below. Be concise and practical. "
    "Give general information, not personal financial advice, and never recommend "
    "specific financial products. If the data doesn't cover the question, say so."
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


def build_context(
    db: Session, household: models.Household, privacy_mode: str, today: dt.date | None = None
) -> str:
    today = today or dt.date.today()
    start, end = fy_bounds(household, today)
    s = summary(db, household, "custom", start, end)
    cb = category_breakdown(db, household, "custom", start, end)
    series = recurring_service.detect(db, household.id, today=today)
    committed = sum(x.monthly_amount_cents for x in series if x.active and x.direction == "expense")
    income = sum(x.monthly_amount_cents for x in series if x.active and x.direction == "income")
    fc = forecast_service.forecast(db, household.id, days=60, today=today)

    lines = [
        f"Household: {household.name} — {household.adults} adults, "
        f"{household.children} children, {household.state or 'AU'}.",
        f"FY{end.year} ({start:%d %b %Y}–{end:%d %b %Y}): income {_m(s.income_cents)}, "
        f"expenses {_m(s.expense_cents)}, net {_m(s.net_cents)}, "
        f"savings rate {s.savings_rate * 100:.0f}%.",
        "Top spending categories this FY:",
    ]
    lines += [
        f"- {it.category_name}: {_m(it.amount_cents)} ({it.pct * 100:.0f}%)"
        for it in cb.items[:10]
    ]
    lines.append(
        f"Recurring: committed {_m(committed)}/mo of expenses; recurring income {_m(income)}/mo."
    )
    lines.append(
        f"Forecast (60d): balance now {_m(fc.starting_balance_cents)}, "
        f"projected {_m(fc.end_balance_cents)}, low {_m(fc.low_balance_cents)} "
        f"around {fc.low_balance_date:%d %b %Y}."
    )

    if privacy_mode in ("full", "local_only"):
        recent = sorted(
            _spendable_leaves(db, household.id, today - dt.timedelta(days=60), today),
            key=lambda t: t.txn_date,
            reverse=True,
        )[:25]
        if recent:
            lines.append("Recent transactions:")
            lines += [
                f"- {t.txn_date:%d %b} {t.merchant or t.raw_description}: {_m(t.amount_cents)}"
                for t in recent
            ]
    return "\n".join(lines)


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
                "max_tokens": 1024,
                "system": system,
                "messages": messages,
            },
        )
        _raise_for_provider(resp)
        return str(resp.json()["content"][0]["text"])
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
            },
        )
        _raise_for_provider(resp)
        return str(resp.json()["choices"][0]["message"]["content"])
    raise NotConfiguredError("AI is not configured")


def chat(
    db: Session, household: models.Household, messages: list[dict[str, str]]
) -> str:
    ai = settings_for(db, household.id)
    if ai.provider not in ("anthropic", "openai"):
        raise NotConfiguredError("AI is not configured")
    context = build_context(db, household, ai.privacy_mode)
    system = f"{SYSTEM_PROMPT}\n\nData you may use:\n{context}"
    return _call_provider(ai, system, messages)


# Substrings that mark an OpenAI model as not chat-capable (kept out of the picker).
_OPENAI_NON_CHAT = (
    "embedding", "whisper", "tts", "dall-e", "moderation", "audio",
    "realtime", "image", "transcribe", "search", "davinci", "babbage",
)


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
    raise NotConfiguredError("AI is not configured")


def test_connection(ai: models.AiSettings) -> str:
    """A minimal round-trip that exercises the provider + key + model together —
    so a bad key (401) or an unknown model (400/404) both surface here."""
    return _call_provider(
        ai,
        "You are a connection test.",
        [{"role": "user", "content": "Reply with the single word: OK"}],
    )
