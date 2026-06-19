"""Read-only release pre-flight gate for the APGuru Grader.

Runs the checks that decide whether `main` is safe to tag and deploy, and prints
a GO / REVIEW summary. It changes nothing — no commits, no tags, no pushes — so
it is safe to run anytime while deciding on a release.

What it checks (blocking checks gate the exit code):
  - on `main` and not behind `origin/main`            (blocking: the deploy ships main)
  - working tree clean                                 (advisory)
  - `pytest tests/` passes                             (blocking; skip with --skip-tests)
  - `ruff check .`                                      (advisory; skip with --skip-ruff)
  - commits since the last tag + a suggested SemVer bump
  - in-app FastAPI version vs. the latest tag

Run it with the project's venv interpreter so it exercises the same pytest /
ruff the deploy will:

    venv/Scripts/python.exe .claude/skills/cut-release/scripts/release_preflight.py
    # macOS/Linux: venv/bin/python .claude/skills/cut-release/scripts/release_preflight.py

Database migrations are NOT part of this repo or this check — they live in the
central `apguru-centralized-alembic` repo, whose own CI/CD applies them to prod.
The grader app does not migrate on boot, so there is no chain to validate here.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}

# (status, headline, detail) accumulated as checks run.
_results: list[tuple[str, str, str]] = []


def record(status: str, headline: str, detail: str = "") -> None:
    _results.append((status, headline, detail))
    line = f"  {_MARK[status]} {headline}"
    print(line)
    if detail:
        for d in detail.splitlines():
            print(f"         {d}")


def run(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command, returning (returncode, combined stdout+stderr)."""
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def repo_root() -> Path:
    code, out = run(["git", "rev-parse", "--show-toplevel"], Path.cwd())
    if code != 0:
        sys.exit("not inside a git repository")
    return Path(out.strip())


# --- individual checks -------------------------------------------------------

def check_branch_and_sync(root: Path) -> None:
    _, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = branch.strip()
    if branch == "main":
        record(OK, "on branch main")
    else:
        record(WARN, f"on branch {branch!r}, not main",
               "Releases ship `main`. Switch to main (git switch main) before tagging.")

    # Best-effort fetch so the ahead/behind comparison is current.
    code, _ = run(["git", "fetch", "origin", "main", "--quiet"], root)
    if code != 0:
        record(WARN, "could not fetch origin/main", "Offline? ahead/behind may be stale.")
        return

    code, out = run(
        ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"], root
    )
    if code != 0:
        record(WARN, "could not compare with origin/main", out.strip())
        return
    ahead, behind = (out.split() + ["0", "0"])[:2]
    if behind != "0":
        record(FAIL, f"HEAD is {behind} commit(s) behind origin/main",
               "Run `git pull` - the deploy ships what's on origin/main.")
    elif ahead != "0":
        record(WARN, f"HEAD is {ahead} commit(s) ahead of origin/main",
               "Unpushed commits won't be in the release unless merged via PR first.")
    else:
        record(OK, "in sync with origin/main")


def check_clean_tree(root: Path) -> None:
    _, out = run(["git", "status", "--porcelain"], root)
    if out.strip():
        n = len(out.strip().splitlines())
        record(WARN, f"working tree has {n} uncommitted change(s)",
               "Releases build from the tagged commit, not your tree - but tidy up to avoid confusion.")
    else:
        record(OK, "working tree clean")


def check_pytest(root: Path) -> None:
    code, out = run([sys.executable, "-m", "pytest", "tests/", "-q"], root)
    summary = ""
    # Match the pytest summary line ("41 passed, 1 warning in 2.69s") and skip
    # unrelated noise like the langfuse "--- Logging error ---" teardown lines,
    # which contain "error" but no leading count.
    for ln in reversed(out.splitlines()):
        if re.search(r"\d+\s+(passed|failed|error)", ln):
            summary = ln.strip()
            break
    if code == 0:
        record(OK, "pytest tests/ passed", summary)
    else:
        record(FAIL, "pytest tests/ failed", summary or out.strip()[-400:])


def check_ruff(root: Path) -> None:
    code, out = run([sys.executable, "-m", "ruff", "check", "."], root)
    last = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
    if code == 0:
        record(OK, "ruff check . clean")
    else:
        record(WARN, "ruff check . reported lints (advisory)",
               f"{last}\nThis repo carries some known lints; not a release blocker.")


def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)$", tag.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def check_version_and_suggest(root: Path) -> None:
    # Latest tag by SemVer order.
    _, tags_out = run(["git", "tag", "--list", "--sort=-v:refname"], root)
    tags = [t for t in tags_out.splitlines() if t.strip()]
    last_tag = tags[0].strip() if tags else None

    # In-app version from app/main.py.
    main_py = root / "app" / "main.py"
    in_app = None
    if main_py.exists():
        m = re.search(r'version\s*=\s*"([^"]+)"', main_py.read_text(encoding="utf-8"))
        in_app = m[1] if m else None

    if last_tag and in_app:
        same = _parse_semver(last_tag) == _parse_semver(in_app)
        record(OK if same else WARN,
               f"in-app version {in_app!r} vs latest tag {last_tag!r}"
               + ("" if same else "  <- out of sync"),
               "" if same else "Bump app/main.py version= to match the tag you publish.")
    elif last_tag:
        record(WARN, f"latest tag {last_tag!r}; couldn't read in-app version", "")
    else:
        record(WARN, "no tags yet", "First release — suggest starting at v1.0.0.")

    # Commits since the last tag + suggested bump.
    rng = f"{last_tag}..HEAD" if last_tag else "HEAD"
    _, log_out = run(["git", "log", rng, "--oneline", "--no-merges"], root)
    commits = [c for c in log_out.splitlines() if c.strip()]
    print()
    print(f"  commits since {last_tag or '(repo start)'}: {len(commits)}")
    for c in commits[:20]:
        print(f"         {c}")
    if len(commits) > 20:
        print(f"         ... and {len(commits) - 20} more")

    base = _parse_semver(last_tag) if last_tag else (0, 0, 0)
    if base is None:
        return
    subjects = [c.split(" ", 1)[1] if " " in c else c for c in commits]
    breaking = any("!:" in s or s.lower().startswith("breaking") for s in subjects)
    feat = any(re.match(r"feat(\(|!|:)", s) for s in subjects)
    maj, minr, pat = base
    if breaking:
        nxt = f"v{maj + 1}.0.0"
        why = "a breaking change is present"
    elif feat:
        nxt = f"v{maj}.{minr + 1}.0"
        why = "new feat: commits are present"
    else:
        nxt = f"v{maj}.{minr}.{pat + 1}"
        why = "only fixes/chores since the last tag"
    print()
    print(f"  suggested next version: {nxt}   ({why} - confirm with the maintainer)")


# --- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Release pre-flight gate (read-only).")
    ap.add_argument("--skip-tests", action="store_true", help="skip the pytest run")
    ap.add_argument("--skip-ruff", action="store_true", help="skip the ruff run")
    args = ap.parse_args()

    root = repo_root()
    print("=" * 70)
    print(f"RELEASE PRE-FLIGHT - {root}")
    print("=" * 70)

    print("\nGit state")
    check_branch_and_sync(root)
    check_clean_tree(root)

    print("\nTests / lint")
    if args.skip_tests:
        record(WARN, "pytest skipped (--skip-tests)")
    else:
        check_pytest(root)
    if args.skip_ruff:
        record(WARN, "ruff skipped (--skip-ruff)")
    else:
        check_ruff(root)

    print("\nVersion")
    check_version_and_suggest(root)

    fails = [h for s, h, _ in _results if s == FAIL]
    warns = [h for s, h, _ in _results if s == WARN]
    print("\n" + "=" * 70)
    if fails:
        print(f"VERDICT: NOT READY - {len(fails)} blocking issue(s):")
        for h in fails:
            print(f"  - {h}")
    else:
        print("VERDICT: GO - no blocking issues." + (f"  ({len(warns)} warning(s) to eyeball.)" if warns else ""))
    print("Reminder: get explicit go-ahead before `gh release create` — publishing")
    print("deploys to production. (Schema migrations live in apguru-centralized-alembic,")
    print("a separate repo, and are NOT applied by this release.)")
    print("=" * 70)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
