from __future__ import annotations

from pathlib import Path

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
    assert data["apply_available"] is False  # no host updater configured in tests


def test_update_blocked_when_not_configured(auth_client: TestClient) -> None:
    assert auth_client.post("/api/admin/update").status_code == 503


def test_update_asks_the_host_when_the_updater_is_installed(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One synchronous, verifiable thing: write the request and say whether that worked.

    This used to start a daemon thread, because Watchtower destroyed the process before
    it could answer, and then return the literal string "update_started" whatever had
    happened. The full set of behaviours around the new endpoint is in
    tests/test_update_button.py; this is the one that used to live here.
    """
    # The layout the installer builds: the container may write only into requests/.
    directory = tmp_path / "saiva-update"
    (directory / "requests").mkdir(parents=True)
    monkeypatch.setenv("SAIVA_UPDATE_DIR", str(directory))

    resp = auth_client.post("/api/admin/update")

    assert resp.status_code == 202
    assert resp.json()["status"] == "requested"
    dropped = list((directory / "requests").iterdir())
    assert len(dropped) == 1 and dropped[0].is_file(), "the host was never actually asked"
