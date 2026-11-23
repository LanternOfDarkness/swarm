"""The scheduler tick: reap runs, finish them, answer asks, drive plans and goals."""

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from swarm import board, gitops, procs, runner, states, util, workers

PURPOSE_KIND = {
    "create": "architect",
    "revise": "architect",
    "review": "reviewer",
    "implement": "implementer",
    "fix": "implementer",
    "implement-claude": "implementer",
    "fix-claude": "implementer",
    "verify": "verifier",
    "preverify": "runner",
    "step": "runner",
    "window": "runner",
    "read": "runner",
    "judge": "verifier",
}
CLAUDE_PURPOSES = ("create", "revise", "review", "verify", "judge")
OMP_PURPOSES = ("implement", "fix")
CLAUDE_IMPL_PURPOSES = ("implement-claude", "fix-claude")
CLAUDE_KINDS = ("architect", "reviewer", "verifier")
TERMINAL_GOALS = ("DONE", "DROPPED")

_SKIP_LOG = {}


def reset_skip_log() -> None:
    """Forget which plans have had a skipped-launch line logged."""
    _SKIP_LOG.clear()


def tick(conn, cfg, launcher, git=gitops, now=None) -> list[str]:
    """Run one scheduler pass; return one human line per action."""
    now_ts = _now_str(now)
    actions = []
    ctx = {"claude": _claude_running(conn)}
    _reap(conn, cfg, launcher, git, now_ts, actions, ctx)
    _windows(conn, cfg, launcher, now_ts, actions)
    _answers(conn, cfg, launcher, git, actions)
    _plans(conn, cfg, launcher, git, ctx, now_ts, actions)
    _measure(conn, cfg, launcher, git, ctx, now_ts, actions)
    _goals(conn, cfg, actions, now_ts)
    return actions


def _now_str(now):
    if now is None:
        return util.now()
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).strftime(util.TS_FORMAT)
    return str(now)


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _write_report(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text)
    return str(path)


def _write_fix(run_id, text):
    log, _, _ = procs.run_paths(run_id)
    path = log.with_name(f"{run_id}-fix.md")
    path.write_text(text)
    return str(path)


def _label(plan):
    return f"{plan['project']}/{plan['ref']}" if plan is not None else "?"


def _project(conn, name):
    return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def _goal(conn, goal_id):
    return conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()


def _end(conn, actions, run, status, cost=0.0, summary=None, exit_code=None,
         report_path=None, session=None):
    board.end_run(conn, run["id"], status, cost_usd=cost, summary=summary,
                  session=session, exit_code=exit_code, report_path=report_path)
    line = f"run #{run['id']} {run['kind']} {status}"
    if summary:
        line += f": {summary}"
    actions.append(line)


def _move(conn, actions, plan, target, **fields):
    try:
        board.move_plan(conn, plan["project"], plan["ref"], target, **fields)
    except (LookupError, ValueError) as exc:
        actions.append(f"{_label(plan)} cannot move to {target}: {exc}")
        return False
    actions.append(f"{_label(plan)} → {target}")
    return True


def _block(conn, actions, plan, question, action=None):
    if plan is None:
        return
    try:
        board.block_plan(conn, plan["project"], plan["ref"], question, action=action)
    except (LookupError, ValueError) as exc:
        actions.append(f"cannot block {_label(plan)}: {exc}")
        return
    actions.append(f"blocked {_label(plan)}: {question.splitlines()[0]}")


def _launch(conn, cfg, launcher, actions, ctx, project, plan, purpose, argv, cwd,
            timeout_s, used_note=False, step=None):
    kind = PURPOSE_KIND[purpose]
    run_id = board.start_run(conn, kind, plan_id=plan["id"], timeout_s=timeout_s,
                             purpose=purpose, step=step)
    if argv[1] == cfg.omp_runner:
        log_dir = procs.run_paths(run_id)[0].parent / f"{run_id}-omp"
        argv = [*argv[:2], "--log-dir", str(log_dir), *argv[2:]]
    try:
        pid = launcher.launch(run_id, argv, cwd, procs.worker_env(cfg, run_id))
    except OSError as exc:
        board.end_run(conn, run_id, "FAILED", summary=f"cannot start: {exc}")
        _block(conn, actions, plan,
               f"cannot start {purpose} for {plan['project']}/{plan['ref']}: {exc}")
        return None
    board.set_run_pid(conn, run_id, pid)
    if used_note:
        board.set_plan_fields(conn, plan["project"], plan["ref"], operator_note=None)
    actions.append(f"launched {purpose} #{run_id} ({kind}) for {_label(plan)}")
    if kind in CLAUDE_KINDS or purpose in CLAUDE_IMPL_PURPOSES:
        ctx["claude"] += 1
    return run_id


def _skip(actions, plan, reason, now_ts):
    """Log a skipped launch at most once per plan and reason per hour."""
    key = (plan["id"], reason)
    when = util.parse_ts(now_ts).timestamp()
    previous = _SKIP_LOG.get(key)
    if previous is not None and when - previous < 3600:
        return
    _SKIP_LOG[key] = when
    actions.append(f"skip {_label(plan)}: {reason}")


def _claude_running(conn) -> int:
    return len(board.running_runs(conn, kinds=list(CLAUDE_KINDS),
                                  purposes=list(CLAUDE_IMPL_PURPOSES)))


def _claude_slot(cfg, ctx) -> bool:
    return ctx["claude"] < cfg.max_claude_jobs


# ---------------------------------------------------------------- A. reap

def _reap(conn, cfg, launcher, git, now_ts, actions, ctx):
    now_dt = util.parse_ts(now_ts)
    for run in board.running_runs(conn):
        try:
            if run["pid"] is None:
                continue
            exit_code = procs.read_exit(run["id"])
            if exit_code is not None:
                _finish(conn, cfg, launcher, git, run["id"], exit_code, actions, ctx)
                continue
            if not launcher.alive(run["pid"]):
                exit_code = procs.read_exit(run["id"])
                if exit_code is not None:
                    _finish(conn, cfg, launcher, git, run["id"], exit_code,
                            actions, ctx)
                    continue
                _fail_run(conn, actions, run, "FAILED", "process vanished", None)
                continue
            if run["timeout_s"] is None:
                continue
            runtime = (now_dt - util.parse_ts(run["started_at"])).total_seconds()
            if runtime > run["timeout_s"] + 300:
                launcher.kill(run["pid"])
                _fail_run(conn, actions, run, "FAILED", "timeout", None)
        except Exception as exc:
            actions.append(
                f"error on run #{run['id']}: {type(exc).__name__}: {exc}")


def _fail_run(conn, actions, run, status, summary, exit_code):
    _end(conn, actions, run, status, summary=summary, exit_code=exit_code)
    plan = _run_plan(conn, run)
    if plan is None:
        return
    log, _, _ = procs.run_paths(run["id"])
    purpose = run["purpose"] or run["kind"]
    question = (f"Run #{run['id']} ({run['kind']}, {purpose}) for {_label(plan)} "
                f"{summary}. Log: {log}")
    _block(conn, actions, plan, question)


def _run_plan(conn, run):
    if run["plan_id"] is None:
        return None
    return conn.execute("SELECT * FROM plans WHERE id = ?", (run["plan_id"],)).fetchone()


def _tail(path, limit=256 * 1024):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _append(path, text):
    if not text:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def _stop_window(cfg, project, window_cfg, run, log, actions):
    stop = window_cfg["stop"]
    if not stop:
        return
    try:
        proc = subprocess.run(
            ["sh", "-c", stop], cwd=project["repo_path"],
            env=procs.worker_env(cfg, run["id"]), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        actions.append(f"stop command for {run['step']} failed: timeout")
        return
    _append(log, proc.stdout)
    if proc.returncode != 0:
        actions.append(f"stop command for {run['step']} failed: exit {proc.returncode}")


def _windows(conn, cfg, launcher, now_ts, actions):
    now_dt = util.parse_ts(now_ts)
    for run in board.running_runs(conn, purposes=["window"]):
        try:
            plan = _run_plan(conn, run)
            project = _project(conn, plan["project"]) if plan is not None else None
            if plan is None or project is None:
                continue
            error = None
            try:
                cfg_r = runner.load(project["name"])
            except ValueError as exc:
                cfg_r = None
                error = exc
            window_cfg = (cfg_r["windows"].get(run["step"])
                          if cfg_r is not None else None)
            if window_cfg is None:
                if run["pid"] is not None:
                    launcher.kill(run["pid"])
                _end(conn, actions, run, "FAILED",
                     summary=("runner config invalid" if error is not None
                              else "runner config missing"))
                _block(conn, actions, plan,
                       (f"{error}; the window was stopped. Answer to measure again."
                        if error is not None else
                        f"runner config for {project['name']} no longer has window "
                        f"{run['step']}; the window was stopped. Answer to measure "
                        "again."))
                continue
            log, _, _ = procs.run_paths(run["id"])
            text = _tail(log)
            pattern = cfg_r["stop_pattern"]
            if pattern is not None and pattern.search(text):
                if run["pid"] is not None:
                    launcher.kill(run["pid"])
                _stop_window(cfg, project, window_cfg, run, log, actions)
                _end(conn, actions, run, "FAILED", summary="safety stop")
                _block(conn, actions, plan,
                       f"Safety stop seen in window {run['step']} for "
                       f"{_label(plan)}. Log: {log}. Answer to measure again.")
                continue
            runtime = (now_dt - util.parse_ts(run["started_at"])).total_seconds()
            if runtime >= window_cfg["length_s"]:
                if run["pid"] is not None:
                    launcher.kill(run["pid"])
                _stop_window(cfg, project, window_cfg, run, log, actions)
                _end(conn, actions, run, "OK", summary="window ended")
        except Exception as exc:
            actions.append(
                f"error on run #{run['id']}: {type(exc).__name__}: {exc}")


# ------------------------------------------------------------ B. finish

def _finish(conn, cfg, launcher, git, run_id, exit_code, actions, ctx):
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None or run["status"] != "RUNNING":
        return
    purpose = run["purpose"] or ""
    kind = run["kind"]
    plan = _run_plan(conn, run)
    project = _project(conn, plan["project"]) if plan is not None else None
    log_path = procs.run_paths(run_id)[0]
    _, _, md_path = procs.run_paths(run_id)
    text = _read(log_path)

    if purpose in CLAUDE_PURPOSES:
        parsed = workers.parse_claude_json(text)
        report_path = _write_report(md_path, parsed["result"])
        if parsed["is_error"] or exit_code != 0:
            result = parsed["result"]
            if workers.is_usage_limit(result):
                summary = "usage limit"
                question = (f"Claude usage limit hit ({kind} for {_label(plan)}). "
                            "Answer anything when it has reset.")
            else:
                summary = f"{kind} failed"
                question = f"{kind} failed: {result[:300].strip()}. Log: {log_path}"
            _end(conn, actions, run, "FAILED", parsed["cost"], summary, exit_code,
                 report_path)
            _block(conn, actions, plan, question)
            return
        if plan is None or project is None:
            _end(conn, actions, run, "OK", parsed["cost"], None, exit_code, report_path)
            return
        if purpose in ("create", "revise"):
            _finish_create(conn, actions, run, plan, project, parsed["result"],
                           log_path, report_path, git, parsed["cost"], exit_code)
        elif purpose == "review":
            _finish_review(conn, actions, run, plan, parsed["result"], report_path,
                           log_path, parsed["cost"], exit_code)
        elif purpose == "judge":
            _finish_judge(conn, actions, run, plan, project, parsed["result"],
                          report_path, log_path, exit_code, parsed["cost"])
        else:
            _finish_verify(conn, cfg, launcher, actions, ctx, run, plan, project,
                           parsed["result"], report_path, log_path, git,
                           parsed["cost"], exit_code)
        return

    if purpose in CLAUDE_IMPL_PURPOSES:
        parsed = workers.parse_claude_json(text)
        report_path = _write_report(md_path, parsed["result"])
        if parsed["is_error"] or exit_code != 0:
            result = parsed["result"]
            if workers.is_usage_limit(result):
                summary = "usage limit"
                question = (f"Claude usage limit hit ({kind} for {_label(plan)}). "
                            "Answer anything when it has reset.")
            else:
                summary = f"{kind} failed"
                question = f"{kind} failed: {result[:300].strip()}. Log: {log_path}"
            _end(conn, actions, run, "FAILED", parsed["cost"], summary, exit_code,
                 report_path)
            _block(conn, actions, plan, question)
            return
        if plan is None or project is None:
            _end(conn, actions, run, "OK", parsed["cost"], None, exit_code, report_path)
            return
        raw_result = workers.last_line_value(parsed["result"], "RESULT")
        result = raw_result.lower() if raw_result is not None else None
        session = parsed["session"]
        if session:
            board.set_plan_fields(conn, plan["project"], plan["ref"],
                                  omp_session=session)
            plan = board.get_plan(conn, plan["project"], plan["ref"])
        _finish_implement(conn, actions, run, plan, project, kind, result,
                          workers.section(parsed["result"], "BLOCKED"),
                          parsed["cost"], session, exit_code, report_path, log_path,
                          git)
        return

    if purpose in OMP_PURPOSES:
        o = workers.parse_omp(text)
        report_path = _write_report(md_path, o["final"])
        if plan is not None and o["session"]:
            board.set_plan_fields(conn, plan["project"], plan["ref"],
                                  omp_session=o["session"])
            plan = board.get_plan(conn, plan["project"], plan["ref"])
        if exit_code == 3:
            _end(conn, actions, run, "COST_LIMIT", o["cost"], "cost limit",
                 exit_code, report_path, session=o["session"])
            _block(conn, actions, plan,
                   f"omp cost limit hit ({kind} for {_label(plan)}). Log: {log_path}")
            return
        _finish_implement(conn, actions, run, plan, project, kind, o["result"],
                          o["blocked"], o["cost"], o["session"], exit_code,
                          report_path, log_path, git)
        return

    if purpose == "preverify":
        if exit_code != 0:
            fix_text = "\n".join(text.splitlines()[-80:])
            _end(conn, actions, run, "FAILED", 0.0, f"exit {exit_code}", exit_code)
            _fix_or_block(conn, actions, run, plan, fix_text, log_path)
            return
        _end(conn, actions, run, "OK", 0.0, None, exit_code)
        if plan is not None and project is not None:
            _merge_step(conn, cfg, launcher, actions, ctx, project, plan, git)
        return

    if purpose in ("step", "window", "read"):
        _finish_runner(conn, cfg, actions, run, purpose, plan, project, exit_code)
        return

    _end(conn, actions, run, "OK", 0.0, None, exit_code)


def _finish_runner(conn, cfg, actions, run, purpose, plan, project, exit_code):
    if exit_code != 0:
        _fail_run(conn, actions, run, "FAILED", f"exit {exit_code}", exit_code)
        return
    if purpose == "window" and plan is not None:
        try:
            cfg_r = runner.load(plan["project"])
        except ValueError:
            cfg_r = None
        window_cfg = cfg_r["windows"].get(run["step"]) if cfg_r is not None else None
        if window_cfg is not None:
            runtime = (util.parse_ts(util.now())
                       - util.parse_ts(run["started_at"])).total_seconds()
            if runtime < window_cfg["length_s"]:
                _fail_run(conn, actions, run, "FAILED",
                          f"window process exited after {int(runtime // 60)}m",
                          exit_code)
                return
            if project is not None:
                _stop_window(cfg, project, window_cfg, run,
                             procs.run_paths(run["id"])[0], actions)
    _end(conn, actions, run, "OK", 0.0, None, exit_code)


def _finish_implement(conn, actions, run, plan, project, kind, result, blocked_text,
                      cost, session, exit_code, report_path, log_path, git):
    if result == "blocked":
        _end(conn, actions, run, "FAILED", cost, "blocked", exit_code,
             report_path, session=session)
        _block(conn, actions, plan, (blocked_text or "")[:1500] or
               f"{kind} reported blocked. Log: {log_path}")
        return
    if exit_code != 0 or result is None:
        _end(conn, actions, run, "FAILED", cost,
             f"exit {exit_code}", exit_code, report_path, session=session)
        _block(conn, actions, plan,
               f"{kind} failed (exit {exit_code}). Log: {log_path}")
        return
    if plan is None or project is None:
        _end(conn, actions, run, "OK", cost, None, exit_code, report_path,
             session=session)
        return
    try:
        commits = git.new_commits(project, plan["base_sha"], plan["branch"])
    except ValueError as exc:
        _end(conn, actions, run, "FAILED", cost, str(exc), exit_code,
             report_path, session=session)
        _block(conn, actions, plan,
               f"cannot count commits for {_label(plan)}: {exc}")
        return
    if commits == 0:
        _end(conn, actions, run, "FAILED", cost, "no new commits",
             exit_code, report_path, session=session)
        _block(conn, actions, plan,
               f"{kind} made no new commits in {plan['branch']}. Log: {log_path}")
        return
    note = "partial" if result == "partial" else None
    _end(conn, actions, run, "OK", cost, result, exit_code,
         report_path, session=session)
    _move(conn, actions, plan, "VERIFYING", note=note)


def _finish_create(conn, actions, run, plan, project, result, log_path, report_path,
                   git, cost, exit_code):
    path = workers.last_line_value(result, "PLAN")
    ref = workers.last_line_value(result, "REF")
    title = workers.last_line_value(result, "TITLE")
    if not (path and ref and title):
        _end(conn, actions, run, "FAILED", cost, "missing PLAN/REF/TITLE",
             exit_code, report_path)
        _block(conn, actions, plan,
               f"{run['kind']} report is missing PLAN/REF/TITLE. Log: {log_path}")
        return
    repo = Path(project["repo_path"])
    if not (repo / path).is_file():
        _end(conn, actions, run, "FAILED", cost, "plan file missing", exit_code,
             report_path)
        _block(conn, actions, plan,
               f"plan file {path} does not exist in {repo}. Log: {log_path}")
        return
    try:
        sha = git.plan_commit(project, path, run["started_at"])
    except ValueError as exc:
        _end(conn, actions, run, "FAILED", cost, str(exc), exit_code, report_path)
        _block(conn, actions, plan, f"cannot read the plan commit: {exc}")
        return
    if not sha:
        _end(conn, actions, run, "FAILED", cost, "no plan commit", exit_code,
             report_path)
        _block(conn, actions, plan,
               f"no new commit for {path} since the run started. Log: {log_path}")
        return
    try:
        files = git.commit_files(project, sha)
    except ValueError as exc:
        _end(conn, actions, run, "FAILED", cost, str(exc), exit_code, report_path)
        _block(conn, actions, plan, f"cannot list the plan commit files: {exc}")
        return
    plans_dir = str(project["plans_dir"]).strip("/")
    outside = [name for name in files
               if not (name == plans_dir or name.startswith(plans_dir + "/"))]
    if not files or outside:
        _end(conn, actions, run, "FAILED", cost, "commit outside plans dir",
             exit_code, report_path)
        detail = ", ".join(outside) if outside else "no files"
        _block(conn, actions, plan,
               f"plan commit {sha[:8]} touches files outside {plans_dir}: {detail}. "
               f"Log: {log_path}")
        return
    _end(conn, actions, run, "OK", cost, None, exit_code, report_path)
    cur_ref = plan["ref"]
    try:
        if ref != cur_ref:
            board.set_plan_ref(conn, plan["project"], cur_ref, ref)
            actions.append(f"renamed {plan['project']}/{cur_ref} to "
                           f"{plan['project']}/{ref}")
            cur_ref = ref
        board.set_plan_fields(conn, plan["project"], cur_ref, path=path,
                              title=title, operator_note=None)
        board.move_plan(conn, plan["project"], cur_ref, "REVIEW")
    except (LookupError, ValueError) as exc:
        current = dict(plan)
        current["ref"] = cur_ref
        _block(conn, actions, current, f"cannot apply the plan commit: {exc}")
        return
    actions.append(f"{plan['project']}/{cur_ref} → REVIEW")


def _finish_review(conn, actions, run, plan, result, report_path, log_path, cost,
                   exit_code):
    verdict = workers.parse_verdict(result, {"APPROVE", "REVISE"})
    rulings = workers.parse_rulings(result)
    if verdict is None:
        _end(conn, actions, run, "FAILED", cost, "no verdict", exit_code, report_path)
        _block(conn, actions, plan, f"review report has no VERDICT. Log: {log_path}")
        return
    _end(conn, actions, run, "OK", cost, None, exit_code, report_path)
    if rulings and rulings > 0:
        text = workers.section(result, "Rulings needed")
        question = (f"Plan {plan['ref']} ({plan['title']}) needs your rulings:\n"
                    f"{text[:1500]}\nFull review: {report_path}")
        _block(conn, actions, plan, question,
               action=f"rulings:{plan['project']}/{plan['ref']}")
        return
    if verdict == "APPROVE":
        _move(conn, actions, plan, "APPROVED")
        return
    rounds = plan["review_rounds"] + 1
    if rounds > states.MAX_REVIEW_ROUNDS:
        _block(conn, actions, plan,
               f"still REVISE after {states.MAX_REVIEW_ROUNDS} revisions; "
               f"last review: {report_path}")
        return
    board.set_plan_fields(conn, plan["project"], plan["ref"],
                          review_rounds=rounds, last_review_path=str(report_path))
    _move(conn, actions, plan, "DRAFT")


def _finish_judge(conn, actions, run, plan, project, result, report_path, log_path,
                  exit_code, cost):
    w = run["step"]
    verdict = workers.parse_verdict(result, {"PASS", "FAIL"})
    if verdict is None:
        _end(conn, actions, run, "FAILED", cost, "no verdict", exit_code, report_path)
        _block(conn, actions, plan,
               f"judge report has no VERDICT. Log: {log_path}")
        return
    _end(conn, actions, run, "OK", cost, None, exit_code, report_path)
    if verdict == "PASS":
        _move(conn, actions, plan, "DONE", note=f"gate {w} PASS")
        return
    follow = workers.section(result, "FOLLOW-UP")
    if follow:
        follow = "\n".join(
            line for line in follow.splitlines()
            if not line.strip().startswith("VERDICT:")
        ).strip()
    follow = follow or f"gate {w} passes"
    goal_id = board.add_goal(conn, project["name"], f"Follow-up: {plan['title']}",
                             done_when=follow)
    _move(conn, actions, plan, "DONE",
          note=f"gate {w} FAIL; follow-up goal #{goal_id}")


def _finish_verify(conn, cfg, launcher, actions, ctx, run, plan, project, result,
                   report_path, log_path, git, cost, exit_code):
    verdict = workers.parse_verdict(result, {"PASS", "FIX"})
    if verdict is None:
        _end(conn, actions, run, "FAILED", cost, "no verdict", exit_code, report_path)
        _block(conn, actions, plan, f"verify report has no VERDICT. Log: {log_path}")
        return
    _end(conn, actions, run, "OK", cost, None, exit_code, report_path)
    if verdict == "FIX":
        fix_text = workers.section(result, "FIX LIST") or result
        _fix_or_block(conn, actions, run, plan, fix_text, report_path)
        return
    try:
        ib_sha = git.update_branch(project, plan)
    except ValueError as exc:
        _block(conn, actions, plan,
               f"cannot update the plan branch with the integration branch: {exc}")
        return
    board.set_plan_fields(conn, plan["project"], plan["ref"], ib_sha=ib_sha)
    plan = board.get_plan(conn, plan["project"], plan["ref"])
    argv, cwd = workers.preverify(project, plan)
    _launch(conn, cfg, launcher, actions, ctx, project, plan, "preverify", argv, cwd,
            cfg.timeout_preverify_s)


def _fix_or_block(conn, actions, run, plan, fix_text, last_report):
    if plan is None:
        return
    if plan["fix_rounds"] < states.MAX_FIX_ROUNDS:
        path = _write_fix(run["id"], fix_text)
        _move(conn, actions, plan, "FIXING", note=f"fix list {path}")
    else:
        _block(conn, actions, plan,
               f"still failing after {states.MAX_FIX_ROUNDS} fix rounds; "
               f"last report: {last_report}")


# ---------------------------------------------------------- merge step

def _creative_running(conn, project_name) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runs r JOIN plans p ON p.id = r.plan_id "
        "WHERE p.project = ? AND r.status = 'RUNNING' "
        "AND r.purpose IN ('create', 'revise', 'judge') LIMIT 1",
        (project_name,),
    ).fetchone()
    return row is not None


def _lock_free(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM locks WHERE name = ?", (name,)).fetchone() is None


def _max_run_id(conn, plan_id) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM runs WHERE plan_id = ?", (plan_id,)
    ).fetchone()[0]


def _measure_decision(conn, actions, project, plan):
    """Decide what a merged plan does now; return False when it was blocked.

    A plan measures only when the project has a gates file, a runner config
    and the plan names a ``Gate window:`` line. Without a gates file or that
    line the plan goes DONE with a note. A gates file without a runner config
    leaves the plan MERGED: the gate is tracked by hand and the goal-done ask
    is still created. A named window missing from the runner config blocks.
    """
    if not (project["gates_file"] or "").strip():
        _move(conn, actions, plan, "DONE", note="no gates file")
        return True
    try:
        cfg_r = runner.load(project["name"])
    except ValueError as exc:
        _block(conn, actions, plan, str(exc))
        return False
    if cfg_r is None:
        return True
    window = runner.gate_window(Path(project["repo_path"]) / plan["path"])
    if window is None:
        _move(conn, actions, plan, "DONE", note="no gate window")
        return True
    if window not in cfg_r["windows"]:
        _block(conn, actions, plan,
               f"Plan {_label(plan)} names gate window {window}, which is "
               f"not in {runner.config_path(project['name'])}.")
        return False
    _move(conn, actions, plan, "MEASURING", window=window,
          measure_from=_max_run_id(conn, plan["id"]))
    return True


def _merge_step(conn, cfg, launcher, actions, ctx, project, plan, git):
    plan = board.get_plan(conn, plan["project"], plan["ref"])
    if plan["state"] != "VERIFYING":
        return
    if _creative_running(conn, project["name"]):
        actions.append(f"merge of {_label(plan)} deferred: an architect run is running")
        return
    if not _lock_free(conn, f"release-build:{project['name']}"):
        actions.append(f"merge of {_label(plan)} deferred: a release build is running")
        return
    try:
        sha = git.merge_into_main(project, plan, plan["ib_sha"])
    except ValueError as exc:
        if str(exc).endswith(" moved"):
            _retry_merge(conn, cfg, launcher, actions, ctx, project, plan, git)
        else:
            _block(conn, actions, plan,
                   f"Cannot merge {_label(plan)}: {exc}. Answer to retry.")
        return
    if not _move(conn, actions, plan, "MERGED", merge_sha=sha):
        return
    try:
        git.cleanup(project, plan)
    except ValueError as exc:
        actions.append(f"cleanup failed for {_label(plan)}: {exc}")
    else:
        actions.append(f"cleaned up {_label(plan)}")
    if not _measure_decision(conn, actions, project, plan):
        return
    plan = board.get_plan(conn, plan["project"], plan["ref"])
    _ask_goal_done(conn, actions, project, plan)


def _ask_goal_done(conn, actions, project, plan):
    goal = _goal(conn, plan["goal_id"]) if plan["goal_id"] is not None else None
    if goal is None:
        return
    question = (f"Plan {plan['ref']} ({plan['title']}) is merged into "
                f"{project['integration_branch']} at {plan['merge_sha'][:8]}. "
                f"Is goal #{goal['id']} done? Done when: {goal['done_when']}. "
                "Answer \"done\", or say what is still missing.")
    board.add_ask(conn, question, project=plan["project"], plan_id=plan["id"],
                  action=f"goal-done:{goal['id']}")
    actions.append(f"asked whether goal #{goal['id']} is done")


def _retry_merge(conn, cfg, launcher, actions, ctx, project, plan, git):
    ib = project["integration_branch"]
    plans_dir = str(project["plans_dir"]).strip("/")
    try:
        head = git.ib_head(project)
    except ValueError as exc:
        _block(conn, actions, plan, f"cannot read {ib}: {exc}")
        return
    try:
        files = git.commit_files(project, f"{plan['ib_sha']}..{head}")
    except ValueError:
        files = None
    plan_only = files is not None and all(
        name == plans_dir or name.startswith(plans_dir + "/") for name in files
    )
    if not plan_only:
        attempts = plan["merge_attempts"] + 1
        if attempts > states.MAX_MERGE_ATTEMPTS:
            _block(conn, actions, plan,
                   f"{ib} moved {attempts} times since the last preverify; "
                   "the merge is blocked. Answer to retry.")
            return
        board.set_plan_fields(conn, plan["project"], plan["ref"],
                              merge_attempts=attempts)
        actions.append(f"merge attempt {attempts} for {_label(plan)}")
    try:
        new_ib = git.update_branch(project, plan)
    except ValueError as exc:
        _block(conn, actions, plan, f"cannot update the plan branch: {exc}")
        return
    board.set_plan_fields(conn, plan["project"], plan["ref"], ib_sha=new_ib)
    plan = board.get_plan(conn, plan["project"], plan["ref"])
    argv, cwd = workers.preverify(project, plan)
    _launch(conn, cfg, launcher, actions, ctx, project, plan, "preverify", argv, cwd,
            cfg.timeout_preverify_s)


# ------------------------------------------------------------- C. answers

def _answers(conn, cfg, launcher, git, actions):
    for ask in board.pending_answers(conn):
        action = ask["action"] or ""
        answer = ask["answer"] or ""
        try:
            if action.startswith("unblock:"):
                _answer_unblock(conn, cfg, launcher, git, actions, ask, action, answer)
            elif action.startswith("window:"):
                _answer_window(conn, actions, ask, action, answer)
            elif action.startswith("rulings:"):
                _answer_rulings(conn, actions, ask, action, answer)
            elif action.startswith("goal-done:"):
                _answer_goal_done(conn, actions, ask, action, answer)
        except (LookupError, ValueError) as exc:
            actions.append(f"cannot handle answer for {action}: {exc}")
        finally:
            board.mark_handled(conn, ask["id"])


def _ask_plan(conn, ask):
    if ask["plan_id"] is None:
        return None
    return conn.execute("SELECT * FROM plans WHERE id = ?", (ask["plan_id"],)).fetchone()


def _answer_unblock(conn, cfg, launcher, git, actions, ask, action, answer):
    project_name, _, ref = action[len("unblock:"):].partition("/")
    board.set_plan_fields(conn, project_name, ref, operator_note=answer)
    plan = board.get_plan(conn, project_name, ref)
    if plan["state"] == "BLOCKED":
        if plan["blocked_from"] is None:
            actions.append(f"{_label(plan)} is BLOCKED with no previous state")
            return
        _move(conn, actions, plan, plan["blocked_from"])
        plan = board.get_plan(conn, project_name, ref)
    if plan["state"] == "MERGED":
        project = _project(conn, project_name)
        if _measure_decision(conn, actions, project, plan):
            plan = board.get_plan(conn, project_name, ref)
            _ask_goal_done(conn, actions, project, plan)
        return
    if plan["state"] == "MEASURING":
        board.set_plan_fields(conn, project_name, ref,
                              measure_from=_max_run_id(conn, plan["id"]))
        plan = board.get_plan(conn, project_name, ref)
    if plan["state"] == "VERIFYING":
        latest = _latest_run(conn, plan["id"])
        if latest is not None and latest["purpose"] == "preverify" \
                and latest["status"] == "OK":
            project = _project(conn, project_name)
            _merge_step(conn, cfg, launcher, actions, {"claude": 0}, project, plan, git)


def _answer_window(conn, actions, ask, action, answer):
    project_name, _, ref = action[len("window:"):].partition("/")
    plan = board.get_plan(conn, project_name, ref)
    if answer.strip().lower().startswith("yes"):
        board.set_plan_fields(conn, project_name, ref, window_ok=1)
        actions.append(f"{_label(plan)} production window approved")
    else:
        _move(conn, actions, plan, "DONE",
              note=f"production window declined: {answer}")


def _answer_rulings(conn, actions, ask, action, answer):
    project_name, _, ref = action[len("rulings:"):].partition("/")
    board.set_plan_fields(conn, project_name, ref, operator_note=answer)
    plan = board.get_plan(conn, project_name, ref)
    review = conn.execute(
        "SELECT report_path FROM runs WHERE plan_id = ? AND purpose = 'review' "
        "AND report_path IS NOT NULL ORDER BY id DESC LIMIT 1",
        (plan["id"],),
    ).fetchone()
    if review is not None:
        board.set_plan_fields(conn, project_name, ref,
                              last_review_path=review["report_path"])
    if plan["state"] == "BLOCKED":
        if _move(conn, actions, plan, "REVIEW"):
            plan = board.get_plan(conn, project_name, ref)
            _move(conn, actions, plan, "DRAFT")


def _answer_goal_done(conn, actions, ask, action, answer):
    try:
        goal_id = int(action[len("goal-done:"):])
    except ValueError:
        return
    goal = _goal(conn, goal_id)
    if goal is None:
        return
    if answer.strip().lower().startswith(("done", "yes")):
        board.set_goal_state(conn, goal_id, "DONE")
        actions.append(f"goal #{goal_id} DONE")
        rows = conn.execute(
            "SELECT ref FROM plans WHERE goal_id = ? AND state = 'MERGED'", (goal_id,)
        ).fetchall()
        for row in rows:
            plan = board.get_plan(conn, goal["project"], row["ref"])
            _move(conn, actions, plan, "DONE")
    else:
        board.add_note(conn, answer, project=goal["project"], kind="decision")
        actions.append(f"goal #{goal_id} stays ACTIVE: {answer}")


# -------------------------------------------------------------- D. plans

def _plans(conn, cfg, launcher, git, ctx, now_ts, actions):
    order = {goal["id"]: index for index, goal
             in enumerate(_ordered_goals(conn, cfg, now_ts)) if goal["state"] == "ACTIVE"}
    rows = conn.execute(
        "SELECT p.* FROM plans p JOIN projects pr ON pr.name = p.project "
        "JOIN goals g ON g.id = p.goal_id "
        "WHERE pr.auto = 1 AND pr.archived = 0 AND g.state = 'ACTIVE' AND NOT EXISTS ("
        "SELECT 1 FROM runs r WHERE r.plan_id = p.id AND r.status = 'RUNNING')"
    ).fetchall()
    rows = sorted(rows, key=lambda row: (order.get(row["goal_id"], 1 << 30), row["id"]))
    for row in rows:
        try:
            plan = board.get_plan(conn, row["project"], row["ref"])
            project = _project(conn, plan["project"])
            _plan_step(conn, cfg, launcher, git, ctx, now_ts, actions, project, plan)
        except Exception as exc:
            actions.append(f"error on {row['project']}/{row['ref']}: "
                           f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------- D2. measure

def _measure(conn, cfg, launcher, git, ctx, now_ts, actions):
    rows = conn.execute(
        "SELECT p.* FROM plans p JOIN projects pr ON pr.name = p.project "
        "WHERE p.state = 'MEASURING' AND pr.auto = 1 AND pr.archived = 0 "
        "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.plan_id = p.id "
        "AND r.status = 'RUNNING')"
    ).fetchall()
    prepared = []
    for row in rows:
        try:
            cfg_r = runner.load(row["project"])
            error = None
        except ValueError as exc:
            cfg_r, error = None, exc
        window_cfg = cfg_r["windows"].get(row["window"]) if cfg_r else None
        length = window_cfg["length_s"] if window_cfg is not None else None
        prepared.append((length, row["id"], row, cfg_r, error))
    prepared.sort(key=lambda item: (item[0] is None, item[0] or 0, item[1]))
    for _, _, row, cfg_r, error in prepared:
        try:
            plan = board.get_plan(conn, row["project"], row["ref"])
            project = _project(conn, plan["project"])
            if error is not None:
                _block(conn, actions, plan, str(error))
                continue
            if cfg_r is None or plan["window"] not in cfg_r["windows"]:
                _block(conn, actions, plan,
                       f"runner config for {project['name']} no longer has "
                       f"window {plan['window']}")
                continue
            _measure_plan(conn, cfg, launcher, git, ctx, now_ts, actions, project,
                          plan, cfg_r)
        except Exception as exc:
            actions.append(f"error on {row['project']}/{row['ref']}: "
                           f"{type(exc).__name__}: {exc}")


def _measure_plan(conn, cfg, launcher, git, ctx, now_ts, actions, project, plan,
                  cfg_r):
    w = plan["window"]
    window_cfg = cfg_r["windows"][w]
    for name in window_cfg["before"]:
        if _ok_run(conn, plan, "step", name) is not None:
            continue
        step = cfg_r["steps"][name]
        locks = [f"{lock}:{project['name']}" for lock in step["locks"]]
        for lock in locks:
            if not _lock_free(conn, lock):
                _skip(actions, plan, f"{lock} busy", now_ts)
                return
        argv, cwd = workers.runner_cmd(project, step["cmd"])
        run_id = _launch(conn, cfg, launcher, actions, ctx, project, plan, "step",
                         argv, cwd, step["timeout_s"], step=name)
        if run_id is not None:
            for lock in locks:
                board.take_lock(conn, lock, f"run:{run_id}")
        return
    if _ok_run(conn, plan, "window") is None:
        if window_cfg["production"] and not cfg_r["safety_stop"] \
                and plan["window_ok"] == 0:
            action = f"window:{project['name']}/{plan['ref']}"
            existing = conn.execute(
                "SELECT 1 FROM asks WHERE action = ? AND state = 'OPEN'", (action,)
            ).fetchone()
            board.add_ask(
                conn,
                f'Start production window {w} for {_label(plan)}? It runs '
                f'"{window_cfg["start"]}" for {window_cfg["length_s"] // 60}m. '
                'Answer "yes" to start; any other answer skips the window and '
                "closes the plan.",
                project=project["name"], plan_id=plan["id"], action=action)
            if existing is None:
                actions.append(
                    f"asked to start production window {w} for {_label(plan)}")
            return
        instance = f"instance:{project['name']}"
        if not _lock_free(conn, instance):
            _skip(actions, plan, f"{instance} busy", now_ts)
            return
        argv, cwd = workers.runner_cmd(project, window_cfg["start"])
        run_id = _launch(conn, cfg, launcher, actions, ctx, project, plan, "window",
                         argv, cwd, None, step=w)
        if run_id is not None:
            board.take_lock(conn, instance, f"run:{run_id}")
        return
    if _ok_run(conn, plan, "read") is None:
        argv, cwd = workers.runner_cmd(project, window_cfg["read"])
        _launch(conn, cfg, launcher, actions, ctx, project, plan, "read", argv, cwd,
                600, step=w)
        return
    if _ok_run(conn, plan, "judge") is None:
        if not _claude_slot(cfg, ctx):
            _skip(actions, plan, "Claude slot busy", now_ts)
            return
        reason = _main_ready(conn, actions, git, project)
        if reason is not None:
            _skip(actions, plan, f"main not ready: {reason}", now_ts)
            return
        read_run = _ok_run(conn, plan, "read")
        read_log = procs.run_paths(read_run["id"])[0]
        argv, cwd = workers.judge(cfg, project, plan, w, read_log)
        _launch(conn, cfg, launcher, actions, ctx, project, plan, "judge", argv, cwd,
                cfg.timeout_verify_s, step=w)


def _ok_run(conn, plan, purpose, step=None):
    sql = ("SELECT id FROM runs WHERE plan_id = ? AND purpose = ? "
           "AND status = 'OK' AND id > ?")
    params = [plan["id"], purpose, plan["measure_from"]]
    if step is not None:
        sql += " AND step = ?"
        params.append(step)
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _plan_step(conn, cfg, launcher, git, ctx, now_ts, actions, project, plan):
    state = plan["state"]
    goal = _goal(conn, plan["goal_id"])
    if goal is None:
        return
    if state == "DRAFT":
        if not plan["path"]:
            if not _claude_slot(cfg, ctx):
                _skip(actions, plan, "Claude slot busy", now_ts)
                return
            reason = _main_ready(conn, actions, git, project)
            if reason is not None:
                _skip(actions, plan, f"main not ready: {reason}", now_ts)
                return
            note = plan["operator_note"]
            argv, cwd = workers.architect(cfg, project, goal, plan,
                                          operator_note=note)
            _launch(conn, cfg, launcher, actions, ctx, project, plan, "create", argv,
                    cwd, cfg.timeout_architect_s, used_note=bool(note))
        elif plan["review_rounds"] == 0 and not plan["operator_note"]:
            _move(conn, actions, plan, "REVIEW")
        else:
            if not _claude_slot(cfg, ctx):
                _skip(actions, plan, "Claude slot busy", now_ts)
                return
            reason = _main_ready(conn, actions, git, project)
            if reason is not None:
                _skip(actions, plan, f"main not ready: {reason}", now_ts)
                return
            note = plan["operator_note"]
            review_text = _read(plan["last_review_path"]) \
                if plan["last_review_path"] else ""
            if not review_text:
                report = plan["last_review_path"] or "(none recorded)"
                _block(conn, actions, plan,
                       f"cannot revise {plan['project']}/{plan['ref']}: "
                       f"review report {report} is missing")
                return
            argv, cwd = workers.architect(cfg, project, goal, plan,
                                          review_text=review_text,
                                          operator_note=note)
            _launch(conn, cfg, launcher, actions, ctx, project, plan, "revise", argv,
                    cwd, cfg.timeout_architect_s, used_note=bool(note))
        return
    if state == "REVIEW":
        if not _claude_slot(cfg, ctx):
            _skip(actions, plan, "Claude slot busy", now_ts)
            return
        argv, cwd = workers.reviewer(cfg, project, plan)
        _launch(conn, cfg, launcher, actions, ctx, project, plan, "review", argv, cwd,
                cfg.timeout_review_s)
        return
    if state == "APPROVED":
        active = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE state IN (?, ?, ?)",
            states.ACTIVE_PLAN_STATES,
        ).fetchone()[0]
        if active >= cfg.max_active_plans:
            _skip(actions, plan, "max active plans reached", now_ts)
            return
        try:
            free_gb = shutil.disk_usage(cfg.disk_path).free / (1024 ** 3)
        except OSError as exc:
            _skip(actions, plan, f"cannot read disk usage: {exc}", now_ts)
            return
        needed = cfg.disk_floor_gb + cfg.worktree_reserve_gb
        if free_gb < needed:
            _skip(actions, plan, f"disk free {free_gb:.0f} GB < {needed} GB", now_ts)
            return
        if not _budget_ok(conn, cfg, actions, now_ts):
            _skip(actions, plan, "monthly budget reached", now_ts)
            return
        try:
            worktree, branch, base_sha = git.create_worktree(project, plan)
        except ValueError as exc:
            _block(conn, actions, plan, f"cannot create the worktree: {exc}")
            return
        board.set_plan_fields(conn, plan["project"], plan["ref"], worktree=worktree,
                              branch=branch, base_sha=base_sha)
        if not _move(conn, actions, plan, "EXECUTING"):
            return
        plan = board.get_plan(conn, plan["project"], plan["ref"])
        _launch_implement(conn, cfg, launcher, actions, ctx, project, plan, now_ts)
        return
    if state == "EXECUTING":
        if not _budget_ok(conn, cfg, actions, now_ts):
            _skip(actions, plan, "monthly budget reached", now_ts)
            return
        _launch_implement(conn, cfg, launcher, actions, ctx, project, plan, now_ts)
        return
    if state == "FIXING":
        if not _budget_ok(conn, cfg, actions, now_ts):
            _skip(actions, plan, "monthly budget reached", now_ts)
            return
        fix_path = _newest_fix(conn, plan)
        if fix_path is None:
            _skip(actions, plan, "no fix list", now_ts)
            return
        note = plan["operator_note"]
        if project["implementer"] == "claude":
            if not _claude_slot(cfg, ctx):
                _skip(actions, plan, "Claude slot busy", now_ts)
                return
            argv, cwd = workers.claude_implementer(cfg, project, plan, fix_path)
            _launch(conn, cfg, launcher, actions, ctx, project, plan, "fix-claude",
                    argv, cwd, cfg.fix_timeout_s(), used_note=bool(note))
            return
        argv, cwd = workers.fixer(cfg, project, plan, fix_path)
        _launch(conn, cfg, launcher, actions, ctx, project, plan, "fix", argv, cwd,
                cfg.fix_timeout_s(), used_note=bool(note))
        return
    if state == "VERIFYING":
        latest = _latest_run(conn, plan["id"])
        if latest is not None and latest["purpose"] == "preverify" \
                and latest["status"] == "OK":
            _merge_step(conn, cfg, launcher, actions, ctx, project, plan, git)
            return
        if not _claude_slot(cfg, ctx):
            _skip(actions, plan, "Claude slot busy", now_ts)
            return
        report = _newest_report(conn, plan, ("implement", "fix",
                                             *CLAUDE_IMPL_PURPOSES))
        if report is None:
            _skip(actions, plan, "no implementation report", now_ts)
            return
        argv, cwd = workers.verifier(cfg, project, plan, report)
        _launch(conn, cfg, launcher, actions, ctx, project, plan, "verify", argv, cwd,
                cfg.timeout_verify_s)


def _launch_implement(conn, cfg, launcher, actions, ctx, project, plan, now_ts):
    note = plan["operator_note"]
    if project["implementer"] == "claude":
        if not _claude_slot(cfg, ctx):
            _skip(actions, plan, "Claude slot busy", now_ts)
            return
        argv, cwd = workers.claude_implementer(cfg, project, plan)
        _launch(conn, cfg, launcher, actions, ctx, project, plan, "implement-claude",
                argv, cwd, cfg.impl_timeout_s(), used_note=bool(note))
        return
    argv, cwd = workers.implementer(cfg, project, plan)
    if plan["omp_session"]:
        argv = argv[:-1] + ["--resume", plan["omp_session"], argv[-1]]
    _launch(conn, cfg, launcher, actions, ctx, project, plan, "implement", argv, cwd,
            cfg.impl_timeout_s(), used_note=bool(note))


def _main_ready(conn, actions, git, project):
    try:
        reason = git.main_ready(project)
    except ValueError as exc:
        reason = str(exc)
    if reason is None:
        return None
    action = f"main-dirty:{project['name']}"
    existing = conn.execute(
        "SELECT 1 FROM asks WHERE action = ? AND state = 'OPEN'", (action,)
    ).fetchone()
    board.add_ask(conn,
                  f"The main checkout of {project['name']} is not ready for plan "
                  f"commits: {reason}.", project=project["name"], action=action)
    if existing is None:
        actions.append(f"asked: main checkout of {project['name']} is not ready")
    return reason


def _budget_ok(conn, cfg, actions, now_ts):
    month = now_ts[:7]
    spend = board.month_spend(conn, month)
    limit = cfg.monthly_budget_usd * cfg.budget_warn_ratio
    if spend < limit:
        return True
    action = f"budget:{month}"
    existing = conn.execute("SELECT 1 FROM asks WHERE action = ?", (action,)).fetchone()
    if existing is None:
        board.add_ask(conn,
                      f"Month spend ${spend:.2f} reached the warning threshold "
                      f"${limit:.2f}; omp launches are paused.", action=action,
                      dedupe_any_state=True)
        actions.append(f"asked: monthly budget {month}")
    return False


def _latest_run(conn, plan_id):
    return conn.execute(
        "SELECT * FROM runs WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)
    ).fetchone()


def _newest_report(conn, plan, purposes):
    placeholders = ", ".join("?" * len(purposes))
    row = conn.execute(
        f"SELECT report_path FROM runs WHERE plan_id = ? AND purpose IN ({placeholders}) "
        "AND report_path IS NOT NULL ORDER BY id DESC LIMIT 1",
        (plan["id"], *purposes),
    ).fetchone()
    return row["report_path"] if row is not None else None


def _newest_fix(conn, plan):
    rows = conn.execute(
        "SELECT id FROM runs WHERE plan_id = ? ORDER BY id DESC", (plan["id"],)
    ).fetchall()
    for row in rows:
        log, _, _ = procs.run_paths(row["id"])
        path = log.with_name(f"{row['id']}-fix.md")
        if path.is_file():
            return str(path)
    return None


# -------------------------------------------------------------- E. goals

def _ordered_goals(conn, cfg, now_ts):
    rows = conn.execute(
        "SELECT g.* FROM goals g JOIN projects pr ON pr.name = g.project "
        "WHERE pr.auto = 1 AND pr.archived = 0 ORDER BY g.id"
    ).fetchall()
    now_dt = util.parse_ts(now_ts)
    aged = []
    rest = []
    for goal in rows:
        waited = (now_dt - util.parse_ts(goal["created_at"])).total_seconds()
        if waited > cfg.max_goal_wait_h * 3600:
            aged.append(goal)
        else:
            rest.append(goal)
    aged.sort(key=lambda goal: (goal["created_at"], goal["id"]))
    rest.sort(key=lambda goal: (
        goal["est_minutes"] is None,
        goal["est_minutes"] if goal["est_minutes"] is not None else 0,
        -goal["priority"],
        goal["id"],
    ))
    return aged + rest


def _goals(conn, cfg, actions, now_ts):
    goals = _ordered_goals(conn, cfg, now_ts)
    active = sum(1 for goal in goals if goal["state"] == "ACTIVE")
    for goal in goals:
        if active >= cfg.max_active_plans:
            break
        if goal["state"] != "QUEUED":
            continue
        board.set_goal_state(conn, goal["id"], "ACTIVE")
        actions.append(f"goal #{goal['id']} QUEUED → ACTIVE")
        active += 1
    for goal in _ordered_goals(conn, cfg, now_ts):
        if goal["state"] != "ACTIVE":
            continue
        count = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE goal_id = ?", (goal["id"],)
        ).fetchone()[0]
        if count == 0:
            board.add_plan(conn, goal["project"], f"g{goal['id']}", goal["title"],
                           goal_id=goal["id"])
            actions.append(f"goal #{goal['id']} placeholder plan g{goal['id']} added")
