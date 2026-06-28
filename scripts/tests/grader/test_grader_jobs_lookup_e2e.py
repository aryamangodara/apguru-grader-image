"""Manual smoke test for GET /api/v1/grader/jobs — job lookup by student_id / test_id.

Exercises the job-lookup endpoint that complements the single-job poll route:

    GET /grader/jobs?student_id=&test_id=   (>=1 filter required; 400 otherwise)
    GET /grader/jobs/{job_id}               (full scorecard — for the cross-check)

It runs five checks and validates the *lightweight-summary* contract: the list
view omits the full scorecard and exposes a flat `percentage` (present once a
job has succeeded), while the single-job detail route still carries the full
scorecard.

  A. ?student_id=<id>           -> 200; summaries, every row matches the student,
                                   no `scorecard` field
  B. ?test_id=<id>              -> 200; summaries, every row matches the test
  C. ?student_id=&test_id=      -> 200; summaries match both filters
  D. (no filter)                -> 400 "provide at least one of student_id or test_id"
  E. cross-check                -> if A/B/C returned a succeeded job, GET that
                                   job_id and assert the detail route DOES include
                                   the full scorecard (proving the list/detail split)

This endpoint may not be deployed to prod yet, so the default target is a LOCAL
dev server. Override with env vars:

    GRADER_BASE_URL          (default http://127.0.0.1:8080/api/v1)
    GRADER_JOBS_STUDENT_ID   (default 3139)
    GRADER_JOBS_TEST_ID      (default 536)

Run (start the dev server first — uvicorn app.main:app --port 8080):
    python scripts/tests/grader/test_grader_jobs_lookup_e2e.py

Against prod once the feature ships:
    GRADER_BASE_URL=http://52.66.25.124:8081/api/v1 \
        python scripts/tests/grader/test_grader_jobs_lookup_e2e.py

Live-tail the log:
    Get-Content -Path .\\scratch\\grader_jobs_lookup_test.log -Wait
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import httpx

sys.stdout.reconfigure(encoding="utf-8")

# --- constants ---------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRATCH = PROJECT_ROOT / "scratch"
LOG_PATH = SCRATCH / "grader_jobs_lookup_test.log"
RESULTS_PATH = SCRATCH / "grader_jobs_lookup_results.json"

BASE_URL = os.environ.get("GRADER_BASE_URL", "http://127.0.0.1:8080/api/v1")
STUDENT_ID = int(os.environ.get("GRADER_JOBS_STUDENT_ID", "3139"))
TEST_ID = int(os.environ.get("GRADER_JOBS_TEST_ID", "536"))

TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

# Fields every JobSummary row must expose (must stay in sync with JobSummary).
SUMMARY_FIELDS = {
    "job_id",
    "test_id",
    "student_id",
    "status",
    "is_handwritten",
    "review_required",
}

_LOG_FH = None
_CHECKS: list[tuple[str, bool, str]] = []


# --- logging helpers ---------------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def log(msg: str = "") -> None:
    line = f"[{_now()}] {msg}" if msg else ""
    print(line, flush=True)
    if _LOG_FH is not None:
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()


def rule(char: str = "-") -> None:
    log(char * 70)


def dump(obj) -> str:
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, indent=2, ensure_ascii=False)
    return str(obj)


def check(label: str, passed: bool, detail: str = "") -> bool:
    """Record one assertion; log it as PASS/FAIL and remember it for the summary."""
    _CHECKS.append((label, passed, detail))
    mark = "PASS" if passed else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    log(f"   [{mark}] {label}{suffix}")
    return passed


# --- HTTP helper -------------------------------------------------------------

def call(client: httpx.Client, method: str, path: str):
    """Issue one request, logging method/url then status/elapsed/body.

    Does NOT raise on non-2xx (scenario D deliberately expects a 400) — only a
    transport-level failure propagates.
    """
    url = BASE_URL + path
    rule()
    log(f"{method} {url}")
    t0 = perf_counter()
    try:
        resp = client.request(method, url, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        log(f"!! request failed: {type(exc).__name__}: {exc}")
        raise
    dt = perf_counter() - t0
    log(f"-> {resp.status_code} {resp.reason_phrase}  ({dt:.2f}s)")
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    log("response:\n" + dump(body))
    return resp, body


# --- summary-list validation -------------------------------------------------

def validate_summary_list(prefix: str, body, *, student_id: int | None = None,
                          test_id: int | None = None) -> str | None:
    """Validate a JobListResponse body; return the first job_id seen (if any)."""
    if not check(f"{prefix}: body is an object with count + jobs",
                 isinstance(body, dict) and "count" in body and "jobs" in body):
        return None

    jobs = body["jobs"]
    check(f"{prefix}: count == len(jobs)", body["count"] == len(jobs),
          f"count={body['count']} len={len(jobs)}")

    if not jobs:
        log(f"   (note) {prefix}: zero jobs matched — content checks skipped")
        return None

    check(f"{prefix}: every row exposes the summary fields",
          all(set(j.keys()) >= SUMMARY_FIELDS for j in jobs))
    check(f"{prefix}: no row carries a full scorecard (lightweight summary)",
          all("scorecard" not in j for j in jobs))

    if student_id is not None:
        check(f"{prefix}: every row matches student_id={student_id}",
              all(j.get("student_id") == student_id for j in jobs))
    if test_id is not None:
        check(f"{prefix}: every row matches test_id={test_id}",
              all(j.get("test_id") == test_id for j in jobs))

    succeeded = [j for j in jobs if j.get("status") == "succeeded"]
    if succeeded:
        check(f"{prefix}: succeeded rows expose a numeric percentage",
              all(isinstance(j.get("percentage"), (int, float)) for j in succeeded))

    # Prefer a succeeded job for the detail cross-check; else the first row.
    return (succeeded[0] if succeeded else jobs[0])["job_id"]


# --- main --------------------------------------------------------------------

def main() -> int:
    global _LOG_FH

    SCRATCH.mkdir(exist_ok=True)
    _LOG_FH = open(LOG_PATH, "w", buffering=1, encoding="utf-8")  # noqa: SIM115

    rule("=")
    log("GRADER JOBS-LOOKUP SMOKE TEST — GET /grader/jobs")
    log(f"base_url   = {BASE_URL}")
    log(f"student_id = {STUDENT_ID}")
    log(f"test_id    = {TEST_ID}")
    log(f"log file   = {LOG_PATH}")
    rule("=")

    detail_job_id: str | None = None

    with httpx.Client() as client:
        # A — filter by student_id
        log("")
        log(f"CHECK A — GET /grader/jobs?student_id={STUDENT_ID}")
        try:
            resp, body = call(client, "GET", f"/grader/jobs?student_id={STUDENT_ID}")
        except httpx.HTTPError:
            log(f"!! could not reach {BASE_URL}. Is the server running? "
                f"Start it or set GRADER_BASE_URL, then re-run.")
            _finish()
            return 1
        if check("A: HTTP 200", resp.status_code == 200, f"got {resp.status_code}"):
            jid = validate_summary_list("A", body, student_id=STUDENT_ID)
            detail_job_id = detail_job_id or jid

        # B — filter by test_id
        log("")
        log(f"CHECK B — GET /grader/jobs?test_id={TEST_ID}")
        resp, body = call(client, "GET", f"/grader/jobs?test_id={TEST_ID}")
        if check("B: HTTP 200", resp.status_code == 200, f"got {resp.status_code}"):
            jid = validate_summary_list("B", body, test_id=TEST_ID)
            detail_job_id = detail_job_id or jid

        # C — filter by both
        log("")
        log(f"CHECK C — GET /grader/jobs?student_id={STUDENT_ID}&test_id={TEST_ID}")
        resp, body = call(
            client, "GET", f"/grader/jobs?student_id={STUDENT_ID}&test_id={TEST_ID}"
        )
        if check("C: HTTP 200", resp.status_code == 200, f"got {resp.status_code}"):
            jid = validate_summary_list("C", body, student_id=STUDENT_ID, test_id=TEST_ID)
            detail_job_id = detail_job_id or jid

        # D — no filter -> 400 guard
        log("")
        log("CHECK D — GET /grader/jobs  (no filter, expect 400)")
        resp, body = call(client, "GET", "/grader/jobs")
        check("D: HTTP 400", resp.status_code == 400, f"got {resp.status_code}")
        detail = body.get("detail", "") if isinstance(body, dict) else str(body)
        check("D: detail mentions 'at least one'", "at least one" in detail.lower(),
              f"detail={detail!r}")

        # E — cross-check the detail route still carries the full scorecard
        log("")
        if detail_job_id is None:
            log("CHECK E — skipped (no job_id available from A/B/C to cross-check)")
        else:
            log(f"CHECK E — GET /grader/jobs/{detail_job_id}  (detail must include scorecard)")
            resp, body = call(client, "GET", f"/grader/jobs/{detail_job_id}")
            if check("E: HTTP 200", resp.status_code == 200, f"got {resp.status_code}"):
                is_ok = isinstance(body, dict)
                if is_ok and body.get("status") == "succeeded":
                    check("E: succeeded detail includes a full scorecard",
                          body.get("scorecard") is not None)
                    check("E: list omitted what detail includes (scorecard split)",
                          body.get("scorecard") is not None)
                else:
                    log(f"   (note) job status={body.get('status') if is_ok else '?'} "
                        f"— no scorecard expected, skipped")

    return _finish()


def _finish() -> int:
    passed = sum(1 for _, ok, _ in _CHECKS if ok)
    failed = [label for label, ok, _ in _CHECKS if not ok]

    rule("=")
    log("RESULT")
    log(f"  checks run    : {len(_CHECKS)}")
    log(f"  passed        : {passed}")
    log(f"  failed        : {len(failed)}")
    for label in failed:
        log(f"    - FAILED: {label}")
    rule("=")

    RESULTS_PATH.write_text(
        json.dumps(
            {
                "base_url": BASE_URL,
                "student_id": STUDENT_ID,
                "test_id": TEST_ID,
                "checks_run": len(_CHECKS),
                "passed": passed,
                "failed": len(failed),
                "failures": failed,
                "checks": [
                    {"label": label, "passed": ok, "detail": detail}
                    for label, ok, detail in _CHECKS
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log(f"(saved {RESULTS_PATH})")
    return 0 if not failed and _CHECKS else 1


if __name__ == "__main__":
    raise SystemExit(main())
