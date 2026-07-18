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


def test_list_models_merges_curated_and_live(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})
    monkeypatch.setattr(
        advisor, "list_models", lambda ai: [{"id": "claude-x", "label": "Claude X"}]
    )
    resp = auth_client.get("/api/ai/models")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()]
    assert "claude-x" in ids  # the live model is included
    assert "claude-opus-4-8" in ids  # …merged onto the curated baseline
    assert ids.index("claude-opus-4-8") < ids.index("claude-x")  # curated first


def test_list_models_previews_curated_without_config(auth_client: TestClient) -> None:
    # The picker can preview a provider the user is choosing but hasn't saved yet —
    # no key required, curated baseline only.
    resp = auth_client.get("/api/ai/models", params={"provider": "gemini"})
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()]
    assert "gemini-2.5-flash" in ids


def test_available_models_falls_back_to_curated_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models

    def boom(ai: models.AiSettings) -> list[dict[str, str]]:
        raise advisor.ProviderError("401 — bad key")

    monkeypatch.setattr(advisor, "list_models", boom)
    ai = models.AiSettings(household_id="h", provider="openai")
    ids = [m["id"] for m in advisor.available_models(ai)]
    assert "gpt-4o" in ids  # curated baseline survives a live-fetch failure


def test_available_models_dedupes_and_keeps_curated_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import models

    monkeypatch.setattr(
        advisor,
        "list_models",
        lambda ai: [{"id": "gpt-4o", "label": "dup"}, {"id": "gpt-9", "label": "GPT-9"}],
    )
    ai = models.AiSettings(household_id="h", provider="openai")
    out = advisor.available_models(ai)
    ids = [m["id"] for m in out]
    assert ids.count("gpt-4o") == 1  # the live duplicate is dropped
    assert "gpt-9" in ids  # a genuinely new live model is appended
    assert next(m for m in out if m["id"] == "gpt-4o")["label"] == "GPT-4o"  # curated label wins


def test_curated_models_skips_custom_openai_host() -> None:
    assert advisor.curated_models("openai", "http://ollama:11434/v1") == []
    assert advisor.curated_models("openai", "https://api.openai.com/v1")  # OpenAI itself: listed
    assert advisor.curated_models("openai", None)  # default endpoint: listed
    assert advisor.curated_models("gemini")  # non-openai providers unaffected by base_url


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


def test_settings_accepts_gemini(auth_client: TestClient) -> None:
    upd = auth_client.patch(
        "/api/ai/settings",
        json={"provider": "gemini", "api_key": "g", "model": "gemini-1.5-flash"},
    ).json()
    assert upd["provider"] == "gemini"
    assert upd["configured"] is True


def test_gemini_call_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import models

    captured: dict[str, object] = {}

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return FakeResp()

    monkeypatch.setattr(advisor.httpx, "post", fake_post)
    ai = models.AiSettings(household_id="h", provider="gemini", model="gemini-1.5-flash")
    out = advisor._call_provider(
        ai,
        "SYS",
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
    )
    assert out == "OK"
    assert "gemini-1.5-flash:generateContent" in str(captured["url"])
    body = captured["json"]
    assert body["system_instruction"]["parts"][0]["text"] == "SYS"
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]  # assistant -> model


def test_gemini_list_models_filters_to_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import models

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {
                "models": [
                    {
                        "name": "models/gemini-1.5-pro",
                        "displayName": "Gemini 1.5 Pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/embedding-001",
                        "displayName": "Embedding",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            }

    monkeypatch.setattr(advisor.httpx, "get", lambda url, **kw: FakeResp())
    ai = models.AiSettings(household_id="h", provider="gemini")
    assert advisor.list_models(ai) == [{"id": "gemini-1.5-pro", "label": "Gemini 1.5 Pro"}]
