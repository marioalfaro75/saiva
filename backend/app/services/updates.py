"""Software-update helpers: check GitHub for the latest release and trigger an
in-place update via a locked-down Watchtower sidecar (PRD §15: the API never
touches the Docker socket itself)."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

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


# Neither of these URLs is settable through the API — both come from the process
# environment — so nothing here is reachable by a request. What these guards remove
# is the quieter failure: `urllib` honours `file://`, and `trigger_watchtower` sends
# a bearer token to whatever WATCHTOWER_URL says, so one wrong line in a .env either
# reads a local file or hands the update token to someone else's server.
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _safe_repo(repo: str) -> str | None:
    """`owner/name` and nothing else — no path traversal into another URL."""
    repo = (repo or "").strip()
    return repo if _REPO_NAME.match(repo) else None


def _http_url(raw: str) -> str | None:
    """An http(s) URL with a host, or nothing."""
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return raw


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


def trigger_watchtower(base_url: str, token: str) -> None:
    """Ask Watchtower to pull + recreate the labelled containers (api/web)."""
    safe = _http_url(base_url.rstrip("/"))
    if safe is None:
        return
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
    request = urllib.request.Request(
        f"{safe}/v1/update",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        urllib.request.urlopen(request, timeout=30)  # nosec B310
    except Exception:  # the API container is recreated mid-update; ignore
        return
