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

## Quickstart

Use **Python 3.11–3.13** to run grading (PyMuPDF has no 3.14 wheel yet) and a reachable **MySQL** instance.

```bash
# 1. Install (requirements-dev.txt pulls in requirements.txt)
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt

# 2. Configure — copy the template and fill in DB + Gemini/Vertex credentials
cp .env.example .env

# 3. Migrate the database (creates ap_exam + grading_job, etc.)
alembic upgrade head

# 4. Run the dev server (auto-reload)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

- Health check: `GET http://localhost:8080/api/v1/health`
- Interactive API docs: `http://localhost:8080/docs`

### Docker

Runs as a single container bound to **`127.0.0.1:8081`** (front it with your host's reverse proxy — the routes are public + handle PII, so they must not be exposed directly):

```bash
docker compose -p apguru-grader up -d --build
docker compose -p apguru-grader down
```

The image entrypoint runs `alembic upgrade head && gunicorn app.main:app -c gunicorn.conf.py`, so a redeploy migrates the DB automatically.

### Deployment & CI/CD

Designed to run as its own container on a shared EC2 host. A push to `main` auto-deploys via a **GitHub Actions self-hosted runner on the box** ([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)) — no SSH key or stored secrets: it checks out the code (built-in `GITHUB_TOKEN`), rsyncs it into the deploy dir (preserving host-only `.env` / `vertex-key.json`), runs `docker compose -p apguru-grader up -d --build`, and health-checks. See [`docs/grader-ec2-deployment.md`](docs/grader-ec2-deployment.md) for the full runbook (runner install, the host reverse-proxy vhost in [`nginx/nginx.conf`](nginx/nginx.conf), Vertex credentials, and edge access control).

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

## Configuration

All settings load from `.env` via pydantic-settings (`app/core/config.py`). Settings without defaults fail fast at startup if missing. Start from [`.env.example`](.env.example).

| Key | Purpose |
|---|---|
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | MySQL connection (**required**). |
| `USE_LOCAL_DB` | When `true`, routes all DB traffic to `LOCAL_DB_*` instead of the cloud host. |
| `GEMINI_API_KEY` | Gemini (AI Studio) key; the grader's client also reads it from the environment. |
| `GRADER_USE_VERTEX` | Route Gemini calls through Vertex AI (needed for the long ~150s OCR call). Honoured only when a Vertex service account is configured; otherwise falls back to `GEMINI_API_KEY`. |
| `GOOGLE_APPLICATION_CREDENTIALS` / `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | Vertex AI service account + project. |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Optional LLM tracing (both blank = disabled). |

Operational grader knobs (models, OCR DPI, concurrency, reaper window, low-confidence threshold) are the `GRADER_*` settings. **Per-subject** grading/OCR guidance lives in the `course_configs` DB table (`grading_addendum` / `ocr_addendum`), resolved at grade time — not in env.

## Testing

```bash
pytest tests/                                   # full suite (unit + integration)
pytest tests/integration/test_grader_api.py     # grader HTTP contract
```

`tests/` is the automated pytest suite (`asyncio_mode=auto`). `scripts/tests/grader/test_grader_handwritten_e2e.py` is a **manual** end-to-end smoke test that drives a running server through all four endpoints with a live LLM — run it directly with `python`, not pytest.

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
    health_service.py
  schemas/       grader_schema, health_schema, llm_schema
  core/          config, database singleton, course_config, observability, logging
  middleware/    request-logging middleware (request_id correlation)
alembic/         database migrations (cumulative chain 001_ … 026_; run `alembic upgrade head`)
nginx/           reverse-proxy config for the Docker stack
docs/            grader-ec2-deployment.md
```
