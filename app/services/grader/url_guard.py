"""SSRF guard for the grader's PDF fetches.

The grader downloads caller-supplied PDF URLs (marking scheme, questions, answer
sheets) and the ``/grader`` endpoints are public, so a caller could point the
fetch at an internal address — the cloud-metadata endpoint ``169.254.169.254``, a
``127.0.0.1`` service, or a private-range host. ``validate_public_http_url``
rejects any URL whose scheme is not HTTP(S) or whose host resolves to a private /
loopback / link-local / reserved / multicast / unspecified address. The fetch
layer calls it before the initial request AND before following each redirect, so
an external→internal redirect cannot bypass the check.

Known limitation: validation resolves DNS and then httpx connects separately, so a
caller controlling an authoritative DNS server could rebind the name between the
check and the connect (a TOCTOU window). Closing that fully needs IP-pinned
connections; for this threat model (an internal grading tool fronted by an edge
egress allowlist — see docs/grader-ec2-deployment.md §5) the resolve-and-validate
approach is the accepted trade-off.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}


def _is_blocked_ip(ip: str) -> bool:
    """True if ``ip`` falls in a range we refuse to fetch from (SSRF targets)."""
    addr = ipaddress.ip_address(ip)
    # An IPv4-mapped IPv6 address (e.g. ::ffff:127.0.0.1) reports is_private /
    # is_loopback as False on the IPv6 object, yet the OS connects to the
    # underlying IPv4 — so validate the mapped IPv4 to close that SSRF bypass.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_public_http_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is an HTTP(S) URL to a public host.

    Rejects non-HTTP(S) schemes and any host that resolves to (or literally is) a
    private, loopback, link-local, reserved, multicast, or unspecified address —
    the SSRF cases (cloud metadata, localhost services, internal ranges). An IP
    literal resolves to itself, so both hostnames and raw IPs are covered.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme {scheme or '(none)'!r} is not allowed; use http(s)")

    host = parts.hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")

    port = parts.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"could not resolve host {host!r}: {exc}") from exc

    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise ValueError(f"host {host!r} did not resolve to any address")

    blocked = sorted(ip for ip in resolved if _is_blocked_ip(ip))
    if blocked:
        raise ValueError(
            f"host {host!r} resolves to a non-public address ({', '.join(blocked)})"
        )
