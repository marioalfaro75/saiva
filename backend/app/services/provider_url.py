"""Deciding whether a configured AI provider URL may be requested.

The provider endpoint is user-supplied, and the server makes the request — so without
a check the app will fetch whatever an account holder names, from wherever the API
container can reach. Inside a Compose deployment that is the other containers, the
host's loopback, and on a hosted box the cloud metadata service. The responses and
error text come back through the chat reply, which makes it a readable probe rather
than a blind one.

Resolved rather than pattern-matched: rejecting "127.0.0.1" by string is easy to walk
around with a hostname that resolves there instead.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class ProviderUrlError(ValueError):
    """The configured provider URL may not be requested."""


def _is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 — cloud metadata lives here
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolved_addresses(host: str) -> list[str]:
    """Every address the host resolves to. Separate so tests can substitute it."""
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as exc:
        raise ProviderUrlError(f"Could not resolve {host}") from exc


def check(raw: str | None, *, allow_local: bool = False) -> None:
    """Raise ProviderUrlError unless this URL may be requested.

    `allow_local` is for the local-model case — Ollama, LM Studio and the like run on
    loopback or the LAN, and refusing those would make self-hosted inference
    impossible. It is the caller's job to only pass it when the user has asked for a
    local provider, not to let it default on.
    """
    if not raw:
        return  # unset means the provider's own default endpoint, which is public

    url = urlparse(raw.strip())
    if url.scheme not in {"http", "https"}:
        raise ProviderUrlError("Provider URL must start with http:// or https://")
    if not url.hostname:
        raise ProviderUrlError("Provider URL must include a host")
    if allow_local:
        return
    if url.scheme != "https":
        raise ProviderUrlError("Provider URL must use https")

    for address in resolved_addresses(url.hostname):
        if _is_internal(ipaddress.ip_address(address)):
            raise ProviderUrlError(
                f"Provider URL resolves to {address}, which is inside your own network. "
                "Point it at the provider's public endpoint."
            )
