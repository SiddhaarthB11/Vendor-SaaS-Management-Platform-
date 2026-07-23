"""Git setup for autofix: local identity, sane staging, baseline commits."""

from __future__ import annotations

import subprocess
from pathlib import Path

# Never stage node_modules, .next, __pycache__, or devtools/state job artifacts.
STAGE_PATHS: tuple[str, ...] = (
    "api",
    "devtools",
    "ui/app",
    "ui/public",
    "ui/package.json",
    "ui/package-lock.json",
    "ui/tsconfig.json",
    "ui/next.config.ts",
    "ui/next.config.js",
    "ui/next.config.mjs",
    "infra",
    ".gitignore",
)

DEFAULT_IDENTITY: dict[str, str] = {
    "user.name": "Derisk360 Autofix",
    "user.email": "autofix@derisk360.local",
}


def run_git(repo_root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=check,
    )


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def ensure_local_identity(repo_root: Path) -> None:
    """Set repo-local git identity when missing (does not touch global config)."""
    for key, value in DEFAULT_IDENTITY.items():
        current = run_git(repo_root, ["config", "--get", key], check=False)
        if not (current.stdout or "").strip():
            run_git(repo_root, ["config", key, value])


def ensure_git_repo(repo_root: Path) -> None:
    if (repo_root / ".git").is_dir():
        return
    run_git(repo_root, ["init"])
    run_git(repo_root, ["checkout", "-B", "main"], check=False)


def normalize_main_branch(repo_root: Path) -> str:
    """Ensure a sensible default branch name; return current branch."""
    branch = run_git(repo_root, ["branch", "--show-current"], check=False).stdout.strip()
    if branch in ("", "master"):
        run_git(repo_root, ["branch", "-M", "main"], check=False)
        return "main"
    return branch or "main"


def unstage_all(repo_root: Path) -> None:
    run_git(repo_root, ["reset"], check=False)


def stage_source_tree(repo_root: Path) -> None:
    """Stage application source only — respects .gitignore, skips node_modules."""
    unstage_all(repo_root)
    for rel in STAGE_PATHS:
        if (repo_root / rel).exists():
            run_git(repo_root, ["add", "--", rel], check=False)
    for md in sorted(repo_root.glob("*.md")):
        run_git(repo_root, ["add", "--", md.name], check=False)


def stage_autofix_changes(repo_root: Path) -> None:
    """Stage edits under api/, devtools/, and ui/app/ for fix commits."""
    for rel in ("api", "devtools", "ui/app"):
        if (repo_root / rel).exists():
            run_git(repo_root, ["add", "-u", "--", rel], check=False)
            run_git(repo_root, ["add", "--", rel], check=False)


def has_commits(repo_root: Path) -> bool:
    return run_git(repo_root, ["rev-parse", "HEAD"], check=False).returncode == 0


def has_changes(repo_root: Path) -> bool:
    return bool(run_git(repo_root, ["status", "--porcelain"], check=False).stdout.strip())


def default_diff_base(repo_root: Path) -> str:
    for name in ("main", "master"):
        if run_git(repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], check=False).returncode == 0:
            return name
    return "HEAD"


def ensure_baseline_commit(repo_root: Path) -> tuple[bool, str]:
    """
    Create an initial commit if the repo has none.
    Clears bad indexes from prior failed runs (e.g. staged node_modules).
    """
    ensure_git_repo(repo_root)
    ensure_local_identity(repo_root)
    normalize_main_branch(repo_root)

    if has_commits(repo_root):
        return True, ""

    stage_source_tree(repo_root)
    if has_changes(repo_root):
        try:
            run_git(repo_root, ["commit", "-m", "autofix baseline"])
            return True, ""
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "").strip()
            if "nothing to commit" not in err.lower():
                pass
            else:
                return False, err or "baseline commit failed"

    try:
        run_git(repo_root, ["commit", "--allow-empty", "-m", "autofix baseline"])
        return True, ""
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        return False, err or "baseline commit failed"


def commit_changes(repo_root: Path, message: str) -> tuple[bool, str]:
    stage_autofix_changes(repo_root)
    if not has_changes(repo_root):
        return False, "nothing to commit"
    try:
        run_git(repo_root, ["commit", "-m", message])
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or exc.stdout or str(exc)).strip()


def branch_exists(repo_root: Path, branch: str) -> bool:
    return run_git(repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0


def current_branch(repo_root: Path) -> str:
    return run_git(repo_root, ["branch", "--show-current"], check=False).stdout.strip()


def diff_stat(repo_root: Path, base: str, branch: str) -> str:
    return run_git(repo_root, ["diff", f"{base}...{branch}", "--stat"], check=False).stdout.strip()


def merge_branch_to_main(
    repo_root: Path,
    branch: str,
    *,
    main_branch: str = "main",
    delete_branch_after: bool = True,
) -> tuple[bool, str]:
    """Merge an autofix branch into main. This is the human-approval action taken
    from the control panel — it never runs automatically as part of the autofix
    loop itself, only in response to an explicit user click.

    Refuses to merge if the working tree has uncommitted changes (would silently
    mix unrelated edits into the merge) or if the branch doesn't exist.
    """
    if not branch_exists(repo_root, branch):
        return False, f"branch '{branch}' does not exist"
    if has_changes(repo_root):
        return False, "working tree has uncommitted changes — commit or discard them before merging"

    started_on = current_branch(repo_root)
    try:
        checkout = run_git(repo_root, ["checkout", main_branch], check=False)
        if checkout.returncode != 0:
            return False, (checkout.stderr or checkout.stdout or f"could not check out {main_branch}").strip()

        merge = run_git(repo_root, ["merge", "--no-ff", branch, "-m", f"Merge autofix branch {branch}"], check=False)
        if merge.returncode != 0:
            # Leave the failed merge state for manual resolution rather than
            # guessing — but return to the branch we started on so the panel's
            # view of "current branch" doesn't silently change on failure.
            run_git(repo_root, ["merge", "--abort"], check=False)
            run_git(repo_root, ["checkout", started_on], check=False)
            return False, (merge.stderr or merge.stdout or "merge failed").strip()

        if delete_branch_after:
            run_git(repo_root, ["branch", "-d", branch], check=False)

        return True, f"merged {branch} into {main_branch}"
    except Exception as exc:  # noqa: BLE001 - surfaced to the panel as a plain message
        run_git(repo_root, ["checkout", started_on], check=False)
        return False, str(exc)
