from __future__ import annotations

import datetime as dt

import pytest
from conftest import create_account
from fastapi.testclient import TestClient

from app.services import advisor

TODAY = dt.date.today().isoformat()


def test_settings_default_and_key_is_write_only(auth_client: TestClient) -> None:
    base = auth_client.get("/api/ai/settings").json()
    assert base["provider"] == "none"
    assert base["privacy_mode"] == "aggregates"
    assert base["has_key"] is False
    assert base["configured"] is False

    upd = auth_client.patch(
        "/api/ai/settings",
        json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-secret"},
    ).json()
    assert upd["provider"] == "openai"
    assert upd["configured"] is True
    assert upd["has_key"] is True
    # The key itself is never returned.
    assert "api_key" not in upd and "api_key_encrypted" not in upd


def test_chat_requires_configuration(auth_client: TestClient) -> None:
    resp = auth_client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400


def _stub_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_call(ai, system, messages):
        captured["system"] = system
        captured["messages"] = messages
        return "Here is some general guidance."

    monkeypatch.setattr(advisor, "_call_provider", fake_call)
    return captured


def test_chat_replies_and_audits(auth_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": TODAY,
            "amount_cents": -4200,
            "description": "POWER BILL",
        },
    )
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})
    captured = _stub_capture(monkeypatch)

    resp = auth_client.post(
        "/api/ai/chat", json={"messages": [{"role": "user", "content": "Where can we save?"}]}
    )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Here is some general guidance."
    assert "Top spending categories" in str(captured["system"])


def test_provider_error_surfaces_message(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})

    def boom(ai, system, messages):
        raise advisor.ProviderError("400 — model: claude-x not found")

    monkeypatch.setattr(advisor, "_call_provider", boom)
    resp = auth_client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 502
    assert "model: claude-x not found" in resp.json()["detail"]


def test_privacy_mode_controls_raw_transactions(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": TODAY,
            "amount_cents": -3300,
            "description": "SECRETMERCHANT XYZ",
        },
    )
    captured = _stub_capture(monkeypatch)

    auth_client.patch(
        "/api/ai/settings",
        json={"provider": "anthropic", "api_key": "k", "privacy_mode": "aggregates"},
    )
    auth_client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert "secretmerchant" not in str(captured["system"]).lower()  # aggregates: no raw txns

    auth_client.patch("/api/ai/settings", json={"privacy_mode": "full"})
    auth_client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert "secretmerchant" in str(captured["system"]).lower()  # full detail includes recent txns


def test_models_requires_configuration(auth_client: TestClient) -> None:
    assert auth_client.get("/api/ai/models").status_code == 400


def test_list_models(auth_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})
    monkeypatch.setattr(
        advisor, "list_models", lambda ai: [{"id": "claude-x", "label": "Claude X"}]
    )
    resp = auth_client.get("/api/ai/models")
    assert resp.status_code == 200
    assert resp.json() == [{"id": "claude-x", "label": "Claude X"}]


def test_connection_ok(auth_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})
    monkeypatch.setattr(advisor, "test_connection", lambda ai: "OK")
    resp = auth_client.post("/api/ai/test")
    assert resp.status_code == 200
    assert "Connected" in resp.json()["message"]


def test_connection_surfaces_provider_error(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "bad"})

    def boom(ai):
        raise advisor.ProviderError("400: model: nope not found")

    monkeypatch.setattr(advisor, "test_connection", boom)
    resp = auth_client.post("/api/ai/test")
    assert resp.status_code == 502
    assert "not found" in resp.json()["detail"]


def test_test_requires_configuration(auth_client: TestClient) -> None:
    assert auth_client.post("/api/ai/test").status_code == 400
