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


# --- The Docker socket ----------------------------------------------------------


def test_nothing_that_serves_http_holds_the_docker_socket() -> None:
    """Container-create access to the daemon is root on the host.

    Watchtower listens for HTTP requests, so a bug in it reached straight through
    to the daemon. The socket now belongs to a filtering proxy that publishes no
    ports and sits on an internal network.
    """
    services = _compose(COMPOSE_PROD)["services"]
    for name, service in services.items():
        mounts = [str(v) for v in (service.get("volumes") or [])]
        holds_socket = any("/var/run/docker.sock" in m for m in mounts)
        if not holds_socket:
            continue
        assert name == "docker-socket-proxy", (
            f"{name} mounts the Docker socket directly; put it behind the proxy"
        )
        assert all(m.endswith(":ro") for m in mounts if "docker.sock" in m), (
            "the proxy should mount the socket read-only"
        )
        assert not service.get("ports"), "the socket proxy must not publish a port"
        assert service.get("networks") == ["docker-api"], (
            "the socket proxy must stay on the isolated network"
        )


def test_the_socket_proxy_refuses_the_dangerous_api_sections() -> None:
    """Blocking exec, volumes and networks is most of what the proxy is for."""
    proxy = _compose(COMPOSE_PROD)["services"]["docker-socket-proxy"]
    env = {k: str(v) for k, v in proxy["environment"].items()}
    for blocked in ("EXEC", "VOLUMES", "NETWORKS", "BUILD", "SWARM", "SYSTEM"):
        assert env.get(blocked) == "0", f"{blocked} is not disabled on the socket proxy"


def test_the_docker_api_network_is_internal() -> None:
    networks = _compose(COMPOSE_PROD)["networks"]
    assert networks["docker-api"]["internal"] is True


# --- Surviving a bad update ------------------------------------------------------

TRUTHY = {"true", "1", "yes", "on"}
INTERPOLATION = re.compile(r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>.*))?\}$")


def _without_dotenv(value: str) -> str:
    """What Compose substitutes for one `${VAR:-default}` when nothing sets VAR.

    Every assertion below is about what the shipped file does on a host that took the
    defaults, so a placeholder has to read as the value it becomes and not as the
    characters it is spelled with. Leaving them unresolved is not a cosmetic
    difference in either direction. A `${SAIVA_CLEANUP:-true}` would slip past a check
    for the literal word "true" and switch image cleanup back on unnoticed, which is
    the regression the next test exists to catch; and a `${SAIVA_X:-}` that resolves
    to nothing would read as a non-empty string of literal characters, which is how an
    earlier version of the disk-budget check in this section passed while the setting
    it was checking configured nothing at all.
    """
    match = INTERPOLATION.match(value.strip())
    if not match:
        return value.strip()
    return (match.group("default") or "").strip()


def _watchtower_env() -> dict[str, str]:
    """The updater's environment, with a loud failure if the service is gone.

    Returning {} for a missing service would make every assertion below pass by
    default, which is the one outcome worth ruling out: the hazard they describe is
    Watchtower's, and a vacuous green here would read as "we checked". If the
    `watchtower` service is deliberately removed — the compose file offers that as an
    option for anyone who does not want the Docker-socket trade — these tests should
    be removed with it rather than left to pass on an absence.
    """
    services = _compose(COMPOSE_PROD)["services"]
    assert "watchtower" in services, (
        "docker-compose.prod.yml has no watchtower service, so these tests are "
        "asserting nothing; delete them along with the service"
    )
    environment = services["watchtower"].get("environment") or {}
    return {k: _without_dotenv(str(v)) for k, v in environment.items()}


def _watchtower_flags() -> str:
    """The updater's `command:`/`entrypoint:` overrides, as one searchable string.

    Reading only `environment:` is not enough to know how Watchtower is configured.
    The fork resolves settings with "flag > env > default" precedence and skips the
    environment for any flag that was explicitly set, so a `command: --cleanup` in this
    service silently wins over `WATCHTOWER_CLEANUP: "false"` and every assertion below
    would stay green while the behaviour flipped. The fork's own documentation puts
    flags in `command:` in most of its compose examples, so this is the likely shape of
    a future edit, not a contrived one.
    """
    service = _compose(COMPOSE_PROD)["services"]["watchtower"]
    parts: list[str] = []
    for key in ("command", "entrypoint"):
        value = service.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
    return " ".join(parts)


def test_the_updater_does_not_delete_the_image_it_replaced() -> None:
    """With cleanup on, a bad release left the box down and nothing to roll back to.

    Watchtower counts a container that merely *starts* as a successful update — it
    does not consult the HEALTHCHECK on this path — and its cleanup then removes the
    previous image with Docker's Force flag, so an extra local tag does not protect it
    either. The replacement meanwhile crash-loops: the API's CMD is
    `python -m app.dbinit && uvicorn`, so a failed migration means nothing binds :8000
    and `restart: unless-stopped` retries it forever. That left the documented recovery
    (`SAIVA_VERSION=sha-<previous>` + `make pull`) needing working network and GHCR
    access at the exact moment a household's records were offline. Off, the image stays
    on the host and the rollback is local.
    """
    cleanup = _watchtower_env().get("WATCHTOWER_CLEANUP", "false")
    assert cleanup.lower() not in TRUTHY, (
        "WATCHTOWER_CLEANUP is back on, so the next bad release prunes the only image "
        "this box could have rolled back to without the network"
    )
    # `--cleanup=false` is a legitimate way to spell the same decision, so match the
    # flag and then read its value rather than treating its presence as the offence.
    flags = _watchtower_flags()
    flagged = re.search(r"(?:^|\s)(?:--cleanup|-c)(?:=(?P<value>\S*))?(?=$|\s)", flags)
    assert flagged is None or (flagged.group("value") or "true").lower() not in TRUTHY, (
        "a --cleanup flag in the updater's command outranks WATCHTOWER_CLEANUP, which "
        f"still reads {cleanup!r}, so cleanup is on and the rollback image is pruned "
        f"after the next update: {flags!r}"
    )


def test_the_updater_is_not_gated_on_a_docker_api_the_socket_proxy_refuses() -> None:
    """A disk tripwire for the trade above is the obvious next move, and it broke it.

    Turning cleanup off means nothing reclaims images, so `WATCHTOWER_DISK_SPACE_WARN`
    reads like the companion setting — and on this deployment it disables the update
    button outright. Watchtower skips its image-usage check only when both thresholds
    are zero; any value arms a `GET /system/df`, the socket proxy answers 403 because
    `SYSTEM: 0`, and the check is fail-closed, so the query failure returns from the
    first statement of the update session, before a single container is listed.
    Choosing warn over max does not help: the abort comes from the query, not from a
    budget being reached.

    It is the silence that makes this worth a test. Watchtower's sync handler still
    answers 200, `trigger_watchtower` in app/services/updates.py inspects neither
    status nor body, and `/api/admin/update` returns "update_started" regardless, so
    the owner is told the update began while nothing happened. There is no log on the
    box that anybody reads and no failing request to notice. The only way to find out
    is to check whether the version changed afterwards.

    The two sides are asserted together because the fix is a choice between them, not
    a value: either no threshold, or `SYSTEM: 1` on the proxy. The second also fails
    test_the_socket_proxy_refuses_the_dangerous_api_sections above, which is correct —
    opening `/system` to keep a disk gauge working is a bad trade on a proxy whose
    whole value is that everything else is off — but it should be a decision somebody
    makes, not a combination that passes.
    """
    env = _watchtower_env()
    thresholds = {
        key: value
        for key, value in env.items()
        if key.startswith("WATCHTOWER_DISK_SPACE") and value
    }
    if not thresholds:
        return
    proxy = _compose(COMPOSE_PROD)["services"]["docker-socket-proxy"]
    system = str((proxy.get("environment") or {}).get("SYSTEM", "0"))
    assert system == "1", (
        "the updater is configured with an image-usage threshold it cannot read: "
        f"{thresholds} needs GET /system/df, and the socket proxy has SYSTEM={system}. "
        'The check fails closed, so every press of "Update now" aborts before '
        "listing a container and reports success anyway. Drop the threshold, or open "
        "SYSTEM and say why"
    )


def test_the_updater_image_cannot_cross_a_major_version_on_its_own() -> None:
    """A floating tag is how this box would acquire the unattended updates it has not got.

    Watchtower carries no watchtower.enable label of its own, and
    `WATCHTOWER_LABEL_ENABLE` means only labelled containers are monitored, so it never
    updates itself: the image moves only when somebody runs `docker compose pull`. That
    sounds safe and is exactly the problem, because on an untagged reference that one
    command can cross several minors at once and eventually a major. The fork's source
    marks the legacy HTTP-API flags "will be removed in the v2 release" and it validates
    no WATCHTOWER_* names at all, so a v2 image would ignore
    `WATCHTOWER_HTTP_API_UPDATE` in silence, enable no endpoint, never bind the API, and
    fall through to the 24-hour schedule it synthesises when none is configured. The
    result is a box that updates itself daily, with cleanup off and no health gate,
    because a tag floated — nobody would have chosen it and nothing would have said so.

    A major-version tag is enough to stop that; a digest is not required, and pinning
    one would stop Watchtower receiving its own fixes.
    """
    image = str(_compose(COMPOSE_PROD)["services"]["watchtower"]["image"])
    assert "@sha256:" in image or re.search(r":\d+(\.\d+)*$", image), (
        f"the updater image is {image!r}, which floats. One `docker compose pull` can "
        "carry it across a major version, and the version that removes the HTTP-API "
        "flag turns this box into a 24-hour unattended updater. Pin at least the major"
    )


def test_the_updater_stays_something_a_person_has_to_ask_for() -> None:
    """Nothing here polls, and the reason is one variable rather than the obvious one.

    This was written down backwards once and acted on, so it is worth pinning. It is
    *not* that no schedule is configured: with neither `WATCHTOWER_SCHEDULE` nor
    `WATCHTOWER_POLL_INTERVAL` set, the fork synthesises `@every 86400s` from its
    24-hour default interval. What suppresses that poll is the update endpoint being
    enabled while `WATCHTOWER_HTTP_API_PERIODIC_POLLS` stays false, which puts
    Watchtower in API-only mode: `block := EnableUpdateAPI && !UnblockHTTPAPI` keeps it
    serving the API and it never reaches the scheduler. So removing an endpoint variable
    does not disable updates on this box — it converts them to daily unattended ones,
    which is the opposite of what deleting a line like that looks like it does.

    Both spellings are required for the same reason. `http-api-update` is deprecated in
    favour of `http-api-endpoints` and documented as working only until v2;
    `http-api-endpoints` is the supported spelling but is absent from older images; and
    an unrecognised WATCHTOWER_* name is ignored without a word. Each alone therefore
    has a version on which it enables nothing and the poller takes over, while
    `ResolveEndpoints` unions the two, so together they enable exactly the one `update`
    endpoint on anything that understands either name. If a deliberate move to a version
    that only has one of them makes this fail, change the assertion and record which
    version — do not simply delete the line that is holding the poll off.
    """
    env = _watchtower_env()
    polls = env.get("WATCHTOWER_HTTP_API_PERIODIC_POLLS", "false")
    assert polls.lower() not in TRUTHY, (
        "WATCHTOWER_HTTP_API_PERIODIC_POLLS is on, so the updater now pulls and "
        "recreates api + web every 24 hours with nobody watching, on a box where "
        "cleanup is off and a replacement that merely starts counts as a success"
    )
    for timer in ("WATCHTOWER_SCHEDULE", "WATCHTOWER_POLL_INTERVAL"):
        assert timer not in env, (
            f"{timer} is set, which asks the updater to apply releases on a timer. "
            "Every rollback story in this file assumes somebody is at the keyboard "
            "when a bad release lands; if that is being given up, say so here"
        )
    # The same three settings spelled as flags, which outrank the environment — see
    # _watchtower_flags. `--http-api-periodic-polls` in `command:` is the shortest
    # edit that turns this box into a daily unattended updater.
    flags = _watchtower_flags()
    timer_flags = re.findall(
        r"(?:^|\s)(--http-api-periodic-polls|--schedule|--interval)(?=$|[\s=])", flags
    )
    assert not timer_flags, (
        f"{timer_flags} in the updater's command puts it back on a timer regardless of "
        f"the variables above: {flags!r}"
    )
    assert env.get("WATCHTOWER_HTTP_API_UPDATE", "").lower() in TRUTHY, (
        "WATCHTOWER_HTTP_API_UPDATE is gone. On any version that predates "
        "http-api-endpoints that silently enables no endpoint at all, the API never "
        "binds, and the synthesised 24-hour poll runs instead"
    )
    allowlist = env.get("WATCHTOWER_HTTP_API_ENDPOINTS", "")
    endpoints = [part for part in re.split(r"[,\s]+", allowlist) if part]
    assert "update" in endpoints or "all" in endpoints, (
        "WATCHTOWER_HTTP_API_ENDPOINTS no longer enables the update endpoint, so the "
        "in-app button depends entirely on a flag the fork says it will remove in v2 — "
        f"and whose removal starts a 24-hour poll rather than failing loudly: {endpoints}"
    )


def test_nothing_the_host_runs_prunes_images_behind_the_owner() -> None:
    """A prune in the deploy would undo the fix above without touching the fix above.

    `docker image prune` removes dangling images, and the image held for a rollback is
    dangling by definition: the tag it had moved to the release that replaced it. So
    adding a prune to deploy.sh or the Makefile — the obvious response to noticing that
    images accumulate now — deletes exactly the thing cleanup was turned off to keep,
    and leaves WATCHTOWER_CLEANUP reading "false" while behaving like "true". Pruning is
    the owner's call, made when a release has proved itself, and the README says so.
    """
    paths = (REPO / "scripts" / "deploy.sh", REPO / "Makefile")
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
        "the deploy prunes images, which deletes the untagged image kept for rolling "
        "back a bad release:\n" + "\n".join(offenders)
    )


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
