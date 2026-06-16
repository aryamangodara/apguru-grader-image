# Shipping the AP FRQ Grader to EC2

Runbook for deploying the grader to the production EC2 host. The grader is **new
to production**, so this covers code, DB migrations, the Vertex credential, and
the decision to run the grader endpoints publicly (no JWT — see §5).

## What ships

| Layer | Item |
|---|---|
| Code | Grader feature (router/controllers/services — already on `main`) + the **Vertex routing fix** in this PR (`grader_use_vertex` / `prefer_vertex`). |
| DB | Alembic migrations **020** (grader tables), **021** (AP catalog 14–36 + addenda columns), **022** (4 extra courses: Psychology 17, Macroeconomics 21, Calculus AB 22, Calculus BC 31), **026** (re-key the grader off `test_id`: drops `exam_key`/`year`/`set_label`/`question_map`, adds `test_id`/`test_name`/`answers_json`; **clears existing grader rows** — re-register after). |
| Config | Vertex service account + `GRADER_USE_VERTEX=true`, other `GRADER_*` knobs. The `/grader` endpoints are public by design (see §5). |

## 1. Merge & deploy the code
1. Merge the PR to `main`.
2. On EC2, check out the release commit and install deps in the venv:
   `pip install -r requirements.txt`.

## 2. Provision the Vertex credential — never in git
The grader's handwriting-OCR call runs ~150 s, which **504s on AI Studio**'s
server-side deadline but completes on **Vertex**. Production must therefore have
a usable Vertex service account. **Do not commit the key** (`.gitignore` already
excludes `vertex-key.json` and the project key filename).

Pick one:
- **Key file on the host (simplest):** copy the SA JSON out-of-band (SSM Run
  Command, Secrets Manager → file, or `scp`) to e.g. `/opt/apguru/secrets/vertex-sa.json`
  (`chmod 600`, owned by the app user). Set `GOOGLE_APPLICATION_CREDENTIALS` to it.
- **No key file (preferred long-term):** attach the service account to the EC2
  instance role (Workload Identity / Application Default Credentials). The
  google-genai SDK picks it up automatically — nothing to store or rotate.

## 3. Environment variables (prod)
Set via your process manager (systemd `Environment=` / gunicorn env / SSM params)
or the prod `.env` (which is gitignored):

```
# DB → production MySQL (NOT local, NOT UAT)
USE_LOCAL_DB=false
DB_HOST=...   DB_USER=...   DB_PASSWORD=...   DB_NAME=...
JWT_SECRET=...                     # >= 32 chars (still required at startup)

# Gemini: chat/LLM keeps using AI Studio; the grader is routed to Vertex.
GEMINI_API_KEY=...                 # used by chat + non-grader LLM features
GRADER_USE_VERTEX=true             # default true; grader prefers Vertex when usable
GOOGLE_APPLICATION_CREDENTIALS=/opt/apguru/secrets/vertex-sa.json   # (omit if using ADC)
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global       # Gemini 3.x is served from the global endpoint

# The /grader endpoints are intentionally public (no JWT); see §5. Nothing to set.
```

`prefer_vertex` is honoured **only when the key file exists** (or ADC is
available); otherwise the grader safely falls back to `GEMINI_API_KEY`, so a
misconfigured key path degrades to AI Studio rather than crashing.

## 4. Database migrations
1. **Back up the prod DB.**
2. `alembic upgrade head` — applies 020/021/022 (idempotent: `CREATE TABLE IF
   NOT EXISTS` / upserts) **and 026**. ⚠️ **026 is destructive for grader data:**
   it `DELETE`s every `ap_exam` / `grading_job` row before re-keying the schema
   to `test_id` (the old `exam_key`/`year`/`set_label` rows can't be migrated in
   place). The grader is parse-once and re-registration is cheap, so this is
   intended — just re-register after (step 4 / §6).
3. Verify:
   - tables `ap_exam`, `grading_job` exist; `ap_exam` has `test_id` (unique) +
     `test_name`, no `exam_key`/`year`/`set_label`/`question_map`; `grading_job`
     has `answers_json`, no `source_test_id`/`source_quiz_id`;
   - `course_configs` has ids 14–36 **and** 17/21/22/31 (all `is_active=1`).
4. **Re-register exams after 026** (they were cleared):
   `python scripts/tests/grader/register_all_exams.py`.

## 5. Grader auth on production — PUBLIC BY DESIGN (no JWT)
The four `/api/v1/grader/*` endpoints are intentionally public in **every**
environment — there is no JWT dependency and no feature flag. (The former
`config/feature_flags.json` / `GRADER_REQUIRE_AUTH` machinery has been removed;
`app/api/router.py` registers the grader router with no auth dependency.)
Everything else in the API stays JWT-protected.

> ⚠️ **Security caveat.** These endpoints (a) **fetch caller-supplied PDF URLs**
> and (b) **return student scorecards** (PII), with **no in-app authorization**
> (the `student_id` is taken from the request body and is not checked against any
> token). You MUST compensate at the edge:
> - Restrict `/api/v1/grader/*` at the ALB / Nginx / security-group / API-gateway
>   / WAF level (IP allowlist, internal-only listener, or gateway-level auth).
> - Keep an egress allowlist so the PDF fetch can only reach trusted hosts (e.g.
>   the `papervideo` S3 bucket). The app already SSRF-guards the fetch
>   (`app/services/grader/url_guard.py`: rejects non-HTTP(S) schemes and any host
>   resolving to a private / loopback / link-local / cloud-metadata IP, and
>   re-validates every redirect hop) — the edge egress allowlist is recommended
>   defence-in-depth on top.
>
> Requiring auth later is a code change (re-add a `Depends(authorize)` dependency
> on the grader router in `app/api/router.py`), not a config toggle.

## 6. Restart & smoke test
1. Restart the app (systemd/gunicorn).
2. `GET /api/v1/grader/exams` → `200`.
3. End-to-end on the host (set `GRADER_BASE_URL` to the prod base, e.g.
   `http://127.0.0.1:8080/api/v1`):
   - `python scripts/tests/grader/register_all_exams.py` — registers every exam.
   - `python scripts/tests/grader/test_grader_handwritten_e2e.py biology` —
     register → submit → poll → scorecard. Confirm the job reaches `succeeded`
     (i.e. OCR ran on Vertex, **no 504**).

## Rollback
- **Code:** redeploy the previous commit.
- **DB:** `alembic downgrade 021` drops the 4 extra courses (022.downgrade).
  020/021 are additive — leave them unless fully removing the grader.

## Cost / capacity note
OCR uses `gemini-3.1-pro-preview` on Vertex (~150–160 s for a ~7-page handwritten
submission). `grader_max_concurrent_jobs` (default 2) caps in-flight grades;
excess jobs queue. Size the instance and Vertex quota accordingly.
