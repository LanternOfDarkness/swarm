"""Git operations for the scheduler.

Reads go through :func:`gitscan.git`; writes use argv lists and never a shell.
Every failure raises ``ValueError`` with a one-line reason.  Only the writes
allowed by the plan live here: worktree add, merges (and aborts), worktree
remove, branch -d, and the pre-push guard hook.
"""

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from swarm import gitscan, util

GIT_TIMEOUT = 60
GUARD_MARKER = "# swarm-guard"
GUARD_SCRIPT = (
    "#!/bin/sh\n"
    "# swarm-guard\n"
    'if [ "$SWARM_WORKER" = "1" ]; then echo "swarm: pushes are blocked for '
    'workers" >&2; exit 1; fi\n'
)
SLUG_LIMIT = 40


def _field(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _repo(project) -> str:
    repo = _field(project, "repo_path")
    if not repo:
        raise ValueError("project has no repo_path")
    return str(repo)


def _branch(project) -> str:
    return _field(project, "integration_branch") or "main"


def _run(repo, *args, timeout=GIT_TIMEOUT) -> str:
    """Run a write git command in ``repo`` and return its stdout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"git {' '.join(args)}: timed out after {timeout} s"
        ) from None
    except OSError as exc:
        raise ValueError(f"git {' '.join(args)}: {exc.strerror or exc}") from None
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        detail = lines[0] if lines else f"exit {result.returncode}"
        raise ValueError(f"git {' '.join(args)}: {detail}")
    return result.stdout


def main_ready(project) -> str | None:
    """Return why the main checkout cannot take a merge, or ``None``."""
    repo = _repo(project)
    ib = _branch(project)
    try:
        head = gitscan.git(repo, "symbolic-ref", "--short", "HEAD").strip()
    except ValueError:
        head = "a detached HEAD"
    if head != ib:
        return f"main checkout is on {head}, not {ib}"
    dirty = gitscan.git(repo, "status", "--porcelain")
    count = len([line for line in dirty.splitlines() if line.strip()])
    if count:
        return f"main checkout has {count} uncommitted changes"
    return None


def worktree_path(project, ref) -> str:
    """Path of a plan worktree next to the main checkout."""
    repo = Path(_repo(project)).resolve()
    return str(repo.parent / f"{repo.name}-{ref}")


def slug(title) -> str:
    """Branch-safe slug: lower-case, punctuation collapsed to dashes."""
    text = re.sub(r"[^a-z0-9]+", "-", (title or "").lower())
    return text.strip("-")[:SLUG_LIMIT].strip("-")


def _branch_exists(repo, branch) -> bool:
    try:
        gitscan.git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except ValueError:
        return False
    return True


def create_worktree(project, plan):
    """Create the plan worktree; return ``(path, branch, base_sha)``."""
    repo = _repo(project)
    ib = _branch(project)
    ref = _field(plan, "ref")
    path = worktree_path(project, ref)
    branch = f"plan/{ref}-{slug(_field(plan, 'title'))}"
    if Path(path).exists():
        raise ValueError(f"worktree path exists: {path}")
    if _branch_exists(repo, branch):
        raise ValueError(f"branch exists: {branch}")
    base_sha = gitscan.git(repo, "rev-parse", ib).strip()
    _run(repo, "worktree", "add", "-b", branch, path, ib)
    return path, branch, base_sha


def update_branch(project, plan) -> str:
    """Merge the integration branch into the plan worktree; return its sha."""
    ib = _branch(project)
    worktree = _field(plan, "worktree")
    if not worktree:
        raise ValueError("plan has no worktree")
    ib_sha = gitscan.git(worktree, "rev-parse", ib).strip()
    try:
        _run(worktree, "merge", "--no-edit", ib)
    except ValueError:
        try:
            unmerged = gitscan.git(
                worktree, "diff", "--name-only", "--diff-filter=U"
            )
        except ValueError:
            unmerged = ""
        names = [line.strip() for line in unmerged.splitlines() if line.strip()]
        files = ", ".join(names) if names else "unknown files"
        try:
            _run(worktree, "merge", "--abort")
        except ValueError:
            pass
        raise ValueError(f"merge conflict with {ib}: {files}") from None
    return ib_sha


def merge_into_main(project, plan, expected_ib_sha) -> str:
    """Merge the plan branch into the integration branch; return merge sha."""
    repo = _repo(project)
    ib = _branch(project)
    reason = main_ready(project)
    if reason:
        raise ValueError(reason)
    head = gitscan.git(repo, "rev-parse", ib).strip()
    if head != expected_ib_sha:
        raise ValueError(f"{ib} moved")
    branch = _field(plan, "branch")
    if not branch:
        raise ValueError("plan has no branch")
    message = f"Merge {branch} (plan {_field(plan, 'ref')})"
    try:
        _run(repo, "merge", "--no-ff", "--no-edit", "-m", message, branch)
    except ValueError as exc:
        try:
            _run(repo, "merge", "--abort")
        except ValueError:
            pass
        raise ValueError(f"merge of {branch} failed: {exc}") from None
    return gitscan.git(repo, "rev-parse", "HEAD").strip()


def cleanup(project, plan):
    """Remove the plan's target dir, worktree and branch."""
    repo = _repo(project)
    ref = _field(plan, "ref")
    expected = worktree_path(project, ref)
    worktree = _field(plan, "worktree")
    if not worktree or str(worktree) != expected:
        raise ValueError(f"refusing to clean up foreign worktree: {worktree}")
    branch = _field(plan, "branch")
    target = Path(worktree) / "target"
    if target.exists():
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise ValueError(
                f"cannot remove {target}: {exc.strerror or exc}"
            ) from None
    _run(repo, "worktree", "remove", worktree)
    if not branch:
        raise ValueError("plan has no branch")
    _run(repo, "branch", "-d", branch)


def _hook_path(project) -> Path:
    repo = Path(_repo(project)).resolve()
    common = gitscan.git(repo, "rev-parse", "--git-common-dir").strip()
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = repo / common_dir
    return (common_dir / "hooks" / "pre-push").resolve()


def install_guard(project) -> str:
    """Install the pre-push hook that blocks worker pushes."""
    hook = _hook_path(project)
    if hook.exists():
        text = hook.read_text(errors="replace")
        if not any(line.strip() == GUARD_MARKER for line in text.splitlines()):
            raise ValueError(f"a different pre-push hook exists: {hook}")
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(GUARD_SCRIPT)
    hook.chmod(0o755)
    return str(hook)


def remove_guard(project) -> bool:
    """Remove the guard hook if it is ours; leave foreign hooks alone."""
    hook = _hook_path(project)
    if not hook.exists():
        return False
    text = hook.read_text(errors="replace")
    if not any(line.strip() == GUARD_MARKER for line in text.splitlines()):
        return False
    hook.unlink()
    return True


def guard_installed(project) -> bool:
    """True when the guard hook is installed and would actually run."""
    repo = Path(_repo(project)).resolve()
    try:
        hook = _hook_path(project)
    except ValueError:
        return False
    common_hooks = hook.parent
    try:
        hooks_path = gitscan.git(repo, "config", "--get", "core.hooksPath").strip()
    except ValueError:
        hooks_path = ""
    if hooks_path:
        configured = Path(hooks_path)
        if not configured.is_absolute():
            configured = repo / configured
        if configured.resolve() != common_hooks:
            return False
    if not hook.is_file():
        return False
    text = hook.read_text(errors="replace")
    return any(line.strip() == GUARD_MARKER for line in text.splitlines())


def plan_commit(project, path, since_iso) -> str | None:
    """Newest commit touching ``path`` dated after ``since_iso``, if any."""
    repo = _repo(project)
    out = gitscan.git(
        repo, "log", "-1", "--format=%H%x00%cI", "--", str(path)
    ).strip()
    if not out:
        return None
    sha, _, when = out.partition("\x00")
    try:
        committed = datetime.fromisoformat(when.strip())
    except ValueError:
        return None
    if committed.tzinfo is None:
        committed = committed.replace(tzinfo=timezone.utc)
    if committed <= util.parse_ts(since_iso):
        return None
    return sha


def commit_files(project, sha) -> list[str]:
    """Files changed by a commit."""
    repo = _repo(project)
    out = gitscan.git(repo, "show", "--pretty=format:", "--name-only", sha)
    return [line.strip() for line in out.splitlines() if line.strip()]


def new_commits(project, base_sha, branch) -> int:
    """Number of commits on ``branch`` not in ``base_sha``."""
    repo = _repo(project)
    out = gitscan.git(repo, "rev-list", "--count", f"{base_sha}..{branch}")
    return int(out.strip())


def ib_head(project) -> str:
    """Current integration branch commit in the main checkout."""
    repo = _repo(project)
    return gitscan.git(repo, "rev-parse", _branch(project)).strip()
