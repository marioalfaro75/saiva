"""The in-app update button, and the two lies it used to tell.

The first lie was "Update applied." The endpoint started a daemon thread that posted to
Watchtower, read neither the status nor the body of the reply, and returned the literal
string `update_started`; the panel polled for four minutes and then declared success
either way. That could not be fixed in any one of those layers, because the process that
would report the outcome was the process Watchtower was destroying.

The second was "You're on the latest version", which appeared whenever the update check
was enabled and no newer release was found -- including when the request to GitHub had
failed outright, and including on `edge` and `sha-` builds whose version string cannot be
compared with a release tag at all.

Both are now structural rather than cosmetic: the api writes a request file and reads a
status file that a root-owned host agent owns, and the check reports *why* it has no
answer rather than defaulting to good news.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import updates

AGENT = Path(__file__).resolve().parents[2] / "scripts" / "saiva-update-agent"


@pytest.fixture
def update_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "saiva-update"
    directory.mkdir()
    monkeypatch.setenv("SAIVA_UPDATE_DIR", str(directory))
    return directory


@pytest.fixture
def no_update_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAIVA_UPDATE_DIR", raising=False)


# --------------------------------------------------------------------------------------
# Whether the button can do anything at all
# --------------------------------------------------------------------------------------


def test_apply_is_unavailable_without_a_configured_directory(no_update_dir: None) -> None:
    assert updates.apply_available() is False


def test_apply_is_unavailable_when_the_directory_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount that exists but is read-only must not advertise a working button.

    This is the case the old check could not see: it tested two strings out of .env, so
    it said yes whenever somebody had set them, whatever the filesystem thought.
    """
    directory = tmp_path / "read-only"
    directory.mkdir()
    directory.chmod(0o555)
    monkeypatch.setenv("SAIVA_UPDATE_DIR", str(directory))
    try:
        if os.geteuid() == 0:
            pytest.skip("running as root, which can write to a 0555 directory anyway")
        assert updates.apply_available() is False
    finally:
        directory.chmod(0o755)


def test_the_endpoint_refuses_and_says_how_to_fix_it(
    auth_client: TestClient, no_update_dir: None
) -> None:
    response = auth_client.post("/api/admin/update")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "install-updater.sh" in detail, "the refusal must name the command that fixes it"
    assert "make update" in detail, "and the way to update without the button"


# --------------------------------------------------------------------------------------
# Asking for an update
# --------------------------------------------------------------------------------------


def test_pressing_update_writes_a_request_the_host_can_see(
    auth_client: TestClient, update_dir: Path
) -> None:
    response = auth_client.post("/api/admin/update")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "requested", "no more 'update_started' for something not started"
    assert body["request_id"]
    assert (update_dir / "request").is_file()
    assert list(update_dir.glob(".request.tmp")) == [], "the staging file must be renamed away"


def test_the_request_is_written_atomically(update_dir: Path) -> None:
    """The host watches for this file's existence, so it must never see a partial one."""
    updates.request_update(requested_by="someone@example.invalid")

    # The only artefact left behind is the final name.
    assert sorted(p.name for p in update_dir.iterdir()) == ["request"]


def test_the_request_carries_nothing_that_selects_what_is_deployed(update_dir: Path) -> None:
    """The security property the whole design rests on, asserted rather than assumed.

    The api container is reachable from the network. If the request could name a ref, a
    branch, a URL or a command, then "a container can write a file" would become "a
    container can make root run arbitrary code". It names the time and the user, for the
    host's log, and nothing else.
    """
    updates.request_update(requested_by="owner@example.invalid")
    contents = (update_dir / "request").read_text()

    assert contents.splitlines() == [contents.strip()], "one line, so there is nowhere to hide"
    keys = {part.split("=", 1)[0] for part in contents.split() if "=" in part}
    assert keys == {"requested_at", "requested_by"}, f"unexpected keys in the request: {keys}"


def test_the_agent_never_reads_the_request_for_anything_but_logging() -> None:
    """The other half of that property lives in the shell, so check the shell.

    A future edit that made the agent act on the file's contents would pass every Python
    test here and quietly re-open the hole. This reads the agent and insists that the
    only thing it does with the contents is put them in a message.
    """
    source = AGENT.read_text()
    assert 'requested_by="$(head -c 200 "$REQUEST"' in source, (
        "the agent no longer reads the request the way this test understands; re-check "
        "by hand that its contents still cannot select what gets deployed"
    )
    uses = [line.strip() for line in source.splitlines() if "requested_by" in line]
    # The declaration, the comment, the assignment, and the printf that logs it.
    assert len(uses) <= 5, f"requested_by is used in more places than expected: {uses}"
    allowed = ("#", "local requested_by", "requested_by=", "printf")
    for line in uses:
        assert line.startswith(allowed), (
            f"the agent does something with the request's contents beyond logging: {line}"
        )
    assert "--apply-release" in source
    assert "--ref" not in source, "the agent must not be able to pass a ref through"


def test_a_second_press_while_one_is_running_is_refused(
    auth_client: TestClient, update_dir: Path
) -> None:
    auth_client.post("/api/admin/update")

    second = auth_client.post("/api/admin/update")

    assert second.status_code == 409
    assert "already running" in second.json()["detail"]


def test_requesting_an_update_is_audited(auth_client: TestClient, update_dir: Path) -> None:
    auth_client.post("/api/admin/update")

    actions = [entry["action"] for entry in auth_client.get("/api/admin/audit").json()]
    assert "update_requested" in actions


# --------------------------------------------------------------------------------------
# Reading back what the host did
# --------------------------------------------------------------------------------------


def _write_status(directory: Path, **fields: object) -> None:
    (directory / "status.json").write_text(json.dumps(fields), encoding="utf-8")


def test_status_is_reported_verbatim_from_the_host_record(
    auth_client: TestClient, update_dir: Path
) -> None:
    _write_status(
        update_dir,
        run_id="20260101T000000Z-abcd",
        phase="rolled_back",
        step="gate",
        from_version="v0.16.1",
        to_version="v0.17.0",
        message="the site did not answer after the update",
    )

    body = auth_client.get("/api/admin/update-status").json()

    assert body["phase"] == "rolled_back"
    assert body["from_version"] == "v0.16.1"
    assert body["message"] == "the site did not answer after the update"
    assert body["available"] is True


def test_status_is_a_shrug_and_not_a_claim_when_there_is_no_record(
    auth_client: TestClient, update_dir: Path
) -> None:
    body = auth_client.get("/api/admin/update-status").json()

    assert body["phase"] is None, "no record means unknown, not applied and not failed"
    assert body["message"] is None


def test_a_malformed_status_file_does_not_break_the_page(
    auth_client: TestClient, update_dir: Path
) -> None:
    (update_dir / "status.json").write_text("{not json at all", encoding="utf-8")

    response = auth_client.get("/api/admin/update-status")

    assert response.status_code == 200
    assert response.json()["phase"] == "unreadable"


def test_the_terminal_phase_that_cannot_claim_a_clean_rollback(
    auth_client: TestClient, update_dir: Path
) -> None:
    """`rolled_back_code_only` must survive the round trip with its detail intact.

    It is the one outcome no design can automate away -- the migration succeeded, so the
    schema is ahead of the code that is now running -- and the panel's whole job in that
    state is to say so precisely instead of saying "rolled back".
    """
    _write_status(
        update_dir,
        phase="rolled_back_code_only",
        from_version="v0.16.1",
        to_version="v0.17.0",
        schema_after="ahead",
        message="running v0.16.1 against the v0.17.0 schema",
    )

    body = auth_client.get("/api/admin/update-status").json()

    assert body["phase"] == "rolled_back_code_only"
    assert body["schema_after"] == "ahead"
    assert "v0.17.0 schema" in body["message"]


# --------------------------------------------------------------------------------------
# The check that used to render a network failure as reassurance
# --------------------------------------------------------------------------------------


def test_a_failed_github_check_is_reported_as_failed_not_as_up_to_date(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "latest_release_cached", lambda repo, force=False: None)

    body = auth_client.get("/api/admin/update-check").json()

    assert body["check_enabled"] is True
    assert body["check_failed"] is True, (
        "without this the panel cannot tell 'you are up to date' from 'I could not ask'"
    )
    assert body["update_available"] is False
    assert body["latest_version"] is None


def test_a_build_with_no_comparable_version_says_so(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`edge` and `sha-` builds have no release number, so the comparison declines."""
    from app.api import admin

    monkeypatch.setattr(admin.settings, "saiva_version", "edge")
    monkeypatch.setattr(
        updates,
        "latest_release_cached",
        lambda repo, force=False: updates.Release("v9.9.9", "u", "2026-01-01", "n"),
    )

    body = auth_client.get("/api/admin/update-check").json()

    assert body["comparable"] is False
    assert body["update_available"] is False, "is_newer cannot compare a non-semver version"
    assert body["check_failed"] is False, "the check ran fine; it is the comparison that cannot"


@pytest.mark.parametrize("version", ["edge", "rollback", "latest", "sha-1a2b3c4", "dev"])
def test_every_non_semver_channel_is_reported_as_incomparable(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """Each of these was a channel on which the badge was dark and the panel showed a tick."""
    from app.api import admin

    monkeypatch.setattr(admin.settings, "saiva_version", version)

    assert auth_client.get("/api/admin/update-check").json()["comparable"] is False


def test_a_real_release_version_is_comparable_and_sees_an_update(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard against over-correcting: the ordinary case must still work."""
    from app.api import admin

    monkeypatch.setattr(admin.settings, "saiva_version", "v0.16.1")
    monkeypatch.setattr(
        updates,
        "latest_release_cached",
        lambda repo, force=False: updates.Release("v0.17.0", "u", "2026-01-01", "notes"),
    )

    body = auth_client.get("/api/admin/update-check").json()

    assert body["comparable"] is True
    assert body["check_failed"] is False
    assert body["update_available"] is True
    assert body["latest_version"] == "v0.17.0"
