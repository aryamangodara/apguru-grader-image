"""Manual end-to-end smoke test of the IB grader path — IB Business Management HL.

Drives a running server (default ``http://127.0.0.1:8080``) through the full
handwritten flow for one **markband** IB subject (Business Management HL,
course_id 116) and logs every request/response to ``scratch/grader_ib_test.log``
(line-buffered) and stdout. This exercises exactly the IB-specific code added for
IB support: ``register-exam`` parses the markscheme with the **IB rubric prompt**
(because the seeded course has ``exam_body='IBO'``), and grading uses the **IB
grade prompt** (markband best-fit, partial credit).

Flow (all four endpoints):
  1. GET  /grader/exams                        (list, before)
  2. POST /grader/register-exam                (parse + cache the IB rubric)
  3. POST /grader/exams/{test_id}/submissions  (enqueue OCR + grading -> job_id)
  4. GET  /grader/exams                        (list, after)
  5. GET  /grader/jobs/{job_id}  every 50s     (poll until succeeded/failed)

Prerequisites:
  * Migration 028 applied so course_id 116 exists in course_configs (exam_body=IBO).
  * Vertex routing enabled (``GOOGLE_CLOUD_PROJECT`` set) — the answers are a PDF,
    so this is the handwritten/OCR path, and the long OCR call 504s on AI Studio.
  * The three PDFs must be reachable by the server's SSRF-guarded fetch. The source
    links are Google Drive ``/view`` pages, so we convert them to direct-download
    URLs. If Drive blocks the fetch (large-file interstitial), re-host the PDFs and
    override via env: GRADER_IB_QUESTIONS_URL / _MARKING_URL / _ANSWERS_URL.

    python scripts/tests/grader/test_grader_ib_e2e.py

The /grader endpoints are public (no JWT), so no auth token is needed.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter, sleep

import httpx

sys.stdout.reconfigure(encoding="utf-8")

# --- config -----------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRATCH = PROJECT_ROOT / "scratch"
LOG_PATH = SCRATCH / "grader_ib_test.log"

BASE_URL = os.environ.get("GRADER_BASE_URL", "http://127.0.0.1:8080/api/v1")
STUDENT_ID = 2371

# IB Business Management HL — a markband subject (the IB stress test).
COURSE_ID = "116"
TEST_ID = 9116  # synthetic tests.id (9000 + course_id), clear of the AP smoke range
TEST_NAME = "IB Business Management HL (smoke)"

# Source PDFs (user-supplied Google Drive links). Converted to direct-download;
# override with re-hosted URLs via env if Drive blocks the server-side fetch.
_DRIVE_IDS = {
    "questions": "1kcSqE0lamZHn5OzGTy2von0RUuJVXiF6",
    "marking": "1Gcpehfi5H_m2a4UuEgyufsB8cc7oe1wy",
    "answers": "14QsnOMufT_h-cmWmC3OsKseT3AK-Fm3k",
}


def _drive_download_url(file_id: str) -> str:
    """Direct-download URL for an 'anyone with link' Drive file."""
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


QUESTIONS_URL = os.environ.get("GRADER_IB_QUESTIONS_URL", _drive_download_url(_DRIVE_IDS["questions"]))
MARKING_URL = os.environ.get("GRADER_IB_MARKING_URL", _drive_download_url(_DRIVE_IDS["marking"]))
ANSWERS_URL = os.environ.get("GRADER_IB_ANSWERS_URL", _drive_download_url(_DRIVE_IDS["answers"]))

POLL_INTERVAL_SECONDS = 50
POLL_CAP_SECONDS = 900  # 15 min hard cap on the grading poll

TIMEOUT_REGISTER = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
TIMEOUT_DEFAULT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

_LOG_FH = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


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


def call(client: httpx.Client, method: str, path: str, payload=None, timeout=TIMEOUT_DEFAULT):
    """Issue one request, logging method/url/payload then status/elapsed/body."""
    url = BASE_URL + path
    rule()
    log(f"{method} {url}")
    if payload is not None:
        log("payload:\n" + dump(payload))
    t0 = perf_counter()
    try:
        resp = client.request(method, url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        log(f"!! request failed: {type(exc).__name__}: {exc}")
        raise
    dt = perf_counter() - t0
    log(f"-> {resp.status_code} {resp.reason_phrase}  ({dt:.1f}s)")
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    log("response:\n" + dump(body))
    return resp, body


def summarize_scorecard(sc: dict) -> None:
    log("")
    log("SCORECARD SUMMARY")
    log(f"  subject               : {sc.get('subject')}")
    log(f"  percentage            : {sc.get('percentage')}")
    log(f"  points earned/possible: {sc.get('total_points_earned')} / {sc.get('total_points_possible')}")
    log(f"  questions_graded      : {sc.get('questions_graded')}")
    log(f"  review_flags          : {sc.get('review_flags')}")
    qwm = sc.get("question_wise_marks") or []
    if qwm:
        log("  question_wise_marks   : " + ", ".join(f"Q{m.get('question_id')}={m.get('marks')}" for m in qwm))
    for q in sc.get("questions", []):
        log(
            f"    - {q.get('question_id')}: {q.get('points_earned')}/{q.get('points_possible')} "
            f"({q.get('status')})"
        )
        # Markband check: each criterion point should show partial credit + a band rationale.
        for p in q.get("points", []):
            log(
                f"        · {p.get('point_id')}: {p.get('points_earned')}/{p.get('points_possible')} "
                f"[{p.get('grading_confidence')}] {str(p.get('rationale'))[:90]}"
            )


def main() -> int:
    global _LOG_FH

    SCRATCH.mkdir(exist_ok=True)
    _LOG_FH = open(LOG_PATH, "w", buffering=1, encoding="utf-8")  # noqa: SIM115 (long-lived log handle)

    rule("=")
    log(f"GRADER IB E2E (markband)  subject=IB Business Management HL  course_id={COURSE_ID}")
    log(f"base_url={BASE_URL}  student_id={STUDENT_ID}  test_id={TEST_ID}")
    log(f"questions={QUESTIONS_URL}")
    log(f"marking  ={MARKING_URL}")
    log(f"answers  ={ANSWERS_URL}")
    log(f"log file: {LOG_PATH}")
    rule("=")

    result: dict = {
        "subject": "IB Business Management HL",
        "course_id": COURSE_ID,
        "test_id": TEST_ID,
        "job_id": None,
        "status": None,
        "percentage": None,
        "total_points_earned": None,
        "total_points_possible": None,
        "review_required": None,
        "error": None,
    }

    with httpx.Client() as client:
        # 1. list (before)
        log("STEP 1/5 - list exams (before)")
        try:
            call(client, "GET", "/grader/exams")
        except httpx.HTTPError:
            log("Is the server up on :8080? Start it, then re-run.")
            return 1

        # 2. register (synchronous IB rubric parse; may take a while)
        log("")
        log("STEP 2/5 - register IB exam (IB rubric prompt; markband extraction)")
        reg_payload = {
            "test_id": TEST_ID,
            "course_id": COURSE_ID,
            "test_name": TEST_NAME,
            "is_handwritten": True,
            "marking_scheme_pdf_url": MARKING_URL,
            "questions_pdf_url": QUESTIONS_URL,
        }
        resp, body = call(client, "POST", "/grader/register-exam", reg_payload, timeout=TIMEOUT_REGISTER)
        if resp.status_code != 201 or not isinstance(body, dict) or not body.get("test_id"):
            log("!! registration did not return 201 + test_id; stopping (gate).")
            result["error"] = f"register failed: {resp.status_code} {body}"
            _finish(result, None)
            return 1
        log(f"   test_id = {body['test_id']}  (cached={body.get('cached')}, "
            f"total_points={body.get('total_points')}, question_count={body.get('question_count')})")

        # 3. submit answers PDF (enqueue OCR + IB grading)
        log("")
        log("STEP 3/5 - submit student answers PDF (enqueue grading)")
        sub_payload = {"student_id": STUDENT_ID, "answers_pdf_url": ANSWERS_URL}
        resp, body = call(client, "POST", f"/grader/exams/{TEST_ID}/submissions", sub_payload)
        if resp.status_code != 202 or not isinstance(body, dict) or not body.get("job_id"):
            log("!! submission did not return 202 + job_id; stopping.")
            result["error"] = f"submit failed: {resp.status_code} {body}"
            _finish(result, None)
            return 1
        result["job_id"] = body["job_id"]
        log(f"   job_id = {result['job_id']}  (status={body.get('status')})")

        # 4. list (after)
        log("")
        log("STEP 4/5 - list exams (after)")
        _, body = call(client, "GET", "/grader/exams")
        present = isinstance(body, dict) and any(
            e.get("test_id") == TEST_ID for e in body.get("exams", [])
        )
        log(f"   our test_id present in list: {present}")

        # 5. poll
        log("")
        log(f"STEP 5/5 - poll job every {POLL_INTERVAL_SECONDS}s (cap {POLL_CAP_SECONDS}s)")
        scorecard = None
        t_start = perf_counter()
        poll_n = 0
        while True:
            poll_n += 1
            elapsed = int(perf_counter() - t_start)
            try:
                resp, body = call(client, "GET", f"/grader/jobs/{result['job_id']}")
            except httpx.HTTPError as exc:
                log(f"   poll #{poll_n} (t+{elapsed}s): transient {type(exc).__name__}: {exc}; retrying")
                if int(perf_counter() - t_start) > POLL_CAP_SECONDS:
                    result["status"] = result["status"] or "timeout"
                    result["error"] = f"poll cap {POLL_CAP_SECONDS}s exceeded after network errors"
                    log(f"!! {result['error']}")
                    break
                sleep(POLL_INTERVAL_SECONDS)
                continue
            status = body.get("status") if isinstance(body, dict) else None
            log(f"   poll #{poll_n} (t+{elapsed}s): status={status}")
            if status in ("succeeded", "failed"):
                result["status"] = status
                result["review_required"] = body.get("review_required")
                if status == "succeeded":
                    scorecard = body.get("scorecard")
                    if isinstance(scorecard, dict):
                        result["percentage"] = scorecard.get("percentage")
                        result["total_points_earned"] = scorecard.get("total_points_earned")
                        result["total_points_possible"] = scorecard.get("total_points_possible")
                        summarize_scorecard(scorecard)
                else:
                    result["error"] = body.get("error")
                    log(f"!! job failed: {result['error']}")
                break
            if elapsed > POLL_CAP_SECONDS:
                result["status"] = status or "timeout"
                result["error"] = f"poll cap {POLL_CAP_SECONDS}s exceeded (last status={status})"
                log(f"!! {result['error']}")
                break
            sleep(POLL_INTERVAL_SECONDS)

    _finish(result, scorecard)
    return 0 if result["status"] == "succeeded" else 1


def _finish(result: dict, scorecard) -> None:
    (SCRATCH / "grader_ib_e2e_results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if isinstance(scorecard, dict):
        (SCRATCH / "scorecard_ib_business_management_hl.json").write_text(
            json.dumps(scorecard, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    rule("=")
    log("FINAL RESULT")
    log(dump(result))
    rule("=")


if __name__ == "__main__":
    raise SystemExit(main())
