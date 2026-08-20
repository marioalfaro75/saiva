"""The lookups the advisor can call while answering.

A fixed snapshot can only answer what it was built to answer; these cover the
questions that made the advisor look broken — "is there a transaction mentioning
Helen", "what are my uncategorised transactions for", "who do I spend most with".
"""

from __future__ import annotations

import datetime as dt

import pytest
from conftest import create_account
from fastapi.testclient import TestClient

from app import models
from app.db import SessionLocal
from app.services import advisor, advisor_tools
from app.services.periods import resolve


def _household(db) -> models.Household:
    return db.query(models.Household).one()


def _seed(client: TestClient) -> None:
    everyday = create_account(client, "Everyday", "everyday")
    savings = create_account(client, "Savings", "savings")
    rows = [
        (everyday, "2025-08-01", -120000, "TRANSFER TO HELEN SMITH"),
        (everyday, "2025-08-02", -8540, "WOOLWORTHS METRO 1234"),
        (everyday, "2025-08-03", -4200, "WOOLWORTHS METRO 5678"),
        (savings, "2025-08-04", -2500, "QANTAS AIRWAYS"),
    ]
    for account, date, cents, desc in rows:
        resp = client.post(
            "/api/transactions",
            json={
                "account_id": account["id"], "txn_date": date,
                "amount_cents": cents, "description": desc,
            },
        )
        assert resp.status_code in (200, 201), resp.text


def _run(name: str, args: dict, privacy_mode: str = "full") -> str:
    with SessionLocal() as db:
        household = _household(db)
        window = resolve(household, "fy:2025", today=dt.date(2025, 8, 15))
        return advisor_tools.run(db, household, privacy_mode, name, args, window)


# --------------------------------------------------------------------- gating


def test_detail_tools_are_withheld_in_aggregates_mode() -> None:
    offered = {t.name for t in advisor_tools.tools_for("aggregates")}
    assert "search_transactions" not in offered
    assert "list_transactions" not in offered
    # Aggregate lookups are still available.
    assert {"spending_by_category", "spending_by_merchant", "compare_periods"} <= offered


def test_detail_tools_are_offered_in_permissive_modes() -> None:
    for mode in ("full", "local_only"):
        offered = {t.name for t in advisor_tools.tools_for(mode)}
        assert "search_transactions" in offered, mode


def test_a_withheld_tool_is_refused_even_if_the_model_invents_the_call(
    auth_client: TestClient,
) -> None:
    """The model can name a tool it was never given, so the mode is re-checked."""
    _seed(auth_client)
    out = _run("search_transactions", {"text": "helen"}, privacy_mode="aggregates")
    assert "Not available" in out
    assert "helen" not in out.lower().replace("'helen'", "")
    assert "Settings > AI advisor" in out


# ---------------------------------------------------------------- the lookups


def test_search_finds_a_transaction_by_description(auth_client: TestClient) -> None:
    """The question that started this."""
    _seed(auth_client)
    out = _run("search_transactions", {"text": "helen"})
    assert "TRANSFER TO HELEN SMITH" in out
    assert "$1,200.00" in out


def test_search_covers_the_whole_history_not_one_period(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"], "txn_date": "2019-03-04",
            "amount_cents": -999, "description": "ANCIENT HELEN PAYMENT",
        },
    )
    assert "ANCIENT HELEN PAYMENT" in _run("search_transactions", {"text": "helen"})


def test_search_reports_no_match_clearly(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert "No transactions match" in _run("search_transactions", {"text": "zzznothing"})


def test_search_needs_something_to_look_for(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert "No search text" in _run("search_transactions", {"text": "   "})


def test_spending_by_merchant_ranks_by_total(auth_client: TestClient) -> None:
    _seed(auth_client)
    out = _run("spending_by_merchant", {})
    assert out.index("Helen Smith") < out.index("Qantas Airways")


def test_list_uncategorised_shows_what_makes_up_the_total(auth_client: TestClient) -> None:
    _seed(auth_client)
    out = _run("list_transactions", {"uncategorised_only": True})
    assert "HELEN SMITH" in out
    assert "$1,200.00" in out


def test_spending_by_category_summarises(auth_client: TestClient) -> None:
    _seed(auth_client)
    out = _run("spending_by_category", {})
    assert "Spending by category" in out


def test_compare_periods_puts_two_windows_side_by_side(auth_client: TestClient) -> None:
    _seed(auth_client)
    out = _run("compare_periods", {"period_a": "fy:2025", "period_b": "fy:2024"})
    assert "FY2025–26" in out and "FY2024–25" in out


def test_an_unparseable_period_falls_back_rather_than_failing(
    auth_client: TestClient,
) -> None:
    _seed(auth_client)
    assert "Spending by category" in _run("spending_by_category", {"period": "nonsense"})


def test_unknown_tool_is_reported(auth_client: TestClient) -> None:
    _seed(auth_client)
    assert "Unknown tool" in _run("made_up", {})


# ------------------------------------------------------------------ the loop


def test_chat_offers_detail_tools_only_when_the_mode_allows(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_respond(ai, system, messages, tools, execute):
        captured["tools"] = [t.name for t in tools]
        return "ok"

    monkeypatch.setattr(advisor, "_respond", fake_respond)
    auth_client.patch(
        "/api/ai/settings",
        json={"provider": "anthropic", "api_key": "k", "privacy_mode": "aggregates"},
    )
    auth_client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert "search_transactions" not in captured["tools"]

    auth_client.patch("/api/ai/settings", json={"privacy_mode": "full"})
    auth_client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert "search_transactions" in captured["tools"]


def test_falls_back_to_the_snapshot_when_tools_are_unsupported(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local models often cannot do tool calling; a plain answer beats an error."""
    auth_client.patch("/api/ai/settings", json={"provider": "openai", "api_key": "k"})

    def no_tools(ai, system, messages, tools, execute):
        raise advisor.ProviderError("400 — this model does not support tools")

    monkeypatch.setattr(advisor, "_CONVERSATIONS", {"openai": no_tools})
    monkeypatch.setattr(advisor, "_call_provider", lambda ai, system, messages: "plain answer")
    resp = auth_client.post(
        "/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "plain answer"


def test_other_provider_errors_still_surface(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "openai", "api_key": "bad"})

    def unauthorised(ai, system, messages, tools, execute):
        raise advisor.ProviderError("401 — invalid api key")

    monkeypatch.setattr(advisor, "_CONVERSATIONS", {"openai": unauthorised})
    resp = auth_client.post(
        "/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 502
    assert "invalid api key" in resp.json()["detail"]
