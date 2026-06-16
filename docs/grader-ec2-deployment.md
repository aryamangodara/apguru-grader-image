# Deploying the AP FRQ Grader to EC2 (Docker + CI/CD)

The grader runs as its **own Docker container** on a shared EC2 host that already
runs other containers. To avoid fighting for ports 80/443, the grader does **not**
ship its own reverse proxy: the `app` container binds to **`127.0.0.1:8081`** and
is fronted by the host's edge proxy (see [`nginx/nginx.conf`](../nginx/nginx.conf)
for a sample vhost). Pushes to `main` auto-deploy via GitHub Actions
([`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)).

## Architecture

```
Internet ──TLS──► host edge proxy (owns 80/443, access control)
                        │  proxy_pass
                        ▼
                  127.0.0.1:8081  ──►  apguru-grader app container (:8080)
                                              │
                                              ├─ Gemini / Vertex AI
                                              └─ MySQL (prod)
```

- Single container (`docker-compose.yml`, project name `apguru-grader`), bound to
  localhost so it is never directly internet-exposed.
- `alembic upgrade head` runs from the image entrypoint on every container start
  (idempotent), so migrations apply automatically on deploy.
- Logs go to CloudWatch via the `awslogs` driver (group `/apguru/app`, stream
  `apguru-grader`).

## 1. One-time host setup

1. **Docker + compose plugin** installed (the repo's `deploy.sh` bootstraps a
   fresh box).
2. **Deploy directory**, e.g. `/opt/apguru/grader`, owned by the deploy user. CI
   rsyncs the repo here.
3. **Secrets on the host — never in git** (the rsync deploy explicitly preserves
   these):
   - `.env` — production values:
     ```
     USE_LOCAL_DB=false
     DB_HOST=...   DB_USER=...   DB_PASSWORD=...   DB_NAME=...
     GRADER_USE_VERTEX=true
     GOOGLE_CLOUD_PROJECT=your-gcp-project-id
     GOOGLE_CLOUD_LOCATION=global
     GOOGLE_APPLICATION_CREDENTIALS=/app/vertex-key.json   # container path; omit if using ADC
     GRADER_HOST_PORT=8081                                  # change if 8081 is taken
     ```
   - `vertex-key.json` — the Vertex service-account key, in the deploy dir
     (`chmod 600`). The handwriting-OCR call runs ~150s and **504s on AI Studio**
     but completes on Vertex, so production needs a usable Vertex credential.
     *Alternative:* attach the service account to the **EC2 instance role** (ADC) —
     then delete the `vertex-key.json` volume line in `docker-compose.yml` and the
     `GOOGLE_APPLICATION_CREDENTIALS` env var; the SDK picks the role up
     automatically.
4. **Edge proxy + DNS + TLS.** Point a subdomain (e.g. `grader.apguru.com`) at the
   host, terminate TLS, and `proxy_pass` to `127.0.0.1:8081`. Use
   [`nginx/nginx.conf`](../nginx/nginx.conf) as a starting point, or add an
   equivalent vhost to your existing proxy.
5. **First boot** (from the deploy dir): `docker compose -p apguru-grader up -d --build`.

## 2. Access control — REQUIRED (public endpoints, PII)

The four `/api/v1/grader/*` endpoints are public by design (no JWT, no in-app
authorization — `student_id` comes from the request body and is not checked). They
**fetch caller-supplied PDF URLs** and **return student scorecards (PII)**. You
**must** compensate at the edge:

- Restrict `/api/v1/grader/*` at the proxy / security group / WAF (IP allowlist,
  internal-only listener, or gateway auth). See the sample vhost's access block.
- Keep an egress allowlist so the PDF fetch can only reach trusted hosts. The app
  already SSRF-guards the fetch (`app/services/grader/url_guard.py`: rejects
  non-HTTP(S) schemes and private/loopback/link-local/metadata IPs, re-validating
  every redirect hop) — the edge allowlist is defence-in-depth on top.

Requiring auth later is a code change (add `Depends(authorize)` back on the grader
router), not a config toggle.

## 3. CI/CD — push to `main` → deploy (self-hosted runner)

Deployment runs on a **GitHub Actions self-hosted runner installed on the EC2
host** — no SSH key, no stored repo secrets, no inbound ports. On every push to
`main` (or manual `workflow_dispatch`),
[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. `actions/checkout` pulls the code into the runner workspace (via the built-in
   `GITHUB_TOKEN`).
2. `rsync` the checkout into the deploy dir (`DEPLOY_DIR`, default
   `/opt/apguru/grader`), preserving host-only `.env` / `vertex-key.json` via
   excludes.
3. `docker compose -p apguru-grader up -d --build` (builds the image, runs
   migrations on start), then `docker image prune -f`.
4. Poll `http://127.0.0.1:8081/api/v1/health` until healthy (dumps container logs
   and fails the run if not).

### Install the runner (one-time)

1. In GitHub: **Settings → Actions → Runners → New self-hosted runner** (Linux).
   Run the download + `./config.sh` steps it shows; when prompted for **labels**,
   add **`apguru-grader`** — the workflow targets
   `runs-on: [self-hosted, apguru-grader]`, which pins the job to this box even if
   you have other self-hosted runners.
2. Install it as a service so it survives reboots:
   `sudo ./svc.sh install && sudo ./svc.sh start`.
3. Give the runner's user Docker + deploy-dir access:
   `sudo usermod -aG docker <runner-user>` (re-login to apply), make it own
   `DEPLOY_DIR`, and ensure `rsync` + `curl` are installed.
4. Put the host-only secrets in `DEPLOY_DIR`: the prod `.env` and `vertex-key.json`
   (§1). They are never in git and the deploy preserves them.

**No repo secrets are required.** Optional Actions **variables** (Settings →
Secrets and variables → Actions → *Variables*): `DEPLOY_DIR` (if not
`/opt/apguru/grader`) and `GRADER_HOST_PORT` (if not `8081`; keep it in sync with
the host `.env`).

> **Security:** the workflow triggers only on `push` to `main` (and manual
> dispatch), so fork/PR code can't execute on the box. Anyone who can push to
> `main` can run commands on the host via the runner — protect `main` and scope
> write access accordingly.

## 4. Smoke test after deploy

```bash
# On the host (or through the proxy with the right Host header):
curl http://127.0.0.1:8081/api/v1/health            # → 200
GRADER_BASE_URL=http://127.0.0.1:8081/api/v1 \
  python scripts/tests/grader/test_grader_handwritten_e2e.py biology
# register → submit → poll → scorecard; confirm the job reaches "succeeded"
# (OCR ran on Vertex, no 504).
```

## Rollback

Redeploy a previous commit (revert on `main`, or check out the prior commit in the
deploy dir and `docker compose -p apguru-grader up -d --build`). Migrations are
additive; only migration `026` was destructive to grader rows and is already
applied — re-register exams if you ever reset that data.

## Capacity note

OCR uses `gemini-3.1-pro-preview` on Vertex (~150–160s for a ~7-page handwritten
submission). `grader_max_concurrent_jobs` (default 2) caps in-flight grades; excess
jobs queue. The build runs **on the host**, so size the instance for occasional
build spikes plus the Vertex quota.
