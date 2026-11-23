"""Board operations: the only code that writes rows to the swarm database."""

import json
import sqlite3
from pathlib import Path

from swarm import db, states, util

PLAN_FIELDS = ("branch", "worktree", "base_sha", "merge_sha", "path", "omp_session",
               "review_rounds", "last_review_path", "operator_note", "merge_attempts",
               "ib_sha", "window", "window_ok", "measure_from", "goal_id", "title")
NOTE_KINDS = ("note", "decision", "handoff", "arch-review")


def _project_exists(conn, project) -> bool:
    row = conn.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone()
    return row is not None


def _require_project(conn, project) -> None:
    if not _project_exists(conn, project):
        raise ValueError(f"no project {project}")


def _check_fields(fields) -> None:
    unknown = sorted(set(fields) - set(PLAN_FIELDS))
    if unknown:
        raise ValueError(
            f"unknown plan field(s): {', '.join(unknown)}; "
            f"allowed: {', '.join(PLAN_FIELDS)}"
        )


def _plan_context(conn, plan_id):
    """Return (project, plan_id, "project/ref") for a plan id, or (None, None, "")."""
    if plan_id is None:
        return None, None, ""
    row = conn.execute(
        "SELECT project, ref FROM plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        return None, None, ""
    return row["project"], plan_id, f"{row['project']}/{row['ref']}"


def add_project(conn, name, repo_path, plans_dir="docs/plans", gates_file=None,
                verify_cmd=None, integration_branch="main"):
    raw = Path(repo_path).expanduser()
    if not raw.is_dir():
        raise ValueError(f"repo path does not exist: {repo_path}")
    absolute = str(raw.resolve())
    with conn:
        if _project_exists(conn, name):
            raise ValueError(f"project {name} already exists")
        try:
            conn.execute(
                "INSERT INTO projects (name, repo_path, plans_dir, gates_file, "
                "verify_cmd, integration_branch, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, absolute, plans_dir, gates_file, verify_cmd, integration_branch,
                 util.now()),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" not in str(exc):
                raise
            raise ValueError(f"project {name} already exists") from None
        db.add_event(conn, "project", f"project {name} added at {absolute}", project=name)


def set_project(conn, name, auto=None, verify_cmd=None, verify_allow=None,
                implementer=None, rules=None) -> None:
    _require_project(conn, name)
    if implementer is not None and implementer not in ("omp", "claude"):
        raise ValueError("implementer must be omp or claude")
    updates = {}
    if auto is not None:
        updates["auto"] = int(bool(auto))
    if verify_cmd is not None:
        updates["verify_cmd"] = verify_cmd
    if verify_allow is not None:
        updates["verify_allow"] = verify_allow
    if implementer is not None:
        updates["implementer"] = implementer
    if rules is not None:
        updates["rules"] = json.dumps(list(rules)) if rules else None
    if not updates:
        return
    assignments = ", ".join(f"{field} = ?" for field in updates)
    with conn:
        conn.execute(
            f"UPDATE projects SET {assignments} WHERE name = ?",
            (*updates.values(), name),
        )
        db.add_event(
            conn, "project",
            f"project {name} set {', '.join(sorted(updates))}", project=name,
        )


def archive_project(conn, name) -> None:
    if not _project_exists(conn, name):
        raise LookupError(f"no project {name}")
    with conn:
        conn.execute(
            "UPDATE projects SET archived = 1, auto = 0 WHERE name = ?", (name,)
        )
        db.add_event(conn, "project", f"project {name} disconnected", project=name)


def unarchive_project(conn, name) -> None:
    if not _project_exists(conn, name):
        raise LookupError(f"no project {name}")
    with conn:
        conn.execute("UPDATE projects SET archived = 0 WHERE name = ?", (name,))
        db.add_event(conn, "project", f"project {name} reconnected", project=name)


def project_rules(row) -> list[str]:
    try:
        rules = json.loads(row["rules"]) if row["rules"] is not None else None
    except (TypeError, ValueError):
        return []
    if not isinstance(rules, list):
        return []
    return [rule for rule in rules if isinstance(rule, str)]


def get_plan(conn, project, ref):
    row = conn.execute(
        "SELECT * FROM plans WHERE project = ? AND ref = ?", (project, ref)
    ).fetchone()
    if row is None:
        raise LookupError(f"no plan {project}/{ref}")
    return row


def add_goal(conn, project, title, done_when, priority=0, est_minutes=None) -> int:
    _require_project(conn, project)
    ts = util.now()
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (project, title, done_when, priority, est_minutes, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)",
            (project, title, done_when, priority, est_minutes, ts, ts),
        )
        goal_id = cur.lastrowid
        db.add_event(conn, "goal", f"goal #{goal_id} {project}: {title}", project=project)
    return goal_id


def set_goal_state(conn, goal_id, state) -> None:
    if state not in states.GOAL_STATES:
        raise ValueError(
            f"unknown goal state {state}; allowed: {', '.join(states.GOAL_STATES)}"
        )
    row = conn.execute(
        "SELECT project, title, state FROM goals WHERE id = ?", (goal_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no goal {goal_id}")
    with conn:
        conn.execute(
            "UPDATE goals SET state = ?, updated_at = ? WHERE id = ?",
            (state, util.now(), goal_id),
        )
        db.add_event(
            conn, "goal",
            f"goal #{goal_id} {row['state']} → {state}", project=row["project"],
        )


def add_plan(conn, project, ref, title, path=None, goal_id=None) -> int:
    _require_project(conn, project)
    if goal_id is not None:
        goal = conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if goal is None:
            raise ValueError(f"no goal {goal_id}")
    existing = conn.execute(
        "SELECT 1 FROM plans WHERE project = ? AND ref = ?", (project, ref)
    ).fetchone()
    if existing is not None:
        raise ValueError(f"plan {project}/{ref} already exists")
    ts = util.now()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO plans (project, ref, title, path, goal_id, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?)",
                (project, ref, title, path, goal_id, ts, ts),
            )
            plan_id = cur.lastrowid
            db.add_event(
                conn, "plan", f"{project}/{ref} added: {title}", project=project,
                plan_id=plan_id,
            )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" not in str(exc):
            raise
        raise ValueError(f"plan {project}/{ref} already exists") from None
    return plan_id


def import_plan(conn, project, ref, title, path, state) -> int:
    _require_project(conn, project)
    if state not in states.PLAN_STATES or state == "BLOCKED":
        raise ValueError(
            f"cannot import plan state {state}; allowed: "
            f"{', '.join(s for s in states.PLAN_STATES if s != 'BLOCKED')}"
        )
    existing = conn.execute(
        "SELECT 1 FROM plans WHERE project = ? AND ref = ?", (project, ref)
    ).fetchone()
    if existing is not None:
        raise ValueError(f"plan {project}/{ref} already exists")
    ts = util.now()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO plans (project, ref, title, path, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project, ref, title, path, state, ts, ts),
            )
            plan_id = cur.lastrowid
            db.add_event(
                conn, "plan", f"{project}/{ref} imported as {state}: {title}",
                project=project, plan_id=plan_id,
            )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" not in str(exc):
            raise
        raise ValueError(f"plan {project}/{ref} already exists") from None
    return plan_id


def set_plan_ref(conn, project, old_ref, new_ref) -> None:
    row = get_plan(conn, project, old_ref)
    taken = conn.execute(
        "SELECT 1 FROM plans WHERE project = ? AND ref = ?", (project, new_ref)
    ).fetchone()
    if taken is not None:
        raise ValueError(f"plan {project}/{new_ref} already exists")
    with conn:
        conn.execute(
            "UPDATE plans SET ref = ?, updated_at = ? WHERE id = ?",
            (new_ref, util.now(), row["id"]),
        )
        db.add_event(
            conn, "plan", f"{project}/{old_ref} renamed to {project}/{new_ref}",
            project=project, plan_id=row["id"],
        )


def move_plan(conn, project, ref, target, note=None, **fields) -> None:
    _check_fields(fields)
    row = get_plan(conn, project, ref)
    current = row["state"]
    states.check_plan_move(current, target, row["fix_rounds"], row["blocked_from"])

    fix_rounds = row["fix_rounds"]
    if current == "VERIFYING" and target == "FIXING":
        fix_rounds += 1
    if target == "BLOCKED":
        blocked_from = current
    elif current == "BLOCKED":
        blocked_from = None
    else:
        blocked_from = row["blocked_from"]

    updates = {
        "state": target,
        "blocked_from": blocked_from,
        "fix_rounds": fix_rounds,
        "updated_at": util.now(),
        **fields,
    }
    assignments = ", ".join(f"{name} = ?" for name in updates)
    with conn:
        conn.execute(
            f"UPDATE plans SET {assignments} WHERE id = ?",
            (*updates.values(), row["id"]),
        )
        text = f"{project}/{ref} {current} → {target}"
        if note:
            text += f": {note}"
        db.add_event(conn, "move", text, project=project, plan_id=row["id"])


def set_plan_fields(conn, project, ref, **fields) -> None:
    _check_fields(fields)
    if not fields:
        return
    row = get_plan(conn, project, ref)
    updates = {**fields, "updated_at": util.now()}
    assignments = ", ".join(f"{name} = ?" for name in updates)
    with conn:
        conn.execute(
            f"UPDATE plans SET {assignments} WHERE id = ?",
            (*updates.values(), row["id"]),
        )
        db.add_event(
            conn, "plan",
            f"{project}/{ref} set {', '.join(sorted(fields))}", project=project,
            plan_id=row["id"],
        )


def start_run(conn, kind, plan_id=None, session=None, log_path=None, pid=None,
              report_path=None, timeout_s=None, purpose=None, step=None) -> int:
    if kind not in states.RUN_KINDS:
        raise ValueError(
            f"unknown run kind {kind}; allowed: {', '.join(states.RUN_KINDS)}"
        )
    requested_plan = plan_id
    project, plan_id, label = _plan_context(conn, plan_id)
    if requested_plan is not None and plan_id is None:
        raise ValueError(f"no plan {requested_plan}")
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (plan_id, kind, session, status, log_path, pid, "
            "report_path, timeout_s, purpose, step, started_at) "
            "VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, kind, session, log_path, pid, report_path, timeout_s, purpose,
             step, util.now()),
        )
        run_id = cur.lastrowid
        text = f"run #{run_id} {kind} started"
        if label:
            text += f": {label}"
        db.add_event(conn, "run", text, project=project, plan_id=plan_id)
    return run_id


def set_run_pid(conn, run_id, pid) -> None:
    row = conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    with conn:
        conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (pid, run_id))


def end_run(conn, run_id, status, cost_usd=0.0, summary=None, session=None,
            exit_code=None, report_path=None) -> None:
    if status not in states.RUN_STATUSES:
        raise ValueError(
            f"unknown run status {status}; allowed: {', '.join(states.RUN_STATUSES)}"
        )
    if cost_usd < 0:
        raise ValueError(f"cost must not be negative: {cost_usd}")
    row = conn.execute(
        "SELECT plan_id, kind, status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    if row["status"] != "RUNNING":
        raise ValueError(f"run #{run_id} is {row['status']}, not RUNNING")
    project, plan_id, label = _plan_context(conn, row["plan_id"])
    updates = {
        "status": status,
        "cost_usd": cost_usd,
        "summary": summary,
        "session": session,
        "ended_at": util.now(),
    }
    if exit_code is not None:
        updates["exit_code"] = exit_code
    if report_path is not None:
        updates["report_path"] = report_path
    assignments = ", ".join(f"{name} = ?" for name in updates)
    with conn:
        conn.execute(
            f"UPDATE runs SET {assignments} WHERE id = ?",
            (*updates.values(), run_id),
        )
        conn.execute("DELETE FROM locks WHERE holder = ?", (f"run:{run_id}",))
        text = f"run #{run_id} {row['kind']} {status} cost ${cost_usd:.2f}"
        if summary:
            text += f": {summary}"
        if label:
            text += f" ({label})"
        db.add_event(conn, "run", text, project=project, plan_id=plan_id)


def running_runs(conn, plan_id=None, kinds=None, purposes=None):
    sql = "SELECT * FROM runs WHERE status = 'RUNNING'"
    params = []
    if plan_id is not None:
        sql += " AND plan_id = ?"
        params.append(plan_id)
    if kinds and purposes:
        sql += (" AND (kind IN (" + ", ".join("?" * len(kinds))
                + ") OR purpose IN (" + ", ".join("?" * len(purposes)) + "))")
        params.extend(sorted(kinds))
        params.extend(sorted(purposes))
    elif kinds:
        sql += " AND kind IN (" + ", ".join("?" * len(kinds)) + ")"
        params.extend(sorted(kinds))
    elif purposes:
        sql += " AND purpose IN (" + ", ".join("?" * len(purposes)) + ")"
        params.extend(sorted(purposes))
    sql += " ORDER BY id ASC"
    return conn.execute(sql, params).fetchall()


def add_ask(conn, question, project=None, plan_id=None, options=None,
            destructive=False, action=None, dedupe_any_state=False) -> int:
    if plan_id is not None:
        plan = conn.execute(
            "SELECT project FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise ValueError(f"no plan {plan_id}")
        if project is not None and project != plan["project"]:
            raise ValueError(
                f"plan {plan_id} belongs to {plan['project']}, not {project}"
            )
        project = plan["project"]
    elif project is not None:
        _require_project(conn, project)
    if isinstance(options, (list, tuple)):
        options = "|".join(str(option) for option in options)
    if action is not None:
        sql = "SELECT id FROM asks WHERE action = ?"
        params = [action]
        if not dedupe_any_state:
            sql += " AND state = 'OPEN'"
        sql += " ORDER BY id ASC LIMIT 1"
        existing = conn.execute(sql, params).fetchone()
        if existing is not None:
            return existing["id"]
    with conn:
        cur = conn.execute(
            "INSERT INTO asks (project, plan_id, question, options, destructive, state, "
            "created_at, action) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?)",
            (project, plan_id, question, options, int(bool(destructive)), util.now(),
             action),
        )
        ask_id = cur.lastrowid
        db.add_event(
            conn, "ask", f"ask #{ask_id}: {question}", project=project, plan_id=plan_id,
        )
    return ask_id


def pending_answers(conn):
    return conn.execute(
        "SELECT * FROM asks WHERE state = 'ANSWERED' AND action IS NOT NULL "
        "AND handled_at IS NULL ORDER BY id ASC"
    ).fetchall()


def mark_handled(conn, ask_id) -> None:
    row = conn.execute("SELECT 1 FROM asks WHERE id = ?", (ask_id,)).fetchone()
    if row is None:
        raise ValueError(f"no ask {ask_id}")
    with conn:
        conn.execute(
            "UPDATE asks SET handled_at = ? WHERE id = ?", (util.now(), ask_id)
        )


def block_plan(conn, project, ref, question, action=None) -> int:
    row = get_plan(conn, project, ref)
    if action is None:
        action = f"unblock:{project}/{ref}"
    first_line = question.splitlines()[0] if question else ""
    if row["state"] != "BLOCKED":
        move_plan(conn, project, ref, "BLOCKED", note=first_line)
    return add_ask(conn, question, project=project, plan_id=row["id"], action=action)


def answer_ask(conn, ask_id, answer) -> None:
    row = conn.execute(
        "SELECT project, plan_id, question, state FROM asks WHERE id = ?", (ask_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no ask {ask_id}")
    if row["state"] != "OPEN":
        raise ValueError(f"ask #{ask_id} is already {row['state']}")
    with conn:
        conn.execute(
            "UPDATE asks SET state = 'ANSWERED', answer = ?, answered_at = ? "
            "WHERE id = ?",
            (answer, util.now(), ask_id),
        )
        db.add_event(
            conn, "answer", f"ask #{ask_id} answered: {answer}",
            project=row["project"], plan_id=row["plan_id"],
        )


def take_lock(conn, name, holder, note=None) -> bool:
    row = conn.execute(
        "SELECT holder FROM locks WHERE name = ?", (name,)
    ).fetchone()
    if row is not None:
        return row["holder"] == holder
    try:
        with conn:
            conn.execute(
                "INSERT INTO locks (name, holder, note, acquired_at) VALUES (?, ?, ?, ?)",
                (name, holder, note, util.now()),
            )
            text = f"lock {name} taken by {holder}"
            if note:
                text += f": {note}"
            db.add_event(conn, "lock", text)
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" not in str(exc):
            raise
        return False
    return True


def drop_lock(conn, name, holder, force=False) -> None:
    row = conn.execute(
        "SELECT holder FROM locks WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return
    forced = row["holder"] != holder
    if forced and not force:
        raise ValueError(
            f"lock {name} is held by {row['holder']}, not {holder}"
        )
    with conn:
        conn.execute("DELETE FROM locks WHERE name = ?", (name,))
        text = f"lock {name} dropped by {holder}"
        if forced:
            text += f" (forced, held by {row['holder']})"
        db.add_event(conn, "lock", text)


def add_note(conn, text, project=None, plan_id=None, kind="note") -> None:
    if kind not in NOTE_KINDS:
        raise ValueError(
            f"unknown note kind {kind}; allowed: {', '.join(NOTE_KINDS)}"
        )
    if project is not None:
        _require_project(conn, project)
    db.add_event(conn, kind, text, project=project, plan_id=plan_id)


def month_spend(conn, month=None) -> float:
    if month is None:
        month = util.now()[:7]
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM runs "
        "WHERE kind = 'implementer' AND started_at LIKE ?",
        (f"{month}%",),
    ).fetchone()
    return float(row[0])


def events_since(conn, after_id, limit=200, kinds=None):
    sql = "SELECT * FROM events WHERE id > ?"
    params = [after_id]
    if kinds:
        sql += " AND kind IN (" + ", ".join("?" * len(kinds)) + ")"
        params.extend(sorted(kinds))
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()
