# 🚀 CI/CD Setup Guide — Release‑Deploy the Grader to EC2

This guide walks you, **one slow step at a time**, through wiring up deployment so
that **publishing a GitHub Release rebuilds and restarts the grader on your EC2
box** (merging to `main` does *not* deploy) — with **no SSH keys and no stored
GitHub secrets**.

> ⏱️ **Time:** ~20–30 minutes, done once.
> 🔑 **Secrets to store in GitHub:** none. The deploy runs *on* your EC2 box via a
> self‑hosted runner, which pulls code using GitHub's built‑in token.

---

## 🗺️ What you're building

```
   you ──publish release (vX.Y.Z)──► GitHub
                        │  triggers
                        ▼
            ┌──────────────────────────────┐
            │   EC2 host (your server)      │
            │                               │
            │   GitHub self‑hosted runner   │ ← runs the deploy job locally
            │           │                   │
            │           ▼                   │
            │   docker compose up --build   │ → grader container on 127.0.0.1:8081
            │           │                   │
            │           ▼                   │
            │   host reverse proxy (TLS)    │ → grader.yourdomain.com
            └──────────────────────────────┘
```

**The moving parts you'll set up:**

| # | Part | Where |
|---|------|-------|
| A | Docker + a deploy folder + your secret files | on EC2 |
| B | A GitHub Actions **self‑hosted runner** | on EC2 |
| C | (Optional) Actions **variables** | on GitHub |
| D | Trigger & verify the first deploy | GitHub + EC2 |
| E | (To go public) a reverse proxy + DNS + TLS | on EC2 |

> 💡 Already in the repo for you: the deploy workflow
> (`.github/workflows/deploy.yml`), the container setup (`docker-compose.yml`), and
> a sample proxy config (`nginx/nginx.conf`). You don't edit these — you just set
> up the host around them.

---

## ✅ Before you start

Make sure you have:

- [ ] SSH access to the EC2 box, with `sudo`.
- [ ] **Docker** + the **compose plugin** installed (`docker compose version` works).
- [ ] **Admin access** to the GitHub repo `aryamangodara/apguru-grader-image`
      (you need *Settings → Actions* to add a runner).
- [ ] Your **production values** handy: MySQL host/user/password, and your
      **Vertex AI** service‑account JSON (or an instance role — see Step A3).

> 🐳 No Docker yet? The repo's `deploy.sh` bootstraps it on a fresh box, or install
> Docker Engine + the compose plugin the normal way, then come back.

---

## 🖥️ Part A — Prepare the EC2 host

### Step A1 — Create the deploy folder
*Why: this is the stable home for the app on the server. The runner syncs code
here on every deploy, and your secrets live here untouched.*

```bash
sudo mkdir -p /opt/apguru/grader
```

> ✅ **Check:** `ls -ld /opt/apguru/grader` shows the folder.

---

### Step A2 — Create the `.env` file (your settings)
*Why: the app reads all its configuration from this file. It is **never** in git,
so it lives only on the server.*

Create `/opt/apguru/grader/.env` and paste this, filling in your real values:

```dotenv
# --- Database (production MySQL) ---
USE_LOCAL_DB=false
DB_HOST=your-prod-mysql-host
DB_PORT=3306
DB_USER=your-db-user
DB_PASSWORD=your-db-password
DB_NAME=apguru

# --- Gemini / Vertex AI (the grader's LLM) ---
GEMINI_API_KEY=your-gemini-api-key
GRADER_USE_VERTEX=true
GOOGLE_APPLICATION_CREDENTIALS=/app/vertex-key.json   # path INSIDE the container
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global

# --- Deployment ---
GRADER_HOST_PORT=8081        # host port the container binds to (127.0.0.1 only)

# --- Langfuse tracing (optional; leave blank to disable) ---
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

> ⚠️ `DB_HOST`, `DB_USER`, and `DB_PASSWORD` are **required** — the app refuses to
> start without them (this is intentional, so a misconfig fails loudly).
> 💡 Is `8081` already used by one of your other containers? Pick a free port here
> and remember it for Step C.

> ✅ **Check:** `cat /opt/apguru/grader/.env` shows your values.

---

### Step A3 — Add the Vertex AI credential
*Why: the handwriting‑OCR call runs ~150s and times out on the AI Studio key, but
works on Vertex — so production needs a Vertex credential.*

**Pick one option:**

**Option 1 — key file (simplest).** Copy your service‑account JSON to the deploy
folder as `vertex-key.json` and lock it down:

```bash
# (copy the file up however you like: scp, SSM, Secrets Manager…)
chmod 600 /opt/apguru/grader/vertex-key.json
```

**Option 2 — instance role (no file to manage).** Attach the service account to the
EC2 instance role. Then **edit `/opt/apguru/grader` later isn't needed** — but you
must remove the key mount: in `docker-compose.yml` delete the
`- ./vertex-key.json:/app/vertex-key.json:ro` line and remove
`GOOGLE_APPLICATION_CREDENTIALS` from `.env`. The SDK picks up the role
automatically.

> ✅ **Check (Option 1):** `ls -l /opt/apguru/grader/vertex-key.json` shows the file
> with `-rw-------` permissions.

---

## 🤖 Part B — Install the self‑hosted runner

This is the piece that makes "push → deploy" work. It's a small program from
GitHub that runs on your EC2 box and waits for jobs.

### Step B1 — Open the runner page on GitHub
*Why: GitHub generates a one‑time registration command (with a token) for you.*

Go to your repo → **Settings → Actions → Runners → New self‑hosted runner** →
choose **Linux**. Keep that page open; it shows a **Download** block and a
**Configure** block with commands.

---

### Step B2 — Download the runner on the EC2 box
*Why: this installs the runner program into a folder in your home directory.*

Run the **Download** commands GitHub shows you (versions change, so copy theirs).
They look like this:

```bash
mkdir actions-runner && cd actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/download/vX.Y.Z/actions-runner-linux-x64-X.Y.Z.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
```

> ✅ **Check:** `ls` inside `actions-runner` shows `config.sh` and `run.sh`.

---

### Step B3 — Configure the runner (with the right label!)
*Why: the label is how the deploy job finds **this** box. The workflow targets
`runs-on: [self-hosted, apguru-grader]`, so the runner must carry that label.*

Use GitHub's **Configure** command, but **add `--labels apguru-grader`**:

```bash
./config.sh \
  --url https://github.com/aryamangodara/apguru-grader-image \
  --token <THE_TOKEN_FROM_GITHUB> \
  --labels apguru-grader \
  --name apguru-grader-ec2 \
  --unattended
```

> ⚠️ The token is shown on the GitHub runner page and expires quickly — copy it
> fresh. (It only registers the runner; it isn't stored anywhere afterward.)

> ✅ **Check:** the command ends with `√ Runner successfully added`. In GitHub
> (Settings → Actions → Runners) the runner appears (it may say *Offline* until the
> next step).

---

### Step B4 — Run it as a service (so it survives reboots)
*Why: as a service it starts automatically and keeps running in the background.*

```bash
sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status
```

> ✅ **Check:** `svc.sh status` says **active (running)**, and the runner shows
> **Idle / Online** (green) in GitHub.

---

### Step B5 — Give the runner Docker + folder permissions
*Why: the deploy job runs Docker and writes into the deploy folder, so the runner's
user needs both.*

Find the user the runner runs as (often `ubuntu` or `ec2-user` — the one you ran
`config.sh` as), then:

```bash
# allow Docker without sudo
sudo usermod -aG docker $(whoami)

# let the runner own the deploy folder
sudo chown -R $(whoami):$(whoami) /opt/apguru/grader

# make sure the deploy tools exist
sudo apt-get install -y rsync curl    # (Debian/Ubuntu; use yum/dnf on Amazon Linux)

# restart the runner so the new docker group takes effect
cd ~/actions-runner && sudo ./svc.sh stop && sudo ./svc.sh start
```

> ✅ **Check:** `docker ps` works **without** `sudo` for the runner user.

---

## ⚙️ Part C — Optional GitHub variables

Only if your paths/ports differ from the defaults. These are **variables, not
secrets** (Settings → Secrets and variables → Actions → **Variables** tab):

| Variable | Set it if… | Default |
|----------|------------|---------|
| `DEPLOY_DIR` | your deploy folder isn't `/opt/apguru/grader` | `/opt/apguru/grader` |
| `GRADER_HOST_PORT` | you chose a port other than `8081` in `.env` | `8081` |

> 💡 If you're using the defaults, skip this part entirely.

---

## ✅ Part D — Deploy & verify

### Step D1 — Trigger the first deploy
*Why: to confirm the whole chain works end‑to‑end.*

Either:
- **Publish a release:** `gh release create v1.0.0 --target main --generate-notes`
  (or GitHub → **Releases → Draft a new release**), **or**
- GitHub → **Actions → "Deploy to EC2" → Run workflow** (manual trigger, no release).

---

### Step D2 — Watch it run
Open the **Actions** tab and click the running job. You'll see the steps:
*Checkout → Sync code → Build & restart → Health check.* A green check ✅ means the
grader is live.

---

### Step D3 — Confirm on the box
*Why: double‑check the container is up and healthy.*

```bash
curl http://127.0.0.1:8081/api/v1/health      # → {"status":"ok","message":"Service is healthy"}
docker compose -p apguru-grader ps             # container is "Up (healthy)"
docker compose -p apguru-grader logs --tail=50 # recent logs
```

> 🎉 **That's the CI/CD loop done.** From now on, **publish a release to deploy** —
> merges to `main` are safe and won't touch prod until you cut a release.

---

## 🔌 Part E — Make it reachable (reverse proxy)

The container is bound to `127.0.0.1:8081` **on purpose** — it's not exposed to the
internet directly, because the `/grader` endpoints are public and handle student
data. Put your reverse proxy in front of it.

1. Use [`nginx/nginx.conf`](../nginx/nginx.conf) as a template (it proxies a
   subdomain → `127.0.0.1:8081` with TLS and an access‑control block).
2. Point a DNS record (e.g. `grader.yourdomain.com`) at the box and install a TLS
   cert.
3. **Lock it down** ⚠️ — add an IP allowlist / internal‑only listener / WAF in the
   proxy. These endpoints have no built‑in auth and return PII.

> Full details and the security rationale are in
> [`grader-ec2-deployment.md`](grader-ec2-deployment.md).

---

## 🛠️ Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Job stuck **"Waiting for a runner"** | runner offline or wrong label | Check `svc.sh status`; confirm the runner has the `apguru-grader` label |
| `permission denied … /var/run/docker.sock` | runner user not in `docker` group | `sudo usermod -aG docker <user>`, then restart the runner service |
| `port is already allocated` | `8081` used by another container | Pick a free port in `.env` **and** the `GRADER_HOST_PORT` variable |
| Container **unhealthy** / health check fails | bad DB creds or missing env | `docker compose -p apguru-grader logs`; fix `/opt/apguru/grader/.env` |
| `failed to initialize logging driver: awslogs` | host can't reach CloudWatch | Attach an IAM role with CloudWatch Logs perms, **or** switch logging (below) |
| `.env: no such file` | secrets not in the deploy folder | Create `/opt/apguru/grader/.env` (Step A2) |
| `vertex-key.json` mount error | key file missing | Add the file (Step A3 Opt 1) or switch to instance role (Opt 2) |
| Migrations fail on start | DB unreachable | Check `DB_HOST`/creds and the MySQL security group |

**Switch logging to local files** (if `awslogs` isn't set up on your box) — in
`docker-compose.yml` replace the `logging:` block with:

```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

---

## 📋 Final checklist

- [ ] `/opt/apguru/grader/.env` created with real values
- [ ] `vertex-key.json` in place (or instance role configured)
- [ ] Runner installed with the **`apguru-grader`** label and **Online**
- [ ] Runner user can run `docker` without `sudo`
- [ ] First deploy is green in the **Actions** tab
- [ ] `curl http://127.0.0.1:8081/api/v1/health` returns `ok`
- [ ] Reverse proxy + TLS + access control in front (Part E)

Once these are ticked, you're done — **publish a release and it ships.** 🚢
