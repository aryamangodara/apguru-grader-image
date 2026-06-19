---
name: cut-release
description: >-
  Cut and ship a production release of the APGuru Grader: run the pre-flight
  gate, pick a SemVer version, and publish the GitHub Release that triggers the
  release-gated EC2 deploy. Use this whenever the user wants to make / cut /
  ship / publish / tag a release, roll out a new version, deploy to production,
  or "push this live" — even if they only say "release it" or "ship it".
  Because publishing a GitHub Release auto-deploys to prod, prefer this skill
  over ad-hoc `gh release` / `git tag` commands so the version-bump and
  verify-the-deploy guardrails are never skipped.
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

> **Database migrations are NOT part of a grader release.** The schema is owned by
> the central [`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic)
> repo, whose own CI/CD applies `alembic upgrade head` to the shared prod DB when a
> migration PR lands on *its* `main`. The grader app does **not** migrate on boot.
> If the code you're releasing needs a new table/column or a course seed, make sure
> the matching migration PR has already merged in that repo (so the schema exists
> before the app that depends on it ships). See **Schema changes** below.

## What happens the moment you publish

Publishing a release fires [`.github/workflows/deploy.yml`](../../../.github/workflows/deploy.yml)
on the `[self-hosted, apguru-grader]` runner installed on the EC2 box:

1. **Checkout** the released tag's commit (a reproducible deploy of exactly what
   was tagged).
2. **rsync** the tree into the deploy dir (`/opt/apguru/grader`), preserving the
   host-only `.env` and `vertex-key.json`.
3. **`docker compose -p apguru-grader up -d --build`** — rebuilds the image and
   restarts the container, whose entrypoint is just **`gunicorn`**. No migrations
   run here; the app talks to a schema the central pipeline already applied.
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
- **Schema changes ship separately, in the central repo.** A grader release never
  migrates the DB. If this release's code depends on a schema change, land that in
  [`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic)
  **first** (its CI/CD applies it to prod on merge), then release the app. Shipping
  app code ahead of its migration means runtime errors against a missing table/column.
- **Release from a fully-merged, green `main`.** The deploy is built from the
  tag, so whatever is on `main` at tag time is what ships. Land every intended
  PR first and confirm tests pass.
- **Use SemVer and keep the in-app version in sync.** Tag `vMAJOR.MINOR.PATCH`.
  The FastAPI `version=` in [`app/main.py`](../../../app/main.py) should match the
  tag; if it lags, bump it as part of release prep (see step 3).
- **The grader endpoints are public by design** — access control is enforced at
  the edge, not in the app. A release doesn't change that, but never "fix" it by
  loosening the SSRF guard or binding the container to `0.0.0.0`.

## Workflow

### 1. Pre-flight gate (read-only — safe to run anytime)

Run the bundled checker with the project's venv interpreter so it exercises the
same pytest / ruff the deploy will:

```bash
venv/Scripts/python.exe .claude/skills/cut-release/scripts/release_preflight.py
# macOS/Linux host: venv/bin/python .claude/skills/cut-release/scripts/release_preflight.py
```

It verifies, and prints a GO / REVIEW summary for:

- you're on `main` and **not behind** `origin/main` (the deploy ships `main`),
- `pytest tests/` is green (blocking),
- `ruff check .` (advisory — the repo carries a few known lints),
- the commits since the last tag, and a **suggested next version** inferred from
  their Conventional-Commit types,
- the current in-app version string vs. the latest tag.

If it reports a blocking failure, stop and fix the cause — a red pre-flight is a
red deploy.

**Schema changes (do this when the release needs one).** The pre-flight can't see
the prod DB. If any code in this release reads/writes a new table, column, or seeded
course, confirm the corresponding migration PR has **already merged** in
[`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic)
and that its deploy-prod workflow ran green (so prod actually has the schema). If the
migration isn't in yet, land it there first — the grader won't create schema itself.
A release with no schema dependency needs nothing here.

### 2. Choose the version

Look at what's shipping (`git log <last-tag>..origin/main --oneline`) and apply
SemVer:

| Bump | When | Example |
|---|---|---|
| **MAJOR** (`x.0.0`) | Breaking API/contract change consumers must adapt to | `v2.0.0` |
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
at the end — the cause is usually there.

- **Bad app release:** **redeploy a known-good ref** via manual dispatch —
  `gh workflow run deploy.yml --ref vLAST-GOOD` — or **publish a hotfix release**
  from a fix commit on `main` (steps 1–5, PATCH bump).
- **App expects schema that isn't there** (e.g. "Unknown column" / "doesn't exist"):
  the migration hasn't been applied. Land/merge the migration PR in
  `apguru-centralized-alembic` (its pipeline applies it), then redeploy. An app
  rollback alone won't help if the code was already shipped ahead of its migration —
  fix the ordering.

Because the app no longer migrates, an app rollback never touches the DB. Schema
roll-forward/rollback is handled entirely in the central repo (additive migrations;
fix forward with a new migration there rather than hand-editing prod).

## Pre-flight checklist (copy-paste)

```
[ ] On main, synced with origin/main (not behind); intended PRs all merged
[ ] pytest tests/ green
[ ] Any schema this release needs is already merged + applied via apguru-centralized-alembic
[ ] Version picked (SemVer) and app/main.py version= bumped + merged
[ ] Release notes previewed (gh ... --generate-notes --draft)
[ ] Explicit go-ahead to deploy to production
[ ] gh release create vX.Y.Z --target main --generate-notes
[ ] Deploy workflow watched to green (gh run watch)
[ ] Prod health 200 + new version visible at /docs
```

## Pointers

- **Database migrations (separate repo):**
  [`apguru-centralized-alembic`](https://github.com/aryamangodara/apguru-centralized-alembic)
  — open a migration PR there; its CI/CD applies it to prod on merge.
- **Infra deep-dive / host setup / rollback detail:**
  [`docs/grader-ec2-deployment.md`](../../../docs/grader-ec2-deployment.md)
- **The deploy workflow itself:**
  [`.github/workflows/deploy.yml`](../../../.github/workflows/deploy.yml)
- **Opening the version-bump PR:** the `create-pr` skill
- **Pre-flight checker:** `.claude/skills/cut-release/scripts/release_preflight.py`
