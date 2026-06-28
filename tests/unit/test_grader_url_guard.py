"""Unit tests for the grader SSRF URL guard and the SSRF-aware PDF fetch.

Pure logic — DNS is mocked and HTTP is served by an httpx ``MockTransport``, so no
network is touched. asyncio_mode=auto means the async tests need no marker.
"""
from __future__ import annotations

import socket

import httpx
import pytest

from app.services.grader import fetch, url_guard


def _fake_getaddrinfo(mapping: dict[str, str]):
    """getaddrinfo stand-in: resolve host via ``mapping``, else treat host as a literal IP."""

    def _inner(host, port, *args, **kwargs):
        ip = mapping.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]

    return _inner


def _patch_dns(monkeypatch, mapping: dict[str, str]) -> None:
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", _fake_getaddrinfo(mapping))


def _patch_transport(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fetch.httpx, "AsyncClient", factory)


# --- validate_public_http_url ------------------------------------------------

def test_accepts_public_https(monkeypatch):
    _patch_dns(monkeypatch, {"files.example.com": "93.184.216.34"})
    url_guard.validate_public_http_url("https://files.example.com/exam.pdf")  # no raise


def test_rejects_cloud_metadata_ip(monkeypatch):
    _patch_dns(monkeypatch, {})
    with pytest.raises(ValueError, match="non-public"):
        url_guard.validate_public_http_url("http://169.254.169.254/latest/meta-data/")


def test_rejects_loopback(monkeypatch):
    _patch_dns(monkeypatch, {})
    with pytest.raises(ValueError, match="non-public"):
        url_guard.validate_public_http_url("http://127.0.0.1:3306/")


def test_rejects_private_resolved_host(monkeypatch):
    _patch_dns(monkeypatch, {"internal.example.com": "10.0.0.5"})
    with pytest.raises(ValueError, match=r"10\.0\.0\.5"):
        url_guard.validate_public_http_url("https://internal.example.com/x.pdf")


def test_rejects_ipv4_mapped_ipv6_loopback(monkeypatch):
    # ::ffff:127.0.0.1 looks public to IPv6 is_loopback but maps to 127.0.0.1.
    _patch_dns(monkeypatch, {"evil.example.com": "::ffff:127.0.0.1"})
    with pytest.raises(ValueError, match="non-public"):
        url_guard.validate_public_http_url("https://evil.example.com/x.pdf")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x.pdf", "gopher://host/"])
def test_rejects_non_http_scheme(url):
    with pytest.raises(ValueError, match="scheme"):
        url_guard.validate_public_http_url(url)


def test_rejects_missing_host():
    with pytest.raises(ValueError, match="no host"):
        url_guard.validate_public_http_url("http:///just-a-path")


# --- fetch_pdf_to_tempfile ---------------------------------------------------

async def test_fetch_downloads_public_pdf(monkeypatch):
    _patch_dns(monkeypatch, {"files.example.com": "93.184.216.34"})
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-1.4 fake"))

    path = await fetch.fetch_pdf_to_tempfile("https://files.example.com/exam.pdf")
    try:
        assert path.read_bytes() == b"%PDF-1.4 fake"
    finally:
        path.unlink(missing_ok=True)


async def test_fetch_blocks_redirect_to_internal(monkeypatch):
    _patch_dns(monkeypatch, {"files.example.com": "93.184.216.34"})
    # The public origin 302-redirects to the cloud-metadata endpoint.
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/x"}),
    )

    with pytest.raises(ValueError, match="non-public"):
        await fetch.fetch_pdf_to_tempfile("https://files.example.com/exam.pdf")
