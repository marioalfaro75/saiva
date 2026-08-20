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

    def fake_respond(ai, system, messages, tools, execute):
        captured["system"] = system
        captured["messages"] = messages
        captured["tools"] = [t.name for t in tools]
        return "Here is some general guidance."

    monkeypatch.setattr(advisor, "_respond", fake_respond)
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
    assert "SPENDING BY CATEGORY" in str(captured["system"])


def test_provider_error_surfaces_message(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})

    def boom(ai, system, messages, tools, execute):
        raise advisor.ProviderError("400 — model: claude-x not found")

    monkeypatch.setattr(advisor, "_respond", boom)
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


# ------------------------------------------------------------------- output limits


class _Resp:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _settings(provider: str, model: str | None = None):
    from app import models

    return models.AiSettings(household_id="h", provider=provider, model=model)


def _capture(monkeypatch: pytest.MonkeyPatch, payload: dict) -> dict:
    sent: dict[str, object] = {}

    def fake_post(url, **kw):
        sent["url"] = url
        sent["json"] = kw.get("json")
        return _Resp(payload)

    monkeypatch.setattr(advisor.httpx, "post", fake_post)
    return sent


def test_anthropic_asks_for_enough_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """1024 tokens could not hold the answers this assistant is asked for."""
    sent = _capture(
        monkeypatch, {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}
    )
    advisor._call_provider(_settings("anthropic"), "SYS", [{"role": "user", "content": "q"}])
    assert sent["json"]["max_tokens"] == advisor.MAX_OUTPUT_TOKENS > 1024


def test_openai_asks_for_enough_room(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture(
        monkeypatch,
        {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]},
    )
    advisor._call_provider(_settings("openai"), "SYS", [{"role": "user", "content": "q"}])
    assert sent["json"]["max_tokens"] == advisor.MAX_OUTPUT_TOKENS


def test_gemini_reserves_a_thinking_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """2.5 models charge their reasoning to the same budget as the reply, so without
    a reserved slice the visible answer can be a single sentence."""
    sent = _capture(
        monkeypatch,
        {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]},
    )
    advisor._call_provider(_settings("gemini"), "SYS", [{"role": "user", "content": "q"}])
    config = sent["json"]["generationConfig"]
    assert config["maxOutputTokens"] == advisor.MAX_OUTPUT_TOKENS
    assert 0 < config["thinkingConfig"]["thinkingBudget"] < advisor.MAX_OUTPUT_TOKENS


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        ("anthropic", {"content": [{"type": "text", "text": "cut"}], "stop_reason": "max_tokens"}),
        ("openai", {"choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]}),
        (
            "gemini",
            {
                "candidates": [
                    {"content": {"parts": [{"text": "cut"}]}, "finishReason": "MAX_TOKENS"}
                ]
            },
        ),
    ],
)
def test_truncated_answers_say_so(
    provider: str, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cut-off answer used to be indistinguishable from a finished one."""
    _capture(monkeypatch, payload)
    out = advisor._call_provider(_settings(provider), "SYS", [{"role": "user", "content": "q"}])
    assert out.startswith("cut")
    assert "cut off" in out


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        ("anthropic", {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}),
        ("openai", {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}),
        (
            "gemini",
            {"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]},
        ),
    ],
)
def test_complete_answers_are_left_alone(
    provider: str, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture(monkeypatch, payload)
    assert advisor._call_provider(
        _settings(provider), "SYS", [{"role": "user", "content": "q"}]
    ) == "done"


def test_gemini_spending_its_whole_budget_thinking_is_explained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No parts at all used to raise a KeyError and surface as a 500."""
    _capture(monkeypatch, {"candidates": [{"content": {}, "finishReason": "MAX_TOKENS"}]})
    with pytest.raises(advisor.ProviderError, match="budget"):
        advisor._call_provider(_settings("gemini"), "SYS", [{"role": "user", "content": "q"}])


def test_anthropic_ignores_non_text_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(
        monkeypatch,
        {
            "content": [{"type": "thinking", "thinking": "…"}, {"type": "text", "text": "answer"}],
            "stop_reason": "end_turn",
        },
    )
    assert advisor._call_provider(
        _settings("anthropic"), "SYS", [{"role": "user", "content": "q"}]
    ) == "answer"


# ------------------------------------------------------------------ the data snapshot


def _add(client: TestClient, account: dict, date: str, cents: int, desc: str) -> None:
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account["id"], "txn_date": date,
            "amount_cents": cents, "description": desc,
        },
    )
    assert resp.status_code in (200, 201), resp.text


def _context_for(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, **params: str
) -> str:
    captured = _stub_capture(monkeypatch)
    resp = client.post(
        f"/api/ai/chat{'?' + '&'.join(f'{k}={v}' for k, v in params.items()) if params else ''}",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    return str(captured["system"])


def test_snapshot_follows_the_selected_period(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking while viewing a past year must answer for that year, not today."""
    account = create_account(auth_client)
    _add(auth_client, account, "2023-09-01", -1000, "OLD SPEND")
    _add(auth_client, account, "2024-09-01", -2500, "NEWER SPEND")
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})

    older = _context_for(auth_client, monkeypatch, period="fy:2023")
    assert "FY2023–24" in older
    assert "$10.00" in older and "$25.00" not in older

    newer = _context_for(auth_client, monkeypatch, period="fy:2024")
    assert "FY2024–25" in newer
    assert "$25.00" in newer


def test_snapshot_states_what_the_model_can_see(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a refusal can name the setting responsible instead of being a dead end."""
    auth_client.patch(
        "/api/ai/settings",
        json={"provider": "anthropic", "api_key": "k", "privacy_mode": "aggregates"},
    )
    context = _context_for(auth_client, monkeypatch)
    assert "WHAT YOU CAN SEE" in context
    assert "Settings > AI advisor" in context

    auth_client.patch("/api/ai/settings", json={"privacy_mode": "full"})
    assert "individual transactions" in _context_for(auth_client, monkeypatch)


def test_aggregates_mode_withholds_merchant_names(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merchant name is the description tidied up, so it is raw transaction data."""
    account = create_account(auth_client)
    _add(auth_client, account, TODAY, -4200, "SECRETMERCHANT XYZ")
    auth_client.patch(
        "/api/ai/settings",
        json={"provider": "anthropic", "api_key": "k", "privacy_mode": "aggregates"},
    )
    aggregates = _context_for(auth_client, monkeypatch)
    assert "secretmerchant" not in aggregates.lower()
    assert "not available in this privacy mode" in aggregates

    auth_client.patch("/api/ai/settings", json={"privacy_mode": "full"})
    assert "secretmerchant" in _context_for(auth_client, monkeypatch).lower()


def test_uncategorised_is_summarised_and_detailed_only_when_permitted(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question that prompted this: what are my uncategorised transactions for?"""
    account = create_account(auth_client)
    _add(auth_client, account, TODAY, -120000, "TRANSFER TO HELEN SMITH")
    auth_client.patch(
        "/api/ai/settings",
        json={"provider": "anthropic", "api_key": "k", "privacy_mode": "aggregates"},
    )
    aggregates = _context_for(auth_client, monkeypatch)
    assert "UNCATEGORISED" in aggregates
    assert "$1,200.00" in aggregates  # the total is an aggregate, so it is shown
    assert "helen" not in aggregates.lower()  # the description is not

    auth_client.patch("/api/ai/settings", json={"privacy_mode": "full"})
    full = _context_for(auth_client, monkeypatch)
    assert "HELEN SMITH" in full


def test_snapshot_reports_what_data_exists(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So the advisor knows the shape of the history even when asked about one period."""
    account = create_account(auth_client)
    _add(auth_client, account, "2023-01-05", -500, "FIRST")
    _add(auth_client, account, "2025-11-20", -700, "LAST")
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})
    context = _context_for(auth_client, monkeypatch)
    assert "05 Jan 2023" in context and "20 Nov 2025" in context
    assert "2 transactions" in context


def test_snapshot_compares_with_the_previous_period(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = create_account(auth_client)
    _add(auth_client, account, "2023-09-01", -10000, "LAST YEAR")
    _add(auth_client, account, "2024-09-01", -20000, "THIS YEAR")
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})
    context = _context_for(auth_client, monkeypatch, period="fy:2024")
    assert "VERSUS THE PREVIOUS" in context
    assert "+100%" in context


def test_snapshot_survives_an_empty_household(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_client.patch("/api/ai/settings", json={"provider": "anthropic", "api_key": "k"})
    context = _context_for(auth_client, monkeypatch)
    assert "No transactions have been imported yet." in context
