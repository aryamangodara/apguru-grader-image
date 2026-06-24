# APGuru Grader

An async **FastAPI** backend that auto-grades **AP® Free-Response Questions (FRQ)**. You register an exam once (its marking-scheme PDF is parsed into a structured rubric and cached), submit a student's answers — as a PDF for handwritten exams or inline JSON for typed exams — and the service grades them against the rubric and returns a point-by-point scorecard.

- **LLM:** Google **Gemini**, called through **Vertex AI** (default) or the AI Studio API key.
- **Data:** MySQL (async, via SQLAlchemy + aiomysql).
- **PDF:** PyMuPDF renders answer pages to images for OCR; Pillow handles the images.
- **Observability:** structlog structured logs + optional Langfuse LLM tracing.

> The grader endpoints are **public (no auth)** by design — they must be restricted at the edge (ALB / Nginx / WAF / security group). Caller-supplied PDF URLs are SSRF-guarded in `app/services/grader/url_guard.py`.

## How it works

```
register-exam ──► (parse + cache rubric)
                      │
submissions ──► enqueue grading job ──► background worker
                      │                      │
                      ▼                      ▼
                  job_id            handwritten: PDF → render → OCR (Gemini)
                                    typed:       inline JSON → label by subpart
                                              │
                                              ▼
                                    grade against rubric (Gemini) → scorecard
jobs/{job_id} ──► poll until status == "succeeded" ──► read scorecard
```

Grading runs asynchronously in a background task (capped by a concurrency semaphore); clients poll the job until it succeeds or fails. A startup reaper fails any job left `running` by a previous restart.

The scorecard is point-by-point (per question and per rubric point) and — when `GRADER_ENABLE_SUMMARIES` is on (the default) — also carries three short, role-tailored summaries: `student_summary`, `teacher_summary`, and `parent_summary`.

## Quickstart

Use **Python 3.11–3.13** to run grading (PyMuPDF has no 3.14 wheel yet) and a reachable **MySQL** instance.

```bash
# 1. Install (requirements-dev.txt pulls in requirements.txt)
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt

# 2. Configure — copy the template and fill in DB + Gemini/Vertex credentials
cp .env.example .env

# 3. Run the dev server (auto-reload)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

- Health check: `GET http://localhost:8080/api/v1/health`
- Interactive API docs: `http://localhost:8080/docs`

> **Database schema/migrations are not managed in this repo.** They live in [`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic) — point that repo at your DB and run `alembic upgrade head` there to create `ap_exam` / `grading_job` / `course_configs`.

### Docker

Runs as a single container bound to **`127.0.0.1:8081`** (front it with your host's reverse proxy — the routes are public + handle PII, so they must not be exposed directly):

```bash
docker compose -p apguru-grader up -d --build
docker compose -p apguru-grader down
```

The image entrypoint runs `gunicorn app.main:app -c gunicorn.conf.py`. It does **not** run migrations — schema changes are applied by the central [`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic) pipeline.

### Deployment & CI/CD

Designed to run as its own container on a shared EC2 host. Deploys are **release-gated**: publishing a **GitHub Release** (a tag like `v1.2.0`) — *not* merging to `main` — triggers a **GitHub Actions self-hosted runner on the box** ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)) to deploy that release's exact commit. No SSH key or stored secrets: it checks out the released code (built-in `GITHUB_TOKEN`), rsyncs it into the deploy dir (preserving host-only `.env` / `vertex-key.json`), runs `docker compose -p apguru-grader up -d --build`, and health-checks. Ship with `gh release create vX.Y.Z --generate-notes`, or use the workflow's manual *Run workflow* button to redeploy without a release. See [`docs/grader-ec2-deployment.md`](docs/grader-ec2-deployment.md) for the full runbook (runner install, the host reverse-proxy vhost in [`nginx/nginx.conf`](nginx/nginx.conf), Vertex credentials, and edge access control).

### Production

```bash
gunicorn app.main:app -c gunicorn.conf.py
```

## API

All routes are under `/api/v1`. Identifiers are keyed on `test_id` everywhere.

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/grader/register-exam` | Register an exam; parse + cache its rubric (idempotent). | `201` |
| `GET`  | `/grader/exams` | List registered exams (newest first); optional `?course_id=`. | `200` |
| `POST` | `/grader/exams/{test_id}/submissions` | Enqueue grading for one student; returns a `job_id`. | `202` |
| `GET`  | `/grader/jobs` | List jobs by `?student_id=` and/or `?test_id=` (≥1 required); lightweight summaries, no scorecard. | `200` |
| `GET`  | `/grader/jobs/{job_id}` | Poll job status; the scorecard is present once `status == "succeeded"`. | `200` |
| `GET`  | `/health`, `/health/ping` | Liveness (used by the Docker healthcheck). | `200` |

**Register an exam** (handwritten needs `questions_pdf_url`; typed does not):

```bash
curl -X POST localhost:8080/api/v1/grader/register-exam -H 'Content-Type: application/json' -d '{
  "test_id": 555,
  "course_id": "14",
  "test_name": "March 2024",
  "is_handwritten": true,
  "marking_scheme_pdf_url": "https://example.com/marking-scheme.pdf",
  "questions_pdf_url": "https://example.com/questions.pdf"
}'
```

**Submit answers** — handwritten (`answers_pdf_url`) or typed (`answers` map):

```bash
# typed
curl -X POST localhost:8080/api/v1/grader/exams/555/submissions -H 'Content-Type: application/json' -d '{
  "student_id": 7,
  "answers": {"1": "first answer", "2": "second answer"}
}'
# → 202 {"job_id": "…", "status": "queued", ...}
```

**Poll the job** until `status` is `succeeded` (or `failed`):

```bash
curl localhost:8080/api/v1/grader/jobs/<job_id>
# → 200 {"job_id": "…", "test_id": 555, "student_id": 7, "status": "succeeded", "scorecard": { … }}
```

### Errors

Every failure returns one envelope — a stable, machine-readable `error_code` plus a human-readable `detail` — so clients can branch on the code instead of parsing messages:

```json
{ "error_code": "RUBRIC_NOT_GENERATED", "detail": "test_id 555 is registered but its rubric is not generated yet" }
```

| HTTP | `error_code` | When |
|---|---|---|
| 400 | `INVALID_TEST_ID` | `test_id` isn't a live test in the main app's `tests` table. |
| 400 | `INVALID_SUBMISSION` | Missing `answers_pdf_url` (handwritten) or `answers` (typed). |
| 400 | `INVALID_PDF_URL` | A supplied PDF URL failed the SSRF / URL guard. |
| 400 | `UNKNOWN_COURSE` | `course_id` has no `course_configs` row. |
| 400 | `MISSING_JOB_FILTER` | `GET /grader/jobs` with neither `student_id` nor `test_id`. |
| 404 | `TEST_NOT_REGISTERED` | Submitting to a `test_id` that was never registered. |
| 404 | `JOB_NOT_FOUND` | Polling an unknown `job_id`. |
| 409 | `RUBRIC_NOT_GENERATED` | The exam is registered but its rubric hasn't been parsed yet. |
| 422 | `VALIDATION_ERROR` | Request-body validation failed (`detail` is FastAPI's field-error list). |
| 500 | `INTERNAL_ERROR` | Unexpected server error (no internals leaked). |

A bare framework 404 / 405 (unknown path, wrong method) uses the same envelope with `NOT_FOUND` / `METHOD_NOT_ALLOWED`. `detail` is unchanged from before — the envelope only **adds** `error_code`. The full list lives in [`app/core/errors.py`](app/core/errors.py) and is documented in the OpenAPI schema at `/docs`.

## Configuration

All settings load from `.env` via pydantic-settings (`app/core/config.py`). Settings without defaults fail fast at startup if missing. Start from [`.env.example`](.env.example).

| Key | Purpose |
|---|---|
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | MySQL connection (**required**). |
| `USE_LOCAL_DB` | When `true`, routes all DB traffic to `LOCAL_DB_*` instead of the cloud host. |
| `GEMINI_API_KEY` | Gemini (AI Studio) key; the grader's client also reads it from the environment. |
| `GRADER_USE_VERTEX` | Route Gemini calls through Vertex AI (needed for the long ~150s OCR call). Honoured only when a Vertex service account is configured; otherwise falls back to `GEMINI_API_KEY`. |
| `GRADER_ENABLE_SUMMARIES` | Generate the post-grade student/teacher/parent summaries (default `true`; set `false` to skip that extra Gemini call). |
| `GOOGLE_APPLICATION_CREDENTIALS` / `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | Vertex AI service account + project. |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional LLM tracing (both blank = disabled). |

Operational grader knobs (models, OCR DPI, concurrency, reaper window, low-confidence threshold) are the `GRADER_*` settings. **Per-subject** grading/OCR guidance lives in the `course_configs` DB table (`grading_addendum` / `ocr_addendum`), resolved at grade time — not in env.

## Testing

```bash
pytest tests/                                   # full suite (unit + integration)
pytest tests/integration/test_grader_api.py     # grader HTTP contract
```

`tests/` is the automated pytest suite (`asyncio_mode=auto`; the DB is mocked). `scripts/smoke_test_api.py` hits **every** endpoint against a running server (read-only, no LLM, no seeded data) — it's the gate the deploy runs on the freshly-built container and the release runbook runs before publishing; run it locally with `python scripts/smoke_test_api.py` (add `--deep` for one real graded run). `scripts/tests/grader/test_grader_handwritten_e2e.py` is a **manual** end-to-end test that drives a running server through register → submit → poll with a live LLM — run it directly with `python`, not pytest.

## Architecture

Strict layer boundaries, async-first:

**Router** (`app/api/v1/`) → **Controller** (`app/controllers/`) → **Service** (`app/services/`) → **Persistence** (the `Database` singleton, raw parameterized SQL). See [`CLAUDE.md`](CLAUDE.md) for the full orientation.

```
app/
  api/v1/        grader_router, health_router (registered in app/api/router.py)
  controllers/   grader_controller, health_controller
  services/
    grader/      vendored grading pipeline: core (Gemini client, OCR, rubric parse,
                 grade, typed-label), schemas, fetch + url_guard, response_builder,
                 tracing, prompts/*.txt
    grader_exam_service.py   exam registry + cached rubric
    grader_job_service.py    async job lifecycle + startup reaper
    grader_prompts.py        exam-body → prompt-set selection (AP / IB / Cambridge)
    grader_summaries.py      post-grade student/teacher/parent summaries
    health_service.py
  schemas/       grader_schema, health_schema, llm_schema
  core/          config, database singleton, course_config, errors (typed
                 GraderError + {error_code, detail} handlers), observability, logging
  middleware/    request-logging middleware (request_id correlation)
nginx/           reverse-proxy config for the Docker stack
docs/            grader-ec2-deployment.md
```
