"""Manual e2e smoke test for test_id=536 / student_id=3139 — AP Psychology.

Drives the production server (http://52.66.25.124:8081 by default) through the
full handwritten grading flow using the three PDFs in ``scratch/``.

Phase 0 — S3 upload
    Uploads scratch/questions-psyc.pdf, scratch/marking_scheme.pdf, and
    scratch/answers.pdf to s3://papervideo/grader-exams/test-536-psyc/ so the
    prod server can fetch them via public URL.  AWS credentials are read from
    .env (``AWS_S3_KEY`` / ``AWS_S3_SECRET`` / ``AWS_S3_REGION`` / ``AWS_S3_BUCKET``).
    Skip the upload and supply pre-hosted URLs via env overrides instead:
        GRADER_PSYC_QUESTIONS_URL=<url>
        GRADER_PSYC_ANSWERS_URL=<url>
        GRADER_PSYC_MARKING_URL=<url>

Phase 1–5 — grader test flow
    1. GET  /grader/exams                        (list, before)
    2. POST /grader/register-exam                (parse + cache rubric — idempotent)
    3. POST /grader/exams/536/submissions        (enqueue grading -> job_id)
    4. GET  /grader/exams                        (list, after)
    5. GET  /grader/jobs/{job_id}  every 50 s    (poll until succeeded/failed, cap 900 s)

Run:
    python scripts/tests/grader/test_grader_psyc_536_e2e.py

Live-tail the log:
    Get-Content -Path .\\scratch\\grader_psyc_536_test.log -Wait
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter, sleep

import httpx
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

# --- constants ---------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRATCH = PROJECT_ROOT / "scratch"
LOG_PATH = SCRATCH / "grader_psyc_536_test.log"

TEST_ID = 536
STUDENT_ID = 3139
COURSE_ID = "17"  # AP Psychology

BASE_URL = os.environ.get("GRADER_BASE_URL", "http://52.66.25.124:8081/api/v1")

S3_BUCKET = "papervideo"
S3_REGION = "ap-south-1"
S3_KEY_PREFIX = "grader-exams/test-536-psyc"

SCRATCH_QUESTIONS = SCRATCH / "questions-psyc.pdf"
SCRATCH_ANSWERS = SCRATCH / "answers.pdf"
SCRATCH_MARKING = SCRATCH / "marking_scheme.pdf"

POLL_INTERVAL_SECONDS = 50
POLL_CAP_SECONDS = 900

TIMEOUT_REGISTER = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
TIMEOUT_DEFAULT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

_LOG_FH = None


# --- logging helpers ---------------------------------------------------------

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


# --- HTTP helper -------------------------------------------------------------

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


# --- scorecard summary -------------------------------------------------------

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


# --- S3 upload ---------------------------------------------------------------

def _public_url(bucket: str, region: str, key: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def _upload_pdf(s3_client, local_path: Path, key: str, bucket: str, region: str) -> str:
    """Upload one PDF world-readable and return its permanent public URL.

    Tries public-read ACL first; falls back to no-ACL if the bucket has
    ACLs disabled (bucket-owner-enforced Object Ownership).
    """
    from botocore.exceptions import ClientError

    base_args = {"ContentType": "application/pdf", "ContentDisposition": "inline"}
    try:
        s3_client.upload_file(
            str(local_path), bucket, key,
            ExtraArgs={**base_args, "ACL": "public-read"},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessControlListNotSupported", "InvalidRequest", "AccessDenied"):
            s3_client.upload_file(str(local_path), bucket, key, ExtraArgs=base_args)
        else:
            raise
    return _public_url(bucket, region, key)


def _ensure_pdf_urls() -> tuple[str, str, str]:
    """Return (questions_url, answers_url, marking_url).

    Uses env-var overrides when all three are set; otherwise uploads the three
    scratch PDFs to S3 and returns their permanent public URLs.
    """
    q_url = os.environ.get("GRADER_PSYC_QUESTIONS_URL")
    a_url = os.environ.get("GRADER_PSYC_ANSWERS_URL")
    m_url = os.environ.get("GRADER_PSYC_MARKING_URL")

    if q_url and a_url and m_url:
        log("Using pre-supplied PDF URLs from env overrides (skipping S3 upload).")
        log(f"  questions : {q_url}")
        log(f"  answers   : {a_url}")
        log(f"  marking   : {m_url}")
        return q_url, a_url, m_url

    # --- boto3 upload --------------------------------------------------------
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        sys.exit(
            "boto3 is not installed. Run:  pip install -r requirements-dev.txt\n"
            "Or skip the upload by setting all three URL env vars:\n"
            "  GRADER_PSYC_QUESTIONS_URL=...\n"
            "  GRADER_PSYC_ANSWERS_URL=...\n"
            "  GRADER_PSYC_MARKING_URL=..."
        )

    load_dotenv(PROJECT_ROOT / ".env", override=True)

    aws_key = os.environ.get("AWS_S3_KEY")
    aws_secret = os.environ.get("AWS_S3_SECRET")
    aws_region = os.environ.get("AWS_S3_REGION", S3_REGION)
    aws_bucket = os.environ.get("AWS_S3_BUCKET", S3_BUCKET)

    if not aws_key or not aws_secret:
        sys.exit(
            "AWS credentials not found. Add AWS_S3_KEY + AWS_S3_SECRET to .env\n"
            "or set the three GRADER_PSYC_*_URL env vars to skip the upload."
        )

    for path, label in [
        (SCRATCH_QUESTIONS, "scratch/questions-psyc.pdf"),
        (SCRATCH_ANSWERS, "scratch/answers.pdf"),
        (SCRATCH_MARKING, "scratch/marking_scheme.pdf"),
    ]:
        if not path.exists():
            sys.exit(f"Missing scratch file: {path}\nCheck that {label} is in the scratch/ folder.")

    s3 = boto3.client(
        "s3",
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        region_name=aws_region,
    )

    uploads = [
        (SCRATCH_QUESTIONS, f"{S3_KEY_PREFIX}/questions.pdf",      "questions"),
        (SCRATCH_ANSWERS,   f"{S3_KEY_PREFIX}/answers.pdf",        "answers"),
        (SCRATCH_MARKING,   f"{S3_KEY_PREFIX}/marking-scheme.pdf", "marking"),
    ]

    urls: dict[str, str] = {}
    for local_path, key, role in uploads:
        log(f"  uploading {local_path.name} -> s3://{aws_bucket}/{key}")
        try:
            url = _upload_pdf(s3, local_path, key, aws_bucket, aws_region)
            urls[role] = url
            log(f"    -> {url}")
        except Exception as exc:
            sys.exit(f"S3 upload failed for {local_path.name}: {exc}")

    return urls["questions"], urls["answers"], urls["marking"]


# --- result persistence ------------------------------------------------------

def _finish(result: dict, scorecard) -> None:
    (SCRATCH / "grader_psyc_536_results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if isinstance(scorecard, dict):
        (SCRATCH / "scorecard_psyc_536.json").write_text(
            json.dumps(scorecard, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    rule("=")
    log("FINAL RESULT")
    log(dump(result))
    extra = " + scorecard_psyc_536.json" if isinstance(scorecard, dict) else ""
    log(f"(saved scratch/grader_psyc_536_results.json{extra})")
    rule("=")


# --- main --------------------------------------------------------------------

def main() -> int:
    global _LOG_FH

    SCRATCH.mkdir(exist_ok=True)
    _LOG_FH = open(LOG_PATH, "w", buffering=1, encoding="utf-8")  # noqa: SIM115

    rule("=")
    log("GRADER E2E — AP Psychology  test_id=536  student_id=3139  course_id=17")
    log(f"base_url={BASE_URL}")
    log(f"log file: {LOG_PATH}")
    rule("=")

    # Phase 0 — ensure PDFs are accessible via public URL
    log("")
    log("PHASE 0 — ensuring PDFs are reachable by the server")
    questions_url, answers_url, marking_url = _ensure_pdf_urls()

    result: dict = {
        "test_id": TEST_ID,
        "student_id": STUDENT_ID,
        "course_id": COURSE_ID,
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
        log("")
        log("STEP 1/5 - list exams (before)")
        try:
            call(client, "GET", "/grader/exams")
        except httpx.HTTPError:
            log(f"Is the server up at {BASE_URL}? Check GRADER_BASE_URL, then re-run.")
            return 1

        # 2. register
        log("")
        log("STEP 2/5 - register exam (synchronous rubric parse; may take a while)")
        reg_payload = {
            "test_id": TEST_ID,
            "course_id": COURSE_ID,
            "test_name": "AP Psychology (e2e smoke)",
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
        log(
            f"   test_id = {body['test_id']}  (cached={body.get('cached')}, "
            f"total_points={body.get('total_points')}, question_count={body.get('question_count')})"
        )

        # 3. submit
        log("")
        log("STEP 3/5 - submit student answers (enqueue grading)")
        sub_payload = {"student_id": STUDENT_ID, "answers_pdf_url": answers_url}
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
        log(f"   test_id {TEST_ID} present in list: {present}")

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
                elapsed = int(perf_counter() - t_start)
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


if __name__ == "__main__":
    raise SystemExit(main())
