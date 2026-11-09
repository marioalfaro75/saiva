"""Invariants about the host side of the backup regime: the units, the installer, the agent.

None of this is application code and none of it can be executed here — there is no systemd
in CI and no Docker daemon in the unit lane. But every one of these invariants regresses in
the same silent way application code does, and each of them was arrived at by being wrong
first:

  * a service unit that breaks the Docker CLI still *starts*, so the failure shows up as a
    missed backup at 02:37 rather than as a red test;
  * a nightly timer routed through the root update agent would stop taking backups the day
    the agent copy went stale, which is the one job that has to keep working while nobody
    is looking;
  * and the retention rule "remove only what this script wrote" is one line of regex away
    from quietly deleting the pre-migration dumps an upgrade depends on.

The companion behavioural tests are in scripts/tests/test-backup.sh, which runs the real
script against a real Postgres. These are the claims that only exist as text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install-updater.sh"
AGENT = REPO / "scripts" / "saiva-update-agent"
BACKUP = REPO / "scripts" / "backup.sh"


def _units(text: str) -> dict[str, str]:
    """Every `cat > "$UNIT_DIR/<name>" <<EOF ... EOF` block the installer writes.

    Read out of the installer rather than from a fixture directory, because the installer
    IS the only place these exist — there is no units/ directory to drift from. The
    variables are left unexpanded; the assertions below match on the literal text, which
    is what a reader of the installed file would see modulo two substitutions.
    """
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'cat > "\$UNIT_DIR/([a-z.\-]+)" <<EOF\n(.*?)\nEOF\n', text, re.S)
    }


@pytest.fixture(scope="module")
def units() -> dict[str, str]:
    found = _units(INSTALLER.read_text())
    assert found, "no systemd units found in the installer; this whole file is then vacuous"
    return found


EXPECTED = {
    "saiva-update.service",
    "saiva-update.path",
    "saiva-backup.service",
    "saiva-backup.timer",
    "saiva-backup-request.service",
    "saiva-backup-request.path",
}


def test_the_installer_writes_every_unit_the_regime_needs(units: dict[str, str]) -> None:
    missing = EXPECTED - set(units)
    assert not missing, f"the installer no longer writes {sorted(missing)}"


def test_the_nightly_timer_actually_recurs_and_survives_a_reboot(units: dict[str, str]) -> None:
    timer = units["saiva-backup.timer"]
    assert "OnCalendar=" in timer
    # Persistent is the difference between "a box that was off overnight catches up" and
    # "a box that was off overnight silently never backed up". On a home LXC the second is
    # the common case, and it is invisible: the age on the Settings page is the only thing
    # that would ever have said so.
    assert "Persistent=true" in timer, (
        "without Persistent=true a container that was shut down at the scheduled time "
        "simply skips the night"
    )
    assert "RandomizedDelaySec=" in timer
    assert "WantedBy=timers.target" in timer


def test_the_nightly_backup_does_not_go_through_the_root_update_agent(
    units: dict[str, str],
) -> None:
    """The one job that must work unattended must not depend on the one thing that goes stale.

    scripts/saiva-update-agent is deliberately a COPY, so that a release cannot change what
    runs as root. The cost is that it drifts from the checkout until somebody re-runs the
    installer. Routing the nightly backup through it would mean that drift stops backups —
    silently, at 02:37, on the box nobody is looking at.
    """
    service = units["saiva-backup.service"]
    assert "scripts/backup.sh" in service
    assert "--trigger timer" in service
    assert "saiva-update-agent" not in service, (
        "the nightly backup must run scripts/backup.sh directly; through the agent copy it "
        "stops the day that copy goes stale"
    )
    assert "Type=oneshot" in service
    assert re.search(r"TimeoutStartSec=\d+", service), (
        "a hung backup must not hold the deploy lock forever"
    )


def test_the_button_path_unit_watches_the_second_drop_box(units: dict[str, str]) -> None:
    path = units["saiva-backup-request.path"]
    # A glob, not a fixed filename: the api writes a uniquely named request so two presses
    # cannot collide, and the agent removes everything it finds, so the unit cannot be left
    # re-triggering on an entry it refused to act on.
    assert "PathExistsGlob=" in path and "/backup-requests/*" in path
    assert "Unit=saiva-backup-request.service" in path
    # And THIS one goes through the agent, because something has to consume the request
    # without reading it before the work starts.
    assert "--backup" in units["saiva-backup-request.service"]


# Directives that leave the unit starting happily and the work broken. Each is here because
# it is the one a reflex would add.
HARDENING_THAT_BREAKS_DOCKER = {
    # A host path handed to `docker run -v` is resolved in the DAEMON's mount namespace, so
    # PrivateTmp silently mounts the real /tmp/x instead of the unit's private one. A wrong
    # directory, not an error.
    "PrivateTmp=true": "docker resolves -v sources in the daemon's namespace",
    # /var/lib/saiva would be read-only and the status file could not be written — which is
    # the only way the Settings page learns anything.
    "ProtectSystem=strict": "the status file lives under /var/lib/saiva",
    # Hides /home and /root. The checkout is at /opt/saiva on the documented setup and at
    # /home/user/saiva in this repository's own sandbox.
    "ProtectHome=true": "a checkout under /home or /root would vanish",
    "PrivateNetwork=true": "breaks the off-box copy and any image pull",
    "IPAddressDeny=any": "breaks the off-box copy and any image pull",
    # Membership of the docker group IS root, so a non-root user here would be theatre with
    # a side of breakage.
    "DynamicUser=yes": "no docker socket access without the docker group, which is root",
}


def _directives(body: str) -> str:
    """The unit's actual settings, with its comments removed.

    The units name several of the directives below in a comment explaining why they are
    absent, which is worth having and would otherwise make this test fail on the very
    explanation that keeps somebody from adding them.
    """
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("directive,why", sorted(HARDENING_THAT_BREAKS_DOCKER.items()))
def test_the_backup_units_avoid_hardening_that_silently_breaks_docker(
    units: dict[str, str], directive: str, why: str
) -> None:
    for name in ("saiva-backup.service", "saiva-backup-request.service"):
        assert directive not in _directives(units[name]), (
            f"{name} sets {directive}, which {why}"
        )


def test_the_backup_units_do_carry_the_hardening_that_is_safe(units: dict[str, str]) -> None:
    """A test that only forbids things would be satisfied by a unit with no hardening at all."""
    service = units["saiva-backup.service"]
    for directive in (
        "NoNewPrivileges=true",
        "ProtectKernelTunables=true",
        "RestrictSUIDSGID=true",
    ):
        assert directive in service, f"saiva-backup.service no longer sets {directive}"
    # RestrictAddressFamilies without AF_UNIX kills the docker socket outright. Allowed, but
    # only with it.
    for name, body in units.items():
        for line in body.splitlines():
            if line.startswith("RestrictAddressFamilies="):
                assert "AF_UNIX" in line, f"{name}: {line.strip()} removes the docker socket"


# --- the installer ----------------------------------------------------------------------


def test_the_installer_creates_both_drop_boxes_and_neither_is_owned_by_the_container() -> None:
    text = INSTALLER.read_text()
    lines = [ln.strip() for ln in text.splitlines() if "install -d" in ln or "chown" in ln]
    assert lines, "no directory-creating lines found; this test would pass on an empty file"
    for box in ("requests", "backup-requests"):
        assert any("0730" in ln and box in ln for ln in lines), (
            f"{box} is no longer created 0730"
        )
        # root:<api-uid>, never <api-uid>:<api-uid>. Owning a directory means being able to
        # unlink and rename root-owned entries inside it, which is how the first version of
        # this let a network-facing container make root open files of its choosing.
        assert any(re.search(r'chown\s+"?root:', ln) and box in ln for ln in lines), (
            f"{box} must stay root-owned with the api uid only as its GROUP"
        )


def test_the_installer_does_not_clobber_a_configured_destination() -> None:
    """Re-running the installer is the documented fix for half a dozen problems.

    If it rewrote /etc/saiva/agent.conf from the template, every one of those fixes would
    silently throw away the off-box destination the owner had configured — and the page
    would then go from "copied off this machine" to "nothing is copied off this box",
    which is a sentence nobody would connect to having run the installer.
    """
    text = INSTALLER.read_text()
    assert 'if [ -f "$CONF" ]; then' in text, (
        "the installer no longer checks for an existing agent.conf before writing one"
    )
    assert "already present, left as it is" in text


def test_the_installer_says_so_when_no_off_box_copy_is_configured() -> None:
    text = INSTALLER.read_text()
    assert "NO OFF-BOX COPY" in text, (
        "the default state of a fresh install is every backup on the disk it protects; "
        "the installer is the one moment somebody is reading its output"
    )


def test_the_state_directory_has_one_authority_and_it_is_dot_env() -> None:
    """.env is what docker-compose interpolates the bind mounts from, so it is the only
    answer the containers can see. The installer, the agent and scripts/lib/state.sh must
    all read it from there.

    They used not to. The agent preferred /etc/saiva/agent.conf, and the first time the two
    disagreed it looked for a status file in the wrong place, found nothing changed, and
    overwrote a good "backed up and verified" with "the backup did not start".
    """
    for path in (INSTALLER, AGENT):
        text = path.read_text()
        assert "state_dir_from_env" in text, (
            f"{path.name} no longer resolves the state directory from .env"
        )
        assert "SAIVA_STATE_DIR=" in text


# --- the retention rule -----------------------------------------------------------------


def test_the_prune_can_only_match_dumps_the_backup_script_itself_wrote() -> None:
    """The one line in this regime that deletes anything.

    `-premigrate` dumps are what an upgrade is rolled back with and `-prerestore` dumps are
    what a mistaken restore is undone with. Neither was written by this script, so neither
    is this script's to remove — and widening the pattern by one character is all it would
    take. scripts/tests/test-backup.sh proves the behaviour; this catches the edit.
    """
    text = BACKUP.read_text()
    # Scoped to the prune function. Other readers in this file deliberately match any kind
    # of dump -- newest_dump() re-verifies whatever is most recent, and survey_dumps()
    # counts everything -- so a whole-file grep would be testing the wrong lines.
    body = re.search(r"\nprune_nightlies\(\) \{(.*?)\n\}\n", text, re.S)
    assert body, "prune_nightlies() is gone or has been renamed"
    patterns = re.findall(
        r'grep -E "\^saiva-\[0-9\]\{8\}-\[0-9\]\{6\}-([^"]*)"', body.group(1)
    )
    assert patterns, "the prune's name pattern is gone or has changed shape"
    for pattern in patterns:
        assert pattern.startswith("nightly"), (
            f"the prune matches {pattern!r}, which is not restricted to nightly dumps"
        )
    assert "rm -f --" in text, "the prune must name files explicitly, never a glob"
    # Deleting the backups to make room for one that has not been verified yet is not a
    # trade this script makes, and the refusal has to stay a refusal.
    assert "SAIVA_BACKUP_KEEP_NIGHTLY" in text
    assert "KEEP_NIGHTLY=1" in text, "retention must never be allowed to reach zero"


def test_a_half_written_dump_cannot_be_listed_as_a_usable_one() -> None:
    text = BACKUP.read_text()
    assert 'DUMP_PART=".${DUMP_NAME}.part"' in text, (
        "the partial dump must be written under a name that the listing's *.sql.gz glob "
        "cannot match -- both the leading dot and the .part suffix exclude it, and either "
        "alone would do, which is the point"
    )


# --- the seams between the three languages ----------------------------------------------
#
# Each of these holds two files together that nothing else does. They are here rather than
# in test_deployment_hardening.py because they are about the host/API boundary specifically,
# and because each was identified as a real gap by mutation-testing the tests themselves.

STATE_LIB = REPO / "scripts" / "lib" / "state.sh"


def test_every_field_the_host_writes_reaches_the_schema() -> None:
    """A field added to the shell renderer and not to BackupStatus never reaches the page.

    It would not fail anything either: FastAPI's response_model drops what it does not
    know, silently, so the host would be recording a fact and the owner would never see
    it. That is the quiet half of this regime's one failure mode — the page not saying
    something it could have said.
    """
    from app import schemas

    body = re.search(r"SAIVA_BACKUP_STATUS_FIELDS=\((.*?)\)", STATE_LIB.read_text(), re.S)
    assert body, "SAIVA_BACKUP_STATUS_FIELDS is gone or has changed shape"
    written = {
        token
        for line in body.group(1).splitlines()
        for token in line.split("#", 1)[0].split()
    }
    assert len(written) > 10, f"only found {written}; the regex is matching the wrong thing"

    missing = sorted(written - set(schemas.BackupStatus.model_fields))
    assert not missing, (
        f"scripts/lib/state.sh writes {missing}, which BackupStatus does not carry, so the "
        "page can never show it"
    )


def test_the_api_stops_believing_a_running_backup_before_systemd_kills_it() -> None:
    """Two numbers in two languages that have to stay in step.

    The API ages out a status file stuck at `running` so the button is not refused
    forever — a host killed mid-dump, a power cut, an OOM. That bound is only correct if
    it is LONGER than the longest run systemd will allow: shorter, and a slow but healthy
    verify gets reported as a run nobody came back to, right in the middle of working
    properly.
    """
    from app.services import backups, updates

    units = _units(INSTALLER.read_text())
    for unit, module, label in (
        ("saiva-backup.service", backups, "backups"),
        ("saiva-update.service", updates, "updates"),
    ):
        timeout = re.search(r"TimeoutStartSec=(\d+)", units[unit])
        assert timeout, f"{unit} no longer sets TimeoutStartSec"
        systemd_seconds = int(timeout.group(1))
        api_seconds = module.STALE_RUN_AFTER.total_seconds()
        assert api_seconds > systemd_seconds, (
            f"{label}.STALE_RUN_AFTER is {api_seconds:.0f}s but {unit} lets a run take "
            f"{systemd_seconds}s, so a healthy slow run would be reported as abandoned"
        )
        # And not absurdly longer: the point of the bound is that there IS a way back.
        assert api_seconds < systemd_seconds * 2, (
            f"{label}.STALE_RUN_AFTER is more than twice {unit}'s timeout, which leaves "
            "the button refused long after systemd has given up"
        )


def test_every_setting_the_owner_sets_in_dot_env_actually_reaches_the_api_container() -> None:
    """A setting the image cannot see is a policy with no effect.

    There is no `env_file:` on any service and backend/Dockerfile copies no .env, so a
    value the owner writes in .env reaches the api process only if docker-compose.prod.yml
    lists it in that service's `environment:` block. SAIVA_BACKUP_STALE_HOURS was not
    listed: the page silently used the code default, and the number in .env did nothing at
    all. Nothing failed, which is what made it worth a test.

    Scoped to the settings whose names say they come from the deployment -- the ones read
    through an explicit SAIVA_ alias. Everything else on Settings is either passed under a
    different name already (DATABASE_URL, SECRET_KEY) or is not an operator knob.
    """
    import yaml

    from app.config import Settings

    aliased = {
        str(field.validation_alias)
        for field in Settings.model_fields.values()
        if isinstance(field.validation_alias, str)
        and field.validation_alias.startswith("SAIVA_")
    }
    assert aliased, "no SAIVA_-aliased settings found; this test would pass on anything"

    compose = yaml.safe_load((REPO / "docker-compose.prod.yml").read_text())
    api_env = compose["services"]["api"].get("environment") or {}
    passed = set(api_env) if isinstance(api_env, dict) else {e.split("=", 1)[0] for e in api_env}

    missing = sorted(aliased - passed)
    assert not missing, (
        f"docker-compose.prod.yml does not pass {missing} to the api service, so whatever "
        "the owner sets for them in .env has no effect on the running app"
    )


def test_every_copy_command_example_is_quoted_so_the_agent_can_source_it() -> None:
    """The one defect here that would have taken out both in-app buttons.

    /etc/saiva/agent.conf is shared: scripts/backup.sh READS the keys it wants, and
    scripts/saiva-update-agent SOURCES the whole file as root. An unquoted
    `SAIVA_BACKUP_COPY_CMD=rclone copyto "$1" ...` is therefore parsed as shell -- `rclone`
    becomes the value, `copyto` becomes a command to run, `$1` expands to nothing, and
    under `set -e` the agent dies. The backup itself would have kept working, which is
    exactly how such a line survives a test: only the buttons break.

    So every example anybody could copy has to be valid as a sourced assignment. Checked
    in the installer's template and in the runbook, because those are the two places a
    value gets copied from.
    """
    sources = {
        "scripts/install-updater.sh": INSTALLER.read_text(),
        "docs/BACKUPS.md": (REPO / "docs" / "BACKUPS.md").read_text(),
    }
    found = 0
    for where, text in sources.items():
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if not stripped.startswith("SAIVA_BACKUP_COPY_CMD="):
                continue
            value = stripped.split("=", 1)[1]
            if not value:  # the empty default in the template
                continue
            found += 1
            assert value.startswith("'") and value.endswith("'"), (
                f"{where} shows `{stripped}` as an example; unquoted, the update agent "
                "dies sourcing it and both in-app buttons stop working"
            )
    assert found, (
        "no SAIVA_BACKUP_COPY_CMD example found in either place -- this test would then "
        "pass however the examples were written"
    )


def test_an_upgrade_adds_the_backup_settings_to_an_existing_config() -> None:
    """The box this ships to already has an agent.conf, written by the previous version.

    The installer must not rewrite it -- that would throw away a configured destination,
    and re-running the installer is the documented fix for half a dozen other things. But
    leaving it entirely alone meant an upgraded box had nowhere to put a destination and
    nothing saying so: the page would report "nothing is copied off this box" forever and
    the owner would have no file to edit.
    """
    text = INSTALLER.read_text()
    assert "added the nightly-backup settings" in text, (
        "the installer no longer backfills the backup keys into an existing agent.conf, "
        "so an upgraded box has nowhere to configure a destination"
    )
    assert "the existing values were left alone" in text
    for key in ("SAIVA_BACKUP_COPY_TO", "SAIVA_BACKUP_COPY_CMD", "SAIVA_BACKUP_KEEP_NIGHTLY"):
        assert text.count(key) >= 2, (
            f"{key} should appear in both the fresh template and the upgrade backfill"
        )
