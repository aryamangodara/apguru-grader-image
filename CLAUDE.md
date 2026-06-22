# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

APGuru Grader — an async **FastAPI** backend that auto-grades **AP® Free-Response Questions (FRQ)**. It registers an exam (parsing the marking-scheme PDF into a structured rubric once, then caching it), accepts a student's answers (as a PDF for handwritten exams, or inline JSON for typed exams), grades them against the rubric, and returns a UI-complete scorecard.

**Google Gemini** is the LLM, called either through **Vertex AI** (default) or the AI Studio API key. **MySQL** is the datastore. **Langfuse** is optional LLM tracing.

> This repo was reduced from a larger analytics dashboard to **only** the grader feature. If you find a reference to chat/quiz/weekly-plan/error-analysis/etc., it is stale — remove it.

## 1. Project Philosophy

This project follows an **AI-first development** approach: structure code so both humans and AI tools can understand, modify, and verify it with minimal context.

- **Type everything.** Every function has parameter types and a return type. Pydantic fields that face external consumers get a description.
- **Enums / constant dicts, not magic strings** for statuses and modes.
- **Fail fast.** Required config uses `Field(...)` so the app crashes on startup if a value is missing, not mid-request.
- **Name things verbosely.** `marking_scheme_pdf_url`, not `ms_url`.
- **Keep modules self-contained.** The grader pipeline (`app/services/grader/`) is a vendored, dependency-light package understandable on its own.

## Commands

```bash
# Install. Use Python 3.11–3.13 to RUN grading: PyMuPDF (PDF→image render) has no
# wheel for 3.14 yet. The app itself imports & tests pass on 3.14 because the
# `import fitz` is deferred into render_pdf_to_images (see tests/unit/test_grader_lazy_import.py).
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt                # pulls in requirements.txt

# Dev server (auto-reload)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# Production server
gunicorn app.main:app -c gunicorn.conf.py

# Tests — pytest, asyncio_mode=auto (no @pytest.mark.asyncio needed)
pytest tests/
pytest tests/integration/test_grader_api.py        # grader HTTP contract
pytest tests/unit/ -k url_guard                     # single test by name

# Lint / autofix (ruff, line-length 120)
ruff check .
ruff check . --fix

# Database migrations are NOT in this repo — they live in the central repo
# apguru-centralized-alembic (its CI/CD applies `alembic upgrade head` to prod on
# merge to its main). The grader app does not migrate on boot.

# Docker (single app container, bound to 127.0.0.1:8081 — front it with the host
# reverse proxy). Always pass -p apguru-grader so redeploys reuse the same named
# containers instead of spawning orphans. Publishing a GitHub Release auto-deploys
# via .github/workflows/deploy.yml — merges to main do NOT deploy (runbook:
# docs/grader-ec2-deployment.md).
docker compose -p apguru-grader up -d --build
docker compose -p apguru-grader down
```

The Docker image entrypoint runs `gunicorn app.main:app -c gunicorn.conf.py` — it does **not** run migrations (schema is owned by the central `apguru-centralized-alembic` pipeline). Manual end-to-end smoke test (hits a **running** server + a live LLM, not collected by pytest):

```bash
python scripts/tests/grader/test_grader_handwritten_e2e.py            # AP Biology
python scripts/tests/grader/test_grader_handwritten_e2e.py psychology # any seeded slug
```

**Grader tuning lives in code/config, not feature env.** Operational knobs (models, DPI, concurrency, reaper window, confidence threshold) are `grader_*` settings in `app/core/config.py`. Per-subject grading/OCR guidance lives in the `course_configs` DB table (`grading_addendum` / `ocr_addendum`), resolved at grade time — not in env.

## Git & PR workflow

- **Never merge to `main` yourself — the maintainer reviews and merges every PR personally.** When work is ready, create a feature branch and open a PR (`gh pr create` or the `create-pr` skill), then **stop**. Do NOT run `gh pr merge`, do NOT push to `main`, and do NOT use `--admin` to bypass protection.
- `main` is **branch-protected** (PR-only; direct pushes are rejected) and deploys are **release-gated** — merging a PR does *not* deploy; the maintainer publishes a GitHub Release to ship (see Commands / [`docs/grader-ec2-deployment.md`](docs/grader-ec2-deployment.md)).
- Branch from the latest `main`, keep PRs focused, and leave the merge decision to the maintainer's review.

## Architecture

**Layered request flow — each layer calls only the one below; never skip a layer:**

1. **Router** (`app/api/v1/*_router.py`) — route definitions; `response_model=` on every route. No business logic, no SQL.
2. **Controller** (`app/controllers/`) — orchestration; maps domain errors → HTTP status codes. No SQL.
3. **Service** (`app/services/`) — business logic and LLM calls.
4. **Persistence** — DB access via raw parameterized SQL through the `Database` singleton.

Routers are registered in `app/api/router.py` (everything under `/api/v1`). Only two routers are mounted: `health_router` and `grader_router`.

**Endpoints.** All grader endpoints are **PUBLIC (no JWT)** by design — they fetch caller-supplied PDF URLs and return scorecards, so they **must** be restricted at the edge (ALB / Nginx / WAF / security group). The PDF fetch is SSRF-guarded in `app/services/grader/url_guard.py` as the in-app backstop.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/grader/register-exam` | Register an exam; parse + cache its rubric (idempotent). → `201` |
| `GET`  | `/api/v1/grader/exams` | List registered exams (newest first); optional `?course_id=`. → `200` |
| `POST` | `/api/v1/grader/exams/{test_id}/submissions` | Enqueue grading for one student; returns `job_id`. → `202` |
| `GET`  | `/api/v1/grader/jobs` | List jobs by `?student_id=` and/or `?test_id=` (≥1 required); lightweight summaries, no scorecard. → `200` |
| `GET`  | `/api/v1/grader/jobs/{job_id}` | Poll job status; scorecard present once `status == "succeeded"`. → `200` |
| `GET`  | `/api/v1/health`, `/api/v1/health/ping` | Liveness (used by the Docker healthcheck). |

**Two grading modes.**
- **Handwritten:** answers PDF → rendered to images (`render_pdf_to_images`, PyMuPDF) → OCR'd with Gemini using the questions PDF as visual context (`ocr_submission`) → graded.
- **Typed:** answers supplied inline as JSON → LLM-labelled by subpart against the rubric structure (`label_typed_answers`) → graded.

Both then run `grade_submission` (Gemini against the rubric, with the per-course grading addendum), post-process (recover missing questions, flag low-confidence items), and persist a `GradedScorecardResponse`. After grading, an optional best-effort step (`grader_enable_summaries`, on by default) generates three role-tailored summaries — for the student, teacher, and parent — via `app/services/grader_summaries.py` and attaches them to the scorecard as `student_summary` / `teacher_summary` / `parent_summary` (one extra Gemini call, traced as `grader.summaries`).

**Async job lifecycle (`app/services/grader_job_service.py`).** `create_job` inserts a `queued` row; grading runs in a FastAPI `BackgroundTask` capped by a module-level `asyncio.Semaphore` (`grader_max_concurrent_jobs`); clients poll `/grader/jobs/{job_id}`. A startup reaper (`reap_stale_jobs`, called from the `lifespan` in `app/main.py`) fails any job left `running` by a previous restart, since in-process BackgroundTasks don't survive a process exit.

**Exam registry & rubric cache (`app/services/grader_exam_service.py`).** `register_exam` parses the marking-scheme PDF once (`parse_rubric_pdf`) and stores the `ParsedRubric` JSON in `ap_exam.rubric_json`; subsequent students reuse it.

**Grader pipeline package (`app/services/grader/`).** Self-contained, imports almost nothing from the rest of `app`:
- `core.py` — `get_gemini_client`, `render_pdf_to_images`, `ocr_submission`, `parse_rubric_pdf`, `grade_submission`, `label_typed_answers`. **Ships its own Gemini client** (it does NOT use a shared LLM abstraction).
- `schemas.py` — `ParsedRubric`, `ParsedSubmission`, `QuestionRubric`, `RubricPoint`, `Scorecard`, `TranscribedAnswer`.
- `fetch.py` + `url_guard.py` — SSRF-guarded PDF fetch to a tempfile.
- `response_builder.py` — `build_scorecard_response` composes the UI-complete response.
- `tracing.py` — emits Langfuse generation spans for the grader's Gemini calls.
- `prompts/*.txt` — shared `ocr.txt` / `segment_typed.txt`, plus a per-exam-body rubric+grade prompt set: AP (`rubric_extract.txt`, `grade_question.txt`), IB (`*_ib.txt`), Cambridge IGCSE/A-Level (`*_cambridge.txt`). `app/services/grader_prompts.py` picks the set by the course's `exam_body` (`College Board` → AP, `IBO` → IB, `Cambridge IGCSE/A-Level` → Cambridge; anything unknown → AP). Plus a board-agnostic `audience_summaries.txt` used by `app/services/grader_summaries.py` for the post-grade student/teacher/parent summaries.

**LLM.** The grader calls Gemini directly via `app/services/grader/core.py::get_gemini_client`. When `grader_use_vertex=true` **and** a Vertex service account is configured (`GOOGLE_APPLICATION_CREDENTIALS` + `GOOGLE_CLOUD_PROJECT`), calls route through Vertex AI's global endpoint — necessary because the handwriting-OCR call routinely runs ~150s, exceeding AI Studio's server-side deadline (504). Otherwise it falls back to `GEMINI_API_KEY`. Default models are the `grader_*_model` settings in `app/core/config.py`.

**Cross-cutting (`app/core/`).** `config.py` (pydantic-settings; required fields use `Field(...)` to fail fast), `database.py` (the `Database` singleton), `course_config.py` (per-course config incl. `get_grading_addendum` / `get_ocr_addendum`), `observability.py` + `logging.py` (structlog + optional Langfuse). `app/middleware/request_logging.py` stamps every log line with a `request_id`. Startup/shutdown (Langfuse init, DB connect, reaper) run in the `lifespan` block in `app/main.py`.

## Database

- MySQL via async SQLAlchemy (`aiomysql`). **All access goes through the `Database` singleton** (`app/core/database.py`) — never construct engines/sessions directly.
- Methods: `query()`, `query_one()`, `write()`, `write_returning_id()` (MySQL `lastrowid`), `write_many()`, and `async with db.transaction() as session:`.
- **Named SQL parameters only** (`:param`) — never f-strings or `%s`.
- `settings.use_local_db` toggles all traffic between local MySQL and the configured cloud host.
- **Grader tables:** `ap_exam` (registered exams + cached rubric JSON) and `grading_job` (per-submission job: status ∈ {queued, running, succeeded, failed}, plus the `scorecard_json`); per-course `grading_addendum` / `ocr_addendum` columns live in `course_configs`.
- **Migrations are NOT in this repo.** The schema (the grader tables and the `course_configs` seeds) is owned by the central [`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic) repo, whose CI/CD runs `alembic upgrade head` against the shared prod DB on merge to its `main`. The grader app does **not** migrate on boot. To add/alter a table or seed courses, open a migration PR there (e.g. the Cambridge IGCSE/A-Level courses are seeded by central migration `031`).

## Conventions

- **Async-first**: route handlers, DB ops, and LLM calls are `async def`; sync only for pure, I/O-free computation. Parallelize independent I/O with `asyncio.gather`.
- **Type everything**, Python 3.11+ syntax (`int | None`). Absolute imports (`from app.core.config import settings`). Every `.py` opens with a module docstring.
- **Pydantic `BaseModel`** for API schemas (descriptions on externally-facing fields); `dataclass` for internal service state.
- **Structured logs** at success/failure milestones via structlog (event name + kwargs), not f-string messages.
- Config that is infrastructure/secrets goes in `app/core/config.py` + `.env.example`. Per-subject grading behaviour goes in the `course_configs` table, not env.

## File placement

| What | Where | Register / wire in |
|---|---|---|
| REST endpoint | `app/api/v1/{feature}_router.py` | `app/api/router.py` |
| Controller | `app/controllers/{feature}_controller.py` | imported by router |
| Service | `app/services/{feature}_service.py` | called by controller |
| Persistence | through the `Database` singleton (raw parameterized SQL) | called by service |
| Schemas | `app/schemas/{feature}_schema.py` | router + controller |
| Grader pipeline primitive | `app/services/grader/` (`core.py` / `schemas.py` / …) | re-exported via `app/services/grader/__init__.py` |
| Grader prompt | `app/services/grader/prompts/*.txt` | loaded by the grader package |
| DB migration / course seed | the central `apguru-centralized-alembic` repo (PR there) | applied by its CI/CD on merge |
| Config setting | `app/core/config.py` | also add to `.env.example` |

## Testing

- `tests/unit/` — pure logic (grader URL-guard, typed-answer labelling, lazy `fitz` import). `tests/integration/` — HTTP endpoints with the DB mocked via the `client` fixture in `tests/conftest.py`.
- pytest with `asyncio_mode = "auto"` — do not add `@pytest.mark.asyncio`.
- `scripts/tests/grader/test_grader_handwritten_e2e.py` is a **manual** smoke test against real infra — not collected by pytest.

## Production notes

CORS is currently wildcard (`allow_origins=["*"]`) — restrict before production. The grader endpoints are public; enforce access control at the edge. Settings without defaults (`DB_HOST` / `DB_USER` / `DB_PASSWORD`) crash startup if missing — intentional fail-fast behaviour. See `docs/grader-ec2-deployment.md` for deployment.
