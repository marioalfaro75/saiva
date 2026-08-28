"""Work out who is actually calling, behind a reverse proxy.

`request.client.host` is the address of the immediate peer. In the shipped
deployment that peer is always Caddy, so every request appeared to come from one
address: the login rate limit was a single shared bucket that any one visitor
could exhaust for everybody, and every row in the audit log recorded the proxy's
container IP instead of the person who did the thing.

`X-Forwarded-For` carries the real address, but a client can send that header
itself, so it is only worth reading when the peer is a proxy we put there.
"""

from __future__ import annotations

import ipaddress
import socket
import time

from fastapi import Request

from .config import get_settings

settings = get_settings()

# Resolved trusted-proxy addresses, refreshed periodically: in Compose the proxy is
# a service name whose container IP changes when it is recreated.
_RESOLVE_TTL_SECONDS = 60.0
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address
_resolved: tuple[float, frozenset[_Address]] = (0.0, frozenset())
_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
_hostnames: list[str] = []


def _parse_setting(raw: str) -> None:
    """Split the setting into literal networks and names needing DNS."""
    _networks.clear()
    _hostnames.clear()
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        try:
            _networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            _hostnames.append(entry)


_parse_setting(settings.trusted_proxies)


def _resolved_hostnames() -> frozenset[_Address]:
    global _resolved
    if not _hostnames:
        return frozenset()
    cached_at, addresses = _resolved
    now = time.monotonic()
    if now - cached_at < _RESOLVE_TTL_SECONDS:
        return addresses
    found: set[_Address] = set()
    for name in _hostnames:
        try:
            for info in socket.getaddrinfo(name, None):
                found.add(ipaddress.ip_address(info[4][0]))
        except (OSError, ValueError):
            # A proxy that will not resolve is simply not trusted this minute; the
            # peer address is still a usable, if coarse, key.
            continue
    addresses = frozenset(found)
    _resolved = (now, addresses)
    return addresses


def is_trusted_proxy(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if any(ip in network for network in _networks):
        return True
    return ip in _resolved_hostnames()


def client_ip(request: Request) -> str | None:
    """The caller's address, reading X-Forwarded-For only from a proxy we trust.

    Takes the *rightmost* entry, which is the one our own proxy appended. Everything
    to its left was supplied by the caller and can say anything at all — reading the
    leftmost entry is the usual way a forwarded-header rate limit becomes no rate
    limit, because every request can claim a fresh address.
    """
    peer = request.client.host if request.client else None
    if peer is None or not is_trusted_proxy(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    return hops[-1] if hops else peer
