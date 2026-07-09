"""Vendored AP FRQ Auto-Grader core — synced from notebooks/Grader/helpers.py.

Faithful copy of the grader's pipeline primitives, imported by the grader
services (app/services/grader_*.py). Keep in sync with the source repo: port
logic changes back to the grader rather than editing the business logic here.

Phases 0-2 surface:
    - render_pdf_to_images
    - get_gemini_client            (AI Studio API key OR Vertex AI service account)
    - ocr_submission               (joint OCR over question PDF + answer PDF)
    - load_rubric                  (parse marking-scheme PDF, cached as .parsed.json)
    - grade_question               (one rubric + one transcript -> QuestionScorecard)
    - character_error_rate         (optional, for manual OCR validation)
"""
from __future__ import annotations

import base64
import html
import os
import random
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal, TypedDict

from google import genai
from google.genai import types
from PIL import Image

# OpenTelemetry context — used to carry the active trace (e.g. Langfuse's
# ``grader.job`` span) into the grading thread pool in ``grade_questions_parallel``.
# Optional for this vendored package: degrade to a no-op if it isn't installed.
try:
    from opentelemetry import context as _otel_context
except Exception:  # pragma: no cover - otel ships alongside langfuse in the app
    _otel_context = None

from .schemas import (
    ParsedRubric,
    ParsedSubmission,
    QuestionRubric,
    QuestionScorecard,
    RubricPointScore,
    Scorecard,
    TranscribedAnswer,
)


class ExamFolder(TypedDict):
    slug: str
    subject: str
    folder: Path
    questions_pdf: Path
    answers_pdf: Path
    marking_scheme_pdf: Path


class GradeExamResult(TypedDict):
    scorecard: Scorecard
    submission: ParsedSubmission
    answer_images: list[Image.Image]
    rubric: ParsedRubric
    qids_to_grade: list[str]
    missing_qids: list[str]
    recovered_qids: list[str]
    merged_parent_answers: dict[str, TranscribedAnswer]


class GradeSubmissionResult(TypedDict):
    """Result of grading a pre-built submission against a pre-parsed rubric.

    Like :class:`GradeExamResult` minus ``answer_images`` — a caller that builds
    the submission itself (server-side OCR or typed-answer labelling) renders no
    answer images for the grading step.
    """
    scorecard: Scorecard
    submission: ParsedSubmission
    rubric: ParsedRubric
    qids_to_grade: list[str]
    missing_qids: list[str]
    recovered_qids: list[str]
    merged_parent_answers: dict[str, TranscribedAnswer]


_ALL_QUESTIONS: Literal["all"] = "all"


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def render_pdf_to_images(pdf_path: Path, dpi: int = 300) -> list[Image.Image]:
    """Render every page of a PDF to a PIL Image at the given DPI."""
    try:
        import fitz  # PyMuPDF — lazy import so the app/tests load without it (#73)
    except ImportError as exc:
        raise RuntimeError(
            "PyMuPDF (pymupdf) is required to render PDFs for the grader but is not "
            "installed. PyMuPDF has no wheel for Python 3.14 yet — use Python 3.11–3.13 "
            "and reinstall with `pip install -r requirements.txt`."
        ) from exc
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(pdf_path)
    try:
        scale = dpi / 72.0  # PDF user-space is 72 DPI
        matrix = fitz.Matrix(scale, scale)
        images: list[Image.Image] = []
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            images.append(img)
        return images
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Gemini client — auto-detect AI Studio vs Vertex AI
# ---------------------------------------------------------------------------

def get_gemini_client(timeout_ms: int = 300_000, prefer_vertex: bool = False) -> genai.Client:
    """Build a Gemini client. Prefers GEMINI_API_KEY (AI Studio); falls back to Vertex.

    `prefer_vertex` (APP-SPECIFIC — not in the vendored source): when True and a
    usable Vertex service account is configured, route through Vertex AI *even
    if* GEMINI_API_KEY is also set. The grader's handwriting-OCR call routinely
    runs ~150s — longer than AI Studio's server-side request deadline (it returns
    504 DEADLINE_EXCEEDED) but fine on Vertex's global endpoint — so the grader
    opts in via ``settings.grader_use_vertex``. Falls back to the API key when
    Vertex isn't usable, so API-key-only setups are unaffected.

    `timeout_ms` is the per-request HTTP timeout in milliseconds (google-genai
    measures timeouts in ms; the SDK default is no explicit timeout). The
    5-minute default comfortably covers a heavy Pro-model OCR call so it isn't
    cancelled mid-flight; pair it with `generate_with_retry` for transient
    499/503 blips.

    Vertex AI mode requires:
        GOOGLE_APPLICATION_CREDENTIALS  → path to service-account JSON on disk
        GOOGLE_CLOUD_PROJECT            → GCP project ID
        GOOGLE_CLOUD_LOCATION           → optional, defaults to us-central1
    """
    http_options = types.HttpOptions(timeout=timeout_ms)
    api_key = os.environ.get("GEMINI_API_KEY")

    # App-specific opt-in: force Vertex regardless of a present GEMINI_API_KEY.
    # Honour it when GOOGLE_CLOUD_PROJECT is set and Vertex is actually usable —
    # either via Application Default Credentials (no GOOGLE_APPLICATION_CREDENTIALS,
    # e.g. an instance-attached service account) OR an explicit key file that
    # exists on disk. A configured-but-missing key path falls back to AI Studio
    # instead of erroring, so an API-key-only deployment never regresses.
    if prefer_vertex:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if project and (not cred_path or Path(cred_path).is_file()):
            location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
            return genai.Client(vertexai=True, project=project, location=location,
                                http_options=http_options)

    if api_key:
        return genai.Client(api_key=api_key, http_options=http_options)

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError(
                "GOOGLE_APPLICATION_CREDENTIALS is set but GOOGLE_CLOUD_PROJECT is not. "
                "Add GOOGLE_CLOUD_PROJECT=<your-gcp-project-id> to Grader/.env."
            )
        # Gemini 3.x is served from the "global" endpoint on Vertex AI; regional
        # endpoints like us-central1 return 404 for these models.
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        return genai.Client(vertexai=True, project=project, location=location,
                            http_options=http_options)

    raise RuntimeError(
        "No Gemini credentials found. In Grader/.env set either:\n"
        "  GEMINI_API_KEY=...                                  (AI Studio, simpler)\n"
        "or:\n"
        "  GOOGLE_APPLICATION_CREDENTIALS=C:/path/to/sa.json   (Vertex AI)\n"
        "  GOOGLE_CLOUD_PROJECT=<your-gcp-project-id>\n"
    )


# ---------------------------------------------------------------------------
# Transient-error retry around Gemini calls
# ---------------------------------------------------------------------------

# Worth retrying: rate limits, 5xx server errors, and the mid-flight
# cancellations (499 CANCELLED / DEADLINE_EXCEEDED) seen on the preview models.
_RETRYABLE_CODES = {408, 429, 499, 500, 502, 503, 504}
_RETRYABLE_STATUSES = {
    "CANCELLED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL",
    "RESOURCE_EXHAUSTED", "ABORTED",
}


def _is_transient(exc: Exception) -> bool:
    """True if `exc` looks like a transient Gemini API error worth retrying."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if isinstance(code, int) and code in _RETRYABLE_CODES:
        return True
    if isinstance(status, str) and status.upper() in _RETRYABLE_STATUSES:
        return True
    blob = str(exc).upper()
    if any(s in blob for s in _RETRYABLE_STATUSES):
        return True
    return any(str(c) in blob for c in _RETRYABLE_CODES)


def _looks_empty(response) -> bool:
    """True if the response carries no parsed structured content.

    All grader call sites use ``response_schema``, so ``response.parsed is None``
    means the model produced nothing usable — typically a safety/recitation
    filter, a MAX_TOKENS truncation of the structured output, or a transient
    blank response. Worth retrying.
    """
    return response is None or getattr(response, "parsed", None) is None


def _diagnose_empty(response) -> str:
    """One-line description of why a response has no parsed content."""
    if response is None:
        return "response is None"
    finish = block = None
    try:
        cands = list(getattr(response, "candidates", None) or [])
        if cands:
            finish = getattr(cands[0], "finish_reason", None)
    except Exception:
        pass
    try:
        pf = getattr(response, "prompt_feedback", None)
        if pf is not None:
            block = getattr(pf, "block_reason", None)
    except Exception:
        pass
    text_len = len(getattr(response, "text", None) or "")
    return f"finish_reason={finish!r}, block_reason={block!r}, text_len={text_len}"


def generate_with_retry(
    client: genai.Client,
    *,
    max_attempts: int = 4,
    base_delay: float = 2.0,
    label: str = "",
    on_response: Callable[..., None] | None = None,
    **kwargs,
):
    """Call ``client.models.generate_content`` with retry on transient failures.

    Retries on two distinct flavours of transience:

    1. **Exception-based** — 429/499/5xx and CANCELLED/UNAVAILABLE/
       DEADLINE_EXCEEDED — with exponential backoff + jitter.
    2. **Empty response** — the call succeeded HTTP-wise but came back with no
       parsed content (``response.parsed is None``), typically a safety filter,
       MAX_TOKENS truncation of structured output, or a transient blank reply.

    Non-transient exceptions (400 bad request, auth failures) raise immediately.
    On the final attempt an empty response is returned to the caller so it can
    raise with the diagnostic from ``_diagnose_empty``.

    ``on_response``, when given, is called with each successful raw response
    (i.e. every billed attempt) — used by a host app to record token usage and
    cost. Hook exceptions are swallowed so tracing never breaks grading.
    """
    tag = f" [{label}]" if label else ""
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(**kwargs)
        except Exception as exc:
            if attempt == max_attempts or not _is_transient(exc):
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            print(f"    transient Gemini error{tag} (attempt {attempt}/{max_attempts}): {exc}")
            print(f"    retrying in {delay:.1f}s...")
            time.sleep(delay)
            continue
        if on_response is not None:
            try:
                on_response(response, kwargs.get("contents"), label)
            except Exception as hook_exc:  # never let a tracing hook break a grade
                print(f"    on_response hook failed{tag}: {hook_exc}")
        if _looks_empty(response):
            if attempt == max_attempts:
                return response  # let the caller raise with full diagnostics
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            print(f"    empty Gemini response{tag} (attempt {attempt}/{max_attempts}): "
                  f"{_diagnose_empty(response)}")
            print(f"    retrying in {delay:.1f}s...")
            time.sleep(delay)
            continue
        return response

    # Unreachable: the final attempt always returns above or raises; making the
    # non-return explicit satisfies RET503 and guards against a future edit
    # silently falling through to an implicit ``return None``.
    raise RuntimeError("generate_with_retry exhausted all attempts without returning")


# ---------------------------------------------------------------------------
# Question-ID normalization — canonical lowercase form everywhere
# ---------------------------------------------------------------------------

def _normalize_qid(qid: str) -> str:
    """Canonicalize a question identifier to lowercase + trimmed.

    Both the OCR and rubric-parse passes can drift on sub-part casing — Gemini
    sometimes returns ``"1A"`` / ``"2B-i"`` when the prompt says ``"1a"`` /
    ``"2b-i"``. Without normalization, the grader's ``answers ∩ rubric`` set
    is empty and every sub-part is silently scored 0/max via
    :func:`build_unattempted_scorecards`. Applying ``.lower()`` at every
    ingestion point guarantees both sides match.
    """
    return (qid or "").strip().lower()


def _normalize_rubric_qids(rubric) -> None:
    """In-place: lowercase every question_id (and point_id) inside a ParsedRubric."""
    for q in rubric.questions:
        q.question_id = _normalize_qid(q.question_id)
        for p in q.rubric_points:
            p.question_id = _normalize_qid(p.question_id)
            p.point_id = _normalize_qid(p.point_id)


# ---------------------------------------------------------------------------
# OCR — joint pass over question + answer PDFs
# ---------------------------------------------------------------------------

def ocr_submission(
    client: genai.Client,
    question_images: list[Image.Image],
    answer_images: list[Image.Image],
    prompt_path: Path,
    model: str = "gemini-3.5-flash",
    thinking_level: str | None = None,
    subject_addendum: str = "",
    on_response: Callable[..., None] | None = None,
) -> ParsedSubmission:
    """OCR the student's handwritten answers, using the question PDF as context.

    Both PDFs go in one call so Gemini uses the canonical question IDs from
    the question PDF when labeling each transcribed answer — no separate
    segmentation step needed.

    `thinking_level` (Gemini 3.x): one of "minimal", "low", "medium", "high".
    Transcription is not a reasoning task, so "low"/"minimal" cuts latency
    sharply. Leave None to use the model's default. Ignored by models that
    predate thinking_level (e.g. 2.5), which use the legacy thinking_budget.

    `subject_addendum` is appended to the base prompt as an extra section
    when non-empty. Used to inject per-subject diagram-description guidance
    from :data:`config.SUBJECT_OCR_ADDENDA` (chemistry Lewis structures,
    physics free-body diagrams, biology cycle diagrams). Empty string leaves
    behaviour identical to subjects without an addendum.
    """
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    if subject_addendum:
        prompt = (
            prompt.rstrip()
            + "\n\n# Subject-specific OCR guidance\n"
            + subject_addendum.strip()
            + "\n"
        )

    contents: list = [prompt, "\n=== QUESTION PDF (typed) — context only, do not transcribe ===\n"]
    for i, img in enumerate(question_images, start=1):
        contents.append(f"[Question PDF page {i}/{len(question_images)}]")
        contents.append(img)

    contents.append("\n=== STUDENT ANSWER PDF (handwritten) — transcribe this ===\n")
    for i, img in enumerate(answer_images, start=1):
        contents.append(f"[Answer PDF page {i}/{len(answer_images)}]")
        contents.append(img)

    config_kwargs: dict = {
        "response_mime_type": "application/json",
        "response_schema": ParsedSubmission,
        "temperature": 0,
    }
    if thinking_level:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)

    response = generate_with_retry(
        client,
        label="OCR",
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(**config_kwargs),
        on_response=on_response,
    )

    parsed = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini returned no parsed ParsedSubmission ({_diagnose_empty(response)}). "
            f"Raw text:\n" + (response.text or "<empty>")
        )
    # Normalize sub-part casing — Gemini occasionally returns "1A" / "2B-i"
    # instead of the prompt-specified "1a" / "2b-i". Without this, the
    # answers-vs-rubric intersection is empty and everything scores 0/max.
    for ans in parsed.answers:
        ans.question_id = _normalize_qid(ans.question_id)
    return parsed  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Typed-answer labelling — split a major-question blob into rubric sub-parts
# ---------------------------------------------------------------------------

def label_typed_answers(
    client: genai.Client,
    *,
    answers_by_major: dict[str, str],
    rubric: ParsedRubric,
    prompt_path: Path,
    model: str = "gemini-3.5-flash",
    on_response: Callable[..., None] | None = None,
) -> tuple[ParsedSubmission, list[str]]:
    """Build a ParsedSubmission from typed answers stored by MAJOR question id.

    Some backends store a student's typed answer keyed by the major question
    number only (e.g. ``"3"``) as one block, while the rubric grades sub-parts
    (``"3a"``, ``"3b"``). For each major question whose rubric defines more than
    one sub-part, a text-only Gemini call (no OCR) splits the block into one
    ``TranscribedAnswer`` per sub-part. A question with a single sub-part (or
    none) passes through unchanged.

    ``answers_by_major`` maps a normalized major question id to the student's
    full typed answer for it. Returns ``(submission, ai_labelled_qids)`` where
    ``ai_labelled_qids`` are the sub-parts produced by an AI split — the caller
    should pass them to :func:`grade_submission` as ``force_review_qids`` so the
    model-generated attribution is flagged for human review.
    """
    base_prompt = Path(prompt_path).read_text(encoding="utf-8")
    answers: list[TranscribedAnswer] = []
    ai_labelled: list[str] = []

    for q in rubric.questions:
        major = _normalize_qid(q.question_id)
        blob = answers_by_major.get(major)
        if blob is None:
            continue  # student didn't answer this major question

        subpart_ids = sorted({_normalize_qid(p.question_id) for p in q.rubric_points})

        # No real sub-parts: grade the blob as-is under its sole qid.
        if len(subpart_ids) <= 1:
            qid = subpart_ids[0] if subpart_ids else major
            answers.append(TranscribedAnswer(
                question_id=qid, transcript=blob, confidence=1.0, source_pages=[],
            ))
            continue

        # Multiple sub-parts: ask Gemini to label the blob by sub-part.
        subpart_block = "\n".join(
            f"- {_normalize_qid(p.question_id)}: {p.criterion}" for p in q.rubric_points
        )
        user_message = (
            f"# Major question\n{major}\n\n"
            f"# Expected sub-parts (id: criterion)\n{subpart_block}\n\n"
            f"# Student's full typed answer to question {major}\n"
            f"```\n{blob}\n```\n"
        )
        response = generate_with_retry(
            client,
            label=f"typed-label {major}",
            model=model,
            contents=[base_prompt, user_message],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ParsedSubmission,
                temperature=0,
            ),
            on_response=on_response,
        )
        parsed = response.parsed
        if parsed is None:
            raise RuntimeError(
                f"Gemini returned no labelled ParsedSubmission for Q{major} "
                f"({_diagnose_empty(response)}). Raw text:\n{response.text or '<empty>'}"
            )

        wanted = set(subpart_ids)
        for ans in parsed.answers:
            ans.question_id = _normalize_qid(ans.question_id)
            if ans.question_id not in wanted:
                continue  # ignore any qid the model invented outside the rubric
            # Typed text — no OCR uncertainty; pin confidence and drop pages.
            ans.confidence = 1.0
            ans.source_pages = []
            answers.append(ans)
            ai_labelled.append(ans.question_id)

    submission = ParsedSubmission(answers=answers, unassigned_text=[], page_count=0)
    return submission, ai_labelled


# ---------------------------------------------------------------------------
# Rubric / marking-scheme parsing (Phase 1) — cached as .parsed.json sidecar
# ---------------------------------------------------------------------------

def parse_rubric_pdf(
    client: genai.Client,
    marking_scheme_pdf: Path,
    *,
    subject: str,
    year: int,
    set_label: str | None,
    prompt_path: Path,
    model: str = "gemini-3.5-flash",
    dpi: int = 200,
    on_response: Callable[..., None] | None = None,
) -> ParsedRubric:
    """Render a marking-scheme PDF and parse it into a ParsedRubric via Gemini.

    The pure parse — no sidecar caching. A server caches the returned rubric in
    its own store (e.g. a DB row) and calls this only once per exam; the
    notebook uses :func:`load_rubric`, which wraps this with a ``.parsed.json``
    sidecar so repeat runs skip Gemini.
    """
    pdf_path = Path(marking_scheme_pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Marking scheme PDF not found: {pdf_path}")

    images = render_pdf_to_images(pdf_path, dpi=dpi)
    prompt = Path(prompt_path).read_text(encoding="utf-8")

    context = (
        f"Subject: {subject}\n"
        f"Year:    {year}\n"
        f"Set:     {set_label or 'N/A'}\n"
    )

    contents: list = [prompt, context]
    for i, img in enumerate(images, start=1):
        contents.append(f"[Marking scheme page {i}/{len(images)}]")
        contents.append(img)

    response = generate_with_retry(
        client,
        label="rubric",
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ParsedRubric,
            temperature=0,
        ),
        on_response=on_response,
    )
    parsed = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini returned no parsed ParsedRubric ({_diagnose_empty(response)}). "
            f"Raw text:\n" + (response.text or "<empty>")
        )

    # Backfill metadata from context if model omitted it
    if not parsed.subject:
        parsed.subject = subject
    if not parsed.year:
        parsed.year = year
    if set_label and not parsed.set_label:
        parsed.set_label = set_label

    # Normalize sub-part casing so downstream matching stays clean.
    _normalize_rubric_qids(parsed)
    return parsed  # type: ignore[return-value]


def load_rubric(
    client: genai.Client,
    marking_scheme_pdf: Path,
    *,
    subject: str,
    year: int,
    set_label: str | None,
    prompt_path: Path,
    model: str = "gemini-3.5-flash",
    dpi: int = 200,
    force_reparse: bool = False,
) -> ParsedRubric:
    """Load a marking-scheme PDF into a ParsedRubric, cached as a sidecar.

    Thin wrapper around :func:`parse_rubric_pdf`: on first call it parses with
    Gemini and writes ``{pdf}.parsed.json`` next to the PDF; later calls load
    that sidecar and skip Gemini. Pass ``force_reparse=True`` (or delete the
    sidecar) to re-parse.
    """
    pdf_path = Path(marking_scheme_pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(f"Marking scheme PDF not found: {pdf_path}")

    cache_path = pdf_path.with_suffix(pdf_path.suffix + ".parsed.json")
    if cache_path.exists() and not force_reparse:
        cached = ParsedRubric.model_validate_json(cache_path.read_text(encoding="utf-8"))
        # Defensive: normalize old caches whose ids might be mixed-case.
        _normalize_rubric_qids(cached)
        return cached

    parsed = parse_rubric_pdf(
        client, pdf_path,
        subject=subject, year=year, set_label=set_label,
        prompt_path=prompt_path, model=model, dpi=dpi,
    )
    cache_path.write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
    return parsed


# ---------------------------------------------------------------------------
# Rubric flattening — align rubric granularity with OCR answer granularity
# ---------------------------------------------------------------------------

def flatten_rubric_by_subpart(rubric: ParsedRubric) -> dict[str, QuestionRubric]:
    """Regroup a ParsedRubric so each entry is a sub-part QuestionRubric.

    Why: the parsed rubric's top-level QuestionRubrics are keyed at
    question level ("1", "2", ...) but their rubric_points carry sub-part
    ids ("1a", "1b", ...) — and OCR labels student answers at sub-part
    granularity too. Matching at top level produces an empty intersection;
    matching at sub-part level grades correctly.
    """
    result: dict[str, QuestionRubric] = {}
    for q in rubric.questions:
        groups: dict[str, list] = defaultdict(list)
        for p in q.rubric_points:
            groups[p.question_id].append(p)
        for subpart_id, points in groups.items():
            result[subpart_id] = QuestionRubric(
                question_id=subpart_id,
                prompt_summary=q.prompt_summary,
                rubric_points=points,
                max_points=sum(p.point_value for p in points),
            )
    return result


# ---------------------------------------------------------------------------
# Grading (Phase 2) — one rubric + one transcript -> QuestionScorecard
# ---------------------------------------------------------------------------

def grade_question(
    client: genai.Client,
    question_rubric: QuestionRubric,
    answer: TranscribedAnswer,
    *,
    subject: str,
    prompt_path: Path,
    subject_addendum: str = "",
    model: str = "gemini-3.5-flash",
    review_recommended: bool = False,
    on_response: Callable[..., None] | None = None,
) -> QuestionScorecard:
    """Grade one transcribed answer against one question's rubric.

    Returns a QuestionScorecard with per-rubric-point awarded/denied,
    quoted rationale, and grading confidence. If `review_recommended` is
    True (upstream OCR confidence was low), every point score is flagged
    for human review in the final scorecard.
    """
    base_prompt = Path(prompt_path).read_text(encoding="utf-8")
    rubric_json = question_rubric.model_dump_json(indent=2)

    user_message = (
        f"# Subject\n{subject}\n\n"
        f"# Subject-specific guidance\n{subject_addendum or '(none)'}\n\n"
        f"# Rubric for this question\n```json\n{rubric_json}\n```\n\n"
        f"# Student's transcribed answer (question {answer.question_id})\n"
        f"OCR confidence: {answer.confidence:.2f}\n\n"
        f"```\n{answer.transcript}\n```\n"
    )

    response = generate_with_retry(
        client,
        label=f"grade {answer.question_id}",
        model=model,
        contents=[base_prompt, user_message],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=QuestionScorecard,
            temperature=0,
        ),
        on_response=on_response,
    )
    parsed = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini returned no parsed QuestionScorecard for Q{answer.question_id} "
            f"({_diagnose_empty(response)}). Raw text:\n{response.text or '<empty>'}"
        )

    if review_recommended:
        for ps in parsed.point_scores:
            ps.review_recommended = True

    return parsed  # type: ignore[return-value]


def grade_questions_parallel(
    client: genai.Client,
    qids: list[str],
    rubric_by_qid: dict[str, QuestionRubric],
    answer_by_qid: dict[str, TranscribedAnswer],
    *,
    subject: str,
    prompt_path: Path,
    subject_addendum: str = "",
    model: str = "gemini-3.5-flash",
    low_confidence_threshold: float = 0.75,
    max_workers: int = 8,
    verbose: bool = False,
    force_review_qids: set[str] | None = None,
    on_response: Callable[..., None] | None = None,
) -> list[QuestionScorecard]:
    """Grade many questions concurrently with a thread pool.

    Each `grade_question` call is an independent, I/O-bound Gemini request, so
    running them on threads gives near-linear speedup over a sequential loop
    without changing any per-question logic. Results are returned in the same
    order as `qids`; any qid missing from the rubric or the answers is skipped
    with a printed note (matching the previous sequential behaviour).

    ``force_review_qids`` flags every rubric point of those qids for human
    review regardless of OCR confidence — used for sub-parts whose transcript
    was recovered from a parent-level OCR block (see
    ``_synthesize_subpart_answers_from_parents``).
    """
    force_review = force_review_qids or set()
    # Resolve the work list up front, preserving qid order and reporting skips.
    work: list[tuple[str, QuestionRubric, TranscribedAnswer]] = []
    for qid in qids:
        qr = rubric_by_qid.get(qid)
        ans = answer_by_qid.get(qid)
        if qr is None:
            print(f"Skipping {qid}: not in parsed rubric")
            continue
        if ans is None:
            print(f"Skipping {qid}: no student answer found")
            continue
        work.append((qid, qr, ans))

    if not work:
        return []

    t0 = time.perf_counter()
    timings: dict[str, tuple[float, float]] = {}  # qid -> (start, end) relative to t0

    # Snapshot the active OTel trace context (e.g. the Langfuse ``grader.job``
    # span) so each worker thread can re-attach it. A bare ThreadPoolExecutor
    # does NOT propagate contextvars the way ``asyncio.to_thread`` does, so
    # without this every ``grade_question`` generation span would orphan onto
    # its own trace instead of nesting under the job — which is why per-question
    # grade cost was invisible on the job trace in Langfuse. No-op without otel.
    parent_ctx = _otel_context.get_current() if _otel_context is not None else None

    def _grade(item: tuple[str, QuestionRubric, TranscribedAnswer]):
        token = _otel_context.attach(parent_ctx) if parent_ctx is not None else None
        try:
            qid, qr, ans = item
            start = time.perf_counter() - t0
            qs = grade_question(
                client=client,
                question_rubric=qr,
                answer=ans,
                subject=subject,
                prompt_path=prompt_path,
                subject_addendum=subject_addendum,
                model=model,
                review_recommended=(
                    qid in force_review or ans.confidence < low_confidence_threshold
                ),
                on_response=on_response,
            )
            end = time.perf_counter() - t0
            timings[qid] = (start, end)
            if verbose:
                print(f"  [{threading.current_thread().name}] Q{qid}: "
                      f"start={start:5.1f}s end={end:5.1f}s ({end - start:4.1f}s)")
            return qid, qs
        finally:
            if token is not None:
                _otel_context.detach(token)

    results: dict[str, QuestionScorecard] = {}
    workers = max(1, min(max_workers, len(work)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_grade, item): item[0] for item in work}
        for fut in as_completed(futures):
            qid = futures[fut]
            try:
                _, qs = fut.result()
            except Exception as exc:  # surface which question failed
                raise RuntimeError(f"Grading failed for Q{qid}: {exc}") from exc
            results[qid] = qs

    _print_concurrency_report(timings, wall=time.perf_counter() - t0, workers=workers)

    # Return in the original qid order (as_completed yields out of order).
    return [results[qid] for qid, _, _ in work]


def _print_concurrency_report(
    timings: dict[str, tuple[float, float]],
    *,
    wall: float,
    workers: int,
) -> None:
    """Print a wall-time / overlap diagnostic for a parallel grading pass.

    ``timings`` maps qid -> (start, end) seconds relative to the pass's t0.
    Peak overlap is computed by sweeping the start/end events in time order.
    """
    busy = sum(e - s for s, e in timings.values())
    events: list[tuple[float, int]] = []
    for s, e in timings.values():
        events.append((s, 1))
        events.append((e, -1))
    events.sort()
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    print(f"\nConcurrency report: wall={wall:.1f}s | sum-of-calls={busy:.1f}s | "
          f"speedup={busy / wall if wall else 0:.1f}x | peak in-flight={peak}/{workers}")
    if peak <= 1:
        print("  ⚠️ Requests ran one-at-a-time (no overlap). Likely server-side "
              "rate-limiting on your Gemini tier — not a threading problem.")


# ---------------------------------------------------------------------------
# Sub-part recovery — rescue parent-level OCR blocks for sub-part rubrics
# ---------------------------------------------------------------------------

def _synthesize_subpart_answers_from_parents(
    answer_by_qid: dict[str, TranscribedAnswer],
    missing_qids: list[str],
) -> tuple[dict[str, TranscribedAnswer], list[str], list[str]]:
    """Recover sub-part answers from a parent-level transcript.

    If the OCR pass labeled a continuous unlabeled response with the parent
    question id (e.g. ``"4"`` because the student wrote one block addressing
    4a–4d without writing the sub-part labels themselves), every sub-part
    missing an explicit answer is given a copy of that parent transcript so
    the grader can locate per-rubric-point evidence inside the same block.
    Without this rescue, every such sub-part is silently scored 0/max via
    :func:`build_unattempted_scorecards` even though the student did write a
    response — the original bug this guards against.

    Matching rule: for each missing sub-part ``X``, the **longest existing**
    OCR'd question id for which :func:`_looks_like_subpart` accepts the pair
    wins (so ``"1b"`` is preferred over ``"1"`` when both exist for a
    sub-part like ``"1b-ii"``). The structural check matters — a raw
    ``startswith`` would incorrectly treat ``6a`` as a parent of the combined
    rubric qid ``6ab``, copying only ``6a``'s transcript and orphaning
    ``6b``'s prose. Combined-form qids are handled separately by
    :func:`_synthesize_parent_answers_from_subparts` via
    :func:`_expand_combined_qid`.

    Returns ``(updated_answer_by_qid, still_missing, recovered_qids)``.
    ``recovered_qids`` is the list of sub-parts we filled in — callers should
    flag those for human review (the per-sub-part attribution came from the
    grader rather than from explicit student labels).
    """
    updated = dict(answer_by_qid)
    recovered: list[str] = []
    still_missing: list[str] = []
    # Sort candidate parents longest-first so the most specific prefix wins.
    candidates = sorted(updated, key=len, reverse=True)
    for sub in missing_qids:
        parent = next(
            (c for c in candidates if _looks_like_subpart(sub, c)),
            None,
        )
        if parent is None:
            still_missing.append(sub)
            continue
        parent_ans = updated[parent]
        updated[sub] = TranscribedAnswer(
            question_id=sub,
            transcript=parent_ans.transcript,
            source_pages=list(parent_ans.source_pages),
            confidence=parent_ans.confidence,
            low_confidence_snippets=list(parent_ans.low_confidence_snippets),
        )
        recovered.append(sub)
    return updated, still_missing, recovered


# Lowercase Roman numerals 1-10 — used as sub-part markers under a parent
# letter (``3a-i``, ``3a-ii``, …). The set lets us tell a true sub-part
# (``3a-i``) from a combined-form qid (``3c-d`` covers parts c AND d).
_ROMAN_NUMERAL_SUBPARTS: frozenset[str] = frozenset({
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
})


def _looks_like_subpart(child: str, parent: str) -> bool:
    """True if ``child`` looks like a structural sub-part of ``parent``.

    A child is a sub-part only when the boundary between parent and child is
    *clean* — either a Roman-numeral dash-suffix (``3a`` -> ``3a-i``) or a
    digit-ending parent that gains a letter (``3`` -> ``3a``, ``6`` ->
    ``6ab``). This deliberately rejects two cases that look like sub-parts
    but aren't:

    - ``6a`` -> ``6ab``: letter glued onto letter with no delimiter. ``6ab``
      is a **combined** rubric qid covering parts a and b, not a child of
      ``6a``. The raw ``startswith`` rule used to false-match this and the
      parent-to-child rescue would copy only ``6a``'s transcript into ``6ab``
      while orphaning ``6b``'s prose — the original AP Statistics bug.
    - ``3c`` -> ``3c-d``: dash followed by a non-Roman letter. ``3c-d`` is a
      combined qid covering parts c and d, not a sub-part of c. We accept the
      dash form only when the suffix matches the project's Roman-numeral
      convention.

    Combined-form qids are handled separately by
    :func:`_expand_combined_qid`.
    """
    if child == parent or not child.startswith(parent):
        return False
    suffix = child[len(parent):]
    if suffix.startswith("-"):
        # Accept only Roman-numeral sub-part suffixes; everything else after
        # a dash is a combined-form range (``3c-d``) or an unknown form.
        rest = suffix[1:]
        return rest in _ROMAN_NUMERAL_SUBPARTS
    # No delimiter: only legitimate when the parent is question-level (ends
    # in a digit) and the suffix opens with a letter (the sub-part letter).
    return bool(parent) and parent[-1].isdigit() and suffix[:1].isalpha()


# Combined-qid forms commonly used in AP rubrics where one rubric point covers
# multiple sub-parts the student answered separately:
#   '6ab'   -> ['6a', '6b']        packed letters after the question number
#   '7abc'  -> ['7a', '7b', '7c']
#   '3c-d'  -> ['3c', '3d']        dashed letter range
#   '3a-c'  -> ['3a', '3b', '3c']
_PACKED_LETTERS_RE = re.compile(r"^(\d+)([a-z]{2,})$")
_LETTER_RANGE_RE = re.compile(r"^(\d+)([a-z])-([a-z])$")


def _expand_combined_qid(qid: str) -> list[str] | None:
    """Expand a combined rubric qid into its constituent sub-part qids.

    Returns ``None`` for single-sub-part qids (``3a``, ``3a-i``, ``6``) so
    the existing parent/sub-part recovery passes are unaffected. Returns the
    constituent list for combined forms — see module-level patterns.

    Used by :func:`_synthesize_parent_answers_from_subparts` to merge OCR'd
    constituents into a synthesized combined answer (e.g. concatenate ``6a``
    and ``6b`` transcripts so the ``6ab`` rubric can grade both halves of the
    response).
    """
    m = _PACKED_LETTERS_RE.match(qid)
    if m:
        num, letters = m.group(1), m.group(2)
        # Don't mistake an un-dashed Roman numeral (rare but defensive) for a
        # packed-letter combination — ``3ii`` is not ``[3i, 3i]``.
        if letters in _ROMAN_NUMERAL_SUBPARTS:
            return None
        return [num + ch for ch in letters]
    m = _LETTER_RANGE_RE.match(qid)
    if m:
        num, start, end = m.group(1), m.group(2), m.group(3)
        # ``i`` / ``v`` / ``x`` as a range endpoint is far more likely to be a
        # Roman-numeral sub-part (``3a-i``, ``3a-v``, ``3a-x``) than a literal
        # a-to-i range. The 24-sub-part theoretical case is sacrificed for
        # the common case; multi-char Romans (``ii``, ``iii``) can't reach
        # here anyway because the regex requires exactly one trailing letter.
        if end in _ROMAN_NUMERAL_SUBPARTS or start in _ROMAN_NUMERAL_SUBPARTS:
            return None
        if start < end:
            return [num + chr(c) for c in range(ord(start), ord(end) + 1)]
    return None


def _synthesize_parent_answers_from_subparts(
    answer_by_qid: dict[str, TranscribedAnswer],
    rubric_qids: set[str],
) -> tuple[dict[str, TranscribedAnswer], dict[str, TranscribedAnswer]]:
    """Fold orphan sub-part OCR blocks into the rubric parent they belong to.

    Inverse of :func:`_synthesize_subpart_answers_from_parents`: when OCR
    returned answers at finer granularity than the rubric expected, the
    sub-part transcripts are orphaned (no rubric entry to grade them
    against) and the parent's rubric points miss the evidence. Two flavours
    are both handled here:

    1. **Parent missing in OCR.** Rubric has ``"3a"`` with three rubric
       points for three calculations; OCR returned ``"3a-i"`` / ``"3a-ii"``
       / ``"3a-iii"`` separately. We synthesize a fresh parent answer by
       concatenating the children — without it the parent lands in
       ``missing_qids`` and is silently scored 0/max.
    2. **Parent present in OCR with extra orphan children.** Rubric has
       ``"1e"`` (with several points including a graph); OCR captured the
       student's first answer block as ``"1e"`` and the graph as ``"1e-ii"``.
       The graph points would be missed because the grader only sees
       ``"1e"``'s transcript. We append the orphan children's transcripts to
       the parent's existing one.
    3. **Combined-form rubric qid.** Rubric has ``"6ab"`` (one point covers
       parts a AND b, e.g. AP Statistics) but the student wrote ``"6a"``
       and ``"6b"`` separately. :func:`_expand_combined_qid` resolves
       ``"6ab"`` to ``["6a", "6b"]`` and both OCR blocks get folded under
       the combined rubric qid. The same applies to dashed ranges like
       ``"3c-d"``.

    Each orphan child is assigned to its **most specific** rubric ancestor
    (longest matching rubric qid), so OCR'd ``"1a-extra"`` lands under
    rubric ``"1a"`` rather than rubric ``"1"`` when both rubric qids exist.
    Orphan children are prefixed with their qid as a header (``[3a-i]``)
    inside the merged transcript so the grader can attribute evidence to the
    right sub-part.

    Returns ``(updated_answer_by_qid, merged_parent_answers)`` where
    ``merged_parent_answers`` maps parent qid -> the synthesized
    :class:`TranscribedAnswer`. Callers should flag those qids for human
    review — the merge format is generated, not what the student literally
    wrote at the parent's granularity.
    """
    # Assign each orphan OCR'd qid to its most-specific rubric ancestor.
    # "Orphan" = OCR'd qid that isn't itself a rubric entry, so it has no
    # direct grading target. It joins its ancestor either as a structural
    # sub-part (`_looks_like_subpart`) or as a combined-qid constituent
    # (`_expand_combined_qid`).
    by_parent: dict[str, list[str]] = defaultdict(list)
    sorted_rubric = sorted(rubric_qids, key=len, reverse=True)
    # Precompute the constituent lookup so combined-form matches are cheap.
    constituents_of: dict[str, list[str]] = {}
    for r in rubric_qids:
        parts = _expand_combined_qid(r)
        if parts:
            constituents_of[r] = parts
    for ocr_qid in answer_by_qid:
        if ocr_qid in rubric_qids:
            continue  # the grader will score it against its own rubric entry
        # Most-specific structural sub-part ancestor.
        parent = next(
            (r for r in sorted_rubric if _looks_like_subpart(ocr_qid, r)),
            None,
        )
        if parent is None:
            # Fall back to combined-form matching: the rubric qid bundles
            # multiple sub-parts (``"6ab"`` covers a AND b), and this OCR
            # answer is one of them.
            parent = next(
                (r for r in sorted_rubric if ocr_qid in constituents_of.get(r, ())),
                None,
            )
        if parent is not None:
            by_parent[parent].append(ocr_qid)

    updated = dict(answer_by_qid)
    merged: dict[str, TranscribedAnswer] = {}
    for parent, children in by_parent.items():
        children.sort()  # natural qid order: 3a-i, 3a-ii, 3a-iii
        contributors = (
            [parent, *children] if parent in answer_by_qid else list(children)
        )
        parts: list[str] = []
        for c in contributors:
            t = answer_by_qid[c].transcript
            # Tag every sub-part block so the grader can locate per-rubric-point
            # evidence; the parent's own block stays untagged so it reads naturally.
            parts.append(t if c == parent else f"[{c}]\n{t}")
        merged_transcript = "\n\n".join(parts)
        all_pages = sorted({
            p for c in contributors for p in answer_by_qid[c].source_pages
        })
        min_conf = min(answer_by_qid[c].confidence for c in contributors)
        all_snippets = [
            s for c in contributors
            for s in answer_by_qid[c].low_confidence_snippets
        ]
        synth = TranscribedAnswer(
            question_id=parent,
            transcript=merged_transcript,
            confidence=min_conf,
            source_pages=all_pages,
            low_confidence_snippets=all_snippets,
        )
        updated[parent] = synth
        merged[parent] = synth
    return updated, merged


# ---------------------------------------------------------------------------
# Unattempted sub-parts — score 0/max so the denominator is the whole exam
# ---------------------------------------------------------------------------

def build_unattempted_scorecards(
    rubric_by_qid: dict[str, QuestionRubric],
    missing_qids: list[str],
    *,
    mark_review: bool = True,
) -> list[QuestionScorecard]:
    """Synthesize 0/max scorecards for rubric sub-parts that have no answer.

    A sub-part present in the rubric but absent from the OCR'd answers is
    either a genuine blank or an OCR/segmentation miss. Either way it counts
    against the full-exam denominator, so we emit a QuestionScorecard worth
    0/max with one denied point per rubric point. With ``mark_review`` set,
    each point is flagged for human review so an OCR drop is visible rather
    than silently scored zero.
    """
    cards: list[QuestionScorecard] = []
    for qid in missing_qids:
        qr = rubric_by_qid.get(qid)
        if qr is None:
            continue
        point_scores = [
            RubricPointScore(
                point_id=p.point_id,
                awarded=False,
                points_earned=0.0,
                rationale=(
                    "No answer was transcribed for this sub-part "
                    "(blank or missed by OCR), so it earns no credit."
                ),
                transcript_evidence="",
                grading_confidence="high",
                review_recommended=mark_review,
            )
            for p in qr.rubric_points
        ]
        cards.append(
            QuestionScorecard(
                question_id=qid,
                points_earned=0.0,
                points_possible=qr.max_points,
                point_scores=point_scores,
                transcript_used="",
                summary_comment="No answer was transcribed for this sub-part.",
            )
        )
    return cards


# ---------------------------------------------------------------------------
# HTML report — answer pages side-by-side with graded points + evidence
# ---------------------------------------------------------------------------

def _img_to_data_uri(img: Image.Image, max_width: int = 1100, quality: int = 80) -> str:
    """Encode a PIL image as a base64 JPEG data URI, downscaled for file size."""
    im = img.convert("RGB")
    if im.width > max_width:
        ratio = max_width / im.width
        im = im.resize((max_width, int(im.height * ratio)), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


_HTML_STYLE = """
:root {
  --bg: #0f1115; --panel: #181b22; --panel-2: #1f232c; --border: #2a2f3a;
  --text: #e6e8eb; --muted: #9aa3b2; --accent: #6ea8fe;
  --good: #2fbf71; --good-bg: rgba(47,191,113,.12);
  --bad: #f0556b; --bad-bg: rgba(240,85,107,.10);
  --warn: #f5b942; --warn-bg: rgba(245,185,66,.12);
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.55; font-size: 15px;
}
.wrap { max-width: 1500px; margin: 0 auto; padding: 32px 24px 80px; }
header.report-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 24px; flex-wrap: wrap; padding: 28px 32px; margin-bottom: 28px;
  background: linear-gradient(135deg, #1d2330, #14171e);
  border: 1px solid var(--border); border-radius: 18px;
}
header.report-head h1 { margin: 0 0 4px; font-size: 26px; letter-spacing: -.3px; }
header.report-head .meta { color: var(--muted); font-size: 14px; }
.score-badge { text-align: center; min-width: 150px; }
.score-badge .pct { font-size: 46px; font-weight: 700; line-height: 1; letter-spacing: -1px; }
.score-badge .frac { color: var(--muted); font-size: 14px; margin-top: 6px; }
.bar { height: 9px; border-radius: 99px; background: var(--panel-2); overflow: hidden; margin-top: 12px; }
.bar > i { display: block; height: 100%; background: linear-gradient(90deg, var(--bad), var(--warn), var(--good)); }

.flags {
  border: 1px solid var(--warn); background: var(--warn-bg); color: #f7d489;
  border-radius: 12px; padding: 14px 18px; margin-bottom: 28px;
}
.flags strong { color: var(--warn); }
.flags ul { margin: 8px 0 0; padding-left: 20px; }

.page-block {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.05fr);
  gap: 24px; margin-bottom: 36px; align-items: start;
}
.page-block .page-title {
  grid-column: 1 / -1; font-size: 13px; text-transform: uppercase;
  letter-spacing: 1.2px; color: var(--muted); border-bottom: 1px solid var(--border);
  padding-bottom: 8px; margin-bottom: 4px;
}
.page-img {
  position: sticky; top: 20px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 14px; padding: 10px; overflow: hidden;
}
.page-img img { width: 100%; display: block; border-radius: 8px; }
.answers-col { display: flex; flex-direction: column; gap: 18px; }

.qcard { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; }
.qcard-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 18px; background: var(--panel-2); border-bottom: 1px solid var(--border);
}
.qcard-head .qid { font-weight: 700; font-size: 17px; }
.qcard-head .qscore { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--accent); }
.transcript {
  margin: 0; padding: 12px 18px; font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
  font-size: 12.5px; color: var(--muted); white-space: pre-wrap; word-break: break-word;
  background: #11141a; border-bottom: 1px solid var(--border); max-height: 220px; overflow: auto;
}
.points { padding: 6px 0; }
.point {
  padding: 12px 18px; border-bottom: 1px solid var(--border);
  display: grid; grid-template-columns: 26px 1fr; gap: 12px;
}
.point:last-child { border-bottom: none; }
.point .icon { font-size: 18px; line-height: 1.4; }
.point.awarded { background: var(--good-bg); }
.point.denied  { background: var(--bad-bg); }
.point .pid { font-weight: 600; }
.point .pts { color: var(--muted); font-weight: 500; font-variant-numeric: tabular-nums; }
.point .rationale { margin: 6px 0 0; }
.point .evidence {
  margin: 8px 0 0; padding: 8px 12px; border-left: 3px solid var(--accent);
  background: #11141a; border-radius: 0 8px 8px 0; font-family: ui-monospace, Consolas, monospace;
  font-size: 12.5px; color: #cdd6e4; white-space: pre-wrap; word-break: break-word;
}
.tag {
  display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px;
  border-radius: 99px; margin-left: 6px; vertical-align: middle;
}
.tag.high { background: var(--good-bg); color: var(--good); }
.tag.medium { background: var(--warn-bg); color: var(--warn); }
.tag.low { background: var(--bad-bg); color: var(--bad); }
.tag.review { background: rgba(110,168,254,.14); color: var(--accent); }
.no-answer { color: var(--muted); font-style: italic; padding: 24px; text-align: center;
  border: 1px dashed var(--border); border-radius: 12px; }
footer.report-foot { margin-top: 40px; color: var(--muted); font-size: 12px; text-align: center; }

.unattempted-block { margin: 0 0 36px; }
.unattempted-title {
  font-size: 13px; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--bad); border-bottom: 1px solid var(--border);
  padding-bottom: 8px; margin-bottom: 12px;
}
.unattempted-note { color: var(--muted); margin: 0 0 16px; max-width: 980px; }
.unattempted-note strong { color: var(--text); }
.qcard.unattempted-card { border-color: var(--bad); }
.qcard.unattempted-card .qcard-head { background: var(--bad-bg); }
.unattempted-card .ua-note {
  margin: 0; padding: 10px 18px; color: var(--muted); font-size: 13px;
  background: #11141a; border-bottom: 1px solid var(--border);
}
"""


def render_html_report(
    scorecard: Scorecard,
    submission: ParsedSubmission,
    answer_images: list[Image.Image],
    *,
    low_confidence_threshold: float = 0.75,
    recovered_qids: list[str] | None = None,
    merged_parent_answers: dict[str, TranscribedAnswer] | None = None,
) -> str:
    """Build a self-contained HTML report.

    Layout: one block per answer-PDF page. Left = the rendered page image
    (sticky); right = every question mapped to that page with its rubric
    points, each showing awarded/denied, points, rationale, and the exact
    transcript evidence quote. Images are embedded as base64 so the file is
    fully portable.

    ``recovered_qids`` lists sub-parts whose transcript was recovered from a
    parent-level OCR block (the student wrote one continuous unlabeled
    response to a multi-part question). They are rendered alongside the
    parent's answer pages — not in the Unattempted section — with a
    "shared transcript" tag, since each sub-part was graded against the same
    parent transcript. The parent's own (now redundant) "not graded" card is
    suppressed in favour of its children.

    ``merged_parent_answers`` maps a parent qid (e.g. ``"3a"``) to a
    synthesized :class:`TranscribedAnswer` whose transcript was assembled by
    concatenating per-sub-part OCR blocks (``"3a-i"``, ``"3a-ii"``,
    ``"3a-iii"``) the rubric expected as a single block. The merged parent
    card is rendered on the children's pages with a "merged from sub-parts"
    tag; the children's own (rubric-less) orphan cards are suppressed.
    """
    esc = html.escape
    scorecards_by_qid = {qs.question_id: qs for qs in scorecard.questions}
    ocr_by_qid = {a.question_id: a for a in submission.answers}
    recovered_list = list(recovered_qids or [])  # preserve caller order for stable card order
    recovered_set = set(recovered_list)
    merged_map = dict(merged_parent_answers or {})
    merged_set = set(merged_map)

    # For each recovered sub-part, find its OCR parent so we can place the
    # sub-part's qcard on the parent's pages and surface the parent transcript.
    # Mirrors the structural rule in `_synthesize_subpart_answers_from_parents`
    # — a raw `startswith` here would false-match combined qids like `6ab` as a
    # parent of `6a`, placing the qcard on the wrong page.
    def _parent_of(sub: str) -> str | None:
        for cand in sorted(ocr_by_qid, key=len, reverse=True):
            if _looks_like_subpart(sub, cand):
                return cand
        return None
    # Build as a plain dict (insertion-ordered) so qcards render in the order
    # the caller passed — typically the sorted sub-part order (4a, 4b, 4c, 4d).
    recovered_parents: dict[str, str] = {}
    for sub in recovered_list:
        parent = _parent_of(sub)
        if parent is not None:
            recovered_parents[sub] = parent
    parents_with_recovered_children = set(recovered_parents.values())

    # Children of merged parents whose own transcripts were absorbed into the
    # parent. They have no scorecard (they aren't in the rubric), so without
    # this suppression they'd render as orphan "not graded" cards next to the
    # real merged-parent card. Mirrors the rubric-aware guard inside
    # `_synthesize_parent_answers_from_subparts`: a child is consumed either
    # as a structural sub-part (``3a-i`` under ``3a``) or as a combined-qid
    # constituent (``6a`` and ``6b`` under ``6ab``; ``3c`` and ``3d`` under
    # ``3c-d``).
    consumed_children: set[str] = set()
    for parent in merged_set:
        constituents = set(_expand_combined_qid(parent) or [])
        for cand in ocr_by_qid:
            if cand in scorecards_by_qid:
                continue  # has its own scorecard — not consumed
            if _looks_like_subpart(cand, parent) or cand in constituents:
                consumed_children.add(cand)

    # Build the answer list we actually render: keep every OCR'd answer except
    # parents whose children took their place, and synthesize one entry per
    # recovered sub-part that inherits the parent's source_pages + transcript
    # so the grading appears beside the page image the student wrote on.
    augmented: list[TranscribedAnswer] = []
    for ans in submission.answers:
        if ans.question_id in parents_with_recovered_children:
            continue  # children render in its place — suppress the orphan card
        if ans.question_id in consumed_children:
            continue  # absorbed into a merged parent — suppress the orphan card
        augmented.append(ans)
    for sub, parent in recovered_parents.items():
        p_ans = ocr_by_qid[parent]
        augmented.append(TranscribedAnswer(
            question_id=sub,
            transcript=p_ans.transcript,
            confidence=p_ans.confidence,
            source_pages=list(p_ans.source_pages),
            low_confidence_snippets=list(p_ans.low_confidence_snippets),
        ))
    for _parent_qid, merged_ans in merged_map.items():
        augmented.append(merged_ans)

    # Map each page -> answers appearing on it (OCR order, recovered last).
    page_to_answers: dict[int, list[TranscribedAnswer]] = defaultdict(list)
    for ans in augmented:
        for p in ans.source_pages:
            page_to_answers[p].append(ans)

    def conf_tag(conf: str) -> str:
        return f'<span class="tag {esc(conf)}">{esc(conf)} confidence</span>'

    def render_point(ps) -> str:
        awarded = ps.awarded
        cls = "awarded" if awarded else "denied"
        icon = "✅" if awarded else "❌"
        review = '<span class="tag review">review</span>' if ps.review_recommended else ""
        evidence = (
            f'<div class="evidence">{esc(ps.transcript_evidence)}</div>'
            if ps.transcript_evidence else ""
        )
        return f"""
        <div class="point {cls}">
          <div class="icon">{icon}</div>
          <div>
            <span class="pid">{esc(ps.point_id)}</span>
            <span class="pts">· {ps.points_earned:g} pt</span>
            {conf_tag(ps.grading_confidence)}{review}
            <div class="rationale">{esc(ps.rationale)}</div>
            {evidence}
          </div>
        </div>"""

    def render_qcard(ans: TranscribedAnswer) -> str:
        qs = scorecards_by_qid.get(ans.question_id)
        if qs is None:
            return f"""
            <div class="qcard">
              <div class="qcard-head"><span class="qid">Q {esc(ans.question_id)}</span>
                <span class="qscore">not graded</span></div>
              <pre class="transcript">{esc(ans.transcript)}</pre>
            </div>"""
        points_html = "".join(render_point(ps) for ps in qs.point_scores)
        ocr_flag = (
            ' <span class="tag low">OCR ' f'{ans.confidence:.2f}</span>'
            if ans.confidence < low_confidence_threshold else ""
        )
        shared_flag = (
            ' <span class="tag review">shared transcript</span>'
            if ans.question_id in recovered_set else ""
        )
        merged_flag = (
            ' <span class="tag review">merged from sub-parts</span>'
            if ans.question_id in merged_set else ""
        )
        return f"""
        <div class="qcard">
          <div class="qcard-head">
            <span class="qid">Q {esc(qs.question_id)}{ocr_flag}{shared_flag}{merged_flag}</span>
            <span class="qscore">{qs.points_earned:g} / {qs.points_possible:g}</span>
          </div>
          <pre class="transcript">{esc(qs.transcript_used or ans.transcript)}</pre>
          <div class="points">{points_html}</div>
        </div>"""

    def render_unattempted_qcard(qs) -> str:
        points_html = "".join(render_point(ps) for ps in qs.point_scores)
        return f"""
        <div class="qcard unattempted-card">
          <div class="qcard-head">
            <span class="qid">Q {esc(qs.question_id)} <span class="tag review">unattempted</span></span>
            <span class="qscore">0 / {qs.points_possible:g}</span>
          </div>
          <div class="ua-note">No answer was transcribed — scored 0 / {qs.points_possible:g}.</div>
          <div class="points">{points_html}</div>
        </div>"""

    blocks = []
    for pi, img in enumerate(answer_images, start=1):
        data_uri = _img_to_data_uri(img)
        answers_here = page_to_answers.get(pi, [])
        if answers_here:
            cards = "".join(render_qcard(a) for a in answers_here)
        else:
            cards = '<div class="no-answer">No answers were mapped to this page.</div>'
        blocks.append(f"""
        <section class="page-block">
          <div class="page-title">Answer page {pi} of {len(answer_images)}</div>
          <div class="page-img"><img src="{data_uri}" alt="Answer page {pi}"></div>
          <div class="answers-col">{cards}</div>
        </section>""")

    flags_html = ""
    if scorecard.review_flags:
        items = "".join(f"<li>{esc(f)}</li>" for f in scorecard.review_flags)
        flags_html = f"""
        <div class="flags"><strong>⚠️ Review recommended</strong>
          <ul>{items}</ul></div>"""

    # Sub-parts that have a scorecard but no transcribed answer are the 0/max
    # "unattempted" ones; they belong to no answer page, so they get their own
    # section that explicitly names them and shows each rubric point as 0.
    # Note: `rendered_qids` is built from `augmented`, not `submission.answers`,
    # so recovered sub-parts (graded against a parent transcript) appear in the
    # per-page section above and do NOT fall into Unattempted.
    rendered_qids = {a.question_id for a in augmented}
    unattempted = [qs for qs in scorecard.questions if qs.question_id not in rendered_qids]
    unattempted_html = ""
    if unattempted:
        ids = ", ".join(esc(qs.question_id) for qs in unattempted)
        zero_pts = sum(qs.points_possible for qs in unattempted)
        cards = "".join(render_unattempted_qcard(qs) for qs in unattempted)
        unattempted_html = f"""
        <section class="unattempted-block">
          <div class="unattempted-title">Unattempted — scored 0</div>
          <p class="unattempted-note">
            No answer was transcribed for <strong>{ids}</strong>
            ({len(unattempted)} sub-part(s)), so the student earned
            <strong>0 / {zero_pts:g}</strong> on these parts — they still count
            toward the total. If OCR may have missed an answer, double-check the
            answer pages.
          </p>
          <div class="answers-col">{cards}</div>
        </section>"""

    set_str = f" · {esc(scorecard.set_label)}" if scorecard.set_label else ""
    pct = scorecard.percentage

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scorecard — {esc(scorecard.subject)} {scorecard.year}</title>
<style>{_HTML_STYLE}</style></head>
<body><div class="wrap">
  <header class="report-head">
    <div>
      <h1>{esc(scorecard.subject)} {scorecard.year}{set_str}</h1>
      <div class="meta">Generated {esc(scorecard.generated_at)} · {len(scorecard.questions)} questions graded</div>
    </div>
    <div class="score-badge">
      <div class="pct">{pct:.0f}%</div>
      <div class="frac">{scorecard.total_points_earned:g} / {scorecard.total_points_possible:g} pts</div>
      <div class="bar"><i style="width:{max(0, min(100, pct)):.1f}%"></i></div>
    </div>
  </header>
  {flags_html}
  {unattempted_html}
  {''.join(blocks)}
  <footer class="report-foot">AP FRQ Auto-Grader · per-rubric-point evidence shown beside each answer page</footer>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Batch orchestration — discover subject folders and grade each end-to-end
# ---------------------------------------------------------------------------

def _find_pdf(folder: Path, *keywords: str) -> Path | None:
    """First PDF in `folder` whose filename stem contains all keywords (case-insensitive)."""
    pdfs = sorted(folder.glob("*.pdf")) + sorted(folder.glob("*.PDF"))
    for p in pdfs:
        stem = p.stem.lower()
        if all(k in stem for k in keywords):
            return p
    return None


def discover_exam_folders(
    data_dir: Path,
    slug_to_subject: dict[str, str],
    *,
    only_slugs: list[str] | None = None,
) -> tuple[list[ExamFolder], list[str]]:
    """Find subject sub-folders under ``data_dir`` that hold a full exam.

    A folder qualifies if it contains a questions PDF, an answers PDF and a
    marking-scheme PDF. Files are matched loosely by filename keyword, so
    ``marking scheme.pdf`` and ``marking-scheme.pdf`` both work. Returns
    ``(exams, notes)``: each exam is a dict with the resolved paths, the folder
    ``slug`` and its canonical ``subject``; ``notes`` holds human-readable
    reasons folders were skipped (so nothing is dropped silently).
    """
    data_dir = Path(data_dir)
    exams: list[ExamFolder] = []
    notes: list[str] = []
    for folder in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        slug = folder.name
        if only_slugs and slug not in only_slugs:
            continue
        q = _find_pdf(folder, "question")
        a = _find_pdf(folder, "answer")
        m = _find_pdf(folder, "marking") or _find_pdf(folder, "scheme")
        missing = [n for n, p in (("questions", q), ("answers", a), ("marking-scheme", m)) if p is None]
        if len(missing) == 3:
            continue  # empty scaffold folder — nothing to grade yet
        if missing:
            notes.append(f"{slug}: skipped — missing {', '.join(missing)} PDF(s)")
            continue
        subject = slug_to_subject.get(slug)
        if subject is None:
            notes.append(f"{slug}: skipped — folder name is not a known subject slug (see config.py)")
            continue
        exams.append({
            "slug": slug,
            "subject": subject,
            "folder": folder,
            "questions_pdf": q,
            "answers_pdf": a,
            "marking_scheme_pdf": m,
        })
    return exams, notes


def assemble_scorecard(
    *,
    subject: str,
    year: int,
    set_label: str | None,
    question_scorecards: list[QuestionScorecard],
    missing_qids: list[str],
    recovered_qids: list[str] | None = None,
    merged_qids: list[str] | None = None,
    config_echo: dict | None = None,
) -> Scorecard:
    """Total per-question scorecards into a Scorecard, with review flags.

    Flags cover unattempted (0/max) sub-parts, sub-parts whose transcript was
    recovered from a parent-level OCR block (unlabeled multi-part response),
    parent questions whose answer was assembled by merging per-sub-part OCR
    blocks, questions whose OCR confidence fell below threshold, and any
    point graded with low confidence.
    """
    total_earned = sum(qs.points_earned for qs in question_scorecards)
    total_possible = sum(qs.points_possible for qs in question_scorecards)
    percentage = (total_earned / total_possible * 100.0) if total_possible else 0.0

    missing_set = set(missing_qids)
    recovered_set = set(recovered_qids or [])
    merged_set = set(merged_qids or [])
    review_flags: list[str] = []
    for qs in question_scorecards:
        if qs.question_id in missing_set:
            review_flags.append(
                f"Q{qs.question_id}: no answer transcribed — scored 0/max; verify it was truly left blank"
            )
            continue
        if qs.question_id in recovered_set:
            review_flags.append(
                f"Q{qs.question_id}: student wrote an unlabeled response covering this and "
                "sibling sub-parts; transcript reused from the parent question — verify the "
                "grader attributed evidence to the right sub-part"
            )
            # Don't double-flag with the generic OCR / low-confidence messages.
            continue
        if qs.question_id in merged_set:
            review_flags.append(
                f"Q{qs.question_id}: rubric expected a single answer here but OCR returned "
                "separate sub-part blocks; transcripts were concatenated — verify the grader "
                "attributed each rubric point to the right sub-part of the merged transcript"
            )
            continue
        if any(ps.review_recommended for ps in qs.point_scores):
            review_flags.append(f"Q{qs.question_id}: OCR confidence below threshold — verify transcript")
        if any(ps.grading_confidence == "low" for ps in qs.point_scores):
            review_flags.append(f"Q{qs.question_id}: one or more rubric points scored with low confidence")

    return Scorecard(
        subject=subject,
        year=year,
        set_label=set_label,
        total_points_earned=total_earned,
        total_points_possible=total_possible,
        percentage=percentage,
        questions=question_scorecards,
        review_flags=review_flags,
        generated_at=datetime.now(UTC).isoformat(),
        config_echo=config_echo or {},
    )


def grade_submission(
    client: genai.Client,
    *,
    subject: str,
    year: int,
    set_label: str | None,
    submission: ParsedSubmission,
    rubric: ParsedRubric,
    grade_prompt_path: Path,
    subject_addendum: str = "",
    model_grading: str = "gemini-3.5-flash",
    grading_max_workers: int = 8,
    low_confidence_threshold: float = 0.75,
    questions: Literal["all"] | list[str] = _ALL_QUESTIONS,
    config_echo: dict | None = None,
    force_review_qids: set[str] | None = None,
    on_response: Callable[..., None] | None = None,
) -> GradeSubmissionResult:
    """Grade an already-built submission against an already-parsed rubric.

    The post-OCR half of :func:`grade_exam`, split out so a server can build the
    ``submission`` itself — from handwriting OCR (:func:`ocr_submission`) or from
    typed answers (:func:`label_typed_answers`) — and supply a rubric it cached
    in its own store. ``force_review_qids`` flags extra sub-parts for human
    review on top of the recovered/merged ones (e.g. AI-labelled typed
    sub-parts, whose split is model-generated). Raises if the rubric/answer
    sub-part overlap is empty.
    """
    rubric_by_qid = flatten_rubric_by_subpart(rubric)
    answer_by_qid = {a.question_id: a for a in submission.answers}

    universe = set(rubric_by_qid) if questions == "all" else set(questions) & set(rubric_by_qid)
    if not universe:
        raise RuntimeError(
            f"No questions to grade for {subject!r}. "
            f"Rubric sub-parts: {sorted(rubric_by_qid)}; "
            f"answer sub-parts: {sorted(answer_by_qid)}; requested: {questions}."
        )

    # First pass: which rubric sub-parts have an explicit answer?
    initial_missing = sorted(universe - set(answer_by_qid))

    # Rescue parent-level blocks (student wrote one continuous answer to Q4
    # without labeling 4a/4b/4c/4d) by copying the parent transcript into each
    # missing sub-part. Sub-parts genuinely not addressed remain in
    # ``missing_qids`` and are scored 0/max as before.
    answer_by_qid, _still_missing, recovered_qids = (
        _synthesize_subpart_answers_from_parents(answer_by_qid, initial_missing)
    )
    if recovered_qids:
        print(
            f"  Recovered {len(recovered_qids)} sub-part(s) from a parent-level "
            f"block: {', '.join(recovered_qids)}"
        )

    # Inverse rescue: fold orphan sub-part blocks into the rubric parent they
    # belong to. Handles both flavours: rubric has ``"3a"`` but answers carry
    # only ``"3a-i"`` / ``"3a-ii"`` / ``"3a-iii"`` (no parent), AND rubric has
    # ``"1e"`` while answers carry both ``"1e"`` and orphan ``"1e-ii"`` (parent
    # + extra children) — without this, evidence in the children is never seen
    # by the parent's grading call.
    answer_by_qid, merged_parent_answers = (
        _synthesize_parent_answers_from_subparts(answer_by_qid, set(rubric_by_qid))
    )
    merged_qids = sorted(merged_parent_answers)
    if merged_qids:
        print(
            f"  Merged {len(merged_qids)} parent answer(s) from per-sub-part "
            f"blocks: {', '.join(merged_qids)}"
        )

    qids_to_grade = sorted(universe & set(answer_by_qid))
    missing_qids = sorted(universe - set(answer_by_qid))

    question_scorecards = grade_questions_parallel(
        client, qids_to_grade, rubric_by_qid, answer_by_qid,
        subject=subject, prompt_path=grade_prompt_path,
        subject_addendum=subject_addendum, model=model_grading,
        low_confidence_threshold=low_confidence_threshold,
        max_workers=grading_max_workers,
        force_review_qids=(
            set(recovered_qids) | set(merged_qids) | (force_review_qids or set())
        ),
        on_response=on_response,
    )
    question_scorecards += build_unattempted_scorecards(rubric_by_qid, missing_qids)
    question_scorecards.sort(key=lambda qs: qs.question_id)

    scorecard = assemble_scorecard(
        subject=subject, year=year, set_label=set_label,
        question_scorecards=question_scorecards, missing_qids=missing_qids,
        recovered_qids=recovered_qids,
        merged_qids=merged_qids,
        config_echo=config_echo,
    )
    return {
        "scorecard": scorecard,
        "submission": submission,
        "rubric": rubric,
        "qids_to_grade": qids_to_grade,
        "missing_qids": missing_qids,
        "recovered_qids": recovered_qids,
        "merged_parent_answers": merged_parent_answers,
    }


def grade_exam(
    client: genai.Client,
    *,
    subject: str,
    year: int,
    set_label: str | None,
    questions_pdf: Path,
    answers_pdf: Path,
    marking_scheme_pdf: Path,
    ocr_prompt_path: Path,
    rubric_prompt_path: Path,
    grade_prompt_path: Path,
    subject_addendum: str = "",
    ocr_subject_addendum: str = "",
    model_ocr: str = "gemini-3.1-pro-preview",
    model_rubric: str = "gemini-3.5-flash",
    model_grading: str = "gemini-3.5-flash",
    ocr_dpi: int = 300,
    rubric_dpi: int = 200,
    ocr_thinking_level: str | None = "low",
    grading_max_workers: int = 8,
    low_confidence_threshold: float = 0.75,
    questions: Literal["all"] | list[str] = _ALL_QUESTIONS,
    config_echo: dict | None = None,
) -> GradeExamResult:
    """Run the full OCR -> rubric -> grade -> assemble pipeline for one exam.

    Returns a dict with: ``scorecard``, ``submission``, ``answer_images``,
    ``rubric``, ``qids_to_grade`` and ``missing_qids``. Pass ``submission`` and
    ``answer_images`` straight to :func:`render_html_report` to build the HTML.
    Unattempted sub-parts (in the rubric but not transcribed) are scored 0/max.
    """
    question_images = render_pdf_to_images(questions_pdf, dpi=ocr_dpi)
    answer_images = render_pdf_to_images(answers_pdf, dpi=ocr_dpi)

    submission = ocr_submission(
        client, question_images, answer_images, ocr_prompt_path,
        model=model_ocr, thinking_level=ocr_thinking_level,
        subject_addendum=ocr_subject_addendum,
    )

    rubric = load_rubric(
        client, marking_scheme_pdf,
        subject=subject, year=year, set_label=set_label,
        prompt_path=rubric_prompt_path, model=model_rubric, dpi=rubric_dpi,
    )

    result = grade_submission(
        client,
        subject=subject, year=year, set_label=set_label,
        submission=submission, rubric=rubric,
        grade_prompt_path=grade_prompt_path,
        subject_addendum=subject_addendum,
        model_grading=model_grading,
        grading_max_workers=grading_max_workers,
        low_confidence_threshold=low_confidence_threshold,
        questions=questions,
        config_echo=config_echo,
    )
    return {
        "scorecard": result["scorecard"],
        "submission": result["submission"],
        "answer_images": answer_images,
        "rubric": result["rubric"],
        "qids_to_grade": result["qids_to_grade"],
        "missing_qids": result["missing_qids"],
        "recovered_qids": result["recovered_qids"],
        "merged_parent_answers": result["merged_parent_answers"],
    }


# ---------------------------------------------------------------------------
# Character error rate (kept for optional manual validation)
# ---------------------------------------------------------------------------

def character_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance / len(reference). 0.0 = perfect; 1.0 = totally wrong."""
    ref = reference.strip()
    hyp = hypothesis.strip()
    if not ref:
        return 0.0 if not hyp else 1.0

    m, n = len(ref), len(hyp)
    if n == 0:
        return 1.0

    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        ref_ch = ref[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ref_ch == hyp[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,         # deletion
                curr[j - 1] + 1,     # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[n] / m
