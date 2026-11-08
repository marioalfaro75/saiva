#!/usr/bin/env python3
"""Every workflow file parses as YAML.

A workflow that does not parse is not a failing run -- GitHub reports it on a page nobody
opens and the job simply never happens, which on the release path means a gate that looks
present and is not. Cheap to check, and checked from a file rather than inline in the
shell because quoting a nested Python one-liner through bash is how the check itself
ended up broken the first time.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"


def main() -> int:
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not paths:
        print(f"no workflows found under {WORKFLOWS}, so this check proves nothing")
        return 1
    bad = []
    for path in paths:
        try:
            if yaml.safe_load(path.read_text()) is None:
                bad.append(f"{path.name}: parses to nothing")
        except Exception as exc:  # noqa: BLE001 - any parse failure is the answer
            bad.append(f"{path.name}: {exc}")
    if bad:
        print("\n".join(bad))
        return 1
    print(f"{len(paths)} workflows parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
