---
name: create-pr
description: >-
  Stage, commit, push, and open a pull request for the APGuru Analytics Dashboard
  repo following this team's git conventions — Conventional Commits, <author>/<topic>
  branches, ruff + pytest pre-flight, and a gh-based PR against `main`. Use this
  whenever the user wants to commit changes, push a branch, "ship" / "raise" / "open"
  / "create" / "send up" a PR, or get their work reviewed — even if they only say
  "commit this" or "push my changes". Prefer this skill over ad-hoc git commands so
  every commit and PR in the project stays consistent and CI-friendly.
---

# Create a Pull Request

This skill turns working-tree changes into a clean, reviewable pull request that
matches how the APGuru Analytics Dashboard team already works. Follow it whenever you
commit, push, or open a PR so history stays readable and CI stays green.

The base branch is always **`main`**. There is no PR template or CONTRIBUTING file in
the repo — **this skill is the team's PR convention**, so keep it accurate.

## Golden rules (and why they matter)

- **This skill publishes work.** Committing and pushing are the point — but confirm
  *what* belongs in the PR before you push. Pushing the wrong changes is far more
  annoying to unwind than asking one clarifying question.
- **Never commit to `main`.** `main` is the integration branch and the CI/PR base.
  Branch first so every change is reviewable and revertible.
- **Stage deliberately — never blind `git add -A` / `git add .`.** This repo
  accumulates scratch at the root (`inspect_*.py`, `temp_script.js`, large `.html`
  dumps, `scratch/`). A blanket add sweeps junk — and sometimes secrets — into history.
  Add explicit paths and re-check `git status`.
- **Keep secrets and local config out.** `.env`, key files (`vertex-key.json`,
  `*-key.json`, `gen-lang-client-*.json`), and `.claude/settings.local.json` are
  gitignored on purpose. If one shows up staged, you added it by force — undo it.
- **Be green before you push.** Run what CI runs (`ruff check .`, `pytest tests/ -q`).
  A red PR burns a reviewer's time and round-trips the work.
- **Never bypass safety.** No `--no-verify`, no skipping CI. If a hook or test fails,
  fix the cause.

## Workflow

### 1. Inspect the working tree

```bash
git status
git diff            # unstaged changes
git diff --staged   # anything already staged
```

Know exactly what changed and decide what belongs in *this* PR. If unrelated changes
are mixed together, plan to split them (see the last section).

### 2. Get onto a feature branch

If you're on `main`, branch before committing. The team names branches
`<author>/<short-topic>` (e.g. `suyash/pinecone`, `Siddharth/auth`); `feature/<topic>`
is also accepted for shared work.

```bash
git rev-parse --abbrev-ref HEAD          # confirm current branch
git switch -c <author>/<short-topic>     # only if you're currently on main
```

Keep the topic short and kebab-case, and reuse your existing remote namespace if you
have one.

### 3. Stage intentionally

```bash
git add <specific paths>      # prefer explicit paths over "."
git status                    # verify nothing unexpected is staged
```

Scan the staged list for any `.env`, key file, data dump, or unrelated scratch file
before moving on.

### 4. Run the pre-flight checks (the CI gate)

CI runs ruff (advisory) and pytest (blocking) on every PR to `main`. Run them locally
first so the PR lands green:

```bash
ruff check .          # autofix the easy ones: ruff check . --fix
pytest tests/ -q
```

For a feature-complete change, also walk the **Feature Delivery Checklist in
`ENGINEERING_BLUEPRINT.md` §12** (layer boundaries respected, request/response schemas
added, async correctness, structured logs at milestones, unit + integration +
negative-path tests, migrations if the schema changed, docs updated).

### 5. Commit with a Conventional Commit message

This repo uses **Conventional Commits** with an optional scope:

```
<type>(<scope>): <imperative, lowercase summary>

<body: what changed and — more importantly — WHY. Wrap around 72 columns.>
```

- Write the subject in the imperative ("add", "fix", "remove"), not past tense.
- Prefer several small, atomic commits over one giant one; each should build and pass
  on its own.
- For multi-line messages, use repeated `-m` flags or `git commit -F <file>` to avoid
  cross-shell quoting pain (teammates are on macOS, Linux, and Windows/PowerShell).

When Claude Code authors the commit, end the message with the standard trailer:

```
Co-Authored-By: Claude <noreply@anthropic.com>
```

See the **Commit type reference** and **examples** below.

### 6. Push

```bash
git push -u origin HEAD       # creates a same-named upstream branch
```

Updating an existing PR branch after a rebase? Use `git push --force-with-lease` —
never plain `--force`, which can clobber a teammate's pushed work.

### 7. Open the PR with `gh`

Target `main`, give it a Conventional-Commits-style title, and write a body a reviewer
can act on. Use `--body-file` (or `-F -`) so multi-line bodies survive any shell:

```bash
gh pr create --base main \
  --title "<type>(<scope>): <summary>" \
  --body-file <path-to-body.md>
```

- Not authenticated? `gh auth status` to check, `gh auth login` to fix.
- No `gh` available? Push the branch and share the compare URL that `git push` prints,
  or `https://github.com/<org>/<repo>/compare/main...<branch>?expand=1`.
- End the PR body with:

  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  ```

### 8. Report back

Print the PR URL that `gh` returns so the user can open and share it.

## Commit type reference

| Type | Use for |
|---|---|
| `feat` | A new capability, endpoint, or behavior |
| `fix` | A bug fix |
| `chore` | Tooling, deps, config, non-behavioral cleanup |
| `docs` | Documentation only |
| `test` | Adding or fixing tests |
| `refactor` | Behavior-preserving restructuring |
| `style` | Formatting / UI styling with no logic change |
| `perf` | A performance improvement |

Scopes mirror the subsystem you touched: `weekly-plan`, `tutor`, `test-history`,
`quiz`, `spaced-repetition`, `auth`, `error-analysis`, etc. Omit the scope for
repo-wide changes.

**Good (real examples from this repo's history):**
- `feat(test-history): add GET /api/v1/tests/history JWT-scoped route`
- `fix(weekly-plan): decode task_payload_json once in get_plan_tasks`
- `chore: untrack stale pinecone log (scripts/logs already gitignored)`

**Avoid:**
- `update code` — no type, no information
- `fixed stuff` — past tense and vague
- `feat: WIP` — don't push work-in-progress noise into a reviewable PR

## PR body template

```markdown
## Summary
<1–3 sentences: what this PR does and why it exists.>

## Changes
- <key change 1>
- <key change 2>

## Test plan
- [ ] `ruff check .`
- [ ] `pytest tests/ -q`
- [ ] <manual verification, if any>

## Notes
<migrations, follow-ups, breaking changes, screenshots — or "none">
```

## When the change is bigger than one PR

If the diff spans unrelated concerns, stop and split it — one branch/PR per concern.
Small, focused PRs get reviewed faster and revert cleanly; a grab-bag PR is the most
common reason a review stalls. Stage and commit each concern separately, or branch off
and cherry-pick.
