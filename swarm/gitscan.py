"""Read-only git scan: worktrees and loose branches, stored as a board snapshot."""

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from swarm import util

GIT_TIMEOUT = 20
DU_TIMEOUT = 60
REF_PREFIX = "refs/heads/"
PLAN_NUMBER = re.compile(r"(?:^|[/-])(\d{4})(?=$|[/-])")
CLEANUP_PLAN_STATES = frozenset({"MERGED", "MEASURING", "DONE", "DROPPED"})


def _utc(value):
    """Convert a git ISO-8601 timestamp to the swarm UTC format."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).strftime(util.TS_FORMAT)


def git(repo, *args, timeout=GIT_TIMEOUT) -> str:
    """Run a read-only git command in ``repo`` and return its stdout."""
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
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        detail = lines[0] if lines else f"exit {result.returncode}"
        raise ValueError(f"git {' '.join(args)}: {detail}")
    return result.stdout


def parse_worktree_porcelain(text) -> list[dict]:
    """Parse ``git worktree list --porcelain`` output into worktree dicts."""
    blocks = []
    current = None
    for line in text.splitlines():
        if not line.strip():
            if current is not None:
                blocks.append(current)
                current = None
            continue
        if current is None:
            current = {"path": None, "head": None, "branch": None,
                       "prunable": False, "bare": False}
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            current["branch"] = ref[len(REF_PREFIX):] if ref.startswith(REF_PREFIX) else ref
        elif line == "detached":
            current["branch"] = None
        elif line == "bare" or line.startswith("bare "):
            current["bare"] = True
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
    if current is not None:
        blocks.append(current)

    worktrees = []
    for block in blocks:
        if block["bare"] or block["path"] is None:
            continue
        worktrees.append({
            "path": block["path"],
            "head": block["head"],
            "branch": block["branch"],
            "is_main": not worktrees,
            "prunable": block["prunable"],
        })
    return worktrees


def _plan_by_branch(conn, project, branch):
    row = conn.execute(
        "SELECT ref FROM plans WHERE project = ? AND branch = ? ORDER BY id LIMIT 1",
        (project, branch),
    ).fetchone()
    if row is not None:
        return row["ref"]
    match = PLAN_NUMBER.search(branch)
    if match is None:
        return None
    row = conn.execute(
        "SELECT ref FROM plans WHERE project = ? AND ref = ?", (project, match.group(1))
    ).fetchone()
    return row["ref"] if row is not None else None


def link_plan(conn, project, path, branch):
    """Return the plan ref for a worktree path/branch, or None. Never writes."""
    target = os.path.realpath(path)
    rows = conn.execute(
        "SELECT ref, worktree FROM plans "
        "WHERE project = ? AND worktree IS NOT NULL ORDER BY id",
        (project,),
    ).fetchall()
    for row in rows:
        if os.path.realpath(row["worktree"]) == target:
            return row["ref"]
    if not branch:
        return None
    return _plan_by_branch(conn, project, branch)


def target_size(path):
    """Return ``<path>/target`` size in bytes via ``du``, or None."""
    target = Path(path) / "target"
    if not target.is_dir():
        return None
    try:
        result = subprocess.run(
            ["du", "-sk", str(target)],
            capture_output=True,
            text=True,
            timeout=DU_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.split()
    if not fields:
        return None
    try:
        kib = int(fields[0])
    except ValueError:
        return None
    return kib * 1024


def plan_states(conn, project=None) -> dict:
    """Return ``{(project, ref): state}`` for the board's plans."""
    sql = "SELECT project, ref, state FROM plans"
    params = []
    if project is not None:
        sql += " WHERE project = ?"
        params.append(project)
    return {
        (row["project"], row["ref"]): row["state"]
        for row in conn.execute(sql, params)
    }


def _field(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def cleanup_candidate(row, plan_state) -> bool:
    """True when a worktree is merged, clean and its linked plan is finished."""
    if _field(row, "is_main"):
        return False
    if _field(row, "dirty") != 0:
        return False
    if _field(row, "merged") != 1:
        return False
    if not _field(row, "plan_ref"):
        return False
    return plan_state in CLEANUP_PLAN_STATES


def branch_deletable(row, plan_state) -> bool:
    """True when a merged branch is unlinked or its linked plan is finished."""
    if _field(row, "merged") != 1:
        return False
    if not _field(row, "plan_ref"):
        return True
    return plan_state in CLEANUP_PLAN_STATES


def worktree_status(row, plan_state) -> str:
    """Return the badge for a scanned worktree: missing, main, cleanup, merged, active."""
    if _field(row, "missing"):
        return "missing"
    if _field(row, "is_main"):
        return "main"
    if cleanup_candidate(row, plan_state):
        return "cleanup"
    if _field(row, "plan_ref"):
        return "active"
    if _field(row, "merged") == 1 and _field(row, "dirty") == 0:
        return "merged"
    return "active"


def _branch_exists(repo, branch) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
             f"{REF_PREFIX}{branch}"],
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"git rev-parse --verify --quiet {REF_PREFIX}{branch}: "
            f"timed out after {GIT_TIMEOUT} s"
        ) from None
    return result.returncode == 0


def _divergence(repo, integration_branch, head):
    """Return (behind, ahead) of ``head`` relative to the integration branch."""
    out = git(repo, "rev-list", "--left-right", "--count",
              f"{integration_branch}...{head}")
    fields = out.split()
    return int(fields[0]), int(fields[1])


def _is_merged(repo, head, integration_branch):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", head,
             integration_branch],
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"git merge-base --is-ancestor {head} {integration_branch}: "
            f"timed out after {GIT_TIMEOUT} s"
        ) from None
    if result.returncode == 0:
        return 1
    if result.returncode == 1:
        return 0
    raise ValueError(
        f"git merge-base --is-ancestor {head} {integration_branch}: "
        f"exit {result.returncode}"
    )


def _dirty_count(path):
    return len(git(path, "status", "--porcelain").splitlines())


def _last_commit(repo, head):
    return _utc(git(repo, "log", "-1", "--format=%cI", head))


def _previous_sizes(conn, project) -> dict:
    return {
        row["path"]: row["target_bytes"]
        for row in conn.execute(
            "SELECT path, target_bytes FROM worktrees WHERE project = ?", (project,)
        )
    }


def _loose_branches(repo, integration_branch, checked_out):
    out = git(repo, "for-each-ref",
              "--format=%(refname:short)%09%(objectname)%09%(committerdate:iso-strict)",
              "refs/heads")
    branches = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        name, head, committed = fields[0], fields[1], fields[2]
        if name == integration_branch or name in checked_out:
            continue
        branches.append((name, head, committed))
    return branches


def scan_project(conn, project, sizes=True) -> dict:
    """Scan a project's worktrees and branches, store the snapshot, return it."""
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (project,)).fetchone()
    if row is None:
        raise LookupError(f"no project {project}")
    repo = row["repo_path"]
    if not Path(repo).is_dir():
        raise ValueError(f"{project}: repo {repo} not found")
    integration_branch = row["integration_branch"]
    scanned_at = util.now()

    worktrees = parse_worktree_porcelain(
        git(repo, "worktree", "list", "--porcelain")
    )
    has_ib = _branch_exists(repo, integration_branch)
    previous = {} if sizes else _previous_sizes(conn, project)

    worktree_rows = []
    for tree in worktrees:
        path = tree["path"]
        head = tree["head"] or ""
        branch = tree["branch"]
        missing = tree["prunable"] or not Path(path).is_dir()

        behind = ahead = None
        merged = 0
        if has_ib:
            behind, ahead = _divergence(repo, integration_branch, head)
            merged = _is_merged(repo, head, integration_branch)

        if missing:
            dirty = None
            target_bytes = None
            last_commit_at = None
        else:
            dirty = _dirty_count(path)
            target_bytes = target_size(path) if sizes else previous.get(path)
            last_commit_at = _last_commit(repo, head)

        worktree_rows.append({
            "project": project,
            "path": path,
            "branch": branch,
            "head": head,
            "is_main": 1 if tree["is_main"] else 0,
            "ahead": ahead,
            "behind": behind,
            "merged": merged,
            "dirty": dirty,
            "target_bytes": target_bytes,
            "last_commit_at": last_commit_at,
            "plan_ref": link_plan(conn, project, path, branch),
            "missing": 1 if missing else 0,
            "scanned_at": scanned_at,
        })

    checked_out = {tree["branch"] for tree in worktrees if tree["branch"]}
    branch_rows = []
    for name, head, committed in _loose_branches(
        repo, integration_branch, checked_out
    ):
        ahead = None
        merged = 0
        if has_ib:
            _, ahead = _divergence(repo, integration_branch, head)
            merged = _is_merged(repo, head, integration_branch)
        branch_rows.append({
            "project": project,
            "name": name,
            "head": head,
            "merged": merged,
            "ahead": ahead,
            "last_commit_at": _utc(committed),
            "plan_ref": _plan_by_branch(conn, project, name),
            "scanned_at": scanned_at,
        })

    total_bytes = sum(
        entry["target_bytes"] for entry in worktree_rows
        if entry["target_bytes"] is not None
    )
    states = plan_states(conn, project)
    cleanup_count = sum(
        1 for entry in worktree_rows
        if cleanup_candidate(entry, states.get((project, entry["plan_ref"])))
    )
    merged_count = sum(1 for entry in branch_rows if entry["merged"] == 1)
    unmerged_count = sum(1 for entry in branch_rows if entry["merged"] == 0)
    worktree_count = len(worktree_rows)
    text = (
        f"{project}: {worktree_count} worktrees ({cleanup_count} cleanup), "
        f"{unmerged_count} unmerged branches, {merged_count} merged branches, "
        f"targets {total_bytes / 1e9:.1f} GB"
    )

    with conn:
        conn.execute("DELETE FROM worktrees WHERE project = ?", (project,))
        conn.execute("DELETE FROM branches WHERE project = ?", (project,))
        conn.executemany(
            "INSERT INTO worktrees (project, path, branch, head, is_main, ahead, "
            "behind, merged, dirty, target_bytes, last_commit_at, plan_ref, missing, "
            "scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                tuple(entry[column] for column in (
                    "project", "path", "branch", "head", "is_main", "ahead", "behind",
                    "merged", "dirty", "target_bytes", "last_commit_at", "plan_ref",
                    "missing", "scanned_at",
                ))
                for entry in worktree_rows
            ],
        )
        conn.executemany(
            "INSERT INTO branches (project, name, head, merged, ahead, "
            "last_commit_at, plan_ref, scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                tuple(entry[column] for column in (
                    "project", "name", "head", "merged", "ahead", "last_commit_at",
                    "plan_ref", "scanned_at",
                ))
                for entry in branch_rows
            ],
        )
        conn.execute(
            "INSERT INTO events (ts, project, kind, text) VALUES (?, ?, ?, ?)",
            (scanned_at, project, "scan", text),
        )

    return {
        "text": text,
        "worktrees": worktree_rows,
        "branches": branch_rows,
        "worktree_count": worktree_count,
        "cleanup_count": cleanup_count,
        "unmerged_count": unmerged_count,
        "merged_count": merged_count,
        "targets_gb": total_bytes / 1e9,
    }
