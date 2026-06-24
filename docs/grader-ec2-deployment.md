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
- Migrations are **not** run by this container. The schema is owned by the central
  [`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic)
  repo, whose CI/CD applies `alembic upgrade head` to the shared prod DB on merge to
  its `main`. The grader app does not migrate on boot.
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

## 3. CI/CD — publish a Release → deploy (self-hosted runner)

Deployment is **release-gated** and runs on a **GitHub Actions self-hosted runner
installed on the EC2 host** — no SSH key, no stored repo secrets, no inbound ports.
**Merging to `main` does not deploy** (main can run ahead of prod). Publishing a
**GitHub Release** (a tag like `v1.2.0`) — or a manual `workflow_dispatch` —
triggers [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. `actions/checkout` pulls the **released tag's** code into the runner workspace
   (via the built-in `GITHUB_TOKEN`) — a reproducible deploy of exactly what you
   released.
2. `rsync` the checkout into the deploy dir (`DEPLOY_DIR`, default
   `/opt/apguru/grader`), preserving host-only `.env` / `vertex-key.json` via
   excludes.
3. `docker compose -p apguru-grader up -d --build` (builds the image and restarts
   the container — it does **not** run migrations), then `docker image prune -f`.
4. Poll `http://127.0.0.1:8081/api/v1/health` until healthy (dumps container logs
   and fails the run if not).

**To ship a deploy:** publish a release —
`gh release create vX.Y.Z --target main --generate-notes` (or GitHub → *Releases →
Draft a new release*). To redeploy `main` without cutting a release, use
**Actions → Deploy to EC2 → Run workflow**. To roll back, re-run the workflow from
an older tag (or publish a hotfix release).

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

> **Security:** the workflow triggers only on `release: published` (and manual
> dispatch), so fork/PR code can't execute on the box. Anyone who can publish a
> release (or run the workflow) can run commands on the host via the runner —
> `main` is branch-protected (PR-only); scope release/write access accordingly.

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
deploy dir and `docker compose -p apguru-grader up -d --build`). Schema changes are
decoupled from app deploys — migrations are applied by the central
`apguru-centralized-alembic` pipeline, so an app rollback does not roll back the DB.

## Capacity note

OCR uses `gemini-3.1-pro-preview` on Vertex (~150–160s for a ~7-page handwritten
submission). `grader_max_concurrent_jobs` (default 2) caps in-flight grades; excess
jobs queue. The build runs **on the host**, so size the instance for occasional
build spikes plus the Vertex quota.

## Memory guardrail

The shared host is RAM-constrained (a co-tenant worker uses ~1.5 GiB), so the grader
container is **capped at 1 GiB** in [`docker-compose.yml`](../docker-compose.yml)
(`mem_limit: 1g`; `memswap_limit: 1g` → no swap, so 1 GiB is a hard real-RAM ceiling;
`mem_reservation: 768m` → the soft target the kernel reclaims toward under host memory
pressure). This **contains an OOM to the grader's own cgroup**: when a grading-job spike
(PDF render @ `grader_ocr_dpi` + concurrent Gemini OCR) exceeds 1 GiB, the kernel kills
the offending process *inside this container* instead of the host running out of RAM and
the global OOM killer wedging the whole box (the 2026-06-23 incident).

**On OOM:** gunicorn respawns the killed worker (the container stays up); if PID 1 (the
gunicorn master) is killed, the container exits 137 and `restart: unless-stopped`
restarts it, returning healthy within the healthcheck `start_period`. gunicorn also
recycles each worker after ~200 requests (`max_requests` in
[`gunicorn.conf.py`](../gunicorn.conf.py)) so a long-lived worker's RSS can't creep
toward the cap.

**Inspect an OOM event / confirm the limits:**
```bash
docker inspect -f '{{.State.OOMKilled}} restarts={{.RestartCount}}' apguru-grader-app-1
docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.MemoryReservation}}' apguru-grader-app-1
# → 1073741824 1073741824 805306368
docker stats --no-stream apguru-grader-app-1   # MEM USAGE / LIMIT vs 1GiB
```

**Apply the cap live, without a release** — the `docker compose` config change otherwise
ships on the next release-gated `--build` deploy (and the `gunicorn` `max_requests`
change needs that rebuild):
```bash
docker update --memory 1g --memory-swap 1g --memory-reservation 768m apguru-grader-app-1
```

**If OOM-kills get frequent** (watch `docker stats`), the cheapest relief, in order:
lower `grader_ocr_dpi` (300→200 cuts render memory ~55%), set `GUNICORN_WORKERS=1`
(halves baseline RSS — the app is async and capped by `grader_max_concurrent_jobs`), or
lower `grader_max_concurrent_jobs` (2→1).

> **Host caveat:** `worker (~1.5 GiB) + grader (1 GiB) > 1.9 GiB` on a t3.small, so the
> 1 GiB cap bounds the *grader* but does not by itself guarantee the host never OOMs if
> both peak together. For full host safety, also cap the co-tenant worker the same way
> and/or upsize to t3.medium (4 GiB), and add a CloudWatch `StatusCheckFailed_Instance`
> alarm.
