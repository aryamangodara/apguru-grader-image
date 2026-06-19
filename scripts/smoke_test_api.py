#!/usr/bin/env python3
"""API endpoint smoke test for the APGuru Grader.

Hits every HTTP endpoint against a RUNNING server and asserts the expected status
code, exercising the real database connection on the read paths. Unlike the pytest
suite — which mocks the ``Database`` singleton (``tests/conftest.py``) and so never
opens a real connection — this catches failures that only surface against live infra,
e.g. a driver / pool ``pre_ping`` incompatibility that 500s every DB-backed request
(the PyMySQL 1.2.0 incident that took prod down on 2026-06-19).

Default mode is a fast, NON-MUTATING smoke test: no LLM calls, no seeded data, and no
writes — only GET / 404 / 422 paths — so it is safe to run against production. It is
the gate the deploy workflow runs against the freshly-built container, and the step the
release runbook runs before publishing.

Usage:
    python scripts/smoke_test_api.py
    GRADER_BASE_URL=http://52.66.25.124:8081/api/v1 python scripts/smoke_test_api.py
    python scripts/smoke_test_api.py --base-url http://127.0.0.1:8081/api/v1
    python scripts/smoke_test_api.py --deep            # also run one real graded run (LLM, slow)

Exit code is 0 only if every case passes, else 1 — so it can gate a release/deploy.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Matches the e2e convention (scripts/tests/grader/test_grader_handwritten_e2e.py): the
# default targets the in-container port (8080); pass --base-url / GRADER_BASE_URL for the
# host (8081) or prod. All grader endpoints are public, so no auth header is sent.
DEFAULT_BASE_URL = os.environ.get("GRADER_BASE_URL", "http://127.0.0.1:8080/api/v1")

# Sentinels picked so they never collide with real rows; the POST cases below fail
# validation or the existence check *before* any write, so nothing is created.
_NONEXISTENT = "__smoke_nonexistent__"
_UNREGISTERED_TEST_ID = 999999999
_UNLIKELY_STUDENT_ID = 999999999

# (label, method, path, json_body | None, expected_status). Cases tagged [DB] do a real
# database round-trip — these are the ones that 500 when the connection/driver is broken.
Case = tuple[str, str, str, "dict | None", int]


def _cases() -> list[Case]:
    return [
        ("health", "GET", "/health", None, 200),
        ("health/ping", "GET", "/health/ping", None, 200),
        ("exams list [DB]", "GET", "/grader/exams", None, 200),
        ("exams ?course_id [DB]", "GET", f"/grader/exams?course_id={_NONEXISTENT}", None, 200),
        ("jobs no-filter -> 400", "GET", "/grader/jobs", None, 400),
        ("jobs ?student_id [DB]", "GET", f"/grader/jobs?student_id={_UNLIKELY_STUDENT_ID}", None, 200),
        ("job unknown -> 404 [DB]", "GET", f"/grader/jobs/{_NONEXISTENT}", None, 404),
        (
            "register handwritten missing questions_pdf -> 422",
            "POST",
            "/grader/register-exam",
            {
                "test_id": _UNREGISTERED_TEST_ID,
                "course_id": "smoke",
                "test_name": "smoke",
                "is_handwritten": True,
                "marking_scheme_pdf_url": "https://example.com/ms.pdf",
            },
            422,
        ),
        (
            "submit to unregistered test -> 404 [DB]",
            "POST",
            f"/grader/exams/{_UNREGISTERED_TEST_ID}/submissions",
            {"student_id": 1, "answers": {"1": "a"}},
            404,
        ),
        (
            "submit missing student_id -> 422",
            "POST",
            f"/grader/exams/{_UNREGISTERED_TEST_ID}/submissions",
            {},
            422,
        ),
    ]


def _request(method: str, url: str, body: dict | None, timeout: float) -> int:
    """Send one request and return the HTTP status code.

    4xx/5xx responses arrive as ``HTTPError`` — they are valid statuses to assert, not
    crashes, so we return their code. A genuine connection failure (server down, DNS,
    timeout) raises ``URLError``, which the caller treats as a hard failure.
    """
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed scheme, our URL)
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def run_smoke(base_url: str, timeout: float) -> bool:
    """Run every smoke case; return True only if all pass."""
    base = base_url.rstrip("/")
    print(f"Smoke-testing {base}\n")
    passed = 0
    failed: list[str] = []
    for label, method, path, body, expected in _cases():
        try:
            status = _request(method, base + path, body, timeout)
        except urllib.error.URLError as exc:
            print(f"  FAIL  {method:4} {path:46} unreachable: {exc.reason}")
            failed.append(label)
            continue
        ok = status == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {method:4} {path:46} got {status}, want {expected}")
        if ok:
            passed += 1
        else:
            failed.append(label)
    total = passed + len(failed)
    print(f"\n{passed}/{total} passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return not failed


def run_deep(base_url: str) -> bool:
    """Run one real register -> submit -> poll grading run via the existing e2e script.

    Opt-in (live LLM, minutes, costs money). Shelled out so it stays isolated and so
    smoke mode never imports it. Only available where the full repo is checked out (not
    in the slim prod image, which ships this file only).
    """
    e2e = Path(__file__).resolve().parent / "tests" / "grader" / "test_grader_handwritten_e2e.py"
    if not e2e.is_file():
        print(f"--deep: e2e script not found at {e2e}", file=sys.stderr)
        return False
    print(f"\n--deep: running full grading e2e via {e2e.name} (live LLM)\n")
    env = {**os.environ, "GRADER_BASE_URL": base_url}
    return subprocess.run([sys.executable, str(e2e)], env=env, check=False).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="APGuru Grader API endpoint smoke test.")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"API base (default: {DEFAULT_BASE_URL})")
    ap.add_argument("--timeout", type=float, default=15.0, help="per-request timeout seconds (default: 15)")
    ap.add_argument("--deep", action="store_true", help="also run one real graded submission (LLM, slow)")
    args = ap.parse_args()

    ok = run_smoke(args.base_url, args.timeout)
    if ok and args.deep:
        ok = run_deep(args.base_url)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
