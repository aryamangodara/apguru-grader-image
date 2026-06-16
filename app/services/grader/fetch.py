"""Fetch a PDF from a durable URL into a temp file for the grader's renderer.

The answer PDF (handwritten) and the exam's questions PDF arrive as URLs — the
backend stores them in S3, but to us they're plain HTTP links. We GET them with
httpx (same style as ``auth_service``) and write to a temp file so the vendored
grader's ``render_pdf_to_images(path)`` can consume them unchanged.

Because the ``/grader`` endpoints are public and the URL is caller-supplied, the
fetch is SSRF-guarded: ``url_guard.validate_public_http_url`` runs before the
initial request and before every redirect hop (auto-redirects are disabled and
followed manually) so an external→internal redirect cannot bypass the check.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from app.core.config import settings
from app.services.grader.url_guard import validate_public_http_url

log = structlog.get_logger(__name__)

_MAX_REDIRECTS = 5


def _host(url: str) -> str | None:
    return urlsplit(url).hostname


async def fetch_pdf_to_tempfile(url: str) -> Path:
    """Download a PDF from ``url`` to a temp file and return its path.

    The URL is SSRF-validated before each request, including every redirect hop
    (see ``url_guard.validate_public_http_url``). The caller owns the returned
    file and should delete it when done. Raises ``ValueError`` on a blocked URL,
    too many redirects, or an empty body; raises on HTTP error status.
    """
    auth_header = settings.grader_pdf_fetch_auth_header
    origin_host = _host(url)

    timeout = httpx.Timeout(settings.grader_pdf_fetch_timeout_seconds, connect=10.0)
    current = url
    data = b""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                # getaddrinfo is blocking I/O — keep it off the event loop.
                await asyncio.to_thread(validate_public_http_url, current)
            except ValueError as exc:
                log.warning("grader_pdf_fetch_blocked", url=current, reason=str(exc))
                raise

            # Only send the configured auth header to the original host — never
            # forward it across a redirect to a different (possibly hostile) host.
            headers: dict[str, str] = {}
            if auth_header and _host(current) == origin_host:
                headers["Authorization"] = auth_header

            resp = await client.get(current, headers=headers)
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect from {current!r} had no Location header")
                current = urljoin(current, location)
                continue

            resp.raise_for_status()
            data = resp.content
            break
        else:
            raise ValueError(f"too many redirects fetching {url!r}")

    if not data:
        raise ValueError(f"Empty PDF body fetched from {url!r}")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="grader_") as fh:
        fh.write(data)
        path = Path(fh.name)

    log.debug("grader_pdf_fetched", url=url, bytes=len(data), path=str(path))
    return path
