"""Software-update helpers: check GitHub for a newer release, ask the host to apply one,
and read back what the host said happened.

The api process does not apply updates and cannot. It writes one file into a bind-mounted
directory and reads another back; a root-owned agent on the host does the work
(scripts/saiva-update-agent). That is not merely a tidier arrangement than the Watchtower
sidecar it replaces -- it is what makes the outcome reportable at all.

The old shape had this process trigger its own replacement, so the process that would
write the response was the process being killed. `trigger_watchtower` wrapped the call in
`except Exception: return` and read neither the status nor the body; the endpoint returned
the literal string "update_started"; and the panel polled for four minutes and then
rendered "Update applied." whether or not anything had been applied. Three layers, each
independently discarding the failure -- and no amount of editing any one of them could
have fixed it, because a process killed partway through its own update cannot report on
it. The status now lives in a file on the host, so the answer outlives the container that
asked for it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_GITHUB_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
_HTTP_TIMEOUT = 6.0
_CACHE_TTL = 6 * 3600  # seconds


def parse_version(value: str) -> tuple[int, int, int] | None:
    """Parse a 'v1.2.3'-style version into a comparable tuple; None if not semver."""
    core = value.strip().lstrip("vV").split("-")[0].split("+")[0]
    if not core:
        return None
    try:
        nums = [int(p) for p in core.split(".")[:3]]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def is_newer(latest: str, current: str) -> bool:
    latest_v, current_v = parse_version(latest), parse_version(current)
    if latest_v is None or current_v is None:
        return False
    return latest_v > current_v


# The repository name is not settable through the API — it comes from the process
# environment — so nothing here is reachable by a request. What this guard removes is the
# quieter failure: `urllib` honours `file://`, so one wrong line in a .env would read a
# local file instead of asking GitHub.
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _safe_repo(repo: str) -> str | None:
    """`owner/name` and nothing else — no path traversal into another URL."""
    repo = (repo or "").strip()
    return repo if _REPO_NAME.match(repo) else None


@dataclass
class Release:
    tag: str
    url: str
    published_at: str
    notes: str


def fetch_latest_release(repo: str) -> Release | None:
    """Fetch the latest GitHub release for `repo`. Returns None on any failure."""
    safe = _safe_repo(repo)
    if safe is None:
        return None
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
    request = urllib.request.Request(
        _GITHUB_LATEST.format(repo=safe),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "saiva-update-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            data = json.load(resp)
    except Exception:  # update checks must never break the app
        return None
    return Release(
        tag=str(data.get("tag_name") or ""),
        url=str(data.get("html_url") or ""),
        published_at=str(data.get("published_at") or ""),
        notes=str(data.get("body") or "")[:4000],
    )


_cache: dict[str, object] = {"ts": 0.0, "release": None}


def latest_release_cached(repo: str, force: bool = False) -> Release | None:
    now = time.time()
    cached = _cache["release"]
    if not force and isinstance(cached, Release) and now - float(_cache["ts"]) < _CACHE_TTL:  # type: ignore[arg-type]
        return cached
    release = fetch_latest_release(repo)
    if release is not None:
        _cache["release"] = release
        _cache["ts"] = now
    return release


# --- asking the host to apply an update, and reading back what it did ----------------

REQUEST_FILENAME = "request"
STATUS_FILENAME = "status.json"

# Phases the host updater can report. Kept here so the API and the panel agree on the
# vocabulary, and so an unrecognised one is visible rather than silently rendered as
# something reassuring.
TERMINAL_PHASES = frozenset(
    {"applied", "failed", "rolled_back", "rolled_back_code_only", "rollback_failed"}
)
ACTIVE_PHASES = frozenset({"requested", "running"})


def update_dir() -> Path | None:
    """The bind-mounted directory shared with the host updater, if one is configured."""
    raw = os.environ.get("SAIVA_UPDATE_DIR", "").strip()
    return Path(raw) if raw else None


def apply_available() -> bool:
    """Whether pressing the button can actually do anything.

    False is a perfectly good answer -- a box updated with `make update` over SSH never
    configures this -- and the panel says so plainly rather than offering a button that
    would do nothing. What it must never do is claim an ability it cannot verify, which
    is what the old test (`bool(watchtower_url and watchtower_token)`) did: both came
    from a .env this container could check nothing about.
    """
    directory = update_dir()
    return bool(directory and directory.is_dir() and os.access(directory, os.W_OK))


def request_update(requested_by: str) -> str:
    """Write the request file. Returns the request id.

    The CONTENTS are a note for the host's log and nothing else. The agent deletes this
    file without reading anything out of it that could select what gets deployed: it
    resolves the target release itself, from the tags on origin. So the most this
    container can ask for is "an update", never "this code" -- which is the line between
    a convenience feature and a path from a network-facing container to root.
    """
    directory = update_dir()
    if directory is None:
        raise RuntimeError("SAIVA_UPDATE_DIR is not set in this deployment")
    request_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    # Written then renamed, so the host's path unit cannot fire on a half-written file.
    tmp = directory / f".{REQUEST_FILENAME}.tmp"
    tmp.write_text(
        f"requested_at={request_id} requested_by={requested_by}\n", encoding="utf-8"
    )
    tmp.replace(directory / REQUEST_FILENAME)
    return request_id


def read_update_status() -> dict[str, Any] | None:
    """The host updater's record of the last run, or None if there has never been one."""
    directory = update_dir()
    if directory is None:
        return None
    try:
        with (directory / STATUS_FILENAME).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - a malformed status must not break the settings page
        return {"phase": "unreadable", "message": "the updater's status file could not be read"}
    return payload if isinstance(payload, dict) else None


def update_in_progress() -> bool:
    """True while a request is queued or a run is under way."""
    directory = update_dir()
    if directory is not None and (directory / REQUEST_FILENAME).exists():
        return True
    return (read_update_status() or {}).get("phase") in ACTIVE_PHASES
