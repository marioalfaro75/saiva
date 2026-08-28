#!/usr/bin/env python3
"""Fail if backend/requirements.lock no longer matches backend/pyproject.toml.

The lockfile is what the production image actually installs, under
`--require-hashes`. pyproject.toml is what a human edits. Nothing links the two
automatically, so without this check the usual failure is silent: someone adds a
dependency or raises a floor, CI installs from pyproject and goes green, and the
image keeps shipping the old resolution — or fails to build hours later.

Run from anywhere:  python scripts/check_lockfile.py
Regenerate the lock: see backend/requirements.lock's own header.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "backend" / "pyproject.toml"
LOCKFILE = ROOT / "backend" / "requirements.lock"

# `name==version` at the start of a line; pip-compile puts hashes on continuations.
PINNED = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s\;]+)")


def locked_versions(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = PINNED.match(line)
        if match:
            found[canonicalize_name(match["name"])] = match["version"]
    return found


def main() -> int:
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    locked = locked_versions(LOCKFILE.read_text())

    problems: list[str] = []
    for raw in declared:
        req = Requirement(raw)
        name = canonicalize_name(req.name)
        version = locked.get(name)
        if version is None:
            problems.append(f"{req.name}: declared in pyproject.toml, missing from the lockfile")
        elif not req.specifier.contains(version, prereleases=True):
            problems.append(
                f"{req.name}: lockfile has {version}, which does not satisfy '{req.specifier}'"
            )

    if problems:
        print("requirements.lock is out of date:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nRegenerate it with the command in the header of backend/requirements.lock.",
            file=sys.stderr,
        )
        return 1

    print(f"requirements.lock satisfies all {len(declared)} declared dependencies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
