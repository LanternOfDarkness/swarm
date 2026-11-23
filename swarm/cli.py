"""Command line interface for the swarm board."""

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

from swarm import (
    board, config, connect, dashboard, db, gitops, gitscan, mdsync, procs,
    runner, scheduler, states, status, telegram, usage, util,
)

WATCH_KINDS = "ask,answer,move,decision"


def _plan_ref(value):
    if "/" not in value:
        raise ValueError(f"bad plan reference {value!r}: expected PROJECT/REF")
    project, ref = value.split("/", 1)
    if not project or not ref:
        raise ValueError(f"bad plan reference {value!r}: expected PROJECT/REF")
    return project, ref


def _plan_row(conn, value):
    project, ref = _plan_ref(value)
    return board.get_plan(conn, project, ref)


def _plan_id(conn, value):
    return _plan_row(conn, value)["id"]


def _event_line(row):
    return f"#{row['id']} {row['ts']} {row['kind']} {row['text']}"


def _plan_fields(args):
    fields = {}
    mapping = {
        "path": "path",
        "branch": "branch",
        "worktree": "worktree",
        "base": "base_sha",
        "merge": "merge_sha",
        "goal": "goal_id",
    }
    for attr, column in mapping.items():
        value = getattr(args, attr, None)
        if value is not None:
            fields[column] = value
    return fields


def _refresh_dashboard(conn, cfg):
    try:
        dashboard.write(conn, cfg)
    except Exception as exc:  # noqa: BLE001 - a dashboard failure never fails a command
        print(f"swarm: warning: dashboard: {exc}", file=sys.stderr)


def _minutes_since(ts):
    try:
        delta = util.parse_ts(util.now()) - util.parse_ts(ts)
    except (TypeError, ValueError):
        return 0
    return max(0, int(delta.total_seconds() // 60))


def _human_age(ts):
    if ts is None:
        return "-"
    minutes = _minutes_since(ts)
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


def _seconds_since(ts):
    try:
        delta = util.parse_ts(util.now()) - util.parse_ts(ts)
    except (TypeError, ValueError):
        return None
    return max(0, int(delta.total_seconds()))


def _project_scope(conn, project):
    if project is not None:
        row = conn.execute(
            "SELECT name FROM projects WHERE name = ?", (project,)
        ).fetchone()
        if row is None:
            raise LookupError(f"no project {project}")
        return [row["name"]]
    rows = conn.execute("SELECT name FROM projects ORDER BY name").fetchall()
    return [row["name"] for row in rows]


def _worktree_line(row, plan_state):
    if row["missing"]:
        changes = "missing"
    elif row["dirty"] == 0:
        changes = "clean"
    else:
        changes = f"{row['dirty']} changes"
    target = (
        "-" if row["target_bytes"] is None else f"{row['target_bytes'] / 1e9:.1f} GB"
    )
    ahead = "?" if row["ahead"] is None else row["ahead"]
    behind = "?" if row["behind"] is None else row["behind"]
    state = gitscan.worktree_status(row, plan_state)
    branch = row["branch"] or "detached"
    plan_ref = row["plan_ref"] or "-"
    return (
        f"{row['project']}  {row['path']}  {branch}  plan {plan_ref}  "
        f"+{ahead}/-{behind}  {changes}  target {target}  {state}"
    )


def _parse_kinds(value):
    if value is None or value.strip().lower() == "all":
        return None
    return tuple(kind.strip() for kind in value.split(",") if kind.strip())


def watch_once(conn, since, kinds):
    """Return (formatted line or None, last event id after ``since``)."""
    rows = conn.execute(
        "SELECT * FROM events WHERE id > ? ORDER BY id ASC", (since,)
    ).fetchall()
    if not rows:
        return None, since
    last_id = rows[-1]["id"]
    matching = [row for row in rows if kinds is None or row["kind"] in kinds]
    if not matching:
        return None, last_id
    first = matching[0]["id"]
    last = matching[-1]["id"]
    text = " · ".join(row["text"] for row in matching)
    line = f"#{first}-#{last} {len(matching)} events: {text}"
    if len(line) > 400:
        line = line[:400] + "…"
    return line, last_id


def _watch(conn, args):
    since = args.since
    if since is None:
        since = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM events"
        ).fetchone()[0]
    kinds = _parse_kinds(args.kinds)
    interval = args.interval
    try:
        while True:
            poll = db.connect()
            try:
                line, since = watch_once(poll, since, kinds)
            finally:
                poll.close()
            if line is not None:
                print(line, flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _cmd_init(conn, cfg, args):
    database = util.home() / "swarm.db"
    print(database)
    _refresh_dashboard(conn, cfg)
    print(util.home() / "dashboard.html")
    return 0


def _cmd_project_add(conn, cfg, args):
    board.add_project(
        conn,
        args.name,
        args.repo,
        plans_dir=args.plans_dir,
        gates_file=args.gates_file,
        verify_cmd=args.verify_cmd,
        integration_branch=args.branch,
    )
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_project_list(conn, cfg, args):
    rows = conn.execute(
        "SELECT name, repo_path, integration_branch, auto, implementer, archived "
        "FROM projects ORDER BY archived, name"
    ).fetchall()
    for row in rows:
        auto = "on" if row["auto"] else "off"
        line = (
            f"{row['name']} {row['repo_path']} {row['integration_branch']} "
            f"auto {auto} implementer {row['implementer']}"
        )
        if row["archived"]:
            line += " archived"
        print(line)
    return 0


def _project_row(conn, name):
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise LookupError(f"no project {name}")
    return row


def _cmd_project_set(conn, cfg, args):
    row = _project_row(conn, args.name)
    if args.rule and args.no_rules:
        raise ValueError("--rule and --no-rules cannot be combined")
    auto = None
    if args.auto is not None:
        auto = args.auto == "on"
    if auto:
        if not gitops.guard_installed(row):
            raise ValueError(
                f"install the push guard first: swarm project guard {args.name}"
            )
        verify_cmd = (
            args.verify_cmd if args.verify_cmd is not None else row["verify_cmd"]
        )
        verify_allow = (
            args.verify_allow
            if args.verify_allow is not None
            else row["verify_allow"]
        )
        if not verify_cmd or not verify_allow:
            raise ValueError(
                "set --verify-cmd and --verify-allow first "
                "(swarm connect suggests them)"
            )
    if args.no_rules:
        rules = []
    elif args.rule:
        rules = args.rule
    else:
        rules = None
    board.set_project(
        conn,
        args.name,
        auto=auto,
        verify_cmd=args.verify_cmd,
        verify_allow=args.verify_allow,
        implementer=args.implementer,
        rules=rules,
    )
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_project_guard(conn, cfg, args):
    row = _project_row(conn, args.name)
    hook = gitops.install_guard(row)
    _refresh_dashboard(conn, cfg)
    print(hook)
    return 0


def _connect_summary(found, import_plans, done):
    print(f"repo {found['repo']}")
    print(f"name {found['name']}")
    print(f"branch {found['branch']}")
    if found["plans_dir_exists"]:
        print(f"plans dir {found['plans_dir']}")
    else:
        print("plans dir (will be created by the first plan)")
    if found["gates_file"]:
        print(f"gates file {found['gates_file']}")
    else:
        print("gates file none (plans go MERGED → DONE)")
    if found["verify_cmd"]:
        print(f"verify command {found['verify_cmd']}")
    else:
        print("verify command none found")
    print("implementer omp")
    print(f"plan files: {len(found['plans'])} found")
    if import_plans:
        for plan in found["plans"]:
            print(f"  {plan['ref']} {connect.import_state(plan, done)}")
    print("auto: off")


def _cmd_connect(conn, cfg, args):
    found = connect.detect(args.path, name=args.name, branch=args.branch)
    done = frozenset()
    if args.import_plans and args.done:
        done = connect.parse_refs(args.done)
    _connect_summary(found, args.import_plans, done)
    if args.dry_run:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("rerun with --yes to connect")
            return 1
        try:
            answer = input("Connect? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    lines = connect.apply(
        conn,
        found,
        branch=args.branch,
        verify_cmd=args.verify_cmd,
        verify_allow=args.verify_allow,
        import_plans=args.import_plans,
        done=done,
    )
    for line in lines:
        print(line)
    return 0


def _active_disconnect(conn, name):
    plans = [
        row["ref"]
        for row in conn.execute(
            "SELECT ref FROM plans WHERE project = ? "
            "AND state IN ('EXECUTING', 'FIXING', 'VERIFYING') ORDER BY ref",
            (name,),
        ).fetchall()
    ]
    runs = [
        row["id"]
        for row in conn.execute(
            "SELECT r.id FROM runs r JOIN plans p ON p.id = r.plan_id "
            "WHERE p.project = ? AND r.status = 'RUNNING' ORDER BY r.id",
            (name,),
        ).fetchall()
    ]
    parts = []
    if plans:
        parts.append("active plans: " + ", ".join(plans))
    if runs:
        parts.append("active runs: " + ", ".join(f"#{run}" for run in runs))
    return parts


def _cmd_disconnect(conn, cfg, args):
    row = _project_row(conn, args.name)
    problems = _active_disconnect(conn, args.name)
    if problems:
        raise ValueError(f"project {args.name} has " + "; ".join(problems))
    if not args.yes:
        if not sys.stdin.isatty():
            print("rerun with --yes to disconnect")
            return 1
        try:
            answer = input("Disconnect? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    board.archive_project(conn, args.name)
    gitops.remove_guard(row)
    _refresh_dashboard(conn, cfg)
    print(f"disconnected {args.name}; history kept")
    return 0


def _cmd_goal_add(conn, cfg, args):
    goal_id = board.add_goal(
        conn,
        args.project,
        args.title,
        args.done_when,
        priority=args.priority,
        est_minutes=args.est_minutes,
    )
    _refresh_dashboard(conn, cfg)
    print(goal_id)
    return 0


def _cmd_goal_set(conn, cfg, args):
    board.set_goal_state(conn, args.id, args.state)
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_goal_list(conn, cfg, args):
    sql = "SELECT * FROM goals"
    params = []
    conditions = []
    if args.project is not None:
        conditions.append("project = ?")
        params.append(args.project)
    if not args.all:
        conditions.append("state NOT IN ('DONE', 'DROPPED')")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY priority DESC, id"
    for row in conn.execute(sql, params):
        print(
            f"#{row['id']} {row['project']} {row['state']} {row['title']} "
            f"— done when: {row['done_when']}"
        )
    return 0


def _cmd_plan_add(conn, cfg, args):
    project, ref = _plan_ref(args.ref)
    plan_id = board.add_plan(
        conn, project, ref, args.title, path=args.path, goal_id=args.goal
    )
    _refresh_dashboard(conn, cfg)
    print(plan_id)
    return 0


def _cmd_plan_move(conn, cfg, args):
    project, ref = _plan_ref(args.ref)
    board.move_plan(
        conn, project, ref, args.state, note=args.note, **_plan_fields(args)
    )
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_plan_set(conn, cfg, args):
    project, ref = _plan_ref(args.ref)
    if args.goal is not None:
        goal = conn.execute(
            "SELECT 1 FROM goals WHERE id = ?", (args.goal,)
        ).fetchone()
        if goal is None:
            raise ValueError(f"no goal {args.goal}")
    board.set_plan_fields(conn, project, ref, **_plan_fields(args))
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_plan_show(conn, cfg, args):
    row = _plan_row(conn, args.ref)
    for key in row.keys():
        print(f"{key}: {row[key]}")
    runs = conn.execute(
        "SELECT * FROM runs WHERE plan_id = ? ORDER BY id", (row["id"],)
    ).fetchall()
    print("")
    print("runs:")
    for run in runs:
        line = f"- run #{run['id']} {run['kind']} {run['status']}"
        if run["cost_usd"]:
            line += f" ${run['cost_usd']:.2f}"
        if run["summary"]:
            line += f": {run['summary']}"
        print(line)
    events = conn.execute(
        "SELECT * FROM events WHERE plan_id = ? ORDER BY id DESC LIMIT 10",
        (row["id"],),
    ).fetchall()
    print("")
    print("events:")
    for event in reversed(events):
        print(_event_line(event))
    return 0


def _cmd_plan_list(conn, cfg, args):
    sql = "SELECT * FROM plans"
    params = []
    conditions = []
    if args.project is not None:
        conditions.append("project = ?")
        params.append(args.project)
    if args.state is not None:
        conditions.append("state = ?")
        params.append(args.state)
    if not args.all:
        conditions.append("state NOT IN ('DONE', 'DROPPED')")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY project, ref"
    for row in conn.execute(sql, params):
        print(f"{row['project']}/{row['ref']} {row['state']} {row['title']}")
    return 0


def _cmd_run_start(conn, cfg, args):
    plan_id = _plan_id(conn, args.plan) if args.plan is not None else None
    run_id = board.start_run(
        conn, args.kind, plan_id=plan_id, session=args.session, log_path=args.log
    )
    _refresh_dashboard(conn, cfg)
    print(run_id)
    return 0


def _cmd_run_end(conn, cfg, args):
    board.end_run(
        conn,
        args.id,
        args.status,
        cost_usd=args.cost,
        summary=args.summary,
        session=args.session,
    )
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_runs(conn, cfg, args):
    sql = (
        "SELECT r.*, p.project AS plan_project, p.ref AS plan_ref "
        "FROM runs r LEFT JOIN plans p ON p.id = r.plan_id"
    )
    params = []
    conditions = []
    if args.plan is not None:
        conditions.append("r.plan_id = ?")
        params.append(_plan_id(conn, args.plan))
    if not args.all:
        conditions.append("r.status = 'RUNNING'")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if args.all:
        sql += " ORDER BY r.id DESC LIMIT 30"
        rows = list(reversed(conn.execute(sql, params).fetchall()))
    else:
        sql += " ORDER BY r.id"
        rows = conn.execute(sql, params).fetchall()
    for row in rows:
        plan = (
            f"{row['plan_project']}/{row['plan_ref']}"
            if row["plan_project"] and row["plan_ref"] else "-"
        )
        log = row["log_path"]
        if not log:
            log = str(procs.run_paths(row["id"])[0])
        print(
            f"{row['id']}  {row['kind']}  {row['purpose'] or '-'}  {plan}  "
            f"{row['pid'] or '-'}  {_human_age(row['started_at'])}  "
            f"{row['status']}  ${row['cost_usd']:.2f}  {log}"
        )
    return 0


def _cmd_ask(conn, cfg, args):
    plan_id = _plan_id(conn, args.plan) if args.plan is not None else None
    ask_id = board.add_ask(
        conn,
        args.question,
        project=args.project,
        plan_id=plan_id,
        options=args.options,
        destructive=args.destructive,
    )
    _refresh_dashboard(conn, cfg)
    print(ask_id)
    return 0


def _cmd_answer(conn, cfg, args):
    board.answer_ask(conn, args.id, args.text)
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_asks(conn, cfg, args):
    rows = conn.execute(
        "SELECT a.*, p.project AS plan_project, p.ref AS plan_ref "
        "FROM asks a LEFT JOIN plans p ON p.id = a.plan_id "
        "WHERE a.state = 'OPEN' ORDER BY a.id"
    ).fetchall()
    for row in rows:
        label = (
            f"{row['plan_project']}/{row['plan_ref']}"
            if row["plan_project"] and row["plan_ref"]
            else row["project"]
        )
        line = f"#{row['id']}"
        if label:
            line += f" ({label})"
        line += f": {row['question']}"
        if row["options"]:
            line += f" — options: {row['options']}"
        if row["destructive"]:
            line += " [destructive]"
        print(line)
    return 0


def _cmd_lock_take(conn, cfg, args):
    if not board.take_lock(conn, args.name, args.holder, note=args.note):
        row = conn.execute(
            "SELECT holder FROM locks WHERE name = ?", (args.name,)
        ).fetchone()
        holder = row["holder"] if row is not None else "unknown"
        print(f"swarm: lock {args.name} held by {holder}", file=sys.stderr)
        return 1
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_lock_drop(conn, cfg, args):
    board.drop_lock(conn, args.name, args.holder, force=args.force)
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_lock_list(conn, cfg, args):
    rows = conn.execute(
        "SELECT name, holder, acquired_at, note FROM locks ORDER BY name"
    ).fetchall()
    for row in rows:
        line = f"{row['name']} {row['holder']} {row['acquired_at']}"
        if row["note"]:
            line += f" ({row['note']})"
        print(line)
    return 0


def _cmd_note(conn, cfg, args):
    plan_id = _plan_id(conn, args.plan) if args.plan is not None else None
    board.add_note(
        conn, args.text, project=args.project, plan_id=plan_id, kind=args.kind
    )
    _refresh_dashboard(conn, cfg)
    return 0


def _cmd_brief(conn, cfg, args):
    print(status.format_brief(conn, cfg, args.project, max_lines=args.max_lines))
    return 0


def _cmd_log(conn, cfg, args):
    if args.since is None:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.limit,)
        ).fetchall()
        rows = list(reversed(rows))
    else:
        rows = board.events_since(conn, args.since, limit=args.limit)
    for row in rows:
        print(_event_line(row))
    return 0


def _cmd_runner_check(conn, cfg, args):
    row = conn.execute(
        "SELECT 1 FROM projects WHERE name = ?", (args.name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no project {args.name}")
    path = runner.config_path(args.name)
    data = runner.load(args.name)
    if data is None:
        print(f"no runner config at {path}")
        return 0
    for name in sorted(data["steps"]):
        step = data["steps"][name]
        locks = ", ".join(step["locks"]) or "-"
        print(f"step {name}: {step['timeout_s'] // 60}m, locks {locks}")
    for name in sorted(data["windows"]):
        window = data["windows"][name]
        before = ", ".join(window["before"]) or "-"
        production = "yes" if window["production"] else "no"
        print(
            f"window {name}: {window['length_s'] // 60}m, before {before}, "
            f"production {production}"
        )
    state = "verified" if data["safety_stop"] else "not verified"
    print(f"safety stop: {state}")
    return 0


def _cmd_status(conn, cfg, args):
    data = status.build_status(conn, cfg, args.project)
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(status.format_status(data))
    return 0


def _cmd_dashboard(conn, cfg, args):
    path = dashboard.write(conn, cfg, args.out)
    print(path)
    if args.open:
        explorer = shutil.which("explorer.exe")
        wslpath = shutil.which("wslpath")
        if explorer and wslpath:
            try:
                converted = subprocess.run(
                    [wslpath, "-w", str(path)],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                subprocess.run([explorer, converted])
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"swarm: warning: open: {exc}", file=sys.stderr)
    return 0


def _cmd_scan(conn, cfg, args):
    failed = False
    succeeded = False
    for name in _project_scope(conn, args.project):
        try:
            result = gitscan.scan_project(conn, name, sizes=not args.no_sizes)
        except (ValueError, LookupError, OSError, subprocess.SubprocessError) as exc:
            print(f"swarm: {exc}", file=sys.stderr)
            failed = True
            continue
        succeeded = True
        print(result["text"])
        states = gitscan.plan_states(conn, name)
        for row in result["worktrees"]:
            print(_worktree_line(row, states.get((name, row["plan_ref"]))))
    if succeeded:
        _refresh_dashboard(conn, cfg)
    return 1 if failed else 0


def _cmd_worktrees(conn, cfg, args):
    blocks = []
    for name in _project_scope(conn, args.project):
        trees = conn.execute(
            "SELECT * FROM worktrees WHERE project = ? ORDER BY is_main DESC, "
            "last_commit_at IS NULL, last_commit_at DESC, path",
            (name,),
        ).fetchall()
        branches = conn.execute(
            "SELECT * FROM branches WHERE project = ? ORDER BY merged, name",
            (name,),
        ).fetchall()
        stamps = [row["scanned_at"] for row in trees]
        stamps.extend(row["scanned_at"] for row in branches)
        if not stamps:
            blocks.append(f"{name}  never scanned — run swarm scan")
            continue
        lines = [f"{name}  scanned {_minutes_since(max(stamps))} min ago"]
        states = gitscan.plan_states(conn, name)
        lines.extend(
            _worktree_line(row, states.get((name, row["plan_ref"])))
            for row in trees
        )
        unmerged = [row for row in branches if not row["merged"]]
        if unmerged:
            lines.append("loose branches:")
            for row in unmerged:
                ref = row["plan_ref"] or "-"
                ahead = "?" if row["ahead"] is None else row["ahead"]
                lines.append(f"  {row['name']}  plan {ref}  +{ahead}")
        merged = sum(
            1 for row in branches
            if gitscan.branch_deletable(
                row, states.get((name, row["plan_ref"]))
            )
        )
        if merged:
            lines.append(f"{merged} merged branches can be deleted")
        blocks.append("\n".join(lines))
    if blocks:
        print("\n\n".join(blocks))
    return 0


def _usage_dates(args):
    today = date.fromisoformat(util.now()[:10])
    since = args.since or (today - timedelta(days=7)).isoformat()
    until = args.until or (today + timedelta(days=1)).isoformat()
    return since, until


def _subagent_rows(records):
    """Aggregate subagent records per ``(session, agent)`` transcript."""
    groups = {}
    for record in records:
        if record["kind"] != "subagent":
            continue
        key = (record["session"], record["agent"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "day": record["day"],
                "session": record["session"],
                "agent": record["agent"],
                "models": {},
                "calls": 0,
                "weighted": 0,
                "description": "",
            }
        if record["day"] < group["day"]:
            group["day"] = record["day"]
        models = group["models"]
        models[record["model"]] = (
            models.get(record["model"], 0) + record["weighted"]
        )
        group["calls"] += 1
        group["weighted"] += record["weighted"]
        if not group["description"] and record.get("description"):
            group["description"] = record["description"]
    rows = []
    for group in groups.values():
        model = min(
            group.pop("models").items(), key=lambda item: (-item[1], item[0])
        )[0]
        group["model"] = model
        rows.append(group)
    rows.sort(
        key=lambda row: (
            -row["weighted"], row["day"], row["session"], row["agent"],
        ),
    )
    return rows


def _cmd_usage(conn, cfg, args):
    since, until = _usage_dates(args)
    root = Path(cfg.claude_projects_dir)
    commits = {}
    if args.project:
        row = _project_row(conn, args.project)
        repo_path = row["repo_path"]
        dirs = usage.project_dirs(root, repo_path)
        commits = usage.commits_per_day(
            repo_path, row["integration_branch"], since, until
        )
    else:
        dirs = usage.project_dirs(root, None)
    records = usage.collect(dirs, since, until)
    summary = usage.summarise(records)
    subagents = None
    if args.subagents:
        subagents = _subagent_rows(records)
    if args.json:
        print(json.dumps({
            "summary": summary,
            "commits": commits,
            "subagents": subagents,
        }))
    else:
        print(usage.format_report(summary, commits, subagents))
    return 0


def _cmd_sync_md(conn, cfg, args):
    project, ref = _plan_ref(args.ref)
    path = mdsync.sync(conn, project, ref)
    _refresh_dashboard(conn, cfg)
    print(path)
    return 0


def _pid_from_holder(holder):
    if holder and holder.startswith("pid:"):
        try:
            return int(holder[4:])
        except ValueError:
            return None
    return None


def _pid_alive(pid):
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    fields = stat.rsplit(")", 1)[-1].split()
    return bool(fields) and fields[0] != "Z"


def _take_pid_lock(conn, name, holder):
    """Take a pid lock; return the blocking holder, or None on success."""
    row = conn.execute(
        "SELECT holder FROM locks WHERE name = ?", (name,)
    ).fetchone()
    if row is not None:
        if _pid_alive(_pid_from_holder(row["holder"])):
            return row["holder"]
        board.drop_lock(conn, name, holder, force=True)
    if not board.take_lock(conn, name, holder):
        row = conn.execute(
            "SELECT holder FROM locks WHERE name = ?", (name,)
        ).fetchone()
        return row["holder"] if row is not None else "unknown"
    return None


def _heartbeat_path():
    return util.home() / "daemon.heartbeat"


def _daemon_tick(conn, cfg, launcher):
    """Run one tick; return False (after reporting) when the tick raised."""
    try:
        actions = scheduler.tick(conn, cfg, launcher)
    except Exception:  # noqa: BLE001 - a bad tick must not kill the daemon
        print(f"[{util.now()}] daemon tick failed:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        _heartbeat_path().write_text(util.now())
        return False
    for action in actions:
        print(f"[{util.now()}] {action}", flush=True)
    _heartbeat_path().write_text(util.now())
    if actions:
        try:
            dashboard.write(conn, cfg)
        except Exception as exc:  # noqa: BLE001 - never fail the daemon
            print(f"swarm: warning: dashboard: {exc}", file=sys.stderr)
    return True


def _cmd_daemon(conn, cfg, args):
    interval = args.interval if args.interval is not None else cfg.daemon_interval_s
    if not interval > 0:
        print(
            f"swarm: --interval must be positive (got {interval})", file=sys.stderr
        )
        return 1
    holder = f"pid:{os.getpid()}"
    blocking = _take_pid_lock(conn, "daemon", holder)
    if blocking is not None:
        pid = _pid_from_holder(blocking)
        if pid is None:
            print(f"daemon already running ({blocking})", file=sys.stderr)
        else:
            print(f"daemon already running (pid {pid})", file=sys.stderr)
        return 1
    launcher = procs.Launcher()
    state = {"stop": False}

    def stop(signum, frame):
        state["stop"] = True

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, stop)
        except (ValueError, OSError):
            pass
    try:
        while not state["stop"]:
            ok = _daemon_tick(conn, cfg, launcher)
            if args.once:
                return 0 if ok else 1
            remaining = float(interval)
            while remaining > 0 and not state["stop"]:
                step = min(0.5, remaining)
                time.sleep(step)
                remaining -= step
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        try:
            board.drop_lock(conn, "daemon", holder)
        except (ValueError, sqlite3.Error) as exc:
            print(f"swarm: warning: daemon lock: {exc}", file=sys.stderr)


def _cmd_telegram(conn, cfg, args):
    token, chat_id = telegram.load_config(util.home())
    holder = f"pid:{os.getpid()}"
    blocking = _take_pid_lock(conn, "telegram", holder)
    if blocking is not None:
        pid = _pid_from_holder(blocking)
        if pid is None:
            print(f"telegram bot already running ({blocking})", file=sys.stderr)
        else:
            print(f"telegram bot already running (pid {pid})", file=sys.stderr)
        return 1
    state = {"stop": False}

    def stop(signum, frame):
        state["stop"] = True

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, stop)
        except (ValueError, OSError):
            pass
    api = telegram.TelegramApi(token)
    try:
        telegram.run(conn, cfg, api, chat_id, util.home() / "telegram.json",
                     once=args.once, stop=lambda: state["stop"])
        return 0
    except telegram.TelegramError as exc:
        if exc.code == 409:
            print("another process is polling this bot", file=sys.stderr)
        elif exc.code == 401:
            print("bot token rejected", file=sys.stderr)
        else:
            print(f"swarm: telegram: {telegram._mask(str(exc))}",
                  file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        try:
            board.drop_lock(conn, "telegram", holder)
        except (ValueError, sqlite3.Error) as exc:
            print(f"swarm: warning: telegram lock: {exc}", file=sys.stderr)


class _DryLauncher:
    """A launcher for --dry-run that only prints what it would start."""

    def launch(self, run_id, argv, cwd, env):
        keys = ("SWARM_WORKER", "SWARM_RUN_ID")
        summary = " ".join(f"{key}={env.get(key)}" for key in keys)
        print(
            f"[{util.now()}] dry-run: would launch run #{run_id}: "
            f"{' '.join(str(part) for part in argv)} (cwd {cwd}, {summary})"
        )
        return 900000 + int(run_id)

    def alive(self, pid):
        return True

    def kill(self, pid):
        print(f"[{util.now()}] dry-run: would kill pid {pid}")


class _DryGit:
    """Reads pass through to gitops; writes only print and return fakes."""

    _SHA = "0" * 40

    def __getattr__(self, name):
        if name in ("create_worktree", "update_branch", "merge_into_main",
                    "cleanup"):
            return getattr(self, f"_dry_{name}")
        return getattr(gitops, name)

    def _dry_create_worktree(self, project, plan):
        path = gitops.worktree_path(project, plan["ref"])
        branch = f"plan/{plan['ref']}-dry"
        print(
            f"[{util.now()}] dry-run: would create worktree {path} "
            f"(branch {branch})"
        )
        return path, branch, self._SHA

    def _dry_update_branch(self, project, plan):
        print(
            f"[{util.now()}] dry-run: would merge the integration branch into "
            f"{plan['worktree']}"
        )
        return self._SHA

    def _dry_merge_into_main(self, project, plan, expected_ib_sha):
        print(
            f"[{util.now()}] dry-run: would merge {plan['branch']} into the "
            "integration branch"
        )
        return self._SHA

    def _dry_cleanup(self, project, plan):
        print(
            f"[{util.now()}] dry-run: would remove the worktree of "
            f"{plan['project']}/{plan['ref']}"
        )


_RUN_ARTIFACT_PATTERNS = ("*.log", "*.exit", "*-fix.md")


def _copy_run_artifacts(source, target):
    """Copy real run artifacts into the dry-run home so previews can see them."""
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for pattern in _RUN_ARTIFACT_PATTERNS:
        for path in source.glob(pattern):
            if path.is_file():
                shutil.copy2(path, target / path.name)


def _daemon_dry_run():
    cfg = config.load()
    source = util.home() / "swarm.db"
    if not source.exists():
        return 0
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    tmp = None
    old_home = os.environ.get("SWARM_HOME")
    home_tmp = tempfile.mkdtemp(prefix="swarm-dry-run-home-")
    try:
        _copy_run_artifacts(util.home() / "runs", Path(home_tmp) / "runs")
        handle, name = tempfile.mkstemp(prefix="swarm-dry-run-", suffix=".db")
        os.close(handle)
        tmp = Path(name)
        copy = sqlite3.connect(tmp)
        try:
            src.backup(copy)
            copy.row_factory = sqlite3.Row
            copy.execute("PRAGMA foreign_keys=ON")
            db.migrate(copy)
            copy.execute(f"PRAGMA user_version={db.VERSION}")
            copy.commit()
            os.environ["SWARM_HOME"] = home_tmp
            try:
                actions = scheduler.tick(
                    copy, cfg, _DryLauncher(), git=_DryGit(), now=util.now()
                )
            finally:
                if old_home is None:
                    os.environ.pop("SWARM_HOME", None)
                else:
                    os.environ["SWARM_HOME"] = old_home
            for action in actions:
                print(f"[{util.now()}] {action}")
        finally:
            copy.close()
    finally:
        src.close()
        shutil.rmtree(home_tmp, ignore_errors=True)
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="swarm")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    connect_parser = sub.add_parser("connect")
    connect_parser.add_argument("path", nargs="?", default=".")
    connect_parser.add_argument("--name")
    connect_parser.add_argument("--branch")
    connect_parser.add_argument("--verify-cmd")
    connect_parser.add_argument("--verify-allow")
    connect_parser.add_argument("--import-plans", action="store_true")
    connect_parser.add_argument("--done")
    connect_parser.add_argument("--yes", action="store_true")
    connect_parser.add_argument("--dry-run", action="store_true")

    disconnect = sub.add_parser("disconnect")
    disconnect.add_argument("name")
    disconnect.add_argument("--yes", action="store_true")

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    add = project_sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--repo", required=True)
    add.add_argument("--plans-dir", default="docs/plans")
    add.add_argument("--gates-file")
    add.add_argument("--verify-cmd")
    add.add_argument("--branch", default="main")
    project_sub.add_parser("list")
    setter = project_sub.add_parser("set")
    setter.add_argument("name")
    setter.add_argument("--auto", choices=("on", "off"))
    setter.add_argument("--verify-cmd")
    setter.add_argument("--verify-allow")
    setter.add_argument("--implementer", choices=("omp", "claude"))
    setter.add_argument("--rule", action="append")
    setter.add_argument("--no-rules", action="store_true")
    guard = project_sub.add_parser("guard")
    guard.add_argument("name")

    goal = sub.add_parser("goal")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)
    add = goal_sub.add_parser("add")
    add.add_argument("project")
    add.add_argument("title")
    add.add_argument("--done-when", required=True)
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--est-minutes", type=int)
    setter = goal_sub.add_parser("set")
    setter.add_argument("id", type=int)
    setter.add_argument("state", choices=states.GOAL_STATES)
    listing = goal_sub.add_parser("list")
    listing.add_argument("--project")
    listing.add_argument("--all", action="store_true")

    plan = sub.add_parser("plan")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    add = plan_sub.add_parser("add")
    add.add_argument("ref")
    add.add_argument("--title", required=True)
    add.add_argument("--path")
    add.add_argument("--goal", type=int)
    move = plan_sub.add_parser("move")
    move.add_argument("ref")
    move.add_argument("state", choices=states.PLAN_STATES)
    move.add_argument("--note")
    move.add_argument("--branch")
    move.add_argument("--worktree")
    move.add_argument("--base")
    move.add_argument("--merge")
    setter = plan_sub.add_parser("set")
    setter.add_argument("ref")
    setter.add_argument("--path")
    setter.add_argument("--branch")
    setter.add_argument("--worktree")
    setter.add_argument("--base")
    setter.add_argument("--merge")
    setter.add_argument("--goal", type=int)
    show = plan_sub.add_parser("show")
    show.add_argument("ref")
    listing = plan_sub.add_parser("list")
    listing.add_argument("--project")
    listing.add_argument("--state", choices=states.PLAN_STATES)
    listing.add_argument("--all", action="store_true")

    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    start = run_sub.add_parser("start")
    start.add_argument("kind", choices=states.RUN_KINDS)
    start.add_argument("--plan")
    start.add_argument("--session")
    start.add_argument("--log")
    end = run_sub.add_parser("end")
    end.add_argument("id", type=int)
    end.add_argument("status", choices=states.RUN_STATUSES)
    end.add_argument("--cost", type=float, default=0.0)
    end.add_argument("--summary")
    end.add_argument("--session")

    runs = sub.add_parser("runs")
    runs.add_argument("--plan")
    runs.add_argument("--all", action="store_true")

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--interval", type=float)
    daemon.add_argument("--dry-run", action="store_true")

    telegram_parser = sub.add_parser("telegram")
    telegram_parser.add_argument("--once", action="store_true")

    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--project")
    ask.add_argument("--plan")
    ask.add_argument("--options")
    ask.add_argument("--destructive", action="store_true")

    answer = sub.add_parser("answer")
    answer.add_argument("id", type=int)
    answer.add_argument("text")

    sub.add_parser("asks")

    hook = sub.add_parser("hook")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_sub.add_parser("session-start")
    hook_sub.add_parser("stop")

    lock = sub.add_parser("lock")
    lock_sub = lock.add_subparsers(dest="lock_command", required=True)
    take = lock_sub.add_parser("take")
    take.add_argument("name")
    take.add_argument("--holder", required=True)
    take.add_argument("--note")
    drop = lock_sub.add_parser("drop")
    drop.add_argument("name")
    drop.add_argument("--holder", required=True)
    drop.add_argument("--force", action="store_true")
    lock_sub.add_parser("list")

    note = sub.add_parser("note")
    note.add_argument("text")
    note.add_argument("--project")
    note.add_argument("--plan")
    note.add_argument("--kind", choices=board.NOTE_KINDS, default="note")

    brief = sub.add_parser("brief")
    brief.add_argument("--project")
    brief.add_argument("--max-lines", type=int, default=60)

    log = sub.add_parser("log")
    log.add_argument("--since", type=int)
    log.add_argument("--limit", type=int, default=30)

    watch = sub.add_parser("watch")
    watch.add_argument("--since", type=int)
    watch.add_argument("--interval", type=float, default=5.0)
    watch.add_argument("--kinds", default=WATCH_KINDS)

    state = sub.add_parser("status")
    state.add_argument("--project")
    state.add_argument("--json", action="store_true")

    board_parser = sub.add_parser("dashboard")
    board_parser.add_argument("--out")
    board_parser.add_argument("--open", action="store_true")

    sync = sub.add_parser("sync-md")
    sync.add_argument("ref")

    scan = sub.add_parser("scan")
    scan.add_argument("--project")
    scan.add_argument("--no-sizes", action="store_true")

    trees = sub.add_parser("worktrees")
    trees.add_argument("--project")

    usage_parser = sub.add_parser("usage")
    usage_parser.add_argument("--project")
    usage_parser.add_argument("--since")
    usage_parser.add_argument("--until")
    usage_parser.add_argument("--subagents", action="store_true")
    usage_parser.add_argument("--json", action="store_true")

    runner_parser = sub.add_parser("runner")
    runner_sub = runner_parser.add_subparsers(dest="runner_command", required=True)
    check = runner_sub.add_parser("check")
    check.add_argument("name")

    return parser


_COMMANDS = {
    "init": _cmd_init,
    "connect": _cmd_connect,
    "disconnect": _cmd_disconnect,
    "project_add": _cmd_project_add,
    "project_list": _cmd_project_list,
    "project_set": _cmd_project_set,
    "project_guard": _cmd_project_guard,
    "goal_add": _cmd_goal_add,
    "goal_set": _cmd_goal_set,
    "goal_list": _cmd_goal_list,
    "plan_add": _cmd_plan_add,
    "plan_move": _cmd_plan_move,
    "plan_set": _cmd_plan_set,
    "plan_show": _cmd_plan_show,
    "plan_list": _cmd_plan_list,
    "run_start": _cmd_run_start,
    "run_end": _cmd_run_end,
    "runs": _cmd_runs,
    "daemon": _cmd_daemon,
    "telegram": _cmd_telegram,
    "ask": _cmd_ask,
    "answer": _cmd_answer,
    "asks": _cmd_asks,
    "lock_take": _cmd_lock_take,
    "lock_drop": _cmd_lock_drop,
    "lock_list": _cmd_lock_list,
    "note": _cmd_note,
    "brief": _cmd_brief,
    "log": _cmd_log,
    "status": _cmd_status,
    "dashboard": _cmd_dashboard,
    "sync-md": _cmd_sync_md,
    "scan": _cmd_scan,
    "worktrees": _cmd_worktrees,
    "usage": _cmd_usage,
    "runner_check": _cmd_runner_check,
}


def _dispatch_name(args):
    if args.command == "project":
        return f"project_{args.project_command}"
    if args.command == "goal":
        return f"goal_{args.goal_command}"
    if args.command == "plan":
        return f"plan_{args.plan_command}"
    if args.command == "run":
        return f"run_{args.run_command}"
    if args.command == "lock":
        return f"lock_{args.lock_command}"
    if args.command == "runner":
        return f"runner_{args.runner_command}"
    return args.command


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 2

    if args.command == "hook":
        from swarm import hooks

        text = sys.stdin.read()
        if args.hook_command == "session-start":
            return hooks.session_start(text)
        return hooks.stop(text)

    if args.command == "daemon" and args.dry_run:
        try:
            return _daemon_dry_run()
        except (ValueError, LookupError, OSError, sqlite3.Error) as exc:
            print(f"swarm: {exc}", file=sys.stderr)
            return 1

    conn = db.connect()
    try:
        cfg = config.load()
        if args.command == "watch":
            return _watch(conn, args)
        handler = _COMMANDS[_dispatch_name(args)]
        return handler(conn, cfg, args)
    except (ValueError, LookupError, OSError) as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
