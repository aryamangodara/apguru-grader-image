"""Manual end-to-end smoke test of the homework & quiz grader (/grader/assessments).

Drives a **real running server** through the whole new assessment surface for one
**homework** and one **real quiz** (rows that exist in the main app's DB), then runs
a **regression pass** over the original exam/test surface (/grader/exams,
/grader/jobs) to prove nothing from the previous iteration broke. Every
request/response is logged to ``scratch/grader_assessments_e2e.log`` (line-buffered)
and stdout so the run can be watched live:

    Get-Content -Path .\\scratch\\grader_assessments_e2e.log -Wait   # PowerShell tail -f

What it exercises
-----------------
NEW assessment surface (all five routes + the {error_code, detail} envelope):
  * POST /grader/assessments/register                 register homework + quiz (assessment_type in BODY)
  * GET  /grader/assessments?assessment_type=          list per type + type-isolation check
  * POST /grader/assessments/{source_id}/submissions   enqueue grading (assessment_type in BODY)
  * GET  /grader/assessments/jobs?assessment_type=     lightweight job summaries (no scorecard)
  * GET  /grader/assessments/jobs/{job_id}             poll -> full scorecard
  Documented failures render the envelope: 422 (assessment_type='exam' rejected /
  handwritten missing questions PDF), 404 TEST_NOT_REGISTERED, 400 MISSING_JOB_FILTER,
  404 JOB_NOT_FOUND.

EXAM regression (unchanged contract):
  * GET  /grader/exams ; POST /grader/register-exam ; POST /grader/exams/{id}/submissions
  * GET  /grader/jobs?test_id= ; GET /grader/jobs/{job_id} ; GET /grader/jobs (no filter -> 400)

Defaults grade REAL rows from the shared DB and exercise BOTH grading modes + both
exam bodies:
  * homework = docs_homework_test #123 (real row). Its OWN marking scheme in prod is a
    resume PDF (a real data-quality issue), so it is graded against a standard AP Biology
    FRQ marking scheme (course 14), HANDWRITTEN -> exercises the OCR path via /assessments.
  * quiz     = quiz #869 (real row) — AP World History (course 30), REAL marking scheme
    (parses to 18 questions / 32 pts) -> AP prompt set, graded TYPED.
  * exam     = tests #536 — AP Biology (course 14), graded TYPED (regression).
  * homework-knowledge = docs_homework_test #124 — AP Biology (course 14), registered WITHOUT a
    marking scheme -> graded from the model's OWN knowledge (grading_mode="knowledge": per-question
    verdict + correct answer + "X of Y correct", NO marks), graded TYPED.
All PDFs are public S3.

Preconditions
-------------
  * A running grader server whose DB has migration 042 applied (the assessment_type
    column) and whose source tables contain the ids above. Locally this is the
    isolated ``grader_e2e`` database seeded by the harness.
  * A live LLM (Vertex/Gemini) + Langfuse — each grade is a real, billable Gemini call.

Not collected by pytest (hits real infra); run it directly:

    python scripts/tests/grader/test_grader_assessments_e2e.py

Env overrides (all optional): GRADER_BASE_URL, GRADER_HW_SOURCE_ID, GRADER_HW_KNOWLEDGE_SOURCE_ID,
GRADER_QUIZ_SOURCE_ID, GRADER_EXAM_TEST_ID, GRADER_HW_COURSE_ID / GRADER_QUIZ_COURSE_ID /
GRADER_EXAM_COURSE_ID, GRADER_HW_MARKING_URL / GRADER_HW_QUESTIONS_URL / GRADER_HW_ANSWERS_URL /
GRADER_QUIZ_MARKING_URL / GRADER_EXAM_MARKING_URL, GRADER_STUDENT_ID (base; +1 per assessment),
GRADER_POLL_CAP.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, sleep

import httpx

sys.stdout.reconfigure(encoding="utf-8")

# --- config ------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRATCH = PROJECT_ROOT / "scratch"
LOG_PATH = SCRATCH / "grader_assessments_e2e.log"
RESULTS_PATH = SCRATCH / "grader_assessments_e2e_results.json"

BASE_URL = os.environ.get("GRADER_BASE_URL", "http://127.0.0.1:8080/api/v1")
STUDENT_BASE = int(os.environ.get("GRADER_STUDENT_ID", "700001"))
POLL_CAP_SECONDS = int(os.environ.get("GRADER_POLL_CAP", "1200"))
POLL_INTERVAL_SECONDS = 20

# S3 root for the known-good AP FRQ material (the same PDFs the exam e2e drives).
_S3_FRQ = "https://papervideo.s3.ap-south-1.amazonaws.com/grader-exams/20260603-073426"

# Homework: REAL row docs_homework_test #123. Its own marking scheme in prod is a
# resume PDF (a real data-quality issue), so grade it against a standard AP Biology
# FRQ marking scheme, HANDWRITTEN -> exercises the OCR path through /assessments.
HW_SOURCE_ID = int(os.environ.get("GRADER_HW_SOURCE_ID", "123"))
HW_COURSE_ID = os.environ.get("GRADER_HW_COURSE_ID", "14")  # AP Biology
_HW_S3 = f"{_S3_FRQ}/biology"
HW_MARKING_URL = os.environ.get("GRADER_HW_MARKING_URL", f"{_HW_S3}/marking-scheme.pdf")
HW_QUESTIONS_URL = os.environ.get("GRADER_HW_QUESTIONS_URL", f"{_HW_S3}/questions.pdf")
HW_ANSWERS_URL = os.environ.get("GRADER_HW_ANSWERS_URL", f"{_HW_S3}/answers.pdf")

# Rubric-free homework: a REAL docs_homework_test row graded WITHOUT a marking scheme —
# right/wrong from the model's own knowledge (no marks). Reuses the AP Biology questions
# PDF; graded TYPED. Needs its OWN source id (registration is idempotent per (type, id), so
# it can't reuse HW_SOURCE_ID which is registered with a marking scheme above).
HW_KNOWLEDGE_SOURCE_ID = int(os.environ.get("GRADER_HW_KNOWLEDGE_SOURCE_ID", "124"))

# Quiz: REAL row quiz #869 — AP World History (course 30), REAL marking scheme
# (parses to 18 questions / 32 points). Graded TYPED.
QUIZ_SOURCE_ID = int(os.environ.get("GRADER_QUIZ_SOURCE_ID", "869"))
QUIZ_COURSE_ID = os.environ.get("GRADER_QUIZ_COURSE_ID", "30")
QUIZ_MARKING_URL = os.environ.get(
    "GRADER_QUIZ_MARKING_URL",
    "https://papervideo.s3.ap-south-1.amazonaws.com/papervideo/apguru/assets/marking_scheme/68f9f2a08990e.pdf",
)

# Exam regression: tests #536 — AP Biology (course 14), graded TYPED. Biology's
# marking scheme parses fast/reliably (~45s); statistics was an outlier at >300s.
EXAM_TEST_ID = int(os.environ.get("GRADER_EXAM_TEST_ID", "536"))
EXAM_COURSE_ID = os.environ.get("GRADER_EXAM_COURSE_ID", "14")
EXAM_MARKING_URL = os.environ.get("GRADER_EXAM_MARKING_URL", f"{_S3_FRQ}/biology/marking-scheme.pdf")

# Representative typed answers (not from a real student — the point is pipeline
# function, not answer correctness). Keys are major-question ids; the label step
# maps them onto the parsed rubric.
QUIZ_TYPED_ANSWERS = {
    "1": (
        "The Columbian Exchange transferred crops, animals, people, and diseases between the "
        "Americas, Europe, Asia, and Africa after 1492, reshaping demographics and economies "
        "on multiple continents."
    ),
    "2": (
        "Nineteenth-century European imperialism was driven by industrial demand for raw "
        "materials and new markets, nationalist competition, and technological advantages such "
        "as steamships and repeating rifles."
    ),
}
EXAM_TYPED_ANSWERS = {
    "1": (
        "Natural selection raises the frequency of advantageous heritable traits over generations "
        "because individuals bearing them survive and reproduce at higher rates."
    ),
    "2": (
        "In aerobic cellular respiration glucose is oxidized through glycolysis, the Krebs cycle, and "
        "the electron transport chain, where oxygen is the final electron acceptor and most ATP is made."
    ),
}
# Typed answers for the rubric-free homework (graded from the model's own AP Biology knowledge).
HW_KNOWLEDGE_TYPED_ANSWERS = {
    "1": (
        "Natural selection increases the frequency of advantageous heritable traits because the "
        "individuals carrying them survive and reproduce more, so those alleles spread over generations."
    ),
    "2": (
        "Enzymes speed up reactions by lowering the activation energy, stabilizing the transition "
        "state; they are substrate-specific and are not consumed by the reaction."
    ),
}

TIMEOUT_REGISTER = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
TIMEOUT_DEFAULT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)

_LOG_FH = None
_CHECKS: list[tuple[str, bool, str]] = []


# --- logging / assertions ----------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def log(msg: str = "") -> None:
    line = f"[{_now()}] {msg}" if msg else ""
    print(line, flush=True)
    if _LOG_FH is not None:
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()


def rule(char: str = "-") -> None:
    log(char * 74)


def dump(obj) -> str:
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, indent=2, ensure_ascii=False)
    return str(obj)


def check(label: str, passed: bool, detail: str = "") -> bool:
    _CHECKS.append((label, passed, detail))
    mark = "PASS" if passed else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    log(f"   [{mark}] {label}{suffix}")
    return passed


def call(client: httpx.Client, method: str, path: str, payload=None, timeout=TIMEOUT_DEFAULT):
    """Issue one request, logging method/url/payload then status/elapsed/body.

    Does NOT raise on non-2xx (several checks expect 4xx) — only a transport-level
    failure propagates.
    """
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


# --- per-assessment tracking -------------------------------------------------

@dataclass
class Assessment:
    label: str            # "homework" / "quiz" / "exam"
    surface: str          # "assessments" / "exams"
    assessment_type: str  # homework / quiz / exam
    source_id: int
    course_id: str
    student_id: int
    is_handwritten: bool
    title: str
    marking_url: str
    questions_url: str | None = None
    answers_url: str | None = None
    typed_answers: dict | None = None
    rubric_free: bool = False  # homework registered WITHOUT a marking scheme (knowledge-graded)
    # outcomes
    registered: bool = False
    total_points: float | None = None
    question_count: int | None = None
    job_id: str | None = None
    status: str | None = None
    percentage: float | None = None
    points_earned: float | None = None
    points_possible: float | None = None
    error: str | None = None


def summarize_scorecard(a: Assessment, sc: dict) -> None:
    log("")
    log(f"SCORECARD — {a.label} ({a.assessment_type} #{a.source_id})")
    log(f"  subject               : {sc.get('subject')}")
    if sc.get("grading_mode") == "knowledge":
        # Rubric-free homework: right/wrong from the model's own knowledge, no marks.
        log("  grading_mode          : knowledge (right/wrong, no marks)")
        log(f"  correct               : {sc.get('correct_count')} / {sc.get('questions_total')}")
        log(f"  review_flags          : {sc.get('review_flags')}")
        for q in sc.get("questions", []):
            log(
                f"    - {q.get('question_id')}: {q.get('verdict')}  "
                f"(correct answer: {q.get('correct_answer')})"
            )
        return
    log(f"  percentage            : {sc.get('percentage')}")
    log(f"  points earned/possible: {sc.get('total_points_earned')} / {sc.get('total_points_possible')}")
    log(f"  questions_graded      : {sc.get('questions_graded')}")
    log(f"  review_flags          : {sc.get('review_flags')}")
    has_summaries = bool(sc.get("student_summary") or sc.get("teacher_summary") or sc.get("parent_summary"))
    log(f"  audience_summaries     : {'present' if has_summaries else 'none'}")
    for q in sc.get("questions", []):
        log(
            f"    - {q.get('question_id')}: {q.get('points_earned')}/{q.get('points_possible')} "
            f"({q.get('status')})"
        )


# --- phases ------------------------------------------------------------------

def register_assessment(client: httpx.Client, a: Assessment) -> None:
    """Register on the NEW surface (assessment_type in the body)."""
    log("")
    log(f"REGISTER (assessments) — {a.label}: {a.assessment_type} #{a.source_id} "
        f"({'handwritten' if a.is_handwritten else 'typed'})")
    payload = {
        "assessment_type": a.assessment_type,
        "source_id": a.source_id,
        "course_id": a.course_id,
        "title": a.title,
        "is_handwritten": a.is_handwritten,
    }
    if not a.rubric_free:  # rubric-free homework omits the marking scheme on purpose
        payload["marking_scheme_pdf_url"] = a.marking_url
    if a.questions_url:
        payload["questions_pdf_url"] = a.questions_url
    try:
        resp, body = call(client, "POST", "/grader/assessments/register", payload, timeout=TIMEOUT_REGISTER)
    except httpx.HTTPError as exc:
        a.error = f"register request failed: {type(exc).__name__}: {exc}"
        check(f"{a.label}: register -> 201", False, a.error)
        return
    ok = resp.status_code == 201 and isinstance(body, dict)
    check(f"{a.label}: register -> 201", ok, f"got {resp.status_code}")
    if not ok:
        a.error = f"register failed: {resp.status_code} {body}"
        return
    check(f"{a.label}: response echoes assessment_type={a.assessment_type}",
          body.get("assessment_type") == a.assessment_type)
    check(f"{a.label}: response carries source_id (not test_id)",
          body.get("source_id") == a.source_id and "test_id" not in body,
          f"source_id={body.get('source_id')} test_id_present={'test_id' in body}")
    if a.rubric_free:
        # No marking scheme -> no rubric parsed -> 0 points / 0 questions, no Gemini call.
        check(f"{a.label}: registered rubric-free (question_count == 0)",
              body.get("question_count") == 0 and (body.get("total_points") or 0) == 0,
              f"question_count={body.get('question_count')} total_points={body.get('total_points')}")
    else:
        check(f"{a.label}: rubric parsed (question_count > 0)",
              isinstance(body.get("question_count"), int) and body["question_count"] > 0,
              f"question_count={body.get('question_count')} total_points={body.get('total_points')}")
    a.registered = True
    a.total_points = body.get("total_points")
    a.question_count = body.get("question_count")


def register_exam(client: httpx.Client, a: Assessment) -> None:
    """Register on the ORIGINAL exam surface (regression)."""
    log("")
    log(f"REGISTER (exams, regression) — {a.assessment_type} #{a.source_id} "
        f"({'handwritten' if a.is_handwritten else 'typed'})")
    payload = {
        "test_id": a.source_id,
        "course_id": a.course_id,
        "test_name": a.title,
        "is_handwritten": a.is_handwritten,
        "marking_scheme_pdf_url": a.marking_url,
    }
    if a.questions_url:
        payload["questions_pdf_url"] = a.questions_url
    try:
        resp, body = call(client, "POST", "/grader/register-exam", payload, timeout=TIMEOUT_REGISTER)
    except httpx.HTTPError as exc:
        a.error = f"register-exam request failed: {type(exc).__name__}: {exc}"
        check(f"{a.label}: register-exam -> 201 with test_id", False, a.error)
        return
    ok = resp.status_code == 201 and isinstance(body, dict) and body.get("test_id") == a.source_id
    check(f"{a.label}: register-exam -> 201 with test_id", ok, f"got {resp.status_code}")
    if not ok:
        a.error = f"register-exam failed: {resp.status_code} {body}"
        return
    a.registered = True
    a.total_points = body.get("total_points")
    a.question_count = body.get("question_count")


def submit(client: httpx.Client, a: Assessment) -> None:
    log("")
    log(f"SUBMIT — {a.label} ({a.surface})")
    if a.surface == "assessments":
        path = f"/grader/assessments/{a.source_id}/submissions"
        payload: dict = {"assessment_type": a.assessment_type, "student_id": a.student_id}
    else:
        path = f"/grader/exams/{a.source_id}/submissions"
        payload = {"student_id": a.student_id}
    if a.is_handwritten:
        payload["answers_pdf_url"] = a.answers_url
    else:
        payload["answers"] = a.typed_answers
    try:
        resp, body = call(client, "POST", path, payload)
    except httpx.HTTPError as exc:
        a.error = f"submit request failed: {type(exc).__name__}: {exc}"
        check(f"{a.label}: submit -> 202 with job_id", False, a.error)
        return
    ok = resp.status_code == 202 and isinstance(body, dict) and body.get("job_id")
    check(f"{a.label}: submit -> 202 with job_id", ok, f"got {resp.status_code}")
    if ok:
        a.job_id = body["job_id"]
        log(f"   job_id = {a.job_id}")
    else:
        a.error = f"submit failed: {resp.status_code} {body}"


def poll_until_done(client: httpx.Client, assessments: list[Assessment]) -> None:
    """Poll every submitted job (across both surfaces) until terminal or the cap."""
    rule("=")
    log(f"POLL grading jobs every {POLL_INTERVAL_SECONDS}s (cap {POLL_CAP_SECONDS}s)")
    pending = {a.job_id: a for a in assessments if a.job_id and a.status not in ("succeeded", "failed")}
    t_start = perf_counter()
    poll_n = 0
    while pending:
        poll_n += 1
        elapsed = int(perf_counter() - t_start)
        for job_id, a in list(pending.items()):
            poll_path = (
                f"/grader/assessments/jobs/{job_id}" if a.surface == "assessments"
                else f"/grader/jobs/{job_id}"
            )
            try:
                _resp, body = call(client, "GET", poll_path)
            except httpx.HTTPError as exc:
                log(f"   [{a.label}] transient poll error: {exc}; retrying next cycle")
                continue
            status = body.get("status") if isinstance(body, dict) else None
            log(f"   poll #{poll_n} (t+{elapsed}s) [{a.label}]: status={status}")
            if status in ("succeeded", "failed"):
                a.status = status
                if status == "succeeded":
                    sc = body.get("scorecard") or {}
                    a.percentage = sc.get("percentage")
                    a.points_earned = sc.get("total_points_earned")
                    a.points_possible = sc.get("total_points_possible")
                    summarize_scorecard(a, sc)
                    if a.rubric_free:
                        vok = (
                            sc.get("grading_mode") == "knowledge"
                            and isinstance(sc.get("correct_count"), int)
                            and isinstance(sc.get("questions_total"), int)
                            and all(q.get("verdict") for q in sc.get("questions", []))
                        )
                        check(f"{a.label}: knowledge grade (verdicts + X/Y correct, no marks)", vok,
                              f"mode={sc.get('grading_mode')} "
                              f"correct={sc.get('correct_count')}/{sc.get('questions_total')}")
                    else:
                        check(f"{a.label}: grading succeeded + scorecard present",
                              isinstance(sc, dict) and sc.get("percentage") is not None)
                else:
                    a.error = body.get("error") if isinstance(body, dict) else "failed"
                    check(f"{a.label}: grading succeeded", False, f"failed: {a.error}")
                del pending[job_id]
        if not pending:
            break
        if perf_counter() - t_start > POLL_CAP_SECONDS:
            for a in pending.values():
                a.status = a.status or "timeout"
                a.error = a.error or f"poll cap {POLL_CAP_SECONDS}s exceeded"
                check(f"{a.label}: grading finished within cap", False, a.error)
            break
        sleep(POLL_INTERVAL_SECONDS)


def check_assessment_listings(client: httpx.Client, hw: Assessment, quiz: Assessment) -> None:
    rule("=")
    log("LIST assessments (per-type + type isolation)")
    _, hw_body = call(client, "GET", f"/grader/assessments?assessment_type=homework&course_id={hw.course_id}")
    hw_ids = [x.get("source_id") for x in hw_body.get("assessments", [])] if isinstance(hw_body, dict) else []
    check("list ?assessment_type=homework includes our homework", hw.source_id in hw_ids, f"ids={hw_ids}")
    check("homework list rows all carry assessment_type=homework",
          all(x.get("assessment_type") == "homework" for x in hw_body.get("assessments", [])))

    _, q_body = call(client, "GET", "/grader/assessments?assessment_type=quiz")
    q_ids = [x.get("source_id") for x in q_body.get("assessments", [])] if isinstance(q_body, dict) else []
    check("list ?assessment_type=quiz includes our quiz", quiz.source_id in q_ids, f"ids={q_ids}")
    check("type isolation: homework NOT in the quiz listing", hw.source_id not in q_ids)


def check_error_envelope(client: httpx.Client) -> None:
    rule("=")
    log("ERROR ENVELOPE — new surface (no LLM cost)")

    # 'exam' is not a valid body enum for this surface -> 422.
    resp, _ = call(client, "POST", "/grader/assessments/register", {
        "assessment_type": "exam", "source_id": 1, "course_id": "16", "title": "X",
        "is_handwritten": False, "marking_scheme_pdf_url": "https://example.com/ms.pdf",
    })
    check("register assessment_type='exam' -> 422", resp.status_code == 422, f"got {resp.status_code}")

    # Handwritten without a questions PDF -> 422 (schema validator).
    resp, _ = call(client, "POST", "/grader/assessments/register", {
        "assessment_type": "homework", "source_id": 1, "course_id": "16", "title": "X",
        "is_handwritten": True, "marking_scheme_pdf_url": "https://example.com/ms.pdf",
    })
    check("register handwritten w/o questions_pdf -> 422", resp.status_code == 422, f"got {resp.status_code}")

    # Submit to an unregistered source -> 404 TEST_NOT_REGISTERED.
    resp, body = call(client, "POST", "/grader/assessments/999999/submissions", {
        "assessment_type": "homework", "student_id": 1, "answers": {"1": "a"},
    })
    check("submit unregistered source -> 404 TEST_NOT_REGISTERED",
          resp.status_code == 404 and isinstance(body, dict) and body.get("error_code") == "TEST_NOT_REGISTERED",
          f"got {resp.status_code} {body.get('error_code') if isinstance(body, dict) else body}")

    # Jobs list with no filter -> 400 MISSING_JOB_FILTER.
    resp, body = call(client, "GET", "/grader/assessments/jobs?assessment_type=homework")
    check("jobs list (no student_id/source_id) -> 400 MISSING_JOB_FILTER",
          resp.status_code == 400 and isinstance(body, dict) and body.get("error_code") == "MISSING_JOB_FILTER",
          f"got {resp.status_code} {body.get('error_code') if isinstance(body, dict) else body}")

    # Poll an unknown job -> 404 JOB_NOT_FOUND.
    resp, body = call(client, "GET", "/grader/assessments/jobs/does-not-exist")
    check("poll unknown job -> 404 JOB_NOT_FOUND",
          resp.status_code == 404 and isinstance(body, dict) and body.get("error_code") == "JOB_NOT_FOUND",
          f"got {resp.status_code} {body.get('error_code') if isinstance(body, dict) else body}")


def check_assessment_jobs(client: httpx.Client, hw: Assessment, quiz: Assessment) -> None:
    rule("=")
    log("LOOKUP assessment jobs (summary list + single-job detail)")

    # by student_id
    _, body = call(client, "GET", f"/grader/assessments/jobs?assessment_type=homework&student_id={hw.student_id}")
    jobs = body.get("jobs", []) if isinstance(body, dict) else []
    row = next((j for j in jobs if j.get("job_id") == hw.job_id), None)
    check("assessments/jobs?student_id returns our homework job", row is not None)
    if row is not None:
        check("homework job summary carries assessment_type + source_id",
              row.get("assessment_type") == "homework" and row.get("source_id") == hw.source_id)
        check("summary omits the full scorecard (lightweight)", "scorecard" not in row)
        if hw.status == "succeeded":
            check("succeeded summary exposes numeric percentage",
                  isinstance(row.get("percentage"), (int, float)), f"percentage={row.get('percentage')}")

    # by source_id
    _, body = call(client, "GET", f"/grader/assessments/jobs?assessment_type=quiz&source_id={quiz.source_id}")
    q_jobs = body.get("jobs", []) if isinstance(body, dict) else []
    check("assessments/jobs?source_id returns our quiz job",
          any(j.get("job_id") == quiz.job_id for j in q_jobs))

    # single-job detail carries type + source_id (+ scorecard when done)
    if hw.job_id:
        _, body = call(client, "GET", f"/grader/assessments/jobs/{hw.job_id}")
        check("assessment job detail carries assessment_type + source_id",
              isinstance(body, dict) and body.get("assessment_type") == "homework"
              and body.get("source_id") == hw.source_id)
        if hw.status == "succeeded":
            check("assessment job detail includes the full scorecard",
                  isinstance(body, dict) and body.get("scorecard") is not None)


def check_exam_regression(client: httpx.Client, exam: Assessment) -> None:
    rule("=")
    log("EXAM REGRESSION — original /grader/jobs lookups still work")

    _, body = call(client, "GET", f"/grader/jobs?test_id={exam.source_id}")
    ex_jobs = body.get("jobs", []) if isinstance(body, dict) else []
    check("exams: /grader/jobs?test_id returns our exam job",
          any(j.get("job_id") == exam.job_id for j in ex_jobs))
    check("exam job summary keyed on test_id (unchanged contract)",
          all("test_id" in j for j in ex_jobs) if ex_jobs else False)

    if exam.job_id:
        _, body = call(client, "GET", f"/grader/jobs/{exam.job_id}")
        check("exams: /grader/jobs/{job_id} carries test_id",
              isinstance(body, dict) and body.get("test_id") == exam.source_id)
        if exam.status == "succeeded":
            check("exams: succeeded detail includes full scorecard",
                  isinstance(body, dict) and body.get("scorecard") is not None)

    resp, body = call(client, "GET", "/grader/jobs")
    check("exams: /grader/jobs no filter -> 400 MISSING_JOB_FILTER",
          resp.status_code == 400 and isinstance(body, dict) and body.get("error_code") == "MISSING_JOB_FILTER",
          f"got {resp.status_code}")


# --- main --------------------------------------------------------------------

def main() -> int:
    global _LOG_FH
    SCRATCH.mkdir(exist_ok=True)
    _LOG_FH = open(LOG_PATH, "w", buffering=1, encoding="utf-8")  # noqa: SIM115

    homework = Assessment(
        label="homework", surface="assessments", assessment_type="homework",
        source_id=HW_SOURCE_ID, course_id=HW_COURSE_ID, student_id=STUDENT_BASE,
        is_handwritten=True, title="Homework #123 (E2E TEST, graded vs standard AP Bio FRQ)",
        marking_url=HW_MARKING_URL, questions_url=HW_QUESTIONS_URL, answers_url=HW_ANSWERS_URL,
    )
    quiz = Assessment(
        label="quiz", surface="assessments", assessment_type="quiz",
        source_id=QUIZ_SOURCE_ID, course_id=QUIZ_COURSE_ID, student_id=STUDENT_BASE + 1,
        is_handwritten=False, title="AP World History Quiz #869 (E2E TEST)",
        marking_url=QUIZ_MARKING_URL, typed_answers=QUIZ_TYPED_ANSWERS,
    )
    exam = Assessment(
        label="exam", surface="exams", assessment_type="exam",
        source_id=EXAM_TEST_ID, course_id=EXAM_COURSE_ID, student_id=STUDENT_BASE + 2,
        is_handwritten=False, title="AP Statistics (regression E2E TEST)",
        marking_url=EXAM_MARKING_URL, typed_answers=EXAM_TYPED_ANSWERS,
    )
    # Rubric-free homework: no marking scheme -> graded from the model's own knowledge
    # (grading_mode="knowledge", verdicts + "X of Y correct", no marks). Graded TYPED.
    hw_knowledge = Assessment(
        label="homework-knowledge", surface="assessments", assessment_type="homework",
        source_id=HW_KNOWLEDGE_SOURCE_ID, course_id=HW_COURSE_ID, student_id=STUDENT_BASE + 3,
        is_handwritten=False, title="Homework (E2E TEST — no marking scheme, graded by AI knowledge)",
        marking_url="", questions_url=HW_QUESTIONS_URL, typed_answers=HW_KNOWLEDGE_TYPED_ANSWERS,
        rubric_free=True,
    )

    rule("=")
    log("GRADER ASSESSMENTS E2E — real homework + real quiz (new surface) + exam regression")
    log(f"base_url = {BASE_URL}")
    log(f"homework : docs_homework_test #{homework.source_id}  course {homework.course_id}  handwritten")
    log(f"quiz     : quiz #{quiz.source_id}  course {quiz.course_id}  typed  (REAL marking scheme)")
    log(f"exam     : tests #{exam.source_id}  course {exam.course_id}  typed  (regression)")
    log(f"hw(know) : docs_homework_test #{hw_knowledge.source_id}  course {hw_knowledge.course_id}  "
        f"typed  (NO marking scheme -> AI knowledge)")
    log(f"log file = {LOG_PATH}")
    rule("=")

    with httpx.Client() as client:
        # Fail fast if the server is down.
        try:
            call(client, "GET", "/grader/exams")
        except httpx.HTTPError:
            log(f"!! could not reach {BASE_URL}. Start the server, then re-run.")
            return _finish([homework, quiz, hw_knowledge, exam])

        # 1) Contract/error checks first (cheap, no LLM).
        check_error_envelope(client)

        # 2) Register everything (synchronous rubric parse; slow).
        rule("=")
        log("REGISTRATION")
        register_assessment(client, homework)
        register_assessment(client, quiz)
        register_assessment(client, hw_knowledge)
        register_exam(client, exam)

        # 3) Listings (new surface) once both assessments are registered.
        if homework.registered and quiz.registered:
            check_assessment_listings(client, homework, quiz)

        # 4) Submit everything, then poll all jobs together (grades overlap).
        rule("=")
        log("SUBMISSIONS")
        for a in (homework, quiz, hw_knowledge, exam):
            if a.registered:
                submit(client, a)

        poll_until_done(client, [homework, quiz, hw_knowledge, exam])

        # 5) Job lookups (both surfaces) after grading.
        check_assessment_jobs(client, homework, quiz)
        check_exam_regression(client, exam)

    return _finish([homework, quiz, hw_knowledge, exam])


def _finish(assessments: list[Assessment]) -> int:
    passed = sum(1 for _, ok, _ in _CHECKS if ok)
    failed = [f"{label}{f'  ({detail})' if detail else ''}" for label, ok, detail in _CHECKS if not ok]

    rule("=")
    log("FINAL REPORT")
    rule("=")
    log(f"  checks run : {len(_CHECKS)}   passed : {passed}   failed : {len(failed)}")
    for f in failed:
        log(f"    - FAILED: {f}")
    log("")
    log("  per-assessment outcome:")
    for a in assessments:
        pct = "-" if a.percentage is None else f"{a.percentage:.1f}%"
        pts = (
            f"{a.points_earned}/{a.points_possible}"
            if a.points_earned is not None else "-"
        )
        log(f"    {a.label:<9} {a.assessment_type:<9} #{a.source_id:<7} "
            f"mode={'handwritten' if a.is_handwritten else 'typed':<11} "
            f"status={a.status or 'n/a':<10} score={pct:<8} pts={pts}"
            + (f"  err={a.error}" if a.error else ""))
    rule("=")

    RESULTS_PATH.write_text(json.dumps({
        "base_url": BASE_URL,
        "checks_run": len(_CHECKS),
        "passed": passed,
        "failed": len(failed),
        "failures": failed,
        "assessments": [asdict(a) for a in assessments],
        "checks": [{"label": lb, "passed": ok, "detail": d} for lb, ok, d in _CHECKS],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"(saved {RESULTS_PATH} + {LOG_PATH})")

    graded_ok = all(a.status == "succeeded" for a in assessments if a.registered)
    return 0 if (not failed and graded_ok and _CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
