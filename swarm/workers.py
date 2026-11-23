"""Worker command lines and worker output parsers (pure functions)."""

import json
import re

from swarm import board, runner

DENY = ["Bash(git push:*)", "Bash(git reset:*)", "Bash(git rebase:*)",
        "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git branch:*)",
        "Bash(git worktree:*)", "Bash(git merge:*)", "Bash(ssh:*)", "Bash(rm:*)"]
GIT_READ = ["Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git status:*)"]
HEADLESS = ("Non-interactive run for the swarm scheduler: nobody can answer you. "
            "Do not wait for rulings. Keep the report under 40 lines.")

_VERDICT_RE = re.compile(r"^VERDICT:\s*(\S+)\s*$")
_RULINGS_RE = re.compile(r"^RULINGS:\s*(.+?)\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_COST_RE = re.compile(r"^COST:\s*\$\s*([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
_OMP_RESULT_RE = re.compile(r"^##\s*RESULT:\s*(\S+)\s*$", re.MULTILINE)
_FINAL_MARKER = "---- worker final message ----"


def claude_argv(cfg, prompt, allowed, system, agent=None, model=None):
    argv = [cfg.claude_bin, "-p", prompt]
    if agent is not None:
        argv += ["--agent", agent]
    argv += ["--output-format", "stream-json", "--verbose",
             "--model", model or cfg.claude_model,
             "--permission-mode", "dontAsk",
             "--append-system-prompt", system,
             "--allowedTools", *allowed,
             "--disallowedTools", *DENY]
    return argv


def rules_text(project) -> str:
    rules = board.project_rules(project)
    if not rules:
        return ""
    return (" Project rules — never do these; they need the operator: "
            + "; ".join(rules) + ".")


def verify_allow(project):
    raw = project["verify_allow"]
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def architect(cfg, project, goal, plan, review_text=None, operator_note=None):
    plans_dir = project["plans_dir"]
    if review_text:
        first = f"Revise plan {plan['path']} using the review below."
    else:
        first = "Write a new plan for this goal."
    lines = [
        first,
        f"Project: {project['name']}, plans dir: {plans_dir}, "
        f"integration branch: {project['integration_branch']}.",
        f"Goal #{goal['id']}: {goal['title']}",
        f"Done when: {goal['done_when']}",
    ]
    try:
        cfg_r = runner.load(project["name"])
    except ValueError:
        cfg_r = None
    if cfg_r is not None and cfg_r["windows"]:
        names = ", ".join(sorted(cfg_r["windows"]))
        lines.append(
            f"Measurement windows for this project: {names}. If the change needs "
            "measuring after merge, add a line 'Gate window: <name>' to the plan."
        )
    if operator_note:
        lines.append(f"Operator answer: {operator_note}")
    if review_text:
        lines.append("Review to address:")
        lines.append(review_text)
    prompt = "\n".join(lines)
    allowed = ["Read", "Grep", "Glob",
               f"Edit({plans_dir}/**)", f"Write({plans_dir}/**)",
               "Bash(git add:*)", "Bash(git commit:*)", *GIT_READ]
    system = HEADLESS + (
        " Commit only the plan file, with: git add <file> && git commit -m "
        "\"<ref>: plan <title>\" -- <file>. The last three lines must be exactly: "
        "PLAN: <path relative to the repo>, REF: <plan ref>, TITLE: <one-line title>."
    ) + rules_text(project)
    return claude_argv(cfg, prompt, allowed, system, agent="swarm-architect",
                       model=cfg.model_for("architect")), project["repo_path"]


def reviewer(cfg, project, plan):
    prompt = f"/review-plan {plan['path']}"
    allowed = ["Read", "Grep", "Glob", "Skill", *GIT_READ]
    system = HEADLESS + (
        " Do not edit files. List choices that belong to the operator under "
        "'Rulings needed'. The second-to-last line must be 'RULINGS: none' or "
        "'RULINGS: <count>'. The last line must be exactly 'VERDICT: APPROVE' or "
        "'VERDICT: REVISE'."
    )
    model = (cfg.model_for("rereview") if plan["review_rounds"] > 0
             else cfg.model_for("reviewer"))
    return claude_argv(cfg, prompt, allowed, system, model=model), project["repo_path"]


def judge(cfg, project, plan, window, read_log):
    gates_file = project["gates_file"]
    prompt = "\n".join([
        f"Write the gate result for plan {plan['path']}, window {window}.",
        f"Read output: {read_log}",
        f"Gates file: {gates_file}",
    ])
    allowed = ["Read", "Grep", "Glob", f"Edit({gates_file})",
               "Bash(git add:*)", "Bash(git commit:*)", *GIT_READ]
    system = HEADLESS + (
        " Record the result in the gates file in the style of its existing rows "
        "(date, plan ref, window, numbers from the read output, PASS or FAIL). "
        "Commit only the gates file: git add <file> && git commit -m "
        f"\"<ref>: gate <window> <PASS|FAIL>\" -- <file>. If it failed, write a "
        "section '## FOLLOW-UP' with one line: what a follow-up plan must achieve. "
        "The last line must be exactly 'VERDICT: PASS' or 'VERDICT: FAIL'."
    ) + rules_text(project)
    return claude_argv(cfg, prompt, allowed, system,
                       model=cfg.model_for("judge")), project["repo_path"]


def verifier(cfg, project, plan, report_path):
    prompt = f"/verify-review {report_path}"
    allowed = ["Read", "Grep", "Glob", "Skill", *GIT_READ, *verify_allow(project)]
    system = HEADLESS + (
        " Do not edit files. The report is the implementer's; verify it against plan "
        f"{plan['path']} and the commits {plan['base_sha']}..HEAD in this worktree. "
        f"Run the verify command yourself: {project['verify_cmd']}. If anything must be "
        "fixed, write a section '## FIX LIST' with numbered items (file:line, expected "
        "vs actual, failing output). The last line must be exactly 'VERDICT: PASS' or "
        "'VERDICT: FIX'."
    ) + rules_text(project) + (
        " Treat a breach of a project rule as a FIX item."
        if board.project_rules(project) else ""
    )
    return claude_argv(cfg, prompt, allowed, system,
                       model=cfg.model_for("verifier")), plan["worktree"]


def implementer(cfg, project, plan):
    worktree = plan["worktree"]
    prompt = (
        "Load skill://orchestrate-omp and follow it exactly. "
        f"Plan: {plan['path']}. Verify command: {project['verify_cmd']}. "
        "Read AGENTS.md or CLAUDE.md first if present. "
        f"Commit message prefix '{plan['ref']} T<n>:'. Never push. "
        f"Do not touch files outside {worktree}."
    )
    prompt += rules_text(project)
    if plan["operator_note"]:
        prompt += f" Operator answer: {plan['operator_note']}"
    argv = ["python3", cfg.omp_runner, "--cwd", worktree,
            "--max-cost", str(cfg.omp_max_cost), "--max-time", cfg.omp_max_time, prompt]
    return argv, worktree


def fixer(cfg, project, plan, fix_path):
    worktree = plan["worktree"]
    prompt = (
        f"Verification found problems; the fix list is in {fix_path}. "
        "Fix them via workers per orchestrate-omp, keep the verify command green, "
        "then give the same final report."
    )
    prompt += rules_text(project)
    if plan["operator_note"]:
        prompt += f" Operator answer: {plan['operator_note']}"
    argv = ["python3", cfg.omp_runner, "--cwd", worktree]
    if plan["omp_session"]:
        argv += ["--resume", plan["omp_session"]]
    argv += ["--max-cost", str(cfg.omp_fix_max_cost), "--max-time", cfg.omp_fix_max_time, prompt]
    return argv, worktree


def claude_implementer(cfg, project, plan, fix_path=None):
    worktree = plan["worktree"]
    if fix_path:
        prompt = (
            f"Verification found problems; the fix list is in {fix_path}. "
            "Fix them, keep the verify command green, commit."
        )
    else:
        prompt = (
            f"Implement plan {plan['path']} in this worktree. "
            f"Verify command: {project['verify_cmd']}. "
            "Read AGENTS.md or CLAUDE.md first if present. "
            f"Commit per task with message prefix '{plan['ref']} T<n>:'."
            f" Never push. Do not touch files outside {worktree}."
        )
    if plan["operator_note"]:
        prompt += f" Operator answer: {plan['operator_note']}"
    allowed = ["Read", "Grep", "Glob", f"Edit({worktree}/**)", f"Write({worktree}/**)",
               "Bash(git add:*)", "Bash(git commit:*)", *GIT_READ,
               *verify_allow(project)]
    system = HEADLESS + rules_text(project) + (
        " End with a section '## BLOCKED' only if you cannot finish. The last line "
        "must be exactly 'RESULT: done', 'RESULT: partial' or 'RESULT: blocked'."
    )
    argv = claude_argv(cfg, prompt, allowed, system, model=cfg.model_for("implementer"))
    if plan["omp_session"] and fix_path:
        argv[3:3] = ["--resume", plan["omp_session"]]
    return argv, worktree


def preverify(project, plan):
    return ["sh", "-c", project["verify_cmd"]], plan["worktree"]


def runner_cmd(project, cmd):
    return ["sh", "-c", cmd], project["repo_path"]


def parse_claude_json(text):
    for line in reversed([line for line in text.splitlines() if line.strip()]):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            result = obj.get("result", "")
            if not isinstance(result, str):
                result = ""
            try:
                cost = float(obj.get("total_cost_usd") or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            return {"result": result,
                    "is_error": bool(obj.get("is_error")),
                    "cost": cost,
                    "session": obj.get("session_id")}
    return {"result": text[-2000:], "is_error": True, "cost": 0.0, "session": None}


def is_usage_limit(text):
    lowered = text.lower()
    return any(marker in lowered for marker in
               ("usage limit", "rate limit", "requires usage credits", "limit reached"))


def last_line_value(text, key):
    pattern = re.compile(r"^%s:\s*(.+)$" % re.escape(key), re.MULTILINE)
    values = [match.group(1).strip() for match in pattern.finditer(text)]
    return values[-1] if values else None


def parse_verdict(text, allowed):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    match = _VERDICT_RE.match(lines[-1])
    if not match:
        return None
    verdict = match.group(1)
    return verdict if verdict in allowed else None


def parse_rulings(text):
    value = None
    for match in _RULINGS_RE.finditer(text):
        value = match.group(1)
    if value is None:
        return None
    if value.lower() == "none":
        return 0
    if value.isdigit():
        return int(value)
    return None


def section(text, heading):
    lines = text.splitlines()
    wanted = heading.lower()
    headings = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip().lower()))
    chosen = next((entry for entry in headings if entry[2] == wanted), None)
    if chosen is None:
        chosen = next((entry for entry in headings if wanted in entry[2]), None)
    if chosen is None:
        return ""
    start, level, _ = chosen
    for index, next_level, _ in headings:
        if index > start and next_level <= level:
            return "\n".join(lines[start + 1:index]).strip()
    return "\n".join(lines[start + 1:]).strip()


def parse_omp(text):
    final = ""
    marker = text.rfind(_FINAL_MARKER)
    if marker >= 0:
        final = text[marker + len(_FINAL_MARKER):].strip()
    result = None
    for match in _OMP_RESULT_RE.finditer(final):
        value = match.group(1).lower()
        result = value if value in ("done", "partial", "blocked") else None
    costs = _COST_RE.findall(text)
    return {
        "session": last_line_value(text, "SESSION"),
        "status": last_line_value(text, "STATUS"),
        "cost": float(costs[-1]) if costs else 0.0,
        "final": final,
        "result": result,
        "blocked": section(final, "BLOCKED"),
    }
