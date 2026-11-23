"""Static HTML dashboard for the swarm board.

The page is regenerated after every mutating command.  It embeds no external
resources and no JavaScript, so it works from a local file and survives a
machine with no network.
"""

import html
import os
import pathlib
import sys
from datetime import timedelta

from swarm import gitscan, status, util

PLANNED = ("DRAFT", "REVIEW", "APPROVED")
EXECUTING = ("EXECUTING", "FIXING")
VERIFYING = ("VERIFYING",)
WAITING = ("BLOCKED",)
DONE_STATES = ("MERGED", "MEASURING")
DONE_WINDOW = 7 * 24 * 60
EVENT_LIMIT = 30

STYLE = """
:root {
  --bg: #f4f6f8;
  --fg: #1c2128;
  --muted: #5c6672;
  --card: #ffffff;
  --border: #d5dae0;
  --badge-bg: #eef2f7;
  --accent: #2b6cb0;
  --warn: #b45309;
  --wait-bg: #fff7ed;
  --wait-border: #d97706;
  --table-head: #e8edf3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #11151a;
    --fg: #e6e9ee;
    --muted: #9aa4b1;
    --card: #1a2028;
    --border: #2c3540;
    --badge-bg: #263040;
    --accent: #7ab7ff;
    --warn: #f6ad55;
    --wait-bg: #2a1d0f;
    --wait-border: #f6ad55;
    --table-head: #232c37;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    Helvetica, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.45;
}
header.strip {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 1.25rem;
  padding: 0.7rem 1rem;
  background: var(--card);
  border-bottom: 1px solid var(--border);
}
header.strip .item { color: var(--muted); }
header.strip .item.warn { color: var(--warn); }
header.strip a.item { color: var(--accent); text-decoration: none; }
header.strip a.item:hover { text-decoration: underline; }
header.strip .updated { margin-left: auto; }
main { padding: 1rem; }
h1 { font-size: 1.15rem; margin: 0 0 0.75rem; }
h2 { font-size: 1rem; margin: 0 0 0.6rem; }
h3 { font-size: 0.95rem; margin: 1rem 0 0.4rem; }
.board {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0.8rem;
  align-items: start;
}
.column {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.7rem;
  min-width: 0;
}
.column.waiting {
  border: 2px solid var(--wait-border);
  background: var(--wait-bg);
}
.column h2 { display: flex; justify-content: space-between; gap: 0.5rem; }
.column h2 .count { color: var(--muted); font-weight: normal; }
.card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.55rem 0.6rem;
  margin-bottom: 0.55rem;
  background: var(--bg);
  overflow-wrap: anywhere;
}
.card:last-child { margin-bottom: 0; }
.card-head {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  align-items: baseline;
}
.card .ref { font-weight: 600; }
.card .title { margin: 0.25rem 0; }
.card .meta { color: var(--muted); font-size: 0.85rem; }
.badge {
  background: var(--badge-bg);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.05rem 0.5rem;
  font-size: 0.75rem;
  white-space: nowrap;
}
.badge.warn { color: var(--warn); border-color: var(--warn); }
.empty { color: var(--muted); font-style: italic; }
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--card);
  border: 1px solid var(--border);
  margin-bottom: 1.25rem;
}
th, td {
  text-align: left;
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  overflow-wrap: anywhere;
}
th { background: var(--table-head); font-weight: 600; }
.muted { color: var(--muted); }
@media (max-width: 700px) {
  header.strip .updated { margin-left: 0; }
}
"""


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _minutes_since(ts) -> int:
    try:
        delta = util.parse_ts(util.now()) - util.parse_ts(ts)
    except (TypeError, ValueError):
        return 0
    return max(0, int(delta.total_seconds() // 60))


def _age(ts) -> str:
    if ts is None:
        return "—"
    minutes = _minutes_since(ts)
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} d ago"


def _gb(size) -> str:
    if size is None:
        return "—"
    return f"{size / 1e9:.1f} GB"


def _changes(row) -> str:
    if row["missing"]:
        return "missing"
    if row["dirty"] is None:
        return "—"
    if row["dirty"] == 0:
        return "clean"
    return f"{row['dirty']} changes"


def _worktree_status(row, plan_state) -> str:
    return gitscan.worktree_status(row, plan_state)


def _plan_cards(plans, worktree_dirs=None, running=None) -> str:
    if not plans:
        return '<p class="empty">—</p>'
    worktree_dirs = worktree_dirs or {}
    running = running or {}
    cards = []
    for plan in plans:
        ref = f"{plan['project']}/{plan['ref']}"
        meta = [f"branch: {plan['branch']}" if plan["branch"] else "branch: —"]
        if plan["fix_rounds"] > 0:
            meta.append(f"fix rounds {plan['fix_rounds']}")
        meta.append(f"updated {_minutes_since(plan['updated_at'])} min ago")
        dirname = worktree_dirs.get((plan["project"], plan["ref"]))
        if dirname is not None:
            meta.append(f"worktree {dirname}")
        run = running.get(plan["id"])
        if run is not None:
            meta.append(
                f"run #{run['id']} {run['kind']} {run['purpose'] or '-'}, "
                f"{_age(run['started_at'])}"
            )
        cards.append(
            '<article class="card">'
            '<div class="card-head">'
            f'<span class="ref">{_esc(ref)}</span>'
            f'<span class="badge">{_esc(plan["state"])}</span>'
            "</div>"
            f'<div class="title">{_esc(plan["title"])}</div>'
            + "".join(f'<div class="meta">{_esc(line)}</div>' for line in meta)
            + "</article>"
        )
    return "\n".join(cards)


def _ask_cards(asks) -> str:
    cards = []
    for ask in asks:
        label = (
            f"{ask['plan_project']}/{ask['plan_ref']}"
            if ask["plan_project"]
            else ask["project"]
        )
        meta = []
        if ask["options"]:
            meta.append(f"options: {ask['options']}")
        if ask["destructive"]:
            meta.append("destructive")
        cards.append(
            '<article class="card">'
            '<div class="card-head">'
            f'<span class="ref">{_esc(label or "ask")}</span>'
            '<span class="badge">ASK</span>'
            "</div>"
            f'<div class="title">{_esc(ask["question"])}</div>'
            + "".join(f'<div class="meta">{_esc(line)}</div>' for line in meta)
            + "</article>"
        )
    return "\n".join(cards)


def _column(title, body, extra_class="") -> str:
    classes = f"column {extra_class}".strip()
    return (
        f'<div class="{classes}">'
        f"<h2>{title}</h2>\n{body}\n</div>"
    )


def _plans(conn, states, done_cutoff=None):
    marks = ", ".join("?" * len(states))
    params = list(states)
    sql = (
        "SELECT id, project, ref, title, state, fix_rounds, branch, updated_at "
        f"FROM plans WHERE state IN ({marks}) "
        "AND project IN (SELECT name FROM projects WHERE archived = 0)"
    )
    if done_cutoff is not None:
        sql += " AND updated_at >= ?"
        params.append(done_cutoff)
    sql += " ORDER BY updated_at DESC, id DESC"
    return conn.execute(sql, params).fetchall()


def _running_run_map(conn):
    rows = conn.execute(
        "SELECT id, plan_id, kind, purpose, started_at FROM runs "
        "WHERE status = 'RUNNING' AND plan_id IS NOT NULL ORDER BY id"
    ).fetchall()
    runs = {}
    for row in rows:
        runs.setdefault(row["plan_id"], row)
    return runs


def _open_asks(conn):
    return conn.execute(
        "SELECT a.id, a.project, a.question, a.options, a.destructive, "
        "       p.project AS plan_project, p.ref AS plan_ref "
        "FROM asks a LEFT JOIN plans p ON p.id = a.plan_id "
        "WHERE a.state = 'OPEN' "
        "AND (a.project IS NULL OR a.project IN "
        "(SELECT name FROM projects WHERE archived = 0)) "
        "ORDER BY a.id"
    ).fetchall()


def _goals(conn):
    return conn.execute(
        "SELECT id, project, title, done_when, state, priority, est_minutes "
        "FROM goals WHERE project IN "
        "(SELECT name FROM projects WHERE archived = 0) ORDER BY id"
    ).fetchall()


def _events(conn):
    return conn.execute(
        "SELECT ts, kind, text FROM events ORDER BY id DESC LIMIT ?",
        (EVENT_LIMIT,),
    ).fetchall()


def _header_items(header):
    free = header["disk_free_gb"]
    if free is None:
        disk = "disk: unknown"
        disk_warn = False
    else:
        disk = (
            f"Disk {header['disk_label']} {free:.1f} GB free "
            f"(floor {header['disk_floor_gb']:g})"
        )
        disk_warn = free < header["disk_floor_gb"]
    spend = (
        f"opencode ${header['month_spend_usd']:.2f} / "
        f"${header['monthly_budget_usd']:.2f} this month"
    )
    locks = header["locks"]
    if locks:
        lock_text = "locks: " + ", ".join(
            f"{lock['name']} ({lock['holder']}, "
            f"{_minutes_since(lock['since'])} min)"
            for lock in locks
        )
    else:
        lock_text = "locks: none"
    items = [
        (disk, disk_warn),
        (spend, header["budget_warn"]),
        (lock_text, False),
    ]
    daemon = header.get("daemon") or {"on": False, "age_s": None}
    if daemon["on"]:
        items.append((f"daemon: on ({daemon['age_s']}s ago)", False))
    else:
        items.append(("daemon: off", False))
    items.append((f"updated {util.now()}", False))
    return [
        f'<span class="item{(" warn" if warn else "")}">{_esc(text)}</span>'
        for text, warn in items
    ]


def _goals_table(goals) -> str:
    rows = []
    for goal in goals:
        estimate = (
            "" if goal["est_minutes"] is None else f"{goal['est_minutes']} min"
        )
        rows.append(
            "<tr>"
            f"<td>{_esc(goal['id'])}</td>"
            f"<td>{_esc(goal['project'])}</td>"
            f"<td>{_esc(goal['title'])}</td>"
            f"<td>{_esc(goal['done_when'])}</td>"
            f"<td>{_esc(goal['state'])}</td>"
            f"<td>{_esc(goal['priority'])}</td>"
            f"<td>{_esc(estimate)}</td>"
            "</tr>"
        )
    if not rows:
        rows = ['<tr><td colspan="7" class="empty">—</td></tr>']
    return (
        "<table><thead><tr>"
        "<th>id</th><th>project</th><th>title</th><th>done when</th>"
        "<th>state</th><th>priority</th><th>estimate</th>"
        "</tr></thead><tbody>\n"
        + "\n".join(rows)
        + "\n</tbody></table>"
    )


def _events_table(events) -> str:
    rows = []
    for event in events:
        rows.append(
            "<tr>"
            f"<td>{_esc(event['ts'])}</td>"
            f"<td>{_esc(event['kind'])}</td>"
            f"<td>{_esc(event['text'])}</td>"
            "</tr>"
        )
    if not rows:
        rows = ['<tr><td colspan="3" class="empty">—</td></tr>']
    return (
        "<table><thead><tr>"
        "<th>ts</th><th>kind</th><th>text</th>"
        "</tr></thead><tbody>\n"
        + "\n".join(rows)
        + "\n</tbody></table>"
    )


def _worktree_rows(conn, project):
    return conn.execute(
        "SELECT project, path, branch, is_main, ahead, behind, merged, dirty, "
        "       target_bytes, last_commit_at, plan_ref, missing, scanned_at "
        "FROM worktrees WHERE project = ? "
        "ORDER BY is_main DESC, last_commit_at IS NULL, last_commit_at DESC, path",
        (project,),
    ).fetchall()


def _branch_rows(conn, project):
    return conn.execute(
        "SELECT project, name, merged, ahead, last_commit_at, plan_ref "
        "FROM branches WHERE project = ? ORDER BY name",
        (project,),
    ).fetchall()


def _plan_states(conn):
    return gitscan.plan_states(conn)


def _worktree_dirs(conn):
    dirs = {}
    for row in conn.execute(
        "SELECT project, plan_ref, path FROM worktrees WHERE plan_ref IS NOT NULL"
    ):
        dirs.setdefault(
            (row["project"], row["plan_ref"]), pathlib.Path(row["path"]).name
        )
    return dirs


def _worktree_table(rows, integration_branch, states) -> str:
    body = []
    for row in rows:
        if row["plan_ref"]:
            plan = _esc(row["plan_ref"])
            state = states.get((row["project"], row["plan_ref"]))
            if state is not None:
                plan += f' <span class="badge">{_esc(state)}</span>'
        else:
            plan = "—"
        ahead = "?" if row["ahead"] is None else row["ahead"]
        behind = "?" if row["behind"] is None else row["behind"]
        status = _worktree_status(
            row, states.get((row["project"], row["plan_ref"]))
        )
        badge = f'<span class="badge{" warn" if status == "cleanup" else ""}">' \
                f"{status}</span>"
        body.append(
            "<tr>"
            f'<td title="{_esc(row["path"])}">'
            f'{_esc(pathlib.Path(row["path"]).name)}</td>'
            f"<td>{_esc(row['branch'] or 'detached')}</td>"
            f"<td>{plan}</td>"
            f"<td>+{_esc(ahead)} / −{_esc(behind)}</td>"
            f"<td>{_esc(_changes(row))}</td>"
            f"<td>{_esc(_gb(row['target_bytes']))}</td>"
            f"<td>{_esc(_age(row['last_commit_at']))}</td>"
            f"<td>{badge}</td>"
            "</tr>"
        )
    return (
        '<div style="overflow-x: auto">'
        "<table><thead><tr>"
        "<th>Worktree</th><th>Branch</th><th>Plan</th>"
        f"<th>vs {_esc(integration_branch)}</th>"
        "<th>Changes</th><th>Target</th><th>Last commit</th><th>Status</th>"
        "</tr></thead><tbody>\n"
        + "\n".join(body)
        + "\n</tbody></table></div>"
    )


def _branch_notes(rows, states) -> str:
    lines = []
    loose = [row for row in rows if not row["merged"]]
    if loose:
        items = []
        for row in loose:
            ahead = "?" if row["ahead"] is None else f"+{row['ahead']}"
            items.append(
                f"{_esc(row['name'])} "
                f"({_esc(row['plan_ref'] or '—')}, {_esc(ahead)}, "
                f"{_esc(_age(row['last_commit_at']))})"
            )
        lines.append(
            '<p class="muted">Loose branches (not merged): '
            + ", ".join(items)
            + "</p>"
        )
    merged = sum(
        1 for row in rows
        if gitscan.branch_deletable(
            row, states.get((row["project"], row["plan_ref"]))
        )
    )
    if merged:
        lines.append(
            f'<p class="muted">{merged} merged branches can be deleted.</p>'
        )
    return "\n".join(lines)


def _worktrees_section(conn) -> str:
    states = _plan_states(conn)
    parts = []
    projects = conn.execute(
        "SELECT name, integration_branch FROM projects "
        "WHERE archived = 0 ORDER BY name"
    ).fetchall()
    for project in projects:
        rows = _worktree_rows(conn, project["name"])
        if not rows:
            parts.append(
                f"<h3>{_esc(project['name'])} — never scanned — "
                "run swarm scan</h3>"
            )
            continue
        scanned = max(row["scanned_at"] for row in rows)
        parts.append(
            f"<h3>{_esc(project['name'])} — scanned "
            f"{_age(scanned)}</h3>"
        )
        parts.append(
            _worktree_table(rows, project["integration_branch"], states)
        )
        notes = _branch_notes(_branch_rows(conn, project["name"]), states)
        if notes:
            parts.append(notes)
    if not parts:
        return '<p class="empty">—</p>'
    return "\n".join(parts)


def _done_cutoff() -> str:
    return (
        util.parse_ts(util.now()) - timedelta(minutes=DONE_WINDOW)
    ).strftime(util.TS_FORMAT)


def render(conn, cfg) -> str:
    header = status.build_status(conn, cfg)["header"]
    done_cutoff = _done_cutoff()

    worktree_dirs = _worktree_dirs(conn)
    running = _running_run_map(conn)

    planned = _plans(conn, PLANNED)
    executing = _plans(conn, EXECUTING)
    verifying = _plans(conn, VERIFYING)
    blocked = _plans(conn, WAITING)
    done = _plans(conn, DONE_STATES) + _plans(
        conn, ("DONE",), done_cutoff=done_cutoff
    )

    columns = [
        _column("Planned", _plan_cards(planned, worktree_dirs)),
        _column("Executing", _plan_cards(executing, worktree_dirs, running)),
        _column("Verifying", _plan_cards(verifying, worktree_dirs, running)),
        _column(
            "Waiting on you",
            _ask_cards(_open_asks(conn))
            + "\n"
            + _plan_cards(blocked, worktree_dirs),
            "waiting",
        ),
        _column("Done / measuring", _plan_cards(done, worktree_dirs)),
    ]

    refresh = int(cfg.dashboard_refresh_s)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="refresh" content="{refresh}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Swarm board</title>\n"
        f"<style>{STYLE}</style>\n"
        "</head>\n<body>\n"
        '<header class="strip">\n'
        '<a class="item help" href="help.html">Help</a>\n'
        + "\n".join(_header_items(header))
        + "\n</header>\n<main>\n"
        "<h1>Swarm board</h1>\n"
        '<section class="board">\n'
        + "\n".join(columns)
        + "\n</section>\n"
        "<section><h2>Worktrees</h2>\n"
        + _worktrees_section(conn)
        + "\n</section>\n"
        "<section><h2>Goals</h2>\n"
        + _goals_table(_goals(conn))
        + "\n</section>\n"
        "<section><h2>Recent events</h2>\n"
        + _events_table(_events(conn))
        + "\n</section>\n"
        "</main>\n</body>\n</html>\n"
    )


def write(conn, cfg, out=None) -> pathlib.Path:
    target = pathlib.Path(out) if out is not None else util.home() / "dashboard.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(render(conn, cfg), encoding="utf-8")
    os.replace(tmp, target)

    help_target = target.parent / "help.html"
    help_tmp = help_target.with_suffix(help_target.suffix + ".tmp")
    try:
        from swarm import helppage

        content = helppage.render(target.name)
        try:
            current = help_target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
        if current != content:
            help_tmp.write_text(content, encoding="utf-8")
            os.replace(help_tmp, help_target)
    except OSError as exc:
        print(f"swarm: warning: help page: {exc}", file=sys.stderr)
        try:
            help_tmp.unlink()
        except OSError:
            pass
    return target
