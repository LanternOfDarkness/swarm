"""Detect a git repo's swarm settings and connect it to the board."""

import json
import re
import sys
from pathlib import Path

from swarm import board, config, dashboard, gitops, gitscan, mdsync, states

PLAN_FILE = re.compile(r"^(\d{3,5})-[^/]+\.md$")
STATUS_RE = re.compile(r"^\*\*Swarm status:\*\*\s+([A-Z][A-Z_]*)\b")
NAME_CHARS = re.compile(r"[^a-z0-9-]+")

VERIFY_RULES = (
    ("Cargo.toml", "cargo check --workspace --all-targets && cargo test --workspace",
     "Bash(cargo check:*),Bash(cargo test:*),Bash(cargo nextest run:*)"),
    ("go.mod", "go vet ./... && go test ./...",
     "Bash(go vet:*),Bash(go test:*)"),
    ("package.json", "npm test",
     "Bash(npm test:*),Bash(npm run test:*)"),
)

PYTEST_FILES = ("pyproject.toml", "setup.py", "pytest.ini", "setup.cfg")


def _default_name(repo) -> str:
    base = Path(repo).name.lower()
    return NAME_CHARS.sub("-", base).strip("-")


def _verify(root: Path):
    if (root / "Cargo.toml").is_file():
        return VERIFY_RULES[0][1], VERIFY_RULES[0][2]
    if (root / "go.mod").is_file():
        return VERIFY_RULES[1][1], VERIFY_RULES[1][2]
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(errors="replace"))
        except (OSError, ValueError):
            data = None
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and scripts.get("test"):
            return VERIFY_RULES[2][1], VERIFY_RULES[2][2]
    for name in PYTEST_FILES:
        path = root / name
        if path.is_file():
            try:
                if "pytest" in path.read_text(errors="replace"):
                    return (
                        "python3 -m pytest -q",
                        "Bash(python3 -m pytest:*),Bash(pytest:*)",
                    )
            except OSError:
                continue
    tests = root / "tests"
    if tests.is_dir() and any(tests.glob("test_*.py")):
        return (
            "python3 -m unittest discover -s tests -v",
            "Bash(python3 -m unittest:*)",
        )
    return None, None


def _status_state(text) -> str | None:
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == mdsync.OPEN_MARKER:
            inside = True
            continue
        if stripped == mdsync.CLOSE_MARKER:
            inside = False
            continue
        if not inside:
            continue
        match = STATUS_RE.match(stripped)
        if match and match.group(1) in states.PLAN_STATES:
            return match.group(1)
    return None


def _title(text, ref, stem) -> str:
    for line in text.splitlines():
        if not line.startswith("# "):
            continue
        title = line[2:].strip()
        if title.startswith(ref):
            title = title[len(ref):]
        for sep in (" — ", " - ", ": "):
            if title.startswith(sep):
                title = title[len(sep):]
                break
        title = title.strip()
        if title:
            return title
        break
    fallback = stem
    prefix = ref + "-"
    if fallback.startswith(prefix):
        fallback = fallback[len(prefix):]
    return fallback.replace("-", " ")


def _plans(root: Path, plans_dir, exists: bool) -> list[dict]:
    if not exists:
        return []
    plans = []
    for path in sorted((root / plans_dir).iterdir()):
        if not path.is_file():
            continue
        match = PLAN_FILE.match(path.name)
        if not match:
            continue
        ref = match.group(1)
        text = path.read_text(errors="replace")
        plans.append(
            {
                "ref": ref,
                "path": f"{plans_dir}/{path.name}",
                "title": _title(text, ref, path.stem),
                "state": _status_state(text),
            }
        )
    plans.sort(key=lambda plan: int(plan["ref"]))
    return plans


def detect(path, name=None, branch=None) -> dict:
    """Read a git repo and return its detected swarm settings (no writes)."""
    try:
        repo = gitscan.git(path, "rev-parse", "--show-toplevel").strip()
    except (ValueError, OSError):
        raise ValueError(f"not a git repo: {path}") from None
    if not repo:
        raise ValueError(f"not a git repo: {path}")
    root = Path(repo)

    if name is None:
        name = _default_name(repo)
    if branch is None:
        try:
            branch = gitscan.git(repo, "symbolic-ref", "--short", "HEAD").strip()
        except ValueError:
            raise ValueError("detached HEAD: pass --branch") from None

    plans_dir = "docs/plans"
    plans_dir_exists = False
    for candidate in ("docs/plans", "plans", ".swarm/plans"):
        if (root / candidate).is_dir():
            plans_dir = candidate
            plans_dir_exists = True
            break

    gates_file = None
    if plans_dir_exists:
        for path in sorted((root / plans_dir).iterdir()):
            lower = path.name.lower()
            if path.is_file() and lower.endswith(".md") and "gates" in lower:
                gates_file = f"{plans_dir}/{path.name}"
                break

    verify_cmd, verify_allow = _verify(root)

    return {
        "repo": repo,
        "name": name,
        "branch": branch,
        "plans_dir": plans_dir,
        "plans_dir_exists": plans_dir_exists,
        "gates_file": gates_file,
        "verify_cmd": verify_cmd,
        "verify_allow": verify_allow,
        "plans": _plans(root, plans_dir, plans_dir_exists),
    }


def import_state(plan, done=frozenset()) -> str:
    """The board state a detected plan would be imported with."""
    state = plan.get("state")
    if state:
        return "DRAFT" if state == "BLOCKED" else state
    if plan["ref"] in done:
        return "DONE"
    return "DRAFT"


def _project_by_repo(conn, repo):
    return conn.execute(
        "SELECT * FROM projects WHERE repo_path = ?", (repo,)
    ).fetchone()


def _project_row(conn, name):
    return conn.execute(
        "SELECT * FROM projects WHERE name = ?", (name,)
    ).fetchone()


def apply(conn, found, branch=None, verify_cmd=None, verify_allow=None,
          import_plans=False, done=frozenset()) -> list[str]:
    """Connect (or reconnect) the detected repo and return the printed lines."""
    lines = []
    repo = str(Path(found["repo"]).resolve())
    row = _project_by_repo(conn, repo)

    if row is not None and not row["archived"]:
        return [f"already connected as {row['name']}"]

    if row is not None:
        name = row["name"]
        board.unarchive_project(conn, name)
    else:
        name = found["name"]
        board.add_project(
            conn,
            name,
            repo,
            plans_dir=found["plans_dir"],
            gates_file=found["gates_file"],
            verify_cmd=verify_cmd if verify_cmd is not None else found["verify_cmd"],
            integration_branch=branch or found["branch"],
        )
        allow = verify_allow if verify_allow is not None else found["verify_allow"]
        if allow:
            board.set_project(conn, name, verify_allow=allow)

    project = _project_row(conn, name)
    assert project is not None

    try:
        gitops.install_guard(project)
    except ValueError as exc:
        lines.append(
            f"push guard NOT installed: {exc} (auto cannot be turned on)"
        )

    if import_plans:
        placed = {
            plan["ref"]
            for plan in conn.execute(
                "SELECT ref FROM plans WHERE project = ?", (name,)
            ).fetchall()
        }
        for plan in found["plans"]:
            if plan["ref"] in placed:
                continue
            state = import_state(plan, done)
            board.import_plan(
                conn, name, plan["ref"], plan["title"], plan["path"], state
            )
            if plan.get("state") == "BLOCKED":
                lines.append(
                    f"{plan['ref']}: was BLOCKED in its file, imported as DRAFT"
                )

    try:
        dashboard.write(conn, config.load())
    except Exception as exc:  # noqa: BLE001 - a dashboard failure never fails a command
        print(f"swarm: warning: dashboard: {exc}", file=sys.stderr)

    missing = []
    if not project["verify_cmd"] or not project["verify_allow"]:
        missing.append("set --verify-cmd and --verify-allow")
    if not gitops.guard_installed(project):
        missing.append("install the push guard")
    if missing:
        lines.append("next: " + ", ".join(missing))
    else:
        lines.append(f"next: swarm project set {name} --auto on")
    return lines


def parse_refs(spec) -> frozenset[str]:
    """Parse ``0001-0003,0007`` into zero-padded refs."""
    if spec is None or not spec.strip():
        return frozenset()
    refs = set()
    width = None
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            raise ValueError(f"bad ref spec: {raw!r}")
        bounds = part.split("-")
        if len(bounds) > 2 or any(not bound.isdigit() for bound in bounds):
            raise ValueError(f"bad ref spec: {part}")
        if width is None:
            width = len(bounds[0])
        if len(bounds) == 1:
            start = end = int(bounds[0])
        else:
            start, end = int(bounds[0]), int(bounds[1])
        if start < 1 or end < 1 or start > end:
            raise ValueError(f"bad ref range: {part}")
        for value in range(start, end + 1):
            refs.add(str(value).zfill(width))
    return frozenset(refs)
