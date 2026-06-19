---
name: cut-release
description: >-
  Cut and ship a production release of the APGuru Grader: run the pre-flight
  gate, pick a SemVer version, and publish the GitHub Release that triggers the
  release-gated EC2 deploy. Use this whenever the user wants to make / cut /
  ship / publish / tag a release, roll out a new version, deploy to production,
  or "push this live" — even if they only say "release it" or "ship it".
  Because publishing a GitHub Release auto-deploys to prod (and runs DB
  migrations on container start), prefer this skill over ad-hoc `gh release` /
  `git tag` commands so the alembic-chain, version-bump, and verify-the-deploy
  guardrails are never skipped.
---

# Cut a Release

This skill turns a green `main` into a deployed production release, the way this
repo actually ships. **Deploys are release-gated**: merging a PR to `main` does
*not* deploy — publishing a GitHub Release (tag `vX.Y.Z`) is what tells the
self-hosted runner on the EC2 host to rebuild and restart the grader container
at that tag's commit. So `main` can safely run ahead of production, and *the
release is the deploy*.

There is no separate release doc the team follows — **this skill is the release
runbook.** The deeper infra details live in
[`docs/grader-ec2-deployment.md`](../../../docs/grader-ec2-deployment.md); read it
when you need the host setup, edge proxy, or CloudWatch specifics.

## What happens the moment you publish

Publishing a release fires [`.github/workflows/deploy.yml`](../../../.github/workflows/deploy.yml)
on the `[self-hosted, apguru-grader]` runner installed on the EC2 box:

1. **Checkout** the released tag's commit (a reproducible deploy of exactly what
   was tagged).
2. **rsync** the tree into the deploy dir (`/opt/apguru/grader`), preserving the
   host-only `.env` and `vertex-key.json`.
3. **`docker compose -p apguru-grader up -d --build`** — rebuilds the image and
   restarts the container. The container entrypoint runs
   **`alembic upgrade head` then `gunicorn`**, so **DB migrations apply here, on
   the shared prod DB (`uat_apguru_new`)**.
4. **Health check** — polls `http://127.0.0.1:8081/api/v1/health` for ~90s and
   fails the run (dumping container logs) if it never comes up.

## Golden rules (and why they matter)

- **Publishing a Release deploys to production. Confirm before you publish.**
  `gh release create` is the trigger — it's outward-facing and hard to unwind.
  Do all the prep first, show the human the exact version + notes, and publish
  only on an explicit go-ahead.
- **Never push to `main`; never bypass branch protection.** `main` is PR-only.
  Any code change a release needs (e.g. the version bump) goes through a normal
  PR — use the `create-pr` skill — and the maintainer merges it. Don't
  `--admin`, don't force-push, don't self-merge.
- **The alembic chain must be a single head *and* in sync with the shared prod
  DB.** Because the container runs `alembic upgrade head` on boot against
  `uat_apguru_new` (shared with other apps), a multi-head chain or a prod DB
  whose revision this codebase doesn't know about makes the container **crash on
  start** — a failed deploy. Verify a single head and that prod is reachable
  from this chain *before* publishing. (See the pre-flight script.)
- **Release from a fully-merged, green `main`.** The deploy is built from the
  tag, so whatever is on `main` at tag time is what ships. Land every intended
  PR first and confirm tests pass.
- **Use SemVer and keep the in-app version in sync.** Tag `vMAJOR.MINOR.PATCH`.
  The FastAPI `version=` in [`app/main.py`](../../../app/main.py) should match the
  tag (it is currently **stale** — `1.0.0` while the latest tag is `v1.1.0`);
  bump it as part of release prep.
- **The grader endpoints are public by design** — access control is enforced at
  the edge, not in the app. A release doesn't change that, but never "fix" it by
  loosening the SSRF guard or binding the container to `0.0.0.0`.

## Workflow

### 1. Pre-flight gate (read-only — safe to run anytime)

Run the bundled checker with the project's venv interpreter so it exercises the
same pytest / ruff / alembic the deploy will:

```bash
venv/Scripts/python.exe .claude/skills/cut-release/scripts/release_preflight.py
# macOS/Linux host: venv/bin/python .claude/skills/cut-release/scripts/release_preflight.py
```

It verifies, and prints a GO / REVIEW summary for:

- you're on `main` and **not behind** `origin/main` (the deploy ships `main`),
- `pytest tests/` is green (blocking),
- `ruff check .` (advisory — the repo carries a few known lints),
- the alembic chain has **exactly one head** (blocking — multi-head crashes the
  boot migration),
- the commits since the last tag, and a **suggested next version** inferred from
  their Conventional-Commit types,
- the current in-app version string vs. the latest tag.

If it reports a blocking failure, stop and fix the cause — a red pre-flight is a
red deploy.

**HARD GATE — prod-DB alembic sync (the script can't check this; do it EVERY
release, even one with no migrations).** `uat_apguru_new` is *shared* with the
parent analytics app, which migrates the common `alembic_version` on its own —
so prod can sit **ahead** of this repo's chain with zero migrations in *your*
release. The container re-runs `alembic upgrade head` on every restart, so a
prod revision this chain doesn't contain **crash-loops the new container and
takes the grader down**. With prod creds in `.env` (`USE_LOCAL_DB=false`):

```bash
venv/Scripts/python.exe -m alembic heads      # the revision this code knows as head
venv/Scripts/python.exe -m alembic current    # the revision prod is actually on
```

Prod's `current` **must** be a revision in this chain (an ancestor of `heads`,
or equal). If it's a revision this repo doesn't have (e.g. prod at `030` while
the chain tops at `028`), **DO NOT PUBLISH** — port the missing revisions into
`alembic/versions/` first, or reconcile per the runbook. If you *can't* run this
check (no prod creds, blocked by a guardrail), the release is **blocked** until
it's confirmed — never substitute the reasoning "no new migrations, so it's
safe." That exact rationalization took prod down at v1.2.0 (2026-06-18): chain
at `028`, prod drifted to `030`, boot migration crash-looped. See the
`release-alembic-sync-gate` memory.

### 2. Choose the version

Look at what's shipping (`git log <last-tag>..origin/main --oneline`) and apply
SemVer:

| Bump | When | Example |
|---|---|---|
| **MAJOR** (`x.0.0`) | Breaking API/contract change, or a destructive migration consumers must know about | `v2.0.0` |
| **MINOR** (`1.x.0`) | New backward-compatible capability (`feat:`) — a new endpoint, new grading mode | `v1.2.0` |
| **PATCH** (`1.1.x`) | Bug fixes / chores only (`fix:`, `chore:`), no new surface | `v1.1.1` |

The pre-flight script suggests a bump from the commit types — treat it as a
starting point, not gospel. Confirm the final number with the user.

### 3. Sync the in-app version (small PR)

If `app/main.py`'s `version=` doesn't already match the version you picked, bump
it so `/docs` and the OpenAPI spec report the truth. This is a code change, so it
goes through a PR — hand off to the **`create-pr`** skill:

```python
# app/main.py
app = FastAPI(
    title=settings.app_name,
    version="X.Y.Z",   # <- match the tag you're about to publish
    ...
)
```

The maintainer merges that PR to `main`. **Wait for it to land** before
publishing — the deploy builds from the tag, so the bump must be on `main` first.

### 4. Publish the release (the deploy trigger — confirm first)

This is the production deploy. Present the version and a preview of the
auto-generated notes, get an explicit go-ahead, then publish from `main`:

```bash
# Preview the notes without creating anything:
gh release create vX.Y.Z --target main --generate-notes --notes-start-tag vLAST --verify-tag --draft
# ...review, then publish for real (drop --draft):
gh release create vX.Y.Z --target main --generate-notes --title "vX.Y.Z — <one-line summary>"
```

- Tag name is `vX.Y.Z` (with the leading `v`, matching `v1.0.0` / `v1.1.0`).
- `--generate-notes` builds the changelog from merged PRs since the last tag —
  this repo has no `CHANGELOG.md`, the release notes *are* the changelog.
- Not authenticated? `gh auth status` → `gh auth login`.

### 5. Watch the deploy and verify

The release event starts the workflow within seconds. Watch it to completion:

```bash
gh run list --workflow=deploy.yml --limit 1
gh run watch <run-id>            # streams until the job finishes
```

Then confirm prod is actually serving the new build:

```bash
curl -fsS http://52.66.25.124:8081/api/v1/health           # → 200
# Optional end-to-end smoke against prod (real LLM + DB):
GRADER_BASE_URL=http://52.66.25.124:8081/api/v1 \
  venv/Scripts/python.exe scripts/tests/grader/test_grader_handwritten_e2e.py biology
```

If you bumped the version in step 3, the new number should show at
`http://52.66.25.124:8081/docs`. A green workflow **and** a healthy prod is the
definition of done — report both back to the user with the release URL.

### 6. If the deploy goes wrong — roll back

The workflow's health check fails loudly if the container doesn't come up. Get
its log tail (`gh run view <id> --log-failed`) and read the **container** dump
at the end — the cause is usually there. First, identify the failure mode:

- **"Can't locate revision …" (DB ahead of the chain).** A tag rollback will
  **NOT** fix this — every grader tag shares the same chain max, so v1.1.0
  crash-loops identically. The old container only stayed up because it hadn't
  restarted since the shared DB drifted. Fix it by porting the missing revisions
  into `alembic/versions/` (resync the chain), or by softening the boot migration
  so that *only* this drift is tolerated (the Dockerfile `CMD` already does this
  — it continues on "locate revision" but still aborts on any other alembic
  error). This is the step-1 HARD GATE you must check *before* publishing.
- **Any other failure** (a genuinely bad release): **redeploy a known-good ref**
  via manual dispatch — `gh workflow run deploy.yml --ref vLAST-GOOD` — or
  **publish a hotfix release** from a fix commit on `main` (steps 1–5, PATCH bump).

Migrations are additive and not auto-reversed; only `026` was ever destructive
(it cleared grader rows, already applied long ago). If a bad migration shipped,
fix forward with a new migration + release rather than hand-editing prod.

## Pre-flight checklist (copy-paste)

```
[ ] On main, synced with origin/main (not behind); intended PRs all merged
[ ] pytest tests/ green
[ ] alembic: single head, and prod `alembic current` is in this chain (HARD GATE — don't publish if unconfirmed)
[ ] Version picked (SemVer) and app/main.py version= bumped + merged
[ ] Release notes previewed (gh ... --generate-notes --draft)
[ ] Explicit go-ahead to deploy to production
[ ] gh release create vX.Y.Z --target main --generate-notes
[ ] Deploy workflow watched to green (gh run watch)
[ ] Prod health 200 + new version visible at /docs
```

## Pointers

- **Infra deep-dive / host setup / rollback detail:**
  [`docs/grader-ec2-deployment.md`](../../../docs/grader-ec2-deployment.md)
- **The deploy workflow itself:**
  [`.github/workflows/deploy.yml`](../../../.github/workflows/deploy.yml)
- **Opening the version-bump PR:** the `create-pr` skill
- **Pre-flight checker:** `.claude/skills/cut-release/scripts/release_preflight.py`
