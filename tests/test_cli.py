import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from swarm import board, cli, db, gitscan, procs, telegram, usage

NOW = "2026-11-17T04:39:00Z"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeTelegramApi:
    instances = []

    def __init__(self, token):
        self.token = token
        self.sent = []
        self.updates = []
        FakeTelegramApi.instances.append(self)

    def send(self, chat_id, text, reply_to=None):
        self.sent.append((chat_id, text, reply_to))
        return len(self.sent)

    def get_updates(self, offset, timeout=25):
        return list(self.updates)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        self._old_now = os.environ.get("SWARM_NOW")
        os.environ["SWARM_HOME"] = self.tmp.name
        os.environ["SWARM_NOW"] = NOW
        self.repo = tempfile.mkdtemp()
        self.extra_dirs = []

    def tearDown(self):
        for path in self.extra_dirs:
            shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home
        if self._old_now is None:
            os.environ.pop("SWARM_NOW", None)
        else:
            os.environ["SWARM_NOW"] = self._old_now

    def run_cli(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def init(self):
        return self.run_cli("init")

    def project(self):
        return self.run_cli(
            "project", "add", "demo", "--repo", self.repo, "--branch", "develop"
        )

    def plan(self, ref="1048", title="Boot check restore"):
        return self.run_cli(
            "plan", "add", f"demo/{ref}", "--title", title
        )

    def git(self, path, *args):
        env = dict(os.environ)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        return subprocess.run(
            [
                "git", "-C", str(path),
                "-c", "user.name=t", "-c", "user.email=t@example.com",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

    def make_repo(self):
        path = pathlib.Path(tempfile.mkdtemp())
        self.extra_dirs.append(str(path))
        path = path.resolve()
        self.git(path, "init", "-b", "main")
        (path / "README").write_text("hello\n")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-m", "init")
        return path

    def test_happy_path(self):
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertIn("swarm.db", out)
        self.assertIn("dashboard.html", out)

        code, _, err = self.project()
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli(
            "goal", "add", "demo", "boot check", "--done-when", "check restored"
        )
        self.assertEqual(code, 0, err)
        goal_id = int(out.strip())

        code, out, err = self.run_cli(
            "plan", "add", "demo/1048", "--title", "Boot check restore",
            "--goal", str(goal_id),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(int(out.strip()), 1)

        for state in ("REVIEW", "APPROVED", "EXECUTING", "VERIFYING", "FIXING",
                      "VERIFYING", "MERGED", "DONE"):
            code, _, err = self.run_cli("plan", "move", "demo/1048", state)
            self.assertEqual(code, 0, f"{state}: {err}")

        code, out, err = self.run_cli("plan", "show", "demo/1048")
        self.assertEqual(code, 0, err)
        self.assertIn("DONE", out)

    def test_illegal_moves(self):
        self.init()
        self.project()
        self.plan()
        self.plan("2000", "fresh plan")

        code, _, err = self.run_cli("plan", "move", "demo/2000", "EXECUTING")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("swarm: "), err)

        for state in ("REVIEW", "APPROVED", "EXECUTING", "VERIFYING", "MERGED", "DONE"):
            code, _, err = self.run_cli("plan", "move", "demo/2000", state)
            self.assertEqual(code, 0, f"{state}: {err}")
        code, _, err = self.run_cli("plan", "move", "demo/2000", "DRAFT")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("swarm: "), err)

    def test_lock_conflict(self):
        self.init()
        code, _, err = self.run_cli("lock", "take", "A", "--holder", "alice")
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("lock", "take", "A", "--holder", "bob")
        self.assertEqual(code, 1)
        self.assertIn("lock A held by alice", err)

    def test_duplicate_project_refused_one_line(self):
        self.init()
        code, _, err = self.project()
        self.assertEqual(code, 0, err)
        code, out, err = self.project()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        lines = err.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("swarm: "), err)
        self.assertIn("already exists", lines[0])

    def test_status_headings_and_json(self):
        self.init()
        self.project()
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        for heading in ("Waiting on you", "Running", "Next", "Done (last 24 h)"):
            self.assertIn(heading, out)

        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(
            set(data), {"schema", "waiting", "running", "next", "done", "arch_due",
                        "projects", "header"}
        )

    def test_status_json_panel_fields(self):
        self.init()
        code, _, err = self.project()
        self.assertEqual(code, 0, err)
        conn = db.connect()
        try:
            board.start_run(conn, "runner", purpose="verify", timeout_s=600)
            board.add_ask(
                conn, "Deploy the fix?", project="demo",
                action="deploy:demo/1048",
            )
        finally:
            conn.close()

        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["schema"], 1)

        runs = [item for item in data["running"] if item["type"] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["purpose"], "verify")
        self.assertEqual(runs[0]["timeout_s"], 600)

        asks = [item for item in data["waiting"] if item["type"] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["action"], "deploy:demo/1048")
        self.assertEqual(asks[0]["created_at"], NOW)

    def test_watch_once(self):
        conn = db.connect()
        try:
            since = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events"
            ).fetchone()[0]
            db.add_event(conn, "move", "1048 REVIEW → APPROVED")
            db.add_event(conn, "ask", "need approval")
            db.add_event(conn, "decision", "shorter goal first")

            line, last_id = cli.watch_once(
                conn, since, ("ask", "answer", "move", "decision")
            )
            self.assertIsNotNone(line)
            self.assertTrue(line.startswith("#"), line)
            self.assertIn("need approval", line)
            self.assertEqual(last_id, since + 3)

            db.add_event(conn, "run", "run #1 started")
            line, last_id = cli.watch_once(
                conn, last_id, ("ask", "answer", "move", "decision")
            )
            self.assertIsNone(line)
            self.assertEqual(last_id, since + 4)

            line, last_id = cli.watch_once(conn, since, None)
            self.assertIsNotNone(line)
            self.assertIn("run #1 started", line)
            self.assertEqual(last_id, since + 4)
        finally:
            conn.close()

    def test_scan_prints_event_and_worktree(self):
        self.init()
        path = self.make_repo()
        code, _, err = self.run_cli(
            "project", "add", "demo", "--repo", str(path), "--branch", "main"
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 0, err)
        event_lines = [line for line in out.splitlines() if "worktrees" in line]
        self.assertTrue(event_lines, out)
        self.assertTrue(event_lines[0].startswith("demo:"), out)
        self.assertIn(str(path), out)

    def test_worktrees_before_scan(self):
        self.init()
        path = self.make_repo()
        code, _, err = self.run_cli(
            "project", "add", "demo", "--repo", str(path), "--branch", "main"
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("worktrees")
        self.assertEqual(code, 0, err)
        self.assertIn("never scanned", out)

    def test_worktrees_shows_five_badges(self):
        self.init()
        code, _, err = self.project()
        self.assertEqual(code, 0, err)
        conn = db.connect()
        try:
            board.add_plan(conn, "demo", "executing-plan", "Active plan")
            for state in ("REVIEW", "APPROVED", "EXECUTING"):
                board.move_plan(conn, "demo", "executing-plan", state)
            board.add_plan(conn, "demo", "done-plan", "Done plan")
            for state in ("REVIEW", "APPROVED", "EXECUTING", "VERIFYING",
                          "MERGED", "DONE"):
                board.move_plan(conn, "demo", "done-plan", state)
            for path, plan_ref, merged, dirty in (
                (str(pathlib.Path(self.repo) / "active-wt"), "executing-plan", 1, 0),
                (str(pathlib.Path(self.repo) / "cleanup-wt"), "done-plan", 1, 0),
                (str(pathlib.Path(self.repo) / "merged-wt"), None, 1, 0),
            ):
                conn.execute(
                    "INSERT INTO worktrees (project, path, branch, head, is_main, "
                    "ahead, behind, merged, dirty, target_bytes, last_commit_at, "
                    "plan_ref, missing, scanned_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("demo", path, "topic", "0" * 40, 0, 0, 0, merged, dirty,
                     None, NOW, plan_ref, 0, NOW),
                )
            conn.commit()
        finally:
            conn.close()

        code, out, err = self.run_cli("worktrees")
        self.assertEqual(code, 0, err)
        lines = {line.split()[1]: line for line in out.splitlines()
                 if line.startswith("demo  ")}
        self.assertTrue(lines[str(pathlib.Path(self.repo) / "active-wt")]
                        .endswith(" active"), out)
        self.assertTrue(lines[str(pathlib.Path(self.repo) / "cleanup-wt")]
                        .endswith(" cleanup"), out)
        self.assertTrue(lines[str(pathlib.Path(self.repo) / "merged-wt")]
                        .endswith(" merged"), out)

    def test_worktrees_counts_only_deletable_merged_branches(self):
        self.init()
        code, _, err = self.project()
        self.assertEqual(code, 0, err)
        conn = db.connect()
        try:
            board.add_plan(conn, "demo", "executing-plan", "Active plan")
            for state in ("REVIEW", "APPROVED", "EXECUTING"):
                board.move_plan(conn, "demo", "executing-plan", state)
            for name, plan_ref in (("running", "executing-plan"),
                                   ("old-thing", None)):
                conn.execute(
                    "INSERT INTO branches (project, name, head, merged, ahead, "
                    "last_commit_at, plan_ref, scanned_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("demo", name, "0" * 40, 1, 0, NOW, plan_ref, NOW),
                )
            conn.commit()
        finally:
            conn.close()

        code, out, err = self.run_cli("worktrees")
        self.assertEqual(code, 0, err)
        self.assertIn("1 merged branches can be deleted", out)
        self.assertNotIn("2 merged branches can be deleted", out)

    def test_scan_continues_after_missing_repo(self):
        self.init()
        good = self.make_repo()
        broken = pathlib.Path(tempfile.mkdtemp())
        conn = db.connect()
        try:
            board.add_project(conn, "broken", broken)
        finally:
            conn.close()
        shutil.rmtree(broken)

        code, _, err = self.run_cli(
            "project", "add", "good", "--repo", str(good), "--branch", "main"
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 1, out)
        self.assertIn("swarm:", err)
        self.assertIn("good:", out)
        self.assertIn("worktrees", out)
        self.assertNotIn("broken:", out)

    def test_scan_continues_after_git_timeout(self):
        self.init()
        for name in ("broken", "good"):
            code, _, err = self.run_cli(
                "project", "add", name, "--repo", self.repo, "--branch", "main"
            )
            self.assertEqual(code, 0, err)

        def fake_scan(conn, name, sizes=True):
            if name == "broken":
                raise subprocess.TimeoutExpired(
                    cmd="git", timeout=gitscan.GIT_TIMEOUT
                )
            return {
                "text": "good: 0 worktrees (0 cleanup), 0 unmerged branches, "
                        "0 merged branches, targets 0.0 GB",
                "worktrees": [],
            }

        with mock.patch.object(gitscan, "scan_project", side_effect=fake_scan):
            code, out, err = self.run_cli("scan")
        self.assertEqual(code, 1, out)
        self.assertIn("swarm:", err)
        self.assertIn("good: 0 worktrees", out)
        self.assertNotIn("broken:", out)

    def test_wrapper(self):
        path = REPO_ROOT / "bin" / "swarm"
        self.assertTrue(path.is_file())
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(path.read_text().splitlines()[0], "#!/usr/bin/env sh")

    def _write_config(self, **values):
        lines = [f"{key} = {json.dumps(str(value))}" for key, value in values.items()]
        (pathlib.Path(self.tmp.name) / "config.toml").write_text(
            "\n".join(lines) + "\n"
        )

    def _auto_project(self):
        repo = self.make_repo()
        code, _, err = self.run_cli(
            "project", "add", "demo", "--repo", str(repo),
            "--branch", "main", "--verify-cmd", "true",
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("project", "guard", "demo")
        self.assertEqual(code, 0, err)
        self.assertIn("pre-push", out)
        code, _, err = self.run_cli(
            "project", "set", "demo", "--auto", "on",
            "--verify-allow", "Bash(true:*)",
        )
        self.assertEqual(code, 0, err)
        return repo

    def test_daemon_once_launches_one_run_with_fake_claude(self):
        fake = REPO_ROOT / "tests" / "fakes" / "claude"
        self._write_config(claude_bin=fake)
        self.init()
        self._auto_project()
        code, out, err = self.run_cli(
            "goal", "add", "demo", "Fake goal", "--done-when", "merged"
        )
        self.assertEqual(code, 0, err)
        goal_id = int(out.strip())
        code, _, err = self.run_cli("goal", "set", str(goal_id), "ACTIVE")
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli(
            "plan", "add", "demo/g1", "--title", "Fake goal",
            "--goal", str(goal_id),
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("daemon", "--once")
        self.assertEqual(code, 0, err)
        self.assertIn("launched create", out)
        self.assertTrue((pathlib.Path(self.tmp.name) / "daemon.heartbeat").exists())
        self.assertTrue((pathlib.Path(self.tmp.name) / "dashboard.html").exists())

        conn = db.connect()
        try:
            runs = conn.execute("SELECT * FROM runs").fetchall()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["kind"], "architect")
            self.assertEqual(runs[0]["purpose"], "create")
            self.assertIsNotNone(runs[0]["pid"])
            daemon_lock = conn.execute(
                "SELECT 1 FROM locks WHERE name = 'daemon'"
            ).fetchone()
            self.assertIsNone(daemon_lock)
        finally:
            conn.close()

    def test_daemon_refused_while_lock_holder_alive(self):
        self.init()
        holder = f"pid:{os.getpid()}"
        conn = db.connect()
        try:
            self.assertTrue(board.take_lock(conn, "daemon", holder))
        finally:
            conn.close()

        code, out, err = self.run_cli("daemon", "--once")
        self.assertEqual(code, 1)
        self.assertIn(f"daemon already running (pid {os.getpid()})", err)
        self.assertFalse((pathlib.Path(self.tmp.name) / "daemon.heartbeat").exists())

    def test_daemon_force_takes_lock_of_dead_pid(self):
        self.init()
        conn = db.connect()
        try:
            self.assertTrue(board.take_lock(conn, "daemon", "pid:999999"))
        finally:
            conn.close()

        code, out, err = self.run_cli("daemon", "--once")
        self.assertEqual(code, 0, err)
        self.assertTrue((pathlib.Path(self.tmp.name) / "daemon.heartbeat").exists())
        conn = db.connect()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM locks WHERE name = 'daemon'"
            ).fetchone())
        finally:
            conn.close()

    def test_daemon_once_reports_tick_failure_and_releases_lock(self):
        self.init()
        with mock.patch(
            "swarm.cli.scheduler.tick",
            side_effect=RuntimeError("boom tick"),
        ):
            code, out, err = self.run_cli("daemon", "--once")
        self.assertEqual(code, 1)
        self.assertIn("daemon tick failed", err)
        self.assertIn("RuntimeError: boom tick", err)
        self.assertTrue((pathlib.Path(self.tmp.name) / "daemon.heartbeat").exists())
        conn = db.connect()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM locks WHERE name = 'daemon'"
            ).fetchone())
        finally:
            conn.close()

    def test_daemon_sigterm_stops_after_tick_and_releases_lock(self):
        self.init()
        env = os.environ.copy()
        env["SWARM_HOME"] = self.tmp.name
        proc = subprocess.Popen(
            [sys.executable, "-m", "swarm", "daemon", "--interval", "30"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            heartbeat = pathlib.Path(self.tmp.name) / "daemon.heartbeat"
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not heartbeat.exists():
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            self.assertTrue(heartbeat.exists(), "daemon did not complete a tick")
            self.assertIsNone(proc.poll(), "daemon exited before SIGTERM")
            proc.terminate()
            try:
                code = proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.fail("daemon did not exit within 3 s of SIGTERM")
            self.assertEqual(code, 0)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
        conn = db.connect()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM locks WHERE name = 'daemon'"
            ).fetchone())
        finally:
            conn.close()

    def test_daemon_signal_during_tick_finishes_tick(self):
        self.init()
        seen = []
        before = signal.getsignal(signal.SIGTERM)

        def tick(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.1)
            seen.append("tick-finished")
            return []

        with mock.patch("swarm.cli.scheduler.tick", side_effect=tick):
            code, _, _ = self.run_cli("daemon", "--interval", "30")
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["tick-finished"])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        conn = db.connect()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM locks WHERE name = 'daemon'"
            ).fetchone())
        finally:
            conn.close()

    def test_daemon_rejects_nonpositive_interval_without_lock(self):
        self.init()
        for value, got in (("-1", "-1.0"), ("nan", "nan")):
            with self.subTest(interval=value):
                code, out, err = self.run_cli("daemon", "--interval", value)
                self.assertEqual(code, 1)
                self.assertIn(
                    f"swarm: --interval must be positive (got {got})", err
                )
                conn = db.connect()
                try:
                    self.assertIsNone(conn.execute(
                        "SELECT 1 FROM locks WHERE name = 'daemon'"
                    ).fetchone())
                finally:
                    conn.close()

    def test_daemon_dry_run_leaves_real_db_unchanged(self):
        self.init()
        self.project()
        self.plan()
        database = pathlib.Path(self.tmp.name) / "swarm.db"
        conn = db.connect()
        try:
            before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            before_version = conn.execute("PRAGMA user_version").fetchone()[0]
            before_locks = conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0]
        finally:
            conn.close()
        before_mtime = database.stat().st_mtime_ns

        code, out, err = self.run_cli("daemon", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(database.stat().st_mtime_ns, before_mtime)

        conn = db.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                before_events,
            )
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], before_version
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0],
                before_locks,
            )
        finally:
            conn.close()

    def test_daemon_dry_run_writes_no_reports_in_real_home(self):
        self.init()
        conn = db.connect()
        try:
            run_id = board.start_run(conn, "architect", purpose="create")
            board.set_run_pid(conn, run_id, 4242)
        finally:
            conn.close()
        log_path, exit_path, _ = procs.run_paths(run_id)
        log_path.write_text(json.dumps({
            "result": "report text", "is_error": False,
            "total_cost_usd": 0.1, "session_id": "cs1",
        }))
        exit_path.write_text("0")
        runs_dir = pathlib.Path(self.tmp.name) / "runs"

        code, out, err = self.run_cli("daemon", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(list(runs_dir.glob("*.md")), [])

        conn = db.connect()
        try:
            row = conn.execute("SELECT status, pid FROM runs WHERE id = ?",
                               (run_id,)).fetchone()
            self.assertEqual(row["status"], "RUNNING")
            self.assertEqual(row["pid"], 4242)
        finally:
            conn.close()

    def test_daemon_dry_run_previews_finished_preverify(self):
        self.init()
        self.project()
        self.plan()
        conn = db.connect()
        try:
            plan = board.get_plan(conn, "demo", "1048")
            conn.execute(
                "UPDATE plans SET state = 'VERIFYING', worktree = ?, "
                "branch = 'plan/1048-dry', base_sha = 'base', ib_sha = 'ib0' "
                "WHERE id = ?",
                (str(pathlib.Path(self.tmp.name) / "wt"), plan["id"]),
            )
            conn.commit()
            run_id = board.start_run(conn, "runner", plan_id=plan["id"],
                                     purpose="preverify")
            board.set_run_pid(conn, run_id, 4242)
        finally:
            conn.close()
        _, exit_path, _ = procs.run_paths(run_id)
        exit_path.write_text("0")
        runs_dir = pathlib.Path(self.tmp.name) / "runs"

        code, out, err = self.run_cli("daemon", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn(f"run #{run_id} runner OK", out)
        self.assertIn("would merge", out.lower())
        self.assertEqual(list(runs_dir.glob("*.md")), [])

        conn = db.connect()
        try:
            self.assertEqual(
                board.get_plan(conn, "demo", "1048")["state"], "VERIFYING"
            )
        finally:
            conn.close()

    def test_daemon_dry_run_without_db_prints_nothing(self):
        database = pathlib.Path(self.tmp.name) / "swarm.db"
        self.assertFalse(database.exists())
        code, out, err = self.run_cli("daemon", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertFalse(database.exists())

    def test_project_auto_requires_guard_and_verify(self):
        self.init()
        repo = self.make_repo()
        code, _, err = self.run_cli(
            "project", "add", "demo", "--repo", str(repo), "--branch", "main"
        )
        self.assertEqual(code, 0, err)

        code, _, err = self.run_cli("project", "set", "demo", "--auto", "on")
        self.assertEqual(code, 1)
        self.assertIn(
            "install the push guard first: swarm project guard demo", err
        )

        code, out, err = self.run_cli("project", "guard", "demo")
        self.assertEqual(code, 0, err)
        self.assertIn("hooks/pre-push", out)

        code, _, err = self.run_cli("project", "set", "demo", "--auto", "on")
        self.assertEqual(code, 1)
        self.assertIn("set --verify-cmd and --verify-allow first", err)
        self.assertIn("swarm connect suggests them", err)

        code, _, err = self.run_cli(
            "project", "set", "demo", "--auto", "on", "--verify-cmd", "true",
            "--verify-allow", "Bash(true:*)",
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("project", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("auto on", out)

        code, _, err = self.run_cli("project", "set", "demo", "--auto", "off")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("project", "list")
        self.assertIn("auto off", out)

    def test_project_set_implementer_and_rules(self):
        self.init()
        self.project()
        code, _, err = self.run_cli(
            "project", "set", "demo", "--implementer", "claude",
            "--rule", "never force push",
        )
        self.assertEqual(code, 0, err)
        conn = db.connect()
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["implementer"], "claude")
        self.assertEqual(board.project_rules(row), ["never force push"])

        code, out, err = self.run_cli("project", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("implementer claude", out)

        code, _, err = self.run_cli("project", "set", "demo", "--no-rules")
        self.assertEqual(code, 0, err)
        row = conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(board.project_rules(row), [])

        code, _, err = self.run_cli(
            "project", "set", "demo", "--rule", "x", "--no-rules"
        )
        self.assertEqual(code, 1)
        self.assertIn("--rule and --no-rules cannot be combined", err)

    def write_runner(self, text, name="demo"):
        directory = pathlib.Path(self.tmp.name) / "projects"
        directory.mkdir(exist_ok=True)
        (directory / f"{name}.toml").write_text(text, encoding="utf-8")

    def test_runner_check_prints_summary(self):
        self.init()
        self.project()
        self.write_runner(
            'safety_stop = "verified"\n'
            '[steps.build]\ncmd = "make release"\ntimeout = "30m"\n'
            'locks = ["release-build"]\n'
            '[steps.boot_test]\ncmd = "./boot.sh"\ntimeout = "10m"\n'
            '[windows.soak]\nbefore = ["build", "boot_test"]\n'
            'start = "./run"\nlength = "2h"\nread = "./read"\n'
        )
        code, out, err = self.run_cli("runner", "check", "demo")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(
            out.splitlines(),
            [
                "step boot_test: 10m, locks -",
                "step build: 30m, locks release-build",
                "window soak: 120m, before build, boot_test, production no",
                "safety stop: verified",
            ],
        )

    def test_runner_check_without_config(self):
        self.init()
        self.project()
        path = pathlib.Path(self.tmp.name) / "projects" / "demo.toml"
        code, out, err = self.run_cli("runner", "check", "demo")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, f"no runner config at {path}\n")

    def test_runner_check_bad_file_exits_one(self):
        self.init()
        self.project()
        self.write_runner('stop_pattern = "("\n')
        code, out, err = self.run_cli("runner", "check", "demo")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("swarm: "), err)
        self.assertIn("stop_pattern", err)

    def test_runner_check_unknown_project(self):
        self.init()
        code, _, err = self.run_cli("runner", "check", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no project nope", err)

    def test_connect_dry_run_writes_nothing(self):
        self.init()
        repo = self.make_repo()
        code, out, err = self.run_cli("connect", str(repo), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn(f"repo {repo.resolve()}", out)
        self.assertIn("branch main", out)
        self.assertIn("plans dir (will be created by the first plan)", out)
        self.assertIn("gates file none (plans go MERGED", out)
        self.assertIn("verify command none found", out)
        self.assertIn("implementer omp", out)
        self.assertIn("plan files: 0 found", out)
        self.assertIn("auto: off", out)

        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
        )
        self.assertFalse((repo / ".git" / "hooks" / "pre-push").exists())

    def test_connect_without_yes_on_non_tty_writes_nothing(self):
        self.init()
        repo = self.make_repo()
        code, out, err = self.run_cli("connect", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("rerun with --yes to connect", out)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0
        )

    def test_connect_yes_adds_project(self):
        self.init()
        repo = self.make_repo()
        code, out, err = self.run_cli(
            "connect", str(repo), "--name", "demo", "--yes"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("name demo", out)
        conn = db.connect()
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["auto"], 0)

    def test_disconnect_refuses_active_plan(self):
        self.init()
        repo = self.make_repo()
        code, _, err = self.run_cli(
            "connect", str(repo), "--name", "demo", "--yes"
        )
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli(
            "plan", "add", "demo/0001", "--title", "Busy plan"
        )
        self.assertEqual(code, 0, err)
        for state in ("REVIEW", "APPROVED", "EXECUTING"):
            code, _, err = self.run_cli(
                "plan", "move", "demo/0001", state
            )
            self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("disconnect", "demo", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("project demo has active plans: 0001", err)
        conn = db.connect()
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["archived"], 0)

    def test_disconnect_then_connect_keeps_same_project(self):
        self.init()
        repo = self.make_repo()
        code, _, err = self.run_cli(
            "connect", str(repo), "--name", "demo", "--yes"
        )
        self.assertEqual(code, 0, err)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.assertTrue(
            (repo / ".git" / "hooks" / "pre-push").is_file()
        )

        code, out, err = self.run_cli("disconnect", "demo", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("disconnected demo; history kept", out)
        row = conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["archived"], 1)
        self.assertFalse(
            (repo / ".git" / "hooks" / "pre-push").exists()
        )

        code, out, err = self.run_cli("connect", str(repo), "--yes")
        self.assertEqual(code, 0, err)
        rows = conn.execute("SELECT * FROM projects").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "demo")
        self.assertEqual(rows[0]["archived"], 0)
        events = [
            event["text"]
            for event in conn.execute("SELECT text FROM events").fetchall()
        ]
        self.assertIn("project demo disconnected", events)
        self.assertIn("project demo reconnected", events)

    def test_disconnect_without_yes_on_non_tty_writes_nothing(self):
        self.init()
        repo = self.make_repo()
        code, _, err = self.run_cli(
            "connect", str(repo), "--name", "demo", "--yes"
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("disconnect", "demo")
        self.assertEqual(code, 1)
        self.assertIn("rerun with --yes to disconnect", out)
        conn = db.connect()
        self.addCleanup(conn.close)
        row = conn.execute(
            "SELECT * FROM projects WHERE name = 'demo'"
        ).fetchone()
        self.assertEqual(row["archived"], 0)

    def test_plan_set_goal_links_and_rejects_missing(self):
        self.init()
        self.project()
        self.plan()
        code, out, err = self.run_cli(
            "goal", "add", "demo", "Ship", "--done-when", "shipped"
        )
        self.assertEqual(code, 0, err)
        goal_id = int(out.strip())

        code, _, err = self.run_cli("plan", "set", "demo/1048", "--goal", "99")
        self.assertEqual(code, 1)
        self.assertIn("swarm: no goal 99", err)

        code, _, err = self.run_cli(
            "plan", "set", "demo/1048", "--goal", str(goal_id)
        )
        self.assertEqual(code, 0, err)
        conn = db.connect()
        try:
            row = board.get_plan(conn, "demo", "1048")
            self.assertEqual(row["goal_id"], goal_id)
        finally:
            conn.close()

    def test_runs_lists_running_and_all(self):
        self.init()
        self.project()
        self.plan()
        code, out, err = self.run_cli(
            "run", "start", "architect", "--plan", "demo/1048"
        )
        self.assertEqual(code, 0, err)
        run_id = int(out.strip())

        code, out, err = self.run_cli("runs")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertTrue(line.startswith(f"{run_id}  architect  -  demo/1048"))
        self.assertIn("RUNNING", line)
        self.assertIn(str(procs.run_paths(run_id)[0]), line)

        code, _, err = self.run_cli(
            "run", "end", str(run_id), "OK", "--cost", "0.5"
        )
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("runs")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")

        code, out, err = self.run_cli("runs", "--all")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{run_id}  architect", out)
        self.assertIn("$0.50", out)

    def config(self):
        (pathlib.Path(self.tmp.name) / "telegram.toml").write_text(
            'bot_token = "123:abc"\nchat_id = "42"\n')

    def run_telegram(self, *argv, api=FakeTelegramApi):
        FakeTelegramApi.instances.clear()
        with mock.patch("swarm.telegram.TelegramApi", api):
            return self.run_cli("telegram", *argv)

    def test_once_sends_hello_and_saves_state(self):
        self.config()
        code, out, err = self.run_telegram("--once")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(FakeTelegramApi.instances), 1)
        api = FakeTelegramApi.instances[0]
        self.assertEqual(
            api.sent,
            [("42", "Swarm bot is on. Open asks: 0. /help for commands.", None)],
        )
        state = json.loads(
            (pathlib.Path(self.tmp.name) / "telegram.json").read_text())
        self.assertEqual(state["last_ask_id"], 0)
        # Taking the pid lock records an event, so the baseline is not zero.
        self.assertIsInstance(state["last_event_id"], int)
        self.assertEqual(state["offset"], 0)
        self.assertIsInstance(state["daemon_on"], bool)
        self.assertEqual(state["ask_messages"], {})

    def test_lock_refused(self):
        self.config()
        conn = db.connect()
        board.take_lock(conn, "telegram", f"pid:{os.getpid()}")
        conn.close()
        code, out, err = self.run_telegram("--once")
        self.assertEqual(code, 1)
        self.assertIn(
            f"telegram bot already running (pid {os.getpid()})", err)
        self.assertEqual(FakeTelegramApi.instances, [])

    def test_missing_config(self):
        code, out, err = self.run_telegram("--once")
        self.assertEqual(code, 1)
        self.assertIn("telegram.toml needs bot_token and chat_id", err)

    def test_conflict_is_reported(self):
        self.config()

        class ConflictApi(FakeTelegramApi):
            def get_updates(self, offset, timeout=25):
                raise telegram.TelegramError(409, "Conflict")

        code, out, err = self.run_telegram("--once", api=ConflictApi)
        self.assertEqual(code, 1)
        self.assertIn("another process is polling this bot", err)

    def test_usage_without_project_keeps_empty_commits(self):
        code, _, err = self.init()
        self.assertEqual(code, 0, err)
        root = pathlib.Path(self.tmp.name) / "claude-projects"
        root.mkdir()
        self._write_config(claude_projects_dir=str(root))

        code, out, err = self.run_cli("usage", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["commits"], {})

        code, out, err = self.run_cli("usage")
        self.assertEqual(code, 0, err)
        self.assertNotIn("commits: unavailable", out)

    def test_usage_json_and_help(self):
        code, _, err = self.init()
        self.assertEqual(code, 0, err)
        code, _, err = self.project()
        self.assertEqual(code, 0, err)

        root = pathlib.Path(self.tmp.name) / "claude-projects"
        self._write_config(claude_projects_dir=str(root))
        session = root / usage.slug(self.repo) / "session-1.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({
            "timestamp": "2026-11-17T10:00:00Z",
            "requestId": "r1",
            "entrypoint": "cli",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "opus",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 4,
                    "cache_read_input_tokens": 100,
                },
            },
        }) + "\n")

        code, out, err = self.run_cli("usage", "--project", "demo", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["summary"]["total"]["weighted"], 35.0)
        self.assertIsNone(data["subagents"])
        self.assertIsNone(data["commits"])

        agent = (
            root / usage.slug(self.repo) / "session-1" / "subagents"
            / "agent-x.jsonl"
        )
        agent.parent.mkdir(parents=True)
        lines = [
            {
                "timestamp": "2026-11-16T09:00:00Z",
                "requestId": f"r{index}",
                "message": {
                    "id": f"m{index}",
                    "role": "assistant",
                    "model": "sonnet",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 4,
                        "cache_read_input_tokens": 100,
                    },
                },
            }
            for index in (2, 3)
        ]
        agent.write_text("".join(json.dumps(line) + "\n" for line in lines))
        (agent.with_name("agent-x.meta.json")).write_text(json.dumps({
            "agentType": "general-purpose",
            "description": "Review plan 0012",
        }))

        code, out, err = self.run_cli(
            "usage", "--project", "demo", "--subagents", "--json"
        )
        self.assertEqual(code, 0, err)
        subagents = json.loads(out)["subagents"]
        self.assertEqual(len(subagents), 1)
        self.assertEqual(subagents[0]["session"], "session-1")
        self.assertEqual(subagents[0]["agent"], "agent-x")
        self.assertEqual(subagents[0]["model"], "sonnet")
        self.assertEqual(subagents[0]["calls"], 2)
        self.assertEqual(subagents[0]["weighted"], 70.0)
        self.assertEqual(subagents[0]["description"], "Review plan 0012")

        code, out, err = self.run_cli("usage", "--help")
        self.assertEqual(code, 0, err)
        for flag in ("--project", "--since", "--until", "--subagents", "--json"):
            self.assertIn(flag, out)


if __name__ == "__main__":
    unittest.main()
