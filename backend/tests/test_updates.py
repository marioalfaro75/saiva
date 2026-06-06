from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.services import updates


def test_parse_version() -> None:
    assert updates.parse_version("v1.2.3") == (1, 2, 3)
    assert updates.parse_version("0.4") == (0, 4, 0)
    assert updates.parse_version("1.2.3-rc1") == (1, 2, 3)
    assert updates.parse_version("dev") is None


def test_is_newer() -> None:
    assert updates.is_newer("v0.5.0", "0.4.0") is True
    assert updates.is_newer("0.4.0", "0.4.0") is False
    assert updates.is_newer("0.3.0", "0.4.0") is False
    assert updates.is_newer("0.5.0", "dev") is False  # non-semver current -> can't compare


def test_latest_release_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_fetch(repo: str) -> updates.Release:
        calls["n"] += 1
        return updates.Release(tag="v1.0.0", url="", published_at="", notes="")

    monkeypatch.setattr(updates, "fetch_latest_release", fake_fetch)
    updates._cache["ts"] = 0.0
    updates._cache["release"] = None

    first = updates.latest_release_cached("repo")
    second = updates.latest_release_cached("repo")  # served from cache
    assert first is second
    assert calls["n"] == 1

    updates.latest_release_cached("repo", force=True)  # bypasses cache
    assert calls["n"] == 2


def test_meta_requires_auth(client: TestClient) -> None:
    assert client.get("/api/meta").status_code == 401


def test_meta_returns_version(auth_client: TestClient) -> None:
    resp = auth_client.get("/api/meta")
    assert resp.status_code == 200
    assert "version" in resp.json()


def test_update_check_reports_available(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import admin

    monkeypatch.setattr(admin.settings, "saiva_version", "0.1.0")
    monkeypatch.setattr(admin.settings, "update_check_enabled", True)
    monkeypatch.setattr(
        updates,
        "latest_release_cached",
        lambda repo, force=False: updates.Release(
            tag="v0.5.0", url="https://x/releases/v0.5.0", published_at="2026-01-01", notes="hi"
        ),
    )
    data = auth_client.get("/api/admin/update-check").json()
    assert data["current_version"] == "0.1.0"
    assert data["latest_version"] == "v0.5.0"
    assert data["update_available"] is True
    assert data["apply_available"] is False  # no Watchtower configured in tests


def test_update_blocked_when_not_configured(auth_client: TestClient) -> None:
    assert auth_client.post("/api/admin/update").status_code == 503


def test_update_triggers_watchtower_when_configured(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import admin

    monkeypatch.setattr(admin.settings, "watchtower_url", "http://watchtower:8080")
    monkeypatch.setattr(admin.settings, "watchtower_token", "tok")
    called: dict[str, tuple[str, str]] = {}
    monkeypatch.setattr(
        updates, "trigger_watchtower", lambda url, token: called.setdefault("hit", (url, token))
    )

    resp = auth_client.post("/api/admin/update")
    assert resp.status_code == 202
    for _ in range(50):  # the trigger runs in a daemon thread
        if "hit" in called:
            break
        time.sleep(0.02)
    assert called["hit"] == ("http://watchtower:8080", "tok")
