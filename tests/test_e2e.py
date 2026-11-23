"""End-to-end scheduler run against the fake claude and omp workers."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from swarm import board, config, db, gitops, procs, scheduler

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = REPO_ROOT / "tests" / "fakes" / "claude"
FAKE_OMP = REPO_ROOT / "tests" / "fakes" / "omp-run"
NOW = "2026-11-01T00:00:00Z"
MAX_TICKS = 60


class E2ETest(unittest.TestCase):
    def setUp(self):
        scheduler.reset_skip_log()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "swarm"
        self.home.mkdir()
        self.repo = self.root / "demo"
        self.repo.mkdir()
        self.git_env = {
            key: value for key, value in os.environ.items() if key != "SWARM_WORKER"
        }
        self.git_env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        })
        self._old_env = {}
        for key, value in {
            "SWARM_HOME": str(self.home),
            "SWARM_NOW": NOW,
            **self.git_env,
        }.items():
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        os.environ.pop("SWARM_WORKER", None)
        self.addCleanup(self._restore_env)

        self.git("init", "-q", "-b", "main")
        (self.repo / "docs" / "plans").mkdir(parents=True)
        (self.repo / "docs" / "plans" / "README.md").write_text("plans\n")
        (self.repo / "README.md").write_text("demo\n")
        self.git("add", "-A")
        self.git("commit", "-m", "init")

        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        board.add_project(self.conn, "demo", str(self.repo))
        board.set_project(self.conn, "demo", verify_cmd="true",
                          verify_allow="Bash(true:*)", auto=1)
        self.project = self.conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        gitops.install_guard(self.project)
        self.assertTrue(gitops.guard_installed(self.project))

        self._write_config()
        self.cfg = config.load()
        self.launcher = procs.Launcher()

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_MODE", None)

    # -- helpers -----------------------------------------------------------

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_config(self):
        lines = [
            f"claude_bin = {json.dumps(str(FAKE_CLAUDE))}",
            f"omp_runner = {json.dumps(str(FAKE_OMP))}",
            "daemon_interval_s = 1",
            f"disk_path = {json.dumps(str(self.root))}",
            "disk_floor_gb = 0",
            "worktree_reserve_gb = 0",
            "max_active_plans = 3",
            "max_claude_jobs = 2",
        ]
        (self.home / "config.toml").write_text("\n".join(lines) + "\n")

    def git(self, *args):
        env = dict(os.environ)
        env.update(self.git_env)
        result = subprocess.run(
            ["git", "-C", str(self.repo),
             "-c", "user.name=t", "-c", "user.email=t@example.com",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             *args],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            self.fail(f"git {args} failed: {result.stderr}")
        return result.stdout

    def git_rc(self, *args):
        env = dict(os.environ)
        env.update(self.git_env)
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, text=True, env=env,
        )

    def _plan(self, ref="0007"):
        return board.get_plan(self.conn, "demo", ref)

    def _runs(self):
        return self.conn.execute(
            "SELECT id, kind, purpose, status, cost_usd FROM runs ORDER BY id"
        ).fetchall()

    def _diagnostics(self, actions):
        runs = "\n".join(
            f"  run #{row['id']} {row['kind']}/{row['purpose']} "
            f"{row['status']} ${row['cost_usd']:.2f}"
            for row in self._runs()
        ) or "  (no runs)"
        plans = self.conn.execute(
            "SELECT project, ref, state, path FROM plans ORDER BY id"
        ).fetchall()
        pl = "\n".join(
            f"  {row['project']}/{row['ref']} {row['state']} "
            f"path={row['path']}" for row in plans
        ) or "  (no plans)"
        asks = self.conn.execute(
            "SELECT id, state, action, question FROM asks ORDER BY id"
        ).fetchall()
        ak = "\n".join(
            f"  ask #{row['id']} {row['state']} {row['action']}: "
            f"{(row['question'] or '')[:200]}" for row in asks
        ) or "  (no asks)"
        return ("\n  actions:\n" + "\n".join(f"    {line}" for line in actions)
                + "\n  runs:\n" + runs + "\n  plans:\n" + pl
                + "\n  asks:\n" + ak)

    def _tick_until(self, predicate, description):
        actions = []
        for _ in range(MAX_TICKS):
            actions.extend(scheduler.tick(self.conn, self.cfg, self.launcher))
            if predicate():
                return actions
            time.sleep(0.2)
        self.fail(f"timed out after {MAX_TICKS} ticks waiting for {description}"
                  + self._diagnostics(actions))

    # -- tests -------------------------------------------------------------

    def test_e2e(self):
        goal_id = board.add_goal(self.conn, "demo", "Do the fake thing",
                                 "omp-fake-work.txt exists and true passes")
        board.set_goal_state(self.conn, goal_id, "ACTIVE")

        actions = self._tick_until(
            lambda: self.conn.execute(
                "SELECT 1 FROM asks WHERE action LIKE 'goal-done:%' "
                "AND state = 'OPEN'"
            ).fetchone() is not None,
            "the goal-done ask",
        )
        diag = self._diagnostics(actions)

        plan = self._plan()
        self.assertEqual(plan["state"], "DONE", "plan state\n" + diag)
        self.assertEqual(plan["path"], "docs/plans/0007-fake.md", diag)

        subject = self.git("log", "-1", "--format=%s").strip()
        self.assertTrue(subject.startswith("Merge plan/0007"),
                        f"main head is {subject!r}\n{diag}")

        worktree = Path(gitops.worktree_path(self.project, "0007"))
        self.assertFalse(worktree.exists(), f"worktree {worktree} still exists\n{diag}")

        branch = plan["branch"]
        self.assertIsNotNone(branch, diag)
        gone = self.git_rc("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        self.assertNotEqual(gone.returncode, 0,
                            f"branch {branch} still exists\n{diag}")

        runs = self._runs()
        self.assertEqual([row["kind"] for row in runs],
                         ["architect", "reviewer", "implementer", "verifier", "runner"],
                         "run kinds\n" + diag)
        self.assertEqual([row["purpose"] for row in runs],
                         ["create", "review", "implement", "verify", "preverify"],
                         "run purposes\n" + diag)
        self.assertAlmostEqual(board.month_spend(self.conn), 0.03, places=6,
                               msg="month spend\n" + diag)

        ask = self.conn.execute(
            "SELECT * FROM asks WHERE action LIKE 'goal-done:%' AND state = 'OPEN'"
        ).fetchone()
        self.assertIn("Is goal #", ask["question"], diag)
        board.answer_ask(self.conn, ask["id"], "done")
        actions = scheduler.tick(self.conn, self.cfg, self.launcher)
        diag = self._diagnostics(actions)

        goal = self.conn.execute(
            "SELECT state FROM goals WHERE id = ?", (goal_id,)
        ).fetchone()
        self.assertEqual(goal["state"], "DONE", "goal state\n" + diag)
        self.assertEqual(self._plan()["state"], "DONE", "plan state\n" + diag)

    def test_e2e_usage_limit(self):
        os.environ["FAKE_CLAUDE_MODE"] = "limit"
        goal_id = board.add_goal(self.conn, "demo", "Do the fake thing",
                                 "omp-fake-work.txt exists and true passes")
        board.set_goal_state(self.conn, goal_id, "ACTIVE")

        actions = self._tick_until(
            lambda: self.conn.execute(
                "SELECT 1 FROM plans WHERE project = 'demo' AND state = 'BLOCKED'"
            ).fetchone() is not None,
            "the plan to block on the usage limit",
        )
        diag = self._diagnostics(actions)

        plan = self.conn.execute(
            "SELECT * FROM plans WHERE project = 'demo' AND state = 'BLOCKED'"
        ).fetchone()
        ask = self.conn.execute(
            "SELECT * FROM asks WHERE plan_id = ? AND state = 'OPEN' ORDER BY id DESC",
            (plan["id"],),
        ).fetchone()
        self.assertIsNotNone(ask, diag)
        self.assertIn("Claude usage limit hit", ask["question"], diag)

        before = len(self._runs())
        scheduler.tick(self.conn, self.cfg, self.launcher)
        scheduler.tick(self.conn, self.cfg, self.launcher)
        self.assertEqual(len(self._runs()), before,
                         "a blocked plan launched another run\n" + self._diagnostics([]))


if __name__ == "__main__":
    unittest.main()
