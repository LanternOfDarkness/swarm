import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from swarm import board, config, db, procs, scheduler, workers

NOW = "2026-11-17T12:00:00Z"
LATER = "2026-11-17T14:30:00Z"

SIMPLE_CONFIG = """
[windows.win]
start = "./start.sh"
length = "30m"
read = "./read.sh"
"""

FULL_CONFIG = """
safety_stop = "verified"
stop_pattern = "SAFETY STOP"

[steps.build]
cmd = "make release"
timeout = "30m"
locks = ["release-build"]

[steps.boot]
cmd = "./boot.sh"
timeout = "10m"

[windows.win]
before = ["build", "boot"]
start = "./start.sh"
length = "30m"
stop = "echo stopped"
read = "./read.sh"

[windows.prod]
start = "./prod.sh"
length = "1h"
read = "./read-prod.sh"
production = true
"""


class FakeLauncher:
    def __init__(self):
        self.launches = []
        self.killed = []
        self.alive_result = True
        self.launch_error = None
        self._pid = 500

    def launch(self, run_id, argv, cwd, env):
        if self.launch_error:
            raise self.launch_error
        pid = self._pid
        self._pid += 1
        self.launches.append({"run_id": run_id, "argv": list(argv), "cwd": cwd,
                              "env": env, "pid": pid})
        return pid

    def alive(self, pid):
        if callable(self.alive_result):
            return self.alive_result(pid)
        return self.alive_result

    def kill(self, pid):
        self.killed.append(pid)


class SyncLauncher:
    """Runs each command to completion at launch time and records its exit code."""

    def __init__(self):
        self.launches = []
        self.killed = []
        self.running = set()

    def launch(self, run_id, argv, cwd, env):
        self.launches.append({"run_id": run_id, "argv": list(argv), "cwd": cwd})
        pid = 900000 + run_id
        if "sleep" in " ".join(argv):
            self.running.add(pid)
            return pid
        log, exit_file, _ = procs.run_paths(run_id)
        with open(log, "a", encoding="utf-8") as fh:
            proc = subprocess.run(argv, cwd=cwd, env=env, stdout=fh,
                                  stderr=subprocess.STDOUT, timeout=60)
        exit_file.write_text(str(proc.returncode))
        return pid

    def alive(self, pid):
        return pid in self.running

    def kill(self, pid):
        self.killed.append(pid)
        self.running.discard(pid)


class FakeGit:
    def __init__(self):
        self.calls = []
        self.main_reason = None
        self.plan_sha = "sha-" + "a" * 20
        self.commit_files_map = {}
        self.commits = 1
        self.update_branch_sha = "ib-updated"
        self.update_branch_error = None
        self.merge_sha = "m" * 40
        self.merge_error = None
        self.cleanup_error = None
        self.ib_head_sha = "ib-head"
        self.ib_head_error = None
        self.worktree_result = ("/tmp/wt-0001", "plan/0042-do-it", "base-sha")
        self.create_error = None
        self.create_raises = {}
        self.cleaned = []

    def main_ready(self, project):
        self.calls.append("main_ready")
        return self.main_reason

    def plan_commit(self, project, path, since_iso):
        self.calls.append(("plan_commit", path))
        return self.plan_sha

    def commit_files(self, project, sha):
        self.calls.append(("commit_files", sha))
        return list(self.commit_files_map.get(sha, []))

    def new_commits(self, project, base_sha, branch):
        self.calls.append(("new_commits", base_sha, branch))
        return self.commits

    def create_worktree(self, project, plan):
        self.calls.append("create_worktree")
        if plan["ref"] in self.create_raises:
            raise self.create_raises[plan["ref"]]
        if self.create_error:
            raise ValueError(self.create_error)
        return self.worktree_result

    def update_branch(self, project, plan):
        self.calls.append("update_branch")
        if self.update_branch_error:
            raise ValueError(self.update_branch_error)
        return self.update_branch_sha

    def merge_into_main(self, project, plan, expected_ib_sha):
        self.calls.append(("merge_into_main", expected_ib_sha))
        if self.merge_error:
            raise ValueError(self.merge_error)
        return self.merge_sha

    def cleanup(self, project, plan):
        self.calls.append("cleanup")
        if self.cleanup_error:
            raise ValueError(self.cleanup_error)
        self.cleaned.append(plan["ref"])

    def ib_head(self, project):
        self.calls.append("ib_head")
        if self.ib_head_error:
            raise ValueError(self.ib_head_error)
        return self.ib_head_sha


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        scheduler.reset_skip_log()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "docs" / "plans").mkdir(parents=True)
        self._old_env = {}
        for key, value in {"SWARM_HOME": str(self.home), "SWARM_NOW": NOW}.items():
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        self.addCleanup(self._restore_env)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = config.Config()
        self.cfg.claude_bin = "/fake/claude"
        self.cfg.omp_runner = "/fake/omp-run.py"
        self.cfg.disk_path = self.tmp.name
        self.cfg.disk_floor_gb = 0
        self.cfg.worktree_reserve_gb = 0
        self.launcher = FakeLauncher()
        self.git = FakeGit()
        self.project = "p"
        board.add_project(self.conn, self.project, str(self.repo),
                          gates_file="docs/plans/PENDING-GATES.md")
        board.set_project(self.conn, self.project, auto=True,
                          verify_cmd="python3 -m unittest",
                          verify_allow="Bash(ls:*)")

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---------------------------------------------------------- helpers

    def goal(self, title="goal", done_when="it works", state="ACTIVE", priority=0,
             est_minutes=None, created_at=None):
        goal_id = board.add_goal(self.conn, self.project, title, done_when,
                                 priority=priority, est_minutes=est_minutes)
        if state != "QUEUED":
            board.set_goal_state(self.conn, goal_id, state)
        if created_at is not None:
            self.conn.execute("UPDATE goals SET created_at = ? WHERE id = ?",
                              (created_at, goal_id))
            self.conn.commit()
        return goal_id

    def plan(self, ref="0001", title="do it", state="DRAFT", path=None, goal_id=None,
             **fields):
        board.add_plan(self.conn, self.project, ref, title, path=path, goal_id=goal_id)
        updates = {"state": state, **fields}
        self._set_plan(ref, **updates)
        return board.get_plan(self.conn, self.project, ref)

    def _set_plan(self, ref, **fields):
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.conn.execute(
            f"UPDATE plans SET {assignments} WHERE project = ? AND ref = ?",
            (*fields.values(), self.project, ref),
        )
        self.conn.commit()

    def plan_row(self, ref="0001"):
        return board.get_plan(self.conn, self.project, ref)

    def start_run(self, purpose, ref="0001", kind=None, pid=900, timeout_s=None,
            exit_code=None, log=None, step=None):
        plan = self.plan_row(ref)
        run_id = board.start_run(self.conn, kind or scheduler.PURPOSE_KIND[purpose],
                                 plan_id=plan["id"], timeout_s=timeout_s,
                                 purpose=purpose, step=step)
        if pid is not None:
            board.set_run_pid(self.conn, run_id, pid)
        log_path, exit_path, _ = procs.run_paths(run_id)
        if log is not None:
            log_path.write_text(log)
        if exit_code is not None:
            exit_path.write_text(str(exit_code))
        return run_id

    def finished_run(self, purpose, ref="0001", status="OK", report_path=None,
                     step=None):
        plan = self.plan_row(ref)
        run_id = board.start_run(self.conn, scheduler.PURPOSE_KIND[purpose],
                                 plan_id=plan["id"], purpose=purpose, step=step)
        board.end_run(self.conn, run_id, status, report_path=report_path)
        return run_id

    def runner_config(self, text=None, name=None):
        directory = self.home / "projects"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name or self.project}.toml"
        path.write_text(SIMPLE_CONFIG if text is None else text)
        return path

    def plan_file(self, ref="0001", text=None):
        path = self.repo / "docs" / "plans" / f"{ref}.md"
        path.write_text("# plan\n" if text is None else text)
        return str(path.relative_to(self.repo))

    def gate_window_plan(self, ref="0001", window="win"):
        return self.plan_file(ref, f"# plan {ref}\n\nGate window: {window}\n")

    def measuring_plan(self, ref="0001", window="win", goal_state="ACTIVE",
                       measure_from=0, **fields):
        goal_id = self.goal(title=f"goal {ref}", state=goal_state)
        path = self.gate_window_plan(ref, window)
        return self.plan(ref=ref, state="MEASURING", path=path, goal_id=goal_id,
                         window=window, measure_from=measure_from, **fields)

    def judge_ready(self, ref="0001", window="win"):
        goal_id = self.goal(title=f"goal {ref}")
        path = self.gate_window_plan(ref, window)
        plan = self.plan(ref=ref, state="MEASURING", path=path, goal_id=goal_id,
                         window=window, measure_from=0)
        self.finished_run("window", ref=ref, step=window)
        read_id = self.finished_run("read", ref=ref, step=window)
        return plan, read_id

    def locks(self):
        return [row["name"] for row in
                self.conn.execute("SELECT name FROM locks ORDER BY name")]

    def finish_launch(self, index=0, exit_code=0):
        run_id = self.launcher.launches[index]["run_id"]
        _, exit_path, _ = procs.run_paths(run_id)
        exit_path.write_text(str(exit_code))
        return run_id

    def claude(self, result, is_error=False, cost=0.5, session="cs1"):
        return json.dumps({"result": result, "is_error": is_error,
                           "total_cost_usd": cost, "session_id": session})

    def omp(self, result="done", session="os1", cost=1.25, blocked="",
            body="work done"):
        lines = ["---- worker final message ----", body, f"## RESULT: {result}"]
        if blocked:
            lines += ["## BLOCKED", blocked]
        return "\n".join(lines) + f"\n\nSESSION: {session}\nCOST: ${cost}"

    def tick(self, now=None):
        return scheduler.tick(self.conn, self.cfg, self.launcher, git=self.git,
                              now=now)

    def asks(self, prefix=None):
        rows = self.conn.execute("SELECT * FROM asks ORDER BY id").fetchall()
        if prefix is None:
            return rows
        return [row for row in rows if (row["action"] or "").startswith(prefix)]

    def run_row(self, run_id):
        return self.conn.execute("SELECT * FROM runs WHERE id = ?",
                                 (run_id,)).fetchone()

    def purposes(self):
        return [self.run_row(item["run_id"])["purpose"] for item in
                self.launcher.launches]

    # ------------------------------------------------------------ tests

    def test_archived_project_is_never_launched(self):
        board.add_project(self.conn, "gone", str(self.repo))
        board.set_project(self.conn, "gone", auto=True,
                          verify_cmd="true", verify_allow="Bash(true:*)")
        goal_id = board.add_goal(self.conn, "gone", "gone goal", "done")
        board.set_goal_state(self.conn, goal_id, "QUEUED")
        board.add_plan(self.conn, "gone", "0001", "gone plan", goal_id=goal_id)
        self.conn.execute(
            "UPDATE plans SET state = 'APPROVED', path = 'docs/plans/0001.md' "
            "WHERE project = 'gone'"
        )
        self.conn.execute("UPDATE projects SET archived = 1 WHERE name = 'gone'")
        self.conn.commit()

        actions = self.tick()

        self.assertEqual(self.launcher.launches, [])
        self.assertFalse([text for text in actions if "gone" in text])
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()["state"],
            "QUEUED",
        )

    def test_goal_then_placeholder_and_architect_launch(self):
        goal_id = self.goal(title="boot the check")
        actions = self.tick()
        row = self.plan_row(f"g{goal_id}")
        self.assertEqual(row["state"], "DRAFT")
        self.assertEqual(row["goal_id"], goal_id)
        self.assertTrue(any("placeholder" in line for line in actions))
        self.assertEqual(self.launcher.launches, [])

        actions = self.tick()
        self.assertEqual(len(self.launcher.launches), 1)
        launch = self.launcher.launches[0]
        self.assertEqual(self.purposes(), ["create"])
        self.assertEqual(launch["argv"][0], "/fake/claude")
        self.assertIn("--agent", launch["argv"])
        self.assertIn("swarm-architect", launch["argv"])
        self.assertEqual(launch["cwd"], str(self.repo))
        self.assertNotIn("--log-dir", launch["argv"])
        self.assertEqual(self.run_row(launch["run_id"])["kind"], "architect")

    def test_create_finish_renames_and_moves_to_review(self):
        goal_id = self.goal()
        self.plan(ref="g1", path=None, goal_id=goal_id)
        plan_path = "docs/plans/0007-thing.md"
        (self.repo / plan_path).write_text("# thing\n")
        report = f"PLAN: {plan_path}\nREF: 0007\nTITLE: The thing"
        run_id = self.start_run("create", ref="g1", exit_code=0,
                          log=self.claude(report, cost=0.75))
        self.git.commit_files_map[self.git.plan_sha] = [plan_path]
        self.tick()

        row = self.plan_row("0007")
        self.assertEqual(row["state"], "REVIEW")
        self.assertEqual(row["path"], plan_path)
        self.assertEqual(row["title"], "The thing")
        with self.assertRaises(LookupError):
            self.plan_row("g1")
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "OK")
        self.assertAlmostEqual(run["cost_usd"], 0.75)
        self.assertTrue(run["report_path"].endswith(".md"))
        self.assertIn("The thing", Path(run["report_path"]).read_text())

    def test_create_missing_ref_blocks(self):
        goal_id = self.goal()
        self.plan(ref="g1", goal_id=goal_id)
        run_id = self.start_run("create", ref="g1", exit_code=0,
                          log=self.claude("PLAN: docs/plans/x.md\nTITLE: X"))
        self.tick()
        self.assertEqual(self.plan_row("g1")["state"], "BLOCKED")
        self.assertEqual(self.run_row(run_id)["status"], "FAILED")
        self.assertIn("PLAN/REF/TITLE", self.asks("unblock:p/g1")[0]["question"])

    def test_create_commit_outside_plans_dir_blocks(self):
        goal_id = self.goal()
        self.plan(ref="g1", goal_id=goal_id)
        plan_path = "docs/plans/0007-thing.md"
        (self.repo / plan_path).write_text("# thing\n")
        self.start_run("create", ref="g1", exit_code=0,
                 log=self.claude(f"PLAN: {plan_path}\nREF: 0007\nTITLE: T"))
        self.git.commit_files_map[self.git.plan_sha] = ["src/main.py"]
        self.tick()
        self.assertEqual(self.plan_row("g1")["state"], "BLOCKED")
        self.assertIn("outside docs/plans", self.asks()[0]["question"])

    def test_create_missing_plan_file_blocks(self):
        goal_id = self.goal()
        self.plan(ref="g1", goal_id=goal_id)
        self.start_run("create", ref="g1", exit_code=0,
                 log=self.claude("PLAN: docs/plans/none.md\nREF: 0007\nTITLE: T"))
        self.tick()
        self.assertEqual(self.plan_row("g1")["state"], "BLOCKED")
        self.assertIn("does not exist", self.asks()[0]["question"])

    def test_review_approve(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="REVIEW", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.cfg.max_active_plans = 0
        self.start_run("review", exit_code=0,
                 log=self.claude("looks good\nRULINGS: none\nVERDICT: APPROVE"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "APPROVED")

    def test_review_revise_then_revise_launch(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="REVIEW", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.cfg.max_active_plans = 0
        text = "fix the tests\nRULINGS: none\nVERDICT: REVISE"
        run_id = self.start_run("review", exit_code=0, log=self.claude(text))
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "DRAFT")
        self.assertEqual(row["review_rounds"], 1)
        self.assertEqual(row["last_review_path"],
                         self.run_row(run_id)["report_path"])
        self.assertEqual(self.purposes(), ["revise"])
        prompt = self.launcher.launches[0]["argv"][2]
        self.assertIn("fix the tests", prompt)

    def test_revise_with_missing_review_report_blocks(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="DRAFT", path="docs/plans/0001.md",
                  review_rounds=1, last_review_path="/missing/review.md",
                  goal_id=goal_id)
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "BLOCKED")
        self.assertEqual(self.launcher.launches, [])
        question = self.asks("unblock:p/0001")[0]["question"]
        self.assertIn("cannot revise p/0001: review report "
                      "/missing/review.md is missing", question)

    def test_review_third_revise_blocks(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="REVIEW", path="docs/plans/0001.md",
                  goal_id=goal_id, review_rounds=2)
        self.start_run("review", exit_code=0,
                 log=self.claude("nope\nRULINGS: none\nVERDICT: REVISE"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn("still REVISE after 2 revisions", self.asks()[0]["question"])

    def test_review_rulings_ask(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="REVIEW", path="docs/plans/0001.md",
                  goal_id=goal_id)
        text = ("## Rulings needed\n- use SQLite or JSON?\n\n"
                "RULINGS: 1\nVERDICT: REVISE")
        run_id = self.start_run("review", exit_code=0, log=self.claude(text))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        asks = self.asks("rulings:p/0001")
        self.assertEqual(len(asks), 1)
        question = asks[0]["question"]
        self.assertIn("needs your rulings", question)
        self.assertIn("use SQLite or JSON?", question)
        self.assertIn(self.run_row(run_id)["report_path"], question)

    def test_rulings_answer_revises_with_note(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="BLOCKED", path="docs/plans/0001.md",
                  goal_id=goal_id, blocked_from="REVIEW")
        review_report = self.home / "runs" / "review.md"
        review_report.parent.mkdir(parents=True, exist_ok=True)
        review_report.write_text("ruling context")
        review_id = self.finished_run("review", report_path=str(review_report))
        ask_id = board.add_ask(self.conn, "rulings?", project=self.project,
                               plan_id=self.plan_row("0001")["id"],
                               action="rulings:p/0001")
        board.answer_ask(self.conn, ask_id, "Use SQLite")
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "DRAFT")
        self.assertIsNone(row["operator_note"])
        self.assertEqual(row["last_review_path"], str(review_report))
        self.assertEqual(self.purposes(), ["revise"])
        prompt = self.launcher.launches[0]["argv"][2]
        self.assertIn("Operator answer: Use SQLite", prompt)
        self.assertIsNotNone(self.conn.execute(
            "SELECT handled_at FROM asks WHERE id = ?", (ask_id,)
        ).fetchone()["handled_at"])
        self.assertEqual(self.run_row(review_id)["purpose"], "review")

    def test_approved_held_by_disk(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="APPROVED", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.cfg.disk_floor_gb = 10 ** 9
        actions = self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertEqual(self.plan_row("0001")["state"], "APPROVED")
        self.assertNotIn("create_worktree", self.git.calls)
        self.assertTrue(any("disk" in line for line in actions))

    def test_approved_held_by_budget_one_ask(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="APPROVED", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.finished_run("implement")
        self.conn.execute("UPDATE runs SET cost_usd = 1000.0")
        self.conn.commit()
        actions = self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertNotIn("create_worktree", self.git.calls)
        self.assertEqual(len(self.asks("budget:")), 1)
        self.assertTrue(any("budget" in line for line in actions))
        self.tick()
        self.assertEqual(len(self.asks("budget:")), 1)

    def test_approved_held_by_max_active_plans(self):
        goal_id = self.goal()
        for index in range(3):
            self.plan(ref=f"000{index}", state="EXECUTING", path="docs/plans/x.md",
                      goal_id=goal_id, worktree="/tmp/wt", branch="plan/x",
                      base_sha="base")
            self.start_run("implement", ref=f"000{index}", pid=800 + index)
        self.plan(ref="0009", state="APPROVED", path="docs/plans/0009.md",
                  goal_id=goal_id)
        self.tick()
        self.assertNotIn("create_worktree", self.git.calls)
        self.assertEqual(self.plan_row("0009")["state"], "APPROVED")

    def test_approved_starts_executing_and_implements(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="APPROVED", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "EXECUTING")
        self.assertEqual(row["worktree"], "/tmp/wt-0001")
        self.assertEqual(row["branch"], "plan/0042-do-it")
        self.assertEqual(row["base_sha"], "base-sha")
        self.assertEqual(self.purposes(), ["implement"])
        argv = self.launcher.launches[0]["argv"]
        self.assertIn("/fake/omp-run.py", argv)
        self.assertEqual(self.launcher.launches[0]["cwd"], "/tmp/wt-0001")
        self.assertEqual(self.run_row(self.launcher.launches[0]["run_id"])
                         ["timeout_s"], self.cfg.impl_timeout_s())

    def test_implement_launch_passes_omp_log_dir(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="APPROVED", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.tick()
        launch = self.launcher.launches[0]
        argv = launch["argv"]
        self.assertEqual(argv[:2], ["python3", self.cfg.omp_runner])
        self.assertEqual(argv[2:4], [
            "--log-dir",
            str(self.home / "runs" / f"{launch['run_id']}-omp"),
        ])
        self.assertIn("--cwd", argv)

    def test_launch_oserror_fails_run_and_blocks_plan(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.launcher.launch_error = OSError("no such worktree")
        actions = self.tick()
        self.assertTrue(any("cannot start implement for p/0001: "
                            "no such worktree" in line for line in actions))
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertEqual(self.launcher.launches, [])
        runs = self.conn.execute("SELECT * FROM runs").fetchall()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "FAILED")
        self.assertEqual(runs[0]["summary"], "cannot start: no such worktree")
        self.assertIsNone(runs[0]["pid"])
        question = self.asks("unblock:p/0001")[0]["question"]
        self.assertIn("cannot start implement for p/0001: no such worktree",
                      question)
        self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertEqual(len(self.conn.execute("SELECT * FROM runs").fetchall()), 1)

    def test_implement_finish_moves_to_verifying(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        run_id = self.start_run("implement", exit_code=0, log=self.omp(session="s9"))
        self.git.commits = 2
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "VERIFYING")
        self.assertEqual(row["omp_session"], "s9")
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "OK")
        self.assertAlmostEqual(run["cost_usd"], 1.25)
        self.assertEqual(self.purposes(), ["verify"])
        report = self.plan_row("0001")
        self.assertIn(run["report_path"],
                      self.launcher.launches[0]["argv"][2])
        self.assertEqual(report["state"], "VERIFYING")

    def test_implement_no_commits_blocks(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        self.git.commits = 0
        self.start_run("implement", exit_code=0, log=self.omp())
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn("no new commits", self.asks()[0]["question"])

    def test_implement_cost_limit_blocks(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        run_id = self.start_run("implement", exit_code=3, log=self.omp())
        self.tick()
        self.assertEqual(self.run_row(run_id)["status"], "COST_LIMIT")
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn("omp cost limit hit", self.asks()[0]["question"])

    def test_claude_implementer_launch(self):
        goal_id = self.goal()
        board.set_project(self.conn, self.project, implementer="claude")
        self.plan(ref="0001", state="APPROVED", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "EXECUTING")
        self.assertEqual(self.purposes(), ["implement-claude"])
        launch = self.launcher.launches[0]
        self.assertEqual(launch["argv"][0], self.cfg.claude_bin)
        self.assertEqual(launch["argv"][1], "-p")
        self.assertEqual(launch["cwd"], "/tmp/wt-0001")
        self.assertEqual(self.run_row(launch["run_id"])["timeout_s"],
                         self.cfg.impl_timeout_s())

    def test_claude_implementer_slot_busy_skips(self):
        goal_id = self.goal()
        board.set_project(self.conn, self.project, implementer="claude")
        self.cfg.max_claude_jobs = 1
        self.plan(ref="0002", state="EXECUTING", path="docs/plans/0002.md",
                  goal_id=goal_id, worktree="/tmp/wt-0002",
                  branch="plan/0043-do-it", base_sha="base-sha")
        self.start_run("implement-claude", ref="0002", pid=600)
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        actions = self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertEqual(self.plan_row("0001")["state"], "EXECUTING")
        self.assertTrue(any("Claude slot busy" in line for line in actions))

    def test_claude_implementer_finish_moves_to_verifying(self):
        goal_id = self.goal()
        board.set_project(self.conn, self.project, implementer="claude")
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        run_id = self.start_run(
            "implement-claude", exit_code=0,
            log=self.claude("Did the work.\nRESULT: done", session="cs9"))
        self.git.commits = 1
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "VERIFYING")
        self.assertEqual(row["omp_session"], "cs9")
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "OK")
        self.assertEqual(run["summary"], "done")
        self.assertAlmostEqual(run["cost_usd"], 0.5)
        self.assertEqual(self.purposes(), ["verify"])

    def test_claude_implementer_blocked(self):
        goal_id = self.goal()
        board.set_project(self.conn, self.project, implementer="claude")
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        run_id = self.start_run(
            "implement-claude", exit_code=0,
            log=self.claude("Tried.\n\n## BLOCKED\nNeed the network.\n\n"
                            "RESULT: blocked"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertEqual(self.run_row(run_id)["summary"], "blocked")
        self.assertIn("Need the network.", self.asks()[0]["question"])

    def test_claude_implementer_no_commits_blocks(self):
        goal_id = self.goal()
        board.set_project(self.conn, self.project, implementer="claude")
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        self.git.commits = 0
        self.start_run("implement-claude", exit_code=0,
                       log=self.claude("Nothing committed.\nRESULT: done"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn("no new commits", self.asks()[0]["question"])

    def test_claude_fix_launch_resumes_session(self):
        goal_id = self.goal()
        board.set_project(self.conn, self.project, implementer="claude")
        self.plan(ref="0001", state="FIXING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  omp_session="sess-1")
        run_id = self.finished_run("verify")
        procs.run_paths(run_id)[0].with_name(f"{run_id}-fix.md").write_text("fix")
        self.tick()
        self.assertEqual(self.purposes(), ["fix-claude"])
        argv = self.launcher.launches[0]["argv"]
        self.assertEqual(argv[3:5], ["--resume", "sess-1"])
        self.assertTrue(argv[2].startswith("Verification found problems;"))
        self.assertEqual(self.run_row(self.launcher.launches[0]["run_id"])
                         ["timeout_s"], self.cfg.fix_timeout_s())

    def test_verify_fix_moves_to_fixing_and_launches_fix(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  fix_rounds=1, omp_session="os1")
        self.start_run("verify", exit_code=0,
                 log=self.claude("## FIX LIST\n1. a.py:3 wrong\nVERDICT: FIX"))
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "FIXING")
        self.assertEqual(row["fix_rounds"], 2)
        self.assertEqual(self.purposes(), ["fix"])
        argv = self.launcher.launches[0]["argv"]
        self.assertIn("--resume", argv)
        self.assertTrue(any("-fix.md" in part for part in argv))

    def test_verify_third_fix_blocks(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha", fix_rounds=2)
        self.start_run("verify", exit_code=0,
                 log=self.claude("## FIX LIST\n1. still\nVERDICT: FIX"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn("still failing after 2 fix rounds",
                      self.asks()[0]["question"])

    def test_verify_pass_updates_branch_and_preverifies(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        self.git.update_branch_sha = "ib-merged"
        self.start_run("verify", exit_code=0, log=self.claude("all green\nVERDICT: PASS"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["ib_sha"], "ib-merged")
        self.assertEqual(self.purposes(), ["preverify"])
        launch = self.launcher.launches[0]
        self.assertEqual(launch["argv"], ["sh", "-c", "python3 -m unittest"])
        self.assertEqual(launch["cwd"], "/tmp/wt-0001")
        self.assertEqual(self.run_row(launch["run_id"])["kind"], "runner")

    def test_preverify_failure_moves_to_fixing(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        self.start_run("preverify", exit_code=1, log="line 1\nboom: tests failed")
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "FIXING")
        self.assertEqual(self.purposes(), ["fix"])
        fix_files = list((self.home / "runs").glob("*-fix.md"))
        self.assertEqual(len(fix_files), 1)
        self.assertIn("boom: tests failed", fix_files[0].read_text())

    def test_merge_success_measures_and_asks_goal_done(self):
        goal_id = self.goal(title="Ship it", done_when="it ships")
        self.runner_config(FULL_CONFIG)
        self.plan(ref="0001", state="VERIFYING", path=self.gate_window_plan(),
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  ib_sha="ib0")
        self.finished_run("preverify")
        self.git.merge_sha = "a" * 40
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "MEASURING")
        self.assertEqual(row["window"], "win")
        self.assertEqual(row["merge_sha"], "a" * 40)
        self.assertEqual(self.git.cleaned, ["0001"])
        asks = self.asks(f"goal-done:{goal_id}")
        self.assertEqual(len(asks), 1)
        question = asks[0]["question"]
        self.assertIn("merged into main at aaaaaaaa", question)
        self.assertIn("Done when: it ships", question)

    def test_merge_no_gates_moves_to_done_and_still_asks_goal(self):
        goal_id = self.goal(title="Ship it", done_when="it ships")
        self.conn.execute("UPDATE projects SET gates_file = NULL WHERE name = ?",
                          (self.project,))
        self.conn.commit()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  ib_sha="ib0")
        self.finished_run("preverify")
        self.git.merge_sha = "a" * 40
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "DONE")
        self.assertEqual(row["merge_sha"], "a" * 40)
        events = self.conn.execute(
            "SELECT text FROM events WHERE kind = 'move' AND text LIKE '% DONE%' "
            "ORDER BY id DESC LIMIT 1").fetchall()
        self.assertIn("no gates file", events[0]["text"])
        self.assertEqual(len(self.asks(f"goal-done:{goal_id}")), 1)

    def test_merge_with_gates_file_no_runner_stays_merged(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path=self.plan_file(),
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  ib_sha="ib0")
        self.finished_run("preverify")
        self.git.merge_sha = "a" * 40
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "MERGED")
        self.assertEqual(row["merge_sha"], "a" * 40)
        self.assertEqual(len(self.asks(f"goal-done:{goal_id}")), 1)
        self.tick()
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "MERGED")
        self.assertEqual(self.launcher.launches, [])

    def test_goal_done_answer_moves_merged_without_runner_to_done(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="MERGED", path=self.plan_file(),
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  ib_sha="ib0", merge_sha="a" * 40)
        ask_id = board.add_ask(
            self.conn, "Is the goal done?", project=self.project,
            plan_id=self.plan_row("0001")["id"], action=f"goal-done:{goal_id}")
        board.answer_ask(self.conn, ask_id, "done")
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "DONE")

    def test_merge_ib_moved_retries_without_attempt(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  ib_sha="ib0", merge_attempts=0)
        self.finished_run("preverify")
        self.git.merge_error = "main moved"
        self.git.ib_head_sha = "ib2"
        self.git.commit_files_map["ib0..ib2"] = ["docs/plans/0007.md"]
        self.git.update_branch_sha = "ib3"
        actions = self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "VERIFYING")
        self.assertEqual(row["merge_attempts"], 0)
        self.assertEqual(row["ib_sha"], "ib3")
        self.assertEqual(self.purposes(), ["preverify"])

    def test_merge_ib_moved_blocks_after_attempts(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  ib_sha="ib0", merge_attempts=2)
        self.finished_run("preverify")
        self.git.merge_error = "main moved"
        self.git.ib_head_sha = "ib2"
        self.git.commit_files_map["ib0..ib2"] = ["src/a.py"]
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn("moved 3 times", self.asks()[0]["question"])
        self.assertEqual(self.launcher.launches, [])

    def test_merge_main_not_ready_blocks_with_unblock_action(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha",
                  ib_sha="ib0")
        self.finished_run("preverify")
        self.git.merge_error = "integration checkout is dirty"
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        asks = self.asks("unblock:p/0001")
        self.assertEqual(len(asks), 1)
        self.assertIn("integration checkout is dirty", asks[0]["question"])

    def test_unblock_after_main_not_ready_retries_merge(self):
        goal_id = self.goal()
        self.runner_config(FULL_CONFIG)
        self.plan(ref="0001", state="VERIFYING", path=self.gate_window_plan(),
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha", ib_sha="ib0")
        self.finished_run("preverify")
        self.git.merge_error = "integration checkout is dirty"
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        ask = self.asks("unblock:p/0001")[0]
        board.answer_ask(self.conn, ask["id"], "checkout cleaned")
        self.git.merge_error = None
        self.git.merge_sha = "b" * 40
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "MEASURING")
        self.assertEqual(row["merge_sha"], "b" * 40)
        merges = [call for call in self.git.calls if isinstance(call, tuple)
                  and call[0] == "merge_into_main"]
        self.assertEqual(len(merges), 2)

    def test_goal_done_answer_completes_goal_and_plans(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="MERGED", path="docs/plans/0001.md",
                  goal_id=goal_id, merge_sha="a" * 40)
        ask_id = board.add_ask(self.conn, "done?", project=self.project,
                               plan_id=self.plan_row("0001")["id"],
                               action=f"goal-done:{goal_id}")
        board.answer_ask(self.conn, ask_id, "Done, thanks")
        self.tick()
        goal = self.conn.execute("SELECT state FROM goals WHERE id = ?",
                                 (goal_id,)).fetchone()
        self.assertEqual(goal["state"], "DONE")
        self.assertEqual(self.plan_row("0001")["state"], "DONE")

    def test_goal_done_answer_not_done_adds_decision(self):
        goal_id = self.goal()
        ask_id = board.add_ask(self.conn, "done?", project=self.project,
                               action=f"goal-done:{goal_id}")
        board.answer_ask(self.conn, ask_id, "tests are still missing")
        self.tick()
        goal = self.conn.execute("SELECT state FROM goals WHERE id = ?",
                                 (goal_id,)).fetchone()
        self.assertEqual(goal["state"], "ACTIVE")
        notes = self.conn.execute(
            "SELECT text FROM events WHERE kind = 'decision'").fetchall()
        self.assertEqual([row["text"] for row in notes], ["tests are still missing"])

    def test_usage_limit_text(self):
        goal_id = self.goal()
        self.plan(ref="g1", goal_id=goal_id)
        run_id = self.start_run("create", ref="g1", exit_code=0,
                          log=self.claude("Claude usage limit reached", is_error=True))
        self.tick()
        self.assertEqual(self.run_row(run_id)["status"], "FAILED")
        ask = self.asks("unblock:p/g1")[0]
        self.assertEqual(
            ask["question"],
            "Claude usage limit hit (architect for p/g1). "
            "Answer anything when it has reset.",
        )

    def test_vanished_process_fails_and_blocks(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        self.launcher.alive_result = False
        run_id = self.start_run("implement", pid=4242)
        self.tick()
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["summary"], "process vanished")
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        question = self.asks()[0]["question"]
        log, _, _ = procs.run_paths(run_id)
        self.assertIn(f"Run #{run_id}", question)
        self.assertIn("implementer", question)
        self.assertIn(str(log), question)

    def test_exit_written_after_process_check_finishes_run(self):
        goal_id = self.goal()
        self.runner_config()
        self.plan(ref="0001", state="VERIFYING", path=self.gate_window_plan(),
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha", ib_sha="ib0")
        run_id = self.start_run("preverify", pid=4242)
        self.launcher.alive_result = False
        calls = []
        real = scheduler.procs.read_exit

        def read_exit(rid):
            calls.append(rid)
            return None if len(calls) == 1 else 0

        scheduler.procs.read_exit = read_exit
        self.addCleanup(setattr, scheduler.procs, "read_exit", real)
        self.tick()
        self.assertEqual(calls, [run_id, run_id])
        self.assertEqual(self.run_row(run_id)["status"], "OK")
        self.assertEqual(self.plan_row("0001")["state"], "MEASURING")
        self.assertEqual([ask["action"] for ask in self.asks() if
                          (ask["action"] or "").startswith("unblock:")], [])

    def test_plan_exception_does_not_stop_other_plans(self):
        goal_id = self.goal()
        for ref in ("0001", "0002"):
            self.plan(ref=ref, state="APPROVED", path=f"docs/plans/{ref}.md",
                      goal_id=goal_id)
        self.git.create_raises["0001"] = RuntimeError("worktree boom")
        actions = self.tick()
        self.assertIn("error on p/0001: RuntimeError: worktree boom", actions)
        self.assertEqual(self.plan_row("0001")["state"], "APPROVED")
        self.assertEqual(self.plan_row("0002")["state"], "EXECUTING")
        self.assertEqual(self.purposes(), ["implement"])

    def test_ib_head_error_blocks_plan(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="VERIFYING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha", ib_sha="ib0")
        self.finished_run("preverify")
        self.git.merge_error = "main moved"
        self.git.ib_head_error = "git broke"
        actions = self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        asks = self.asks("unblock:p/0001")
        self.assertEqual(len(asks), 1)
        self.assertIn("cannot read main: git broke", asks[0]["question"])
        self.assertTrue(any("blocked p/0001" in line for line in actions))

    def test_timeout_kills_and_blocks(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        run_id = self.start_run("implement", pid=777, timeout_s=100)
        self.conn.execute("UPDATE runs SET started_at = ? WHERE id = ?",
                          ("2026-11-17T00:00:00Z", run_id))
        self.conn.commit()
        self.tick(now="2026-11-17T06:00:00Z")
        self.assertEqual(self.launcher.killed, [777])
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["summary"], "timeout")
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")

    def test_pidless_running_run_is_untouched(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="EXECUTING", path="docs/plans/0001.md",
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha")
        run_id = self.start_run("implement", pid=None, exit_code=0, log=self.omp())
        self.tick()
        self.assertEqual(self.run_row(run_id)["status"], "RUNNING")
        self.assertEqual(self.launcher.launches, [])

    def test_non_auto_and_goal_less_plans_untouched(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        board.add_project(self.conn, "other", str(other))
        other_goal = board.add_goal(self.conn, "other", "x", "y")
        board.set_goal_state(self.conn, other_goal, "ACTIVE")
        board.add_plan(self.conn, "other", "0001", "x", goal_id=other_goal)
        board.add_plan(self.conn, self.project, "0002", "loose plan")
        self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertEqual(board.get_plan(self.conn, "other", "0001")["state"], "DRAFT")
        self.assertEqual(self.plan_row("0002")["state"], "DRAFT")

    def test_claude_slot_limit_and_skip_logging(self):
        goal_id = self.goal()
        self.plan(ref="0001", state="REVIEW", path="docs/plans/0001.md",
                  goal_id=goal_id)
        self.plan(ref="0002", state="REVIEW", path="docs/plans/0002.md",
                  goal_id=goal_id)
        self.plan(ref="0004", state="REVIEW", path="docs/plans/0004.md",
                  goal_id=goal_id)
        self.start_run("review", ref="0004", pid=600)
        self.cfg.max_claude_jobs = 2
        actions = self.tick()
        self.assertEqual(self.purposes(), ["review"])
        self.assertEqual(len(self.launcher.launches), 1)
        skipped = [line for line in actions if line.startswith("skip")]
        self.assertEqual(len(skipped), 1)

        actions = self.tick()
        self.assertEqual([line for line in actions if line.startswith("skip")], [])
        actions = self.tick(now=LATER)
        skipped = [line for line in actions if line.startswith("skip")]
        self.assertEqual(len(skipped), 1)

    def test_skip_logs_changed_reason_within_the_hour(self):
        goal_id = self.goal()
        review_report = self.home / "review.md"
        review_report.write_text("review")
        self.plan(ref="0001", state="DRAFT", path="docs/plans/0001.md",
                  review_rounds=1, last_review_path=str(review_report),
                  goal_id=goal_id)
        self.cfg.max_claude_jobs = 0
        skipped = [line for line in self.tick() if line.startswith("skip")]
        self.assertEqual(len(skipped), 1)
        self.assertIn("Claude slot busy", skipped[0])

        self.cfg.max_claude_jobs = 2
        self.git.main_reason = "main is dirty"
        skipped = [line for line in self.tick() if line.startswith("skip")]
        self.assertEqual(len(skipped), 1)
        self.assertIn("main not ready: main is dirty", skipped[0])

    def test_goal_order_aging_first(self):
        old = self.goal(title="old", state="QUEUED", priority=0, est_minutes=99,
                        created_at="2026-11-16T20:00:00Z")
        new = self.goal(title="new", state="QUEUED", priority=9, est_minutes=1,
                        created_at="2026-11-17T11:00:00Z")
        self.cfg.max_active_plans = 1
        self.tick()
        states = {row["id"]: row["state"] for row in self.conn.execute(
            "SELECT id, state FROM goals")}
        self.assertEqual(states[old], "ACTIVE")
        self.assertEqual(states[new], "QUEUED")

    def test_goal_order_by_estimate_when_not_aged(self):
        slow = self.goal(title="slow", state="QUEUED", priority=9, est_minutes=30,
                         created_at="2026-11-17T11:00:00Z")
        quick = self.goal(title="quick", state="QUEUED", priority=0, est_minutes=5,
                          created_at="2026-11-17T11:30:00Z")
        self.cfg.max_active_plans = 1
        self.tick()
        states = {row["id"]: row["state"] for row in self.conn.execute(
            "SELECT id, state FROM goals")}
        self.assertEqual(states[quick], "ACTIVE")
        self.assertEqual(states[slow], "QUEUED")

    def test_cleanup_failure_is_noted_not_fatal(self):
        goal_id = self.goal()
        self.runner_config()
        self.plan(ref="0001", state="VERIFYING", path=self.gate_window_plan(),
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha", ib_sha="ib0")
        self.finished_run("preverify")
        self.git.cleanup_error = "worktree busy"
        actions = self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "MEASURING")
        self.assertTrue(any("cleanup failed" in line for line in actions))

    # ------------------------------------------- merge into MEASURING

    def _verifying_plan(self, goal_id, path):
        self.plan(ref="0001", state="VERIFYING", path=path, goal_id=goal_id,
                  worktree="/tmp/wt-0001", branch="plan/0042-do-it",
                  base_sha="base-sha", ib_sha="ib0")
        self.finished_run("preverify")

    def test_merge_no_gate_window_moves_to_done(self):
        goal_id = self.goal()
        self.runner_config()
        self._verifying_plan(goal_id, self.plan_file())
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "DONE")
        events = self.conn.execute(
            "SELECT text FROM events WHERE kind = 'move' AND text LIKE '% DONE%' "
            "ORDER BY id DESC LIMIT 1").fetchall()
        self.assertIn("no gate window", events[0]["text"])

    def test_merge_unknown_gate_window_blocks(self):
        goal_id = self.goal()
        config_path = self.runner_config()
        self._verifying_plan(goal_id, self.gate_window_plan(window="nope"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        ask = self.asks("unblock:p/0001")[0]
        self.assertEqual(
            ask["question"],
            f"Plan p/0001 names gate window nope, which is not in {config_path}.",
        )

    def test_unblock_after_unknown_gate_window_measures(self):
        goal_id = self.goal(title="Ship it", done_when="it ships")
        self.runner_config("""
[windows.other]
start = "./start.sh"
length = "30m"
read = "./read.sh"
""")
        self._verifying_plan(goal_id, self.gate_window_plan(window="win"))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        ask = self.asks("unblock:p/0001")[0]
        self.runner_config()
        board.answer_ask(self.conn, ask["id"], "added the win window")
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "MEASURING")
        self.assertEqual(row["window"], "win")
        self.assertEqual(self.purposes(), ["window"])
        asks = self.asks(f"goal-done:{goal_id}")
        self.assertEqual(len(asks), 1)
        self.assertIn("merged into main at mmmmmmmm", asks[0]["question"])
        self.assertIn("Done when: it ships", asks[0]["question"])
        self.tick()
        self.assertEqual(len(self.asks(f"goal-done:{goal_id}")), 1)

    def test_merge_bad_runner_config_blocks(self):
        goal_id = self.goal()
        config_path = self.runner_config("steps = 3\n")
        self._verifying_plan(goal_id, self.gate_window_plan())
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn(str(config_path), self.asks("unblock:p/0001")[0]["question"])

    def test_merge_deferred_while_release_build_held(self):
        goal_id = self.goal()
        self.runner_config(FULL_CONFIG)
        self._verifying_plan(goal_id, self.gate_window_plan())
        self.git.merge_sha = "a" * 40
        board.take_lock(self.conn, f"release-build:{self.project}", "run:77")
        actions = self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "VERIFYING")
        self.assertIn("merge of p/0001 deferred: a release build is running", actions)
        self.assertEqual(self.git.cleaned, [])
        board.drop_lock(self.conn, f"release-build:{self.project}", "run:77")
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "MEASURING")

    # ------------------------------------------------------ measuring

    def test_runner_cmd_uses_sh_in_repo(self):
        argv, cwd = workers.runner_cmd({"repo_path": "/r"}, "make release")
        self.assertEqual(argv, ["sh", "-c", "make release"])
        self.assertEqual(cwd, "/r")

    def test_end_run_releases_run_locks(self):
        self.plan()
        run_id = self.start_run("preverify")
        board.take_lock(self.conn, f"release-build:{self.project}",
                        f"run:{run_id}")
        self.assertEqual(self.locks(), [f"release-build:{self.project}"])
        board.end_run(self.conn, run_id, "OK")
        self.assertEqual(self.locks(), [])

    def test_measure_steps_run_in_order_one_per_tick(self):
        self.runner_config(FULL_CONFIG)
        self.measuring_plan()
        first = self.tick()
        self.assertEqual(self.purposes(), ["step"])
        launch = self.launcher.launches[0]
        run = self.run_row(launch["run_id"])
        self.assertEqual(run["step"], "build")
        self.assertEqual(run["timeout_s"], 1800)
        self.assertEqual(launch["argv"], ["sh", "-c", "make release"])
        self.assertEqual(launch["cwd"], str(self.repo))
        self.assertTrue(any("launched step" in line for line in first))

        self.finish_launch()
        self.tick()
        self.assertEqual(self.purposes(), ["step", "step"])
        second = self.run_row(self.launcher.launches[1]["run_id"])
        self.assertEqual(second["step"], "boot")
        self.assertEqual(second["timeout_s"], 600)

    def test_step_lock_held_while_running_and_released_on_ok(self):
        self.runner_config(FULL_CONFIG)
        self.measuring_plan()
        self.tick()
        run_id = self.launcher.launches[0]["run_id"]
        rows = self.conn.execute("SELECT name, holder FROM locks").fetchall()
        self.assertEqual([(row["name"], row["holder"]) for row in rows],
                         [("release-build:p", f"run:{run_id}")])
        self.finish_launch()
        self.tick()
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.run_row(run_id)["status"], "OK")
        self.assertEqual(self.run_row(self.launcher.launches[1]["run_id"])["step"],
                         "boot")

    def test_step_failure_releases_lock_and_blocks(self):
        self.runner_config(FULL_CONFIG)
        self.measuring_plan()
        self.tick()
        run_id = self.finish_launch(exit_code=1)
        self.tick()
        self.assertEqual(self.run_row(run_id)["status"], "FAILED")
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertEqual(self.locks(), [])
        self.assertIn(f"Run #{run_id}", self.asks("unblock:p/0001")[0]["question"])

    def test_busy_step_lock_skips(self):
        self.runner_config(FULL_CONFIG)
        self.measuring_plan()
        board.take_lock(self.conn, "release-build:p", "run:77")
        actions = self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertIn("skip p/0001: release-build:p busy", actions)

    def test_window_launch_has_no_timeout_and_instance_lock(self):
        self.runner_config()
        self.measuring_plan()
        self.tick()
        self.assertEqual(self.purposes(), ["window"])
        launch = self.launcher.launches[0]
        run = self.run_row(launch["run_id"])
        self.assertIsNone(run["timeout_s"])
        self.assertEqual(run["step"], "win")
        self.assertEqual(launch["argv"], ["sh", "-c", "./start.sh"])
        self.assertEqual(launch["cwd"], str(self.repo))
        rows = self.conn.execute("SELECT name, holder FROM locks").fetchall()
        self.assertEqual([(row["name"], row["holder"]) for row in rows],
                         [("instance:p", f"run:{launch['run_id']}")])

    def test_shorter_window_starts_first(self):
        self.runner_config("""
[windows.short]
start = "./short.sh"
length = "10m"
read = "./read-short.sh"

[windows.long]
start = "./long.sh"
length = "2h"
read = "./read-long.sh"
""")
        self.measuring_plan(ref="0001", window="long")
        self.measuring_plan(ref="0002", window="short")
        actions = self.tick()
        self.assertEqual(len(self.launcher.launches), 1)
        self.assertEqual(
            self.run_row(self.launcher.launches[0]["run_id"])["step"], "short")
        self.assertIn("skip p/0001: instance:p busy", actions)
        self.tick()
        self.assertEqual(len(self.launcher.launches), 1)
        self.assertEqual(self.plan_row("0001")["state"], "MEASURING")

    def test_window_end_kills_runs_stop_and_launches_read(self):
        self.runner_config("""
[windows.win]
start = "./start.sh"
length = "30m"
stop = "echo stopped"
read = "./read.sh"
""")
        self.measuring_plan()
        self.tick()
        launch = self.launcher.launches[0]
        run_id = launch["run_id"]
        self.tick(now=LATER)
        self.assertIn(launch["pid"], self.launcher.killed)
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "OK")
        self.assertEqual(run["summary"], "window ended")
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.purposes(), ["window", "read"])
        read_run = self.run_row(self.launcher.launches[1]["run_id"])
        self.assertEqual(read_run["timeout_s"], 600)
        self.assertEqual(read_run["step"], "win")
        self.assertIn("stopped", procs.run_paths(run_id)[0].read_text())

    def test_stop_command_failure_is_noted(self):
        self.runner_config("""
[windows.win]
start = "./start.sh"
length = "30m"
stop = "exit 3"
read = "./read.sh"
""")
        self.measuring_plan()
        self.tick()
        actions = self.tick(now=LATER)
        self.assertEqual(self.run_row(self.launcher.launches[0]["run_id"])["status"],
                         "OK")
        self.assertIn("stop command for win failed: exit 3", actions)

    def test_window_natural_exit_after_length_runs_stop(self):
        marker = Path(self.tmp.name) / "stopped"
        self.runner_config(f"""
[windows.win]
start = "./start.sh"
length = "1s"
stop = "touch {marker}"
read = "./read.sh"
""")
        self.measuring_plan()
        self.tick()
        run_id = self.finish_launch()
        self.conn.execute(
            "UPDATE runs SET started_at = '2026-11-17T10:00:00Z' WHERE id = ?",
            (run_id,))
        self.conn.commit()
        self.tick()
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "OK")
        self.assertTrue(marker.exists())

    def test_stop_pattern_fails_run_and_blocks(self):
        self.runner_config("""
stop_pattern = "SAFETY STOP"

[windows.win]
start = "./start.sh"
length = "2h"
read = "./read.sh"
""")
        self.measuring_plan()
        self.tick()
        launch = self.launcher.launches[0]
        run_id = launch["run_id"]
        log, _, _ = procs.run_paths(run_id)
        log.write_text("all good\nSAFETY STOP\n")
        self.tick()
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["summary"], "safety stop")
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn(launch["pid"], self.launcher.killed)
        self.assertEqual(self.locks(), [])
        question = self.asks("unblock:p/0001")[0]["question"]
        self.assertIn("Safety stop seen in window win for p/0001", question)
        self.assertIn(str(log), question)

    def test_window_early_exit_blocks(self):
        self.runner_config()
        self.measuring_plan()
        self.tick()
        run_id = self.finish_launch()
        self.tick()
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["summary"], "window process exited after 0m")
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertIn("window process exited after 0m",
                      self.asks("unblock:p/0001")[0]["question"])

    def test_window_missing_config_stops_and_blocks(self):
        path = self.runner_config()
        self.measuring_plan()
        self.tick()
        launch = self.launcher.launches[0]
        path.unlink()
        self.tick(now=LATER)
        run = self.run_row(launch["run_id"])
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["summary"], "runner config missing")
        self.assertIn(launch["pid"], self.launcher.killed)
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        question = self.asks("unblock:p/0001")[0]["question"]
        self.assertIn("runner config for p no longer has window win", question)
        self.assertIn("Answer to measure again.", question)

    def test_window_malformed_config_reports_parser_error(self):
        path = self.runner_config()
        self.measuring_plan()
        self.tick()
        launch = self.launcher.launches[0]
        path.write_text("steps = 3\n")
        self.tick(now=LATER)
        run = self.run_row(launch["run_id"])
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["summary"], "runner config invalid")
        self.assertIn(launch["pid"], self.launcher.killed)
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        question = self.asks("unblock:p/0001")[0]["question"]
        self.assertIn(str(path), question)
        self.assertIn("steps is not a table", question)
        self.assertIn("the window was stopped. Answer to measure again.", question)

    def test_production_window_asks_and_waits(self):
        self.runner_config("""
[windows.prod]
start = "./prod.sh"
length = "1h"
read = "./read-prod.sh"
production = true
""")
        self.measuring_plan(window="prod")
        actions = self.tick()
        self.assertEqual(self.launcher.launches, [])
        asks = self.asks("window:p/0001")
        self.assertEqual(len(asks), 1)
        self.assertEqual(
            asks[0]["question"],
            'Start production window prod for p/0001? It runs "./prod.sh" for 60m. '
            'Answer "yes" to start; any other answer skips the window and closes '
            "the plan.",
        )
        self.assertIn("asked to start production window prod for p/0001", actions)
        self.tick()
        self.assertEqual(len(self.asks("window:p/0001")), 1)
        self.assertEqual(self.launcher.launches, [])

    def test_production_window_yes_launches(self):
        self.runner_config("""
[windows.prod]
start = "./prod.sh"
length = "1h"
read = "./read-prod.sh"
production = true
""")
        self.measuring_plan(window="prod")
        self.tick()
        ask = self.asks("window:p/0001")[0]
        board.answer_ask(self.conn, ask["id"], "Yes, start it")
        self.tick()
        self.assertEqual(self.purposes(), ["window"])
        self.assertEqual(self.plan_row("0001")["window_ok"], 1)

    def test_production_window_declined_moves_to_done(self):
        self.runner_config("""
[windows.prod]
start = "./prod.sh"
length = "1h"
read = "./read-prod.sh"
production = true
""")
        self.measuring_plan(window="prod")
        self.tick()
        ask = self.asks("window:p/0001")[0]
        board.answer_ask(self.conn, ask["id"], "not today")
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "DONE")
        self.assertEqual(self.launcher.launches, [])
        events = self.conn.execute(
            "SELECT text FROM events WHERE kind = 'move' AND text LIKE '% DONE%' "
            "ORDER BY id DESC LIMIT 1").fetchall()
        self.assertIn("production window declined: not today", events[0]["text"])

    def test_production_safety_stop_launches_without_ask(self):
        self.runner_config("""
safety_stop = "verified"

[windows.prod]
start = "./prod.sh"
length = "1h"
read = "./read-prod.sh"
production = true
""")
        self.measuring_plan(window="prod")
        self.tick()
        self.assertEqual(self.purposes(), ["window"])
        self.assertEqual(self.asks("window:"), [])

    def test_unblock_after_failed_step_reruns_from_first_step(self):
        self.runner_config(FULL_CONFIG)
        self.measuring_plan()
        self.tick()
        failed_id = self.finish_launch(exit_code=1)
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        ask = self.asks("unblock:p/0001")[0]
        board.answer_ask(self.conn, ask["id"], "fixed the build")
        self.tick()
        row = self.plan_row("0001")
        self.assertEqual(row["state"], "MEASURING")
        self.assertEqual(row["measure_from"], failed_id)
        self.assertEqual(len(self.launcher.launches), 2)
        self.assertEqual(
            self.run_row(self.launcher.launches[1]["run_id"])["step"], "build")

    def test_measuring_plan_with_done_goal_advances(self):
        self.runner_config()
        self.measuring_plan(goal_state="DONE")
        self.tick()
        self.assertEqual(self.purposes(), ["window"])

    def test_measure_missing_runner_config_blocks(self):
        self.measuring_plan()
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        self.assertEqual(self.asks("unblock:p/0001")[0]["question"],
                         "runner config for p no longer has window win")

    def test_measure_bad_runner_config_blocks(self):
        config_path = self.runner_config("steps = 3\n")
        self.measuring_plan()
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        question = self.asks("unblock:p/0001")[0]["question"]
        self.assertIn(str(config_path), question)
        self.assertIn("steps is not a table", question)

    def test_judge_launches_with_read_output(self):
        self.runner_config()
        _, read_id = self.judge_ready()
        actions = self.tick()
        self.assertEqual(self.purposes(), ["judge"])
        self.assertTrue(any("launched judge" in line for line in actions))
        launch = self.launcher.launches[0]
        run = self.run_row(launch["run_id"])
        self.assertEqual(run["kind"], "verifier")
        self.assertEqual(run["timeout_s"], self.cfg.timeout_verify_s)
        self.assertEqual(run["step"], "win")
        self.assertEqual(launch["cwd"], str(self.repo))
        log, _, _ = procs.run_paths(read_id)
        self.assertEqual(
            launch["argv"][2],
            "Write the gate result for plan docs/plans/0001.md, window win.\n"
            f"Read output: {log}\n"
            "Gates file: docs/plans/PENDING-GATES.md")
        tools = launch["argv"][launch["argv"].index("--allowedTools") + 1:
                               launch["argv"].index("--disallowedTools")]
        self.assertIn("Edit(docs/plans/PENDING-GATES.md)", tools)
        self.assertEqual([tool for tool in tools if tool.startswith("Edit(")],
                         ["Edit(docs/plans/PENDING-GATES.md)"])

    def test_judge_pass_moves_plan_done(self):
        self.runner_config()
        self.judge_ready()
        run_id = self.start_run("judge", step="win", exit_code=0,
                                log=self.claude("row written\nVERDICT: PASS",
                                                cost=0.75))
        self.tick()
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "OK")
        self.assertAlmostEqual(run["cost_usd"], 0.75)
        self.assertEqual(self.plan_row("0001")["state"], "DONE")
        events = self.conn.execute(
            "SELECT text FROM events WHERE kind = 'move' AND text LIKE '% DONE%' "
            "ORDER BY id DESC LIMIT 1").fetchall()
        self.assertIn("gate win PASS", events[0]["text"])

    def test_judge_fail_creates_follow_up_goal(self):
        self.runner_config()
        self.judge_ready()
        text = "numbers\n\n## FOLLOW-UP\nMake the soak pass.\nVERDICT: FAIL"
        self.start_run("judge", step="win", exit_code=0, log=self.claude(text))
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "DONE")
        goal = self.conn.execute(
            "SELECT * FROM goals ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(goal["project"], self.project)
        self.assertEqual(goal["title"], "Follow-up: do it")
        self.assertEqual(goal["done_when"], "Make the soak pass.")
        self.assertEqual(goal["state"], "ACTIVE")
        events = self.conn.execute(
            "SELECT text FROM events WHERE kind = 'move' AND text LIKE '% DONE%' "
            "ORDER BY id DESC LIMIT 1").fetchall()
        self.assertIn(f"gate win FAIL; follow-up goal #{goal['id']}", events[0]["text"])

    def test_judge_fail_without_follow_up_uses_gate_line(self):
        self.runner_config()
        self.judge_ready()
        self.start_run("judge", step="win", exit_code=0,
                       log=self.claude("numbers\nVERDICT: FAIL"))
        self.tick()
        goal = self.conn.execute(
            "SELECT * FROM goals ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(goal["done_when"], "gate win passes")

    def test_judge_without_verdict_blocks(self):
        self.runner_config()
        self.judge_ready()
        run_id = self.start_run("judge", step="win", exit_code=0,
                                log=self.claude("wrote a row, no verdict"))
        self.tick()
        run = self.run_row(run_id)
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["summary"], "no verdict")
        self.assertEqual(self.plan_row("0001")["state"], "BLOCKED")
        question = self.asks("unblock:p/0001")[0]["question"]
        self.assertIn("judge report has no VERDICT", question)
        self.assertIn(str(procs.run_paths(run_id)[0]), question)

    def test_judge_waits_for_claude_slot(self):
        self.runner_config()
        self.judge_ready()
        self.cfg.max_claude_jobs = 0
        actions = self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertIn("skip p/0001: Claude slot busy", actions)

    def test_judge_waits_for_main_ready(self):
        self.runner_config()
        self.judge_ready()
        self.git.main_reason = "dirty"
        actions = self.tick()
        self.assertEqual(self.launcher.launches, [])
        self.assertIn("skip p/0001: main not ready: dirty", actions)
        asks = self.asks("main-dirty:p")
        self.assertEqual(len(asks), 1)
        self.assertIn("not ready for plan commits: dirty", asks[0]["question"])

    def test_merge_deferred_while_judge_runs(self):
        self.runner_config()
        goal_id = self.goal()
        self.measuring_plan(ref="0002")
        self.start_run("judge", ref="0002", step="win")
        self.plan(ref="0001", state="VERIFYING", path=self.gate_window_plan(),
                  goal_id=goal_id, worktree="/tmp/wt-0001",
                  branch="plan/0042-do-it", base_sha="base-sha", ib_sha="ib0")
        self.finished_run("preverify")
        actions = self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "VERIFYING")
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "merge_into_main"
                             for call in self.git.calls))
        self.assertTrue(any("deferred: an architect run is running" in line
                            for line in actions))

    def test_full_measure_walk_to_done(self):
        self.launcher = SyncLauncher()
        script = self.home / "fake-claude"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'result': 'gate row written\\nVERDICT: PASS',"
            " 'is_error': False, 'total_cost_usd': 0.1, 'session_id': 's'}))\n")
        script.chmod(0o755)
        self.cfg.claude_bin = str(script)
        self.runner_config("""
[steps.build]
cmd = "true"
timeout = "10m"

[windows.soak]
before = ["build"]
start = "sleep 1"
length = "1s"
read = "true"
""")
        goal_id = self.goal(title="Ship it", done_when="it ships")
        self.plan(ref="0001", state="VERIFYING",
                  path=self.gate_window_plan(window="soak"), goal_id=goal_id,
                  worktree="/tmp/wt-0001", branch="plan/0042-do-it",
                  base_sha="base-sha", ib_sha="ib0")
        self.finished_run("preverify")
        self.git.merge_sha = "a" * 40

        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "MEASURING")
        self.assertEqual(self.run_row(self.launcher.launches[-1]["run_id"])["step"],
                         "build")
        self.tick()
        self.assertEqual(self.run_row(self.launcher.launches[-1]["run_id"])["purpose"],
                         "window")
        self.tick(now=LATER)
        self.assertEqual(self.run_row(self.launcher.launches[-1]["run_id"])["purpose"],
                         "read")
        self.tick()
        self.assertEqual(self.run_row(self.launcher.launches[-1]["run_id"])["purpose"],
                         "judge")
        self.assertEqual(self.plan_row("0001")["state"], "MEASURING")
        self.tick()
        self.assertEqual(self.plan_row("0001")["state"], "DONE")
        events = self.conn.execute(
            "SELECT text FROM events WHERE kind = 'move' AND text LIKE '% DONE%' "
            "ORDER BY id DESC LIMIT 1").fetchall()
        self.assertIn("gate soak PASS", events[0]["text"])


if __name__ == "__main__":
    unittest.main()
