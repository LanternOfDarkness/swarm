"""Write the board status line into a plan markdown file."""

from pathlib import Path

from swarm import db, util

OPEN_MARKER = "<!-- swarm:status -->"
CLOSE_MARKER = "<!-- /swarm:status -->"


def _get(row, key, default=None):
    try:
        keys = row.keys()
    except AttributeError:
        keys = None
    if keys is not None:
        return row[key] if key in keys else default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def status_line(plan_row) -> str:
    state = _get(plan_row, "state") or "DRAFT"
    parts = [str(state)]

    branch = _get(plan_row, "branch")
    if branch is not None and str(branch).strip():
        parts.append(f"branch {branch}")

    try:
        fix_rounds = int(_get(plan_row, "fix_rounds") or 0)
    except (TypeError, ValueError):
        fix_rounds = 0
    if fix_rounds > 0:
        parts.append(f"fix rounds {fix_rounds}")

    updated = _get(plan_row, "updated_at") or util.now()
    parts.append(f"updated {updated}")

    return "**Swarm status:** " + " · ".join(parts)


def _eol(text: str) -> str:
    idx = text.find("\n")
    if idx > 0 and text[idx - 1] == "\r":
        return "\r\n"
    return "\n"


def _block(eol: str, line: str) -> str:
    return f"{OPEN_MARKER}{eol}{line}{eol}{CLOSE_MARKER}{eol}"


def _spans(lines):
    spans = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == OPEN_MARKER:
            j = i + 1
            while j < len(lines) and lines[j].strip() != CLOSE_MARKER:
                j += 1
            if j < len(lines):
                spans.append((i, j + 1))
                i = j + 1
                continue
        i += 1
    return spans


def _embed(text: str, line: str) -> str:
    if not text:
        return _block("\n", line)

    eol = _eol(text)
    lines = text.splitlines(keepends=True)
    spans = _spans(lines)
    block = _block(eol, line)

    if spans:
        first = spans[0][0]
        parts = []
        cursor = 0
        for start, end in spans:
            parts.append("".join(lines[cursor:start]))
            if start == first:
                parts.append(block)
            cursor = end
        parts.append("".join(lines[cursor:]))
        return "".join(parts)

    for n, ln in enumerate(lines):
        if ln.startswith("# "):
            prefix = "".join(lines[: n + 1])
            if not prefix.endswith(("\n", "\r")):
                prefix += eol
            return prefix + block + "".join(lines[n + 1 :])

    return block + text


def sync(conn, project: str, ref: str) -> Path:
    row = conn.execute(
        "SELECT * FROM plans WHERE project=? AND ref=?", (project, ref)
    ).fetchone()
    if row is None:
        raise LookupError(f"no plan {project}/{ref}")

    proj = conn.execute(
        "SELECT * FROM projects WHERE name=?", (project,)
    ).fetchone()
    if proj is None:
        raise LookupError(f"no project {project}")

    rel = _get(row, "path")
    if not rel or not str(rel).strip():
        raise ValueError(f"plan {project}/{ref} has no path")

    repo_path = _get(proj, "repo_path")
    if not repo_path:
        raise ValueError(f"project {project} has no repo_path")

    path = Path(repo_path) / str(rel)
    if not path.is_file():
        raise FileNotFoundError(f"plan file missing: {path}")

    with path.open("r", encoding="utf-8", newline="") as f:
        text = f.read()
    updated = _embed(text, status_line(row))
    if updated != text:
        path.write_text(updated, encoding="utf-8", newline="")

    with conn:
        db.add_event(
            conn,
            "sync",
            f"{project}/{ref} status synced to {path}",
            project=project,
            plan_id=_get(row, "id"),
        )

    return path
