"""Manual end-to-end smoke test of the four AP FRQ grader APIs (handwritten).

Drives a real running server (default ``http://127.0.0.1:8080``) through the full
handwritten flow for one subject and logs **every request, payload, and response**
to ``scratch/grader_test.log`` (line-buffered) and stdout, so the run can be
watched live:

    Get-Content -Path .\\scratch\\grader_test.log -Wait     # PowerShell tail -f

Flow (exercises all four endpoints):
  1. GET  /grader/exams                        (list, before)
  2. POST /grader/register-exam                (parse + cache the rubric)
  3. POST /grader/exams/{test_id}/submissions  (enqueue grading -> job_id)
  4. GET  /grader/exams                        (list, after)
  5. GET  /grader/jobs/{job_id}  every 15s     (poll until succeeded/failed)

Not collected by pytest — run it directly (real infra + a live LLM, like the
other ``scripts/tests`` smoke tests). The /grader endpoints are public (no JWT),
so no auth token is needed.

    python scripts/tests/grader/test_grader_handwritten_e2e.py            # AP Biology
    python scripts/tests/grader/test_grader_handwritten_e2e.py psychology # any seeded slug

The subject arg always selects the course (and its grading/OCR addenda). Optional
env vars override the defaults so the same script can drive a real exam record with
specific PDFs instead of the synthetic IDs + generic per-subject S3 PDFs:

    GRADER_BASE_URL        server base    (default http://127.0.0.1:8080/api/v1)
    GRADER_TEST_ID         exam test_id   (default 9000 + course_id)
    GRADER_STUDENT_ID      student_id     (default 2371)
    GRADER_QUESTIONS_URL   questions PDF  (default <s3>/<subject>/questions.pdf)
    GRADER_ANSWERS_URL     answers PDF    (default <s3>/<subject>/answers.pdf)
    GRADER_MARKING_URL     marking PDF    (default <s3>/<subject>/marking-scheme.pdf)

E.g. grade the real test_id=536 / student_id=3139 AP Psychology record against prod,
using PDFs already hosted on S3:

    GRADER_BASE_URL=http://52.66.25.124:8081/api/v1 \
    GRADER_TEST_ID=536 GRADER_STUDENT_ID=3139 \
    GRADER_QUESTIONS_URL=https://papervideo.s3.ap-south-1.amazonaws.com/grader-exams/test-536-psyc/questions.pdf \
    GRADER_ANSWERS_URL=https://papervideo.s3.ap-south-1.amazonaws.com/grader-exams/test-536-psyc/answers.pdf \
    GRADER_MARKING_URL=https://papervideo.s3.ap-south-1.amazonaws.com/grader-exams/test-536-psyc/marking-scheme.pdf \
    python scripts/tests/grader/test_grader_handwritten_e2e.py psychology
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
LOG_PATH = SCRATCH / "grader_test.log"

BASE_URL = os.environ.get("GRADER_BASE_URL", "http://127.0.0.1:8080/api/v1")
STUDENT_ID = 2371  # default; override with GRADER_STUDENT_ID to grade a real student
# Synthetic tests.id for this handwritten smoke (grades a PDF; needn't be a real
# test row). 9000 + course_id keeps it unique and clear of real tests; override with
# GRADER_TEST_ID to grade a real exam record.
TEST_ID_BASE = 9000

POLL_INTERVAL_SECONDS = 50
POLL_CAP_SECONDS = 900  # 15 min hard cap on the grading poll

S3_BASE = "https://papervideo.s3.ap-south-1.amazonaws.com/grader-exams/20260603-073426"

# s3 folder -> course_id (from s3_links.md + the verified `course` catalog).
SUBJECTS: dict[str, str] = {
    "biology": "14",
    "statistics": "15",
    "chemistry": "16",
    "precalculus": "18",
    "human-geography": "25",
    "computer-science-a": "26",
    "physics-c-mechanics": "28",
    "environmental-science": "29",
    "world-history": "30",
    "english-language": "33",
    "comparative-government-politics": "35",
    # newly added to course_configs (see scripts/seed_grader_extra_courses.py)
    "calculus-bc": "31",
    "macroeconomics": "21",
    "psychology": "17",
}

TIMEOUT_REGISTER = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
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
    log(f"  subject              : {sc.get('subject')}")
    log(f"  percentage           : {sc.get('percentage')}")
    log(f"  points earned/possible: {sc.get('total_points_earned')} / {sc.get('total_points_possible')}")
    log(f"  questions_graded     : {sc.get('questions_graded')}")
    log(f"  review_flags         : {sc.get('review_flags')}")
    for q in sc.get("questions", []):
        log(
            f"    - {q.get('question_id')}: {q.get('points_earned')}/{q.get('points_possible')} "
            f"({q.get('status')})"
        )
    unattempted = sc.get("unattempted") or []
    if unattempted:
        log(f"  unattempted          : {[q.get('question_id') for q in unattempted]}")


def main() -> int:
    global _LOG_FH

    subject = sys.argv[1] if len(sys.argv) > 1 else "biology"
    if subject not in SUBJECTS:
        print(f"Unknown subject {subject!r}; choose from {sorted(SUBJECTS)}")
        return 2
    course_id = SUBJECTS[subject]
    test_id = int(os.environ.get("GRADER_TEST_ID", TEST_ID_BASE + int(course_id)))
    student_id = int(os.environ.get("GRADER_STUDENT_ID", STUDENT_ID))
    test_name = f"{subject.replace('-', ' ').title()} (smoke)"

    SCRATCH.mkdir(exist_ok=True)
    _LOG_FH = open(LOG_PATH, "w", buffering=1, encoding="utf-8")  # noqa: SIM115 (long-lived log handle)

    folder = f"{S3_BASE}/{subject}"
    questions_url = os.environ.get("GRADER_QUESTIONS_URL", f"{folder}/questions.pdf")
    answers_url = os.environ.get("GRADER_ANSWERS_URL", f"{folder}/answers.pdf")
    marking_url = os.environ.get("GRADER_MARKING_URL", f"{folder}/marking-scheme.pdf")

    overrides = [
        v for v in (
            "GRADER_TEST_ID", "GRADER_STUDENT_ID",
            "GRADER_QUESTIONS_URL", "GRADER_ANSWERS_URL", "GRADER_MARKING_URL",
        ) if v in os.environ
    ]

    rule("=")
    log(f"GRADER E2E (handwritten)  subject={subject}  course_id={course_id}")
    log(f"base_url={BASE_URL}  student_id={student_id}  test_id={test_id}  test_name={test_name!r}")
    if overrides:
        log(f"env overrides active: {', '.join(overrides)}")
    log(f"log file: {LOG_PATH}")
    rule("=")

    result: dict = {
        "subject": subject,
        "course_id": course_id,
        "test_id": test_id,
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

        # 2. register
        log("")
        log("STEP 2/5 - register exam (synchronous rubric parse; may take a while)")
        reg_payload = {
            "test_id": test_id,
            "course_id": course_id,
            "test_name": test_name,
            "is_handwritten": True,
            "marking_scheme_pdf_url": marking_url,
            "questions_pdf_url": questions_url,
        }
        resp, body = call(client, "POST", "/grader/register-exam", reg_payload, timeout=TIMEOUT_REGISTER)
        if resp.status_code != 201 or not isinstance(body, dict) or not body.get("test_id"):
            log("!! registration did not return 201 + test_id; stopping (gate).")
            result["error"] = f"register failed: {resp.status_code} {body}"
            _finish(result, None)
            return 1
        result["test_id"] = body["test_id"]
        log(f"   test_id = {result['test_id']}  (cached={body.get('cached')}, "
            f"total_points={body.get('total_points')}, question_count={body.get('question_count')})")

        # 3. submit
        log("")
        log("STEP 3/5 - submit student answers (enqueue grading)")
        sub_payload = {"student_id": student_id, "answers_pdf_url": answers_url}
        resp, body = call(client, "POST", f"/grader/exams/{result['test_id']}/submissions", sub_payload)
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
            e.get("test_id") == result["test_id"] for e in body.get("exams", [])
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
                # Transient network blip (e.g. DNS failure after the machine wakes
                # from sleep) — don't crash the whole test; keep polling to the cap.
                log(f"   poll #{poll_n} (t+{elapsed}s): transient {type(exc).__name__}: {exc}; retrying")
                elapsed = int(perf_counter() - t_start)  # recompute: the failed call may have blocked
                if elapsed > POLL_CAP_SECONDS:
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
    (SCRATCH / "grader_e2e_results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if isinstance(scorecard, dict):
        (SCRATCH / f"scorecard_{result['subject']}.json").write_text(
            json.dumps(scorecard, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    rule("=")
    log("FINAL RESULT")
    log(dump(result))
    extra = f" + scorecard_{result['subject']}.json" if isinstance(scorecard, dict) else ""
    log(f"(saved scratch/grader_e2e_results.json{extra})")
    rule("=")


if __name__ == "__main__":
    raise SystemExit(main())
