"""Status digest for the swarm board.

``build_status`` returns a JSON-serialisable snapshot and ``format_status`` /
``format_brief`` render it as plain text.

Project filtering: ``project`` narrows the waiting/running/next/done lists, the
decisions/handoff/active-goals lists and the worktree summary.  Locks and
monthly spend in the header stay global because neither locks nor ``runs``
carry a project directly (spend belongs to the implementation account, not a
single project).
"""

import json
import shutil
from datetime import timedelta

from swarm import board, gitscan, procs, states, util
from swarm.scheduler import (CLAUDE_IMPL_PURPOSES, CLAUDE_KINDS,
                             CLAUDE_PURPOSES, OMP_PURPOSES)

SECTION_TITLES = ("Waiting on you", "Running", "Next", "Done (last 24 h)")
BRIEF_SECTIONS = ("Recent decisions", "Last handoff", "Active goals")
ARCH_SECTION = "Architecture review due"
CUT_LINE = "(cut — run swarm status / swarm log for more)"
RUNNING_PLAN_STATES = ("EXECUTING", "VERIFYING", "FIXING", "REVIEW")
NEXT_PLAN_STATES = ("DRAFT", "APPROVED", "MERGED", "MEASURING")
DONE_WINDOW = timedelta(hours=24)
CONTEXT_TAIL_BYTES = 256 * 1024


def _cutoff() -> str:
    return (util.parse_ts(util.now()) - DONE_WINDOW).strftime(util.TS_FORMAT)


def _minutes_since(ts):
    try:
        delta = util.parse_ts(util.now()) - util.parse_ts(ts)
    except (TypeError, ValueError):
        return None
    return max(0, int(delta.total_seconds() // 60))


def _plan_label(project, ref):
    return f"{project}/{ref}" if project and ref else None


def _disk_free_gb(path):
    try:
        return round(shutil.disk_usage(path).free / 1e9, 1)
    except (OSError, ValueError):
        return None


def _asks(conn, project=None):
    sql = (
        "SELECT a.id, a.project, a.question, a.options, a.destructive, a.action, "
        "a.created_at, p.project AS plan_project, p.ref AS plan_ref "
        "FROM asks a LEFT JOIN plans p ON p.id = a.plan_id "
        "WHERE a.state = 'OPEN' "
        "AND (a.project IS NULL OR a.project IN "
        "(SELECT name FROM projects WHERE archived = 0))"
    )
    params = []
    if project is not None:
        sql += " AND a.project = ?"
        params.append(project)
    sql += " ORDER BY a.id"
    out = []
    for row in conn.execute(sql, params):
        out.append({
            "type": "ask",
            "id": row["id"],
            "project": row["project"],
            "plan": _plan_label(row["plan_project"], row["plan_ref"]),
            "question": row["question"],
            "options": row["options"],
            "destructive": bool(row["destructive"]),
            "action": row["action"],
            "created_at": row["created_at"],
        })
    return out


def _blocked_plans(conn, project=None):
    sql = (
        "SELECT id, project, ref, title, blocked_from FROM plans "
        "WHERE state = 'BLOCKED' "
        "AND project IN (SELECT name FROM projects WHERE archived = 0)"
    )
    params = []
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY id"
    out = []
    for row in conn.execute(sql, params):
        label = _plan_label(row["project"], row["ref"])
        was = row["blocked_from"] or "unknown"
        out.append({
            "type": "blocked",
            "id": row["id"],
            "project": row["project"],
            "plan": label,
            "title": row["title"],
            "blocked_from": row["blocked_from"],
            "text": f"{label} — {row['title']}: blocked (was {was})",
        })
    return out


def _running_plans(conn, project=None):
    marks = ", ".join("?" * len(RUNNING_PLAN_STATES))
    sql = (
        "SELECT id, project, ref, title, state, updated_at FROM plans "
        f"WHERE state IN ({marks}) "
        "AND project IN (SELECT name FROM projects WHERE archived = 0)"
    )
    params = list(RUNNING_PLAN_STATES)
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY updated_at DESC, id"
    return [{
        "type": "plan",
        "id": row["id"],
        "project": row["project"],
        "plan": _plan_label(row["project"], row["ref"]),
        "title": row["title"],
        "state": row["state"],
        "updated_at": row["updated_at"],
    } for row in conn.execute(sql, params)]


def _tail_lines(path):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - CONTEXT_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def _last_json_line(lines, matches):
    for line in reversed(lines or []):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and matches(obj):
            return obj
    return None


def _token_count(usage, *keys):
    total = 0
    for key in keys:
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _claude_context(cfg, run_id):
    lines = _tail_lines(procs.run_paths(run_id)[0])
    if lines is None:
        return None
    obj = _last_json_line(
        lines,
        lambda item: item.get("type") == "assistant"
        and item.get("parent_tool_use_id") is None,
    )
    if obj is None:
        return None
    message = obj.get("message") or {}
    usage = message.get("usage") or {}
    return {
        "tokens": _token_count(
            usage, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
        ),
        "window": cfg.claude_context_window,
    }


def _omp_context(cfg, run_id):
    log_dir = procs.run_paths(run_id)[0].parent / f"{run_id}-omp"
    try:
        logs = sorted(path for path in log_dir.glob("*.jsonl") if path.is_file())
    except OSError:
        return None
    if not logs:
        return None
    lines = _tail_lines(logs[-1])
    if lines is None:
        return None
    obj = _last_json_line(
        lines,
        lambda item: item.get("type") == "message_end"
        and (item.get("message") or {}).get("role") == "assistant",
    )
    if obj is None:
        return None
    usage = (obj.get("message") or {}).get("usage") or {}
    return {
        "tokens": _token_count(usage, "input", "cacheRead", "cacheWrite"),
        "window": cfg.omp_context_window,
    }


def _run_context(cfg, run_id, kind, purpose=None):
    try:
        if purpose in CLAUDE_PURPOSES + CLAUDE_IMPL_PURPOSES:
            return _claude_context(cfg, run_id)
        if purpose in OMP_PURPOSES:
            return _omp_context(cfg, run_id)
        if purpose is None:
            if kind in CLAUDE_KINDS:
                return _claude_context(cfg, run_id)
            if kind == "implementer":
                return _omp_context(cfg, run_id)
    except Exception:  # noqa: BLE001 - status must never fail on a bad log
        return None
    return None


def _running_runs(conn, cfg, project=None):
    sql = (
        "SELECT r.id, r.kind, r.session, r.started_at, r.purpose, r.timeout_s, "
        "p.project AS plan_project, p.ref AS plan_ref "
        "FROM runs r LEFT JOIN plans p ON p.id = r.plan_id "
        "WHERE r.status = 'RUNNING' "
        "AND (p.project IS NULL OR p.project IN "
        "(SELECT name FROM projects WHERE archived = 0))"
    )
    params = []
    if project is not None:
        sql += " AND p.project = ?"
        params.append(project)
    sql += " ORDER BY r.started_at, r.id"
    return [{
        "type": "run",
        "id": row["id"],
        "kind": row["kind"],
        "plan": _plan_label(row["plan_project"], row["plan_ref"]),
        "session": row["session"],
        "started_at": row["started_at"],
        "minutes": _minutes_since(row["started_at"]),
        "purpose": row["purpose"],
        "timeout_s": row["timeout_s"],
        "context": _run_context(cfg, row["id"], row["kind"], row["purpose"]),
    } for row in conn.execute(sql, params)]


def _next_goals(conn, project=None):
    sql = (
        "SELECT id, project, title, done_when, priority, est_minutes, state "
        "FROM goals WHERE state IN ('QUEUED', 'ACTIVE') "
        "AND project IN (SELECT name FROM projects WHERE archived = 0)"
    )
    params = []
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY priority DESC, est_minutes IS NULL, est_minutes ASC, id"
    return [{
        "type": "goal",
        "id": row["id"],
        "project": row["project"],
        "title": row["title"],
        "done_when": row["done_when"],
        "priority": row["priority"],
        "est_minutes": row["est_minutes"],
        "state": row["state"],
    } for row in conn.execute(sql, params)]


def _next_plans(conn, project=None):
    marks = ", ".join("?" * len(NEXT_PLAN_STATES))
    sql = (
        "SELECT id, project, ref, title, state, updated_at FROM plans "
        f"WHERE state IN ({marks}) "
        "AND project IN (SELECT name FROM projects WHERE archived = 0)"
    )
    params = list(NEXT_PLAN_STATES)
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY updated_at DESC, id"
    return [{
        "type": "plan",
        "id": row["id"],
        "project": row["project"],
        "plan": _plan_label(row["project"], row["ref"]),
        "title": row["title"],
        "state": row["state"],
        "updated_at": row["updated_at"],
    } for row in conn.execute(sql, params)]


def _move_target(text):
    if " → " not in text:
        return None
    return text.split(" → ", 1)[1].split(":", 1)[0].strip()


def _done_plans(conn, project=None):
    sql = (
        "SELECT e.id AS event_id, e.ts, e.text, p.id AS plan_id, p.project, "
        "p.ref, p.title, p.state FROM events e "
        "JOIN plans p ON p.id = e.plan_id "
        "WHERE e.kind = 'move' AND e.ts >= ? "
        "AND p.project IN (SELECT name FROM projects WHERE archived = 0)"
    )
    params = [_cutoff()]
    if project is not None:
        sql += " AND p.project = ?"
        params.append(project)
    sql += " ORDER BY e.ts DESC, e.id DESC"
    seen = set()
    out = []
    for row in conn.execute(sql, params):
        if _move_target(row["text"]) not in ("DONE", "MERGED"):
            continue
        if row["plan_id"] in seen:
            continue
        seen.add(row["plan_id"])
        out.append({
            "type": "plan",
            "id": row["plan_id"],
            "project": row["project"],
            "plan": _plan_label(row["project"], row["ref"]),
            "title": row["title"],
            "state": row["state"],
            "at": row["ts"],
        })
    return out


def arch_due(conn, cfg, project=None) -> list[dict]:
    if cfg.arch_review_every <= 0:
        return []
    sql = "SELECT name FROM projects WHERE archived = 0"
    params = []
    if project is not None:
        sql += " AND name = ?"
        params.append(project)
    out = []
    for name in [row["name"] for row in conn.execute(sql, params)]:
        review = conn.execute(
            "SELECT id, ts FROM events WHERE kind = 'arch-review' AND project = ? "
            "ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        after_id = review["id"] if review is not None else 0
        merged = set()
        for row in conn.execute(
            "SELECT e.id, e.text, p.id AS plan_id FROM events e "
            "JOIN plans p ON p.id = e.plan_id "
            "WHERE e.kind = 'move' AND p.project = ? AND e.id > ? "
            "ORDER BY e.id",
            (name, after_id),
        ):
            if _move_target(row["text"]) == "MERGED":
                merged.add(row["plan_id"])
        if len(merged) >= cfg.arch_review_every:
            out.append({
                "project": name,
                "merged_since": len(merged),
                "last_review": review["ts"] if review is not None else None,
            })
    return out


def _done_runs(conn, project=None):
    sql = (
        "SELECT r.id, r.kind, r.session, r.summary, r.cost_usd, r.ended_at, "
        "p.project AS plan_project, p.ref AS plan_ref "
        "FROM runs r LEFT JOIN plans p ON p.id = r.plan_id "
        "WHERE r.status = 'OK' AND r.ended_at >= ? "
        "AND (p.project IS NULL OR p.project IN "
        "(SELECT name FROM projects WHERE archived = 0))"
    )
    params = [_cutoff()]
    if project is not None:
        sql += " AND p.project = ?"
        params.append(project)
    sql += " ORDER BY r.ended_at DESC, r.id DESC"
    return [{
        "type": "run",
        "id": row["id"],
        "kind": row["kind"],
        "plan": _plan_label(row["plan_project"], row["plan_ref"]),
        "session": row["session"],
        "summary": row["summary"],
        "cost_usd": row["cost_usd"],
        "ended_at": row["ended_at"],
    } for row in conn.execute(sql, params)]


def _worktrees(conn, project=None):
    sql = (
        "SELECT project, is_main, merged, dirty, plan_ref, target_bytes, scanned_at "
        "FROM worktrees"
    )
    sql += " WHERE project IN (SELECT name FROM projects WHERE archived = 0)"
    params = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    rows = list(conn.execute(sql, params))
    scanned = [row["scanned_at"] for row in rows if row["scanned_at"]]
    if not scanned:
        return None
    states = gitscan.plan_states(conn, project)
    targets = sum(row["target_bytes"] or 0 for row in rows)
    return {
        "count": len(rows),
        "cleanup": sum(
            1 for row in rows
            if gitscan.cleanup_candidate(
                row, states.get((row["project"], row["plan_ref"]))
            )
        ),
        "targets_gb": round(targets / 1e9, 1),
        "scanned_at": max(scanned),
    }


def _daemon_state(cfg):
    path = util.home() / "daemon.heartbeat"
    try:
        stamp = path.read_text().strip()
    except OSError:
        return {"on": False, "age_s": None}
    try:
        delta = util.parse_ts(util.now()) - util.parse_ts(stamp)
    except (TypeError, ValueError):
        return {"on": False, "age_s": None}
    age = max(0, int(delta.total_seconds()))
    return {"on": age < 3 * cfg.daemon_interval_s, "age_s": age}


def _header(conn, cfg, project=None):
    spend = round(board.month_spend(conn), 2)
    budget = cfg.monthly_budget_usd
    locks = [{
        "name": row["name"],
        "holder": row["holder"],
        "since": row["acquired_at"],
    } for row in conn.execute(
        "SELECT name, holder, acquired_at FROM locks ORDER BY name"
    )]
    return {
        "disk_free_gb": _disk_free_gb(cfg.disk_path),
        "disk_label": cfg.disk_name(),
        "disk_floor_gb": cfg.disk_floor_gb,
        "month_spend_usd": spend,
        "monthly_budget_usd": budget,
        "budget_warn": spend >= budget * cfg.budget_warn_ratio,
        "locks": locks,
        "daemon": _daemon_state(cfg),
        "worktrees": _worktrees(conn, project),
        "context_warn": {
            "tokens": cfg.pm_context_warn_tokens,
            "step": cfg.pm_context_warn_step,
            "alert": cfg.pm_context_alert_tokens,
        },
    }


def _projects(conn, project=None):
    sql = "SELECT name, auto, integration_branch FROM projects WHERE archived = 0"
    params = []
    if project is not None:
        sql += " AND name = ?"
        params.append(project)
    sql += " ORDER BY name"
    goal_marks = ", ".join("?" * len(states.TERMINAL))
    out = []
    for row in conn.execute(sql, params):
        counts = {state: 0 for state in states.PLAN_STATES}
        for count_row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM plans WHERE project = ? GROUP BY state",
            (row["name"],),
        ):
            if count_row["state"] in counts:
                counts[count_row["state"]] = count_row["n"]
        goals_open = conn.execute(
            f"SELECT COUNT(*) FROM goals WHERE project = ? "
            f"AND state NOT IN ({goal_marks})",
            (row["name"], *states.TERMINAL),
        ).fetchone()[0]
        out.append({
            "name": row["name"],
            "auto": bool(row["auto"]),
            "branch": row["integration_branch"],
            "counts": {key: value for key, value in counts.items() if value}
            | {"goals_open": goals_open},
        })
    return out


def build_status(conn, cfg, project=None) -> dict:
    waiting = _asks(conn, project) + _blocked_plans(conn, project)
    running = _running_plans(conn, project) + _running_runs(conn, cfg, project)
    nxt = _next_goals(conn, project) + _next_plans(conn, project)
    done = _done_plans(conn, project) + _done_runs(conn, project)
    return {
        "schema": 1,
        "waiting": waiting,
        "running": running,
        "next": nxt,
        "done": done,
        "arch_due": arch_due(conn, cfg, project),
        "projects": _projects(conn, project),
        "header": _header(conn, cfg, project),
    }


def _fmt_gb(value):
    return f"{value:.1f}"


def _fmt_floor(value):
    return f"{value:g}"


def _lock_minutes(since):
    minutes = _minutes_since(since)
    return 0 if minutes is None else minutes


def _header_line(header):
    free = header["disk_free_gb"]
    if free is None:
        disk = "disk: unknown"
    else:
        disk = (f"Disk {header['disk_label']} {_fmt_gb(free)} GB free "
                f"(floor {_fmt_floor(header['disk_floor_gb'])})")
        if free < header["disk_floor_gb"]:
            disk = "⚠ " + disk
    spend = (
        f"opencode ${header['month_spend_usd']:.2f} / "
        f"${header['monthly_budget_usd']:.2f} this month"
    )
    if header["budget_warn"]:
        spend = "⚠ " + spend
    locks = header["locks"]
    if locks:
        lock_text = "locks: " + ", ".join(
            f"{lock['name']} ({lock['holder']}, {_lock_minutes(lock['since'])} min)"
            for lock in locks
        )
    else:
        lock_text = "locks: none"
    worktrees = header.get("worktrees")
    if worktrees is None:
        worktree_text = "worktrees: not scanned"
    else:
        worktree_text = (
            f"worktrees: {worktrees['count']} ({worktrees['cleanup']} cleanup), "
            f"targets {_fmt_gb(worktrees['targets_gb'])} GB"
        )
    daemon = header.get("daemon") or {"on": False, "age_s": None}
    if daemon["on"]:
        daemon_text = f"daemon: on ({daemon['age_s']}s ago)"
    else:
        daemon_text = "daemon: off"
    return " · ".join([disk, spend, daemon_text, lock_text, worktree_text])


def _ask_line(item):
    label = item["plan"] or item["project"]
    head = f"- ask #{item['id']}"
    if label:
        head += f" ({label})"
    line = f"{head}: {item['question']}"
    if item["options"]:
        line += f" — options: {item['options']}"
    if item["destructive"]:
        line += " [destructive]"
    return line


def _plan_line(item):
    return f"- {item['plan']} — {item['title']}: {item['state']}"


def _run_line(item):
    line = f"- run #{item['id']} {item['kind']}"
    if item["plan"]:
        line += f" {item['plan']}"
    minutes = "unknown" if item["minutes"] is None else f"{item['minutes']} min"
    line += (
        f" — session {item['session'] or 'none'}, "
        f"started {item['started_at']}, {minutes}"
    )
    return line


def _goal_line(item):
    extras = [item["state"], f"priority {item['priority']}"]
    if item["est_minutes"] is not None:
        extras.append(f"{item['est_minutes']} min")
    return f"- goal #{item['id']} {item['project']}: {item['title']} — " + ", ".join(extras)


def _done_plan_line(item):
    return f"{_plan_line(item)} ({item['at']})"


def _done_run_line(item):
    line = f"- run #{item['id']} {item['kind']}"
    if item["plan"]:
        line += f" {item['plan']}"
    return f"{line}: {item['summary'] or 'ok'} ({item['ended_at']})"


def _section_lines(items, renderers):
    lines = []
    for item in items:
        renderer = renderers.get(item["type"])
        if renderer is not None:
            lines.append(renderer(item))
    return lines


def _status_parts(d):
    head = [
        f"Swarm status — {util.now()}",
        _header_line(d["header"]),
        "",
    ]
    sections = [
        (SECTION_TITLES[0], _section_lines(
            d["waiting"], {"ask": _ask_line, "blocked": lambda i: f"- {i['text']}"}
        )),
        (SECTION_TITLES[1], _section_lines(
            d["running"], {"plan": _plan_line, "run": _run_line}
        )),
        (SECTION_TITLES[2], _section_lines(
            d["next"], {"goal": _goal_line, "plan": _plan_line}
        )),
        (SECTION_TITLES[3], _section_lines(
            d["done"], {"plan": _done_plan_line, "run": _done_run_line}
        )),
    ]
    return head, sections


def _render(head, sections):
    lines = list(head)
    for title, bullets in sections:
        lines.append(title)
        lines.extend(bullets or ["- nothing"])
    return lines


def format_status(d) -> str:
    head, sections = _status_parts(d)
    return "\n".join(_render(head, sections))


def _decisions(conn, project=None):
    sql = "SELECT ts, text FROM events WHERE kind = 'decision'"
    params = []
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY id DESC LIMIT 10"
    return [f"- {row['ts']} {row['text']}" for row in conn.execute(sql, params)]


def _handoff(conn, project=None):
    sql = "SELECT ts, text FROM events WHERE kind = 'handoff'"
    params = []
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY id DESC LIMIT 1"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    return [f"- {rows[0]['ts']} {rows[0]['text']}"]


def _active_goals(conn, project=None):
    sql = (
        "SELECT id, project, title, done_when FROM goals "
        "WHERE state IN ('QUEUED', 'ACTIVE') "
        "AND project IN (SELECT name FROM projects WHERE archived = 0)"
    )
    params = []
    if project is not None:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY priority DESC, est_minutes IS NULL, est_minutes ASC, id"
    return [
        f"- #{row['id']} {row['project']}: {row['title']} — done when: {row['done_when']}"
        for row in conn.execute(sql, params)
    ]


def _arch_bullets(items):
    out = []
    for item in items:
        date = item["last_review"][:10] if item["last_review"] else "never"
        out.append(
            f"- {item['project']}: {item['merged_since']} plans merged since the "
            f"last review ({date}) — suggest /improve-codebase-architecture"
        )
    return out


def format_brief(conn, cfg, project=None, max_lines=60) -> str:
    d = build_status(conn, cfg, project)
    head, sections = _status_parts(d)
    brief = [
        ("Recent decisions", _decisions(conn, project)),
        ("Last handoff", _handoff(conn, project)),
        ("Active goals", _active_goals(conn, project)),
    ]
    arch = _arch_bullets(d["arch_due"])
    if arch:
        brief.append((ARCH_SECTION, arch))

    def count():
        total = len(head) + 1  # header lines plus the blank before the brief
        for _, bullets in sections:
            total += 1 + max(1, len(bullets))
        for _, bullets in brief:
            total += 1 + max(1, len(bullets))
        return total

    truncated = count() > max_lines
    if truncated:
        target = max_lines - 1
        candidates = [bullets for _, bullets in sections] + [bullets for _, bullets in brief]
        while count() > target:
            eligible = [bullets for bullets in candidates if len(bullets) > 1]
            if not eligible:
                break
            max(eligible, key=len).pop()

    lines = _render(head, sections)
    lines.append("")
    for title, bullets in brief:
        lines.append(title)
        lines.extend(bullets or ["- nothing" if title != "Last handoff" else "- none"])
    if truncated:
        lines.append(CUT_LINE)
    return "\n".join(lines)
