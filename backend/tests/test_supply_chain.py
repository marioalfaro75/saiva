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
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
DOCKERFILE = REPO / "backend" / "Dockerfile"
RUNTIME_SOURCES = sorted((REPO / "backend" / "app").rglob("*.py")) + sorted(
    (REPO / "backend" / "migrations").rglob("*.py")
)
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


def _pushing_build_steps(workflow: Path) -> list[str]:
    """Steps that build an image and actually publish it.

    Keyed on whether the step pushes, not on whether it uses the build action: a job
    may build an image locally in order to scan it, which publishes nothing. Anything
    other than a literal `push: false` counts as pushing — an expression like
    `push: ${{ ... }}` is not something this test will reason about, so it is treated
    as a publish and has to be gated.
    """
    spec = yaml.safe_load(workflow.read_text())
    pushing = []
    for job_name, job in (spec.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            uses = str(step.get("uses") or "")
            if "docker/build-push-action" not in uses:
                continue
            push = (step.get("with") or {}).get("push")
            if push is not False:
                pushing.append(f"{job_name}:{step.get('name') or uses}")
    return pushing


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_nothing_publishes_an_image_without_running_the_checks(workflow: Path) -> None:
    """A `v*` tag once reached `:latest` without running a single test.

    The edge channel was gated and the release channel — the one users pull — was
    not, so the least-tested images were the ones most likely to be deployed.
    """
    pushing = _pushing_build_steps(workflow)
    if not pushing:
        return
    text = workflow.read_text()
    assert "./.github/workflows/checks.yml" in text, (
        f"{workflow.name} publishes an image ({', '.join(pushing)}) but never calls "
        "the shared checks workflow"
    )
    assert re.search(r"needs:.*\bchecks\b", text), (
        f"{workflow.name} calls the checks but does not gate publishing on them"
    )


def test_a_local_build_for_scanning_is_not_treated_as_a_publish() -> None:
    """The precision the test above depends on.

    security-scan.yml builds both images to scan their OS packages and pushes
    neither. Keying the gate on the action's presence rather than on `push` made that
    job look like an ungated release — and the obvious way to make the suite green
    again would have been to loosen the gate that protects the real one.
    """
    scan = REPO / ".github" / "workflows" / "security-scan.yml"
    assert "docker/build-push-action" in scan.read_text(), (
        "this test is pointless if security-scan.yml no longer builds anything"
    )
    assert _pushing_build_steps(scan) == []


def test_the_dependency_audit_can_actually_fail_the_build() -> None:
    """`pip-audit || true` runs the audit, prints the findings and ships anyway."""
    checks = (REPO / ".github" / "workflows" / "checks.yml").read_text()
    audit_lines = [
        line for line in checks.splitlines() if "pip-audit" in line or "npm audit" in line
    ]
    assert audit_lines, "the checks workflow no longer runs a dependency audit"
    for line in audit_lines:
        assert "|| true" not in line, f"audit is non-blocking: {line.strip()}"


# --------------------------------------------------------------------------------
# The packaging toolchain. Everything below exists because the first run of the
# `images` job in security-scan.yml reported ten fixable CVEs in the API image and
# every one of them was in pip or setuptools rather than in anything this project
# depends on. Both vendor their own third-party code — pip pins msgpack, urllib3 and
# setuptools inside itself; that setuptools pins wheel and jaraco.context inside
# itself — so none of those versions appears in requirements.lock, pip-audit cannot
# see them, Dependabot cannot bump them, and upgrading pip cannot fix them because
# the current pip is what pins them. Not shipping either package is the whole fix.


def _instructions(dockerfile: Path) -> list[str]:
    """Each Dockerfile instruction as one line, `\\` continuations joined.

    Comment lines are dropped first, deliberately. The tests below assert that a
    package is *removed* and that the app is *run*, and the comments explaining why
    must not be the things that satisfy them — the same trap
    test_the_image_installs_only_from_the_hash_pinned_lockfile above documents having
    already been caught by once.
    """
    instructions: list[str] = []
    current = ""
    for line in dockerfile.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = f"{current} {stripped}" if current else stripped
        if current.endswith("\\"):
            current = current[:-1]
            continue
        instructions.append(" ".join(current.split()))
        current = ""
    if current:
        instructions.append(" ".join(current.split()))
    return instructions


def _uninstalled_packages(layer: str) -> set[str]:
    """The package names handed to `pip uninstall` in one joined RUN instruction.

    Arguments only, never the command text. `pip uninstall -y setuptools wheel`
    contains the word "pip" because that is the program being run, so a test that
    searched the command would report pip as removed by the very command that keeps
    it — green on a strip that still ships seven of the ten advisories, five of them
    HIGH. That is not hypothetical: it is the first version of this test, and
    forgetting the last word of the argv is the likeliest way to reintroduce it.
    """
    removed: set[str] = set()
    for command in re.split(r"&&|\|\||;", layer):
        words = command.split()
        if "uninstall" not in words:
            continue
        removed.update(w for w in words[words.index("uninstall") + 1 :] if not w.startswith("-"))
    return removed


def test_the_image_does_not_ship_the_python_packaging_toolchain() -> None:
    """pip and setuptools are build-time tools with no runtime caller here.

    The removal has to live in the same instruction as the install. A later `RUN rm`
    makes the flattened image look clean to a scanner while the layer underneath
    still carries the files, and that layer is still pushed and still pullable.
    """
    installs = [i for i in _instructions(DOCKERFILE) if "-r requirements.lock" in i]
    assert len(installs) == 1, (
        f"expected exactly one instruction installing the lockfile, found {installs}"
    )
    layer = installs[0]
    removed = _uninstalled_packages(layer)
    for package in ("pip", "setuptools", "wheel"):
        assert package in removed, (
            f"backend/Dockerfile installs the lockfile and then ships {package}. "
            "Remove pip, setuptools and wheel with `pip uninstall` in this same "
            "instruction: a later layer hides the files without unshipping them, and "
            f"`rm -rf` orphans setuptools' distutils-precedence.pth.\n{layer}"
        )


def test_nothing_puts_the_packaging_toolchain_back_after_removing_it() -> None:
    """A reinstall below the strip undoes it without touching the line that strips.

    `pip install` after the removal would at least fail the build loudly, but
    `python -m ensurepip` succeeds and quietly restores an *older* pip and setuptools
    than the ones that were taken out.
    """
    instructions = _instructions(DOCKERFILE)
    strips = [index for index, inst in enumerate(instructions) if "pip uninstall" in inst]
    assert strips, "nothing removes the packaging toolchain any more"
    last = strips[-1]
    # Everything after the strip, plus whatever follows `uninstall` inside its own
    # instruction — `pip install --upgrade pip` legitimately precedes it in there.
    after = [*instructions[last + 1 :], instructions[last].split("uninstall", 1)[1]]
    for inst in after:
        for restores in ("pip install", "ensurepip", "get-pip"):
            assert restores not in inst, (
                f"backend/Dockerfile removes the packaging toolchain and then puts it "
                f"back with `{restores}`:\n{inst}"
            )


def test_the_build_runs_the_app_once_with_the_toolchain_already_gone() -> None:
    """Nothing else executes this image before Watchtower rolls it out unattended.

    The `images` job builds amd64 and only reads the filesystem — it never runs the
    container, and it sets no exit-code, so it cannot fail a build either. The release
    build publishes arm64 under QEMU, which nothing scans and nothing runs. So this
    one RUN is the whole of the evidence that the strip did not take something the app
    imports, and the only evidence that covers both architectures. Without it, the
    first execution of a stripped image is a family's container at 3am.
    """
    instructions = _instructions(DOCKERFILE)
    strips = [i for i in instructions if "pip uninstall" in i]
    assert strips, "nothing removes the packaging toolchain any more"
    smoke = [i for i in instructions if i.startswith("RUN ") and "import app.main" in i]
    assert smoke, (
        "backend/Dockerfile no longer imports the app during the build. Restore the "
        '`RUN python -c "import app.main, app.dbinit"` step: without it nothing '
        "executes this image until a user's container does."
    )
    assert instructions.index(smoke[-1]) > instructions.index(strips[-1]), (
        "the build-time import runs before the toolchain is removed, so it proves the "
        "app imports in an image we do not ship"
    )
    for package in ("pip", "setuptools"):
        assert package in smoke[-1], (
            f"the build-time check no longer verifies {package} is gone, so a strip "
            f"that quietly became a no-op would still go green:\n{smoke[-1]}"
        )


def test_no_runtime_module_imports_the_packaging_toolchain() -> None:
    """The source-level half of the strip, and the half that runs on every pull request.

    The Dockerfile tests above cannot see this: a change that makes the app import
    pkg_resources leaves the Dockerfile untouched, so the image scan is not even
    rebuilt (`image-inputs` in security-scan.yml filters on the Dockerfiles and the
    dependency manifests, never on app/**) and the strip only breaks at boot, in the
    image nobody runs until it is deployed. `pkg_resources` is the usual way in, for
    reading package metadata — importlib.metadata is the stdlib answer and ships free.
    """
    assert len(RUNTIME_SOURCES) > 50, (
        f"expected the backend sources, found {len(RUNTIME_SOURCES)} — this test "
        "passes vacuously if the glob stops matching"
    )
    imports = re.compile(
        r"^\s*(?:import|from)\s+(pkg_resources|setuptools|wheel|_distutils_hack|distutils)\b",
        re.MULTILINE,
    )
    offenders = [
        f"{path.relative_to(REPO)}: {match.group(0).strip()}"
        for path in RUNTIME_SOURCES
        for match in imports.finditer(path.read_text())
    ]
    assert not offenders, (
        "the runtime imports the build-time packaging toolchain, which the API image "
        "does not ship (see backend/Dockerfile). Use importlib.metadata instead of "
        "pkg_resources:\n" + "\n".join(offenders)
    )
