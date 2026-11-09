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


def _split_mount(spec: str) -> tuple[str, str, str]:
    """Split a compose short-syntax volume into (host, container, mode).

    By hand, because the host side interpolates a default — `${SAIVA_STATE_DIR:-/var/…}`
    — and the `:-` in that is not a field separator. `spec.split(":")` returns five
    fields for these mounts, which would not fail the classification below so much as
    make it meaningless.
    """
    fields: list[str] = []
    current = ""
    depth = 0
    for char in spec:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
        elif char == ":" and depth == 0:
            fields.append(current)
            current = ""
            continue
        current += char
    fields.append(current)
    host, container = fields[0], fields[1] if len(fields) > 1 else ""
    return host, container, ":".join(fields[2:])


def test_the_app_can_write_only_the_drop_box_and_read_the_rest() -> None:
    """The security boundary, and the one this design got wrong the first time.

    Giving the container the whole directory was reviewed and broken end to end. Owning a
    directory means being able to unlink and rename root-owned entries inside it, so the
    container could point root's deploy.lock and run.log at any file on the host (shell
    redirections follow symlinks), point run.log at a file it wanted to READ back through
    the status API, and rename records/ aside so a rollback restored its .env and pinned
    every service to an image of its choosing.

    This used to assert that there were exactly two such mounts, which was a proxy for
    the real invariant and expired the moment "Back up now" added a second drop-box. The
    count is not the property; the shape is. One read-only mount of the directory, and
    any number of writable mounts each of which is a sub-directory of it, named the same
    on both sides. A third drop-box needs no change here. Widening one does.
    """
    api = _compose(COMPOSE_PROD)["services"]["api"]
    mounts = [str(v) for v in (api.get("volumes") or [])]
    # Either side, because a mount is in scope here if it touches the state directory at
    # all. Matching only the container path would miss `$STATE/records:/run/anything`,
    # which is both a second writable path and a window onto a directory that holds a
    # verbatim copy of .env.
    update_mounts = [m for m in mounts if "saiva-update" in m or "SAIVA_STATE_DIR" in m]
    assert update_mounts, "no state-directory mounts on api at all, so this proves nothing"

    env = {str(k): str(v) for k, v in (api.get("environment") or {}).items()}
    root = env.get("SAIVA_UPDATE_DIR")
    assert root == "/run/saiva-update", (
        "the api container is not told where its state directory is, so the mounts below "
        f"cannot be classified against anything: {env.get('SAIVA_UPDATE_DIR')!r}"
    )

    outer = [m for m in update_mounts if _split_mount(m)[1] == root]
    assert len(outer) == 1, (
        f"expected exactly one mount of {root} itself, got {outer}. Two would mean one of "
        "them decides, and which one is not something this file states."
    )
    assert _split_mount(outer[0])[2] == "ro", (
        f"the api container can write the whole state directory: {outer[0]}. That is "
        "enough to make root open files of its choosing — see scripts/saiva-update-agent."
    )
    outer_host = _split_mount(outer[0])[0]

    drop_boxes = {}
    for mount in update_mounts:
        if mount in outer:
            continue
        host, container, mode = _split_mount(mount)
        assert container.startswith(root + "/"), (
            f"a second mount of the state directory, outside {root}: {mount}. The "
            "read-only outer mount is what makes a drop-box a drop-box; anything beside "
            "it is either a second writable path or a second way to read records/, which "
            "holds a verbatim copy of .env."
        )
        name = container[len(root) + 1 :]
        assert "/" not in name, (
            f"the drop-box {name!r} is nested rather than a direct sub-directory: {mount}"
        )
        assert mode != "ro", (
            f"{name} is mounted read-only, so the app cannot drop a request in it: {mount}"
        )
        assert host == f"{outer_host}/{name}", (
            f"{name} is mounted from somewhere other than {outer_host}/{name}: {mount}. "
            "The host watches the directory it owns; a request written anywhere else is "
            "one nothing picks up, and nothing would say so."
        )
        drop_boxes[name] = mount

    assert {"requests", "backup-requests"} <= set(drop_boxes), (
        "one of the two drop-boxes the app writes into is not mounted, so pressing that "
        f"button would 503 on a correctly installed box: {sorted(drop_boxes)}"
    )


def test_the_installer_never_hands_the_state_directory_to_the_container() -> None:
    """The fix for that, asserted where it is made.

    `chown <api uid> $STATE` is one word away from `chown root:<api uid> $STATE/requests`
    and the difference is a privilege escalation, so it is worth a test that reads the
    installer rather than trusting the comment above it.

    The skip used to be the substring "requests", which meant the backup drop-box passed
    this test for no better reason than `"requests" in "backup-requests"` — tolerated
    rather than checked. So each drop-box is now named, and each has to be root-owned and
    0730 in its own right: 0730 is what stops the container listing what is in there, and
    root ownership is what stops it renaming root's entries. Group-only access is the
    whole mechanism, so `-g "$API_UID"` on a line is only acceptable on one of these.
    """
    installer = (REPO / "scripts" / "install-updater.sh").read_text()
    # The directories the api container may write into, and nothing else may be handed to
    # it. Both are relative to $STATE: see app/services/{updates,backups}.py.
    drop_boxes = ("requests", "backup-requests")
    lines = [
        line.strip()
        for line in installer.splitlines()
        if not line.lstrip().startswith("#") and ("chown" in line or "install -d" in line)
    ]
    assert lines, "no ownership statements found in the installer, so this proves nothing"

    def _targets(line: str) -> set[str]:
        """What a line acts on, as paths under $STATE — "." being $STATE itself."""
        return {
            match.group(1).lstrip("/") or "."
            for match in re.finditer(r'"\$STATE((?:/[A-Za-z0-9._-]+)*)"', line)
        }

    for line in lines:
        if "API_UID" not in line:
            continue
        acted_on = _targets(line)
        assert acted_on and acted_on <= set(drop_boxes), (
            "the installer gives the api container something other than a drop-box "
            f"({sorted(acted_on) or 'an unrecognised path'}): {line}"
        )

    for name in drop_boxes:
        created = [
            line for line in lines if line.startswith("install -d") and _targets(line) == {name}
        ]
        assert created, f"the installer does not create $STATE/{name} at all"
        for line in created:
            assert "-o root" in line and '-g "$API_UID"' in line, (
                f"$STATE/{name} is not root-owned with the api container as its group, so "
                f"either root cannot trust what is in it or the app cannot write it: {line}"
            )
            assert "-m 0730" in line, (
                f"$STATE/{name} is not created 0730, so the container can list what is in "
                f"it — which is how it would learn whether root had acted yet: {line}"
            )
        # install -d leaves an existing directory's mode alone, so a box upgraded from the
        # old layout only gets these modes if they are re-asserted unconditionally.
        reasserted = [line for line in lines if "chmod" in line and _targets(line) == {name}]
        assert any('chown "root:$API_UID"' in line and "0730" in line for line in reasserted), (
            f"$STATE/{name}'s mode and ownership are not re-asserted, so a box upgraded "
            f"from a layout that had this directory keeps whatever it had: {reasserted}"
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
        # A root-run host script on a nightly timer, and the one most likely to attract a
        # prune: it is the thing that notices the disk filling up. The dumps it writes and
        # the rollback images share a filesystem, so "reclaim some space while we are
        # here" would delete the rollback and call it housekeeping.
        REPO / "scripts" / "backup.sh",
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
    # Four now: the api's read-only mount of the directory, its read-write mounts of the
    # two drop-boxes inside it (requests, for an update; backup-requests, for "Back up
    # now"), and caddy's read-only mount.
    assert compose.count("${SAIVA_STATE_DIR:-" + default + "}") == 4, (
        "the api's three mounts and caddy's one no longer all interpolate "
        f"SAIVA_STATE_DIR with the default {default}"
    )
    assert default in (REPO / "scripts" / "lib" / "state.sh").read_text()
    assert default in (REPO / "scripts" / "install-updater.sh").read_text()
    assert f"SAIVA_STATE_DIR={default}" in (REPO / ".env.example").read_text(), (
        "a fresh .env does not record the state directory, so a box whose /var/lib is "
        "not writable by the deploying user silently uses a different one"
    )


# The two surfaces that work this way, each a shell script writing a phase into a status
# file, an API module passing it through verbatim, and a panel turning it into words. They
# are parametrized rather than copied because the invariant is identical and the failure
# is identical, and because a copy is how one of them would end up with half the checks.
#
# `anchor` is the family's own non-vacuity guard: phases the family is known to report
# today. Without it a discovery regex that silently stopped matching -- a rename, a
# refactor into a helper -- would turn this into a test that proves nothing while staying
# green, which is the specific way cross-language tests rot.
_PHASE_FAMILIES = [
    pytest.param(
        REPO / "scripts" / "lib" / "release.sh",
        REPO / "frontend" / "src" / "components" / "UpdatesPanel.tsx",
        "app.services.updates",
        {"applied", "rolled_back", "running"},
        id="update",
    ),
    pytest.param(
        REPO / "scripts" / "backup.sh",
        REPO / "frontend" / "src" / "components" / "BackupsPanel.tsx",
        "app.services.backups",
        {"ok", "failed", "skipped", "running"},
        id="backup",
    ),
]


def _panel_switch_cases(panel: Path, anchor: str) -> set[str]:
    """The case labels of the one `switch` in `panel` that handles `anchor`.

    Scoped to a single switch rather than searched over the whole file, because the whole
    file is ambiguous and quietly so. BackupsPanel.tsx has two switches — one over
    `offsite`, one over `phase` — and `ok` and `failed` are labels in BOTH. Measured while
    writing this: deleting `case "ok":` from the off-box switch left a plain substring
    check green, because the phase switch still had one. The test would then have been
    asserting that the panel contains the word somewhere, which is not the property.

    `anchor` is the label that identifies the switch, and it earns its keep twice: it is a
    label the shell never writes, so nothing else in this file requires the panel to have
    it. `unreadable` is the API's own answer to a record it cannot parse, and a panel
    without a branch for it would render the words "unreadable" at the owner.
    """
    source = panel.read_text()
    segments = [seg for seg in source.split("switch (") if f'case "{anchor}":' in seg]
    assert len(segments) == 1, (
        f"{panel.name} has {len(segments)} switch statements handling {anchor!r}, so which "
        "one is authoritative is not something this file states. One is the invariant; it "
        "is also what makes the labels below unambiguous."
    )
    return set(re.findall(r'case "([a-z_-]+)":', segments[0]))


# Two ways a phase reaches the status file, and both have to be read. `status_finish`
# writes the terminal ones; `status_set phase` is how `running` gets there, and it was
# missing from this test -- so nothing proved that either panel could render a run in
# progress, which is the state an owner is most likely to be looking at.
_PHASE_PATTERNS = (r"status_finish\s+([a-z_]+)", r"status_set\s+phase\s+([a-z_]+)")


@pytest.mark.parametrize(("script", "panel_path", "module_name", "anchor"), _PHASE_FAMILIES)
def test_every_outcome_the_host_can_report_is_one_the_panel_handles(
    script: Path, panel_path: Path, module_name: str, anchor: set[str]
) -> None:
    """A cross-language invariant, so it needs a test that reads every side.

    The shell writes a phase string; the API passes it through verbatim; the panel turns
    it into words. Nothing but this connects them. A new state added to the host script
    without a branch in the panel renders as a bare phase name on the one screen the owner
    looks at when something has gone wrong -- which is a quiet version of exactly the
    failure this whole design exists to remove.

    The API side matters for a second reason: a phase the service module does not know is
    one {update,backup}_in_progress() cannot reason about, so a run in a state it has
    never heard of either blocks the button forever or never blocks it at all.
    """
    import importlib

    source = script.read_text()
    assert source.strip(), f"read nothing from {script.name}, so this test proves nothing"
    phases = {
        phase for pattern in _PHASE_PATTERNS for phase in re.findall(pattern, source)
    }
    assert anchor <= phases, (
        f"{script.name} no longer reports the outcomes this test knows about "
        f"({sorted(anchor - phases)}), so the discovery regexes have stopped seeing its "
        f"vocabulary and everything below passes for the wrong reason: {sorted(phases)}"
    )

    assert panel_path.is_file(), (
        f"{panel_path.relative_to(REPO)} does not exist, so nothing renders these states "
        f"for the owner: {sorted(phases)}"
    )
    handled = _panel_switch_cases(panel_path, "unreadable")
    unhandled = sorted(phases - handled)
    assert not unhandled, (
        f"{script.name} can end in states {panel_path.name} has no words for, so they "
        f"would render as a raw phase name: {unhandled}"
    )

    module = importlib.import_module(module_name)
    missing = sorted(phases - module.TERMINAL_PHASES - module.ACTIVE_PHASES)
    assert not missing, (
        f"{module_name} does not know these phases, so it cannot tell a run that is "
        f"still going from one that has finished: {missing}"
    )


def test_every_off_box_state_the_backup_can_report_is_one_the_panel_handles() -> None:
    """The same shape as above, for the field that is most likely to be read as a tick.

    `offsite` is not a boolean, and the reason is measured rather than theoretical: `stat
    -c %d` reports a btrfs subvolume, a ZFS dataset, an LVM thin volume and a Proxmox
    bind-mount as DIFFERENT devices while they live on the same physical disk -- which are
    exactly the configurations a Proxmox box has. So a device-number comparison would have
    called every one of those "copied off this box". The vocabulary that replaced it has
    six words in it, only one of which is good news, and a word the panel has no branch
    for would render as itself next to a copy that never left the machine.

    Three things are checked, not one: that the shell still declares the vocabulary in one
    place, that every literal it can actually write is in that declaration, and that the
    panel has a branch for each. The middle one is the load-bearing step -- a state the
    script writes but does not declare would never be compared against the panel at all,
    so without it the other two could both pass over a silent gap.
    """
    source = (REPO / "scripts" / "backup.sh").read_text()
    assert source.strip(), "read nothing from backup.sh, so this test proves nothing"

    declared = re.search(r"SAIVA_OFFSITE_STATES=\(([^)]*)\)", source)
    assert declared, (
        "scripts/backup.sh no longer declares SAIVA_OFFSITE_STATES. That list is the "
        "whole vocabulary, in one place, precisely so that the API can pass it through "
        "and the panel can have a sentence for each; without it there is nothing to hold "
        "the three languages to."
    )
    states = set(declared.group(1).split())
    # The named anchor: the states whose whole purpose is to NOT read as good news, plus
    # the one that may. If the declaration stops containing these, reading it has stopped
    # telling us anything.
    anchor = {"off", "failed", "same-filesystem", "same-host", "remote"}
    assert anchor <= states, (
        f"backup.sh no longer declares the off-box states this test knows about "
        f"({sorted(anchor - states)}); found {sorted(states)}"
    )

    # Every literal the script can actually put in that field, however it gets there:
    # written straight, chosen by the worsening helper, or assigned to the variable the
    # final write reads. A literal outside the declared list would be a state nothing
    # ever checks the panel for, which is the hole the declaration exists to close.
    written = set(re.findall(r"status_set\s+offsite\s+([a-z][a-z-]*)\b", source))
    written |= set(re.findall(r"_offsite_worsen\s+([a-z][a-z-]*)\b", source))
    written |= set(re.findall(r"\bOFFSITE_STATE=([a-z][a-z-]*)\b", source))
    assert written, "no off-box state is written anywhere, so this test is checking nothing"
    assert written <= states, (
        "scripts/backup.sh writes off-box states that SAIVA_OFFSITE_STATES does not "
        f"declare, so nothing checks the panel for them: {sorted(written - states)}"
    )

    # And nothing reaches that field through a variable this test does not follow. The
    # previous shape of this script wrote `${worst:-ok}`, i.e. it defaulted to the most
    # reassuring answer in the union when no branch had said anything; a form this test
    # cannot read is how that would come back unnoticed.
    for token in re.findall(r"status_set\s+offsite\s+(\S+)", source):
        bare = token.strip('"')
        assert re.fullmatch(r"[a-z][a-z-]*", bare) or bare in {
            "$OFFSITE_STATE",
            "${OFFSITE_STATE}",
        }, (
            f"`status_set offsite {token}` is in a form this test cannot follow, so the "
            "off-box vocabulary is no longer being checked against the panel"
        )

    panel_path = REPO / "frontend" / "src" / "components" / "BackupsPanel.tsx"
    assert panel_path.is_file(), (
        f"{panel_path.relative_to(REPO)} does not exist, so nothing tells the owner how "
        f"far their backup actually got: {sorted(states)}"
    )
    # `same-filesystem` is the label that identifies the off-box switch rather than the
    # phase switch beside it, and it is the one word in this vocabulary that must never be
    # rendered as a tick: a copy on the same disk as the database is not a backup.
    unhandled = sorted(states - _panel_switch_cases(panel_path, "same-filesystem"))
    assert not unhandled, (
        "backup.sh can report off-box states BackupsPanel.tsx has no words for, so a copy "
        f"that never left this machine would render as a bare state name: {unhandled}"
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
