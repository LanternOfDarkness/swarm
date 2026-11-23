import os
import tempfile
import time
import unittest
from pathlib import Path

from swarm import config, procs, runwrap


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class RunwrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "run.log"
        self.exit_file = self.dir / "run.exit"

    def tearDown(self):
        self.tmp.cleanup()

    def _argv(self, *command):
        return ["--log", str(self.log), "--exit-file", str(self.exit_file),
                "--cwd", str(self.dir), "--", *command]

    def test_log_and_exit_code(self):
        code = runwrap.main(self._argv("sh", "-c", "echo hi; exit 3"))
        self.assertEqual(code, 3)
        self.assertIn("hi", self.log.read_text())
        self.assertEqual(self.exit_file.read_text().strip(), "3")

    def test_unstartable_command_writes_127(self):
        code = runwrap.main(self._argv("/nonexistent/no-such-command"))
        self.assertEqual(code, 127)
        self.assertEqual(self.exit_file.read_text().strip(), "127")


class ProcsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SWARM_HOME")
        os.environ["SWARM_HOME"] = self.tmp.name
        self.cwd = Path(self.tmp.name) / "cwd"
        self.cwd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()
        if self._old_home is None:
            os.environ.pop("SWARM_HOME", None)
        else:
            os.environ["SWARM_HOME"] = self._old_home

    def test_run_paths_created(self):
        log, exit_file, report = procs.run_paths("42")
        self.assertEqual(log, Path(self.tmp.name) / "runs" / "42.log")
        self.assertEqual(exit_file, Path(self.tmp.name) / "runs" / "42.exit")
        self.assertEqual(report, Path(self.tmp.name) / "runs" / "42.md")
        self.assertTrue(log.parent.is_dir())
        self.assertIsNone(procs.read_exit("42"))
        exit_file.write_text("3\n")
        self.assertEqual(procs.read_exit("42"), 3)

    def test_launch_and_reap(self):
        launcher = procs.Launcher()
        pid = launcher.launch("1", ["sh", "-c", "echo hi; exit 3"], self.cwd, os.environ)
        self.assertGreater(pid, 0)
        self.assertTrue(_wait_for(lambda: not launcher.alive(pid)))
        log, _, _ = procs.run_paths("1")
        self.assertIn("hi", log.read_text())
        self.assertEqual(procs.read_exit("1"), 3)

    def test_kill_sends_sigterm_and_writes_exit(self):
        marker = self.cwd / "started"
        launcher = procs.Launcher()
        pid = launcher.launch(
            "2", ["sh", "-c", "touch started; exec sleep 30"], self.cwd, os.environ)
        self.assertTrue(_wait_for(marker.exists))
        self.assertTrue(launcher.alive(pid))
        launcher.kill(pid)
        self.assertTrue(_wait_for(lambda: not launcher.alive(pid), timeout=5.0))
        _, exit_file, _ = procs.run_paths("2")
        self.assertTrue(_wait_for(exit_file.exists))
        self.assertIsNotNone(procs.read_exit("2"))

    def test_worker_env(self):
        cfg = config.Config(worker_env={"CARGO_BUILD_JOBS": "2"})
        old_pm = os.environ.get("SWARM_PM")
        old_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["SWARM_PM"] = "x"
        os.environ["ANTHROPIC_API_KEY"] = "y"
        try:
            env = procs.worker_env(cfg, 7)
        finally:
            for name, old in (("SWARM_PM", old_pm), ("ANTHROPIC_API_KEY", old_key)):
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
        self.assertEqual(env["SWARM_WORKER"], "1")
        self.assertEqual(env["SWARM_RUN_ID"], "7")
        self.assertEqual(env["CARGO_BUILD_JOBS"], "2")
        self.assertNotIn("SWARM_PM", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_alive_rejects_bad_pids(self):
        launcher = procs.Launcher()
        self.assertFalse(launcher.alive(0))
        self.assertFalse(launcher.alive(-1))
        self.assertFalse(launcher.alive(None))
        launcher.kill(0)
        launcher.kill(-1)
        launcher.kill(None)


if __name__ == "__main__":
    unittest.main()
