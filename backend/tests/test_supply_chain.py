"""Supply-chain invariants for the build and release pipeline.

None of this is reachable from a request, which is exactly why it needs a test:
a mutable action tag, an unpinned dependency or an ungated release path is
invisible in review and stays broken until someone else exploits it. The
tj-actions/changed-files compromise (CVE-2025-30066) worked precisely because
thousands of repositories referenced `@v35` and got whatever that tag pointed at
this morning.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
DOCKERFILE = REPO / "backend" / "Dockerfile"
LOCKFILE = REPO / "backend" / "requirements.lock"
PYPROJECT = REPO / "backend" / "pyproject.toml"

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)", re.MULTILINE)
SHA = re.compile(r"^[0-9a-f]{40}$")
PINNED_LINE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;]+)")


def test_the_workflow_directory_was_actually_found() -> None:
    """Guards every parametrised test below: an empty glob passes vacuously."""
    assert len(WORKFLOWS) >= 4, f"expected the workflow files, found {WORKFLOWS}"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_third_party_action_is_pinned_to_a_commit(workflow: Path) -> None:
    """`uses: someone/action@v4` runs whatever that tag points at right now.

    A tag is a mutable pointer the action's owner (or anyone who compromises them)
    can move. These workflows hold `packages: write` and GITHUB_TOKEN, so a moved
    tag publishes an attacker's image under our name.
    """
    for ref in USES.findall(workflow.read_text()):
        if ref.startswith("./"):
            continue  # a reusable workflow in this repo, pinned by the commit itself
        assert "@" in ref, f"{workflow.name}: '{ref}' has no version at all"
        _, _, version = ref.partition("@")
        assert SHA.match(version), (
            f"{workflow.name}: '{ref}' is pinned to a mutable tag. "
            "Pin it to the 40-character commit SHA and put the tag in a trailing comment."
        )


def test_the_image_installs_only_from_the_hash_pinned_lockfile() -> None:
    """Ranges in pyproject.toml resolve fresh on every rebuild; the lock does not."""
    # Comments mentioning the flag do not count — an earlier version of this test
    # searched the whole file and stayed green after the flag was taken off the
    # command, because the comment above it still said the word.
    commands = [
        line for line in DOCKERFILE.read_text().splitlines() if not line.lstrip().startswith("#")
    ]
    install = [line for line in commands if "pip install" in line or "requirements.lock" in line]
    assert install, "backend/Dockerfile no longer installs dependencies at all"
    joined = "\n".join(install)
    assert "--require-hashes" in joined, (
        "backend/Dockerfile must install with --require-hashes, or a rebuild will "
        f"silently pick up anything newly published inside the pyproject ranges:\n{joined}"
    )
    assert "-r requirements.lock" in joined
    assert not re.search(r"pip install\s+\.\s*$", joined, re.MULTILINE), (
        "a bare `pip install .` resolves dependencies from the unpinned ranges again"
    )


def test_every_locked_dependency_carries_a_hash() -> None:
    """--require-hashes only checks the hashes that are present."""
    lines = LOCKFILE.read_text().splitlines()
    unhashed = [
        line
        for index, line in enumerate(lines)
        if PINNED_LINE.match(line) and not line.rstrip().endswith("\\")
    ]
    assert not unhashed, f"pinned without a hash: {unhashed}"
    assert len([line for line in lines if PINNED_LINE.match(line)]) > 20


def test_the_lockfile_still_satisfies_pyproject() -> None:
    """Adding a dependency without regenerating the lock breaks the image, not CI."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    locked = {
        canonicalize_name(m["name"]): m["version"]
        for m in (PINNED_LINE.match(line) for line in LOCKFILE.read_text().splitlines())
        if m
    }
    for raw in tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]:
        req = Requirement(raw)
        version = locked.get(canonicalize_name(req.name))
        assert version is not None, f"{req.name} is declared but not locked"
        assert req.specifier.contains(version, prereleases=True), (
            f"{req.name}: locked at {version}, which does not satisfy '{req.specifier}'"
        )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_nothing_publishes_an_image_without_running_the_checks(workflow: Path) -> None:
    """A `v*` tag once reached `:latest` without running a single test.

    The edge channel was gated and the release channel — the one users pull — was
    not, so the least-tested images were the ones most likely to be deployed.
    """
    text = workflow.read_text()
    if "docker/build-push-action" not in text:
        return
    assert "./.github/workflows/checks.yml" in text, (
        f"{workflow.name} publishes an image but never calls the shared checks workflow"
    )
    assert re.search(r"needs:.*\bchecks\b", text), (
        f"{workflow.name} calls the checks but does not gate publishing on them"
    )


def test_the_dependency_audit_can_actually_fail_the_build() -> None:
    """`pip-audit || true` runs the audit, prints the findings and ships anyway."""
    checks = (REPO / ".github" / "workflows" / "checks.yml").read_text()
    audit_lines = [
        line for line in checks.splitlines() if "pip-audit" in line or "npm audit" in line
    ]
    assert audit_lines, "the checks workflow no longer runs a dependency audit"
    for line in audit_lines:
        assert "|| true" not in line, f"audit is non-blocking: {line.strip()}"
