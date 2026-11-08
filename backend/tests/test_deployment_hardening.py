"""Invariants about how the app is deployed and who may change what.

The compose and Caddy assertions look unusual in a Python suite, but there is no
other test runner in this repository and these regress in exactly the same silent
way application code does: someone adds a service, mounts the socket for
convenience, and nothing anywhere says no.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
import yaml
from conftest import DEFAULT_PASSWORD, sync_csrf
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
COMPOSE_PROD = REPO / "docker-compose.prod.yml"
COMPOSE_DEV = REPO / "docker-compose.yml"
CADDYFILE = REPO / "infra" / "Caddyfile"


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# --- Household configuration is an owner decision -------------------------------


def _second_user(client: TestClient, role: str) -> TestClient:
    """Add a user in the given role and return a client signed in as them."""
    resp = client.post(
        "/api/household/users",
        json={
            "name": "Adult",
            "email": f"{role}@example.com",
            "password": DEFAULT_PASSWORD,
            "role": role,
        },
    )
    assert resp.status_code == 201, resp.text

    from app.main import app

    other = TestClient(app)
    sync_csrf(other)
    login = other.post(
        "/api/auth/login", json={"email": f"{role}@example.com", "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200, login.text
    sync_csrf(other, login.json()["csrf_token"])
    return other


def test_a_writer_cannot_reshape_the_household_reporting_periods(
    auth_client: TestClient,
) -> None:
    """`fy_start_month` and `period_basis` decide where every period boundary falls.

    Changing one silently restates every figure the household has been shown, which
    is not a rung below "add a user" — and adding a user was already owner-only.
    """
    adult = _second_user(auth_client, "adult")
    assert adult.get("/api/auth/me").status_code == 200, "the second user never signed in"

    resp = adult.patch("/api/household", json={"fy_start_month": 1, "period_basis": "weekly"})
    assert resp.status_code == 403, (
        "a non-owner restated every historical figure the household reports"
    )


def test_the_owner_can_still_change_it(auth_client: TestClient) -> None:
    """The lock must not have locked the owner out of their own settings."""
    resp = auth_client.patch("/api/household", json={"name": "New Name"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "New Name"


# --- Alerts actually get sent ---------------------------------------------------


def test_an_alert_is_emailed_even_when_a_browser_saw_it_first(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`generate` is idempotent and the feed calls it on every read.

    So if any tab loaded before the cron ran, the notification row already existed,
    nothing was "created", and the alert was never emailed to anyone. Which is most
    of the time — people leave the app open.
    """
    from app import models
    from app.db import SessionLocal
    from app.services import notifications

    household_id = auth_client.get("/api/auth/me").json()["household"]["id"]
    with SessionLocal() as db:
        ns = notifications.settings_for(db, household_id)
        ns.email_enabled = True
        db.add(
            models.Notification(
                household_id=household_id,
                key="budget:groceries:2026-03",
                type="budget",
                severity="alert",
                title="Groceries budget exceeded",
                body="You are over by $40.",
            )
        )
        db.commit()

    sent: list[tuple] = []
    monkeypatch.setattr(
        notifications, "send_email", lambda *a, **k: (sent.append(a), True)[1]
    )
    monkeypatch.setattr(
        notifications.get_settings(), "smtp_host", "smtp.example.com", raising=False
    )
    monkeypatch.setattr(
        notifications.get_settings(), "smtp_from", "saiva@example.com", raising=False
    )

    with SessionLocal() as db:
        household = db.get(models.Household, household_id)
        assert household is not None
        result = notifications.run_for_household(db, household, today=dt.date.today())

    assert result["emailed"] == 1, (
        "an alert that already existed in the feed was never emailed to anybody"
    )
    assert sent, "send_email was not called"


def test_an_alert_already_read_in_the_app_is_not_emailed(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chasing someone by email about something they have plainly seen is noise."""
    from app import models
    from app.db import SessionLocal
    from app.services import notifications

    household_id = auth_client.get("/api/auth/me").json()["household"]["id"]
    with SessionLocal() as db:
        notifications.settings_for(db, household_id).email_enabled = True
        db.add(
            models.Notification(
                household_id=household_id,
                key="budget:already-seen",
                type="budget",
                severity="alert",
                title="Seen already",
                body="...",
                read_at=dt.datetime.utcnow(),
            )
        )
        db.commit()
        pending = notifications._pending_email(db, household_id, dt.date.today())
    assert pending == []


def test_a_stale_alert_is_not_emailed(auth_client: TestClient) -> None:
    """Otherwise the first cron run after a quiet month is a wall of old news."""
    from app import models
    from app.db import SessionLocal
    from app.services import notifications

    household_id = auth_client.get("/api/auth/me").json()["household"]["id"]
    old = dt.datetime.utcnow() - dt.timedelta(days=notifications.EMAIL_BACKLOG_DAYS + 5)
    with SessionLocal() as db:
        db.add(
            models.Notification(
                household_id=household_id,
                key="budget:ancient",
                type="budget",
                severity="alert",
                title="Ancient history",
                body="...",
                created_at=old,
            )
        )
        db.commit()
        assert notifications._pending_email(db, household_id, dt.date.today()) == []


# --- The Docker socket is simply not here any more ------------------------------
#
# These used to assert that exactly one container held the socket, that it was behind a
# filtering proxy, and that the proxy sat on an internal network. All of that was true
# and none of it was safe: the proxy's own comment said so plainly, because updating a
# container means creating one, and anything that can create a container can create a
# privileged one and own the host. That access existed so a button could pull two images.
#
# The button now writes a file into a bind-mounted directory and a root-owned agent on
# the host does the work, so the assertions become much simpler and much stronger: no
# container anywhere touches the daemon.


def test_no_container_can_reach_the_docker_daemon() -> None:
    """The strongest form of the old property: there is no socket to hold.

    Checks both compose files, including the development one, because a dev-only socket
    mount is still a socket mount on a laptop that also has the family's data on it.
    """
    for path in (COMPOSE_PROD, COMPOSE_DEV):
        services = _compose(path)["services"]
        assert services, f"read no services from {path.name}, so this test proves nothing"
        for name, service in services.items():
            mounts = [str(v) for v in (service.get("volumes") or [])]
            assert not any("docker.sock" in m for m in mounts), (
                f"{path.name}: {name} mounts the Docker socket. Nothing in this stack "
                f"needs it: updates are applied by scripts/deploy.sh on the host."
            )
            env = {str(k): str(v) for k, v in (service.get("environment") or {}).items()}
            assert "DOCKER_HOST" not in env, (
                f"{path.name}: {name} is pointed at a Docker API. Same reason."
            )


def test_the_app_can_write_only_the_drop_box_and_read_the_rest() -> None:
    """The security boundary, and the one this design got wrong the first time.

    Giving the container the whole directory was reviewed and broken end to end. Owning a
    directory means being able to unlink and rename root-owned entries inside it, so the
    container could point root's deploy.lock and run.log at any file on the host (shell
    redirections follow symlinks), point run.log at a file it wanted to READ back through
    the status API, and rename records/ aside so a rollback restored its .env and pinned
    every service to an image of its choosing.

    Two mounts: read-only on the directory, read-write on the drop-box inside it. The
    obvious 'improvement' when the button needs to do something new is to widen this, so
    it is a test rather than a comment.
    """
    api = _compose(COMPOSE_PROD)["services"]["api"]
    mounts = [str(v) for v in (api.get("volumes") or [])]
    update_mounts = [m for m in mounts if "saiva-update" in m]
    assert len(update_mounts) == 2, f"expected two update mounts on api, got {update_mounts}"

    outer = [m for m in update_mounts if m.endswith(":/run/saiva-update:ro")]
    assert outer, (
        f"the api container can write the whole update directory: {update_mounts}. "
        "That is enough to make root open files of its choosing."
    )
    drop_box = [m for m in update_mounts if m.endswith(":/run/saiva-update/requests")]
    assert drop_box, f"no writable drop-box mount for the button: {update_mounts}"
    assert "/requests:" in drop_box[0], (
        f"the writable mount is not the requests sub-directory: {drop_box[0]}"
    )

    env = {str(k): str(v) for k, v in (api.get("environment") or {}).items()}
    assert env.get("SAIVA_UPDATE_DIR") == "/run/saiva-update", (
        "the api container is not told where its update directory is"
    )


def test_the_installer_never_hands_the_state_directory_to_the_container() -> None:
    """The fix for that, asserted where it is made.

    `chown <api uid> $STATE` is one word away from `chown root:<api uid> $STATE/requests`
    and the difference is a privilege escalation, so it is worth a test that reads the
    installer rather than trusting the comment above it.
    """
    installer = (REPO / "scripts" / "install-updater.sh").read_text()
    lines = [
        line.strip()
        for line in installer.splitlines()
        if not line.lstrip().startswith("#") and ("chown" in line or "install -d" in line)
    ]
    assert lines, "no ownership statements found in the installer, so this proves nothing"
    for line in lines:
        if "requests" in line:
            continue
        assert "API_UID" not in line, (
            f"the installer gives the api container something other than the drop-box: {line}"
        )
    assert any("0730" in line and "requests" in line for line in lines), (
        "the drop-box is not 0730, so the container can list what is in it"
    )
    assert any("0700" in line and "records" in line for line in lines), (
        "records/ is not 0700, and it holds a verbatim copy of .env"
    )


def test_caddy_reads_the_status_file_and_cannot_write_it() -> None:
    """Caddy serves the updater's public record, so it must not be able to forge one."""
    caddy = _compose(COMPOSE_PROD)["services"]["caddy"]
    mounts = [str(v) for v in (caddy.get("volumes") or [])]
    status_mounts = [m for m in mounts if "saiva-update" in m]
    assert len(status_mounts) == 1, f"expected one status mount on caddy, got {status_mounts}"
    assert status_mounts[0].endswith(":ro"), (
        "caddy mounts the updater's directory read-write; it only ever reads from it"
    )


# --- Surviving a bad update ------------------------------------------------------


def test_every_service_image_can_be_pinned_to_an_exact_reference() -> None:
    """A rollback restores digests, not tags, so every image needs an override.

    `latest` is not a thing you can go back to: by the time you want to, it points at the
    release you are rolling back from. scripts/lib/release.sh records the resolved digest
    of every service before it changes anything and writes those digests into these
    variables if it has to put the box back. A service without an override is a service
    the rollback cannot restore.
    """
    services = _compose(COMPOSE_PROD)["services"]
    expected = {"api": "SAIVA_API_IMAGE", "web": "SAIVA_WEB_IMAGE",
                "db": "SAIVA_DB_IMAGE", "caddy": "SAIVA_CADDY_IMAGE"}
    for name, variable in expected.items():
        image = str(services[name]["image"])
        assert image.startswith("${" + variable), (
            f"{name}'s image cannot be pinned with {variable}, so a rollback cannot "
            f"restore it by digest: {image}"
        )


def test_the_rollback_pin_list_matches_the_services_it_claims_to_cover() -> None:
    """The shell's list and the compose file must not drift apart silently."""
    release_sh = (REPO / "scripts" / "lib" / "release.sh").read_text()
    assert "SAIVA_SERVICES=(api web db caddy)" in release_sh, (
        "scripts/lib/release.sh no longer records all four services; a rollback that "
        "restores only some of them leaves the box on the combination that failed"
    )
    for variable in ("SAIVA_API_IMAGE", "SAIVA_WEB_IMAGE", "SAIVA_DB_IMAGE", "SAIVA_CADDY_IMAGE"):
        assert variable in release_sh, f"{variable} is not in the rollback pin list"


def test_the_deploy_never_derives_persisted_state_from_a_missing_flag() -> None:
    """The defect that took the production site off the LAN, asserted structurally.

    scripts/deploy.sh used to end its site-address block with an `else` that wrote
    SAIVA_BIND_ADDR=127.0.0.1, so a bare `make pull` -- the command the README gave for
    upgrades -- un-published an install that had been reachable on the LAN for months.
    The flag's absence meant "the owner did not say" and was read as "the owner said
    localhost".

    scripts/tests/run.sh proves the behaviour by running the deploy twice and diffing
    .env. This asserts the shape, so the branch cannot come back in a form that happens
    to pass that test on the day it is written.
    """
    source = (REPO / "scripts" / "deploy.sh").read_text()
    assert source.strip(), "read nothing from deploy.sh, so this test passes vacuously"
    body = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    in_site_block = False
    for line in body:
        if line.startswith('if [ -n "$SITE" ]'):
            in_site_block = True
            continue
        if in_site_block:
            if line.startswith("fi"):
                break
            assert not line.startswith("else"), (
                "deploy.sh has an `else` on the site-address branch again. A flag's "
                "absence must never change persisted state -- see scripts/lib/env-file.sh."
            )


def test_nothing_the_host_runs_prunes_images_behind_the_owner() -> None:
    """A prune in the deploy would delete exactly what a rollback needs.

    `docker image prune` removes dangling images, and an image kept for a rollback is
    dangling by definition: the tag it had moved to the release that replaced it. So
    adding a prune to the deploy -- the obvious response to noticing that images
    accumulate -- deletes the one thing that makes an offline rollback possible. Pruning
    is the owner's call, made once a release has proved itself, and the README says so.
    """
    paths = (
        REPO / "scripts" / "deploy.sh",
        REPO / "scripts" / "lib" / "release.sh",
        REPO / "scripts" / "saiva-update-agent",
        REPO / "Makefile",
    )
    host_scripts = {path.relative_to(REPO): path.read_text() for path in paths}
    assert all(text.strip() for text in host_scripts.values()), (
        f"read nothing from {list(host_scripts)}, so this test passes vacuously"
    )
    prunes = re.compile(r"\b(image|system)\s+prune\b|\bdocker\s+rmi\b")
    offenders = [
        f"{name}:{number}: {line.strip()}"
        for name, text in host_scripts.items()
        for number, line in enumerate(text.splitlines(), 1)
        if not line.lstrip().startswith("#") and prunes.search(line)
    ]
    assert not offenders, (
        "the deploy prunes images, which deletes the image kept for rolling back a bad "
        "release:\n" + "\n".join(offenders)
    )


def test_the_state_directory_has_one_spelling() -> None:
    """compose, the installer and the deploy script must name the same directory.

    They are three separate literals in three languages. When they drift, the symptom is
    not an error: the deploy writes its status where no container mounts, the panel
    reports "no update has ever run" after every successful update, and nothing says why.
    """
    default = "/var/lib/saiva/update"
    compose = COMPOSE_PROD.read_text()
    # Three now: the api's read-only mount of the directory, its read-write mount of the
    # drop-box inside it, and caddy's read-only mount.
    assert compose.count("${SAIVA_STATE_DIR:-" + default + "}") == 3, (
        "the api and caddy mounts no longer all interpolate SAIVA_STATE_DIR with the "
        f"default {default}"
    )
    assert default in (REPO / "scripts" / "lib" / "state.sh").read_text()
    assert default in (REPO / "scripts" / "install-updater.sh").read_text()
    assert f"SAIVA_STATE_DIR={default}" in (REPO / ".env.example").read_text(), (
        "a fresh .env does not record the state directory, so a box whose /var/lib is "
        "not writable by the deploying user silently uses a different one"
    )


def test_every_outcome_the_updater_can_report_is_one_the_panel_handles() -> None:
    """A cross-language invariant, so it needs a test that reads both sides.

    The shell writes a phase string; the API passes it through verbatim; the panel turns
    it into words. Nothing but this connects them. A new terminal state added to the
    updater without a branch in the panel renders as a bare phase name on the one screen
    the owner looks at when an update has gone wrong -- which is a quiet version of
    exactly the failure this whole design exists to remove.
    """
    release_sh = (REPO / "scripts" / "lib" / "release.sh").read_text()
    phases = set(re.findall(r"status_finish\s+([a-z_]+)", release_sh))
    assert phases, "no phases found in release.sh, so this test is checking nothing"
    assert "applied" in phases and "rolled_back" in phases, (
        f"release.sh no longer reports the outcomes this test knows about: {sorted(phases)}"
    )

    panel = (REPO / "frontend" / "src" / "components" / "UpdatesPanel.tsx").read_text()
    unhandled = sorted(p for p in phases if f'case "{p}":' not in panel)
    assert not unhandled, (
        "the updater can end in states the panel has no words for, so they would render "
        f"as a raw phase name: {unhandled}"
    )

    from app.services import updates

    missing = sorted(phases - updates.TERMINAL_PHASES - updates.ACTIVE_PHASES)
    assert not missing, (
        f"app/services/updates.py does not know these phases, so update_in_progress() "
        f"cannot reason about them: {missing}"
    )


def test_an_update_cannot_run_twice_at_once() -> None:
    """Two concurrent `compose up` runs against one project is a way to lose a database.

    The api refuses a second request while one is in flight, but the api is not the only
    way in -- `make update` on the host goes straight to the script. The lock has to be
    in the script.
    """
    release_sh = (REPO / "scripts" / "lib" / "release.sh").read_text()
    assert "flock" in release_sh, "scripts/lib/release.sh no longer takes a lock"
    assert "already running" in release_sh, "and no longer says so when it cannot get one"

# --- HSTS -----------------------------------------------------------------------


def test_hsts_is_set_from_the_certificate_actually_in_use() -> None:
    """A fixed max-age with Caddy's internal CA turns the cert warning into a wall.

    Hard-coding it off instead meant a public deployment never got HSTS at all, so
    the value comes from the deploy, which knows which CA issued the certificate.
    """
    caddyfile = CADDYFILE.read_text()
    assert "Strict-Transport-Security" in caddyfile, "no HSTS header at all"
    assert "{$SAIVA_HSTS_MAX_AGE:0}" in caddyfile, (
        "HSTS should default to 0 and be raised by the deploy, not hard-coded"
    )
    for path in (COMPOSE_PROD, COMPOSE_DEV):
        caddy = _compose(path)["services"]["caddy"]
        assert "SAIVA_HSTS_MAX_AGE" in caddy["environment"], (
            f"{path.name} does not pass SAIVA_HSTS_MAX_AGE to Caddy"
        )


def test_the_deploy_turns_hsts_on_only_for_a_trusted_certificate() -> None:
    deploy = (REPO / "scripts" / "deploy.sh").read_text()
    assert "SAIVA_HSTS_MAX_AGE" in deploy
    assert '"internal"' in deploy, "the deploy no longer distinguishes the internal CA"
